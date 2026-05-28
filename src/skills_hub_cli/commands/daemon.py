"""``skills-hub daemon`` — управление event-tracking daemon'ом (E23).

- ``daemon run`` — forground цикл (для systemd/launchd).
- ``daemon start`` — detached background (POSIX fork / Windows DETACHED).
- ``daemon stop`` — SIGTERM по PID.
- ``daemon status`` — alive + last_cycle + queue size.
- ``daemon install [--platform macos|linux|windows]`` — генерация
  autostart-файлов.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from skills_hub_cli.commands import _common
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.daemon.autostart import (
    detect_platform,
    install_for_platform,
)
from skills_hub_cli.daemon.daemon_runner import (
    DaemonRunner,
    default_pid_path,
    default_queue_path,
    default_state_path,
    is_process_alive,
    kill_process,
    read_running_pid,
    read_state,
)
from skills_hub_cli.daemon.event_collector import EventCollector
from skills_hub_cli.daemon.event_sender import EventSender
from skills_hub_cli.output import emit_data, emit_error, emit_message

console = Console()


def _build_runner(*, interval_seconds: float) -> DaemonRunner:
    """Создать ``DaemonRunner`` с реальной очередью + sender'ом."""
    cfg = ClientConfig.load()
    access_holder: dict[str, str | None] = {"token": None}

    def _factory():  # type: ignore[no-untyped-def]
        # Lazy: каждый цикл подтягиваем актуальный token из keyring (refresh
        # callback может его обновить).
        access = access_holder["token"]
        if not access and cfg.user_email:
            from skills_hub_cli.config import load_tokens

            access, _ = load_tokens(cfg.user_email)
            access_holder["token"] = access
        return _common.make_client(cfg, access or "")

    collector = EventCollector(default_queue_path())
    sender = EventSender(collector, _factory)
    return DaemonRunner(sender, interval_seconds=interval_seconds)


def cmd_daemon_run(
    interval: float = typer.Option(
        60.0, "--interval", min=1.0, max=3600.0, help="Секунд между циклами"
    ),
) -> None:
    """Foreground-цикл daemon'а (вызывается из systemd / launchd / schtasks).

    Блокирует процесс пока не получит SIGTERM. Для start/stop — см. соседние
    команды.
    """
    runner = _build_runner(interval_seconds=interval)
    try:
        asyncio.run(runner.run_forever())
    except KeyboardInterrupt:
        emit_message("daemon stopped (KeyboardInterrupt)", level="warn")


