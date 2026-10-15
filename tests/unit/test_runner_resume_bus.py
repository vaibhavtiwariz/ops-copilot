"""An approval resume must stream its tokens to the console via the bus.

The resume path has no direct consumer (_drain discards the generator), so
without the bus publish the resumed run's answer never reaches the console:
the card clears, phase flips idle, user sees nothing happened. Normal turns
keep tokens generator-only to avoid duplicating them through the WS pump.
"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage

from ops_copilot.api.runner import GraphRunner


class FakeBus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def publish(self, thread_id: str, event: dict) -> None:
        self.events.append((thread_id, event))


class FakeGraph:
    async def astream(self, payload, config, stream_mode=None):
        yield ("messages", (AIMessage(content="applied"), {"langgraph_node": "executor"}))
        yield ("updates", {"executor": {}})

    async def aget_state(self, config):
        return SimpleNamespace(tasks=[], values={})


async def _drain(runner, **kwargs):
    seen = []
    async for event in runner.run_turn("t1", "", **kwargs):
        seen.append(event)
    return seen


async def test_resume_publishes_tokens_to_bus():
    bus = FakeBus()
    runner = GraphRunner(FakeGraph(), bus)
    seen = await _drain(runner, resume_value={"approved": True})
    assert any(e["type"] == "token" for e in seen)  # generator still yields
    bus_types = [e["type"] for _, e in bus.events]
    assert "token" in bus_types  # and the bus carries it to the socket
    assert "done" in bus_types


async def test_normal_turn_does_not_publish_tokens_to_bus():
    bus = FakeBus()
    runner = GraphRunner(FakeGraph(), bus)
    seen = await _drain(runner)
    assert any(e["type"] == "token" for e in seen)
    bus_types = [e["type"] for _, e in bus.events]
    assert "token" not in bus_types  # generator-only: no duplication
    assert "done" in bus_types
