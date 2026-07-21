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