def _spawn_detached_daemon(interval: float) -> int:
    """Запустить ``skills-hub daemon run`` в detached background.

    Returns PID нового процесса. Реальный fork для POSIX, DETACHED_PROCESS
    для Windows.
    """
    import shutil

    binary = shutil.which("skills-hub") or sys.executable
    args = [binary, "daemon", "run", "--interval", str(interval)]
    if binary == sys.executable:
        # Если skills-hub не на PATH (dev-окружение) — запускаем модуль
        args = [sys.executable, "-m", "skills_hub_cli", "daemon", "run", "--interval", str(interval)]
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        proc = subprocess.Popen(  # noqa: S603
            args,
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return proc.pid
    # POSIX: double fork + setsid
    pid = os.fork()
    if pid > 0:
        # Родитель ждёт первый fork
        os.waitpid(pid, 0)
        # Реальный PID daemon'а узнаем через PID-файл (daemon сам его записал).
        return _wait_pid_file()
    # Child
    os.setsid()
    pid2 = os.fork()
    if pid2 > 0:
        os._exit(0)
    # Grand-child: redirect stdio
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        try:
            os.dup2(devnull, fd)
        except OSError:
            pass
    os.execvp(args[0], args)


def _wait_pid_file(timeout: float = 5.0) -> int:
    """Подождать пока daemon запишет PID-файл (max timeout секунд)."""
    import time

    start = time.time()
    while time.time() - start < timeout:
        pid = read_running_pid()
        if pid is not None and is_process_alive(pid):
            return pid
        time.sleep(0.1)
    return -1


def cmd_daemon_start(
    interval: float = typer.Option(
        60.0, "--interval", min=1.0, max=3600.0, help="Секунд между циклами"
    ),
) -> None:
    """Запустить daemon в background."""
    existing = read_running_pid()
    if existing is not None and is_process_alive(existing):
        emit_data(
            {
                "event": "already_running",
                "pid": existing,
            },
            text_renderer=lambda p: console.print(
                f"[yellow]Daemon уже запущен (pid={p['pid']})[/]"
            ),
        )
        return
    pid = _spawn_detached_daemon(interval)
    if pid <= 0:
        emit_error(
            "DAEMON_SPAWN_FAILED",
            "Не удалось запустить daemon — PID-файл не появился",
        )
        raise typer.Exit(1)
    emit_data(
        {"event": "daemon_started", "pid": pid, "interval": interval},
        text_renderer=lambda p: console.print(
            f"[green]✓[/] Daemon запущен pid={p['pid']} interval={p['interval']}s"
        ),
    )


def cmd_daemon_stop() -> None:
    """Послать SIGTERM daemon'у по PID-файлу."""
    pid = read_running_pid()
    if pid is None:
        emit_data(
            {"event": "not_running"},
            text_renderer=lambda _: console.print(
                "[yellow]Daemon не запущен (PID-файл отсутствует)[/]"
            ),
        )
        return
    if not is_process_alive(pid):
        emit_data(
            {"event": "stale_pid", "pid": pid},
            text_renderer=lambda p: console.print(
                f"[yellow]PID-файл stale (pid={p['pid']} мёртв)[/]"
            ),
        )
        # Очистим PID-файл
        from contextlib import suppress

        with suppress(OSError):
            default_pid_path().unlink()
        return
    ok = kill_process(pid)
    emit_data(
        {"event": "daemon_stopped" if ok else "kill_failed", "pid": pid},
        text_renderer=lambda p: console.print(
            f"[green]✓[/] SIGTERM → pid={p['pid']}"
            if p["event"] == "daemon_stopped"
            else f"[red]Не удалось убить pid={p['pid']}[/]"
        ),
    )


def cmd_daemon_status() -> None:
    """Status: alive + last_cycle + queue size."""
    pid = read_running_pid()
    alive = pid is not None and is_process_alive(pid)
    state = read_state()
    queue = EventCollector(default_queue_path())
    payload: dict[str, Any] = {
        "alive": alive,
        "pid": pid,
        "pid_path": str(default_pid_path()),
        "queue_path": str(queue.path),
        "queue_size": queue.size(),
        "state": state,
    }

    def _render(p: dict[str, Any]) -> None:
        status = "[green]running[/]" if p["alive"] else "[yellow]stopped[/]"
        console.print(f"Daemon:      {status}  pid={p['pid'] or '—'}")
        console.print(f"PID file:    {p['pid_path']}")
        console.print(f"Queue:       {p['queue_path']}  size={p['queue_size']}")
        st = p["state"] or {}
        if st:
            console.print(f"Cycles:      {st.get('cycles', 0)}")
            console.print(f"Last cycle:  {st.get('last_cycle_at') or '—'}")
            console.print(f"Total sent:  {st.get('total_sent', 0)}")
            console.print(
                f"Accepted:    {st.get('total_accepted', 0)}"
            )
            if st.get("last_error"):
                console.print(
                    f"[yellow]Last error: {st['last_error']}[/]"
                )

    emit_data(payload, text_renderer=_render)


def cmd_daemon_install(
    platform: str | None = typer.Option(
        None,
        "--platform",
        help="macos | linux | windows (default: автодетект)",
    ),
    home: Path | None = typer.Option(
        None,
        "--home",
        help="Override $HOME для теста (default: реальный $HOME)",
    ),
    binary: str | None = typer.Option(
        None,
        "--binary",
        help="Полный путь до skills-hub binary (default: shutil.which)",
    ),
) -> None:
    """Сгенерировать autostart unit-файл + распечатать инструкцию.

    НЕ вызывает sudo и не модифицирует system-wide settings. Файл пишется
    в user-scope (``~/Library/LaunchAgents`` / ``~/.config/systemd/user``
    / ``~/.skills-hub/tasks``); финальная активация (``launchctl load`` /
    ``systemctl enable`` / ``schtasks /Create``) — задача пользователя.
    """
    if platform not in (None, "macos", "linux", "windows"):
        emit_error("VALIDATION", "platform: macos | linux | windows")
        raise typer.Exit(1)
    home_dir = home or Path.home()
    artifact = install_for_platform(
        platform, home_dir=home_dir, binary=binary
    )
    payload = {
        "event": "autostart_installed",
        "platform": artifact.platform,
        "unit_path": str(artifact.unit_path),
        "instructions": artifact.instructions,
    }

    def _render(p: dict[str, Any]) -> None:
        console.print(
            f"[green]✓[/] Autostart {p['platform']} → {p['unit_path']}"
        )
        console.print("[bold]Что делать дальше:[/]")
        for line in p["instructions"]:
            console.print(f"  {line}")

    emit_data(payload, text_renderer=_render)


def register(app: typer.Typer) -> None:
    """Register ``daemon`` sub-app."""
    d_app = typer.Typer(
        no_args_is_help=True,
        help="Event-tracking daemon (E23)",
    )
    d_app.command("run")(cmd_daemon_run)
    d_app.command("start")(cmd_daemon_start)
    d_app.command("stop")(cmd_daemon_stop)
    d_app.command("status")(cmd_daemon_status)
    d_app.command("install")(cmd_daemon_install)
    app.add_typer(d_app, name="daemon")


# Detect platform export — для CLI вне sub-app (если кто-то хочет проверить).
__all__ = ["register", "detect_platform"]
