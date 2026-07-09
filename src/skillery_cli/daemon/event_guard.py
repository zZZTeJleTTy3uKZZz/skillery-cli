"""Анти-спам guard перед записью event в очередь.

Каждый ``skillery`` вызов — отдельный процесс. Чтобы дедуп и throttle
переживали между процессами, состояние (недавно виденные события + их
timestamps) персистится в sidecar-JSON рядом с очередью
(``~/.skillery/events.guard.json``).

Две защиты, обе принимают решение ДО ``EventCollector.append``:

ДЕДУП (идемпотентность)
    Идентичный *fingerprint* события (``event_type`` + ``resource_id`` +
    стабильный хэш ключевых полей payload) в окне ``dedup_window`` секунд →
    отклоняем. Гасит дубли при ретраях/двойных кликах/повторном sync.

THROTTLE (rate-limit)
    Более ``max_per_window`` событий одного ``(event_type, resource_id)`` в окне
    ``throttle_window`` секунд → отклоняем. Гасит флуд по одному ресурсу даже
    когда события формально различаются (разный payload).

Решение монотонно по времени: при каждом обращении устаревшие записи (старше
самого широкого из окон) вычищаются — файл не растёт бесконечно.

Guard НИКОГДА не роняет основную команду: при битом/недоступном sidecar
считает окно пустым (fail-open — лучше лишний event, чем падение CLI).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


class EventGuard:
    """Персистентный дедуп + throttle решатель.

    Запись на диске: ``{"records": [[ts, type, resource_id, fp], ...]}``.
    ``ts`` — epoch-секунды (float). ``fp`` — sha1-хэш ключевых полей payload
    (для дедупа); throttle игнорирует ``fp`` и считает по ``(type, resource_id)``.
    """

    def __init__(
        self,
        path: Path,
        *,
        dedup_window: float = 5.0,
        throttle_window: float = 60.0,
        max_per_window: int = 20,
    ) -> None:
        self._path = path
        self._dedup_window = max(0.0, float(dedup_window))
        self._throttle_window = max(0.0, float(throttle_window))
        self._max_per_window = max(1, int(max_per_window))

    @property
    def path(self) -> Path:
        return self._path

    # ── persistence (fail-open) ──
    def _read(self) -> list[list[Any]]:
        if not self._path.exists():
            return []
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(data, dict):
            return []
        recs = data.get("records")
        if not isinstance(recs, list):
            return []
        out: list[list[Any]] = []
        for r in recs:
            # форма [ts, type, resource_id, fp]
            if isinstance(r, list) and len(r) == 4:
                out.append(list(r))
        return out

    def _write(self, records: list[list[Any]]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".guard.tmp")
            tmp.write_text(
                json.dumps({"records": records}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            # fail-open: не смогли записать окно — не роняем команду.
            pass

    # ── fingerprint ──
    @staticmethod
    def _fingerprint(
        event_type: str, resource_id: str | None, payload: dict[str, Any] | None
    ) -> str:
        """Стабильный хэш «содержимого» события для дедупа.

        Берём event_type + resource_id + канонизированный payload (sorted keys).
        Разный version/scope/source/agent → разный fp → НЕ дубль.
        """
        canon = json.dumps(
            {
                "t": event_type,
                "r": resource_id or "",
                "p": payload or {},
            },
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        return hashlib.sha1(canon.encode("utf-8")).hexdigest()[:16]

    # ── решение ──
    def should_accept(
        self,
        event_type: str,
        resource_id: str | None = None,
        *,
        payload: dict[str, Any] | None = None,
        now: float | None = None,
    ) -> bool:
        """True — событие можно класть в очередь; False — отклонить (спам).

        Побочный эффект: при accept фиксирует запись (и сбрасывает на диск),
        при reject — не фиксирует (но всё равно прунит устаревшее).
        """
        ts = time.time() if now is None else float(now)
        fp = self._fingerprint(event_type, resource_id, payload)
        records = self._read()

        widest = max(self._dedup_window, self._throttle_window)
        # прунинг устаревших (старше самого широкого окна) — файл не растёт.
        fresh = [r for r in records if (ts - float(r[0])) <= widest]

        rid = resource_id or ""

        # ДЕДУП: идентичный fp в окне dedup_window.
        if self._dedup_window > 0:
            for r in fresh:
                if r[3] == fp and (ts - float(r[0])) <= self._dedup_window:
                    self._write(fresh)  # зафиксируем прунинг, но не новую запись
                    return False

        # THROTTLE: число (type, resource) в окне throttle_window.
        if self._throttle_window > 0:
            same = [
                r
                for r in fresh
                if r[1] == event_type
                and r[2] == rid
                and (ts - float(r[0])) <= self._throttle_window
            ]
            if len(same) >= self._max_per_window:
                self._write(fresh)
                return False

        # принято — фиксируем.
        fresh.append([ts, event_type, rid, fp])
        self._write(fresh)
        return True

    def record_count(self) -> int:
        return len(self._read())
