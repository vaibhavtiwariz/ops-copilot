# Changelog: ops-copilot fix pass

Running log of every change the fix team makes and why. Newest first. Each entry names the finding, the files touched, what changed, and why in plain language.

## 2026-10-15: FIX: WebSocket idle timeout no longer kills a socket mid-run
- Files: src/ops_copilot/api/main.py, src/ops_copilot/api/ratelimit.py, tests/integration/test_ws_idle_during_resume.py (new)
- What: the WebSocket loop closes a socket with code 4408 after WS_IDLE_TIMEOUT_S (600 seconds, 10 minutes) with no inbound frame, instead of letting idle sockets linger with their queue and pump task. Exception: if the runner reports the thread busy, the loop keeps waiting instead of closing, because a run in flight (for example an approval resume driven by the HTTP endpoint, not by this socket) still needs this socket for its done event. Tests: 2 new tests (socket survives the idle timeout during a resume; a truly idle socket still closes with 4408).
- Why: an approval resume is driven by the HTTP endpoint, so the socket sits idle in receive while the resume runs. An idle timeout firing mid-resume closed the socket and the resume's done event never arrived. Caveat: the resume test does not discriminate in TestClient (queued frames are delivered after close), so the live check on a long resume is still the final proof.

## 2026-10-15: FIX: worker tool calls now have a 60-second timeout
- Files: src/ops_copilot/graph/workers.py, tests/unit/test_worker_tool_timeout.py (new)
- What: new _execute_tool wraps every worker MCP tool call in asyncio.wait_for with TOOL_TIMEOUT_S (60.0 seconds). On timeout the worker gets a JSON error observation ("tool X timed out") it can work with, instead of hanging. Other tool exceptions still return JSON error observations, and successful results are unchanged.
- Why: an MCP tool call with no deadline can stall a run silently forever (a wedged server, a dropped response). Bounding it turns a silent hang into an error the worker can report or route around. Tests: 3 new tests (hanging tool returns the timeout error fast, failing tool still returns error JSON, ok tool still returns its result).

## 2026-10-15: FIX: approval card no longer loops on a 409, history newest-first
- Files: web/lib/api.ts, web/app/page.tsx, src/ops_copilot/api/main.py, tests/unit/test_llm_normalizer.py, tests/integration/test_api_ws.py
- What: submitApproval now throws a typed ApprovalError carrying the HTTP status, and decide() branches on it. On a 409 (gate gone, interrupt already consumed) it clears the stale card, shows a system bubble ("approval no longer pending: the gate already moved on, state refreshed"), and refreshes state instead of restoring a card that can never decide. On all other errors (504 drain timeout, 502, network) it still restores the card so the user can re-decide. Correction to the earlier approval-history entry in this log: that entry said a failed POST restores the card for 504, 409, and 502; the 409 case now clears instead. The history panel sorts newest-first. The transcript endpoint skips AIMessage content starting with "[guardrail]", so reopening a thread no longer shows internal plumbing as a chat bubble. Tests: 2 new normalizer tests (tool-transcript append keeps the AIMessage(tool_calls)/ToolMessage pair adjacent with the HumanMessage last; empty-list case) and a backend contract test (POST /approvals with nothing waiting returns 409, "nothing is waiting"). The frontend 409 branch itself has no JS test harness in this repo (no vitest/jest), so it is covered by the contract test plus tsc.
- Why: after a 504-cancel that lands past the gate, the gate has already cleared awaiting and the interrupt is consumed, so every re-decide got 409 while the UI kept restoring the card: an infinite approve-to-409 loop that actively misled, worse than the original "nothing happened". Known limitation, not fixed here: after a past-gate 504 the run itself is dead (interrupt consumed, no resume endpoint), so re-decide cannot revive it; the UI is now honest about that (card clears, state refreshes, new thread). The 202 decoupling of the approval POST is the complete fix and stays a follow-up.

## 2026-10-15: FIX: approval resume now delivers the resumed answer
- Files: src/ops_copilot/api/runner.py, src/ops_copilot/api/main.py, src/ops_copilot/api/schemas.py, web/lib/api.ts, web/app/page.tsx
- What: the runner now also publishes token and node events to the event bus when a run is resumed after an approval, so the WebSocket receives the resumed run's answer exactly like a user turn. New token-gated GET /threads/{id}/transcript returns the thread's chat transcript (human and assistant text only, no tool messages, no SQL), and the console fetches it on thread open and on run done, so a reopened thread shows its real conversation and a run whose streamed tokens were missed still lands its answer.
- Why: the approval endpoint resumed the run through a drain that discarded every yielded event, and the console on done only fetched tasks and approvals, never the conversation. The write applied, but the console showed nothing after approval. The approval POST still waits for the resume to finish; returning 202 and streaming via WebSocket is deferred to its own review pass.

## 2026-10-15: FIX: every LLM call now carries a user message (systemic normalizer)
- Files: src/ops_copilot/llm.py, src/ops_copilot/graph/supervisor.py
- What: new ensure_user_message in llm.py, wired into ainvoke_patient so every call site (planner, synthesis, workers, future calls) is covered. It is append-only and only-if-absent: if no user-role message exists it appends one, and it never splits an AIMessage(tool_calls)/ToolMessage pair, so tool transcripts are untouched. Synthesis also carries the real user query from state for prompt quality.
- Why: qwen's chat template rejects prompts with no user message ("No user query found in messages"). The synthesis prompt was system-only, and a worker prompt can lose its user turn when the 24-message context tail slices past the last user message, so patching synthesis alone left every worker call exposed. The live Groq/qwen call is still the final proof; the unit tests prove the prompt now carries a user message.

