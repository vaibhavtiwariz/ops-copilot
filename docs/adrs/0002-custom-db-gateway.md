# 2. A custom database gateway instead of the reference postgres MCP server

**Status:** accepted

Anthropic's archived `postgres-mcp-server` is the obvious starting point for
"LLM queries Postgres". We deliberately did not reuse it:

1. It was archived in 2025 after a read-only sandbox escape via multi-statement
   input was documented. That failure mode is exactly what our threat model
   cares about.
2. Its design centers "run SQL, maybe read-only". Ours needs mutation requests
   to be *structurally impossible*, not discouraged by a flag.

## What the gateway does differently (`mcp/servers/ops_db.py`)

- **One statement per call.** Input is comment-stripped and split; anything
  with more than one statement is rejected before parsing even matters.
- **Verb classification, not string matching.** CTE-wrapped writes
  (`WITH x AS (...) DELETE ...`) and `EXPLAIN ANALYZE <write>` resolve to their
  effective verb and are refused.
- **Side-channel denylist.** `pg_sleep`, `pg_read_file`, `dblink`,
  `lo_import`, and friends are rejected by name outside string literals.
- **Defense in depth.** The read path runs on a connection with
  `default_transaction_read_only = on` set at the session level, so even a
  policy bug cannot commit a write. Row caps are enforced with
  `fetchmany(limit + 1)` and statement timeouts at session level.
- **Mutations as proposals.** The only way data changes is: worker registers a
  proposal row -> human approves -> executor calls `apply_approved_write` ->
  gateway verifies approval status *and* idempotency (an applications row)
  inside the same transaction as the effect.

PII masking runs at the source: emails, phone numbers, card numbers and SSNs
are masked in query results and issue text before anything reaches a prompt
or trace.
