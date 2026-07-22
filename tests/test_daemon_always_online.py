"""#1057: «устройство всегда в сети» — надёжный автозапуск демона + watchdog.

Три корневые причины, найденные live-диагностикой, и их регрессы:

1. `ensure_autostart` делал ранний ``return fallback`` при Startup-fallback →
   пропускал ``install_watchdog``. На schtasks-denied машинах (logon/boot-
   триггеры требуют админа) периодического воскрешения демона не было вовсе,
   и «всегда в сети» не выполнялось. Watchdog (time-триггер ``/SC MINUTE``)
   админа НЕ требует и обязан ставиться ВСЕГДА.
2. Не все login-пути поднимали демон: code-login и password-login
   регистрировали устройство, но демон не запускали.
3. Живой демон не подстраховывал watchdog — если login-time установка не
   удалась, периодической страховки не появлялось до следующего входа.
"""
from __future__ import annotations

import inspect

import pytest

from skillery_cli import __main__ as m


class TestWatchdogAlwaysInstalled:
    """Bug #1: watchdog ставится ДАЖЕ когда logon-автозапуск ушёл в fallback."""

    def _win_env(self, tmp_path, monkeypatch):
        from skillery_cli.daemon import autostart

        monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
        monkeypatch.setattr(autostart, "detect_platform", lambda: "windows")
        monkeypatch.setattr(autostart, "_resolve_cli_binary", lambda: "skillery")
        return autostart

    def test_watchdog_installed_on_startup_fallback(self, tmp_path, monkeypatch):
        """schtasks отказал → Startup-fallback, но watchdog всё равно поставлен."""
        autostart = self._win_env(tmp_path, monkeypatch)
        # logon-таск отказал («Access is denied») → уходим в Startup-fallback.
        monkeypatch.setattr(
            autostart, "activate_autostart",
            lambda artifact: {"activated": False, "error": "Access is denied."},
        )
        wd_calls: list = []
        monkeypatch.setattr(
            autostart, "install_watchdog",
            lambda **kw: wd_calls.append(kw) or {"installed": True, "task": "W"},
        )

        result = autostart.ensure_autostart(home_dir=tmp_path)

        assert result["activated"] is True  # fallback сработал (вход покрыт)
        assert "startup:" in str(result["command"])
        assert wd_calls, "watchdog обязан ставиться и на fallback-пути (был баг)"
        assert result["watchdog"]["installed"] is True

    def test_watchdog_installed_on_normal_activation(self, tmp_path, monkeypatch):
        """И на успешном logon-пути watchdog тоже ставится (двойная гарантия)."""
        autostart = self._win_env(tmp_path, monkeypatch)
        monkeypatch.setattr(
            autostart, "activate_autostart",
            lambda artifact: {"activated": True, "command": "schtasks", "error": None},
        )
        wd_calls: list = []
        monkeypatch.setattr(
            autostart, "install_watchdog",
            lambda **kw: wd_calls.append(kw) or {"installed": True},
        )

        result = autostart.ensure_autostart(home_dir=tmp_path)

        assert wd_calls, "watchdog обязан ставиться и на успешном logon-пути"
        assert result["watchdog"]["installed"] is True

    def test_watchdog_default_interval_is_tight(self):
        """«Всегда в сети»: интервал воскрешения ≤ 3 мин (был 10 — слишком долго)."""
        from skillery_cli.daemon import autostart

        default = inspect.signature(
            autostart.install_watchdog
        ).parameters["interval_min"].default
        assert default <= 3, "после краха демон должен воскресать быстро"


class TestAllLoginPathsStartDaemon:
    """#954 расширение: демон поднимается на ЛЮБОМ login-пути."""

    @pytest.mark.parametrize(
        "func_name",
        ["_do_code_login", "_do_password_login", "_do_browser_login"],
    )
    def test_login_path_ensures_daemon(self, func_name):
        """code/password/browser login — каждый обязан поднять демон после входа."""
        src = inspect.getsource(getattr(m, func_name))
        assert "_ensure_daemon_after_login" in src, (
            f"{func_name}: устройство останется офлайн, задания зависнут в очереди"
        )


class TestDaemonSelfInstallsWatchdog:
    """Живой демон при старте сам (пере)регистрирует свой watchdog-таск."""

    def test_daemon_run_installs_watchdog(self):
        from skillery_cli.commands import daemon as daemon_cmd

        src = inspect.getsource(daemon_cmd.cmd_daemon_run)
        assert "install_watchdog" in src, (
            "демон обязан подстраховывать watchdog даже если login-time установка "
            "не удалась"
        )


class TestFailuresAreLoggedNotSilent:
    """Пользователь: «только если что-то не так — писать в логи ошибки»."""

    def test_heal_failure_is_logged(self, monkeypatch):
        """Провал самолечения пишется в лог (тихо для команды, но не молча)."""
        monkeypatch.setattr(m.sys, "argv", ["skillery", "skill", "list"])

        class _Cfg:
            @staticmethod
            def load():  # type: ignore[no-untyped-def]
                class _C:
                    @staticmethod
                    def is_logged_in() -> bool:
                        return True

                return _C()

        monkeypatch.setattr("skillery_cli.config.ClientConfig", _Cfg)
        # Демон «не поднялся» — event=failed.
        monkeypatch.setattr(
            "skillery_cli.commands.daemon.ensure_daemon_running",
            lambda: {"event": "failed", "pid": None, "error": "нет прав"},
        )
        logged: list[str] = []
        monkeypatch.setattr(m, "_log_daemon_issue", lambda msg: logged.append(msg))

        m._heal_daemon_if_dead()

        assert logged, "провал самолечения должен попасть в лог"
        assert "failed" in logged[0]

    def test_heal_success_is_not_logged(self, monkeypatch):
        """Успешный heal (already_running/started) — без шума в логе."""
        monkeypatch.setattr(m.sys, "argv", ["skillery", "skill", "list"])

        class _Cfg:
            @staticmethod
            def load():  # type: ignore[no-untyped-def]
                class _C:
                    @staticmethod
                    def is_logged_in() -> bool:
                        return True

                return _C()

        monkeypatch.setattr("skillery_cli.config.ClientConfig", _Cfg)
        monkeypatch.setattr(
            "skillery_cli.commands.daemon.ensure_daemon_running",
            lambda: {"event": "already_running", "pid": 42},
        )
        logged: list[str] = []
        monkeypatch.setattr(m, "_log_daemon_issue", lambda msg: logged.append(msg))

        m._heal_daemon_if_dead()

        assert logged == [], "здоровый heal не должен сыпать в лог"