## 2026-10-15: FIX: approval history reads from the audit log, card restored on failure
- Files: web/lib/api.ts, web/app/page.tsx
- What: the console's resolved-approvals list now comes from GET /threads/{id}/audit (token-gated), with timestamp, proposal id, and decided-by from the audit record, so the history survives thread switches and reloads. If the approval POST fails (504, 409, 502), decide() restores the approval card, returns the phase to awaiting approval, and shows an error bubble, so the user can re-decide. The history panel is labeled "threads for this API key" because the list endpoint returns all threads for the shared key, not per-user threads. The console also fetches the thread list on first load, so history shows before any thread starts.
- Why: the history lived in React state, which the thread opener cleared on switch, and a failed approval POST dropped the card with no way to re-decide. Decided-by is the caller-asserted value from the approval record, per the S12 honesty note.

## 2026-10-15: FIX: rate-limit waits now show in the console
- Files: src/ops_copilot/llm.py, src/ops_copilot/api/runner.py, web/lib/api.ts, web/app/page.tsx
- What: a rate_limit_sink ContextVar carries the event bus and thread id into ainvoke_patient during a run; on a 429 it publishes a rate_limited event (wait seconds, context), and the console appends a system bubble ("rate limited, waiting ~20s before retrying (triage:step2)…"). The phase stays running.
- Why: waits were log-only, so the console showed silent "working…" and the user read a rate-limit wait as a stall. A failed publish cannot break the retry.

## 2026-10-15: FIX: unverified-action tag now needs an actual assertion
- Files: src/ops_copilot/guardrails.py, tests/unit/test_claim_guardrail.py
- What: claims_execution now matches only first-person assertions ("I refunded", "we have processed") or passive-with-auxiliary assertions ("the order has been refunded"). Bare participles ("orders processed", "Comment posted on the PR") no longer match. One existing test updated with the rationale.
- Why: the old regex matched past-tense verbs anywhere in worker prose, so a read-only revenue summary got branded "[unverified action claim]", the tag prefixed the task summary, and a guardrail message was appended to conversation state, polluting context and confusing synthesis.

## 2026-10-15: TEST: resume, transcript, normalizer, and claim-guardrail tests
- Files: tests/unit/test_llm_normalizer.py (new), tests/unit/test_runner_resume_bus.py (new), tests/integration/test_thread_transcript.py (new), tests/unit/test_synthesis_user_message.py, tests/unit/test_claim_guardrail.py
- What: 6 normalizer tests (append-only, unchanged-if-present, tool transcripts not split, rate-limit publish with sink, retry without sink, normalizer inside ainvoke_patient), 2 resume-bus tests (resume publishes tokens to the bus, normal turn stays generator-only), 3 transcript tests (conversation returned, empty for a fresh thread, 403 without/wrong token), 2 synthesis tests (prompt carries the user query), and the claim-guardrail update (bare participles not flagged, first-person and passive still flagged). The resume-bus test fails on reverted code (tokens discarded) and passes on fixed code.
- Why: so each fix in this pass is proven by a test that fails without it. A dedicated test_thread_list.py for the list endpoint was not written; the list endpoint is covered indirectly by the panel behavior.

## 2026-10-15: DOC: README notes the uv lock flow for reproducible builds
- Files: README.md
- What: added a line telling the host operator to run `uv lock` on the dev machine and switch the Dockerfile back to `uv sync --frozen` when locking dependencies.
- Why: no lockfile is generated in this sandbox, and the dev machine is the source of truth for dependency resolution. The Dockerfile already documents the path back to --frozen once a lockfile exists.

## 2026-10-15: DOC: README notes F5 starts a new thread session by design
- Files: README.md
- What: added a line stating that a browser page refresh (F5) requires starting a new thread session by design, to preserve per-thread isolation.
- Why: thread tokens stay in memory only, never persisted, so a refresh loses them. Moving them to sessionStorage was considered and rejected: module-scoped storage is not reachable from window scope, while sessionStorage is globally reachable by any script in the origin, and tokens have no expiry, so persistence would extend exposure. A token re-issue endpoint was also considered and rejected: with a shared API key it would let any key holder re-mint another thread's token and break S4 isolation.

## 2026-10-15: DOC: README documents the M1 ledger restart window as accepted risk
- Files: README.md
- What: added a Known limitations bullet: the single-use approval-token ledger lives in process memory, so an API restart inside the 15-minute token window resets it. Primary defenses remain token non-exposure, TTL plus target/content binding plus signature, and the replay guard.
- Why: keeping the source-control MCP service database-free preserves the service boundary and least privilege. The TTL was left at 900 seconds: tightening to 300 would pressure human reviewers and risk legitimate approvals expiring mid-review.

