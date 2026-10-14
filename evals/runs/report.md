# Golden trajectory evals

Run at 2026-10-14T13:56:39+00:00

| case | routing | expected | actual | tool F1 | gate |
|---|---|---|---|---|---|
| conversational | pass | (none) | (none) | 1.0 | pass |
| revenue_lookup | pass | analyst | analyst | 1.0 | pass |
| schema_before_query | pass | analyst | analyst | 1.0 | pass |
| repo_health | pass | devops | devops | 1.0 | pass |
| mutation_goes_to_human | pass | analyst | analyst | 1.0 | pass |
| multi_task_handoff | pass | analyst→triage | analyst→triage | 1.0 | pass |

## Totals

| metric | value | threshold | status |
|---|---|---|---|
| routing_accuracy | 1.0 | >= 1.0 | pass |
| tool_f1 | 1.0 | >= 0.9 | pass |
| gate_accuracy | 1.0 | >= 1.0 | pass |
