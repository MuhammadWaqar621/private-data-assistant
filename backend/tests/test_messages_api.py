"""
Integration tests for POST /api/chats/{chat_id}/messages.

`stream_agentic_reply` is replaced by a scripted fake async generator that
speaks the same handshake protocol (see app/engine/__init__.py), so no
Azure/Groq call is ever made and the exact sequence of events can be
controlled. What's under test is the ENDPOINT's own behavior:

  - the isolation invariant: user_id / connection_id / engine_name are
    taken from the authenticated request and the owned connection row -
    never from the request body, and never from anything the model emits;
  - the query executor is called with the endpoint's own engine and
    decrypted credentials;
  - SSE plumbing (token / query / query_result / chart / error / done);
  - persistence of the assistant turn, including query_sql and chart_spec;
  - the 503/404/400 gates.

`app.api.messages.SessionLocal` is pointed at the test database, because
the final assistant-message write deliberately uses a fresh session rather
than the request-scoped one (see that module's comment) - without this the
write would target the app's real engine and be invisible here.
"""

import json

import pytest
from sqlalchemy.orm import Session, sessionmaker

import app.api.messages as messages_module
from app.models import (
    ConnectionStatus,
    DatabaseConnection,
    DatabaseEngine,
    Message,
    MessageRole,
    User,
)

from .conftest import auth_headers, signup

DB_PASSWORD = "connection-password-1234"


@pytest.fixture(autouse=True)
def ai_ready(monkeypatch):
    monkeypatch.setattr(messages_module, "ai_configured", lambda: True)


@pytest.fixture(autouse=True)
def write_session(db_engine, monkeypatch):
    """Point the endpoint's out-of-request write session at the test DB."""
    monkeypatch.setattr(
        messages_module,
        "SessionLocal",
        sessionmaker(autocommit=False, autoflush=False, bind=db_engine),
    )


@pytest.fixture(autouse=True)
def no_schema_lookup(monkeypatch):
    """Schema retrieval hits Postgres (pgvector) + Azure; stub it and
    record the args so tests can assert it is scoped to the caller."""
    calls = []

    def _retrieve(user_id, connection_id, question, *args, **kwargs):
        calls.append({"user_id": user_id, "connection_id": connection_id, "question": question})
        return "Table: orders\nColumns:\n  - id (INTEGER) [primary key, not null]"

    monkeypatch.setattr(messages_module.schema_rag, "retrieve_relevant_schema", _retrieve)
    return calls


def _insert_connection(db_engine, email, engine=DatabaseEngine.postgres):
    from app.core.crypto import encrypt_secret

    with Session(db_engine) as session:
        user = session.query(User).filter(User.email == email).one()
        connection = DatabaseConnection(
            user_id=user.id,
            name="Prod",
            engine=engine,
            host="db.example.com",
            port=5432,
            database_name="analytics",
            username="reader",
            encrypted_password=encrypt_secret(DB_PASSWORD),
            extra_params={"ssl_mode": "require"},
            status=ConnectionStatus.ready,
        )
        session.add(connection)
        session.commit()
        return connection.id


def _create_chat(client, user, **payload):
    response = client.post("/api/chats", json=payload, headers=auth_headers(user))
    assert response.status_code in (200, 201), response.text
    return response.json()


def _script_agent(monkeypatch, events, recorder=None):
    """Install a fake stream_agentic_reply that yields `events` in order and
    accepts a sent-back result after each tool_call - the same protocol the
    real engine uses."""
    recorder = recorder if recorder is not None else []

    async def _fake(**kwargs):
        recorder.append({"call": kwargs})
        for event in events:
            if event.get("type") == "tool_call":
                result = yield event
                recorder.append({"tool_result": result})
            else:
                yield event

    monkeypatch.setattr(messages_module, "stream_agentic_reply", _fake)
    return recorder


def _sse_events(text):
    """Parse a raw SSE body into [(event_name, data_dict), ...]."""
    parsed = []
    for block in text.split("\n\n"):
        if not block.strip():
            continue
        name = None
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:") :].strip())
        if name is not None:
            parsed.append((name, data))
    return parsed


def _send(client, user, chat_id, content="how many orders?"):
    return client.post(
        f"/api/chats/{chat_id}/messages",
        json={"content": content},
        headers=auth_headers(user),
    )


