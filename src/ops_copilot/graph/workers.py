"""Specialist workers.

All three share one tool-calling loop; what differs is the system prompt and
the tool set the MCP layer hands them (see mcp/client.ROLE_SERVERS). A worker
that finds its server unreachable reports that instead of raising - a partial
outage should degrade the answer, not kill the thread.
"""

import json
from typing import Any, Literal

from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langgraph.types import Command

from ops_copilot.guardrails import claims_execution
from ops_copilot.llm import ainvoke_patient, build_chat_model
from ops_copilot.logging import get_logger
from ops_copilot.mcp.client import tools_for_role
from ops_copilot.settings import get_settings

from .prompts import ANALYST_SYSTEM, DEVOPS_SYSTEM, TRIAGE_SYSTEM
from .state import AgentState

log = get_logger(__name__)

MAX_TOOL_STEPS = 8
CONTEXT_TAIL = 24  # messages sent to the model; state keeps everything

# Tools whose successful call means "stop and wait for a human".
GATED_TOOLS = {"propose_write", "draft_pr_comment"}


def _unwrap_result(result: Any) -> str:
    """Normalize MCP tool outputs into plain text observations."""
    if isinstance(result, str):
        return result[:6000]
    if isinstance(result, list):
        # langchain-mcp-adapters returns MCP content blocks: [{"type": "text", "text": "..."}]
        texts = [
            block.get("text", "")
            for block in result
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        joined = "\n".join(t for t in texts if t)
        return (joined or json.dumps(result, default=str))[:6000]
    try:
        return json.dumps(result, default=str)[:6000]
    except TypeError:
        return str(result)[:6000]


async def _execute_tool(tool: Any, args: dict[str, Any]) -> str:
    try:
        result = await tool.ainvoke(args)
    except Exception as exc:
        return json.dumps({"error": str(exc)[:500]})
    return _unwrap_result(result)


def _extract_gated_payload(
    name: str, tool_args: dict[str, Any], raw_result: str
) -> dict[str, Any] | None:
    """Build the interrupt payload when a proposal-style tool succeeded."""
    if name == "propose_write":
        try:
            outcome = json.loads(raw_result)
        except json.JSONDecodeError:
            return None
        if isinstance(outcome, list):  # tolerate nested content blocks
            outcome = next(
                (item for item in outcome if isinstance(item, dict) and "proposal_id" in item),
                None,
            )
        if not isinstance(outcome, dict) or not outcome.get("proposal_id"):
            return None
        # S6: the interrupt payload is deliberately minimal. The full SQL
        # would otherwise land in the checkpoints DB, audit detail, and
        # /state. The reviewer fetches the exact SQL on demand via
        # GET /threads/{id}/proposals/{proposal_id}; the executor re-reads it
        # from the proposals table at apply time.
        summary = f"Proposed {tool_args.get('action')} on {tool_args.get('table')}"
        if outcome.get("target_label") == "best-effort":
            # S8: the table label on a mixed statement is a guess at best.
            # Say so plainly instead of presenting it as fact.
            summary += " (best-effort label: mixed write verbs, review the SQL itself)"
        return {
            "kind": "db_write",
            "proposal_id": int(outcome["proposal_id"]),
            "action": tool_args.get("action"),
            "table": tool_args.get("table"),
            "summary": summary,
        }
    if name == "draft_pr_comment":
        return {
            "kind": "github_comment",
            "repo": tool_args.get("repo"),
            "number": int(tool_args.get("number") or 0),
            "body": tool_args.get("body"),
            "summary": f"Draft comment for {tool_args.get('repo')}#{tool_args.get('number')}",
        }
    return None


async def run_worker(
    role: str,
    system_prompt: str,
    task_description: str,
    state: AgentState,
) -> Command[Literal["supervisor", "approval_gate"]]:
    settings = get_settings()
    tools = await tools_for_role(role)

    base_update: dict[str, Any] = {
        "messages": [],
        "tasks": [],
        "awaiting": None,
    }
    if not tools:
        summary = f"{role}: tool servers unreachable, nothing could be inspected."
        return Command(
            update={
                **base_update,
                "tasks": [_task(state, role, summary, status="rejected")],
                "messages": [AIMessage(content=summary)],
            },
            goto="supervisor",
        )

    tool_map = {t.name: t for t in tools}
    model = build_chat_model(settings).bind_tools(tools)

    transcript = [
        SystemMessage(content=f"{system_prompt}\n\nCurrent assignment: {task_description}"),
        *state["messages"][-CONTEXT_TAIL:],
    ]

    new_messages: list[AnyMessage] = []
    gated_payload: dict[str, Any] | None = None
    final_text = ""

    for step in range(MAX_TOOL_STEPS):
        response = await ainvoke_patient(model, transcript, context=f"{role}:step{step}")
        new_messages.append(response)
        transcript.append(response)

        calls = getattr(response, "tool_calls", None) or []
        if not calls:
            text = getattr(response, "text", None)
            final_text = text if isinstance(text, str) else str(response.content)
            break

        for call in calls:
            name = call.get("name") or ""
            args = call.get("args") or {}
            tool = tool_map.get(name)
            if tool is None:
                observation = json.dumps({"error": f"unknown tool {name!r}"})
            else:
                log.info("worker_tool_call", role=role, tool=name)
                observation = await _execute_tool(tool, args)
            message = ToolMessage(content=observation, tool_call_id=call.get("id") or "")
            new_messages.append(message)
            transcript.append(message)

            if name in GATED_TOOLS:
                candidate = _extract_gated_payload(name, args, observation)
                if candidate is not None:
                    gated_payload = candidate

        if gated_payload is not None:
            # The proposal is registered; execution waits at the gate.
            break

    if not final_text:
        final_text = f"{role}: stopped after {MAX_TOOL_STEPS} tool steps." + (
            " A human needs to review the pending proposal." if gated_payload else ""
        )

    # A pending proposal keeps the task open until the human decides;
    # the gate or executor closes it out.
    if gated_payload is not None:
        status = "in_progress"
        summary = f"{role} registered {gated_payload.get('summary', 'a proposal')}; awaiting review"
    else:
        status = "done"
        summary = final_text.strip()[:400] or f"{role} finished with no findings."

        # Claim auditing: a worker asserting that something was executed
        # without having gone through the gate is exactly the failure mode
        # this system exists to prevent. Surface it loudly, never silently.
        if claims_execution(final_text):
            log.warning("unverified_action_claim", role=role)
            new_messages.append(
                AIMessage(
                    content="[guardrail] The answer above contains action claims but no proposal "
                    "was registered for human review; treat execution claims as unverified."
                )
            )
            summary = f"[unverified action claim] {summary[:380]}"

    return Command(
        update={
            "messages": new_messages,
            "awaiting": gated_payload,
            "tasks": [_task(state, role, summary, status=status)],
        },
        goto="approval_gate" if gated_payload else "supervisor",
    )


def _task(state: AgentState, role: str, summary: str, *, status: str = "done"):
    from .state import Task

    tasks = state.get("tasks") or []
    current = next((t for t in tasks if t.assignee == role and t.status == "in_progress"), None)
    if current is None:
        current = next((t for t in reversed(tasks) if t.assignee == role), None)
    if current is None:
        return Task(
            id=f"{role}-adhoc",
            description="(ad-hoc)",
            assignee=role,
            status=status,
            summary=summary,
        )
    return current.model_copy(update={"status": status, "summary": summary})


async def analyst(state: AgentState) -> Command[Literal["supervisor", "approval_gate"]]:
    task = _current_description(state, "analyst")
    return await run_worker("analyst", ANALYST_SYSTEM, task, state)


async def devops(state: AgentState) -> Command[Literal["supervisor", "approval_gate"]]:
    task = _current_description(state, "devops")
    return await run_worker("devops", DEVOPS_SYSTEM, task, state)


async def triage(state: AgentState) -> Command[Literal["supervisor", "approval_gate"]]:
    task = _current_description(state, "triage")
    return await run_worker("triage", TRIAGE_SYSTEM, task, state)


def _current_description(state: AgentState, role: str) -> str:
    tasks = state.get("tasks") or []
    active = next((t for t in tasks if t.assignee == role and t.status == "in_progress"), None)
    return active.description if active else "(no explicit task; answer from your tools)"
