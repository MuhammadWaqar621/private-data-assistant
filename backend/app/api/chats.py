"""
Chat CRUD endpoints - create/list/get/delete chats, read their message
history, and set or clear which database connection a chat is bound to.

All endpoints require a valid access token (get_current_user) and every
lookup filters by the current user's id, which is what keeps one user's
chats invisible to another. A chat that exists but belongs to someone else
404s, indistinguishable from one that was never created.

`connection_id` is the one field here with a security dimension: PATCHing
it verifies the target connection is the CALLER'S OWN (404 otherwise), so
a user can never point a chat at another account's registered database.
It may also be set to null, which unbinds the chat - greetings and product
questions still work with no database selected (see
app/engine/rag.py's AGENT_SYSTEM_PROMPT).

Sending a message and streaming an assistant reply lives in
app/api/messages.py, not here.
"""

from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models import Chat, ConnectionStatus, DatabaseConnection, MessageRole, User

DEFAULT_TITLE = "New chat"

router = APIRouter(prefix="/api/chats", tags=["chats"])


# --- Schemas -----------------------------------------------------------------


class ChatCreate(BaseModel):
    title: Optional[str] = None
    connection_id: Optional[int] = None


class ChatUpdate(BaseModel):
    """Both fields are optional AND nullable, which are different things
    here - so presence is detected via `model_fields_set` rather than by
    checking for None. `{"connection_id": null}` clears the binding;
    omitting the key leaves it alone."""

    title: Optional[str] = None
    connection_id: Optional[int] = None


class ChatOut(BaseModel):
    id: int
    title: str
    connection_id: Optional[int]
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: int
    role: MessageRole
    content: str
    # The query that actually ran for this turn (null when no query was
    # needed) and the chart that was rendered from its rows, both persisted
    # so reloading history shows exactly what the user saw live - see
    # app/models/message.py.
    query_sql: Optional[str] = None
    chart_spec: Optional[Dict[str, Any]] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class ChatDetailOut(ChatOut):
    messages: list[MessageOut]


# --- Helpers -------------------------------------------------------------


def _get_owned_chat(db: Session, chat_id: int, user: User) -> Chat:
    chat = db.get(Chat, chat_id)
    if chat is None or chat.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chat not found")
    return chat


def _require_owned_connection(db: Session, connection_id: int, user: User) -> None:
    """404 (not 403) if the connection isn't this user's - same
    ownership-hides-existence rule used everywhere else."""
    connection = db.get(DatabaseConnection, connection_id)
    if connection is None or connection.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found"
        )


# --- Endpoints ---------------------------------------------------------------


@router.post("", response_model=ChatOut, status_code=status.HTTP_201_CREATED)
def create_chat(
    body: ChatCreate,
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Chat:
    connection_id = body.connection_id
    if connection_id is not None:
        _require_owned_connection(db, connection_id, current_user)
    else:
        # If this user has registered exactly one database, there is
        # nothing to choose between - default every new chat to it rather
        # than making them open the selector and pick the only option.
        # Only `ready` connections count: a `pending`/`indexing`/`failed`
        # one isn't usable yet, so defaulting to it would just trade one
        # piece of friction for a confusing "why doesn't this work" one.
        # As soon as a second connection exists, this stops applying and
        # new chats go back to starting unbound, same as always.
        ready_connections = (
            db.query(DatabaseConnection)
            .filter(
                DatabaseConnection.user_id == current_user.id,
                DatabaseConnection.status == ConnectionStatus.ready,
            )
            .limit(2)
            .all()
        )
        if len(ready_connections) == 1:
            connection_id = ready_connections[0].id

    # At most one untitled, unbound, empty chat per user at a time: reuse
    # an existing one instead of stacking duplicates when "+ New chat" is
    # clicked repeatedly. Checked against the database, so it can't go
    # stale the way a client-side check could. Matched against the
    # RESOLVED connection_id (post auto-default), not body.connection_id -
    # an existing empty chat only counts as reusable if it already has the
    # same binding a fresh one would get, auto-default included.
    if body.title is None and body.connection_id is None:
        existing_empty = (
            db.query(Chat)
            .filter(
                Chat.user_id == current_user.id,
                Chat.title == DEFAULT_TITLE,
                Chat.connection_id == connection_id,
            )
            .filter(~Chat.messages.any())
            .order_by(Chat.created_at.desc())
            .first()
        )
        if existing_empty is not None:
            response.status_code = status.HTTP_200_OK
            return existing_empty

    chat = Chat(
        user_id=current_user.id,
        title=body.title or DEFAULT_TITLE,
        connection_id=connection_id,
    )
    db.add(chat)
    db.commit()
    db.refresh(chat)
    return chat


@router.get("", response_model=list[ChatOut])
def list_chats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> list[Chat]:
    return (
        db.query(Chat)
        .filter(Chat.user_id == current_user.id)
        .order_by(Chat.created_at.desc(), Chat.id.desc())
        .all()
    )


@router.get("/{chat_id}", response_model=ChatDetailOut)
def get_chat(
    chat_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Chat:
    # chat.messages is already ordered by created_at (see Chat.messages
    # relationship's order_by), so no extra query is needed here.
    return _get_owned_chat(db, chat_id, current_user)


@router.patch("/{chat_id}", response_model=ChatOut)
def update_chat(
    chat_id: int,
    body: ChatUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Chat:
    """Rename a chat and/or set/change/clear which database it queries.

    Sending `{"connection_id": 7}` binds it to connection 7 (404 unless
    that connection is the caller's own); `{"connection_id": null}`
    unbinds it; omitting the key entirely leaves the current binding
    untouched."""
    chat = _get_owned_chat(db, chat_id, current_user)
    provided = body.model_fields_set

    if "title" in provided:
        title = (body.title or "").strip()
        if not title:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Title must not be blank.",
            )
        chat.title = title

    if "connection_id" in provided:
        if body.connection_id is None:
            chat.connection_id = None
        else:
            _require_owned_connection(db, body.connection_id, current_user)
            chat.connection_id = body.connection_id

    db.commit()
    db.refresh(chat)
    return chat


@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat(
    chat_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> None:
    chat = _get_owned_chat(db, chat_id, current_user)
    db.delete(chat)  # cascades to Message rows via the relationship + FK ondelete
    db.commit()
