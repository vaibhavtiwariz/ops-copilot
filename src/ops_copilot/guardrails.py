"""Policy layer for database access.

The reference postgres MCP server that Anthropic archived in 2025 shipped with
a read-only mode that could be escaped through multi-statement input. These
checks are the lesson from that incident, applied before any statement reaches
the driver:

1. one statement per call,
2. read verbs only for the read path,
3. a denylist of side-channel functions (pg_sleep, pg_read_file, dblink...),
4. row and wall-clock caps enforced at the session level.
"""

import re
from dataclasses import dataclass

WRITE_VERBS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "drop",
        "alter",
        "truncate",
        "grant",
        "revoke",
        "create",
        "copy",
        "comment",
        "reindex",
        "cluster",
        "vacuum",
        "call",
        "do",
    }
)

# Functions that have no business running behind an analytics copilot.
DENIED_FUNCTIONS = frozenset(
    {
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "pg_logical_slot_get_changes",
    }
)

_COMMENT_RE = re.compile(r"(--[^\n]*$)|(/\*.*?\*/)", re.MULTILINE | re.DOTALL)
_SEMICOLON_SPLIT_RE = re.compile(r";\s*(?:$)")


class SqlPolicyError(ValueError):
    """Raised when a statement fails the read-only policy."""


def strip_comments(sql: str) -> str:
    return _COMMENT_RE.sub("", sql).strip()


def split_statements(sql: str) -> list[str]:
    stripped = strip_comments(sql)
    if not stripped:
        return []
    return [part.strip() for part in stripped.split(";") if part.strip()]


def first_keyword(statement: str) -> str:
    match = re.match(r"\(? *(with|explain|select|table|values|[a-z]+)", statement.lower())
    return match.group(1) if match else ""


def _reject_write_shaped_read(body: str) -> None:
    """Reject read-shaped statements that smuggle writes in their body.

    The first keyword can be a plain SELECT while the rest hides a write:
    SELECT ... INTO creates a table, SELECT ... FOR UPDATE takes row locks,
    and a FROM-clause subquery can run DML (... RETURNING) without any WITH
    in sight. The body arrives with comments and string literals already
    folded, so words inside data can neither trip nor dodge this scan.
    """
    if re.search(r"\binto\b", body):
        raise SqlPolicyError("SELECT ... INTO is not permitted on the read path")
    if re.search(r"\bfor\s+(?:no\s+key\s+)?update\b", body):
        raise SqlPolicyError("row-locking FOR UPDATE is not permitted on the read path")
    hit = re.search(r"\b(" + "|".join(sorted(WRITE_VERBS)) + r")\b", body)
    if hit:
        raise SqlPolicyError(
            f"data-modifying verb {hit.group(1)!r} is not permitted in a read-path statement"
        )


def enforce_read_only(sql: str) -> str:
    """Validate a single SELECT-shaped statement; returns normalized SQL."""
    statements = split_statements(sql)
    if len(statements) != 1:
        raise SqlPolicyError("exactly one statement is allowed per call")
    statement = statements[0]

    verb = first_keyword(statement)
    # WITH ... INSERT/UPDATE/DELETE and EXPLAIN ANALYZE on writes are writes.
    effective = verb
    lowered = statement.lower()
    # String literals are folded before any keyword scan so words inside data
    # can never trip (or dodge) the policy.
    lowered_no_strings = re.sub(r"'(?:[^']|'')*'", "''", lowered)
    if verb == "with":
        # A WITH statement may only describe reads. Reject any data-modifying
        # verb anywhere in it: CTE bodies and the final query alike. Plain
        # CTE selects keep passing.
        if re.search(r"\b(insert|update|delete)\b", lowered_no_strings):
            raise SqlPolicyError(
                "data-modifying statements are not permitted inside WITH clauses on the read path"
            )
        after_cte = re.search(r"\)\s*(select|insert|update|delete|values)", lowered)
        effective = after_cte.group(1) if after_cte else "unknown"
    if verb == "explain":
        tail = re.sub(r"^explain\s+(analyze|analyse)?\s*", "", lowered)
        tail = re.sub(r"^(verbose|costs|buffers|timing|summary|format\s+\w+)\s+", "", tail)
        effective = first_keyword(tail.lstrip()) or "unknown"

    if effective in {"select", "table", "values", "with", "explain"}:
        # S5: the first keyword being a read verb is not enough; scan the
        # whole body, subqueries included, for anything write-shaped.
        _reject_write_shaped_read(lowered_no_strings)

    if effective in WRITE_VERBS or effective not in {"select", "table", "values", "explain"}:
        raise SqlPolicyError(f"statement type {effective!r} is not permitted on the read path")

    for fn in DENIED_FUNCTIONS:
        if re.search(rf"\b{fn}\s*\(", lowered_no_strings):
            raise SqlPolicyError(f"function {fn}() is denied by policy")

    return statement


