"""#996: единственность демона обеспечивает ЯДРО, а не PID-файл.

Пользователь видел, как «закрываешь одно окно — открываются два»: на машине
накопилось СЕМЬ процессов демона. Причина — единственность держалась на
PID-файле: между «прочитали PID, он мёртв» и «запустили» есть окно гонки, а
источников старта несколько (автозапуск, самолечение при команде, ручной
`daemon start`). PID-файл вдобавок врёт после жёсткого kill'а.
"""
from __future__ import annotations

import os
import sys

import pytest

from skillery_cli.daemon import single_instance as si


@pytest.fixture
def iso_lock(tmp_path):
    """Изолированный лок: отдельное имя мьютекса + отдельный файл.

    Без этого тест зависел бы от того, крутится ли на машине настоящий демон —
    он держит ГЛОБАЛЬНЫЙ мьютекс, и первый же `acquire` в тесте возвращал бы
    `acquired=False`. Уникальное имя убирает эту связь с системой.
    """
    name = f"Local\\SkilleryTest_{os.getpid()}"
    path = tmp_path / "daemon.lock"
    return {"mutex_name": name, "lock_path": path}


class TestKernelLock:
    def test_second_acquire_fails_while_first_holds(self, iso_lock) -> None:
        first = si.acquire_daemon_lock(**iso_lock)
        assert first.acquired is True
        try:
            second = si.acquire_daemon_lock(**iso_lock)
            assert second.acquired is False, "два демона одновременно недопустимы"
        finally:
            first.release()

    def test_lock_is_reusable_after_release(self, iso_lock) -> None:
        """Освободился — снова берётся: «залипшего» состояния не остаётся."""
        first = si.acquire_daemon_lock(**iso_lock)
        first.release()
        second = si.acquire_daemon_lock(**iso_lock)
        assert second.acquired is True
        second.release()

    def test_release_is_idempotent(self, iso_lock) -> None:
        lock = si.acquire_daemon_lock(**iso_lock)
        lock.release()
        lock.release()  # не бросает
        assert lock.acquired is False


class TestProcessDiscoverySafety:
    """Зачистка не должна задевать ЧУЖИЕ процессы."""

    @pytest.mark.skipif(sys.platform != "win32", reason="windows-специфика")
    def test_windows_filter_requires_our_binary_and_args(self) -> None:
        import inspect

        src = inspect.getsource(si._windows_daemon_pids)
        # Матч только по «daemon run» ловил посторонние процессы (например,
        # шелл, в аргументах которого встретились эти слова) — и мы бы их убили.
        assert "skillery.exe" in src
        assert "'*skillery*'" in src

    @pytest.mark.skipif(sys.platform == "win32", reason="posix-специфика")
    def test_posix_filter_is_narrow(self) -> None:
        import inspect

        src = inspect.getsource(si._posix_daemon_pids)
        assert "skillery.*daemon run" in src

    def test_finder_never_returns_own_pid(self, monkeypatch) -> None:
        """Себя в список на убийство не добавляем."""
        import os

        # librarykit.proc.run отдаёт text=True → stdout строкой, не bytes.
        class _R:
            stdout = f"{os.getpid()}\n999999\n"

        monkeypatch.setattr(si, "proc_run", lambda *a, **k: _R())
        pids = si.find_daemon_pids()
        assert os.getpid() not in pids

    def test_finder_survives_missing_tooling(self, monkeypatch) -> None:
        def _boom(*a, **k):  # type: ignore[no-untyped-def]
            raise FileNotFoundError("нет powershell/pgrep")

        monkeypatch.setattr(si, "proc_run", _boom)
        assert si.find_daemon_pids() == []


class TestEnsureRespectsLock:
    def test_does_not_spawn_when_lock_held(self, monkeypatch) -> None:
        """Главный сценарий: лок занят → второй процесс НЕ создаётся."""
        from skillery_cli.commands import daemon as daemon_cmd

        spawned: list[float] = []
        monkeypatch.setattr(daemon_cmd, "is_daemon_locked", lambda: True)
        monkeypatch.setattr(daemon_cmd, "read_running_pid", lambda: 4242)
        monkeypatch.setattr(
            daemon_cmd, "_spawn_detached_daemon", lambda i: spawned.append(i) or 1
        )

        state = daemon_cmd.ensure_daemon_running()

        assert state["event"] == "already_running"
        assert spawned == [], "демон уже работает — вторая копия недопустима"

    def test_waits_until_child_takes_lock(self, monkeypatch) -> None:
        """После спавна ждём занятия лока — иначе параллельный вызов плодит копию."""
        from skillery_cli.commands import daemon as daemon_cmd

        states = iter([False, False, True])
        monkeypatch.setattr(daemon_cmd, "read_running_pid", lambda: None)
        monkeypatch.setattr(daemon_cmd, "is_process_alive", lambda pid: False)
        monkeypatch.setattr(daemon_cmd, "_spawn_detached_daemon", lambda i: 777)
        monkeypatch.setattr(daemon_cmd.time, "sleep", lambda s: None)
        monkeypatch.setattr(
            daemon_cmd, "is_daemon_locked", lambda: next(states, True)
        )

        state = daemon_cmd.ensure_daemon_running()
        assert state == {"event": "started", "pid": 777}
