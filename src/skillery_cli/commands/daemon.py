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
import time
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.daemon.single_instance import (
    acquire_daemon_lock,
    find_daemon_pids,
    is_daemon_locked,
)
from skillery_cli.daemon.autostart import (
    detect_platform,
    install_for_platform,
    install_watchdog,
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

# LONG-POLL: сколько сек держим запрос очереди открытым (сервер отвечает раньше,
# если появилось задание). < 30 (потолок сервера) и < idle-таймаута прокси.
_LONGPOLL_WAIT_SEC = 25
# Межцикловый sleep при long-poll — малый: переподключаемся сразу после ответа
# (ритм ведёт сам 25-сек висящий коннект, а не этот интервал).
_LONGPOLL_LOOP_INTERVAL = 2.0
# Тяжёлые reconcile (device-sync набора + auto-update до latest) — не каждый
# long-poll-цикл, а раз в ~3 мин (это фон, не мгновенная доставка задания).
_HEAVY_RECONCILE_SEC = 180.0

# #1102: каденс-fallback авто-апгрейда CLI — если push-задача cli_upgrade не
# прилетела, живой демон сам сверяется с PyPI. При СТАРТЕ — принудительно (раз),
# далее периодически раз в час (сам PyPI-запрос гейтит суточный cooldown в
# `_check_cli_update_detailed`, поэтому реальный опрос — максимум раз в сутки).
_SELF_UPGRADE_CHECK_SEC = 3600.0


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
    loop_interval = interval_seconds
    if reconcile_installs and cfg.is_logged_in():
        # LONG-POLL: очередь устройства висит на сервере до _LONGPOLL_WAIT_SEC,
        # задание доставляется МГНОВЕННО, а сам висящий коннект = heartbeat.
        # Тяжёлые reconcile (device-sync НАБОРА + auto-update до latest) — реже
        # (_HEAVY_RECONCILE_SEC): они не про мгновенную доставку.
        last_heavy: dict[str, float] = {"at": 0.0}
        # #1102: каденс self-upgrade CLI — старт (force, один раз) + раз в час.
        self_upgrade: dict[str, float | bool] = {"at": 0.0, "started": False}

        async def _reconcile() -> None:
            access = _current_access()
            if not access:
                return
            # Lazy-import: избегаем циклической зависимости __main__ ↔ daemon.
            import skillery_cli.__main__ as main_mod
            from skillery_cli.core.agents import get_target

            target = get_target(cfg.agent)
            # 0) #905: АДРЕСНАЯ очередь этого устройства — LONG-POLL (висим до
            #    ~25с). Применяем и РАПОРТУЕМ факт (skill-очередь + #1102
            #    device_tasks: cli_upgrade и пр.). Висящий коннект держит
            #    last_seen свежим (устройство почти всегда «на связи»).
            await main_mod._reconcile_device_queue(
                cfg, access, channel="published", agent_target=target,
                wait=_LONGPOLL_WAIT_SEC,
            )
            # 1-2) device-sync НАБОРА + auto-update до latest — РЕЖЕ (не каждый
            #    long-poll-цикл; это фон, не мгновенная доставка).
            now = _time.monotonic()
            if now - last_heavy["at"] >= _HEAVY_RECONCILE_SEC:
                last_heavy["at"] = now
                await main_mod._reconcile_hub_installs(
                    cfg, access, channel="published", agent_target=target,
                    initiator="web-queue",
                )
                await main_mod._auto_update_hub_installs(
                    cfg, access, agent_target=target, channel="published",
                )
            # 3) #1102 каденс-fallback авто-апгрейда CLI: при СТАРТЕ демона —
            #    принудительно (конвергируем на известный latest), далее раз в час
            #    (сам PyPI-запрос гейтит суточный cooldown). Push через device_tasks
            #    остаётся основным каналом; это страховка «демон жив, а не прилетело».
            if not self_upgrade["started"]:
                self_upgrade["started"] = True
                self_upgrade["at"] = now
                await main_mod._daemon_cli_self_upgrade(cfg, force=True)
            elif now - float(self_upgrade["at"]) >= _SELF_UPGRADE_CHECK_SEC:
                self_upgrade["at"] = now
                await main_mod._daemon_cli_self_upgrade(cfg, force=False)

        reconcile = _reconcile
        # Ритм ведёт long-poll (25с блок); межцикловый sleep малый, чтобы
        # переподключаться сразу после ответа. ⚠️СТАРЫЙ backend без ?wait
        # вернёт очередь мгновенно → этот tight-loop опрашивал бы часто; поэтому
        # backend с long-poll ДЕПЛОИТСЯ ПЕРВЫМ (он уже поддерживает ?wait).
        loop_interval = _LONGPOLL_LOOP_INTERVAL

    return DaemonRunner(
        sender, interval_seconds=loop_interval, reconcile=reconcile
    )


def cmd_daemon_run(
    interval: float = typer.Option(
        60.0, "--interval", min=1.0, max=3600.0, help="Секунд между циклами"
    ),
) -> None:
    """Foreground-цикл daemon'а (вызывается из systemd / launchd / schtasks).

    Блокирует процесс пока не получит SIGTERM. Для start/stop — см. соседние
    команды.

    Единственность обеспечивает ЯДЕРНЫЙ лок, а не PID-файл: сколько бы
    источников ни попыталось поднять демона одновременно (автозапуск,
    самолечение при команде, ручной `daemon start`), выживет ровно один. Раньше
    между «проверили PID» и «запустили» было окно гонки — демоны плодились.
    """
    lock = acquire_daemon_lock()
    if not lock.acquired:
        emit_data(
            {"event": "already_running"},
            text_renderer=lambda _: console.print(
                "[yellow]Демон уже запущен — второй экземпляр не нужен[/]"
            ),
        )
        return
    # Логи демона в ~/.skillery/logs/daemon.log (уровень из конфига).
    try:
        from skillery_cli.config import ClientConfig
        from skillery_cli.core.logging_setup import configure_logging, get_logger

        configure_logging(ClientConfig.load().log_level, filename="daemon.log")
        _log = get_logger("daemon")
        _log.info("daemon started", extra={"context": {"pid": os.getpid()}})
    except Exception:  # noqa: BLE001 — логи не критичны
        _log = None
    # Watchdog самоподдержка: демон при КАЖДОМ старте (пере)регистрирует свой
    # периодический watchdog-таск. Если login-time установка не сработала (или
    # машину не логинили через CLI после включения фичи), живой демон всё равно
    # поднимет страховку «всегда в сети». Idempotent (/F), best-effort, только
    # Windows (на launchd/systemd перезапуск встроен). При live-демоне watchdog
    # находит лок и выходит, так что повторной регистрации на каждый тик нет.
    if sys.platform == "win32":
        try:
            wd = install_watchdog()
            if _log and not wd.get("installed"):
                _log.error("watchdog install failed", extra={"context": wd})
        except Exception:  # noqa: BLE001 — страховка не критична
            pass
    try:
        runner = _build_runner(interval_seconds=interval)
        try:
            asyncio.run(runner.run_forever())
        except KeyboardInterrupt:
            emit_message("daemon stopped (KeyboardInterrupt)", level="warn")
            if _log:
                _log.warning("daemon stopped (KeyboardInterrupt)")
    finally:
        lock.release()


def _spawn_detached_daemon(interval: float) -> int:
    """Запустить ``skillery daemon run`` в detached background.

    Returns PID нового процесса. Реальный fork для POSIX, DETACHED_PROCESS
    для Windows.
    """
    import shutil

    from skillery_cli import _branding

    pkg = (__package__ or "").split(".")[0] or "skillery_cli"
    if sys.platform == "win32":
        # Запускаем интерпретатором напрямую (`pythonw -m skillery_cli`), а НЕ
        # через `skillery.exe`: exe — это uv-трамплин, он добавляет лишний
        # процесс в цепочку и требует наличия CLI на PATH. pythonw (без консоли)
        # рядом с sys.executable — если его нет, сам sys.executable + CREATE_NO_WINDOW.
        exe = Path(sys.executable)
        pythonw = exe.with_name("pythonw.exe")
        launcher = str(pythonw) if pythonw.exists() else sys.executable
        args = [launcher, "-m", pkg, "daemon", "run", "--interval", str(interval)]
    else:
        binary = shutil.which(_branding.APP_NAME) or sys.executable
        args = [binary, "daemon", "run", "--interval", str(interval)]
        if binary == sys.executable:
            # CLI не на PATH (dev-окружение) — запускаем пакет как модуль.
            args = [sys.executable, "-m", pkg, "daemon", "run", "--interval", str(interval)]
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        # CREATE_NO_WINDOW обязателен: без него консольный exe может получить
        # собственное окно (и пользователь видит болтающуюся вкладку терминала).
        # Демон — фоновый процесс, окна у него быть не должно.
        CREATE_NO_WINDOW = 0x08000000
        proc = subprocess.Popen(  # noqa: S603
            args,
            creationflags=(
                DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            ),
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
        # Источник правды — ядерный лок: PID-файл врёт после жёсткого kill'а и
        # при переиспользовании PID системой, а лок отпускается вместе с
        # процессом. Без этого самолечение плодило вторые копии.
        if is_daemon_locked():
            return {"event": "already_running", "pid": read_running_pid()}
        existing = read_running_pid()
        if existing is not None and is_process_alive(existing):
            return {"event": "already_running", "pid": existing}
        pid = _spawn_detached_daemon(interval)
        if pid > 0:
            # Ждём, пока дочерний процесс ВОЗЬМЁТ лок. Без этого остаётся окно
            # гонки: спавн асинхронный, лок берётся уже внутри демона, и
            # параллельный вызов успевал увидеть «свободно» и поднять вторую
            # копию. Теперь второй вызов дождётся занятого лока и не станет
            # плодить процесс.
            for _ in range(50):  # до ~5с
                if is_daemon_locked():
                    break
                time.sleep(0.1)
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
    if is_daemon_locked() or (existing is not None and is_process_alive(existing)):
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
    """Остановить демона — ВСЕ его экземпляры, а не только записанный в PID-файл.

    До появления лока демоны могли расплодиться (автозапуск + самолечение +
    ручной старт), и `stop` гасил лишь последнего: пользователь закрывал одно
    окно, а остальные продолжали работать. Поэтому здесь честная зачистка:
    PID-файл + поиск живых процессов демона по командной строке.
    """
    from contextlib import suppress

    targets: list[int] = []
    pid = read_running_pid()
    if pid is not None and is_process_alive(pid):
        targets.append(pid)
    for extra in find_daemon_pids():
        if extra not in targets:
            targets.append(extra)

    if not targets:
        with suppress(OSError):
            default_pid_path().unlink()
        emit_data(
            {"event": "not_running", "stopped": []},
            text_renderer=lambda _: console.print("[yellow]Демон не запущен[/]"),
        )
        return

    stopped = [p for p in targets if kill_process(p)]
    with suppress(OSError):
        default_pid_path().unlink()

    emit_data(
        {
            "event": "daemon_stopped" if stopped else "kill_failed",
            "stopped": stopped,
            "found": targets,
        },
        # «процессов», а не «экземпляров»: одна логическая копия демона на
        # Windows — это ЦЕПОЧКА процессов (launcher-трамплин uv → venv-редиректор
        # → базовый интерпретатор). Три процесса ≠ три демона; говорить
        # «экземпляров: 3» вводило в заблуждение.
        text_renderer=lambda payload: console.print(
            f"[green]✓[/] Демон остановлен (процессов снято: "
            f"{len(payload['stopped'])}; pid={', '.join(str(x) for x in payload['stopped'])})"
            if payload["stopped"]
            else f"[red]Не удалось остановить: {payload['found']}[/]"
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
