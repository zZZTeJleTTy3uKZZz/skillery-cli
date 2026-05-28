"""Тесты `EventCollector` (E23): append, drain, requeue, recovery."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from skills_hub_cli.daemon.event_collector import EventCollector, QueuedEvent


def _make_collector(tmp_path: Path, **kwargs) -> EventCollector:  # noqa: ANN003
    return EventCollector(tmp_path / "events.queue.json", **kwargs)


def test_collector_append_then_peek(tmp_path: Path) -> None:
    c = _make_collector(tmp_path)
    ev = c.append(
        "skill.run",
        resource_type="skill",
        resource_id="my-skill",
        payload={"slug": "my-skill"},
    )
    assert isinstance(ev, QueuedEvent)
    assert ev.event_type == "skill.run"
    peek = c.peek()
    assert len(peek) == 1
    assert peek[0].event_type == "skill.run"
    assert peek[0].resource_id == "my-skill"
    assert peek[0].payload == {"slug": "my-skill"}
    # Файл должен быть на диске
    assert c.path.exists()


def test_collector_drain_pops_head(tmp_path: Path) -> None:
    c = _make_collector(tmp_path)
    for i in range(5):
        c.append(f"event.{i}")
    head = c.drain(limit=3)
    assert len(head) == 3
    assert [e.event_type for e in head] == ["event.0", "event.1", "event.2"]
    tail = c.peek()
    assert [e.event_type for e in tail] == ["event.3", "event.4"]


def test_collector_requeue_puts_back_in_head(tmp_path: Path) -> None:
    c = _make_collector(tmp_path)
    c.append("a")
    c.append("b")
    head = c.drain(limit=2)
    assert len(head) == 2
    # После drain — пустая очередь
    assert c.size() == 0
    # Requeue возвращает на место
    c.requeue(head)
    assert c.size() == 2
    assert [e.event_type for e in c.peek()] == ["a", "b"]


def test_collector_corrupt_file_recovers(tmp_path: Path) -> None:
    """Если файл повреждён — drain не падает, начинает с чистой очереди."""
    path = tmp_path / "events.queue.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    c = EventCollector(path)
    # peek НЕ должен бросать
    assert c.peek() == []
    # Файл должен быть отправлен в backup
    assert path.with_suffix(".queue.corrupt.json").exists() or not path.exists()
    # После recovery можем добавлять
    c.append("recovered")
    assert c.size() == 1


def test_collector_overflow_drops_oldest(tmp_path: Path) -> None:
    c = _make_collector(tmp_path, max_queue_size=3)
    for i in range(5):
        c.append(f"e.{i}")
    events = c.peek()
    assert len(events) == 3
    # Самые старые (e.0, e.1) должны быть выброшены
    assert [e.event_type for e in events] == ["e.2", "e.3", "e.4"]


def test_collector_drain_empty(tmp_path: Path) -> None:
    c = _make_collector(tmp_path)
    assert c.drain(limit=10) == []
    assert c.size() == 0


def test_to_ingest_dto_shape(tmp_path: Path) -> None:
    """Проверяем что to_ingest_dto даёт payload пригодный для POST /events."""
    c = _make_collector(tmp_path)
    now = datetime(2026, 5, 26, 12, 0, 0, tzinfo=UTC)
    c.append(
        "skill.install",
        resource_type="skill",
        resource_id="slk_x",
        payload={"slug": "my-skill", "version": "1.0.0"},
        metadata={"source": "cli"},
        occurred_at=now,
    )
    dto = c.peek()[0].to_ingest_dto()
    assert dto["event_type"] == "skill.install"
    assert dto["resource_type"] == "skill"
    assert dto["resource_id"] == "slk_x"
    assert dto["payload"] == {"slug": "my-skill", "version": "1.0.0"}
    assert dto["metadata"] == {"source": "cli"}
    assert dto["occurred_at"].startswith("2026-05-26")


def test_collector_persistence_across_instances(tmp_path: Path) -> None:
    """Новый instance видит ранее записанные events."""
    c1 = _make_collector(tmp_path)
    c1.append("e.first")
    c1.append("e.second")
    # Новый instance того же файла
    c2 = EventCollector(c1.path)
    events = c2.peek()
    assert [e.event_type for e in events] == ["e.first", "e.second"]


def test_collector_clear_resets(tmp_path: Path) -> None:
    c = _make_collector(tmp_path)
    c.append("e.x")
    assert c.size() == 1
    c.clear()
    assert c.size() == 0
    # Файл всё ещё существует (пустой массив)
    assert c.path.exists()
    raw = c.path.read_text(encoding="utf-8")
    assert json.loads(raw) == []
