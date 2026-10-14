# 3. Postgres-backed checkpointing for durable human-in-the-loop

**Status:** accepted

LangGraph's `AsyncPostgresSaver` (psycopg3, not asyncpg) persists every
superstep. Three consequences shaped the design:

- **Interrupts survive restarts.** When the gate calls `interrupt()`, the
  pending proposal is in the checkpoint, not process memory. An approver can
  take an hour; a deploy can happen in between.
- **Time travel is free.** `get_state_history` powers `/api/threads/{id}/history`
  and the replay endpoint forks execution from any prior checkpoint. This is
  the cheapest debugging tool we have and we did not have to build it.
- **Serialization is part of the contract.** Our Task model is registered with
  the checkpointer's serde allowlist; forgetting this produces deprecation
  warnings today and hard failures in future versions.

## Notes

- The saver requires `await setup()` once per database - done at API startup
  and in `scripts/bootstrap_db.py`.
- Connections must be autocommit with dict row factory; the pool is configured
  accordingly (`api/main.py`).
- Checkpoints live in a dedicated database so operational cleanup (dropping
  old threads) never touches application tables.
