.PHONY: install lint fmt typecheck test eval up down seed logs clean docker-build

install:
	uv sync --extra dev

lint:
	uv run ruff check src tests evals scripts

fmt:
	uv run ruff check --fix src tests evals scripts && uv run ruff format src tests evals scripts

typecheck:
	uv run mypy

test:
	uv run pytest tests/

eval:
	uv run python evals/runner.py --fail-under-gap

seed:
	uv run python -m scripts.bootstrap_db

up:
	docker compose up --build -d
	@echo "console → http://localhost:3000   api → http://localhost:8000/api/healthz"

down:
	docker compose down

logs:
	docker compose logs -f api

docker-build:
	docker build -t ops-copilot:local .
	docker compose build web

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache evals/runs data
