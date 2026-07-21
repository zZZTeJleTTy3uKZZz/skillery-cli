"""#954: после входа демон поднимается сам.

Раньше `skillery login` только регистрировал устройство. Демона никто не
запускал, поэтому задания из веба висели в очереди («устанавливается»
бесконечно), а устройство в вебе выглядело офлайн — признак «на связи» даёт
именно опрос очереди демоном. Пользователь видел «CLI подключён», но ничего
не происходило.
"""
from __future__ import annotations

import pytest

from skillery_cli import __main__ as m
from skillery_cli.commands import daemon as daemon_cmd


class TestEnsureDaemonRunning:
    def test_starts_daemon_when_not_running(self, monkeypatch) -> None:
        monkeypatch.setattr(daemon_cmd, "read_running_pid", lambda: None)
        monkeypatch.setattr(daemon_cmd, "is_process_alive", lambda pid: False)
        monkeypatch.setattr(daemon_cmd, "_spawn_detached_daemon", lambda interval: 4242)

        state = daemon_cmd.ensure_daemon_running()

        assert state == {"event": "started", "pid": 4242}

    def test_idempotent_when_already_running(self, monkeypatch) -> None:
        """Повторный вход не плодит вторую копию демона."""
        spawned: list[float] = []
        monkeypatch.setattr(daemon_cmd, "read_running_pid", lambda: 777)
        monkeypatch.setattr(daemon_cmd, "is_process_alive", lambda pid: True)
        monkeypatch.setattr(
            daemon_cmd, "_spawn_detached_daemon", lambda i: spawned.append(i) or 1
        )

        state = daemon_cmd.ensure_daemon_running()

        assert state == {"event": "already_running", "pid": 777}
        assert spawned == [], "демон уже работал — второй запускать нельзя"

    def test_stale_pid_file_leads_to_restart(self, monkeypatch) -> None:
        """PID-файл остался от мёртвого процесса → поднимаем заново."""
        monkeypatch.setattr(daemon_cmd, "read_running_pid", lambda: 999)
        monkeypatch.setattr(daemon_cmd, "is_process_alive", lambda pid: False)
        monkeypatch.setattr(daemon_cmd, "_spawn_detached_daemon", lambda interval: 5150)

        assert daemon_cmd.ensure_daemon_running()["pid"] == 5150

    def test_spawn_failure_never_breaks_login(self, monkeypatch) -> None:
        def _boom(interval):  # type: ignore[no-untyped-def]
            raise RuntimeError("нет прав на fork")

        monkeypatch.setattr(daemon_cmd, "read_running_pid", lambda: None)
        monkeypatch.setattr(daemon_cmd, "_spawn_detached_daemon", _boom)

        state = daemon_cmd.ensure_daemon_running()
        assert state["event"] == "failed"


class TestLoginHint:
    @pytest.mark.parametrize(
        ("state", "must_contain"),
        [
            ({"event": "started", "pid": 12}, "запущен"),
            ({"event": "already_running", "pid": 12}, "уже работает"),
            ({"event": "failed", "pid": None}, "daemon start"),
        ],
    )
    def test_user_is_told_what_happened(
        self, state: dict, must_contain: str, capsys
    ) -> None:
        m._print_daemon_hint(state)
        out = capsys.readouterr().out
        assert must_contain in out

    def test_failure_hint_explains_consequence(self, capsys) -> None:
        """Не запустился — пользователь должен понять, ЧЕМ это грозит."""
        m._print_daemon_hint({"event": "failed", "pid": None})
        out = capsys.readouterr().out
        assert "задания из веба" in out


