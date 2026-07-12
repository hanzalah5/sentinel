.PHONY: install lint format typecheck test check run up down

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

up:
	docker compose up --build

down:
	docker compose down -v
