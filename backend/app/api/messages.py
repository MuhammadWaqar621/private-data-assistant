"""
Message-sending endpoint: persist a user message, then stream back an
assistant reply from the agentic text-to-query pipeline
(app/engine/rag.py's stream_agentic_reply), executing any query the model
asks for against the user's own registered database.

This is one of the two places (with app/api/connections.py) that touches
both the DB/auth stack and app/engine/* - and it is the ONLY place that
ever executes a model-authored query. It checks auth/ownership, persists
the user Message, decrypts the connection's credentials, drives the
engine's generator handshake, runs each requested query through the
adapter's read-only path, streams events back as Server-Sent Events, and
persists the assistant Message (content + query_sql + chart_spec) when the
turn finishes.

--------------------------------------------------------------------------
THE INVARIANT THIS ENDPOINT EXISTS TO PROTECT
--------------------------------------------------------------------------
`user_id`, `connection_id` and `engine_name` are read from the
authenticated request and the owned DatabaseConnection row, and captured as
PLAIN VALUES before the streaming generator starts running. Two separate
reasons, both real:

  1. Security. The model's only inputs to a query are the free-text
     `query` string and the chart's labels. It has no tool parameter for a
     user id, a connection id, or an engine - so nothing in the request
     body, the schema context, a table name, or the conversation itself can
     redirect a query at a different account's database. The connection is
     also re-verified as the caller's own on every message, even though
     `chat.connection_id` could only have been set through an
     ownership-checked PATCH - defense in depth against a future code path
     that forgets.
  2. Correctness. `event_stream()` is a lazy generator that only executes
     once StreamingResponse begins streaming, by which point FastAPI has
     closed the request-scoped `db` session and detached `current_user` /
     `chat` - touching any ORM attribute at that point raises
     DetachedInstanceError. So everything the stream needs (including the
     decrypted ConnectionInfo) is materialized first.

Schema retrieval reads Qdrant ONLY (app/engine/schema_rag.py): sending a
message never re-introspects the live database, and never touches the
user's data at all unless and until the model actually calls run_query.
"""

import json
from typing import Any, AsyncGenerator, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.connections import build_connection_info
from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.crypto import DecryptionError, EncryptionNotConfiguredError
from app.db.session import SessionLocal, get_db
from app.engine import schema_rag
from app.engine.azure_client import ai_configured
from app.engine.db_adapters import get_adapter
from app.engine.db_adapters.base import ConnectionInfo
from app.engine.db_adapters.errors import AdapterError
from app.engine.llm_provider import get_llm_provider_name
from app.engine.rag import stream_agentic_reply
from app.models import Chat, DatabaseConnection, Message, MessageRole, User

router = APIRouter(prefix="/api/chats/{chat_id}/messages", tags=["messages"])

_TITLE_MAX_LENGTH = 50
_DEFAULT_TITLE = "New chat"


class MessageCreate(BaseModel):
    content: str


def _get_owned_chat(db: Session, chat_id: int, user: User) -> Chat:
    chat = db.get(Chat, chat_id)
    if chat is None or chat.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
    return chat


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _derive_title(content: str) -> str:
    """A short chat title from the first message's content, truncated at a
    word boundary rather than mid-word."""
    text = content.strip().splitlines()[0]
    if len(text) <= _TITLE_MAX_LENGTH:
        return text
    truncated = text[:_TITLE_MAX_LENGTH].rsplit(" ", 1)[0]
    return (truncated or text[:_TITLE_MAX_LENGTH]).rstrip() + "..."


def execute_read_only_query(
    engine_name: str,
    info: ConnectionInfo,
    query: str,
    max_rows: int,
    timeout_seconds: int,
) -> Dict[str, Any]:
    """Run one model-authored query and return the plain result dict the
    engine's handshake expects.

    Never raises: a read-only violation, a database error, or an
    unexpected driver failure all come back as `{"ok": False, "error":
    "..."}` so the model can be told what went wrong and try again, rather
    than the whole stream dying. The error text always comes from
    app/engine/db_adapters/errors.py's already-humanized messages - never
    a raw traceback (see that module's docstring)."""
    try:
        adapter = get_adapter(engine_name)
        columns, rows = adapter.execute_read_only(
            info, query, max_rows=max_rows, timeout_seconds=timeout_seconds
        )
    except AdapterError as exc:
        # NotReadOnlyError / QueryExecutionError / ConnectionFailedError /
        # UnsupportedEngineError - all already safe to show a user.
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - last-resort net, don't leak internals
        return {
            "ok": False,
            "error": f"The query could not be run ({exc.__class__.__name__}).",
        }

    return {
        "ok": True,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        # The adapter caps at max_rows, so a full page back means there may
        # be more - the model is told so it can say so.
        "truncated": len(rows) >= max_rows,
        "error": None,
    }


