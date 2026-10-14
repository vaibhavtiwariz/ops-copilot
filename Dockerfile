FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

COPY pyproject.toml README.md ./
COPY src ./src
# No uv.lock in the tree (it lives on the dev machine and is not pulled into
# this copy), so resolve here instead of --frozen. If a lockfile is added,
# copy it above and switch back to `uv sync --frozen`.
RUN uv sync --no-dev --no-install-project && \
    uv sync --no-dev


FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*
# curl exists only so compose healthchecks can probe the MCP servers; the
# runtime itself is pure python.
RUN useradd -m -u 10001 copilot

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY scripts ./scripts
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

# Audit sink lives here when mounted as a volume; pre-create with the right
# owner so the named volume inherits it.
RUN mkdir -p /data && chown copilot:copilot /data

USER copilot
EXPOSE 8000 8001 8002

CMD ["uvicorn", "ops_copilot.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
