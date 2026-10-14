# 6. Evals: golden trajectories, CI-ready but not wired

**Status:** accepted

Most agent demos break silently: prompts drift, a tool name changes, routing
flips between two workers, and nothing fails until a human notices the demo
got dumber. The countermeasure here is a deterministic eval suite that runs
locally with **no API keys** and is ready to drop into CI:

## Layer 1 - golden trajectories (`evals/runner.py`)

Each dataset case carries a scripted planner output and scripted worker turns.
The runner replays them through the *real* graph - real supervisor logic,
reducers, conditional edges, approval gate, budget checks; only model calls
are canned - then scores:

| metric | what it catches |
|---|---|
| routing accuracy | delegation changes after prompt or schema edits |
| tool F1 | tools renamed / dropped from RBAC-filtered sets |
| gate accuracy | sensitive actions silently skipping human review |

Thresholds (currently 1.0 / 0.9 / 1.0) make the harness exit non-zero, so a
future CI job can gate on `make eval`. No workflow is wired yet. Reports land
in `evals/runs/report.md`.

## Layer 2 - live judge (`evals/judges.py`)

An LLM-as-judge pass over final answers scores groundedness/completeness/tone
when credentials are present. It is explicitly *not* a CI gate: non-determinism
and paid-API dependence would make it one more flaky check everyone learns to
ignore.

## What we gave up

End-to-end evals against real models would catch bad prompt regressions the
golden set cannot. That requires either paid API calls in CI or self-hosted
models in runners; both are heavier than this project warrants today. The gap
is stated here rather than papered over.
