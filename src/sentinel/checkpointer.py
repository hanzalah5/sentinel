"""LangGraph checkpointer factory.

The checkpointer is what makes human-in-the-loop *durable*: when the graph pauses at an
approval interrupt, its state is persisted, so a decision can resume it later — even
after a process restart. Postgres gives true cross-restart durability; the in-memory
saver keeps the interrupt/resume mechanics working for local dev and tests.
"""

from __future__ import annotations

import contextlib
from typing import Any

import structlog
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from sentinel.config import Settings

logger = structlog.get_logger(__name__)


def build_checkpointer(settings: Settings) -> BaseCheckpointSaver[Any]:
    """Return a Postgres-backed saver when a DB is configured, else an in-memory one."""
    if not settings.database_url:
        logger.info("checkpointer.memory", reason="DATABASE_URL not configured")
        return MemorySaver()
    # psycopg's pool generics don't reflect the runtime row_factory, so keep this
    # boundary untyped and let PostgresSaver see the dict-row pool it expects.
    pool: Any = None
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        # A pool, not a single connection: an incident can sit paused for hours, and a
        # serverless/failover Postgres will drop an idle connection in the meantime.
        # ``check_connection`` validates (and replaces) a connection on checkout, so a
        # decision arriving the next morning still resumes instead of erroring forever.
        # PostgresSaver requires autocommit + dict rows; it creates its own tables.
        pool = ConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=settings.db_pool_max_size,
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "connect_timeout": settings.db_connect_timeout_seconds,
            },
            check=ConnectionPool.check_connection,
            open=True,
        )
        saver = PostgresSaver(pool)
        saver.setup()
        logger.info("checkpointer.postgres", status="ready")
        return saver
    except Exception as exc:  # pragma: no cover - falls back so startup never fails
        logger.warning("checkpointer.postgres_failed", error=str(exc))
        if pool is not None:
            # Don't leak the pool's connections when setup fails.
            with contextlib.suppress(Exception):
                pool.close()
        return MemorySaver()
