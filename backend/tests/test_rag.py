"""
Unit tests for app/engine/rag.py's agentic loop and its generator
handshake with the API layer.

Real chat-completion calls are never made: `get_active_chat_provider()` is
monkeypatched to return a fake async client whose
`chat.completions.create()` records the `messages`/`tools` it was called
with and returns a canned async-iterable "stream" shaped like a real
OpenAI/Groq streaming response.

The three properties these tests exist to pin down:

  1. A greeting is answered directly - no tool call, no query, one
     completion.
  2. When the model DOES request a query, the caller (not the model)
     supplies user_id / connection_id / engine_name; the model's only
     contribution is the query text.
  3. `render_chart` charts the rows the backend held from the last
     `run_query` - the tool has no data parameter, so a model that tries to
     supply different numbers cannot.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest

from app.engine import rag as rag_module
from app.engine.llm_provider import ActiveChatProvider


# --- Fake streaming plumbing -------------------------------------------------


@dataclass
class _FakeToolCallFunction:
    name: Optional[str]
    arguments: Optional[str]


@dataclass
class _FakeToolCall:
    index: int
    id: Optional[str]
    function: Optional[_FakeToolCallFunction]


@dataclass
class _FakeDelta:
    content: Optional[str] = None
    tool_calls: Optional[List[_FakeToolCall]] = None


@dataclass
class _FakeChoice:
    delta: _FakeDelta


@dataclass
class _FakeEvent:
    choices: List[_FakeChoice]


class _FakeEventStream:
    def __init__(self, events: List[_FakeEvent]):
        self._events = events

    def __aiter__(self):
        return self._iterator()

    async def _iterator(self):
        for event in self._events:
            yield event


class _MultiCallChatCompletions:
    """Returns a DIFFERENT canned stream on each successive call, so a
    multi-round tool loop can be simulated."""

    def __init__(self, responses: List[List[_FakeEvent]]):
        self.calls: List[dict] = []
        self._responses = responses

    async def create(self, model, messages, stream, tools=None, tool_choice=None):
        self.calls.append(
            {
                "model": model,
                "messages": messages,
                "stream": stream,
                "tools": tools,
                "tool_choice": tool_choice,
            }
        )
        index = len(self.calls) - 1
        events = self._responses[index] if index < len(self._responses) else []
        return _FakeEventStream(events)


class _FakeAsyncClient:
    def __init__(self, completions: _MultiCallChatCompletions):
        self.chat = type("_Chat", (), {"completions": completions})()


def _content_events(tokens: List[str]) -> List[_FakeEvent]:
    return [_FakeEvent(choices=[_FakeChoice(delta=_FakeDelta(content=token))]) for token in tokens]


def _tool_call_events(
    name: str, argument_chunks: List[str], call_id: str = "call_1"
) -> List[_FakeEvent]:
    events = [
        _FakeEvent(
            choices=[
                _FakeChoice(
                    delta=_FakeDelta(
                        tool_calls=[
                            _FakeToolCall(
                                index=0,
                                id=call_id,
                                function=_FakeToolCallFunction(name=name, arguments=None),
                            )
                        ]
                    )
                )
            ]
        )
    ]
    for chunk in argument_chunks:
        events.append(
            _FakeEvent(
                choices=[
                    _FakeChoice(
                        delta=_FakeDelta(
                            tool_calls=[
                                _FakeToolCall(
                                    index=0,
                                    id=None,
                                    function=_FakeToolCallFunction(
                                        name=None, arguments=chunk
                                    ),
                                )
                            ]
                        )
                    )
                ]
            )
        )
    return events


def _patch_provider(monkeypatch, responses: List[List[_FakeEvent]]):
    completions = _MultiCallChatCompletions(responses)
    provider = ActiveChatProvider(
        name="groq", client=_FakeAsyncClient(completions), model="test-model"
    )
    monkeypatch.setattr(rag_module, "get_active_chat_provider", lambda: provider)
    return completions


# --- The driving loop under test (mirrors app/api/messages.py) ---------------


async def drive(agen, query_results: Optional[List[Dict[str, Any]]] = None):
    """Drive the generator handshake exactly the way app/api/messages.py
    does, returning everything that happened.

    `query_results` is the queue of results to hand back for successive
    run_query tool calls - standing in for the real adapter execution."""
    pending = list(query_results or [])
    executed: List[dict] = []
    events: List[dict] = []

    to_send = None
    while True:
        try:
            event = await agen.asend(to_send)
        except StopAsyncIteration:
            break
        to_send = None
        events.append(event)
        if event["type"] == "tool_call":
            executed.append(event)
            to_send = (
                pending.pop(0)
                if pending
                else {"ok": False, "error": "no result configured in this test"}
            )

    tokens = "".join(e["text"] for e in events if e["type"] == "token")
    done = next((e for e in events if e["type"] == "done"), None)
    return {
        "events": events,
        "tokens": tokens,
        "executed": executed,
        "charts": [e for e in events if e["type"] == "chart"],
        "errors": [e for e in events if e["type"] == "error"],
        "done": done,
    }


def _ok_result(columns, rows, truncated=False):
    return {
        "ok": True,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "truncated": truncated,
        "error": None,
    }


# --- (1) direct answers never run a query ------------------------------------


@pytest.mark.asyncio
async def test_greeting_is_answered_directly_with_no_query(monkeypatch):
    completions = _patch_provider(monkeypatch, [_content_events(["Hi", " there!"])])

    result = await drive(
        rag_module.stream_agentic_reply(
            user_id=1,
            connection_id=7,
            engine_name="postgres",
            chat_history=None,
            message="hi",
            schema_context="Table: orders",
        )
    )

    assert result["tokens"] == "Hi there!"
    assert result["executed"] == []  # no query was ever requested
    assert len(completions.calls) == 1
    assert result["done"]["query_sql"] is None
    assert result["done"]["chart_spec"] is None
    assert result["done"]["content"] == "Hi there!"


@pytest.mark.asyncio
async def test_the_first_call_carries_the_system_prompt_schema_context_and_tools(
    monkeypatch,
):
    completions = _patch_provider(monkeypatch, [_content_events(["ok"])])

    await drive(
        rag_module.stream_agentic_reply(
            user_id=1,
            connection_id=7,
            engine_name="postgres",
            chat_history=None,
            message="hello",
            schema_context="Table: orders\nColumns:\n  - id",
        )
    )

    messages = completions.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert rag_module.AGENT_SYSTEM_PROMPT in messages[0]["content"]
    assert any("Table: orders" in m.get("content", "") for m in messages)
    assert completions.calls[0]["tools"] == rag_module.TOOLS
    assert completions.calls[0]["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_prior_history_turns_are_included(monkeypatch):
    completions = _patch_provider(monkeypatch, [_content_events(["ok"])])
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "Hello! Ask me about your data."},
    ]

    await drive(
        rag_module.stream_agentic_reply(
            user_id=1,
            connection_id=7,
            engine_name="postgres",
            chat_history=history,
            message="how many orders?",
            schema_context="",
        )
    )

    pairs = [(m["role"], m.get("content")) for m in completions.calls[0]["messages"]]
    assert ("user", "hi") in pairs
    assert ("assistant", "Hello! Ask me about your data.") in pairs


# --- (2) with no connection there is no tool to call at all ------------------


@pytest.mark.asyncio
async def test_no_connection_means_no_tools_are_offered_to_the_model(monkeypatch):
    """Structural, not just prompted: with no database bound to the chat
    the model is given no tools, so it CANNOT request a query - there would
    be nothing to run it against."""
    completions = _patch_provider(
        monkeypatch, [_content_events(["Connect a database first."])]
    )

    result = await drive(
        rag_module.stream_agentic_reply(
            user_id=1,
            connection_id=None,
            engine_name=None,
            chat_history=None,
            message="how many orders do we have?",
            schema_context="",
        )
    )

    assert completions.calls[0]["tools"] is None
    assert result["executed"] == []
    assert "NO DATABASE IS CONNECTED" in completions.calls[0]["messages"][0]["content"]


# --- (3) the tool-call handshake ----------------------------------------------


@pytest.mark.asyncio
async def test_run_query_handshake_passes_the_callers_ids_never_the_models(monkeypatch):
    """The model supplies ONLY the query text. user_id / connection_id /
    engine come from this function's own arguments (which the endpoint sets
    from the authenticated request), so no prompt injection or request body
    can redirect a query at another account's database."""
    completions = _patch_provider(
        monkeypatch,
        [
            _tool_call_events("run_query", ['{"query": "SELECT count(*) ', 'FROM orders"}']),
            _content_events(["You have 42 orders."]),
        ],
    )

    result = await drive(
        rag_module.stream_agentic_reply(
            user_id=42,
            connection_id=99,
            engine_name="postgres",
            chat_history=None,
            message="how many orders?",
            schema_context="Table: orders",
        ),
        query_results=[_ok_result(["count"], [[42]])],
    )

    assert len(result["executed"]) == 1
    call = result["executed"][0]
    assert call["name"] == "run_query"
    assert call["query"] == "SELECT count(*) FROM orders"
    assert call["user_id"] == 42
    assert call["connection_id"] == 99
    assert call["engine"] == "postgres"

    assert result["tokens"] == "You have 42 orders."
    assert result["done"]["query_sql"] == "SELECT count(*) FROM orders"
    assert len(completions.calls) == 2