class TestAutostartActivation:
    """#955: автозапуск теперь ВКЛЮЧАЕТСЯ, а не только генерируется файлом."""

    def test_activation_command_per_platform(self) -> None:
        from pathlib import Path

        from skillery_cli.daemon.autostart import _activation_command

        unit = Path("/tmp/unit")
        assert _activation_command("macos", unit)[:2] == ["launchctl", "load"]
        assert _activation_command("linux", unit)[:3] == [
            "systemctl", "--user", "enable",
        ]
        assert _activation_command("windows", unit)[:2] == ["schtasks", "/Create"]
        assert _activation_command("plan9", unit) is None

    def test_activation_uses_user_scope_only(self) -> None:
        """Ни sudo, ни system-wide: автозапуск не трогает систему целиком."""
        from pathlib import Path

        from skillery_cli.daemon.autostart import _activation_command

        for platform in ("macos", "linux", "windows"):
            cmd = _activation_command(platform, Path("/tmp/unit"))
            assert cmd is not None
            assert "sudo" not in cmd
            assert "--system" not in cmd

    def test_missing_tool_reported_not_raised(self, monkeypatch) -> None:
        from pathlib import Path

        from skillery_cli.daemon import autostart

        artifact = autostart.AutostartArtifact(
            platform="linux",
            unit_path=Path("/tmp/unit"),
            content="",
            instructions=[],
        )
        monkeypatch.setattr(autostart.shutil, "which", lambda name: None)

        result = autostart.activate_autostart(artifact)
        assert result["activated"] is False
        assert "systemctl" in str(result["error"])

    def test_failure_never_raises(self, monkeypatch) -> None:
        from pathlib import Path

        from skillery_cli.daemon import autostart

        artifact = autostart.AutostartArtifact(
            platform="windows",
            unit_path=Path("/tmp/unit.xml"),
            content="",
            instructions=[],
        )
        monkeypatch.setattr(autostart.shutil, "which", lambda name: "schtasks")

        def _boom(*a, **k):  # type: ignore[no-untyped-def]
            raise OSError("нет прав")

        monkeypatch.setattr(autostart.subprocess, "run", _boom)
        result = autostart.activate_autostart(artifact)
        assert result["activated"] is False


class TestAdaptivePollRhythm:
    """#956: ритм опроса адаптивный — быстро при работе, экономно в простое."""

    def test_idle_is_slower_than_busy(self) -> None:
        from skillery_cli.commands.daemon import (
            _BUSY_POLL_SECONDS,
            _IDLE_POLL_SECONDS,
        )

        assert _BUSY_POLL_SECONDS < _IDLE_POLL_SECONDS

    def test_idle_rhythm_halves_previous_load(self) -> None:
        """Раньше опрос шёл каждые 60с независимо ни от чего."""
        from skillery_cli.commands.daemon import _IDLE_POLL_SECONDS

        assert _IDLE_POLL_SECONDS >= 120.0

    def test_online_window_covers_missed_tick(self) -> None:
        """Окно «онлайн» должно прощать пропуск одного тика (иначе мигание)."""
        from skillery_cli.commands.daemon import _IDLE_POLL_SECONDS

        online_window_seconds = 5 * 60  # backend _DEVICE_ONLINE_WINDOW
        assert online_window_seconds >= 2 * _IDLE_POLL_SECONDS


class TestDaemonSelfHealing:
    """Демон умер посреди сессии → следующая команда CLI его поднимает."""

    def test_heal_skips_daemon_commands(self, monkeypatch) -> None:
        """`daemon stop` не должен тут же воскрешать демона."""
        called: list[int] = []
        monkeypatch.setattr(m.sys, "argv", ["skillery", "daemon", "stop"])
        monkeypatch.setattr(
            "skillery_cli.commands.daemon.ensure_daemon_running",
            lambda: called.append(1),
        )
        m._heal_daemon_if_dead()
        assert called == []

    def test_heal_skips_when_not_logged_in(self, monkeypatch) -> None:
        called: list[int] = []
        monkeypatch.setattr(m.sys, "argv", ["skillery", "skill", "list"])
        monkeypatch.setattr(
            "skillery_cli.commands.daemon.ensure_daemon_running",
            lambda: called.append(1),
        )

        class _Cfg:
            @staticmethod
            def load():  # type: ignore[no-untyped-def]
                class _C:
                    @staticmethod
                    def is_logged_in() -> bool:
                        return False

                return _C()

        monkeypatch.setattr("skillery_cli.config.ClientConfig", _Cfg)
        m._heal_daemon_if_dead()
        assert called == []

    def test_heal_is_silent_on_error(self, monkeypatch) -> None:
        monkeypatch.setattr(m.sys, "argv", ["skillery", "skill", "list"])
        monkeypatch.setattr(
            "skillery_cli.config.ClientConfig",
            property(lambda self: (_ for _ in ()).throw(RuntimeError("boom"))),
        )
        m._heal_daemon_if_dead()  # не бросает
