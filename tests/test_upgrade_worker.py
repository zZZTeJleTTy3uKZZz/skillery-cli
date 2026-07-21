"""Автономный worker апгрейда: единственность, полное гашение, возврат демона.

Пользователь ловил три беды подряд: обновление молча не наступало, всплывали
окна терминала, а после апгрейда демон оставался лежать, пока не запустишь
какую-нибудь команду. Здесь закреплены инварианты, которыми это лечится.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from skillery_cli import _upgrade_worker as w


class TestSingleFlight:
    """Два одновременных апгрейда рвут установку — второй не должен стартовать.

    Гонка двух worker'ов на перезаписи одних файлов оставляла trampoline битым:
    `skillery` падал с «uv trampoline failed to canonicalize script path» и
    лечился только полной переустановкой.
    """

    def test_second_acquire_fails_while_held(self) -> None:
        first = w.acquire_lock()
        assert first is not None
        try:
            assert w.acquire_lock() is None, "два апгрейда одновременно недопустимы"
        finally:
            _release(first)

    def test_cli_sees_running_upgrade(self, monkeypatch) -> None:
        from skillery_cli import __main__ as m

        held = w.acquire_lock()
        try:
            assert m._upgrade_already_running() is True
        finally:
            _release(held)

    def test_cli_does_not_spawn_when_upgrade_in_flight(self, monkeypatch) -> None:
        from skillery_cli import __main__ as m

        spawned: list = []
        monkeypatch.setattr(m, "_upgrade_already_running", lambda: True)
        monkeypatch.setattr("subprocess.Popen", lambda *a, **k: spawned.append(a))

        assert m._spawn_background_upgrade(delay=0, version="1.2.3") is False
        assert spawned == []


class TestStopsEverything:
    """Перед заменой файлов гасим ВСЮ цепочку, а не только PID-файл."""

    def test_waits_until_processes_are_gone(self, monkeypatch) -> None:
        """Ждать обязательно: пока хоть один держит Scripts/, uv даёт os error 5."""
        seen = iter([[111], [111], []])
        monkeypatch.setattr(w, "find_daemon_pids", lambda: next(seen, []))
        monkeypatch.setattr(w.time, "sleep", lambda s: None)
        killed = []
        if sys.platform == "win32":
            monkeypatch.setattr(w, "IS_WIN", False)  # уводим на posix-ветку kill
        monkeypatch.setattr(w.os, "kill", lambda pid, sig: killed.append(pid))

        assert w.stop_daemons(timeout=1) == [111]
        assert killed == [111]

    def test_survives_missing_tooling(self, monkeypatch) -> None:
        def _boom(*a, **k):
            raise FileNotFoundError("нет powershell/pgrep")

        monkeypatch.setattr(w.subprocess, "run", _boom)
        assert w.find_daemon_pids() == []


class TestBringsDaemonBack:
    """После апгрейда демон обязан подняться САМ.

    Раньше worker только менял файлы, а демон ждал следующей команды CLI
    (самолечения) — до тех пор очередь заданий не применялась и устройство
    выглядело офлайн.
    """

    def test_daemon_started_after_successful_upgrade(self, monkeypatch, tmp_path) -> None:
        started: list = []
        monkeypatch.setattr(w, "acquire_lock", lambda: True)
        monkeypatch.setattr(w.time, "sleep", lambda s: None)
        monkeypatch.setattr(w, "stop_daemons", lambda *a, **k: [])
        monkeypatch.setattr(w, "run_upgrade", lambda cmds: True)
        monkeypatch.setattr(w, "start_daemon", lambda b: started.append(b) or True)

        cfg = tmp_path / "c.json"
        cfg.write_text('{"delay":0,"commands":[],"daemon_binary":"/x/skillery"}',
                       encoding="utf-8")

        assert w.main(["worker", str(cfg)]) == 0
        assert started == ["/x/skillery"]

    def test_daemon_started_even_if_upgrade_failed(self, monkeypatch, tmp_path) -> None:
        """Не обновились — не повод оставлять пользователя без демона."""
        started: list = []
        monkeypatch.setattr(w, "acquire_lock", lambda: True)
        monkeypatch.setattr(w.time, "sleep", lambda s: None)
        monkeypatch.setattr(w, "stop_daemons", lambda *a, **k: [])
        monkeypatch.setattr(w, "run_upgrade", lambda cmds: False)
        monkeypatch.setattr(w, "start_daemon", lambda b: started.append(b) or True)

        cfg = tmp_path / "c.json"
        cfg.write_text('{"delay":0,"commands":[],"daemon_binary":"/x/skillery"}',
                       encoding="utf-8")

        assert w.main(["worker", str(cfg)]) == 1  # апгрейд не удался…
        assert started == ["/x/skillery"]         # …но демон вернулся

    def test_does_nothing_when_upgrade_already_running(self, monkeypatch, tmp_path) -> None:
        touched: list = []
        monkeypatch.setattr(w, "acquire_lock", lambda: None)
        monkeypatch.setattr(w, "stop_daemons", lambda *a, **k: touched.append("stop"))
        monkeypatch.setattr(w, "run_upgrade", lambda cmds: touched.append("run"))

        cfg = tmp_path / "c.json"
        cfg.write_text('{"delay":0,"commands":[]}', encoding="utf-8")

        assert w.main(["worker", str(cfg)]) == 0
        assert touched == [], "второй worker не должен трогать установку"


class TestNoWindows:
    """Ни один дочерний процесс не показывает консольного окна."""

    @pytest.mark.skipif(sys.platform != "win32", reason="windows-специфика")
    def test_child_calls_carry_no_window_flag(self) -> None:
        assert w._no_window_kwargs()["creationflags"] == w.CREATE_NO_WINDOW

    @pytest.mark.skipif(sys.platform != "win32", reason="windows-специфика")
    def test_daemon_start_is_windowless_and_checks_rc(self, monkeypatch) -> None:
        """Синхронный запуск (проверяем rc), без окна. Detach делает cmd_daemon_start."""
        captured: dict = {}

        class _R:
            returncode = 0

        def _run(cmd, **kw):
            captured.update(cmd=cmd, kw=kw)
            return _R()

        monkeypatch.setattr(w.subprocess, "run", _run)

        assert w.start_daemon("skillery.exe") is True
        assert captured["kw"]["creationflags"] & w.CREATE_NO_WINDOW
        assert captured["cmd"] == ["skillery.exe", "daemon", "start"]

    def test_daemon_start_retries_broken_trampoline(self, monkeypatch) -> None:
        """Сразу после апгрейда trampoline транзиентно битый — повторяем."""
        rcs = iter([1, 1, 0])  # первые два раза бинарь ещё не готов
        monkeypatch.setattr(w.time, "sleep", lambda s: None)

        class _R:
            def __init__(self, rc):
                self.returncode = rc

        monkeypatch.setattr(w.subprocess, "run", lambda cmd, **kw: _R(next(rcs)))

        assert w.start_daemon("skillery.exe") is True

    def test_chain_stops_at_first_success(self, monkeypatch) -> None:
        calls: list = []

        class _R:
            returncode = 0

        def _run(cmd, **kw):
            calls.append(cmd)
            return _R()

        monkeypatch.setattr(w.subprocess, "run", _run)
        assert w.run_upgrade([["a"], ["b"]]) is True
        assert calls == [["a"]], "вторая команда не нужна после успеха"


def _release(handle) -> None:
    """Отпустить лок независимо от платформы (в тестах — вручную)."""
    if handle in (None, True):
        return
    if sys.platform == "win32":
        import ctypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
            ctypes.c_void_p(handle)
        )
    else:
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
