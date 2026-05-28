"""Skill-side instrumentation: silent event push в очередь.

Когда CLI делает ``install`` / ``update`` / ``uninstall`` / ``run``, мы
кладём в очередь соответствующий event (``skill.install`` /
``skill.update`` / etc.). Daemon позже отправит batch.

Эта функция НЕ создаёт offending dependencies между __main__ и daemon:
если очередь не пишется (permission denied, missing dir) — всё
silently ignored. Telemetry никогда не ломает основную команду.
"""
from __future__ import annotations

from typing import Any

from skills_hub_cli.daemon.daemon_runner import default_queue_path
from skills_hub_cli.daemon.event_collector import EventCollector


def track_skill_event(
    event_type: str,
    *,
    slug: str,
    version: str | None = None,
    scope: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Положить event в очередь. Никогда не raises (silent fail).

    Параметры:
        event_type: ``skill.install`` / ``skill.update`` / ``skill.uninstall``
            / ``skill.run``.
        slug: slug скилла.
        version: версия (если применимо).
        scope: ``global`` / ``project``.
        extra: дополнительные поля в payload.
    """
    try:
        payload: dict[str, Any] = {"slug": slug}
        if version:
            payload["version"] = version
        if scope:
            payload["scope"] = scope
        if extra:
            payload.update(extra)
        collector = EventCollector(default_queue_path())
        collector.append(
            event_type,
            resource_type="skill",
            resource_id=slug,
            payload=payload,
            metadata={"source": "cli"},
        )
    except Exception:
        # Telemetry никогда не ломает основную команду.
        pass
