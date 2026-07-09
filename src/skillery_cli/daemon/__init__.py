"""Event tracking daemon.

Состоит из трёх компонентов:

- :mod:`event_collector` — append-only очередь events в
  ``~/.skillery/events.queue.json``.
- :mod:`event_sender` — batch sender, раз в N секунд снимает chunk из
  очереди и POST'ит на ``/events``.
- :mod:`daemon_runner` — long-running orchestrator.

И четыре autostart-installer'а (systemd / launchd / Task Scheduler) —
см. :mod:`autostart`.

Daemon НЕ требует чтобы реально стоял autostart: ``skillery daemon
install`` только генерирует unit-file + печатает инструкцию что с ним
делать. Это даёт возможность тестировать pipeline на чистой VM без
sudo.
"""
from skillery_cli.daemon.event_collector import EventCollector, QueuedEvent
from skillery_cli.daemon.event_sender import EventSender
from skillery_cli.daemon.daemon_runner import DaemonRunner
from skillery_cli.daemon.instrumentation import track_skill_event

__all__ = [
    "EventCollector",
    "QueuedEvent",
    "EventSender",
    "DaemonRunner",
    "track_skill_event",
]
