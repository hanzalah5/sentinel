.PHONY: install lint format typecheck test check run worker seed ingest up down

install:
	uv sync

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy src tests

test:
	uv run pytest

check: lint typecheck test

run:
	uv run uvicorn sentinel.api.app:create_app --factory --reload --port 8000

worker:
	uv run python -m sentinel.ingestion.worker

seed:
	uv run python -m sentinel.ingestion.generator --count 10 --rate 1

ingest:
	uv run python -m sentinel.rag.ingest --path runbooks

up:
	docker compose up --build

down:
	docker compose down -v
