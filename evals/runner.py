"""Golden-trajectory evals, fully offline.

Each dataset case carries its own scripted planner output and worker turns.
The runner replays them through the real graph (real routing logic, real gate,
real reducers - only the model calls are canned) and scores:

- routing: did work land on the workers we expect, in order?
- tools: did workers reach for the tools the case expects? (precision/recall)
- gate: did sensitive proposals actually interrupt for human review?

`make eval` runs this; CI fails under threshold. A live LLM judge lives in
judges.py but only activates when credentials are present.
"""

import argparse
import asyncio
import contextlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ["OPS_EVAL_OFFLINE"] = "1"
os.environ.setdefault("OPS_LLM_PROVIDER", "groq")

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from ops_copilot.graph.build import build_graph

# Explicitly offline: the golden trajectories never touch a real provider, so
# the runner plants a dummy key instead of requiring one. The constant name
# says what it is so nobody mistakes it for a real credential, and
# OPS_EVAL_OFFLINE (set above, before any provider import) keeps the live
# judge in judges.py switched off: without it, this dummy key would defeat
# the judge's own "credentials present" guard.
OFFLINE_DUMMY_KEY = "offline-eval-key"
os.environ.setdefault("OPS_GROQ_API_KEY", OFFLINE_DUMMY_KEY)

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals" / "datasets" / "golden_trajectories.jsonl"
RUNS_DIR = ROOT / "evals" / "runs"

THRESHOLDS = {"routing_accuracy": 1.0, "tool_f1": 0.9, "gate_accuracy": 1.0}


class ReplayModel(BaseChatModel):
    responses: list[AIMessage]
    cursor: int = 0

    @property
    def _llm_type(self) -> str:
        return "replay"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        response = self.responses[self.cursor % max(len(self.responses), 1)]
        self.cursor += 1
        return ChatResult(generations=[ChatGeneration(message=response)])


FAKE_TOOL_RESULTS = {
    "run_readonly_query": {"columns": ["n"], "rows": [{"n": 4}], "row_count": 1},
    "describe_table": {
        "table": "incidents",
        "columns": [{"column_name": "service", "data_type": "text"}],
        "indexes": [],
    },
    "list_open_issues": [{"number": 101, "title": "timeout on retries", "author": "a"}],
    "issue_thread": {"title": "timeout on retries", "comments": []},
    "profile_column": {"stats": {}, "sample": []},
    # must carry a proposal_id or the worker's gate hand-off is skipped
    "propose_write": {"proposal_id": 777, "status": "pending"},
}


def make_tool(name: str):
    @tool
    async def dynamic(**kwargs):  # type: ignore[no-untyped-def]
        """Canned stand-in for an MCP tool."""
        return json.dumps(FAKE_TOOL_RESULTS.get(name, {}))

    dynamic.name = name  # type: ignore[misc]
    return dynamic


def load_cases() -> list[dict]:
    cases = []
    for line in DATASET.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            cases.append(json.loads(line))
    return cases