@pytest.mark.asyncio
async def test_query_rows_are_fed_back_as_a_tool_message(monkeypatch):
    completions = _patch_provider(
        monkeypatch,
        [
            _tool_call_events("run_query", ['{"query": "SELECT status FROM orders"}']),
            _content_events(["Mostly shipped."]),
        ],
    )

    await drive(
        rag_module.stream_agentic_reply(
            user_id=1,
            connection_id=1,
            engine_name="postgres",
            chat_history=None,
            message="what statuses?",
            schema_context="Table: orders",
        ),
        query_results=[_ok_result(["status", "n"], [["shipped", 9], ["pending", 2]])],
    )

    second_call_messages = completions.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert "shipped" in tool_messages[0]["content"]
    assert "2 row(s) returned" in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_a_failed_query_is_reported_to_the_model_not_raised(monkeypatch):
    """A read-only violation or a database error must come back as a tool
    message the model can react to - the stream keeps going and the user
    gets a plain-language explanation, not a 500."""
    completions = _patch_provider(
        monkeypatch,
        [
            _tool_call_events("run_query", ['{"query": "DELETE FROM orders"}']),
            _content_events(["Sorry - I can only read data, not change it."]),
        ],
    )

    result = await drive(
        rag_module.stream_agentic_reply(
            user_id=1,
            connection_id=1,
            engine_name="postgres",
            chat_history=None,
            message="delete everything",
            schema_context="Table: orders",
        ),
        query_results=[
            {"ok": False, "error": "Only read-only queries are allowed: 'DELETE' ..."}
        ],
    )

    tool_messages = [
        m for m in completions.calls[1]["messages"] if m.get("role") == "tool"
    ]
    assert "did not run" in tool_messages[0]["content"]
    assert "DELETE" in tool_messages[0]["content"]
    assert result["errors"] == []  # not an error event - the turn continues
    assert "only read data" in result["tokens"]