# --- streaming basics ---------------------------------------------------------


def test_a_plain_answer_streams_tokens_and_a_done_event(client, monkeypatch):
    _script_agent(
        monkeypatch,
        [
            {"type": "token", "text": "Hello"},
            {"type": "token", "text": " there!"},
            {"type": "done", "content": "Hello there!", "query_sql": None, "chart_spec": None},
        ],
    )
    user = signup(client)
    chat = _create_chat(client, user)

    response = _send(client, user, chat["id"], "hi")

    assert response.status_code == 200
    events = _sse_events(response.text)
    tokens = "".join(d["content"] for name, d in events if name == "token")
    assert tokens == "Hello there!"
    assert any(name == "done" for name, _ in events)


def test_the_assistant_reply_is_persisted(client, monkeypatch, db_engine):
    _script_agent(
        monkeypatch,
        [
            {"type": "token", "text": "Hello!"},
            {"type": "done", "content": "Hello!", "query_sql": None, "chart_spec": None},
        ],
    )
    user = signup(client)
    chat = _create_chat(client, user)

    _send(client, user, chat["id"], "hi")

    history = client.get(f"/api/chats/{chat['id']}", headers=auth_headers(user)).json()
    roles = [m["role"] for m in history["messages"]]
    assert roles == ["user", "assistant"]
    assert history["messages"][1]["content"] == "Hello!"
    assert history["messages"][1]["query_sql"] is None


def test_the_first_message_auto_titles_the_chat(client, monkeypatch):
    _script_agent(
        monkeypatch,
        [{"type": "done", "content": "ok", "query_sql": None, "chart_spec": None}],
    )
    user = signup(client)
    chat = _create_chat(client, user)
    assert chat["title"] == "New chat"

    _send(client, user, chat["id"], "What is our monthly revenue?")

    detail = client.get(f"/api/chats/{chat['id']}", headers=auth_headers(user)).json()
    assert detail["title"] == "What is our monthly revenue?"


def test_auto_title_truncates_a_long_first_message_at_a_word_boundary(client, monkeypatch):
    _script_agent(
        monkeypatch,
        [{"type": "done", "content": "ok", "query_sql": None, "chart_spec": None}],
    )
    user = signup(client)
    chat = _create_chat(client, user)

    _send(client, user, chat["id"], "word " * 30)

    title = client.get(f"/api/chats/{chat['id']}", headers=auth_headers(user)).json()["title"]
    assert title.endswith("...")
    assert len(title) <= 53


def test_auto_title_never_overwrites_an_explicit_title(client, monkeypatch):
    _script_agent(
        monkeypatch,
        [{"type": "done", "content": "ok", "query_sql": None, "chart_spec": None}],
    )
    user = signup(client)
    chat = _create_chat(client, user, title="My custom title")

    _send(client, user, chat["id"], "hello")

    detail = client.get(f"/api/chats/{chat['id']}", headers=auth_headers(user)).json()
    assert detail["title"] == "My custom title"


# --- THE isolation invariant ---------------------------------------------------


def test_the_engine_is_given_the_authenticated_user_and_the_chats_own_connection(
    client, monkeypatch, db_engine, no_schema_lookup
):
    recorder = _script_agent(
        monkeypatch,
        [{"type": "done", "content": "ok", "query_sql": None, "chart_spec": None}],
    )
    user = signup(client)
    connection_id = _insert_connection(db_engine, user["email"])
    chat = _create_chat(client, user, connection_id=connection_id)

    _send(client, user, chat["id"])

    call = recorder[0]["call"]
    assert call["connection_id"] == connection_id
    assert call["engine_name"] == "postgres"
    assert call["message"] == "how many orders?"
    with Session(db_engine) as session:
        expected_user_id = session.query(User).filter(User.email == user["email"]).one().id
    assert call["user_id"] == expected_user_id

    # Schema retrieval was scoped to exactly that (user, connection) pair.
    assert no_schema_lookup[0]["user_id"] == expected_user_id
    assert no_schema_lookup[0]["connection_id"] == connection_id


