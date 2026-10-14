"""Deterministic end-to-end graph tests with scripted models and fake tools."""

import json

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ops_copilot.graph.build import build_graph


class ScriptedModel(BaseChatModel):
    """Plays back prepared AI turns, one per invocation."""

    responses: list[AIMessage]
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        # Scripted responses already carry their tool_calls.
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        response = self.responses[self.cursor % len(self.responses)]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


def plan_message(tasks: list[dict], reply: str = "") -> AIMessage:
    return AIMessage(content=json.dumps({"reply": reply, "tasks": tasks}))


@tool
async def run_readonly_query(sql: str) -> str:
    """Run a read-only query."""
    return json.dumps({"columns": ["revenue"], "rows": [{"revenue": 1234567}], "row_count": 1})


@tool
async def propose_write(action: str, table: str, sql: str, rationale: str) -> str:
    """Register a mutation for human review."""
    return json.dumps({"proposal_id": 77, "status": "pending"})


ANALYST_TOOLS = [run_readonly_query, propose_write]


@pytest.fixture()
def patched_tools(monkeypatch):
    async def fake(role):
        if role == "analyst":
            return ANALYST_TOOLS
        return []

    monkeypatch.setattr("ops_copilot.graph.workers.tools_for_role", fake)


def wire(monkeypatch, *, supervisor_script: list[AIMessage], worker_script: list[AIMessage]):
    sup = ScriptedModel(responses=list(supervisor_script))
    wrk = ScriptedModel(responses=list(worker_script))
    monkeypatch.setattr("ops_copilot.graph.supervisor.build_chat_model", lambda *a, **k: sup)
    monkeypatch.setattr("ops_copilot.graph.workers.build_chat_model", lambda *a, **k: wrk)
    return build_graph(checkpointer=InMemorySaver())


async def run_to_completion(graph, config, payload):
    result = await graph.ainvoke(payload, config)
    return result


class TestDelegation:
    async def test_analyst_runs_query_and_supervisor_synthesizes(self, patched_tools, monkeypatch):
        graph = wire(
            monkeypatch,
            supervisor_script=[
                plan_message([{"id": "", "description": "monthly revenue", "assignee": "analyst"}]),
                AIMessage(content="Revenue was $1.23M."),
            ],
            worker_script=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "run_readonly_query",
                            "args": {"sql": "select sum(amount_cents) from orders"},
                            "id": "c1",
                        }
                    ],
                ),
                AIMessage(content="Summed order amounts; total revenue found."),
            ],
        )
        config = {"configurable": {"thread_id": "t-delegate"}}
        result = await run_to_completion(
            graph, config, {"messages": [("human", "what is monthly revenue")]}
        )

        statuses = {t.assignee: t.status for t in result["tasks"]}
        assert statuses == {"analyst": "done"}
        assert result["messages"][-1].content == "Revenue was $1.23M."
        # the worker transcript made it into state too
        tool_messages = [m for m in result["messages"] if m.type == "tool"]
        assert any("1234567" in m.content for m in tool_messages)

    async def test_direct_reply_without_delegation(self, patched_tools, monkeypatch):
        graph = wire(
            monkeypatch,
            supervisor_script=[plan_message([], reply="Hello! Ask me about data or repos.")],
            worker_script=[],
        )
        config = {"configurable": {"thread_id": "t-direct"}}
        result = await run_to_completion(graph, config, {"messages": [("human", "hi")]})
        assert result["tasks"] == []
        assert result["messages"][-1].content == "Hello! Ask me about data or repos."


class TestHumanGate:
    async def test_proposal_interrupts_and_rejection_marks_task(self, patched_tools, monkeypatch):
        graph = wire(
            monkeypatch,
            supervisor_script=[
                plan_message(
                    [{"id": "", "description": "refund bad orders", "assignee": "analyst"}]
                ),
                AIMessage(content="Wrap-up."),
            ],
            worker_script=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "propose_write",
                            "args": {
                                "action": "update",
                                "table": "orders",
                                "sql": "update orders set status='refunded' where id=5",
                                "rationale": "customer double-charged",
                            },
                            "id": "p1",
                        }
                    ],
                ),
            ],
        )
        config = {"configurable": {"thread_id": "t-gate"}}
        await graph.ainvoke({"messages": [("human", "refund order 5")]}, config)

        snapshot = await graph.aget_state(config)
        interrupts = [i for task in snapshot.tasks for i in task.interrupts]
        assert len(interrupts) == 1
        payload = interrupts[0].value
        assert payload["kind"] == "db_write"
        assert payload["proposal_id"] == 77
        # S6: the interrupt payload is minimal (no SQL); the reviewer fetches
        # the exact statement via GET /threads/{id}/proposals/{proposal_id}.
        assert "sql" not in payload
        assert payload["action"] == "update"
        assert payload["table"] == "orders"
        assert "update" in payload["summary"] and "orders" in payload["summary"]

        result = await graph.ainvoke(
            Command(
                resume={
                    "approved": False,
                    "note": "not without finance sign-off",
                    "decided_by": "vaibhav",
                }
            ),
            config,
        )
        rejected = [t for t in result["tasks"] if t.status == "rejected"]
        assert len(rejected) == 1
        assert "finance sign-off" in rejected[0].summary
        assert result["awaiting"] is None

    async def test_approval_routes_through_executor_path_state(self, patched_tools, monkeypatch):
        # Executor would call the real MCP apply tool; offline we assert the
        # gate hands a fully-prepared payload to the executor node instead.
        graph = wire(
            monkeypatch,
            supervisor_script=[
                plan_message([{"id": "", "description": "fix row", "assignee": "analyst"}]),
                AIMessage(content="Done summary."),
            ],
            worker_script=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "propose_write",
                            "args": {
                                "action": "update",
                                "table": "incidents",
                                "sql": "update incidents set status='resolved' where id=3",
                                "rationale": "stale",
                            },
                            "id": "p2",
                        }
                    ],
                ),
            ],
        )
        config = {"configurable": {"thread_id": "t-approve"}}
        await graph.ainvoke({"messages": [("human", "close incident 3")]}, config)

        history = []
        async for snap in graph.aget_state_history(config, limit=30):
            history.append(snap)
        assert any("approval_gate" in [str(n) for n in snap.next] for snap in history)