@pytest.mark.asyncio
async def test_a_truncated_result_is_flagged_to_the_model(monkeypatch):
    completions = _patch_provider(
        monkeypatch,
        [
            _tool_call_events("run_query", ['{"query": "SELECT * FROM orders"}']),
            _content_events(["Here are the first rows."]),
        ],
    )

    await drive(
        rag_module.stream_agentic_reply(
            user_id=1,
            connection_id=1,
            engine_name="postgres",
            chat_history=None,
            message="show me orders",
            schema_context="Table: orders",
        ),
        query_results=[_ok_result(["id"], [[i] for i in range(3)], truncated=True)],
    )

    tool_message = [m for m in completions.calls[1]["messages"] if m.get("role") == "tool"][0]
    assert "row cap" in tool_message["content"]


@pytest.mark.asyncio
async def test_an_empty_result_is_reported_as_a_real_answer(monkeypatch):
    completions = _patch_provider(
        monkeypatch,
        [
            _tool_call_events("run_query", ['{"query": "SELECT * FROM orders"}']),
            _content_events(["No orders matched."]),
        ],
    )

    await drive(
        rag_module.stream_agentic_reply(
            user_id=1,
            connection_id=1,
            engine_name="postgres",
            chat_history=None,
            message="any orders from mars?",
            schema_context="Table: orders",
        ),
        query_results=[_ok_result(["id"], [])],
    )

    tool_message = [m for m in completions.calls[1]["messages"] if m.get("role") == "tool"][0]
    assert "0 rows" in tool_message["content"]


# --- (4) render_chart uses the held rows, never model-supplied data ----------


