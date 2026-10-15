"""Supervisor node: plans work, dispatches it, and writes the final answer."""

import json
from typing import Any, Literal, cast

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.types import Command
from pydantic import BaseModel

from ops_copilot.api.errors import public_error
from ops_copilot.guardrails import BudgetExceeded, check_budget
from ops_copilot.llm import ainvoke_patient, build_chat_model
from ops_copilot.logging import get_logger
from ops_copilot.settings import get_settings

from .prompts import SUPERVISOR_SYSTEM
from .state import WORKER_NAMES, AgentState, Task

log = get_logger(__name__)

WorkerNode = Literal["analyst", "devops", "triage"]


def _worker_node(name: str) -> WorkerNode:
    # Assignees are validated against WORKER_NAMES at plan parse time; this
    # narrows the type for Command's benefit.
    return cast(WorkerNode, name)


class TaskPlan(BaseModel):
    """Tasks to delegate, or empty plus a direct reply for simple requests."""

    reply: str = ""
    tasks: list[Task] = []


async def _make_plan(state: AgentState) -> TaskPlan:
    settings = get_settings()
    model = build_chat_model(settings, temperature=0.0)
    schema_hint = json.dumps(
        {
            "reply": "direct answer or empty",
            "tasks": [{"id": "", "description": "", "assignee": "analyst"}],
        },
        indent=None,
    )
    prompt = [
        SystemMessage(content=SUPERVISOR_SYSTEM),
        *state["messages"][-8:],
        HumanMessage(
            content=(
                "Respond with ONLY a JSON object like "
                f"{schema_hint}, assignee must be exactly one of "
                f"{', '.join(WORKER_NAMES)}. Zero tasks means you answer in "
                "'reply' yourself."
            )
        ),
    ]
    response = await ainvoke_patient(model, prompt, context="planner")
    return _parse_plan(getattr(response, "content", ""))


def _parse_plan(raw: Any) -> TaskPlan:
    text = raw if isinstance(raw, str) else str(raw)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in planner output")
    data = json.loads(text[start : end + 1])
    plan = TaskPlan.model_validate(data)
    valid_tasks = []
    for i, task in enumerate(plan.tasks):
        if task.assignee in WORKER_NAMES:
            valid_tasks.append(task.model_copy(update={"id": task.id or f"t{i + 1}"}))
    return plan.model_copy(update={"tasks": valid_tasks})


async def _synthesize(state: AgentState) -> str:
    """Turn completed task summaries into the answer the user actually reads."""
    settings = get_settings()
    model = build_chat_model(settings, temperature=0.3)
    tasks = state.get("tasks") or []
    lines = [
        f"task {t.id} ({t.assignee}, {t.status}): {t.description}\n  outcome: {t.summary}"
        for t in tasks
    ]
    # Chat templates on some providers (Groq/qwen) reject a prompt with no
    # user-role message ("No user query found in messages"). This prompt used
    # to be system-only, so carry the user's question as the user turn.
    user_query = "Summarize the completed work for the user."
    for message in reversed(state.get("messages") or []):
        if isinstance(message, HumanMessage):
            content = message.content
            user_query = content if isinstance(content, str) else str(content)
            break
    prompt = [
        SystemMessage(content=SUPERVISOR_SYSTEM),
        SystemMessage(
            content="Team results:\n"
            + "\n".join(lines)
            + "\n\nWrite the user-facing wrap-up. Lead with the answer, include key numbers,"
            " mention anything still blocked on human approval. No preamble."
        ),
        HumanMessage(content=user_query),
    ]
    response = await ainvoke_patient(model, prompt, context="synthesis")
    return getattr(response, "text", None) or str(response.content)


async def planner(
    state: AgentState,
) -> Command[Literal["analyst", "devops", "triage", "finalize"]]:
    """Own node so the runner can suppress its raw-JSON tokens from chat."""
    try:
        plan = await _make_plan(state)
    except Exception as exc:
        log.warning("planner_failed", error=str(exc))
        # S13: the planner's raw exception can carry provider internals; the
        # chat gets the allowlisted shape only.
        return Command(
            update={
                "messages": [
                    AIMessage(
                        content=f"I could not make a plan ({public_error(exc)}). Please try again."
                    )
                ]
            },
            goto="finalize",
        )

    if not plan.tasks:
        reply = (
            plan.reply
            or "I read that as conversational - happy to dig into data or repos if you ask."
        )
        return Command(
            update={"messages": [AIMessage(content=reply)], "error": None},
            goto="finalize",
        )

    normalized = [t.model_copy(update={"id": f"t{i + 1}"}) for i, t in enumerate(plan.tasks)]
    first = normalized[0].model_copy(update={"status": "in_progress"})
    return Command(update={"tasks": [*normalized, first]}, goto=_worker_node(first.assignee))


async def supervisor(
    state: AgentState,
) -> Command[Literal["planner", "analyst", "devops", "triage", "approval_gate", "finalize"]]:
    settings = get_settings()
    messages = state.get("messages") or []

    try:
        check_budget(messages, settings.run_token_budget)
    except BudgetExceeded as exc:
        log.warning("budget_exceeded", detail=str(exc))
        return Command(
            update={
                "error": str(exc),
                "messages": [
                    AIMessage(
                        content="Stopped this run at its token budget before doing anything risky. "
                        "Start a fresh thread if you want me to continue."
                    )
                ],
            },
            goto="finalize",
        )

    # A worker proposed something sensitive; it must pass human review.
    if state.get("awaiting") is not None:
        return Command(goto="approval_gate")

    # New user turn within an existing thread: the previous plan is finished
    # business, so clear it and plan afresh. Mid-cycle supervisor entries
    # always follow a worker/gate message instead.
    messages = state.get("messages") or []
    fresh_turn = bool(messages) and isinstance(messages[-1], HumanMessage)
    tasks = state.get("tasks") or []

    if not tasks or (fresh_turn and any(t.status != "pending" for t in tasks)):
        update: dict[str, Any] = {"error": None}
        if tasks:
            # merge-only reducer: reset via sentinel
            from .state import CLEAR_TASKS, Task

            update["tasks"] = [Task(id=CLEAR_TASKS, description="", assignee="supervisor")]
        return Command(update=update, goto="planner")

    pending = next((t for t in tasks if t.status == "pending"), None)

    if pending is None:
        # A task stuck in_progress means a previous run died mid-worker
        # (provider outage, restart). Re-dispatch it rather than declaring
        # the plan complete over unfinished work.
        orphaned = next((t for t in tasks if t.status == "in_progress"), None)
        if orphaned is not None:
            log.info("redispatch_orphaned_task", task=orphaned.id, assignee=orphaned.assignee)
            return Command(goto=_worker_node(orphaned.assignee))

        # Everything finished: synthesize once, then stop.
        try:
            answer = await _synthesize(state)
        except Exception as exc:
            log.warning("synthesis_failed", error=str(exc))
            answer = (
                "\n".join(f"- [{t.assignee}] {t.summary}" for t in tasks if t.summary)
                or "Work finished but I could not summarize the outcomes."
            )
        rejected = sum(1 for t in tasks if t.status == "rejected")
        if rejected:
            answer += (
                f"\n\n{rejected} proposed action(s) were declined in review; nothing was executed."
            )
        return Command(
            update={"messages": [AIMessage(content=answer)], "awaiting": None},
            goto="finalize",
        )

    marked = pending.model_copy(update={"status": "in_progress"})
    return Command(update={"tasks": [marked]}, goto=_worker_node(marked.assignee))


async def finalize(state: AgentState) -> dict[str, Any]:
    return {"awaiting": None}
