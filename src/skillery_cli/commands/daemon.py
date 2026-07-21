"""``skillery daemon`` — управление event-tracking daemon'ом.

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

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.daemon.autostart import (
    detect_platform,
    install_for_platform,
    uninstall_for_platform,
)
from skillery_cli.daemon.daemon_runner import (
    DaemonRunner,
    default_pid_path,
    default_queue_path,
    default_state_path,
    is_process_alive,
    kill_process,
    read_running_pid,
    read_state,
)
from skillery_cli.daemon.event_collector import EventCollector
from skillery_cli.daemon.event_sender import EventSender
from skillery_cli.output import emit_data, emit_error, emit_message

console = Console()


# Best-effort reconcile-инсталлов в демоне выполняется НЕ каждый event-цикл, а
# не чаще раза в N секунд — чтобы не дёргать /me/installs + git каждые 60с.
# #905: опрос очереди устройства должен быть отзывчивым — нажал в вебе и через
# ~минуту применилось. Раньше 300с: web-install демон замечал только через 5 мин.
_RECONCILE_MIN_INTERVAL_SECONDS = 60.0

# #956: ритм опроса очереди адаптивный — он же heartbeat устройства («на связи»
# в вебе даёт именно этот запрос). Пока есть работа, опрашиваем часто: нажатие
# «Установить» не должно ждать минуту. В простое разряжаем вдвое против прежних
# 60с — на парк из N машин это вдвое меньше запросов к хабу на ровном месте.
# Окно «онлайн» на бэкенде согласовано с _IDLE_POLL_SECONDS (2.5×).
_BUSY_POLL_SECONDS = 20.0
_IDLE_POLL_SECONDS = 120.0


def _build_runner(
    *, interval_seconds: float, reconcile_installs: bool = True
) -> DaemonRunner:
    """Создать ``DaemonRunner`` с реальной очередью + sender'ом.

    ``reconcile_installs`` — подключить best-effort reconcile («нажал Установить
    в вебе → демон скачал»). Дефолт on; выключается, если юзер не залогинен или
    флагом. Reconcile НЕ влияет на event-цикл (обёрнут в suppress в runner'е).
    """
    import time as _time

    cfg = ClientConfig.load()
    access_holder: dict[str, str | None] = {"token": None}

    def _current_access() -> str | None:
        access = access_holder["token"]
        if not access and cfg.user_email:
            from skillery_cli.config import load_tokens

            access, _ = load_tokens(cfg.user_email)
            access_holder["token"] = access
        return access

    def _factory(anonymous: bool = False):  # type: ignore[no-untyped-def]
        # Lazy: каждый цикл подтягиваем актуальный token из keyring (refresh
        # callback может его обновить).
        access = _current_access()
        if anonymous:
            # POST /events анонимен: при 401 (токен протух) sender ретраит
            # batch без Bearer. Деградировать некуда, если токена и не было.
            if not access:
                return None
            return _common.make_client(cfg, "")
        return _common.make_client(cfg, access or "")

    collector = EventCollector(default_queue_path())
    sender = EventSender(collector, _factory)

    reconcile = None
    if reconcile_installs and cfg.is_logged_in():
        last_run: dict[str, float] = {"at": 0.0}
        # Первый цикл — «быстрый»: после логина задания обычно уже ждут.
        poll_every: dict[str, float] = {"sec": _BUSY_POLL_SECONDS}

        async def _reconcile() -> None:
            now = _time.monotonic()
            if now - last_run["at"] < poll_every["sec"]:
                return
            last_run["at"] = now
            access = _current_access()
            if not access:
                return
            # Lazy-import: избегаем циклической зависимости __main__ ↔ daemon.
            from skillery_cli.__main__ import (
                _auto_update_hub_installs,
                _reconcile_device_queue,
                _reconcile_hub_installs,
            )
            from skillery_cli.core.agents import get_target

            target = get_target(cfg.agent)
            # 0) #905: АДРЕСНАЯ очередь этого устройства (веб выбрал устройства).
            #    Применяем и РАПОРТУЕМ факт — сервер узнаёт, что реально встало.
            #    Идёт первым: это явные задания пользователя.
            queue_report = await _reconcile_device_queue(
                cfg, access, channel="published", agent_target=target,
            )
            # Была работа → держим быстрый ритм (следующее задание применится
            # почти сразу). Тишина → разряжаем и не жжём бэкенд впустую.
            had_work = bool(
                (queue_report or {}).get("applied")
                or (queue_report or {}).get("failed")
            )
            poll_every["sec"] = (
                _BUSY_POLL_SECONDS if had_work else _IDLE_POLL_SECONDS
            )
            # 1) device-sync: «нажал Установить в вебе → демон скачал» (набор
            #    установленного между устройствами по installed_version).
            await _reconcile_hub_installs(
                cfg, access, channel="published", agent_target=target,
            )
            # 2) auto-update: поднять установленные хаб-навыки до latest published
            #    хаба (device-sync выше синхронизирует лишь НАБОР по записанной в
            #    вебе версии, а не до latest). Гейтится cfg.auto_update + cooldown.
            await _auto_update_hub_installs(
                cfg, access, agent_target=target, channel="published",
            )

        reconcile = _reconcile

    return DaemonRunner(
        sender, interval_seconds=interval_seconds, reconcile=reconcile
    )


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
    """Запустить ``skillery daemon run`` в detached background.

    Returns PID нового процесса. Реальный fork для POSIX, DETACHED_PROCESS
    для Windows.
    """
    import shutil

    from skillery_cli import _branding

    binary = shutil.which(_branding.APP_NAME) or sys.executable
    args = [binary, "daemon", "run", "--interval", str(interval)]
    if binary == sys.executable:
        # CLI не на PATH (dev-окружение) — запускаем пакет как модуль.
        pkg = (__package__ or "").split(".")[0]
        args = [sys.executable, "-m", pkg, "daemon", "run", "--interval", str(interval)]
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


def ensure_daemon_running(interval: float = 60.0) -> dict[str, object]:
    """Поднять демон, если он не работает. Идемпотентно, никогда не бросает.

    Зачем (#954): после `login` устройство регистрировалось, но демон никто не
    запускал. Веб исправно ставил задания в очередь, а применять их было
    некому: навык «устанавливается» бесконечно, а устройство выглядело
    офлайн — ведь признак «на связи» даёт именно опрос очереди демоном.

    Возвращает ``{"event": already_running|started|failed, "pid": …}``.
    """
    try:
        existing = read_running_pid()
        if existing is not None and is_process_alive(existing):
            return {"event": "already_running", "pid": existing}
        pid = _spawn_detached_daemon(interval)
        if pid > 0:
            return {"event": "started", "pid": pid}
        return {"event": "failed", "pid": None}
    except Exception as exc:  # демон — не повод валить login
        return {"event": "failed", "pid": None, "error": str(exc)}


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
    queue_size = queue.size()
    payload: dict[str, Any] = {
        "alive": alive,
        "pid": pid,
        "pid_path": str(default_pid_path()),
        "queue_path": str(queue.path),
        "queue_size": queue_size,
        "state": state,
    }
    # Демон мёртв, а в очереди копятся события → телеметрия не уходит.
    # Явное предупреждение + машинно-читаемое поле (для автоматики/CI).
    if not alive and queue_size > 0:
        payload["stalled_events"] = queue_size
        payload["warning"] = (
            f"демон не запущен, {queue_size} "
            f"событ{'ие' if queue_size == 1 else 'ий'} не "
            f"отправлен{'о' if queue_size == 1 else 'о'}: skillery daemon start"
        )

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
        if p.get("warning"):
            console.print(f"[yellow]⚠ {p['warning']}[/]")

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
        help="Полный путь до skillery binary (default: shutil.which)",
    ),
) -> None:
    """Сгенерировать autostart unit-файл + распечатать инструкцию.

    НЕ вызывает sudo и не модифицирует system-wide settings. Файл пишется
    в user-scope (``~/Library/LaunchAgents`` / ``~/.config/systemd/user``
    / ``~/.skillery/tasks``); финальная активация (``launchctl load`` /
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


def cmd_daemon_uninstall(
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
) -> None:
    """Снять autostart unit-файл (обратное к ``daemon install``).

    Удаляет тот же user-scope файл, что писал ``daemon install``
    (``~/Library/LaunchAgents`` / ``~/.config/systemd/user`` /
    ``~/.skillery/tasks``). НЕ вызывает sudo и не трогает system-wide
    settings. После удаления печатает инструкцию, как окончательно
    деактивировать autostart (``launchctl unload`` / ``systemctl --user
    disable`` / ``schtasks /Delete``). Идемпотентно: если файла нет —
    ``removed=False`` без ошибки.
    """
    if platform not in (None, "macos", "linux", "windows"):
        emit_error("VALIDATION", "platform: macos | linux | windows")
        raise typer.Exit(1)
    home_dir = home or Path.home()
    result = uninstall_for_platform(platform, home_dir=home_dir)
    payload = {
        "event": "autostart_uninstalled",
        "platform": result.platform,
        "unit_path": str(result.unit_path),
        "removed": result.removed,
        "instructions": result.instructions,
    }

    def _render(p: dict[str, Any]) -> None:
        if p["removed"]:
            console.print(
                f"[green]✓[/] Autostart {p['platform']} снят → {p['unit_path']}"
            )
        else:
            console.print(
                f"[yellow]Unit-файл не найден[/] ({p['platform']}): "
                f"{p['unit_path']} — нечего удалять"
            )
        console.print("[bold]Что делать дальше:[/]")
        for line in p["instructions"]:
            console.print(f"  {line}")

    emit_data(payload, text_renderer=_render)


def register(app: typer.Typer) -> None:
    """Register ``daemon`` sub-app."""
    d_app = typer.Typer(
        no_args_is_help=True,
        help="Event-tracking daemon",
    )
    d_app.command("run")(cmd_daemon_run)
    d_app.command("start")(cmd_daemon_start)
    d_app.command("stop")(cmd_daemon_stop)
    d_app.command("status")(cmd_daemon_status)
    d_app.command("install")(cmd_daemon_install)
    d_app.command("uninstall")(cmd_daemon_uninstall)
    app.add_typer(d_app, name="daemon")


# Detect platform export — для CLI вне sub-app (если кто-то хочет проверить).
__all__ = ["register", "detect_platform"]
