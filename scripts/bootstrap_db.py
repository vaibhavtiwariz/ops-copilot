"""Create the databases the copilot needs and seed demo operations data.

Run: uv run python -m scripts.bootstrap_db [--with-demo-data]

Safe to re-run: schema statements are idempotent, seeding only fills empty
tables.
"""

import argparse
import asyncio
import os
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

APP_DB = "ops_ops"
CHECKPOINT_DB = "ops_checkpoints"


def _require_dsn(env_name: str) -> str:
    """Read a database DSN from the environment; fail loudly if it is unset.

    Bootstrap never falls back to a hardcoded credential.
    """
    dsn = os.environ.get(env_name)
    if not dsn:
        raise SystemExit(
            f"{env_name} is not set. Bootstrap refuses to use a default database "
            f"credential; export it first, e.g. "
            f"{env_name}=postgresql://<user>:<pass>@localhost:5432/<db>"
        )
    return dsn


SCHEMA_SQL = """
create table if not exists proposals (
    id          bigserial primary key,
    action      text not null,
    table_name  text not null,
    sql_text    text not null,
    rationale   text,
    status      text not null default 'pending',
    created_at  timestamptz not null default now()
);

create table if not exists approvals (
    id          bigserial primary key,
    proposal_id bigint not null references proposals(id) unique,
    approved    boolean not null,
    note        text default '',
    decided_by  text not null default 'human',
    created_at  timestamptz not null default now()
);

create table if not exists applications (
    id            bigserial primary key,
    proposal_id   bigint not null references proposals(id) unique,
    rows_affected int not null default 0,
    applied_at    timestamptz not null default now()
);

-- S4: per-thread ownership tokens. Only the sha256 of each token is stored;
-- the token itself is returned once at thread creation and never persisted.
create table if not exists thread_tokens (
    thread_id  text primary key,
    token_hash text not null,
    created_at timestamptz not null default now()
);
"""

DEMO_SCHEMA = """
create table if not exists customers (
    id          serial primary key,
    name        text not null,
    email       text unique not null,
    plan        text not null check (plan in ('starter', 'growth', 'enterprise')),
    signup_date date not null default current_date
);

create table if not exists orders (
    id           serial primary key,
    customer_id  int not null references customers(id),
    amount_cents int not null,
    status       text not null check (status in ('paid', 'pending', 'refunded')),
    created_at   timestamptz not null default now()
);

create table if not exists incidents (
    id           serial primary key,
    title        text not null,
    severity     text not null check (severity in ('sev1', 'sev2', 'sev3')),
    service      text not null,
    status       text not null check (status in ('open', 'mitigated', 'resolved')),
    opened_at    timestamptz not null default now(),
    resolved_at  timestamptz
);

create index if not exists orders_created_idx on orders (created_at);
create index if not exists incidents_status_idx on incidents (status);
"""

FIRST = ["Avery", "Jordan", "Riley", "Morgan", "Casey", "Quinn", "Dana", "Alex", "Robin", "Sasha"]
LAST = ["Nguyen", "Patel", "Garcia", "Kim", "Okafor", "Silva", "Haddad", "Novak", "Iyer", "Brooks"]
SERVICES = ["checkout-api", "auth-service", "ingest-pipeline", "search-index", "billing-worker"]


def ensure_databases() -> None:
    with psycopg.connect(_require_dsn("OPS_ADMIN_DSN"), autocommit=True) as conn:
        for db in (APP_DB, CHECKPOINT_DB):
            exists = conn.execute("select 1 from pg_database where datname = %s", (db,)).fetchone()
            if not exists:
                conn.execute(f'create database "{db}"')
                print(f"created database {db}")


def apply_schema(dsn: str, sql: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql)


def seed_demo(dsn: str, customers: int = 40, orders: int = 400, incidents: int = 12) -> None:
    rng = random.Random(42)
    now = datetime.now(tz=UTC)

    with psycopg.connect(dsn, autocommit=True) as conn:
        count = conn.execute("select count(*) from customers").fetchone()[0]
        if count:
            print(f"demo data already present ({count} customers); skipping seed")
            return

        plans = ["starter", "growth", "enterprise"]
        ids = []
        for i in range(customers):
            name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
            email = f"{name.lower().replace(' ', '.')}@example{i}.com"
            plan = rng.choices(plans, weights=[50, 35, 15])[0]
            age = rng.randint(30, 700)
            row = conn.execute(
                "insert into customers (name, email, plan, signup_date) "
                "values (%s, %s, %s, current_date - (%s || ' days')::interval) returning id",
                (name, email, plan, age),
            ).fetchone()
            ids.append(row[0])

        statuses = ["paid", "paid", "paid", "pending", "refunded"]
        with conn.cursor() as cur:
            for _ in range(orders):
                cur.execute(
                    "insert into orders (customer_id, amount_cents, status, created_at) values (%s, %s, %s, %s)",
                    (
                        rng.choice(ids),
                        rng.randint(900, 120_000),
                        rng.choices(statuses, weights=[70, 8, 8, 7, 7])[0],
                        now - timedelta(days=rng.uniform(0, 180)),
                    ),
                )

        titles = [
            "Elevated checkout error rate",
            "Auth latency spikes",
            "Ingest backlog growing",
            "Search stale results",
            "Duplicate billing events",
            "Webhook delivery failures",
            "Slow dashboard queries",
            "Certificate renewal warning",
            "Queue consumer crash-loop",
            "CDN cache miss surge",
            "DB connection pool exhaustion",
            "Email bounce rate up",
        ]
        incident_rows = []
        for i, title in enumerate(titles[:incidents]):
            severity = rng.choices(["sev1", "sev2", "sev3"], weights=[10, 40, 50])[0]
            opened = now - timedelta(hours=rng.uniform(2, 96))
            resolved = None if i < 4 else opened + timedelta(hours=rng.uniform(0.5, 8))
            status = (
                "open" if resolved is None else ("mitigated" if rng.random() < 0.4 else "resolved")
            )
            incident_rows.append((title, severity, rng.choice(SERVICES), status, opened, resolved))
        with conn.cursor() as cur:
            for row in incident_rows:
                cur.execute(
                    """
                    insert into incidents (title, severity, service, status, opened_at, resolved_at)
                    values (%s, %s, %s, %s, %s, %s)
                    """,
                    row,
                )

    print(f"seeded {customers} customers, {orders} orders, {incidents} incidents")


async def setup_checkpointer_tables() -> None:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    dsn = _require_dsn("OPS_CHECKPOINT_DB_URL")
    async with AsyncPostgresSaver.from_conn_string(dsn) as saver:
        await saver.setup()
    print("checkpoint tables ready")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-demo-data", action="store_true")
    parser.add_argument("--skip-checkpoints", action="store_true")
    args = parser.parse_args()

    ensure_databases()

    app_dsn = _require_dsn("OPS_DATABASE_URL")
    apply_schema(app_dsn, SCHEMA_SQL)
    print("control-plane schema applied (proposals/approvals/applications)")
    if not args.skip_demo_data:
        apply_schema(app_dsn, DEMO_SCHEMA)
        seed_demo(app_dsn)

    if not args.skip_checkpoints:
        asyncio.run(setup_checkpointer_tables())


if __name__ == "__main__":
    main()
