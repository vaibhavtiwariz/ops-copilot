"""Database gateway MCP server.

Exposes schema inspection and a tightly fenced read-only query path over
FastMCP. Mutations are never executed directly: workers can only *propose*
writes, which land in a proposals table and wait for a human decision. The
executor later calls apply_approved_write with that proposal id, and the
gateway refuses anything without a matching approval row.
"""

import argparse
import os
import re
from typing import Any

import psycopg
from fastmcp import FastMCP

from ops_copilot.guardrails import enforce_read_only, mask_pii, split_statements, write_target
from ops_copilot.logging import configure_logging, get_logger

configure_logging()
log = get_logger("ops_db_mcp")

mcp = FastMCP(
    "ops-db",
    instructions=(
        "Fenced gateway to the operations Postgres instance. Inspect schema, run "
        "single read-only SELECT statements within row/time caps, and propose "
        "mutations for human approval. Direct write execution is impossible here."
    ),
)

_ident_re = re.compile(r"^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$", re.IGNORECASE)

_conn: psycopg.Connection | None = None
_readonly_conn: psycopg.Connection | None = None


def _settings_from_env() -> tuple[str, int, int]:
    dsn = os.environ.get("OPS_DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "OPS_DATABASE_URL is not set; the ops-db gateway refuses to fall back "
            "to a hardcoded credential. Export OPS_DATABASE_URL first."
        )
    max_rows = int(os.environ.get("OPS_SQL_MAX_ROWS", "200"))
    timeout_ms = int(os.environ.get("OPS_SQL_STATEMENT_TIMEOUT_MS", "8000"))
    return dsn, max_rows, timeout_ms


def connect() -> psycopg.Connection:
    """Control-plane connection: proposals and approved applications only."""
    global _conn
    if _conn is None or _conn.closed:
        dsn, _, timeout_ms = _settings_from_env()
        _conn = psycopg.connect(dsn, autocommit=True)
        _conn.execute(f"SET statement_timeout = {timeout_ms}")
    return _conn


def connect_readonly() -> psycopg.Connection:
    """Query connection: the driver itself refuses writes, belt-and-suspenders."""
    global _readonly_conn
    if _readonly_conn is None or _readonly_conn.closed:
        dsn, _, timeout_ms = _settings_from_env()
        _readonly_conn = psycopg.connect(dsn, autocommit=True)
        _readonly_conn.execute("SET default_transaction_read_only = on")
        _readonly_conn.execute(f"SET statement_timeout = {timeout_ms}")
    return _readonly_conn


def _rows(cursor: psycopg.Cursor) -> list[dict[str, Any]]:
    if cursor.description is None:
        return []
    cols = [desc.name for desc in cursor.description]
    return [dict(zip(cols, row, strict=True)) for row in cursor.fetchall()]


def _check_identifier(name: str) -> str:
    if not _ident_re.match(name):
        raise ValueError(f"invalid object name {name!r}")
    return name


@mcp.tool()
def list_tables(schema: str = "public") -> dict[str, Any]:
    """List user tables with column counts and approximate row counts."""
    conn = connect_readonly()
    rows = _rows(
        conn.execute(
            """
            select c.relname as table_name,
                   count(a.attname) as columns,
                   coalesce(c.reltuples::bigint, 0) as approx_rows
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            left join pg_attribute a
              on a.attrelid = c.oid and a.attnum > 0 and not a.attisdropped
            where n.nspname = %s and c.relkind = 'r'
            group by c.relname, c.reltuples
            order by c.relname
            """,
            (_check_identifier(schema),),
        )
    )
    return {"schema": schema, "tables": rows}


@mcp.tool()
def describe_table(table: str) -> dict[str, Any]:
    """Column names, types, nullability and indexes for one table."""
    _check_identifier(table)
    schema_name, _, bare = table.partition(".")
    if not bare:
        schema_name, bare = "public", table
    conn = connect_readonly()

    columns = _rows(
        conn.execute(
            """
            select column_name, data_type, is_nullable, column_default
            from information_schema.columns
            where table_schema = %s and table_name = %s
            order by ordinal_position
            """,
            (schema_name, bare),
        )
    )
    indexes = _rows(
        conn.execute(
            """
            select indexname as name, indexdef as definition
            from pg_indexes
            where schemaname = %s and tablename = %s
            """,
            (schema_name, bare),
        )
    )
    if not columns:
        raise ValueError(f"table {table!r} not found")
    return {"table": f"{schema_name}.{bare}", "columns": columns, "indexes": indexes}


@mcp.tool()
def run_readonly_query(sql: str, max_rows: int | None = None) -> dict[str, Any]:
    """Run exactly one SELECT/WITH statement under the read-only policy.

    Rows are capped (default from OPS_SQL_MAX_ROWS) and the statement runs
    under a session-level timeout on a connection the driver itself keeps
    read-only.
    """
    _, default_max_rows, _ = _settings_from_env()
    limit = max(1, min(max_rows if max_rows is not None else default_max_rows, default_max_rows))

    normalized = enforce_read_only(sql)
    conn = connect_readonly()
    try:
        with conn.cursor() as cur:
            cur.execute(normalized)
            rows = cur.fetchmany(limit + 1)
            cols = [d.name for d in cur.description] if cur.description else []
    except psycopg.errors.InsufficientPrivilege as exc:
        raise ValueError(f"blocked by read-only session guard: {exc}") from exc
    except psycopg.Error as exc:
        raise ValueError(f"query failed: {str(exc).splitlines()[0]}") from exc

    truncated = len(rows) > limit
    body = [dict(zip(cols, row, strict=True)) if cols else {"value": row} for row in rows[:limit]]
    masked = [
        {key: mask_pii(value) if isinstance(value, str) else value for key, value in r.items()}
        for r in body
    ]
    return {
        "sql": normalized,
        "columns": cols,
        "row_count": len(masked),
        "truncated": truncated,
        "rows": masked,
    }