async def run_case(case: dict) -> dict:
    plan = case["plan"]
    supervisor_responses = [
        AIMessage(content=json.dumps(plan)),
        AIMessage(content="Final summary from the scripted supervisor."),
    ]

    worker_turns: list[AIMessage] = []
    pending_calls = []
    for step in case["worker_script"]:
        if "tool" in step:
            call_id = f"c{len(worker_turns)}"
            pending_calls.append(call_id)
            worker_turns.append(
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": step["tool"], "args": step.get("args", {}), "id": call_id}
                    ],
                )
            )
        else:
            worker_turns.append(AIMessage(content=step["say"]))
    if not worker_turns:
        worker_turns.append(AIMessage(content="nothing to do"))

    sup_model = ReplayModel(responses=supervisor_responses)
    wrk_model = ReplayModel(responses=worker_turns)

    async def fake_tools(role):
        names = set()
        for step in case["worker_script"]:
            if "tool" in step:
                names.add(step["tool"])
        return [make_tool(n) for n in sorted(names)]

    with (
        _patch("ops_copilot.graph.supervisor.build_chat_model", lambda *a, **k: sup_model),
        _patch("ops_copilot.graph.workers.build_chat_model", lambda *a, **k: wrk_model),
        _patch("ops_copilot.graph.workers.tools_for_role", fake_tools),
    ):
        graph = build_graph(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": f"eval-{case['id']}"}}

        visited: list[str] = []
        tools_called: list[str] = []
        interrupted = False

        payload = {"messages": [("human", case["input"])]}
        result = await graph.ainvoke(payload, {**config, "recursion_limit": 40})

        # astream would give node order directly; ainvoke makes us reconstruct
        # it from task history, which is fine for these linear flows.
        for t in result["tasks"]:
            if t.assignee not in visited:
                visited.append(t.assignee)
        tools_called = [
            c["name"]
            for m in result["messages"]
            if getattr(m, "tool_calls", None)
            for c in m.tool_calls
        ]
        snapshot = await graph.aget_state(config)
        interrupted = any(i for task in snapshot.tasks for i in getattr(task, "interrupts", []))
        if case["expected"]["gate"] and not interrupted:
            # resume-and-reject so the thread ends cleanly (also mirrors prod)
            await graph.ainvoke(Command(resume={"approved": False, "note": "eval"}), config)

        expected = case["expected"]
        route_ok = visited == expected["route"]
        exp_tools = set(expected["tools"])
        got_tools = set(tools_called)
        precision = len(exp_tools & got_tools) / len(got_tools) if got_tools else 1.0
        recall = len(exp_tools & got_tools) / len(exp_tools) if exp_tools else 1.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else (1.0 if not exp_tools else 0.0)
        )
        gate_ok = interrupted == expected["gate"]

        return {
            "id": case["id"],
            "route_ok": route_ok,
            "route_expected": expected["route"],
            "route_actual": visited,
            "tool_f1": round(f1, 3),
            "gate_ok": gate_ok,
        }


@contextlib.contextmanager
def _patch(target: str, replacement):
    module_path, _, attr = target.rpartition(".")
    import importlib

    module = importlib.import_module(module_path)
    original = getattr(module, attr)
    setattr(module, attr, replacement)
    try:
        yield
    finally:
        setattr(module, attr, original)


def score(results: list[dict]) -> dict:
    n = len(results) or 1
    routing = sum(r["route_ok"] for r in results) / n
    gate = sum(r["gate_ok"] for r in results) / n
    tool_f1 = sum(r["tool_f1"] for r in results) / n
    return {
        "routing_accuracy": round(routing, 3),
        "tool_f1": round(tool_f1, 3),
        "gate_accuracy": round(gate, 3),
    }


def write_report(results: list[dict], totals: dict, path: Path) -> None:
    lines = [
        "# Golden trajectory evals",
        "",
        f"Run at {datetime.now(UTC).isoformat(timespec='seconds')}",
        "",
        "| case | routing | expected | actual | tool F1 | gate |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(
            f"| {r['id']} | {'pass' if r['route_ok'] else 'FAIL'} "
            f"| {'→'.join(r['route_expected']) or '(none)'} "
            f"| {'→'.join(r['route_actual']) or '(none)'} "
            f"| {r['tool_f1']} | {'pass' if r['gate_ok'] else 'FAIL'} |"
        )
    lines += [
        "",
        "## Totals",
        "",
        "| metric | value | threshold | status |",
        "|---|---|---|---|",
    ]
    for metric, value in totals.items():
        threshold = THRESHOLDS[metric]
        ok = "pass" if value >= threshold else "**FAIL**"
        lines.append(f"| {metric} | {value} | >= {threshold} | {ok} |")
    path.write_text("\n".join(lines) + "\n")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fail-under-gap", action="store_true", help="exit 1 below thresholds (CI mode)"
    )
    args = parser.parse_args()

    cases = load_cases()
    results = []
    for case in cases:
        try:
            results.append(await run_case(case))
        except Exception as exc:
            results.append(
                {
                    "id": case["id"],
                    "route_ok": False,
                    "route_expected": case["expected"]["route"],
                    "route_actual": [f"error: {exc}"],
                    "tool_f1": 0.0,
                    "gate_ok": False,
                }
            )

    totals = score(results)
    RUNS_DIR.mkdir(exist_ok=True)
    report = RUNS_DIR / "report.md"
    write_report(results, totals, report)

    print(f"{len(results)} cases evaluated")
    for metric, value in totals.items():
        marker = "OK " if value >= THRESHOLDS[metric] else "LOW"
        print(f"  [{marker}] {metric}: {value} (threshold {THRESHOLDS[metric]})")
    print(f"report: {report.relative_to(ROOT)}")

    below = [m for m, v in totals.items() if v < THRESHOLDS[m]]
    if args.fail_under_gap and below:
        print(f"failing CI gate: {below}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
