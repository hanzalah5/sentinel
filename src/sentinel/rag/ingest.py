"""Ingest a directory of markdown runbooks into Postgres + pgvector.

Usage:
    python -m sentinel.rag.ingest --path runbooks
"""

from __future__ import annotations

import argparse
from pathlib import Path

import structlog

from sentinel.config import get_settings
from sentinel.logging import configure_logging
from sentinel.rag.embeddings import get_embeddings
from sentinel.rag.store import PgVectorRunbookStore

logger = structlog.get_logger(__name__)


def _title_of(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _load_runbooks(path: Path) -> list[tuple[str, str, str]]:
    """Return (title, source, content) for each markdown runbook in ``path``."""
    docs: list[tuple[str, str, str]] = []
    for md in sorted(path.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        docs.append((_title_of(text, md.stem), md.name, text))
    return docs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest markdown runbooks into Postgres + pgvector."
    )
    parser.add_argument("--path", default="runbooks", help="directory of .md runbooks")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level, settings.json_logs)

    if not settings.rag_enabled:
        raise SystemExit("DATABASE_URL not configured; cannot ingest runbooks.")
    if not settings.llm_configured:
        raise SystemExit("GOOGLE_API_KEY not configured; cannot embed runbooks.")

    docs = _load_runbooks(Path(args.path))
    if not docs:
        raise SystemExit(f"No .md runbooks found under {args.path!r}.")

    embeddings = get_embeddings(settings)
    vectors = embeddings.embed_documents([content for _, _, content in docs])
    rows = [
        (title, source, content, vector)
        for (title, source, content), vector in zip(docs, vectors, strict=True)
    ]

    store = PgVectorRunbookStore(settings)
    store.init_schema()
    count = store.replace_all(rows)
    logger.info("rag.ingested", runbooks=count, path=str(args.path))


if __name__ == "__main__":
    main()