@dataclass(frozen=True)
class WriteTarget:
    table: str
    action: str
    verbs: tuple[str, ...] = ()
    """Best-effort label, never verified fact.

    "unknown" means no write verb was found at all; "mixed" means several
    distinct verbs appear, so `table` is only the first candidate. Anything
    downstream that shows this to a human must say it is best-effort.
    """


# Full-body target labeling for proposals. Each write verb is matched together
# with the table name that follows it, anywhere in the statement (CTE bodies
# and subqueries included), after comments and string literals are stripped.
_WRITE_VERB_TARGET_RE = re.compile(
    r"\b(insert\s+into|update|delete\s+from|drop\s+table|truncate(?:\s+table)?|"
    r"alter\s+table|create\s+table)\s+([a-z_][a-z0-9_.]*)",
    re.IGNORECASE,
)


def write_target(sql: str) -> WriteTarget:
    """Identify the write verb(s) in a statement, best-effort.

    Exactly one distinct write verb -> (verb, its target). No write verb ->
    action "unknown". Several distinct verbs -> action "mixed" with the first
    verb's table. A statement like `insert into audit_log select * from
    (delete from orders returning *) d` labels as "mixed", not as a benign
    insert on audit_log.
    """
    body = strip_comments(sql)
    body = re.sub(r"'(?:[^']|'')*'", "''", body)
    found: dict[str, str] = {}
    for match in _WRITE_VERB_TARGET_RE.finditer(body):
        verb = match.group(1).split()[0].lower()
        if verb not in found:
            found[verb] = match.group(2)
    if len(found) == 1:
        verb, table = next(iter(found.items()))
        return WriteTarget(table=table, action=verb, verbs=(verb,))
    if not found:
        return WriteTarget(table="", action="unknown", verbs=())
    return WriteTarget(table=next(iter(found.values())), action="mixed", verbs=tuple(found))


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone", re.compile(r"(?<!\w)(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\w)")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
)


def mask_pii(text: str, *, replacement: str = "[redacted]") -> str:
    """Best-effort PII scrubbing for content that flows back to the model.

    GitHub issue bodies are untrusted third-party text; anything scraped from
    them gets masked before it can land in a prompt or a trace export.
    """
    masked = text
    for _, pattern in _PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


class BudgetExceeded(RuntimeError):
    pass


# Past-tense execution claims that should only ever appear after a gated tool
# actually succeeded. Deliberately conservative: false negatives are fine
# (prompts handle those), false positives just add a harmless notice.
_CLAIM_RE = re.compile(
    r"\b(refund(?:ed|s|ing)?|reversed?|credited|deleted|removed|updated?|"
    r"cancelled|canceled|processed|executed|initiated|posted|sent|migrated)\b",
    re.IGNORECASE,
)


def claims_execution(text: str) -> bool:
    """Does this worker output assert that a state-changing action happened?"""
    return bool(_CLAIM_RE.search(text or ""))


def estimate_tokens(text: str) -> int:
    # ~4 chars/token is close enough for budget enforcement; exact counts come
    # from usage_metadata when the provider reports it.
    return max(1, len(text) // 4)


def tokens_of(messages: list) -> int:
    total = 0
    for message in messages:
        usage = getattr(message, "usage_metadata", None)
        if usage and usage.get("total_tokens"):
            total += int(usage["total_tokens"])
        else:
            total += estimate_tokens(str(getattr(message, "content", "")))
    return total


def check_budget(messages: list, limit: int) -> None:
    used = tokens_of(messages)
    if used > limit:
        raise BudgetExceeded(
            f"run consumed ~{used} tokens against a budget of {limit}; "
            "halting before more tool calls"
        )
