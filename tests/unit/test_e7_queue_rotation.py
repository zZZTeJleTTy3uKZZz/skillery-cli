"""E7 офлайн-batch — лимит размера очереди + ротация старых при переполнении.

При длительном офлайне (daemon встал / backend недоступен) очередь не должна
расти безгранично и съедать диск. ``EventCollector(max_queue_size=N)`` держит
не более N событий, выкидывая САМЫЕ СТАРЫЕ (голову) — и на ``append``, и на
``requeue`` (возврат после неудачного flush). Сохраняем самые свежие события.
"""
from __future__ import annotations

from pathlib import Path

from skills_hub_cli.daemon.event_collector import EventCollector, QueuedEvent


def test_append_overflow_drops_oldest_keeps_newest(tmp_path: Path) -> None:
    c = EventCollector(tmp_path / "q.json", max_queue_size=3)
    for i in range(6):
        c.append(f"e.{i}")
    kept = [e.event_type for e in c.peek()]
    # ровно 3, и это самые свежие
    assert kept == ["e.3", "e.4", "e.5"]


def test_requeue_respects_max_size(tmp_path: Path) -> None:
    """Возврат большого batch'а после провала flush не переполняет очередь."""
    c = EventCollector(tmp_path / "q.json", max_queue_size=4)
    # в очереди уже 2 свежих
    c.append("fresh.0")
    c.append("fresh.1")
    # requeue 5 «старых» событий (как при неудачном flush большого batch'а)
    old = [
        QueuedEvent(event_type=f"old.{i}", occurred_at="2026-01-01T00:00:00+00:00")
        for i in range(5)
    ]
    c.requeue(old)
    kept = [e.event_type for e in c.peek()]
    # cap=4 → хвост из 4 самых свежих (requeue кладёт old в голову, fresh в хвост)
    assert len(kept) == 4
    # самые свежие (fresh.*) сохраняются в хвосте
    assert kept[-2:] == ["fresh.0", "fresh.1"]


def test_queue_never_exceeds_cap_under_sustained_load(tmp_path: Path) -> None:
    c = EventCollector(tmp_path / "q.json", max_queue_size=10)
    n = 200
    for i in range(n):
        c.append(f"flood.{i}")
    assert c.size() == 10
    # последнее событие гарантированно в очереди
    assert c.peek()[-1].event_type == f"flood.{n - 1}"
