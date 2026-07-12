"""The ingestion worker: consume alerts and run investigations, reliably.

Reliability model (at-least-once):
- A message is ACKed only after its investigation succeeds.
- A failed investigation is left pending and retried by ``reclaim_pending`` once it
  has been idle long enough.
- A message that has been delivered more than ``max_delivery_attempts`` times, or that
  cannot be parsed at all (a poison message), is moved to the dead-letter stream and
  ACKed so it can never block the pipeline.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Protocol

import structlog
from pydantic import ValidationError

from sentinel.config import get_settings
from sentinel.contracts import Alert, InvestigationResult
from sentinel.ingestion.streams import AlertStream

logger = structlog.get_logger(__name__)


class Investigator(Protocol):
    """Anything that can turn an alert into a result (the service, or a test double)."""

    def investigate(
        self, alert: Alert, correlation_id: str | None = None
    ) -> InvestigationResult: ...


class IngestionWorker:
    def __init__(
        self,
        stream: AlertStream,
        service: Investigator,
        *,
        batch_count: int,
        block_ms: int,
        reclaim_idle_ms: int,
        max_delivery_attempts: int,
    ) -> None:
        self._stream = stream
        self._service = service
        self._batch_count = batch_count
        self._block_ms = block_ms
        self._reclaim_idle_ms = reclaim_idle_ms
        self._max_delivery_attempts = max_delivery_attempts

    async def process_message(self, message_id: str, fields: dict[str, str]) -> bool:
        """Process one message. Returns True on success (message ACKed)."""
        raw = fields.get("alert", "")
        try:
            alert = Alert.model_validate_json(raw)
        except ValidationError as exc:
            # Poison message: it can never succeed, so don't let it retry forever.
            await self._stream.to_dead_letter(message_id, raw, f"validation_error: {exc}")
            await self._stream.ack(message_id)
            return False

        try:
            # The graph is synchronous; run it off the event loop so one slow
            # investigation doesn't stall the consumer.
            result = await asyncio.to_thread(self._service.investigate, alert, message_id)
        except Exception as exc:  # any failure here just means "retry later"
            logger.error(
                "ingestion.failed",
                message_id=message_id,
                alert_id=alert.alert_id,
                error=str(exc),
            )
            return False  # left unacked -> reclaimed and retried

        await self._stream.ack(message_id)
        logger.info(
            "ingestion.processed",
            message_id=message_id,
            alert_id=alert.alert_id,
            candidate_causes=len(result.hypothesis.candidate_causes),
        )
        return True

    async def reclaim_pending(self) -> None:
        """Retry or dead-letter messages that were delivered but never ACKed."""
        for entry in await self._stream.pending(self._batch_count, self._reclaim_idle_ms):
            message_id = str(entry["message_id"])
            delivered = int(entry["times_delivered"])
            if delivered > self._max_delivery_attempts:
                fields = await self._stream.fetch(message_id) or {}
                await self._stream.to_dead_letter(
                    message_id,
                    fields.get("alert", ""),
                    f"max_delivery_attempts_exceeded ({delivered})",
                )
                await self._stream.ack(message_id)
                continue
            for claimed_id, claimed_fields in await self._stream.claim(
                [message_id], self._reclaim_idle_ms
            ):
                await self.process_message(claimed_id, claimed_fields)

    async def run_once(self) -> None:
        """One pass: retry stragglers, then process a batch of new messages."""
        await self.reclaim_pending()
        for message_id, fields in await self._stream.read_new(self._batch_count, self._block_ms):
            await self.process_message(message_id, fields)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run until ``stop_event`` is set (graceful shutdown)."""
        await self._stream.ensure_group()
        logger.info("ingestion.worker_started")
        while not stop_event.is_set():
            await self.run_once()
        logger.info("ingestion.worker_stopped")


async def _amain() -> None:
    from redis.asyncio import Redis

    from sentinel.api.app import build_service
    from sentinel.logging import configure_logging
    from sentinel.observability import configure_langfuse

    settings = get_settings()
    configure_logging(settings.log_level, settings.json_logs)
    configure_langfuse(settings)

    if not settings.llm_configured:
        raise SystemExit("GOOGLE_API_KEY not configured; the worker cannot investigate.")

    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    stream = AlertStream(
        redis,
        stream=settings.alert_stream,
        group=settings.consumer_group,
        consumer=settings.consumer_name,
        dead_letter_stream=settings.dead_letter_stream,
    )
    worker = IngestionWorker(
        stream,
        build_service(settings),
        batch_count=settings.stream_batch_count,
        block_ms=settings.stream_block_ms,
        reclaim_idle_ms=settings.reclaim_idle_ms,
        max_delivery_attempts=settings.max_delivery_attempts,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows event loops don't implement add_signal_handler.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    try:
        await worker.run(stop_event)
    finally:
        await redis.aclose()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_amain())


if __name__ == "__main__":
    main()