@router.post("")
async def send_message(
    chat_id: int,
    body: MessageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    settings = get_settings()
    chat = _get_owned_chat(db, chat_id, current_user)

    if not ai_configured():
        provider = get_llm_provider_name()
        chat_hint = "LLM_ENDPOINT*" if provider == "azure" else "GROQ_API_KEY"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "ai_not_configured",
                "message": (
                    "AI is not configured. Set AZURE_EM_* (embeddings) and "
                    f"{chat_hint} (chat, provider={provider}) in .env."
                ),
            },
        )

    if not body.content.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Message content cannot be empty"
        )

    # --- resolve the connection (may be none) -----------------------------
    # Captured as plain values; see the module docstring's invariant.
    user_id: int = current_user.id
    connection_id: Optional[int] = chat.connection_id
    engine_name: Optional[str] = None
    connection_info: Optional[ConnectionInfo] = None

    if connection_id is not None:
        connection = db.get(DatabaseConnection, connection_id)
        # Defense in depth: this could only have been set through an
        # ownership-checked PATCH, but a 404 here costs nothing and closes
        # the hole permanently.
        if connection is None or connection.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found"
            )
        engine_name = connection.engine.value
        try:
            connection_info = build_connection_info(connection)
        except (DecryptionError, EncryptionNotConfiguredError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "credentials_unavailable", "message": str(exc)},
            )

    # --- schema context (Qdrant only - never the live database) -----------
    schema_context = ""
    if connection_id is not None:
        try:
            schema_context = await run_in_threadpool(
                schema_rag.retrieve_relevant_schema,
                user_id,
                connection_id,
                body.content,
            )
        except Exception:  # noqa: BLE001 - a Qdrant/embedding hiccup must not
            # kill the turn: the model is told it has no schema context and
            # will say so rather than guessing table names.
            schema_context = ""

    # Prior turns as plain dicts (never ORM objects) - app.engine.rag never
    # sees a SQLAlchemy Message, satisfying the engine's isolation contract.
    history = [{"role": m.role.value, "content": m.content} for m in chat.messages]

    # Auto-title from the first message, the way ChatGPT/Claude do - only
    # when this is genuinely the first message AND the chat still has the
    # generic default title.
    if not history and chat.title == _DEFAULT_TITLE:
        chat.title = _derive_title(body.content)
        db.add(chat)

    db.add(Message(chat_id=chat_id, role=MessageRole.user, content=body.content))
    db.commit()

    question = body.content
    max_rows = int(settings.MAX_QUERY_ROWS)
    timeout_seconds = int(settings.QUERY_TIMEOUT_SECONDS)

    async def event_stream() -> AsyncGenerator[str, None]:
        final_content = ""
        streamed_text = ""
        query_sql: Optional[str] = None
        chart_spec: Optional[Dict[str, Any]] = None

        agen = stream_agentic_reply(
            user_id=user_id,
            connection_id=connection_id,
            engine_name=engine_name,
            chat_history=history,
            message=question,
            schema_context=schema_context,
        )

        # The generator handshake (see app/engine/__init__.py): driven with
        # asend() rather than `async for`, because a tool_call event has to
        # be answered with the query's real result.
        to_send: Optional[Dict[str, Any]] = None
        try:
            while True:
                try:
                    event = await agen.asend(to_send)
                except StopAsyncIteration:
                    break
                to_send = None

                kind = event.get("type")

                if kind == "token":
                    streamed_text += event.get("text", "")
                    yield _sse("token", {"content": event.get("text", "")})

                elif kind == "tool_call":
                    # The ONLY place a model-authored query is executed.
                    # engine_name / connection_info come from this
                    # endpoint's captured values - the event's echoed ids
                    # are never trusted as input.
                    query = event.get("query", "")
                    query_sql = query
                    yield _sse("query", {"query": query})
                    if connection_info is None or engine_name is None:
                        to_send = {
                            "ok": False,
                            "error": "No database is connected to this chat.",
                        }
                    else:
                        to_send = await run_in_threadpool(
                            execute_read_only_query,
                            engine_name,
                            connection_info,
                            query,
                            max_rows,
                            timeout_seconds,
                        )
                        yield _sse(
                            "query_result",
                            {
                                "ok": bool(to_send.get("ok")),
                                "row_count": to_send.get("row_count", 0),
                                "error": to_send.get("error"),
                            },
                        )

                elif kind == "chart":
                    chart_spec = {
                        "chart_type": event.get("chart_type"),
                        "title": event.get("title"),
                        "x_field": event.get("x_field"),
                        "y_field": event.get("y_field"),
                        "columns": event.get("columns", []),
                        "rows": event.get("rows", []),
                    }
                    yield _sse("chart", chart_spec)

                elif kind == "done":
                    final_content = event.get("content") or streamed_text
                    query_sql = event.get("query_sql", query_sql)
                    chart_spec = event.get("chart_spec", chart_spec)

                elif kind == "error":
                    yield _sse("error", {"message": event.get("message", "Something failed.")})

        except Exception as exc:  # noqa: BLE001 - surface it over SSE, don't hang up
            yield _sse("error", {"message": str(exc)})
        finally:
            await agen.aclose()

        content_to_persist = final_content or streamed_text
        if content_to_persist:
            # A fresh session (rather than the request-scoped `db` above):
            # FastAPI's yield-dependency cleanup for `db` is tied to the
            # request handler's scope, which is a subtler lifetime to reason
            # about once a StreamingResponse is involved - a short-lived
            # session here sidesteps that entirely.
            write_db = SessionLocal()
            try:
                write_db.add(
                    Message(
                        chat_id=chat_id,
                        role=MessageRole.assistant,
                        content=content_to_persist,
                        query_sql=query_sql,
                        chart_spec=chart_spec,
                    )
                )
                write_db.commit()
            finally:
                write_db.close()

        yield _sse("done", {"query_sql": query_sql, "chart_spec": chart_spec})

    return StreamingResponse(event_stream(), media_type="text/event-stream")
