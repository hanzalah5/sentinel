"""LangGraph checkpointer factory.

The checkpointer is what makes human-in-the-loop *durable*: when the graph pauses at an
approval interrupt, its state is persisted, so a decision can resume it later — even
after a process restart. Postgres gives true cross-restart durability; the in-memory
saver keeps the interrupt/resume mechanics working for local dev and tests.
"""

from __future__ import annotations

import contextlib
import importlib
from enum import Enum
from typing import Any

import structlog
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

from sentinel.config import Settings

logger = structlog.get_logger(__name__)

# Modules whose types make up checkpointed incident state.
_STATE_TYPE_MODULES = ("sentinel.contracts", "sentinel.rag.runbooks")


def checkpoint_allowlist() -> tuple[tuple[str, str], ...]:
    """Every Pydantic model and Enum defined in the modules that make up incident state.

    Derived rather than hand-listed, so a new state type can't be forgotten; a test
    walks ``IncidentState`` to prove every type it can hold is covered.
    """
    allowed: set[tuple[str, str]] = set()
    for module_name in _STATE_TYPE_MODULES:
        module = importlib.import_module(module_name)
        for name, obj in vars(module).items():
            if (
                isinstance(obj, type)
                and obj.__module__ == module_name
                and issubclass(obj, (BaseModel, Enum))
            ):
                allowed.add((module_name, name))
    return tuple(sorted(allowed))


def build_serializer() -> JsonPlusSerializer:
    """A strict checkpoint serializer.

    LangGraph's default revives *any* class named in a checkpoint, with only a warning,
    so whoever can write to the checkpoint store chooses what gets instantiated. This
    serializer revives LangGraph's built-in safe types plus Sentinel's own state types;
    anything else comes back as plain data. It doesn't depend on the process-wide
    ``LANGGRAPH_STRICT_MSGPACK`` switch.
    """
    return JsonPlusSerializer(allowed_msgpack_modules=checkpoint_allowlist())


def build_checkpointer(settings: Settings) -> BaseCheckpointSaver[Any]:
    """Return a Postgres-backed saver when a DB is configured, else an in-memory one."""
    if not settings.database_url:
        logger.info("checkpointer.memory", reason="DATABASE_URL not configured")
        return MemorySaver(serde=build_serializer())
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
        saver = PostgresSaver(pool, serde=build_serializer())
        saver.setup()
        logger.info("checkpointer.postgres", status="ready")
        return saver
    except Exception as exc:  # pragma: no cover - falls back so startup never fails
        logger.warning("checkpointer.postgres_failed", error=str(exc))
        if pool is not None:
            # Don't leak the pool's connections when setup fails.
            with contextlib.suppress(Exception):
                pool.close()
        return MemorySaver(serde=build_serializer())
