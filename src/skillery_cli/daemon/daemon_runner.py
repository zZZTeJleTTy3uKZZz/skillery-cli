"""Long-running daemon process: периодически зовёт ``EventSender.send_once``.

CLI поднимает daemon как detached background process:
- POSIX — ``os.fork`` + ``setsid`` + redirect stdio to /dev/null.
- Windows — ``subprocess.Popen`` с ``DETACHED_PROCESS`` flag.

PID + state файлы (каталог — общий с config CLI, ``~/.skillery`` с
legacy-fallback ``~/.skillery``):
- ``~/.skillery/daemon.pid`` — PID запущенного процесса.
- ``~/.skillery/daemon.state.json`` — last_cycle_at, last_send_result.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from skillery_cli.core.logging_setup import get_logger

ReconcileCallback = Callable[[], Awaitable[None]]
"""Опциональный best-effort хук reconcile-инсталлов («нажал в вебе → демон
скачал»). Вызывается ПОСЛЕ send_once и оборачивается в suppress(Exception),
чтобы НЕ ломать event-цикл. None ⇒ демон только шлёт события (как раньше)."""

from skillery_cli.daemon.backoff import BackoffPolicy
from skillery_cli.daemon.event_collector import EventCollector
from skillery_cli.daemon.event_sender import EventSender

# Логгер демон-цикла. Пишет в тот файл, что настроил процесс (``daemon.log`` в
# демоне, ``cli.log`` в foreground). На стандартном ERROR молчит про такты
# (DEBUG отбрасывается), но ошибки цикла/reconcile видны всегда.
_log = get_logger("daemon")


@dataclass
class DaemonState:
    started_at: str | None = None
    last_cycle_at: str | None = None
    cycles: int = 0
    total_sent: int = 0
    total_accepted: int = 0
    last_error: str | None = None
    last_send: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _default_data_dir() -> Path:
    """Каталог daemon-артефактов (pid/state/queue/guard).

    Делегируем в ``config._default_config_dir`` — единый источник правды о
    home-каталоге (ребренд ``~/.skillery`` + legacy-fallback ``~/.skillery`` +
    уважение env ``SKILLERY_CONFIG_DIR`` и профилей). Раньше тут была СВОЯ
    копия env+дефолта — при рассинхроне demon писал бы pid/queue в старый
    каталог, мимо config. Импорт ленивый, чтобы не тянуть config на уровне
    модуля (daemon-модуль автономен в тестах).
    """
    from skillery_cli.config import _default_config_dir

    return _default_config_dir()


def default_queue_path() -> Path:
    return _default_data_dir() / "events.queue.json"


def default_guard_path() -> Path:
    """Sidecar для анти-спам ``EventGuard`` (дедуп/throttle окно)."""
    return _default_data_dir() / "events.guard.json"


def default_pid_path() -> Path:
    return _default_data_dir() / "daemon.pid"


def default_state_path() -> Path:
    return _default_data_dir() / "daemon.state.json"


def is_process_alive(pid: int) -> bool:
    """Кросс-платформенная проверка alive по PID."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # На Windows используем OpenProcess через ctypes; если процесс жив —
        # handle ≠ 0, иначе 0. Без cleanup — handle leak'нем, но это short-lived.
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            h = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not h:
                return False
            try:
                exit_code = ctypes.c_ulong()
                ok = kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
                if not ok:
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_process(pid: int) -> bool:
    """Послать SIGTERM (POSIX) / TerminateProcess (Windows).

    Returns True если сигнал отправился (не гарантирует graceful shutdown).
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            PROCESS_TERMINATE = 0x0001
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if not h:
                return False
            try:
                ok = kernel32.TerminateProcess(h, 1)
                return bool(ok)
            finally:
                kernel32.CloseHandle(h)
        except Exception:
            return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except ProcessLookupError:
        return False


class DaemonRunner:
    """Orchestrator: цикл send_once → sleep(interval) → repeat.

    Управляет state-файлами; не делает fork (это задача
    ``daemon_start_detached``). Для тестов — можно вызвать ``cycle_once``
    напрямую.
    """

    def __init__(
        self,
        sender: EventSender,
        *,
        interval_seconds: float = 60.0,
        pid_path: Path | None = None,
        state_path: Path | None = None,
        backoff: BackoffPolicy | None = None,
        reconcile: ReconcileCallback | None = None,
    ) -> None:
        self._sender = sender
        self._interval = max(interval_seconds, 1.0)
        self._pid_path = pid_path or default_pid_path()
        self._state_path = state_path or default_state_path()
        self._stop = asyncio.Event()
        self._state = DaemonState()
        # Экспоненциальный backoff между провальными flush'ами: cap 1ч,
        # фактор 2. Пустой цикл (нечего слать) backoff НЕ растит.
        self._backoff = backoff or BackoffPolicy(factor=2.0, max_delay=3600.0)
        # Best-effort reconcile-инсталлов («нажал в вебе → демон скачал»).
        # None ⇒ демон только шлёт события (поведение/тесты не меняются).
        self._reconcile = reconcile

    @property
    def state(self) -> DaemonState:
        return self._state

    @property
    def pid_path(self) -> Path:
        return self._pid_path

    @property
    def state_path(self) -> Path:
        return self._state_path

    def _ensure_dir(self) -> None:
        self._pid_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.parent.mkdir(parents=True, exist_ok=True)

    def _write_pid(self) -> None:
        self._ensure_dir()
        self._pid_path.write_text(str(os.getpid()), encoding="utf-8")

    def _clear_pid(self) -> None:
        with suppress(OSError):
            self._pid_path.unlink()

    def _write_state(self) -> None:
        self._ensure_dir()
        self._state_path.write_text(
            json.dumps(self._state.to_dict(), ensure_ascii=False, default=str),
            encoding="utf-8",
        )

    async def cycle_once(self) -> dict[str, Any]:
        """Один такт: send_once (+ best-effort reconcile) + обновление state."""
        result = await self._sender.send_once()
        self._state.cycles += 1
        self._state.last_cycle_at = datetime.now(UTC).isoformat()
        self._state.total_sent += result.sent
        self._state.total_accepted += result.accepted
        self._state.last_error = result.last_error
        self._state.last_send = {
            "sent": result.sent,
            "accepted": result.accepted,
            "skipped": result.skipped,
            "requeued": result.requeued,
            "last_error": result.last_error,
        }
        self._write_state()
        # Трейс такта на DEBUG: на стандартном ERROR не пишется (лог не пухнет),
        # на debug/verbose виден каждый sent/accepted/requeued.
        with suppress(Exception):
            _log.debug("daemon cycle", extra={"context": {
                "cycle": self._state.cycles,
                "sent": result.sent,
                "accepted": result.accepted,
                "skipped": result.skipped,
                "requeued": result.requeued,
                "last_error": result.last_error,
            }})
        # Best-effort reconcile ПОСЛЕ event-flush: «нажал Установить в вебе →
        # демон скачал». Ошибка reconcile НЕ валит event-цикл и его backoff-
        # классификацию (по send-результату выше), но теперь ОБЯЗАНА оставить
        # ERROR-след (раньше глоталась suppress'ом бесследно).
        if self._reconcile is not None:
            try:
                await self._reconcile()
            except Exception as exc:  # noqa: BLE001 — reconcile best-effort
                with suppress(Exception):
                    _log.error(
                        "daemon reconcile failed",
                        exc_info=exc,
                        extra={"context": {"error": str(exc)}},
                    )
        return self._state.last_send

    async def run_forever(self) -> None:
        """Старт цикла. Останавливается по SIGTERM / stop()."""
        self._state.started_at = datetime.now(UTC).isoformat()
        self._write_pid()
        self._write_state()
        if sys.platform != "win32":
            loop = asyncio.get_running_loop()
            for sig_name in ("SIGTERM", "SIGINT"):
                sig = getattr(signal, sig_name, None)
                if sig is not None:
                    with suppress(NotImplementedError, RuntimeError):
                        loop.add_signal_handler(sig, self._stop.set)
        try:
            while not self._stop.is_set():
                last: dict[str, Any] = {}
                try:
                    last = await self.cycle_once()
                except Exception as exc:  # noqa: BLE001 — такт не валит демон
                    with suppress(Exception):
                        _log.error(
                            "daemon cycle failed",
                            exc_info=exc,
                            extra={"context": {"error": str(exc)}},
                        )
                # Классификация исхода для backoff: «провал» = что-то слали, но
                # всё ушло в requeue (network/5xx). Пустой цикл (sent=0) и успех
                # сбрасывают backoff к базовому интервалу.
                sent = int(last.get("sent", 0)) if last else 0
                requeued = int(last.get("requeued", 0)) if last else 0
                if sent > 0 and requeued >= sent:
                    self._backoff.record_failure()
                else:
                    self._backoff.record_success()
                delay = self._backoff.current_delay(base=self._interval)
                with suppress(Exception):
                    _log.debug("daemon backoff", extra={"context": {
                        "sent": sent, "requeued": requeued, "delay": delay,
                    }})
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    continue
        finally:
            self._clear_pid()

    def stop(self) -> None:
        self._stop.set()


def read_running_pid(pid_path: Path | None = None) -> int | None:
    """Прочитать PID-файл; вернуть int или None если файла нет."""
    p = pid_path or default_pid_path()
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8").strip()
        return int(raw)
    except (OSError, ValueError):
        return None


def read_state(state_path: Path | None = None) -> dict[str, Any]:
    """Прочитать state-файл daemon'а."""
    p = state_path or default_state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
