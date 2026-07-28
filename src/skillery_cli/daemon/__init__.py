"""Демон CLI: доставка ОБЩЕЙ исходящей очереди + опрос очереди устройства.

Состав:

- :mod:`instrumentation` — продюсер аналитики (``track_skill_event``): кладёт
  событие в ОБЩИЙ outbox конвертом ``kind="analytics_event"``
  (:mod:`skillery_cli.core.analytics_sync`). Своей очереди у аналитики больше
  нет — #1180 убрал третью (``~/.skillery/events.queue.json``).
- :mod:`event_guard` — анти-спам (дедуп + throttle) ПЕРЕД публикацией. Это
  свойство продюсера, а не очереди, поэтому переезд его не затронул.
- :mod:`event_sender` — такт доставки: обёртка над единым воркером
  :mod:`skillery_cli.core.outbox_worker`.
- :mod:`daemon_runner` — long-running orchestrator (такт + backoff + state).

И autostart-installer'ы (systemd / launchd / Task Scheduler) — см.
:mod:`autostart`. Демон НЕ требует, чтобы реально стоял autostart: ``skillery
daemon install`` только генерирует unit-file и печатает инструкцию, что с ним
делать. Это даёт возможность тестировать pipeline на чистой VM без sudo.
"""
from skillery_cli.daemon.daemon_runner import DaemonRunner
from skillery_cli.daemon.event_guard import EventGuard
from skillery_cli.daemon.event_sender import OutboxSender, SendResult
from skillery_cli.daemon.instrumentation import track_skill_event

__all__ = [
    "DaemonRunner",
    "EventGuard",
    "OutboxSender",
    "SendResult",
    "track_skill_event",
]
