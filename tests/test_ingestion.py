"""Ingestion worker behaviour, exercised against an in-memory fake Redis."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import fakeredis.aioredis
import pytest

from sentinel.contracts import (
    Alert,
    AlertSeverity,
    InvestigationResult,
    RootCauseHypothesis,
)
from sentinel.ingestion.streams import AlertStream
from sentinel.ingestion.worker import IngestionWorker

_HYPOTHESIS = RootCauseHypothesis(summary="s", candidate_causes=[], recommended_next_step="n")


class RecordingService:
    """Records the alert IDs it was asked to investigate."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.received: list[tuple[str, str | None]] = []

    def investigate(self, alert: Alert, correlation_id: str | None = None) -> InvestigationResult:
        self.calls.append(alert.alert_id)
        self.received.append((alert.alert_id, correlation_id))
        return InvestigationResult(
            alert_id=alert.alert_id,
            correlation_id=correlation_id or "x",
            model="fake",
            hypothesis=_HYPOTHESIS,
            created_at=datetime.now(UTC),
        )


class FailingService:
    """Always fails, to exercise retry / dead-letter paths."""

    def investigate(self, alert: Alert, correlation_id: str | None = None) -> InvestigationResult:
        raise RuntimeError("boom")


def _alert(alert_id: str) -> Alert:
    return Alert(
        alert_id=alert_id,
        title="High latency",
        description="p99 latency spiked",
        severity=AlertSeverity.warning,
        service="checkout-api",
        source="test",
        fired_at=datetime.now(UTC),
    )


@pytest.fixture
async def redis_client() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


def _stream(client: fakeredis.aioredis.FakeRedis) -> AlertStream:
    return AlertStream(
        client,
        stream="t:alerts",
        group="g",
        consumer="c",
        dead_letter_stream="t:dlq",
    )


def _worker(stream: AlertStream, service: object, *, max_attempts: int = 3) -> IngestionWorker:
    return IngestionWorker(
        stream,
        service,  # type: ignore[arg-type]  # duck-typed test double
        batch_count=10,
        block_ms=50,
        reclaim_idle_ms=0,
        max_delivery_attempts=max_attempts,
    )


async def test_message_processed_and_acked(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    stream = _stream(redis_client)
    await stream.ensure_group()
    service = RecordingService()
    worker = _worker(stream, service)

    await stream.publish(_alert("a-1"))
    await worker.run_once()

    assert service.calls == ["a-1"]
    assert await stream.pending(10, 0) == []  # nothing left unacked


async def test_backlog_published_before_group_is_still_consumed(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    # Publish BEFORE the group exists; ensure_group(id=0) must still deliver it.
    stream = _stream(redis_client)
    await stream.publish(_alert("a-early"))
    await stream.ensure_group()
    service = RecordingService()

    await _worker(stream, service).run_once()

    assert service.calls == ["a-early"]


async def test_poison_message_goes_to_dead_letter(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    stream = _stream(redis_client)
    await stream.ensure_group()
    service = RecordingService()

    await redis_client.xadd("t:alerts", {"alert": "not-valid-json"})
    await _worker(stream, service).run_once()

    assert service.calls == []
    dlq = await redis_client.xrange("t:dlq")
    assert dlq is not None and len(dlq) == 1
    # The dead-letter entry must preserve the raw payload and failure context.
    fields = cast("dict[str, str]", dlq[0][1])
    assert fields["alert"] == "not-valid-json"
    assert fields["error"].startswith("validation_error")
    assert fields["source_stream"] == "t:alerts"
    assert await stream.pending(10, 0) == []


async def test_failing_investigation_dead_letters_after_max_attempts(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    stream = _stream(redis_client)
    await stream.ensure_group()
    worker = _worker(stream, FailingService(), max_attempts=2)

    await stream.publish(_alert("a-2"))
    await worker.run_once()  # first delivery fails -> left pending

    for _ in range(10):
        if await redis_client.xrange("t:dlq"):
            break
        await worker.reclaim_pending()

    dlq = await redis_client.xrange("t:dlq")
    assert dlq is not None and len(dlq) == 1
    assert await stream.pending(10, 0) == []


async def test_worker_threads_message_id_as_correlation_id(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    stream = _stream(redis_client)
    await stream.ensure_group()
    service = RecordingService()

    message_id = await stream.publish(_alert("corr-1"))
    await _worker(stream, service).run_once()

    # The Redis message id is threaded through as the correlation id.
    assert service.received == [("corr-1", message_id)]


async def test_run_processes_then_stops_on_event(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    stream = _stream(redis_client)
    await stream.ensure_group()
    service = RecordingService()
    worker = _worker(stream, service)
    await stream.publish(_alert("run-1"))

    stop = asyncio.Event()

    async def stopper() -> None:
        for _ in range(100):
            if service.calls:
                break
            await asyncio.sleep(0.01)
        stop.set()

    await asyncio.gather(worker.run(stop), stopper())

    assert service.calls == ["run-1"]


async def test_run_stops_promptly_when_reads_return_immediately(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    """The run loop must yield to the event loop even when reads never suspend.

    fakeredis answers a blocking XREADGROUP on an empty stream without suspending. With
    no yield point the loop starves every other task, including whatever sets the stop
    event, and never exits. A real Redis connection always suspends on network I/O, so
    this mostly affects test doubles, but shutdown must not depend on the client.
    """
    worker = _worker(_stream(redis_client), RecordingService())
    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        stop.set()

    await asyncio.gather(worker.run(stop), stop_soon())

    assert stop.is_set()
