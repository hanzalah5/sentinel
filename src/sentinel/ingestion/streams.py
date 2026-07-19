"""Thin, typed wrapper over a Redis Stream + consumer group for alerts.

Encapsulates the raw Redis commands so the worker reads like domain logic
(publish / read_new / ack / reclaim / dead-letter) rather than XADD/XREADGROUP calls.
"""

from __future__ import annotations

from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from sentinel.contracts import Alert

logger = structlog.get_logger(__name__)

# A stream entry as returned by redis-py with decode_responses=True.
StreamMessage = tuple[str, dict[str, str]]


class AlertStream:
    """A Redis Stream carrying alerts, consumed via a named consumer group."""

    def __init__(
        self,
        redis: Redis,
        *,
        stream: str,
        group: str,
        consumer: str,
        dead_letter_stream: str,
        maxlen: int | None = None,
    ) -> None:
        # redis-py types every reply as ``bytes | str`` because it can't see the
        # runtime ``decode_responses=True``; treat the client as untyped internally
        # and keep the precise types on this class's own methods.
        self._redis: Any = redis
        self._stream = stream
        self._group = group
        self._consumer = consumer
        self._dlq = dead_letter_stream
        # Approximate cap so both streams stay bounded (None = no trimming).
        self._maxlen = maxlen or None

    async def ensure_group(self) -> None:
        """Create the consumer group (and stream) if it does not already exist.

        Uses id="0" so the group also picks up any messages published before it
        existed — no alert is lost to a race between producer and consumer startup.
        """
        try:
            await self._redis.xgroup_create(self._stream, self._group, id="0", mkstream=True)
            logger.info("stream.group_created", stream=self._stream, group=self._group)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def publish(self, alert: Alert) -> str:
        """Append an alert to the stream; returns the new message ID."""
        message_id: str = await self._redis.xadd(
            self._stream,
            {"alert": alert.model_dump_json()},
            maxlen=self._maxlen,
            approximate=True,
        )
        return message_id

    async def read_new(self, count: int, block_ms: int) -> list[StreamMessage]:
        """Read never-before-delivered messages for this consumer (blocking)."""
        resp = await self._redis.xreadgroup(
            self._group,
            self._consumer,
            {self._stream: ">"},
            count=count,
            block=block_ms,
        )
        if not resp:
            return []
        _, messages = resp[0]
        return messages

    async def ack(self, message_id: str) -> None:
        """Acknowledge a message, removing it from the pending list."""
        await self._redis.xack(self._stream, self._group, message_id)

    async def pending(self, count: int, idle_ms: int) -> list[dict[str, Any]]:
        """Return pending (delivered but unacked) entries idle for >= idle_ms."""
        entries: list[dict[str, Any]] = await self._redis.xpending_range(
            self._stream,
            self._group,
            min="-",
            max="+",
            count=count,
            idle=idle_ms,
        )
        return entries

    async def claim(self, message_ids: list[str], idle_ms: int) -> list[StreamMessage]:
        """Reassign pending messages to this consumer (increments delivery count)."""
        claimed: list[StreamMessage] = await self._redis.xclaim(
            self._stream,
            self._group,
            self._consumer,
            min_idle_time=idle_ms,
            message_ids=message_ids,
        )
        return claimed

    async def fetch(self, message_id: str) -> dict[str, str] | None:
        """Return the fields of a single message by ID, if it still exists."""
        result = await self._redis.xrange(self._stream, min=message_id, max=message_id)
        return result[0][1] if result else None

    async def to_dead_letter(self, message_id: str, alert_json: str, error: str) -> None:
        """Move a message to the dead-letter stream with failure context."""
        await self._redis.xadd(
            self._dlq,
            {
                "alert": alert_json,
                "error": error,
                "source_id": message_id,
                "source_stream": self._stream,
            },
            maxlen=self._maxlen,
            approximate=True,
        )
        logger.warning("stream.dead_lettered", message_id=message_id, error=error)
