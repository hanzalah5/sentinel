"""Replayable synthetic-incident generator.

Publishes realistic alerts onto the Redis stream so the worker can be exercised without
a live monitoring system. Deterministic when given ``--seed``.

Usage:
    python -m sentinel.ingestion.generator --count 20 --rate 2 --seed 42
"""

from __future__ import annotations

import argparse
import asyncio
import random
from collections.abc import Callable
from datetime import UTC, datetime

import structlog

from sentinel.config import get_settings
from sentinel.contracts import Alert, AlertSeverity
from sentinel.ingestion.streams import AlertStream
from sentinel.logging import configure_logging

logger = structlog.get_logger(__name__)

SERVICES = [
    "checkout-api",
    "payments-api",
    "user-service",
    "search-api",
    "cart-service",
    "inventory-service",
]
REGIONS = ["us-east-1", "us-west-2", "eu-west-1"]

# Each template returns (title, description, severity) for a given service.
Template = Callable[[random.Random, str], tuple[str, str, AlertSeverity]]


def _high_latency(rng: random.Random, service: str) -> tuple[str, str, AlertSeverity]:
    before = rng.randint(80, 250)
    after = round(rng.uniform(1.5, 4.0), 1)
    window = rng.randint(3, 10)
    return (
        f"High p99 latency on {service}",
        f"p99 latency rose from {before}ms to {after}s over {window} minutes. "
        f"Error rate steady. DB connection pool saturation warnings in {service} logs.",
        AlertSeverity.critical,
    )


def _oom_kill(rng: random.Random, service: str) -> tuple[str, str, AlertSeverity]:
    limit = rng.choice([512, 1024, 2048])
    restarts = rng.randint(2, 9)
    return (
        f"OOMKilled containers on {service}",
        f"{restarts} containers OOMKilled in the last 10 minutes; memory limit "
        f"{limit}Mi. RSS climbing steadily before each kill — possible memory leak.",
        AlertSeverity.critical,
    )


def _error_rate(rng: random.Random, service: str) -> tuple[str, str, AlertSeverity]:
    rate = round(rng.uniform(4.0, 30.0), 1)
    return (
        f"Elevated 5xx error rate on {service}",
        f"HTTP 5xx rate rose to {rate}% (baseline < 0.5%). Spike began shortly after "
        f"the latest deploy. Upstream dependencies report healthy.",
        AlertSeverity.critical,
    )


def _disk_full(rng: random.Random, service: str) -> tuple[str, str, AlertSeverity]:
    usage = rng.randint(88, 99)
    return (
        f"Disk usage high on {service} node",
        f"Root volume at {usage}% on the node hosting {service}. Log growth accelerating; "
        f"projected to reach 100% within the hour.",
        AlertSeverity.warning,
    )


def _crashloop(rng: random.Random, service: str) -> tuple[str, str, AlertSeverity]:
    restarts = rng.randint(5, 40)
    return (
        f"CrashLoopBackOff on {service}",
        f"Pod restarting repeatedly ({restarts} restarts). Readiness probe failing on "
        f"startup; exit code 1 with no clear stack trace in logs.",
        AlertSeverity.warning,
    )


def _cert_expiry(rng: random.Random, service: str) -> tuple[str, str, AlertSeverity]:
    days = rng.randint(1, 7)
    return (
        f"TLS certificate expiring for {service}",
        f"The TLS certificate for {service} expires in {days} day(s). Automated renewal "
        f"has not completed.",
        AlertSeverity.warning,
    )


TEMPLATES: list[Template] = [
    _high_latency,
    _oom_kill,
    _error_rate,
    _disk_full,
    _crashloop,
    _cert_expiry,
]


def make_alert(rng: random.Random, index: int) -> Alert:
    service = rng.choice(SERVICES)
    title, description, severity = rng.choice(TEMPLATES)(rng, service)
    return Alert(
        alert_id=f"gen-{index:04d}",
        title=title,
        description=description,
        severity=severity,
        service=service,
        source="synthetic-generator",
        fired_at=datetime.now(UTC),
        labels={"region": rng.choice(REGIONS), "generated": "true"},
    )


async def _amain(count: int, rate: float, seed: int | None) -> None:
    from redis.asyncio import Redis

    settings = get_settings()
    configure_logging(settings.log_level, settings.json_logs)
    rng = random.Random(seed)

    redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
    stream = AlertStream(
        redis,
        stream=settings.alert_stream,
        group=settings.consumer_group,
        consumer=settings.consumer_name,
        dead_letter_stream=settings.dead_letter_stream,
    )
    interval = 1.0 / rate if rate > 0 else 0.0
    try:
        for i in range(count):
            alert = make_alert(rng, i)
            message_id = await stream.publish(alert)
            logger.info(
                "generator.published",
                message_id=message_id,
                alert_id=alert.alert_id,
                severity=alert.severity.value,
                service=alert.service,
            )
            if interval and i < count - 1:
                await asyncio.sleep(interval)
    finally:
        await redis.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish synthetic incident alerts to the Redis stream."
    )
    parser.add_argument("--count", type=int, default=10, help="number of alerts to emit")
    parser.add_argument("--rate", type=float, default=1.0, help="alerts per second")
    parser.add_argument("--seed", type=int, default=None, help="seed for reproducibility")
    args = parser.parse_args()
    asyncio.run(_amain(args.count, args.rate, args.seed))


if __name__ == "__main__":
    main()