def test_extra_fields_in_the_request_body_cannot_redirect_the_query(
    client, monkeypatch, db_engine
):
    """The request body carries `content` and nothing else that matters -
    a client that invents user_id/connection_id fields is ignored."""
    recorder = _script_agent(
        monkeypatch,
        [{"type": "done", "content": "ok", "query_sql": None, "chart_spec": None}],
    )
    user = signup(client)
    connection_id = _insert_connection(db_engine, user["email"])
    chat = _create_chat(client, user, connection_id=connection_id)

    client.post(
        f"/api/chats/{chat['id']}/messages",
        json={
            "content": "hi",
            "user_id": 9999,
            "connection_id": 4242,
            "engine_name": "mssql",
        },
        headers=auth_headers(user),
    )

    call = recorder[0]["call"]
    assert call["connection_id"] == connection_id
    assert call["engine_name"] == "postgres"
    assert call["user_id"] != 9999


def test_a_chat_with_no_connection_passes_none_and_retrieves_no_schema(
    client, monkeypatch, no_schema_lookup
):
    recorder = _script_agent(
        monkeypatch,
        [{"type": "done", "content": "ok", "query_sql": None, "chart_spec": None}],
    )
    user = signup(client)
    chat = _create_chat(client, user)

    _send(client, user, chat["id"], "hi")

    call = recorder[0]["call"]
    assert call["connection_id"] is None
    assert call["engine_name"] is None
    assert call["schema_context"] == ""
    assert no_schema_lookup == []  # the schema_chunks table was never touched


def test_prior_turns_are_passed_as_plain_dicts(client, monkeypatch):
    recorder = _script_agent(
        monkeypatch,
        [{"type": "done", "content": "first", "query_sql": None, "chart_spec": None}],
    )
    user = signup(client)
    chat = _create_chat(client, user)
    _send(client, user, chat["id"], "first question")

    _script_agent(
        monkeypatch,
        [{"type": "done", "content": "second", "query_sql": None, "chart_spec": None}],
        recorder=recorder,
    )
    _send(client, user, chat["id"], "second question")

    history = recorder[-1]["call"]["chat_history"]
    assert {"role": "user", "content": "first question"} in history
    assert all(isinstance(turn, dict) for turn in history)


# --- executing a query the model asked for -------------------------------------


def test_a_tool_call_is_executed_with_the_endpoints_own_engine_and_credentials(
    client, monkeypatch, db_engine
):
    executed = []

    def _fake_execute(engine_name, info, query, max_rows, timeout_seconds):
        executed.append(
            {
                "engine_name": engine_name,
                "info": info,
                "query": query,
                "max_rows": max_rows,
                "timeout_seconds": timeout_seconds,
            }
        )
        return {
            "ok": True,
            "columns": ["n"],
            "rows": [[42]],
            "row_count": 1,
            "truncated": False,
            "error": None,
        }

    monkeypatch.setattr(messages_module, "execute_read_only_query", _fake_execute)
    recorder = _script_agent(
        monkeypatch,
        [
            # Note the deliberately bogus ids echoed in the event - the
            # endpoint must ignore them and use its own captured values.
            {
                "type": "tool_call",
                "name": "run_query",
                "query": "SELECT count(*) FROM orders",
                "user_id": 31337,
                "connection_id": 31337,
                "engine": "mssql",
            },
            {"type": "token", "text": "42 orders."},
            {
                "type": "done",
                "content": "42 orders.",
                "query_sql": "SELECT count(*) FROM orders",
                "chart_spec": None,
            },
        ],
    )

    user = signup(client)
    connection_id = _insert_connection(db_engine, user["email"])
    chat = _create_chat(client, user, connection_id=connection_id)

    response = _send(client, user, chat["id"])

    assert len(executed) == 1
    assert executed[0]["engine_name"] == "postgres"  # NOT the event's "mssql"
    assert executed[0]["query"] == "SELECT count(*) FROM orders"
    assert executed[0]["info"].password == DB_PASSWORD  # decrypted at the last moment
    assert executed[0]["info"].host == "db.example.com"
    assert executed[0]["max_rows"] == 200
    assert executed[0]["timeout_seconds"] == 15

    # The real result was handed back into the generator.
    assert recorder[-1]["tool_result"]["rows"] == [[42]]

    # And the client saw the query + its outcome.
    events = dict((name, data) for name, data in _sse_events(response.text))
    assert events["query"]["query"] == "SELECT count(*) FROM orders"
    assert events["query_result"]["ok"] is True
    assert events["query_result"]["row_count"] == 1


