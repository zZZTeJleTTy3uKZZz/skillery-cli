"""Живучесть демона (инцидент 2026-07-24 на машине владельца).

Симптом: ``skillery daemon status`` → ``Daemon: stopped``, ``Cycles: 1``.
Push-обновление (backend→PyPI→вебхук→фан-аут) доехало до статуса ``delivered``,
но CLI его не применил — потому что демон умирал ПОСЛЕ ПЕРВОГО ТАКТА и очередь
больше никто не опрашивал.

Две первопричины, зафиксированные здесь тестами:

1. **САМОУБИЙСТВО.** ``_spawn_background_upgrade`` перед стартом worker'а звал
   ``_stop_daemon_for_upgrade``, а тот слал SIGTERM/TerminateProcess по PID из
   PID-файла. Когда апгрейд инициирует САМ демон (push-задача ``cli_upgrade`` и
   старт-fallback ``_daemon_cli_self_upgrade(force=True)``), в PID-файле стоит
   ЕГО СОБСТВЕННЫЙ pid → демон убивал себя ДО ``subprocess.Popen`` worker'а: ни
   апгрейда, ни демона.
2. **НЕЗАЩИЩЁННЫЙ СТАРТ ЦИКЛА.** ``DaemonRunner.run_forever`` защищал ТАКТ, но
   ни ``_build_runner``, ни сам ``asyncio.run`` защищены не были: секундный
   сетевой сбой ронял процесс насовсем.

Инвариант: исключение из любого шага такта/старта → ERROR в лог и СЛЕДУЮЩИЙ
такт; процесс жив.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import suppress
from pathlib import Path

import pytest
from librarykit.errors import TransportError

from skillery_cli import __main__ as m
from skillery_cli.commands import daemon as daemon_cmd
from skillery_cli.core import logging_setup as ls
from skillery_cli.daemon.daemon_runner import DaemonRunner
from skillery_cli.daemon.event_sender import SendResult

_LOGGER_NAMES = ("skillery", "skillery.daemon", "skillery.reconcile", "skillery.install")


@pytest.fixture(autouse=True)
def _reset_loggers():
    def _clear() -> None:
        for name in _LOGGER_NAMES:
            lg = logging.getLogger(name)
            for h in list(lg.handlers):
                lg.removeHandler(h)
                with suppress(Exception):
                    h.close()

    _clear()
    ls._configured = False
    yield
    _clear()
    ls._configured = False


def _records(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# 1. Демон не убивает сам себя перед апгрейдом
# --------------------------------------------------------------------------- #
class TestNoSelfKillOnUpgrade:
    def test_stop_daemon_for_upgrade_never_kills_current_process(
        self, monkeypatch
    ) -> None:
        """PID-файл указывает на НАС → не стреляем (иначе демон умирает молча)."""
        killed: list[int] = []
        monkeypatch.setattr(
            "skillery_cli.daemon.daemon_runner.read_running_pid", lambda: os.getpid()
        )
        monkeypatch.setattr(
            "skillery_cli.daemon.daemon_runner.is_process_alive", lambda pid: True
        )
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))

        assert m._stop_daemon_for_upgrade() is False
        assert killed == [], "демон отправил SIGTERM самому себе"

    def test_stop_daemon_for_upgrade_still_kills_foreign_daemon(
        self, monkeypatch
    ) -> None:
        """Чужой (реальный фоновый) демон гасится как раньше — регресс не сломан."""
        killed: list[int] = []
        foreign = os.getpid() + 12345
        monkeypatch.setattr(
            "skillery_cli.daemon.daemon_runner.read_running_pid", lambda: foreign
        )
        alive = {"v": True}

        def _alive(pid: int) -> bool:
            if pid == foreign and killed:
                alive["v"] = False
            return alive["v"]

        monkeypatch.setattr(
            "skillery_cli.daemon.daemon_runner.is_process_alive", _alive
        )
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append(pid))

        assert m._stop_daemon_for_upgrade() is True
        assert killed == [foreign]

    def test_spawn_background_upgrade_from_daemon_spawns_worker(
        self, monkeypatch, tmp_path
    ) -> None:
        """Апгрейд, начатый САМИМ демоном, доходит до запуска worker'а.

        Раньше процесс умирал на ``_stop_daemon_for_upgrade`` — до ``Popen`` дело
        не доходило: ни обновления, ни демона. Задача при этом висела в
        ``delivered`` вечно.
        """
        import subprocess

        monkeypatch.setattr(m, "_upgrade_already_running", lambda: False)
        # PID-файл указывает на нас: мы и есть демон.
        monkeypatch.setattr(
            "skillery_cli.daemon.daemon_runner.read_running_pid", lambda: os.getpid()
        )
        monkeypatch.setattr(
            "skillery_cli.daemon.daemon_runner.is_process_alive", lambda pid: True
        )
        suicides: list[int] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: suicides.append(pid))
        spawned: list[list[str]] = []

        class _Proc:
            pid = 4242

        monkeypatch.setattr(
            subprocess, "Popen", lambda args, **kw: (spawned.append(args) or _Proc())
        )

        assert m._spawn_background_upgrade(version="9.9.9") is True
        assert suicides == []
        assert spawned and "_upgrade_worker.py" in " ".join(spawned[0])


# --------------------------------------------------------------------------- #
# 2. Такт демона переживает ЛЮБУЮ ошибку и делает следующий
# --------------------------------------------------------------------------- #
class _BoomSender:
    """Первый такт падает сетевой ошибкой, второй — успешен."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    async def send_once(self) -> SendResult:
        self.calls += 1
        if self.calls == 1:
            raise self._exc
        return SendResult(sent=0, accepted=0, skipped=0, requeued=0, last_error=None)


