# Task shortcuts. On Windows without make, run the uv commands directly (see README).
.PHONY: install test test-postgres lint format typecheck check migrate web worker seed requirements

install:
	uv sync

test:
	uv run pytest

# Requires TEST_DATABASE_URL pointing at a disposable Postgres database.
test-postgres:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff format .
	uv run ruff check . --fix

typecheck:
	uv run mypy project_buffer

check: lint typecheck test

migrate:
	uv run alembic upgrade head

web:
	uv run uvicorn project_buffer.web.app:create_app --factory --reload --port 8000

worker:
	uv run python -m project_buffer.worker

seed:
	uv run python -m project_buffer.cli seed-demo

# Regenerate after changing dependencies. Render installs from this file.
requirements:
	uv export --no-dev --no-hashes --no-emit-project --format requirements-txt -o requirements.txt