def test_the_executed_query_and_chart_are_persisted_on_the_assistant_message(
    client, monkeypatch, db_engine
):
    monkeypatch.setattr(
        messages_module,
        "execute_read_only_query",
        lambda *a, **k: {
            "ok": True,
            "columns": ["status", "n"],
            "rows": [["shipped", 9]],
            "row_count": 1,
            "truncated": False,
            "error": None,
        },
    )
    chart = {
        "type": "chart",
        "chart_type": "bar",
        "title": "Orders by status",
        "x_field": "status",
        "y_field": "n",
        "columns": ["status", "n"],
        "rows": [["shipped", 9]],
    }
    _script_agent(
        monkeypatch,
        [
            {"type": "tool_call", "name": "run_query", "query": "SELECT status, count(*) n FROM orders GROUP BY status"},
            chart,
            {"type": "token", "text": "Mostly shipped."},
            {
                "type": "done",
                "content": "Mostly shipped.",
                "query_sql": "SELECT status, count(*) n FROM orders GROUP BY status",
                "chart_spec": {k: v for k, v in chart.items() if k != "type"},
            },
        ],
    )

    user = signup(client)
    connection_id = _insert_connection(db_engine, user["email"])
    chat = _create_chat(client, user, connection_id=connection_id)

    response = _send(client, user, chat["id"])

    events = dict((name, data) for name, data in _sse_events(response.text))
    assert events["chart"]["rows"] == [["shipped", 9]]

    with Session(db_engine) as session:
        assistant = (
            session.query(Message)
            .filter(Message.chat_id == chat["id"], Message.role == MessageRole.assistant)
            .one()
        )
        assert assistant.content == "Mostly shipped."
        assert "GROUP BY status" in assistant.query_sql
        assert assistant.chart_spec["rows"] == [["shipped", 9]]

    # And it survives a history reload, so the UI can re-render the chart.
    reloaded = client.get(f"/api/chats/{chat['id']}", headers=auth_headers(user)).json()
    assert reloaded["messages"][1]["chart_spec"]["chart_type"] == "bar"


def test_a_tool_call_with_no_connection_is_answered_with_an_error_result(
    client, monkeypatch
):
    """Belt and braces: the engine already withholds the tools when no
    database is bound, but if a tool_call arrives anyway the endpoint must
    hand back a failure rather than trying to execute against nothing."""
    recorder = _script_agent(
        monkeypatch,
        [
            {"type": "tool_call", "name": "run_query", "query": "SELECT 1"},
            {"type": "done", "content": "Connect a database first.", "query_sql": None, "chart_spec": None},
        ],
    )
    user = signup(client)
    chat = _create_chat(client, user)

    _send(client, user, chat["id"])

    assert recorder[-1]["tool_result"]["ok"] is False
    assert "No database is connected" in recorder[-1]["tool_result"]["error"]


def test_an_error_event_reaches_the_client_as_an_sse_error(client, monkeypatch):
    _script_agent(
        monkeypatch,
        [
            {"type": "error", "message": "The AI provider failed: boom"},
            {"type": "done", "content": "", "query_sql": None, "chart_spec": None},
        ],
    )
    user = signup(client)
    chat = _create_chat(client, user)

    response = _send(client, user, chat["id"])

    events = dict((name, data) for name, data in _sse_events(response.text))
    assert "boom" in events["error"]["message"]


# --- gates ---------------------------------------------------------------------


def test_503_when_ai_is_not_configured(client, monkeypatch):
    monkeypatch.setattr(messages_module, "ai_configured", lambda: False)
    user = signup(client)
    chat = _create_chat(client, user)

    response = _send(client, user, chat["id"])

    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "ai_not_configured"


def test_400_for_an_empty_message(client, monkeypatch):
    _script_agent(monkeypatch, [])
    user = signup(client)
    chat = _create_chat(client, user)

    response = _send(client, user, chat["id"], "   ")

    assert response.status_code == 400


def test_404_for_another_users_chat(client, monkeypatch):
    _script_agent(monkeypatch, [])
    owner = signup(client)
    intruder = signup(client)
    chat = _create_chat(client, owner)

    response = _send(client, intruder, chat["id"])

    assert response.status_code == 404


def test_messages_endpoint_requires_authentication(client):
    response = client.post("/api/chats/1/messages", json={"content": "hi"})
    assert response.status_code == 401