def _runner(tmp_path: Path, sender, **kw) -> DaemonRunner:
    return DaemonRunner(
        sender,  # type: ignore[arg-type]
        interval_seconds=1,
        pid_path=tmp_path / "d.pid",
        state_path=tmp_path / "d.state.json",
        **kw,
    )


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(
            TransportError("ConnectError: "), id="transport-error-empty-text"
        ),
        pytest.param(OSError(10054, "соединение сброшено"), id="oserror"),
        pytest.param(BaseExceptionGroup("net", [OSError("boom")]), id="exception-group"),
    ],
)
async def test_cycle_failure_logs_error_and_next_cycle_runs(
    tmp_path, monkeypatch, exc
) -> None:
    """Сбой такта → ERROR в daemon.log + СЛЕДУЮЩИЙ такт; процесс жив."""
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    monkeypatch.delenv("SKILLERY_LOG_LEVEL", raising=False)
    ls.configure_logging("error", filename="daemon.log")

    sender = _BoomSender(exc)
    runner = _runner(tmp_path, sender)

    async def _drive() -> None:
        task = asyncio.create_task(runner.run_forever())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if sender.calls >= 2:
                break
        assert not task.done(), "демон умер после первого такта"
        runner.stop()
        await asyncio.wait_for(task, timeout=5)

    await _drive()

    assert sender.calls >= 2, "второго такта не было — демон не пережил ошибку"
    errors = [r for r in _records(tmp_path / "daemon.log") if r["level"] == "ERROR"]
    assert errors, "провал такта не оставил ERROR-следа"
    ctx = errors[0].get("context") or {}
    # Пустой str(exc) («ConnectError: ») бесполезен — тогда обязан быть тип.
    assert ctx.get("error_type"), "в логе нет типа исключения"


async def test_reconcile_failure_does_not_stop_the_loop(tmp_path, monkeypatch) -> None:
    """Провал reconcile (сеть) логируется и НЕ мешает следующему такту."""
    monkeypatch.setattr(ls, "log_dir", lambda: tmp_path)
    monkeypatch.delenv("SKILLERY_LOG_LEVEL", raising=False)
    ls.configure_logging("error", filename="daemon.log")

    class _OkSender:
        async def send_once(self) -> SendResult:
            return SendResult(sent=0, accepted=0, skipped=0, requeued=0, last_error=None)

    calls = {"n": 0}

    async def _reconcile() -> None:
        calls["n"] += 1
        raise RuntimeError("")  # пустой текст — как в живом инциденте

    runner = _runner(tmp_path, _OkSender(), reconcile=_reconcile)
    await runner.cycle_once()
    await runner.cycle_once()

    assert calls["n"] == 2
    errors = [r for r in _records(tmp_path / "daemon.log") if r["level"] == "ERROR"]
    assert errors, "провал reconcile не оставил следа"
    assert (errors[0].get("context") or {}).get("error_type") == "RuntimeError"


# --------------------------------------------------------------------------- #
# 3. Супервизор: сбой ВНЕ такта (построение runner'а / asyncio.run) — не смерть
# --------------------------------------------------------------------------- #
class TestSupervisor:
    def test_build_failure_is_logged_and_retried(self, monkeypatch) -> None:
        """``_build_runner`` падает → ERROR + пауза + новый заход (демон жив)."""
        attempts = {"n": 0}

        def _boom(*a, **k):
            attempts["n"] += 1
            raise RuntimeError("нет сети при чтении конфига")

        monkeypatch.setattr(daemon_cmd, "_build_runner", _boom)
        logged: list[tuple[str, dict]] = []

        class _Log:
            def error(self, msg, **kw):
                logged.append((msg, kw.get("extra", {})))

            def warning(self, *a, **k):
                pass

        slept: list[float] = []
        restarts = daemon_cmd._supervise_daemon(
            60.0, log=_Log(), max_restarts=3, sleep=slept.append
        )

        assert restarts == 3
        assert attempts["n"] == 3, "супервизор не повторил заход"
        assert logged, "падение цикла не залогировано"
        assert slept == [5.0, 10.0], "нет backoff между перезапусками"

    def test_run_forever_failure_is_retried(self, monkeypatch) -> None:
        """Падение самого ``asyncio.run(run_forever)`` тоже переживаем."""
        calls = {"n": 0}

        class _Runner:
            async def run_forever(self) -> None:
                calls["n"] += 1
                raise OSError("TLS handshake failed")

        monkeypatch.setattr(daemon_cmd, "_build_runner", lambda **kw: _Runner())
        restarts = daemon_cmd._supervise_daemon(
            60.0, log=None, max_restarts=2, sleep=lambda _d: None
        )
        assert restarts == 2
        assert calls["n"] == 2

    def test_clean_stop_returns_without_restart(self, monkeypatch) -> None:
        """Штатная остановка (stop/SIGTERM) — выходим, а не крутим цикл."""

        class _Runner:
            async def run_forever(self) -> None:
                return None

        monkeypatch.setattr(daemon_cmd, "_build_runner", lambda **kw: _Runner())
        assert daemon_cmd._supervise_daemon(60.0, log=None, sleep=lambda _d: None) == 0

    def test_cmd_daemon_run_uses_supervisor(self) -> None:
        """Команда обязана идти через супервизор, а не звать asyncio.run напрямую."""
        import inspect

        src = inspect.getsource(daemon_cmd.cmd_daemon_run)
        assert "_supervise_daemon" in src
