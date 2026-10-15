"""The LLM boundary guarantees a user-role message and surfaces 429 waits.

ensure_user_message is append-only and only-if-absent, so it never splits
an AIMessage(tool_calls)/ToolMessage pair. Rate-limit waits publish a
rate_limited bus event when a run sets the sink, so the console names the
wait instead of showing a silent "working...".
"""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from ops_copilot.llm import ainvoke_patient, ensure_user_message, rate_limit_sink


class TestEnsureUserMessage:
    def test_system_only_gets_user_message_appended(self):
        prompt = [SystemMessage(content="be helpful"), SystemMessage(content="results")]
        out = ensure_user_message(prompt)
        assert out[:-1] == prompt
        assert isinstance(out[-1], HumanMessage)
        assert out[-1].content == "Continue."

    def test_existing_user_message_unchanged(self):
        prompt = [SystemMessage(content="x"), HumanMessage(content="go")]
        assert ensure_user_message(prompt) is prompt

    def test_tool_transcript_not_split(self):
        # AIMessage(tool_calls) followed by its ToolMessage must stay adjacent;
        # the normalizer only appends at the end.
        call = AIMessage(
            content="",
            tool_calls=[{"name": "q", "args": {}, "id": "c1"}],
        )
        result = ToolMessage(content="ok", tool_call_id="c1")
        prompt = [SystemMessage(content="x"), HumanMessage(content="q?"), call, result]
        out = ensure_user_message(prompt)
        assert out is prompt  # a user message is already present: no change

    def test_tool_transcript_append_keeps_pair_adjacent(self):
        # No HumanMessage anywhere: the normalizer appends one at the end,
        # and the AIMessage(tool_calls)/ToolMessage pair must stay adjacent.
        call = AIMessage(
            content="",
            tool_calls=[{"name": "q", "args": {}, "id": "c1"}],
        )
        result = ToolMessage(content="ok", tool_call_id="c1")
        prompt = [SystemMessage(content="x"), call, result]
        out = ensure_user_message(prompt)
        assert out is not prompt
        assert out[0] is prompt[0]
        assert out[1] is call
        assert out[2] is result
        assert isinstance(out[3], HumanMessage)
        assert out[3].content == "Continue."

    def test_empty_list_gets_user_message(self):
        out = ensure_user_message([])
        assert len(out) == 1
        assert isinstance(out[0], HumanMessage)
        assert out[0].content == "Continue."


class FakeBus:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def publish(self, thread_id: str, event: dict) -> None:
        self.events.append((thread_id, event))


class RateLimitedOnce:
    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("429 Too Many Requests")
        return SimpleNamespace(content="done")


async def test_rate_limit_wait_publishes_event(monkeypatch):
    import ops_copilot.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_rate_limit_wait", lambda exc, attempt: 0)
    bus = FakeBus()
    token = rate_limit_sink.set((bus, "t1"))
    try:
        result = await ainvoke_patient(RateLimitedOnce(), [SystemMessage(content="x")])
    finally:
        rate_limit_sink.reset(token)
    assert result.content == "done"
    assert bus.events == [("t1", {"type": "rate_limited", "wait_seconds": 0, "context": ""})]


async def test_rate_limit_wait_without_sink_still_retries(monkeypatch):
    import ops_copilot.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_rate_limit_wait", lambda exc, attempt: 0)
    assert rate_limit_sink.get() is None
    result = await ainvoke_patient(RateLimitedOnce(), [SystemMessage(content="x")])
    assert result.content == "done"


async def test_normalizer_applies_inside_ainvoke_patient(monkeypatch):
    import ops_copilot.llm as llm_mod

    monkeypatch.setattr(llm_mod, "_rate_limit_wait", lambda exc, attempt: 0)
    seen: list = []

    class Capture:
        async def ainvoke(self, messages):
            seen.append(messages)
            return SimpleNamespace(content="ok")

    await ainvoke_patient(Capture(), [SystemMessage(content="x")])
    assert isinstance(seen[0][-1], HumanMessage)
