"""Здоровье маршрутов backend: 404/405 на фоновом пути перестаёт быть тишиной.

**Проблема, которую чинит модуль (#1441).** Фоновый путь демона глушит
исключения (``except Exception: return report``), потому что тонуть из-за одного
неудачного опроса очереди он не должен. Побочный эффект: ``404 Not Found`` на
переименованном роуте неотличим от «заданий нет». Устройство молчит, лог пуст,
веб показывает «онлайн» — и так до тех пор, пока владелец не заметит, что кнопка
«Установить» ничего не делает.

404/405 — это **не** сетевой сбой и **не** пустая очередь. Это «клиент и сервер
разошлись по контракту», то есть состояние, которое само не пройдёт и требует
обновления CLI. Поэтому оно:

1. пишется WARNING в ``logs/daemon.log`` (всегда, независимо от ``log_level``);
2. поднимает флаг в ``~/.skillery/route_health.json``, который видно в
   ``skillery status`` — там, куда владелец смотрит первым делом.

Флаг снимается сам, как только тот же маршрут ответил успешно
(:func:`record_ok`) — «починилось после апдейта CLI» не требует ручной уборки.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Статусы, означающие расхождение контракта (а не сбой сети или прав).
CONTRACT_STATUSES = frozenset({404, 405})

_MAX_ENTRIES = 20


def state_path() -> Path:
    """``~/.skillery/route_health.json`` (переопределяется ``SKILLERY_HOME``)."""
    home = os.environ.get("SKILLERY_HOME")
    base = Path(home) if home else Path.home() / ".skillery"
    return base / "route_health.json"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except Exception:
        return {"unknown_routes": {}}
    if not isinstance(data, dict) or not isinstance(
        data.get("unknown_routes"), dict
    ):
        return {"unknown_routes": {}}
    return data


def _save(data: dict[str, Any]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception:
        return


def key_of(method: str, path: str) -> str:
    """``GET /me/device-queue`` — ключ записи (query отрезан)."""
    return f"{method.upper()} {path.split('?', 1)[0]}"


def record_unknown_route(
    method: str, path: str, status: int, *, source: str = "daemon"
) -> None:
    """Зафиксировать «backend не знает этот маршрут» + WARNING в daemon.log."""
    now = datetime.now(UTC).isoformat()
    key = key_of(method, path)
    data = _load()
    routes: dict[str, Any] = data["unknown_routes"]
    entry = routes.get(key)
    if isinstance(entry, dict):
        entry["last_seen"] = now
        entry["count"] = int(entry.get("count", 0)) + 1
        entry["status"] = status
        entry["source"] = source
    else:
        routes[key] = {
            "status": status,
            "source": source,
            "first_seen": now,
            "last_seen": now,
            "count": 1,
        }
    # Потолок: не даём файлу расти бесконечно при системном расхождении версий.
    if len(routes) > _MAX_ENTRIES:
        for stale in sorted(routes, key=lambda k: routes[k].get("last_seen", ""))[
            : len(routes) - _MAX_ENTRIES
        ]:
            routes.pop(stale, None)
    _save(data)

    try:
        import logging

        from skillery_cli.core.logging_setup import always_on_logger

        log = always_on_logger(
            "route", filename="daemon.log", level=logging.WARNING
        )
        log.warning(
            "backend не знает маршрут — CLI устарел относительно хаба",
            extra={"context": {
                "route": key,
                "status": status,
                "source": source,
                "hint": "обнови CLI: skillery self upgrade",
            }},
        )
    except Exception:
        return


def record_ok(method: str, path: str) -> None:
    """Маршрут ответил успешно — снимаем флаг, если он был."""
    key = key_of(method, path)
    data = _load()
    if key in data["unknown_routes"]:
        data["unknown_routes"].pop(key, None)
        _save(data)


def unknown_routes() -> list[dict[str, Any]]:
    """Список расхождений контракта для ``skillery status`` (свежие первыми)."""
    routes = _load()["unknown_routes"]
    items = [dict(value, route=key) for key, value in routes.items()]
    return sorted(items, key=lambda i: i.get("last_seen", ""), reverse=True)


def clear() -> None:
    """Сбросить все флаги (после успешного апдейта CLI / вручную)."""
    _save({"unknown_routes": {}})
