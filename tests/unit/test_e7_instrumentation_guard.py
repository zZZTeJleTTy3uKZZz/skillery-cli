"""E7 анти-спам — `track_skill_event` пропускает дубли через `EventGuard`.

Silent-телеметрия (``track_skill_event``) — главный источник спама: быстрый
повтор install/enable/sync кладёт одинаковые события. Перед append'ом
инструментация сверяется с персистентным ``EventGuard`` (sidecar рядом с
очередью). Дубль в окне дедупа → событие НЕ попадает в очередь.

Guard НИКОГДА не роняет команду: при ошибке guard'а событие всё равно
обрабатывается (телеметрия — best-effort).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skills_hub_cli.daemon import instrumentation as instr
from skills_hub_cli.daemon.event_collector import EventCollector


def _wire(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> EventCollector:
    queue = tmp_path / "events.queue.json"
    guard = tmp_path / "events.guard.json"
    monkeypatch.setattr(instr, "default_queue_path", lambda: queue)
    monkeypatch.setattr(instr, "default_guard_path", lambda: guard)
    return EventCollector(queue)


def test_duplicate_enable_within_window_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Два идентичных skill.enable подряд → в очереди ровно один."""
    coll = _wire(monkeypatch, tmp_path)
    instr.track_skill_event(
        "skill.enable", slug="demo", resource_id="42",
        scope="project", source="hub", agent="claude_code",
    )
    instr.track_skill_event(
        "skill.enable", slug="demo", resource_id="42",
        scope="project", source="hub", agent="claude_code",
    )
    assert coll.size() == 1


def test_distinct_events_not_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Разные типы/ресурсы — оба проходят (не дубль)."""
    coll = _wire(monkeypatch, tmp_path)
    instr.track_skill_event(
        "skill.enable", slug="a", resource_id="1", scope="project"
    )
    instr.track_skill_event(
        "skill.disable", slug="a", resource_id="1", scope="project"
    )
    instr.track_skill_event(
        "skill.enable", slug="b", resource_id="2", scope="project"
    )
    assert coll.size() == 3


def test_guard_failure_does_not_block_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Если guard кидает — событие всё равно кладётся (telemetry best-effort)."""
    coll = _wire(monkeypatch, tmp_path)

    class _BoomGuard:
        def __init__(self, *a, **k):
            pass

        def should_accept(self, *a, **k):
            raise RuntimeError("guard boom")

    monkeypatch.setattr(instr, "EventGuard", _BoomGuard)
    instr.track_skill_event(
        "skill.install", slug="demo", resource_id="42", scope="global"
    )
    # guard упал → fail-open: событие в очереди
    assert coll.size() == 1


def test_update_with_different_version_not_deduped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """skill.update 1.0.0 затем 2.0.0 — разный payload.version → оба проходят."""
    coll = _wire(monkeypatch, tmp_path)
    instr.track_skill_event(
        "skill.update", slug="demo", resource_id="42", version="1.0.0"
    )
    instr.track_skill_event(
        "skill.update", slug="demo", resource_id="42", version="2.0.0"
    )
    assert coll.size() == 2
