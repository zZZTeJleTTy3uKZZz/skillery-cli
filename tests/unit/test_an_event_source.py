"""Аналитика-эпик W1a: `track_skill_event` прокидывает `source` в payload.

Источник установки (`hub` / `local-path` / `git-url`) — отдельная ось
аналитики (см. план 2026-06-11 «source (ИСТОЧНИК установки)»). Кладётся в
`payload["source"]`, рядом со `scope`. НЕ путать с `metadata.source="cli"`
(транспортный канал — какой клиент прислал событие).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli.core import analytics_sync
from skillery_cli.daemon import instrumentation as instr


class _Queue:
    """Аналитика читается из ОБЩЕГО outbox'а (#1180: третьей очереди нет)."""

    @staticmethod
    def peek() -> list[dict]:
        return analytics_sync.pending()


def _capture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Queue:
    return _Queue()


def test_track_skill_event_puts_source_in_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    coll = _capture(monkeypatch, tmp_path)
    instr.track_skill_event(
        "skill.install", slug="bitrix24", scope="project", source="hub"
    )
    ev = coll.peek()[0]
    assert ev["event_type"] == "skill.install"
    assert ev["payload"]["scope"] == "project"
    assert ev["payload"]["source"] == "hub"
    # metadata.source — транспорт (cli), НЕ источник установки.
    assert ev["metadata"] == {"source": "cli"}


def test_track_skill_event_source_none_omitted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """source=None (или не передан) → ключа в payload нет (не шумим)."""
    coll = _capture(monkeypatch, tmp_path)
    instr.track_skill_event("skill.uninstall", slug="x", scope="global")
    ev = coll.peek()[0]
    assert "source" not in ev["payload"]


@pytest.mark.parametrize("source", ["hub", "local-path", "git-url"])
def test_track_skill_event_all_source_variants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source: str
) -> None:
    coll = _capture(monkeypatch, tmp_path)
    instr.track_skill_event(
        "skill.enable", slug="s", scope="project", source=source
    )
    assert coll.peek()[0]["payload"]["source"] == source
