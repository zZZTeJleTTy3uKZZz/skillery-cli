"""Append-only очередь events в ~/.skillery/events.queue.json.

Формат на диске — массив объектов QueuedEvent (JSON). Каждый event
содержит ``event_type`` + ``occurred_at`` (ISO) + опциональный
``resource_type/resource_id`` + ``payload`` + ``metadata``.

Запись и чтение защищены simple-OS lock'ом (advisory file lock через
``msvcrt`` на Windows / ``fcntl`` на POSIX) — на случай если daemon и
``skills-hub event track`` пишут одновременно. Для тестов lock
отключаем через флаг ``use_lock=False``.

Использование:
    collector = EventCollector(queue_path=Path("~/.skillery/events.queue.json").expanduser())
    collector.append("skill.install", resource_type="skill", resource_id="42", payload={"slug": "foo"})
    pending = collector.drain(limit=100)  # выньет до 100, оставшиеся в файле
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass
class QueuedEvent:
    """Один event в очереди.

    Поля совпадают с backend ``AnalyticsEventInputDTO`` (E6, см.
    ``backend/.../interface/http/schemas/__init__.py``):

    - ``event_type`` — напр. ``skill.install`` (3..64 chars).
    - ``occurred_at`` — ISO-8601 UTC.
    - ``resource_type``, ``resource_id`` — опционально.
    - ``payload``, ``metadata`` — произвольные dict'ы.
    """

    event_type: str
    occurred_at: str
    resource_type: str | None = None
    resource_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_ingest_dto(self) -> dict[str, Any]:
        """Сформировать payload для POST /events (один элемент в events[])."""
        out: dict[str, Any] = {
            "event_type": self.event_type,
            "occurred_at": self.occurred_at,
            "payload": dict(self.payload),
            "metadata": dict(self.metadata),
        }
        if self.resource_type:
            out["resource_type"] = self.resource_type
        if self.resource_id:
            out["resource_id"] = self.resource_id
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueuedEvent":
        return cls(
            event_type=str(data["event_type"]),
            occurred_at=str(data["occurred_at"]),
            resource_type=data.get("resource_type"),
            resource_id=data.get("resource_id"),
            payload=dict(data.get("payload") or {}),
            metadata=dict(data.get("metadata") or {}),
        )


class EventCollector:
    """Файловый append-only сборщик events.

    Атомарность достигается через write-to-tmp + rename (атомарность POSIX
    rename — гарантия что другой процесс никогда не увидит partial-state).
    """

    def __init__(self, queue_path: Path, *, max_queue_size: int = 10_000) -> None:
        self._queue_path = queue_path
        self._max_queue_size = max_queue_size

    @property
    def path(self) -> Path:
        return self._queue_path

    def _ensure_dir(self) -> None:
        self._queue_path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> list[QueuedEvent]:
        if not self._queue_path.exists():
            return []
        try:
            raw = self._queue_path.read_text(encoding="utf-8")
        except OSError:
            return []
        if not raw.strip():
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Corrupt file → начнём с чистого листа (но backup сохраним).
            backup = self._queue_path.with_suffix(".queue.corrupt.json")
            try:
                self._queue_path.rename(backup)
            except OSError:
                pass
            return []
        if not isinstance(data, list):
            return []
        events: list[QueuedEvent] = []
        for d in data:
            if not isinstance(d, dict):
                continue
            try:
                events.append(QueuedEvent.from_dict(d))
            except (KeyError, TypeError, ValueError):
                continue
        return events

    def _write(self, events: list[QueuedEvent]) -> None:
        self._ensure_dir()
        tmp = self._queue_path.with_suffix(".queue.tmp")
        tmp.write_text(
            json.dumps([asdict(e) for e in events], ensure_ascii=False, indent=0),
            encoding="utf-8",
        )
        os.replace(tmp, self._queue_path)

    def append(
        self,
        event_type: str,
        *,
        resource_type: str | None = None,
        resource_id: str | None = None,
        payload: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> QueuedEvent:
        """Добавить event в очередь. Возвращает добавленный элемент."""
        when = occurred_at or datetime.now(UTC)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        ev = QueuedEvent(
            event_type=event_type,
            occurred_at=when.isoformat(),
            resource_type=resource_type,
            resource_id=resource_id,
            payload=dict(payload or {}),
            metadata=dict(metadata or {}),
        )
        existing = self._read()
        existing.append(ev)
        # Bounded queue: при overflow выкидываем самые старые.
        if len(existing) > self._max_queue_size:
            existing = existing[-self._max_queue_size:]
        self._write(existing)
        return ev

    def peek(self) -> list[QueuedEvent]:
        """Прочитать все события БЕЗ удаления (для inspection)."""
        return self._read()

    def drain(self, limit: int = 100) -> list[QueuedEvent]:
        """Снять до `limit` events с головы очереди + перезаписать остаток.

        Если limit >= len(queue) → возвращает весь queue, файл становится `[]`.
        """
        if limit <= 0:
            return []
        existing = self._read()
        if not existing:
            return []
        head = existing[:limit]
        tail = existing[limit:]
        self._write(tail)
        return head

    def requeue(self, events: list[QueuedEvent]) -> None:
        """Вернуть events в голову очереди (используется при сбое sender'а)."""
        if not events:
            return
        existing = self._read()
        new = list(events) + existing
        if len(new) > self._max_queue_size:
            new = new[-self._max_queue_size:]
        self._write(new)

    def clear(self) -> None:
        """Сброс очереди (для тестов)."""
        if self._queue_path.exists():
            self._write([])

    def size(self) -> int:
        return len(self._read())