## 2026-10-15: DOC: README adds a local debugging snippet
- Files: README.md
- What: added a "Local Debugging & Inspection" snippet with docker compose exec postgres psql commands for the ops_ops and ops_checkpoints databases, plus a line that MCP ports are intentionally unpublished.
- Why: no override file or token-printing helper was created. Publishing MCP ports on loopback would reintroduce the S1 gate bypass for any local process, and printing tokens would put live credentials in shell history. Compose exec inspects the database without either risk.

## 2026-10-15: FIX: E501 global ignore removed, remaining long lines fixed
- Files: pyproject.toml, src/ops_copilot/graph/gate.py, src/ops_copilot/guardrails.py, scripts/bootstrap_db.py, tests/integration/test_ops_db.py, src/ops_copilot/graph/prompts.py
- What: removed E501 from the ruff ignore list. Wrapped the 2 plain f-strings and the 2 SQL strings via concatenation, verified byte-identical to the originals. The 2 prompts.py lines got a targeted per-file ignore instead of inline noqa.
- Why: a global ignore blinded the linter repo-wide for the sake of 6 lines, and the parallel-pass file locking that justified it is over. The prompts.py lines sit inside triple-quoted prompt strings, so appending `# noqa: E501` to them would change the prompt bytes and invalidate the eval baseline; the per-file ignore suppresses E501 for prompts.py (which today has only those 2 long lines) without touching the bytes.

## 2026-10-15: FIX: lint fix on the proxy port check
- Files: src/ops_copilot/mcp/client.py
- What: assigned the port check result instead of leaving it as a bare expression.
- Why: the linter flags a bare expression as a useless statement, so the project's own lint gate failed. Same behavior, no logic change.

## 2026-10-15: FIX: missing await on async database reads broke three API paths
- Files: src/ops_copilot/api/main.py
- What: added await to three fetchone() calls on the async Postgres cursor (proposal lookup, thread-token lookup, approval insert check).
- Why: fetchone() on an async cursor is itself a coroutine, so without await each call returned a coroutine object instead of a row. The proposal endpoint 503d, the thread-token check always failed, and the approval insert was silently skipped, so the API approval flow could never complete. The existing tests missed it because one test file replaces the token lookup with a fake. A sweep of the whole src tree found no other unawaited async cursor calls; the MCP server uses sync psycopg, where fetchone() is correctly synchronous.

