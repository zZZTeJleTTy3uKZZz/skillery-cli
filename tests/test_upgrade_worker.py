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


def _quiet_cycle(
    monkeypatch,
    *,
    upgrade_ok: bool,
    started: list | None = None,
    alive: bool = True,
) -> None:
    """Заглушить внешний мир worker'а, оставив логику ``main`` (#1399).

    ЧТО ЗАМОКАНО и почему — всё это Windows-специфика, не воспроизводимая в CI:
    ядерный лок, ``time.sleep``, перечисление/убийство процессов
    (powershell ``Get-CimInstance``), планировщик ``schtasks``, реальный запуск
    демона и запись sidecar'а в настоящий HOME.
    """
    monkeypatch.setattr(w, "acquire_lock", lambda: True)
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    monkeypatch.setattr(w, "stop_daemons", lambda *a, **k: [])
    monkeypatch.setattr(w, "run_upgrade", lambda cmds: upgrade_ok)
    monkeypatch.setattr(w, "suspend_watchdog", lambda *a, **k: True)
    monkeypatch.setattr(w, "restore_watchdog", lambda *a, **k: True)

    def _ensure(binary, **kw):
        if started is not None:
            started.append(binary)
        return {
            "pids": [4242] if alive else [],
            "status_rc": 0 if alive else 1,
            "alive": alive,
            "started_with": binary if alive else None,
        }

    monkeypatch.setattr(w, "ensure_daemon_back", _ensure)
    monkeypatch.setattr(w, "write_result", lambda *a, **k: None)


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
        # МОК: перечисление процессов через powershell/pgrep в CI недоступно —
        # соседние искатели глушим, проверяем именно цикл ожидания.
        monkeypatch.setattr(w, "find_starting_daemon_pids", list)
        monkeypatch.setattr(w, "find_watchdog_pids", list)
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
        _quiet_cycle(monkeypatch, upgrade_ok=True, started=started)

        cfg = tmp_path / "c.json"
        cfg.write_text('{"delay":0,"commands":[],"daemon_binary":"/x/skillery"}',
                       encoding="utf-8")

        assert w.main(["worker", str(cfg)]) == 0
        assert started == ["/x/skillery"]

    def test_daemon_started_even_if_upgrade_failed(self, monkeypatch, tmp_path) -> None:
        """Не обновились — не повод оставлять пользователя без демона."""
        started: list = []
        _quiet_cycle(monkeypatch, upgrade_ok=False, started=started)

        cfg = tmp_path / "c.json"
        cfg.write_text('{"delay":0,"commands":[],"daemon_binary":"/x/skillery"}',
                       encoding="utf-8")

        assert w.main(["worker", str(cfg)]) == 1  # апгрейд не удался…
        assert started == ["/x/skillery"]         # …но демон вернулся

    def test_daemon_returns_even_if_install_raised(self, monkeypatch, tmp_path) -> None:
        """#1399: исключение внутри установки не имеет права «съесть» возврат демона."""
        started: list = []

        def _boom(_cmds):
            raise RuntimeError("uv умер посреди установки")

        _quiet_cycle(monkeypatch, upgrade_ok=False, started=started)
        monkeypatch.setattr(w, "run_upgrade", _boom)

        cfg = tmp_path / "c.json"
        cfg.write_text('{"delay":0,"commands":[],"daemon_binary":"/x/skillery"}',
                       encoding="utf-8")

        assert w.main(["worker", str(cfg)]) == 1
        assert started == ["/x/skillery"]


class TestWritesResult:
    """Worker кладёт итог апгрейда — чтобы «запущено → тишина» стало «✓/✗»."""

    def test_write_result_success_shape(self, tmp_path) -> None:
        import json

        w.write_result(
            {"from_version": "0.5.59", "to_version": "0.5.60"}, True, base=tmp_path
        )
        data = json.loads((tmp_path / "_upgrade_result.json").read_text("utf-8"))
        assert data == {"ok": True, "from": "0.5.59", "to": "0.5.60", "error": ""}

    def test_write_result_failure_carries_error(self, tmp_path) -> None:
        import json

        w.write_result(
            {"from_version": "0.5.59", "to_version": "0.5.60"},
            False,
            "boom",
            base=tmp_path,
        )
        data = json.loads((tmp_path / "_upgrade_result.json").read_text("utf-8"))
        assert data["ok"] is False and data["error"] == "boom"

    def test_main_records_result_after_upgrade(self, monkeypatch, tmp_path) -> None:
        """main() обязан записать итог (ok=True) после успешного апгрейда."""
        recorded: list = []
        _quiet_cycle(monkeypatch, upgrade_ok=True)
        monkeypatch.setattr(
            w, "write_result", lambda cfg, ok, *a, **k: recorded.append(ok)
        )

        cfg = tmp_path / "c.json"
        cfg.write_text(
            '{"delay":0,"commands":[],"from_version":"0.5.59","to_version":"0.5.60"}',
            encoding="utf-8",
        )
        assert w.main(["worker", str(cfg)]) == 0
        assert recorded == [True]

    def test_does_nothing_when_upgrade_already_running(self, monkeypatch, tmp_path) -> None:
        touched: list = []
        monkeypatch.setattr(w, "acquire_lock", lambda: None)
        monkeypatch.setattr(w, "stop_daemons", lambda *a, **k: touched.append("stop"))
        monkeypatch.setattr(w, "run_upgrade", lambda cmds: touched.append("run"))
        # #1399: второй worker не имеет права ни глушить watchdog, ни трогать
        # демона — иначе идущая установка теряет процессы из-под себя.
        monkeypatch.setattr(w, "suspend_watchdog", lambda *a, **k: touched.append("wd"))
        monkeypatch.setattr(
            w, "ensure_daemon_back", lambda *a, **k: touched.append("daemon")
        )

        cfg = tmp_path / "c.json"
        cfg.write_text('{"delay":0,"commands":[]}', encoding="utf-8")

        assert w.main(["worker", str(cfg)]) == 0
        assert touched == [], "второй worker не должен трогать установку"