@pytest.mark.asyncio
async def test_render_chart_charts_the_rows_from_the_last_run_query(monkeypatch):
    real_rows = [["shipped", 9], ["pending", 2]]
    completions = _patch_provider(
        monkeypatch,
        [
            _tool_call_events("run_query", ['{"query": "SELECT status, n FROM v"}']),
            _tool_call_events(
                "render_chart",
                [
                    '{"chart_type": "bar", "title": "Orders by status", '
                    '"x_field": "status", "y_field": "n", '
                    '"rows": [["ignored", 999]], "data": "ignored"}'
                ],
                call_id="call_2",
            ),
            _content_events(["Most orders are shipped."]),
        ],
    )

    result = await drive(
        rag_module.stream_agentic_reply(
            user_id=1,
            connection_id=1,
            engine_name="postgres",
            chat_history=None,
            message="chart orders by status",
            schema_context="Table: orders",
        ),
        query_results=[_ok_result(["status", "n"], real_rows)],
    )

    assert len(result["charts"]) == 1
    chart = result["charts"][0]
    assert chart["chart_type"] == "bar"
    assert chart["title"] == "Orders by status"
    assert chart["x_field"] == "status"
    assert chart["y_field"] == "n"
    # The data is the REAL query result. The extra "rows"/"data" keys the
    # model tried to smuggle into the arguments are ignored entirely -
    # render_chart's schema has no data parameter.
    assert chart["columns"] == ["status", "n"]
    assert chart["rows"] == real_rows
    assert [999] not in chart["rows"]

    assert result["done"]["chart_spec"]["rows"] == real_rows


@pytest.mark.asyncio
async def test_render_chart_without_a_prior_query_is_refused(monkeypatch):
    completions = _patch_provider(
        monkeypatch,
        [
            _tool_call_events(
                "render_chart",
                ['{"chart_type": "pie", "title": "t", "x_field": "a", "y_field": "b"}'],
            ),
            _content_events(["Let me query first."]),
        ],
    )

    result = await drive(
        rag_module.stream_agentic_reply(
            user_id=1,
            connection_id=1,
            engine_name="postgres",
            chat_history=None,
            message="chart it",
            schema_context="Table: orders",
        )
    )

    assert result["charts"] == []
    tool_message = [m for m in completions.calls[1]["messages"] if m.get("role") == "tool"][0]
    assert "no query results to chart" in tool_message["content"].lower()


# --- (5) loop termination and dialect hints ----------------------------------


@pytest.mark.asyncio
async def test_the_final_round_is_issued_with_no_tools_so_the_loop_terminates(
    monkeypatch,
):
    """Even a model that asks for a query every single round can't loop
    forever: the last round is sent without tools attached."""
    completions = _patch_provider(
        monkeypatch,
        [
            _tool_call_events("run_query", ['{"query": "SELECT 1"}'], call_id="a"),
            _tool_call_events("run_query", ['{"query": "SELECT 2"}'], call_id="b"),
            _content_events(["Final answer."]),
        ],
    )

    result = await drive(
        rag_module.stream_agentic_reply(
            user_id=1,
            connection_id=1,
            engine_name="postgres",
            chat_history=None,
            message="q",
            schema_context="Table: t",
        ),
        query_results=[_ok_result(["x"], [[1]]), _ok_result(["x"], [[2]])],
    )

    assert len(completions.calls) == rag_module.MAX_TOOL_ROUNDS
    assert completions.calls[-1]["tools"] is None
    assert result["tokens"] == "Final answer."


@pytest.mark.parametrize(
    "engine,expected",
    [
        ("mssql", "TOP (n)"),
        ("postgres", "PostgreSQL"),
        ("mysql", "MySQL/MariaDB"),
        ("sqlite", "SQLite"),
        ("mongodb", "operation"),
    ],
)
def test_system_prompt_carries_the_right_dialect_hint(engine, expected):
    prompt = rag_module.build_system_prompt(engine, has_connection=True)
    assert expected in prompt


def test_system_prompt_without_a_connection_says_so():
    prompt = rag_module.build_system_prompt(None, has_connection=False)
    assert "NO DATABASE IS CONNECTED" in prompt


# --- (6) provider failures surface as error events, not exceptions -----------


@pytest.mark.asyncio
async def test_a_provider_failure_becomes_an_error_event(monkeypatch):
    class _Boom:
        async def create(self, **kwargs):
            raise RuntimeError("provider exploded")

    provider = ActiveChatProvider(
        name="groq", client=_FakeAsyncClient(_Boom()), model="test-model"
    )
    monkeypatch.setattr(rag_module, "get_active_chat_provider", lambda: provider)

    result = await drive(
        rag_module.stream_agentic_reply(
            user_id=1,
            connection_id=1,
            engine_name="postgres",
            chat_history=None,
            message="q",
            schema_context="",
        )
    )

    assert len(result["errors"]) == 1
    assert "provider exploded" in result["errors"][0]["message"]
    assert result["done"] is not None  # the turn still closes cleanly
