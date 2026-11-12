.PHONY: install lint format typecheck test check run worker seed ingest eval eval-build up down

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

eval:  # live root-cause eval on the dev split (needs GOOGLE_API_KEY; DATABASE_URL for RAG)
	uv run python -m sentinel.evals run --split dev

eval-build:  # rebuild evals/datasets/generated.jsonl from the alert generator
	uv run python -m sentinel.evals build

up:
	docker compose up --build

down:
	docker compose down -v