class TestWatchdogWindow:
    """#1399: watchdog-тик посреди установки = «os error 5». Глушим и возвращаем.

    Первопричина с живой машины: watchdog-задача раз в 3 минуты поднимает
    ``skillery daemon start``. Он заново занимает ``Scripts/``, и
    ``uv tool install --force`` падает «failed to remove directory Scripts:
    Отказано в доступе». Отсюда порядок: DISABLE → установка → ENABLE.
    """

    def test_suspend_disables_task_and_ends_running_tick(self, monkeypatch) -> None:
        calls: list = []
        monkeypatch.setattr(w, "IS_WIN", True)  # МОК: schtasks есть только на Windows
        monkeypatch.setattr(w, "_run_quiet", lambda cmd, **k: calls.append(cmd) or 0)

        assert w.suspend_watchdog("T") is True
        assert calls == [
            ["schtasks", "/End", "/TN", "T"],
            ["schtasks", "/Change", "/TN", "T", "/DISABLE"],
        ]

    def test_suspend_is_noop_on_posix(self, monkeypatch) -> None:
        calls: list = []
        monkeypatch.setattr(w, "IS_WIN", False)
        monkeypatch.setattr(w, "_run_quiet", lambda cmd, **k: calls.append(cmd) or 0)

        assert w.suspend_watchdog("T") is False
        assert calls == [], "на systemd/launchd своя перезапускалка, schtasks нет"

    def test_restore_retries_and_skips_when_not_suspended(self, monkeypatch) -> None:
        rcs = iter([1, 0])
        monkeypatch.setattr(w.time, "sleep", lambda s: None)
        monkeypatch.setattr(w, "_run_quiet", lambda cmd, **k: next(rcs))

        assert w.restore_watchdog(True, "T") is True
        assert w.restore_watchdog(False, "T") is False, "не мы гасили — не включаем"

    def test_order_disable_install_enable(self, monkeypatch, tmp_path) -> None:
        """Инвариант последовательности: установка идёт ВНУТРИ окна без watchdog."""
        order: list = []
        _quiet_cycle(monkeypatch, upgrade_ok=True)
        monkeypatch.setattr(
            w, "suspend_watchdog", lambda *a, **k: order.append("disable") or True
        )
        monkeypatch.setattr(
            w, "stop_daemons", lambda *a, **k: order.append("stop") or []
        )
        monkeypatch.setattr(w, "run_upgrade", lambda cmds: order.append("install") or True)
        monkeypatch.setattr(
            w, "restore_watchdog", lambda flag, *a, **k: order.append("enable") or True
        )

        cfg = tmp_path / "c.json"
        cfg.write_text('{"delay":0,"commands":[],"daemon_binary":"/x/skillery"}',
                       encoding="utf-8")
        assert w.main(["worker", str(cfg)]) == 0
        assert order == ["disable", "stop", "install", "enable"]

    def test_watchdog_restored_even_if_install_failed(self, monkeypatch, tmp_path) -> None:
        """Провал установки НЕ имеет права оставить watchdog выключенным."""
        restored: list = []
        _quiet_cycle(monkeypatch, upgrade_ok=False)
        monkeypatch.setattr(
            w, "restore_watchdog", lambda flag, *a, **k: restored.append(flag) or True
        )

        cfg = tmp_path / "c.json"
        cfg.write_text('{"delay":0,"commands":[],"daemon_binary":"/x/skillery"}',
                       encoding="utf-8")
        assert w.main(["worker", str(cfg)]) == 1
        assert restored == [True]


class TestKillsEverythingHoldingScripts:
    """Держат ``Scripts/`` не только ``daemon run`` — ещё старт и watchdog-лаунчер."""

    def test_blocking_set_merges_three_sources(self, monkeypatch) -> None:
        monkeypatch.setattr(w, "find_daemon_pids", lambda: [11, 12])
        monkeypatch.setattr(w, "find_starting_daemon_pids", lambda: [12, 13])
        monkeypatch.setattr(w, "find_watchdog_pids", lambda: [14])

        assert w.find_blocking_pids() == [11, 12, 13, 14], "без дублей, все три источника"

    def test_broken_source_does_not_hide_the_others(self, monkeypatch) -> None:
        def _boom():
            raise OSError("powershell не отвечает")

        monkeypatch.setattr(w, "find_daemon_pids", lambda: [11])
        monkeypatch.setattr(w, "find_starting_daemon_pids", _boom)
        monkeypatch.setattr(w, "find_watchdog_pids", lambda: [14])

        assert w.find_blocking_pids() == [11, 14]

    def test_watchdog_search_is_windows_only(self, monkeypatch) -> None:
        monkeypatch.setattr(w, "IS_WIN", False)
        assert w.find_watchdog_pids() == []


