"""Synthetic-incident generator (pure, offline)."""

from __future__ import annotations

import random

from sentinel.contracts import Alert, AlertSeverity
from sentinel.ingestion.generator import REGIONS, SERVICES, TEMPLATES, make_alert


def test_alert_id_is_zero_padded_index() -> None:
    assert make_alert(random.Random(42), 5).alert_id == "gen-0005"


def test_same_seed_yields_identical_content() -> None:
    a = make_alert(random.Random(1), 0)
    b = make_alert(random.Random(1), 0)
    # fired_at uses wall-clock, so compare only the content-bearing fields.
    assert (a.title, a.description, a.service, a.severity, a.labels) == (
        b.title,
        b.description,
        b.service,
        b.severity,
        b.labels,
    )


def test_every_template_builds_a_valid_alert() -> None:
    rng = random.Random(7)
    for template in TEMPLATES:
        title, description, severity = template(rng, "checkout-api")
        assert title and description
        assert isinstance(severity, AlertSeverity)


def test_make_alert_invariants() -> None:
    for i in range(len(TEMPLATES) * 3):
        alert = make_alert(random.Random(i), i)
        assert isinstance(alert, Alert)
        assert alert.service in SERVICES
        assert alert.source == "synthetic-generator"
        assert alert.labels["generated"] == "true"
        assert alert.labels["region"] in REGIONS
