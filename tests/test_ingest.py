"""Runbook ingest helpers (pure, offline)."""

from __future__ import annotations

from pathlib import Path

from sentinel.rag.ingest import _load_runbooks, _title_of


def test_title_of_uses_first_h1() -> None:
    assert _title_of("intro\n# Real Title\nbody", "fallback") == "Real Title"


def test_title_of_ignores_h2_and_falls_back() -> None:
    assert _title_of("## sub only\nbody", "fallback") == "fallback"


def test_load_runbooks_filters_to_markdown_and_sorts(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("no heading here", encoding="utf-8")
    (tmp_path / "b.md").write_text("# Beta Title\nbody", encoding="utf-8")
    (tmp_path / "ignore.txt").write_text("not markdown", encoding="utf-8")

    docs = _load_runbooks(tmp_path)

    assert [(title, source) for title, source, _ in docs] == [
        ("a", "a.md"),  # no H1 -> filename stem fallback
        ("Beta Title", "b.md"),
    ]


def test_load_runbooks_empty_dir(tmp_path: Path) -> None:
    assert _load_runbooks(tmp_path) == []