class TestConfirmsDaemonIsBack:
    """«Команда вернула 0» — не доказательство. Доказательство — живой pid."""

    def test_confirm_uses_pids_and_status(self, monkeypatch) -> None:
        monkeypatch.setattr(w, "find_daemon_pids", lambda: [777])
        monkeypatch.setattr(w, "_run_quiet", lambda cmd, **k: 0)

        report = w.confirm_daemon("skillery.exe")
        assert report["alive"] is True
        assert report["pids"] == [777] and report["status_rc"] == 0

    def test_start_returning_zero_without_pid_is_not_success(self, monkeypatch) -> None:
        """Трамплин отвечает 0, а демон тут же умирает — это НЕ «поднят»."""
        monkeypatch.setattr(w.time, "sleep", lambda s: None)
        monkeypatch.setattr(w, "_run_quiet", lambda cmd, **k: 0)
        monkeypatch.setattr(w, "find_daemon_pids", list)
        monkeypatch.setattr(w, "tool_venv_python", lambda: None)

        assert w.ensure_daemon_back("skillery.exe", attempts=2)["alive"] is False

    def test_falls_back_to_tool_venv_python(self, monkeypatch) -> None:
        """Битый трамплин (``uv trampoline failed to canonicalize``) — запасной путь."""
        used: list = []
        alive: list = []
        monkeypatch.setattr(w.time, "sleep", lambda s: None)
        monkeypatch.setattr(w, "tool_venv_python", lambda: "/venv/pythonw.exe")

        def _run(cmd, **k):
            used.append(cmd[0])
            if cmd[0] == "/venv/pythonw.exe":
                alive.append(1)  # запасной путь поднял демон
            return 0 if cmd[0] == "/venv/pythonw.exe" else 1

        monkeypatch.setattr(w, "_run_quiet", _run)
        monkeypatch.setattr(w, "find_daemon_pids", lambda: [999] if alive else [])

        report = w.ensure_daemon_back("skillery.exe", attempts=2)
        assert report["alive"] is True
        assert report["started_with"] == "/venv/pythonw.exe"
        assert "/venv/pythonw.exe" in used

    def test_result_is_not_ok_when_daemon_did_not_return(self, monkeypatch, tmp_path) -> None:
        """Обновились, но устройство молчит — пользователю это не «✓»."""
        recorded: list = []
        _quiet_cycle(monkeypatch, upgrade_ok=True, alive=False)
        monkeypatch.setattr(
            w, "write_result", lambda cfg, ok, err="", **k: recorded.append((ok, err))
        )

        cfg = tmp_path / "c.json"
        cfg.write_text('{"delay":0,"commands":[],"daemon_binary":"/x/skillery"}',
                       encoding="utf-8")
        w.main(["worker", str(cfg)])
        assert recorded and recorded[0][0] is False
        assert "демон" in recorded[0][1]

    def test_result_carries_daemon_proof(self, tmp_path) -> None:
        import json

        w.write_result(
            {"from_version": "0.5.80", "to_version": "0.5.81"},
            True,
            base=tmp_path,
            daemon={"pids": [42], "status_rc": 0, "alive": True},
            watchdog_restored=True,
        )
        data = json.loads((tmp_path / "_upgrade_result.json").read_text("utf-8"))
        assert data["daemon_alive"] is True and data["daemon_pids"] == [42]
        assert data["watchdog_restored"] is True


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
        monkeypatch.setattr(w, "find_daemon_pids", lambda: [321])
        monkeypatch.setattr(w, "tool_venv_python", lambda: None)

        assert w.ensure_daemon_back("skillery.exe")["alive"] is True
        assert captured["kw"]["creationflags"] & w.CREATE_NO_WINDOW

    def test_daemon_start_retries_broken_trampoline(self, monkeypatch) -> None:
        """Сразу после апгрейда trampoline транзиентно битый — повторяем."""
        rcs = iter([1, 1, 0, 0, 0, 0])  # первые два раза бинарь ещё не готов
        seen: list = []
        monkeypatch.setattr(w.time, "sleep", lambda s: None)
        monkeypatch.setattr(w, "tool_venv_python", lambda: None)

        def _run(cmd, **k):
            rc = next(rcs)
            seen.append(rc)
            return rc

        monkeypatch.setattr(w, "_run_quiet", _run)
        # Демон считается поднятым только когда команда старта наконец дала 0.
        monkeypatch.setattr(w, "find_daemon_pids", lambda: [55] if 0 in seen else [])

        assert w.ensure_daemon_back("skillery.exe")["alive"] is True

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