@mcp.tool()
def profile_column(table: str, column: str) -> dict[str, Any]:
    """Quick distribution stats for one column: nulls, distincts, range."""
    _check_identifier(table)
    _check_identifier(column)
    schema_name, _, bare = table.partition(".")
    if not bare:
        schema_name, bare = "public", table
    qualified = f"{_q(schema_name)}.{_q(bare)}"
    col = _q(column)
    conn = connect_readonly()
    stats = _rows(
        conn.execute(
            f"""
            select
                count(*) as total,
                count(*) - count({col}) as nulls,
                count(distinct {col}) as distinct_values,
                min({col})::text as min_value,
                max({col})::text as max_value
            from {qualified}
            """
        )
    )
    sample = _rows(
        conn.execute(
            f"select {col}::text as value from {qualified} where {col} is not null limit 5"
        )
    )
    masked_sample = [
        {key: mask_pii(value) if isinstance(value, str) else value for key, value in row.items()}
        for row in sample
    ]
    return {
        "table": f"{schema_name}.{bare}",
        "column": column,
        "stats": stats[0],
        "sample": masked_sample,
    }


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


@mcp.tool()
def propose_write(action: str, table: str, sql: str, rationale: str) -> dict[str, Any]:
    """Register a mutation request. Nothing is executed.

    Returns a proposal record that must be approved by a human before
    apply_approved_write may reference it.
    """
    action_norm = action.strip().lower()
    if action_norm not in {"insert", "update", "delete"}:
        raise ValueError("action must be one of insert/update/delete")
    target = write_target(sql)
    # The label is best-effort (see WriteTarget): accept when the declared
    # action is one of the verbs actually found, including in "mixed"
    # statements. "unknown" (no write verb at all) always fails.
    if action_norm not in target.verbs:
        raise ValueError("sql does not match the declared action")
    stmts = split_statements(sql)
    if len(stmts) != 1:
        raise ValueError("exactly one statement is allowed per proposal")
    _check_identifier(target.table)
    if len(sql) > 4000 or len(rationale) > 1000:
        raise ValueError("sql or rationale exceeds length limits")

    conn = connect()
    row = conn.execute(
        """
        insert into proposals (action, table_name, sql_text, rationale, status)
        values (%s, %s, %s, %s, 'pending')
        returning id, created_at
        """,
        (action_norm, target.table, sql, rationale),
    ).fetchone()
    if row is None:  # pragma: no cover - insert..returning always yields a row
        raise RuntimeError("proposal insert returned no row")
    proposal_id, created_at = row[0], row[1]
    log.info("proposal_registered", proposal_id=proposal_id, table=target.table)
    message = "registered for human review; no data was modified"
    if target.action == "mixed":
        # The table label on a mixed statement is a guess at best; say so.
        message += (
            " Target label is best-effort: this statement mixes several write "
            f"verbs ({', '.join(target.verbs)}), so review the SQL itself."
        )
    return {
        "proposal_id": proposal_id,
        "created_at": created_at.isoformat(),
        "status": "pending",
        "message": message,
        "target_label": "best-effort" if target.action == "mixed" else "single",
    }


@mcp.tool()
def apply_approved_write(proposal_id: int) -> dict[str, Any]:
    """Execute a previously approved proposal, exactly as registered."""
    conn = connect()

    try:
        # Check-then-act must happen inside the transaction (with the row
        # locked) so the status cannot change between the check and the
        # write. The unique constraint on applications.proposal_id stays as
        # the final safety net against a racing double apply.
        with conn.transaction():
            proposal = conn.execute(
                "select id, action, table_name, sql_text, status from proposals "
                "where id = %s for update",
                (proposal_id,),
            ).fetchone()
            if proposal is None:
                raise ValueError(f"unknown proposal {proposal_id}")

            pid, action, table, sql_text, status = proposal
            if status != "approved":
                raise ValueError(
                    f"proposal {pid} has status {status!r}; only approved work can be applied"
                )

            already = conn.execute(
                "select 1 from applications where proposal_id = %s", (pid,)
            ).fetchone()
            if already:
                raise ValueError(f"proposal {pid} was already applied")

            conn.execute("set local default_transaction_read_only = off")
            result = conn.execute(sql_text)
            affected = result.rowcount
            conn.execute(
                "insert into applications (proposal_id, rows_affected) values (%s, %s)",
                (pid, affected),
            )
            conn.execute("update proposals set status = 'applied' where id = %s", (pid,))
    except psycopg.Error as exc:
        raise ValueError(f"application failed and rolled back: {exc}") from exc

    log.info("proposal_applied", proposal_id=pid, table=table, rows=affected)
    return {
        "proposal_id": pid,
        "action": action,
        "table": table,
        "rows_affected": affected,
        "status": "applied",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="ops-db MCP server")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    if args.transport == "http":
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
