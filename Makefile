# Everyday commands. `make help` lists them.
.DEFAULT_GOAL := help

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install:  ## Install dependencies
	uv sync

up:  ## Start PostGIS + Redis in Docker (see docker-compose.yml)
	docker compose up -d

run:  ## Run the API with auto-reload on http://localhost:8000/docs
	uv run uvicorn app.main:app --reload

test:  ## Run the test suite
	uv run pytest

lint:  ## Check code style
	uv run ruff check app tests && uv run ruff format --check app tests

format:  ## Auto-format code
	uv run ruff format app tests && uv run ruff check --fix app tests

migrate:  ## Apply database migrations
	uv run alembic upgrade head

seed:  ## Load demo data (matches the admin dashboard)
	uv run python -m app.seed

demo:  ## Run the scripted end-to-end demo against a running server (make run, then make demo)
	uv run python -m scripts.demo

worker:  ## Run background jobs (escrow release, payouts, reminders)
	uv run celery -A app.worker.celery_app worker --beat --loglevel=info

migration:  ## Create a migration after changing models: make migration m="add x"
	uv run alembic revision --autogenerate -m "$(m)"

.PHONY: help install up run test lint format migrate seed demo worker migration

# Start the local database if it is not running (used inside cloud sandboxes, where it stops when idle).
.PHONY: db-start
db-start:
	@pg_ctlcluster 16 main start 2>/dev/null || true
