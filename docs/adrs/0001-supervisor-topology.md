# 1. Why a supervisor over a single tool-calling agent

**Status:** accepted

A single ReAct agent with all tools attached solves most of what this copilot
does. We chose supervision anyway, and the reasons are operational rather than
capability-related:

- **Least privilege per step.** The analyst should never see GitHub write
  paths, and the triage agent should never see raw SQL execution. With one
  agent, tool scoping is a prompt suggestion. With workers, it is enforced in
  `mcp/client.py` before any model call happens.
- **Bounded blast radius per failure.** A worker that wedges on bad tool
  output burns its own MAX_TOOL_STEPS budget and returns a summary. The
  supervisor keeps the plan and can re-delegate or report partial results.
- **Auditable delegation.** Every unit of work is a Task record with assignee,
  status, and outcome summary in checkpointed state. "What did the system do?"
  is a data question, not a log archaeology project.

The cost is real: two model hops per simple request (plan + execute) versus
one, plus state-merge complexity. For a system whose worst case is executing
SQL against production data, we consider that cost well spent.

## Alternatives considered

- Single agent + role prompt. Cheapest, fails least-privilege.
- Static pipeline (analyst then devops). No adaptivity to multi-intent
  requests like "check revenue and tell customers about incident #3".
