"""Postgres + pgvector runbook store and retriever.

Hand-rolled SQL (no ORM/vector-store wrapper) so the retrieval path is explicit:
cosine distance via pgvector's ``<=>`` with an HNSW index. Embeddings are passed as a
``vector`` literal, so the only extra dependency is psycopg.
"""

from __future__ import annotations

from typing import Any

import structlog
from langchain_core.embeddings import Embeddings

from sentinel.config import Settings
from sentinel.rag.embeddings import get_embeddings
from sentinel.rag.runbooks import NullRetriever, RunbookMatch, RunbookRetriever

logger = structlog.get_logger(__name__)

# Fixed identifier (never user input) so it is safe to interpolate into SQL.
RUNBOOK_TABLE = "sentinel_runbook_chunks"


def _vector_literal(vector: list[float]) -> str:
    """Render an embedding as a pgvector literal: ``[0.1,0.2,...]``."""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


class PgVectorRunbookStore:
    """Owns the schema and read/write access for runbook chunks."""

    def __init__(self, settings: Settings) -> None:
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL is not configured.")
        self._dsn = settings.database_url
        self._dim = settings.embedding_dim
        self._connect_timeout = settings.db_connect_timeout_seconds
        self._statement_timeout_ms = settings.db_statement_timeout_ms

    def _connect(self) -> Any:
        """Open a connection with bounded connect + statement timeouts.

        Without these, an unreachable host would block on the OS TCP timeout (tens of
        seconds) on every alert instead of degrading quickly to "no runbooks".
        """
        import psycopg

        return psycopg.connect(
            self._dsn,
            connect_timeout=self._connect_timeout,
            options=f"-c statement_timeout={self._statement_timeout_ms}",
        )

    def init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {RUNBOOK_TABLE} ("
                "  id bigserial PRIMARY KEY,"
                "  title text NOT NULL,"
                "  source text NOT NULL,"
                "  content text NOT NULL,"
                f"  embedding vector({self._dim}) NOT NULL"
                ")"
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS {RUNBOOK_TABLE}_embedding_idx "
                f"ON {RUNBOOK_TABLE} USING hnsw (embedding vector_cosine_ops)"
            )
            conn.commit()

    def replace_all(self, rows: list[tuple[str, str, str, list[float]]]) -> int:
        """Replace the whole corpus (curated set, so a full rebuild is simplest)."""
        with self._connect() as conn:
            conn.execute(f"TRUNCATE {RUNBOOK_TABLE}")
            with conn.cursor() as cur:
                for title, source, content, embedding in rows:
                    cur.execute(
                        f"INSERT INTO {RUNBOOK_TABLE} (title, source, content, embedding) "
                        "VALUES (%s, %s, %s, %s::vector)",
                        (title, source, content, _vector_literal(embedding)),
                    )
            conn.commit()
        return len(rows)

    def search(self, embedding: list[float], k: int) -> list[RunbookMatch]:
        vector = _vector_literal(embedding)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT title, source, content, 1 - (embedding <=> %s::vector) AS score "
                f"FROM {RUNBOOK_TABLE} "
                f"ORDER BY embedding <=> %s::vector "
                "LIMIT %s",
                (vector, vector, k),
            )
            return [
                RunbookMatch(title=title, source=source, content=content, score=float(score))
                for title, source, content, score in cur.fetchall()
            ]


class PgVectorRunbookRetriever:
    """Embeds the query and returns the nearest runbooks from pgvector."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._store = PgVectorRunbookStore(settings)
        self._embeddings: Embeddings | None = None

    def _embed(self, query: str) -> list[float]:
        if self._embeddings is None:
            self._embeddings = get_embeddings(self._settings)
        return self._embeddings.embed_query(query)

    def retrieve(self, query: str, k: int) -> list[RunbookMatch]:
        return self._store.search(self._embed(query), k)


def build_retriever(settings: Settings) -> RunbookRetriever:
    """Return a pgvector-backed retriever, or a no-op one when RAG is disabled."""
    if not settings.rag_enabled:
        logger.info("rag.disabled", reason="DATABASE_URL not configured")
        return NullRetriever()
    return PgVectorRunbookRetriever(settings)