## 2026-10-15: FIX: stdio MCP servers now receive their environment
- Files: src/ops_copilot/mcp/client.py
- What: the stdio server configs now carry an env built from OPS_* settings, basic process vars, and only proxy vars that parse cleanly. Malformed proxy values (like a NO_PROXY containing "[::1]") are dropped instead of forwarded. Proxy URL validation also checks the port, so values with a bad port (like http://proxy:abc, http://proxy:80x, or http://proxy:99999) are dropped instead of crashing httpx, which raises on those at client construction.
- Why: the SDK spawn inherits only a small safe subset on Linux, so OPS_DATABASE_URL never reached the servers and every database call failed in stdio mode, which is the documented no-Docker path. The HTTP path is unchanged.

## 2026-10-15: TEST: regression tests for the async-fetchone and stdio-env bugs
- Files: tests/integration/test_api_async_db.py, tests/unit/test_stdio_env.py, tests/integration/test_mcp_stdio.py
- What: the async tests exercise the real token lookup, approval insert, and proposal endpoint against a real async pool with no fakes; the stdio tests prove the config env carries OPS_* and drops malformed proxy values, plus a live test that spawns the real ops-db server and runs a query through it.
- Why: so neither bug can silently come back. All new tests were run against the unfixed code first and failed there (21 failed, 1 passed), then passed on the fixed code (22 passed).

## 2026-10-14: TEST: regression suite for the fix pass (9 files)
- Files: tests/regression/conftest.py
- What: sets up throwaway test values (fake API key, fake approval secret, fake audit log path) so the regression tests run with no live services, no network, and no database.
- Why: so every fix below is proven by a test that anyone can run, and the suite never touches real credentials or real data.

## 2026-10-14: TEST: H1 fail-closed auth regression tests
- Files: tests/regression/test_h1_auth_fail_closed.py
- What: proves the API refuses to boot at all when no API key is configured, and that the key check rejects wrong keys and accepts the right one.
- Why: so the unauthenticated-by-default hole can never silently come back; a deployment without a key simply cannot start.

## 2026-10-14: TEST: H2 replay-guard regression tests
- Files: tests/regression/test_h2_replay_guard.py
- What: proves that replaying from a checkpoint past the approval gate is refused and nothing re-executes, while replaying from an earlier checkpoint still works. Also proves the thread-token check still sits in front of the endpoint.
- Why: so the duplicate-comment-via-replay hole stays closed without breaking the legitimate time-travel demo.

## 2026-10-14: TEST: H4 CTE-wrapped-write regression tests
- Files: tests/regression/test_h4_cte_wrapped_writes.py
- What: proves the SQL policy rejects writes hidden inside WITH clauses (delete, update, insert tucked into CTEs), while plain read-only CTEs still pass.
- Why: so the exact attack payloads from the security review are permanently covered; if the policy regresses, the suite fails.

## 2026-10-14: TEST: M1 single-use token regression tests
- Files: tests/regression/test_m1_token_single_use.py
- What: proves every approval token carries a unique id, tokens without one are rejected, and a used token cannot post a second time (the used-token ledger in post_comment).
- Why: so a captured token can never be replayed to post duplicate comments within its lifetime.

## 2026-10-14: TEST: M2 default-repo target regression tests
- Files: tests/regression/test_m2_default_repo_target.py
- What: proves the approval token is minted for the normalized repo name, so comments on the default repo verify correctly instead of being rejected after approval.
- Why: so the main demo path (commenting without naming a repo explicitly) actually works end to end.

## 2026-10-14: TEST: M5 planner-failure message regression tests
- Files: tests/regression/test_m5_planner_failure_message.py
- What: proves that when the planner crashes, an explanatory message lands in the chat instead of the run ending in silence.
- Why: so a dead AI provider shows the user what happened rather than a spinner followed by nothing.

## 2026-10-14: TEST: M6 PII masking regression tests
- Files: tests/regression/test_m6_pii_masking.py
- What: proves emails and phone numbers are masked in database column samples and in GitHub issue and pull-request titles, not just in bodies.
- Why: so private contact details cannot leak through the fields the original code left unmasked.

## 2026-10-14: TEST: M9/S2 single-statement regression tests
- Files: tests/regression/test_m9_propose_write_single_statement.py
- What: proves stacked SQL (two statements in one proposal, like an update followed by a delete) is rejected at the policy level before any database call, with the database stubbed out to prove rejection happens in policy, not the driver.
- Why: so the now load-bearing single-statement rule (stacked statements were proven to execute at apply time) can never be weakened without the suite failing.

## 2026-10-14: TEST: approval secret set earlier for full-suite runs
- Files: tests/conftest.py
- What: the test approval secret is now set in the top-level conftest, which loads before any test module imports the API (whose settings are cached at import time).
- Why: so the M2 regression tests see the secret in full-suite runs instead of failing on a stale cached empty value.

## 2026-10-14: S4/H2: regression test stub updated for thread tokens
- Files: tests/regression/test_h2_replay_guard.py
- What: the stub request now carries a real rate limiter and stubs out the thread-token check, so the two H2 replay-guard tests exercise the guard instead of failing on the newer token gate.
- Why: so the regression suite stays green after the S4 auth change without weakening what the tests prove.

## 2026-10-14: DOC: honest docstrings on the audit and proposal endpoints
- Files: src/ops_copilot/api/main.py
- What: the thread-audit docstring no longer claims apply records carry thread ids (they do not), and the proposal endpoint docstring now states its honest limit: proposals are not linked to threads, so any valid thread token can read any proposal.
- Why: so the docs do not promise a boundary the code does not enforce.

## 2026-10-14: DOC: .env.example corrections
- Files: .env.example
- What: the WebSocket key is now documented as the x-api-key subprotocol (the query-string form no longer works); added the missing OPS_ADMIN_DSN with guidance; the example DSNs now tell the user to substitute their generated POSTGRES_PASSWORD instead of shipping ops:ops.
- Why: so the setup instructions match the code and contain no weak credential.

## 2026-10-14: M7: audit hash chain keyed by file path
- Files: src/ops_copilot/audit.py
- What: the chain-resume cache is now keyed by absolute file path instead of a single global, so two audit logs in one process cannot cross-contaminate each other's chains.
- Why: so the tamper-evidence guarantee holds when several log files are open at once, as in tests.

## 2026-10-14: S7: queue bound wired to the shared constant
- Files: src/ops_copilot/api/events.py, src/ops_copilot/api/ratelimit.py
- What: the subscriber queue now uses the WS_QUEUE_MAXSIZE constant instead of a hardcoded 512, so the documented bound is the enforced one.
- Why: so there is one source of truth for the limit instead of two numbers that can drift apart.

## 2026-10-14: M2: reviewed text matches the minted token
- Files: src/ops_copilot/graph/gate.py
- What: the gate now stores the stripped comment body in the payload alongside the normalized repo, so what the human reviews is byte-identical to what the token covers and what gets posted.
- Why: so a trailing space cannot make an approved comment fail at post time.

## 2026-10-14: DOC: test count updated to 137
- Files: README.md
- What: corrected to 137 tests (124 run without a database, 13 need Postgres) after the testing team's regression suite and the new rate-limit tests landed.
- Why: so the count in the docs matches the suite.

## 2026-10-14: S9: stray audit artifact removed, data/ ignored
- Files: .gitignore
- What: deleted the test-run data/audit.jsonl artifact and added data/ to .gitignore.
- Why: so local run output never gets committed.

## 2026-10-14: DOC: stray em dashes removed
- Files: src/ops_copilot/graph/supervisor.py, web/app/page.tsx
- What: removed leftover em dashes from a planner prompt string and three UI error messages.
- Why: housekeeping to match the project's no-em-dash rule.

## 2026-10-14: S8: mixed-proposal summaries say the label is a guess
- Files: src/ops_copilot/graph/workers.py
- What: when a proposal's table label is best-effort (mixed write verbs), the interrupt summary now says so plainly instead of presenting the guessed table as fact.
- Why: so the reviewer is told to read the SQL itself rather than trusting a label on a mixed statement.

## 2026-10-14: S9: web image gets its own .dockerignore
- Files: web/.dockerignore (new)
- What: added a .dockerignore for the web build context covering .env files, keys, node_modules, and build output.
- Why: so secrets cannot leak into the web image, which builds with its own context.

## 2026-10-14: S12: decided_by honesty note in code
- Files: src/ops_copilot/api/schemas.py
- What: added a code comment on the ApprovalRequest schema stating decided_by is caller-asserted and only as trustworthy as the shared API key.
- Why: so future readers do not mistake the free-form field for verified identity.

## 2026-10-14: S3: database role split documented
- Files: README.md, docker-compose.yml
- What: compose now carries a comment and README Known limitations a line stating the app DSNs should use a least-privilege role (no CREATEDB, no superuser), with the privileged account reserved for scripts/bootstrap_db.py, which needs CREATEDB to create the databases. No code changes: the current single-role setup works, splitting roles is a deployment step, not a code step.
- Why: so a production deploy does not run everyday queries as a superuser-grade role.

## 2026-10-14: S5: read-path policy rejects write-shaped SELECTs
- Files: src/ops_copilot/guardrails.py, tests/unit/test_guardrails.py, README.md
- What: enforce_read_only now scans the whole statement body (subqueries included, comments and string literals folded first) for anything write-shaped: SELECT ... INTO is rejected (it creates a table), SELECT ... FOR UPDATE / FOR NO KEY UPDATE is rejected (row locks), and any data-modifying verb anywhere in the body is rejected. This builds on the H4 WITH-clause fix. Proven by execution: 14 hostile shapes blocked (the three finding examples, five write-CTE shapes, five own variants: nested DML subqueries, mixed case, schema-qualified INTO, FOR NO KEY UPDATE), 9 legit shapes still pass (plain selects, CTE selects, verb words inside string literals, updated_at-style columns, comments).
- Why: so a destructive query cannot hide behind a SELECT first keyword.

## 2026-10-14: S8: proposal table labels come from a full-body scan
- Files: src/ops_copilot/guardrails.py, src/ops_copilot/mcp/servers/ops_db.py, tests/unit/test_guardrails.py
- What: write_target no longer uses a start-anchored regex; it scans the full statement body for write verbs with their tables. Exactly one distinct verb -> (verb, its target); none -> action "unknown"; several -> action "mixed" with the first verb's table. propose_write now accepts when the declared action is one of the verbs actually found (so "mixed" statements with a matching declared action still register), and its response marks the label: target_label "best-effort" plus a plain-words note in the message for mixed statements, telling the reviewer to read the SQL itself. The WriteTarget docstring states the label is best-effort, never verified fact.
- Why: so `insert into audit_log select * from (delete from orders returning *) d` is labeled "mixed", not a benign insert on audit_log. (Done in this pass: the interrupt summary in graph/workers.py now appends the best-effort wording for mixed proposals.)

## 2026-10-14: S9: ignore files for secrets and build junk
- Files: .gitignore (new), .dockerignore (new)
- What: both cover .env, .env.* (with an explicit !.env.example exception so the example stays committable), *.pem, *.key, *_secret*, __pycache__/, *.pyc, .pytest_cache/, .ruff_cache/, uploads/. The root .dockerignore covers the `build: .` context (API + MCP images).
- Why: so the .env the quickstart creates cannot be committed or baked into an image. (Done in this pass: web/.dockerignore added for the web build context.)

## 2026-10-14: S12: decided_by is caller-asserted, documented honestly
- Files: README.md
- What: one line in Known limitations: with a single shared API key there is no per-user identity, so decided_by is caller-asserted and only as trustworthy as the key.
- Why: so nobody reads an approval record as authenticated identity. Follow-up for the lead: the brief code comment at the ApprovalRequest schema (api/schemas.py) still needs adding; schemas.py is outside this fixer's region.

## 2026-10-14: S15: dependency pins tightened, lockfile fallback
- Files: pyproject.toml, Dockerfile, docker-compose.yml
- What: dropped open-ended >= pins where cheap: litellm>=1.60,<2, uvicorn[standard]>=0.34,<1, httpx>=0.27,<1, and dev extras ruff>=0.9,<1, mypy>=1.14,<2, pytest>=8.3,<9, pytest-asyncio>=0.25,<1, respx>=0.22,<1. structlog>=24.4 left open-ended deliberately: structlog 25.x exists and capping its major without testing the 25 line could break installs; flagged, not changed. Removed types-psycopg2 (grep-confirmed zero uses). Pinned the two :latest images to verified tags: ollama/ollama:0.40.4, arizephoenix/phoenix:version-20.21 (both behind optional compose profiles). uv.lock: the Mac was offline so the fetch failed; per the fallback, dropped --frozen from the Dockerfile and removed uv.lock from the COPY line (a missing COPY source would fail the build), with a comment explaining how to switch back when a lockfile is added.
- Why: so fresh installs and image builds resolve to known-good ranges instead of floating majors.

## 2026-10-14: S17: live_chat.py sends the API key
- Files: scripts/live_chat.py
- What: reads OPS_API_KEY from the environment and sends it exactly like the web console: X-API-Key header on the thread-creation POST, x-api-key.<key> subprotocol token on the WebSocket, never the query string. Also handles the thread-token contract defensively: if creation returns thread_token (S4), it is sent back as an x-thread-token.<tok> subprotocol; absent, nothing is sent. Prints a note when no key is configured.
- Why: so the script works against the fail-closed API instead of being rejected.

## 2026-10-14: S18: eval offline path is explicit
- Files: evals/runner.py, evals/judges.py
- What: the dummy provider key is now the clearly-named constant OFFLINE_DUMMY_KEY with a comment stating it is intentional for keyless offline evals, and the runner sets OPS_EVAL_OFFLINE=1. judges.py returns None immediately when that flag is set, so the dummy key can no longer defeat the judge's "credentials present" guard. Eval harness still reproduces 6/6 at 1.0.
- Why: so the fake key reads as deliberate instead of accidental, and the live judge cannot be tricked on.

## 2026-10-14: S4: per-thread ownership tokens
- Files: src/ops_copilot/api/main.py, src/ops_copilot/api/schemas.py, scripts/bootstrap_db.py, web/lib/api.ts, web/app/page.tsx, tests/integration/test_api_ws.py, README.md
- What: thread creation now mints a secret token (secrets.token_hex(32)); only its sha256 is stored, in a new thread_tokens table. ThreadCreated returns it as thread_token. Every thread-scoped endpoint (state, history, approvals, replay, the new thread audit endpoint below) requires it as the X-Thread-Token header (403 on mismatch/missing, compare_digest), and the WebSocket requires it as an x-thread-token.<tok> subprotocol (close 4403 on failure). The console keeps the token in memory per thread, sends the header on thread-scoped fetches, and offers the subprotocol on connect. Added GET /threads/{id}/audit (API key + thread token) returning records attributable to the thread, since no thread-scoped audit endpoint existed; approval/rejection records now carry the thread id in their audit detail. README Known limitations documents the remaining boundary: one shared API key plus per-thread tokens, no user/principal concept, full multi-user auth out of scope.
- Why: so one API key holder can no longer read, approve, replay, or drive another thread.

## 2026-10-14: S6: interrupt payloads no longer carry the full SQL
- Files: src/ops_copilot/graph/workers.py, src/ops_copilot/api/main.py, src/ops_copilot/api/schemas.py, web/lib/api.ts, web/app/page.tsx, tests/integration/test_graph_flow.py
- What: the db_write interrupt payload now carries kind + proposal_id + action + table + summary only (sql and rationale dropped). New endpoint GET /threads/{id}/proposals/{proposal_id} (API key + thread token) returns the exact registered SQL from the proposals table for the reviewer; the executor already re-reads the SQL at apply time, so nothing else needed it. The console's ApprovalCard fetches the SQL on demand via a new getProposal() helper. The one test asserting on payload["sql"] now asserts the minimized shape instead.
- Why: so the full proposal SQL stops landing in the checkpoints DB, audit detail, and /state responses.

## 2026-10-14: S11: approval_token stripped from the /state view
- Files: src/ops_copilot/api/main.py
- What: the state endpoint now drops the approval_token key from the awaiting payload before responding (the token lives in awaiting between approval and execution).
- Why: so any API caller with a thread token cannot lift a live approval token from the state view.

## 2026-10-14: S7: simple rate limiting and WebSocket guards
- Files: src/ops_copilot/api/ratelimit.py (new), src/ops_copilot/api/main.py, tests/unit/test_ratelimit_errors.py, tests/integration/test_api_ws.py
- What: new in-memory sliding-window RateLimiter (per API key): POST /threads capped at 20/min, POST /threads/{id}/replay at 10/min, 429 when exceeded. WebSocket: global cap of 100 concurrent connections (close 4429 when full), sockets idle 10 minutes with no inbound frames are closed (4408), and the per-subscriber event queue stays bounded at 512 (existing EventBus bound, now documented as policy). All constants documented in the module docstring.
- Why: so thread creation and replay cannot be used for unbounded LLM-spend, and sockets cannot accumulate forever.

## 2026-10-14: S13: client-facing errors go through an allowlist
- Files: src/ops_copilot/api/errors.py (new), src/ops_copilot/api/main.py, src/ops_copilot/api/runner.py, src/ops_copilot/graph/supervisor.py, tests/unit/test_ratelimit_errors.py
- What: new public_error(exc) helper maps known safe types (ThreadBusy, timeouts) to their messages and returns "internal error; check server logs" for everything else. Used at the runner's WS error events, the supervisor's planner-failure chat message, and the approval endpoint's 502 detail. Server logs still get the full text.
- Why: so raw exception text (SQL fragments, paths, provider internals) never reaches WS clients or /state.

## 2026-10-14: S16: API binds loopback by default
- Files: src/ops_copilot/settings.py, .env.example
- What: settings api_host default changed from 0.0.0.0 to 127.0.0.1. docker compose already passes --host 0.0.0.0 explicitly for the api service, so containers are unaffected; .env.example documents the new default with a commented-out LAN override.
- Why: so a local run does not listen on every interface unless asked.

## 2026-10-14: S19: WebSocket validates inbound frames
- Files: src/ops_copilot/api/main.py, web/lib/api.ts, tests/integration/test_api_ws.py
- What: the receive loop now answers malformed JSON and non-dict frames (arrays, strings) with a {"type":"error","detail":"..."} event (message included for the console) instead of dying with 1011; the session survives and keeps working.
- Why: so one bad frame cannot kill a live session.

## 2026-10-14: H1/S3: API tests set their own credentials
- Files: tests/integration/test_api_ws.py
- What: the test module now sets throwaway OPS_API_KEY and POSTGRES_PASSWORD itself instead of relying only on tests/conftest.py. Key is passed as X-API-Key header on HTTP and x-api-key subprotocol on WS, matching the console.
- Why: so the 5 API tests pass even when an external runner bypasses conftest, keeping the suite green under the new fail-closed boot checks.

## 2026-10-14: M1: approval tokens are single-use now
- Files: src/ops_copilot/security.py, src/ops_copilot/mcp/servers/source_control.py
- What: every minted token carries a unique id, and the GitHub server keeps a ledger of used ids, rejecting any token the second time. Tokens without an id are treated as invalid.
- Why: so a captured approval token cannot be replayed to post again within its 15-minute window. Honest limit: the ledger lives in memory per process, so the replay guard remains the primary defense.

## 2026-10-14: M3: post-replay status reads the new run, not the old checkpoint
- Files: src/ops_copilot/api/runner.py
- What: the pinned checkpoint id is removed from the config before the final state read after a replay.
- Why: so the events reported after a replay describe what actually just ran.

## 2026-10-14: M4: approval responses tell the truth
- Files: src/ops_copilot/api/main.py
- What: the approval endpoint now waits for the resume to finish (2-minute cap) and reports real failures, timeout as 504, busy thread as 409, other errors as 502, instead of always answering accepted.
- Why: so clicking approve never shows success while nothing happened behind the scenes.

## 2026-10-14: M5: planner failures speak up
- Files: src/ops_copilot/graph/supervisor.py
- What: when plan generation fails, the chat now shows an explanatory message instead of ending silently.
- Why: so users see what went wrong instead of a spinner followed by nothing.

## 2026-10-14: M6: PII masking covers samples and titles
- Files: src/ops_copilot/mcp/servers/ops_db.py, src/ops_copilot/mcp/servers/source_control.py
- What: column profile samples and issue/PR titles now go through the same PII masking as everything else.
- Why: so personal data cannot leak through the two paths the masker was skipping.

## 2026-10-14: M7: audit log is hash-chained and records each decision once
- Files: src/ops_copilot/audit.py, src/ops_copilot/graph/gate.py
- What: every audit record now links to the previous record's hash, and the chain resumes correctly after restarts. The duplicate gate emits were removed, so each human decision is recorded exactly once.
- Why: so tampering with the log is detectable, and the log is not noisy with double entries.

## 2026-10-14: M9/S2: proposals must be exactly one statement
- Files: src/ops_copilot/mcp/servers/ops_db.py
- What: propose_write now requires exactly one SQL statement. Proven by execution: the kill payload `update orders set x=1; delete from users` is rejected before any database touch.
- Why: so a stacked destructive statement cannot ride in behind a benign-looking proposal and execute at apply time.

## 2026-10-14: L5: double approvals cannot duplicate rows
- Files: scripts/bootstrap_db.py, src/ops_copilot/api/main.py
- What: unique constraint on approvals(proposal_id), and the approval recorder reuses an existing row instead of inserting a duplicate.
- Why: so double-clicking approve does not create a mess of duplicate records.

## 2026-10-14: L7: thread locks no longer grow forever
- Files: src/ops_copilot/api/runner.py
- What: the per-thread lock map is capped at 1000 entries, evicting the oldest idle locks.
- Why: so long-running servers do not slowly leak memory.

## 2026-10-14: L12: row limit cannot go negative
- Files: src/ops_copilot/mcp/servers/ops_db.py
- What: the max_rows parameter is clamped to at least 1 while keeping the settings-driven default.
- Why: so a negative value cannot produce confusing empty results.

## 2026-10-14: L15: apply-time checks run inside the transaction
- Files: src/ops_copilot/mcp/servers/ops_db.py
- What: the existence and approval-status checks in apply_approved_write now run inside the transaction with row locking. The unique constraint stays as the safety net.
- Why: so two simultaneous applies cannot both slip through the check.

## 2026-10-14: S3/S10: database credentials come from the environment only
- Files: scripts/bootstrap_db.py, src/ops_copilot/mcp/servers/ops_db.py, tests/integration/test_ops_db.py
- What: no hardcoded ops/ops remains in any of the three (grep-verified). Bootstrap reads the admin DSN from the environment and exits loudly when unset. The DB server module raises a clear error without its URL. The test file uses env-driven DSNs too.
- Why: so the weak password exists nowhere in the codebase, not even as a fallback.

## 2026-10-14: H1: API refuses to boot without an API key
- Files: src/ops_copilot/api/main.py, tests/conftest.py
- What: the API now raises a clear error at startup when OPS_API_KEY is not set, instead of running with every endpoint open. Test-only credentials were added in conftest so the suite stays green.
- Why: so a stranger on the network cannot approve database changes.

## 2026-10-14: H2: replay cannot re-run past the approval gate
- Files: src/ops_copilot/api/main.py
- What: the replay endpoint now refuses (409) any checkpoint whose next step is the executor.
- Why: so replaying an old conversation cannot post a duplicate GitHub comment or repeat a real-world action.

## 2026-10-14: H3: compose passes the approval secret to the GitHub server
- Files: docker-compose.yml
- What: OPS_APPROVAL_SECRET is now a required variable on the source-control-mcp service. Compose fails fast with a clear message when it is missing.
- Why: so approved GitHub comments actually post in the Docker setup instead of failing with "posting disabled".

## 2026-10-14: H4: SQL checker catches writes hidden in WITH clauses
- Files: src/ops_copilot/guardrails.py
- What: any WITH statement containing insert, update, or delete anywhere inside it is now rejected. Comments and string literals are stripped first, so legitimate queries still pass (verified: 6 hostile shapes blocked, 6 legit pass).
- Why: so a destructive query cannot slip past the safety check disguised as a read.

## 2026-10-14: M2: approval tokens now name the right repo
- Files: src/ops_copilot/repo.py (new), src/ops_copilot/graph/gate.py, src/ops_copilot/mcp/servers/source_control.py
- What: a new shared normalize_repo() is the single source of truth for repo names. The gate resolves the default repo before stamping the approval and stamps the trimmed comment text, matching exactly what the server verifies.
- Why: so approving a comment for the default repo actually posts instead of failing on a name mismatch.

## 2026-10-14: M8/S14: API key travels safely to the web console
- Files: web/lib/api.ts, web/app/page.tsx, src/ops_copilot/api/main.py, tests/integration/test_api_ws.py
- What: the console has a password-type API key field kept in memory only. The key is sent as an X-API-Key header on requests and as an x-api-key subprotocol token on the WebSocket, never in the URL. The server checks it with a timing-safe comparison and closes the socket (4401) on failure.
- Why: so turning on the password does not break the website, and the key never leaks through browser history or server logs.

## 2026-10-14: S1: internal ports no longer published to the host
- Files: docker-compose.yml
- What: removed published ports for postgres (5432) and both MCP servers (8001/8002). They are reachable only inside the compose network. The API (8000) and web console (3000) stay published.
- Why: so nobody on the network can bypass the API, reach the database directly, and approve their own writes.

## 2026-10-14: S3: no weak default database password
- Files: src/ops_copilot/settings.py, docker-compose.yml, tests/conftest.py
- What: the hardcoded ops/ops credential is gone. The API and compose fail fast with a clear message when POSTGRES_PASSWORD is not set.
- Why: so a fresh deploy does not ship with a password anyone can guess.

## 2026-10-14: L4: failed executions are labeled failed, not done
- Files: src/ops_copilot/graph/gate.py, src/ops_copilot/graph/state.py
- What: the executor now marks failed runs as "failed" (a new status) instead of "done".
- Why: so a failed run is not mistaken for a finished one, and the recovery logic can pick it up.

## 2026-10-14: L8: timing-safe API key comparison
- Files: src/ops_copilot/api/main.py
- What: the key check now uses hmac.compare_digest instead of ==.
- Why: so attackers cannot guess the key character by character by measuring response times.

## 2026-10-14: L11: pg_stat_file blocked on the read path
- Files: src/ops_copilot/guardrails.py
- What: pg_stat_file was added to the denied functions list.
- Why: so read-only queries cannot be used to probe which files exist on the database server.

## 2026-10-14: DOC: removed dead provider fallback code
- Files: src/ops_copilot/llm.py, src/ops_copilot/settings.py, tests/unit/test_settings_llm.py
- What: removed build_chat_model_chain (never called in production) and the now-dead Settings.provider_credentials(), plus the two tests that covered the dead function. The module docstring now describes what the code actually does.
- Why: so the code does not advertise an "automatic fallback" feature that was never wired up.

## 2026-10-14: DOC: new .env.example with all required secrets
- Files: .env.example (new)
- What: created with every variable the quickstart needs, including the three required secrets (OPS_API_KEY, OPS_APPROVAL_SECRET, POSTGRES_PASSWORD) with generation instructions.
- Why: so the README's first setup step works instead of pointing at a file that does not exist.

## 2026-10-14: DOC: README claims corrected
- Files: README.md, docs/adrs/0006-eval-strategy.md
- What: the quickstart now documents all three required secrets. The CTE sentence now matches the fixed behavior. Test count corrected to 68 (55 run without a database, 13 need Postgres). The CI-gate claim softened to "CI-ready but not wired". Removed the "GitHub Actions" and "automatic fallback ordering" claims. Added an honest audit-log trust-boundary note to Known limitations.
- Why: so the docs describe what the project actually does instead of promising things that are not there.

## 2026-10-14: L17: pytest no longer hides deprecation warnings
- Files: pyproject.toml, tests/conftest.py
- What: removed the DeprecationWarning filter from the pytest config (suite still passes, 39/39). Re-wrapped one long line in conftest.py. The E501 ignore stays for now because the 7 violations sit in other fixers' files.
- Why: so warnings about outdated dependencies stay visible instead of being silently swallowed.

## 2026-10-14: DOC: removed unused code
- Files: src/ops_copilot/api/events.py, src/ops_copilot/api/schemas.py
- What: removed EventBus.has_subscribers and StateView.pending_interrupts, both with zero uses.
- Why: so dead code does not confuse the next person reading the project.
