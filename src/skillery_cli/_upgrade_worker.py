"""Автономный worker обновления CLI. Запускается ОТДЕЛЬНЫМ процессом.

Почему отдельный файл, а не строка в ``python -c`` и не обычный модуль:

* worker переживает ЗАМЕНУ пакета — во время апгрейда `skillery_cli` физически
  перезаписывается, поэтому worker не имеет права ничего из него импортировать;
* он копируется в ``~/.skillery/`` (вне tool-каталога): интерпретатор и скрипт
  внутри ``…/tools/skillery-cli/`` держали бы этот каталог, и `uv` не смог бы
  его удалить — ровно так апгрейд молча падал с «os error 5».

Отсюда же вынужденный дубль логики поиска процессов демона (в пакете она живёт
в ``daemon/single_instance.py``): импортировать её здесь нельзя по той же
причине. Дубль намеренный и локальный — держим его минимальным.

ПО ТОЙ ЖЕ ПРИЧИНЕ ЭТОТ МОДУЛЬ — ЕДИНСТВЕННОЕ ИСКЛЮЧЕНИЕ ИЗ ``librarykit.proc``
(#1144, см. allowlist в ``tests/test_no_raw_subprocess.py``). Запускает нас
БАЗОВЫЙ интерпретатор (``sys.base_prefix``, см. ``_upgrade_launcher`` в
``__main__``), а не python tool-venv — ``librarykit`` там просто не установлен,
``from librarykit.proc import run`` упал бы ``ModuleNotFoundError`` и апгрейд
перестал бы работать вовсе. Поэтому политику «без окна» модуль несёт сам:
:func:`_no_window_kwargs` (``CREATE_NO_WINDOW`` + ``DEVNULL`` на все три потока),
и КАЖДЫЙ ``subprocess.run`` здесь обязан идти с явным ``timeout``.

Порядок работы (#1399; всё best-effort, но ВОЗВРАТ ДЕМОНА важнее апгрейда):
1. взять single-flight лок — два одновременных апгрейда рвут trampoline;
2. подождать, пока выйдет породивший нас launcher;
3. ПРИГЛУШИТЬ watchdog-задачу (schtasks ``/End`` + ``/DISABLE``) — иначе её тик
   раз в 3 минуты поднимет НОВЫЙ ``skillery daemon start`` ровно посреди
   установки, и он снова залочит ``Scripts/`` («os error 5»);
4. погасить ВСЮ цепочку демона И живые процессы watchdog-лаунчера (wscript);
5. пройти цепочку команд обновления до первой удачной;
6. в ``finally`` — вернуть watchdog и поднять демон заново (при неудаче
   установки ТОЖЕ: устройство без демона теряет связь с хабом навсегда), с
   резервным путём запуска на случай битого трамплина;
7. ПОДТВЕРДИТЬ: живые pid демона + рабочий ``daemon status``.

Конфиг приходит одним JSON-файлом (см. ``main``).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
_ERROR_ALREADY_EXISTS = 183
MUTEX_NAME = "Global\\SkilleryUpgradeSingleton"
LOCK_FILENAME = "upgrade.lock"  # POSIX-плечо лока; путь считает lock_path()

#: env-оверрайд имени мьютекса — ТОЛЬКО для изоляции тестов. Мьютекс живёт в
#: ядре, а не в HOME: прогон pytest, взявший боевое имя, заставлял НАСТОЯЩИЙ
#: демон считать «апгрейд уже идёт» и молча пропускать задачу ``cli_upgrade``.
MUTEX_ENV = "SKILLERY_UPGRADE_MUTEX"

IS_WIN = sys.platform == "win32"

#: Имя watchdog-задачи планировщика. Вынужденный дубль
#: ``daemon/autostart.py`` (``_branding.DAEMON_TASK_NAME + "Watchdog"``):
#: worker переживает замену пакета и не имеет права импортировать его.
WATCHDOG_TASK = "SkilleryDaemonWatchdog"


def mutex_name() -> str:
    """Имя ядерного мьютекса апгрейда (env-оверрайд для изоляции тестов)."""
    return os.environ.get(MUTEX_ENV) or MUTEX_NAME


def lock_path() -> Path:
    """Файл лока на POSIX. Общий с проверкой в CLI — иначе они бы не встретились."""
    return Path.home() / ".skillery" / LOCK_FILENAME


def result_path() -> Path:
    """Итог апгрейда для CLI. Общий путь с ``_consume_upgrade_result`` в CLI."""
    return Path.home() / ".skillery" / "_upgrade_result.json"


def write_result(
    cfg: dict,
    ok: bool,
    error: str = "",
    *,
    base: Path | None = None,
    daemon: dict | None = None,
    watchdog_restored: bool | None = None,
) -> None:
    """Записать итог фонового апгрейда, чтобы CLI показал его при следующем запуске.

    ``upgrade`` на Windows/в фоне запускает worker отдельным процессом и сразу
    выходит («обновление запущено»), поэтому пользователь не узнавал, чем оно
    кончилось — успех и провал выглядели одинаково. Кладём результат в
    ~/.skillery/_upgrade_result.json; CLI покажет его один раз и удалит.

    ``daemon`` / ``watchdog_restored`` (#1399) — доказательство, что после цикла
    демон и watchdog вернулись; кладутся ТОЛЬКО если переданы, чтобы форма
    sidecar'а оставалась совместимой со старым CLI, который их не ждёт.

    ``base`` — для тестов (иначе путь считает :func:`result_path`). Best-effort:
    сбой записи не должен ронять и без того хрупкий финал апгрейда.
    """
    try:
        path = (base / "_upgrade_result.json") if base is not None else result_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict = {
            "ok": bool(ok),
            "from": cfg.get("from_version", ""),
            "to": cfg.get("to_version", ""),
            "error": error or "",
        }
        if daemon is not None:
            payload["daemon_alive"] = bool(daemon.get("alive"))
            payload["daemon_pids"] = list(daemon.get("pids") or [])
            payload["daemon_status_rc"] = daemon.get("status_rc")
        if watchdog_restored is not None:
            payload["watchdog_restored"] = bool(watchdog_restored)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _log(msg: str) -> None:
    """Дневник апгрейда: worker фоновый и невидимый, без лога он — чёрный ящик.

    Именно из-за этого «обновление запущено → и тишина» диагностировалось только
    по внешним симптомам. Пишем ход в ~/.skillery/upgrade.log (best-effort).
    """
    try:
        p = Path.home() / ".skillery" / "upgrade.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except Exception:
        pass


def _no_window_kwargs() -> dict:
    """Флаги, чтобы дочерний процесс не показал консольное окно.

    Мы сами detached, консоли у нас НЕТ. Если запустить консольный uv/git без
    флагов, Windows 11 отдаёт консоль ребёнка терминалу по умолчанию и на экране
    всплывает вкладка Windows Terminal.
    """
    kw: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if IS_WIN:
        kw["creationflags"] = CREATE_NO_WINDOW
    return kw


# --------------------------------------------------------------------------
#  single-flight
# --------------------------------------------------------------------------
def acquire_lock():
    """Занять лок апгрейда. Возвращает handle или ``None``, если уже идёт.

    Держится ядром до конца процесса: аварийное завершение worker'а не оставляет
    «залипший» лок, из-за которого апгрейд потом не запустился бы никогда.
    """
    if IS_WIN:
        try:
            import ctypes

            # use_last_error обязателен: через ctypes.windll код ошибки теряется
            # (маршалинг затирает last-error), и ERROR_ALREADY_EXISTS не долетает.
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p
            ]
            handle = kernel32.CreateMutexW(None, True, mutex_name())
            if not handle or ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                return None
            return handle
        except Exception:
            return True  # ctypes недоступен — не блокируем апгрейд совсем
    try:
        import fcntl

        path = lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        return fh
    except Exception:
        return True


# --------------------------------------------------------------------------
#  остановка демона (вынужденный дубль single_instance — см. модуль-docstring)
# --------------------------------------------------------------------------
def _pids_from(cmd: list[str]) -> list[int]:
    """Выполнить команду-перечислитель и вернуть PID из её вывода (без своего).

    Свой PID исключается ВСЕГДА: worker запускается тем же ``pythonw.exe``, и
    без этого он попал бы в список «мешающих» и убил бы сам себя.
    """
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=30, check=False,
                              **({"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}))
    except Exception:
        return []
    out = (proc.stdout or b"").decode("utf-8", "ignore")
    pids: list[int] = []
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit() and int(line) != os.getpid():
            pids.append(int(line))
    return pids


def find_daemon_pids() -> list[int]:
    """PID ВСЕХ процессов демона (``daemon run``), а не только из PID-файла.

    Одна логическая копия демона на Windows — это цепочка процессов
    (launcher-трамплин → venv-редиректор → базовый интерпретатор), и держат
    файлы окружения они все.

    Матч намеренно узкий (только ``daemon run``): по этой же функции мы
    ПОДТВЕРЖДАЕМ, что демон вернулся, а короткоживущий ``daemon start`` давал бы
    ложное «жив».
    """
    if IS_WIN:
        script = (
            "Get-CimInstance Win32_Process | Where-Object { "
            "($_.Name -eq 'skillery.exe' -or $_.Name -eq 'python.exe' "
            "-or $_.Name -eq 'pythonw.exe') "
            "-and $_.CommandLine -like '*skillery*' "
            "-and $_.CommandLine -like '*daemon*run*' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    else:
        cmd = ["pgrep", "-f", r"skillery.*daemon run"]
    return _pids_from(cmd)


def find_watchdog_pids() -> list[int]:
    """PID живого watchdog-тика: ``wscript.exe //B skillery-watchdog.vbs``.

    Сам wscript ``Scripts/`` не держит, но он ПРЯМО СЕЙЧАС запускает
    ``skillery.exe daemon start`` — а тот держит. Гасим и его: иначе установка
    ловит «os error 5» от процесса, которого секунду назад ещё не было.
    """
    if not IS_WIN:
        return []
    script = (
        "Get-CimInstance Win32_Process | Where-Object { "
        "($_.Name -eq 'wscript.exe' -or $_.Name -eq 'cscript.exe') "
        "-and $_.CommandLine -like '*skillery-watchdog*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    return _pids_from(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])


def find_starting_daemon_pids() -> list[int]:
    """PID процессов ``skillery daemon start`` (короткий, но держит ``Scripts/``).

    Именно они и оказывались «вторым pythonw.exe» в момент падения установки.
    """
    if IS_WIN:
        script = (
            "Get-CimInstance Win32_Process | Where-Object { "
            "($_.Name -eq 'skillery.exe' -or $_.Name -eq 'python.exe' "
            "-or $_.Name -eq 'pythonw.exe') "
            "-and $_.CommandLine -like '*skillery*' "
            "-and $_.CommandLine -like '*daemon*start*' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]
    else:
        cmd = ["pgrep", "-f", r"skillery.*daemon start"]
    return _pids_from(cmd)


def find_blocking_pids() -> list[int]:
    """ВСЁ, что держит файлы окружения: демон + его старт + watchdog-лаунчер."""
    seen: list[int] = []
    for finder in (find_daemon_pids, find_starting_daemon_pids, find_watchdog_pids):
        try:
            for pid in finder():
                if pid not in seen:
                    seen.append(pid)
        except Exception:  # noqa: BLE001 — один сломавшийся поиск не рушит остальные
            continue
    return seen


# --------------------------------------------------------------------------
#  watchdog (schtasks) — приглушить на время установки и обязательно вернуть
# --------------------------------------------------------------------------
def _run_quiet(cmd: list[str], timeout: float = 60.0) -> int:
    """``subprocess.run`` без окна и с обязательным таймаутом. rc или 1."""
    try:
        return subprocess.run(cmd, timeout=timeout, **_no_window_kwargs()).returncode
    except Exception as exc:  # noqa: BLE001
        _log(f"cmd EXC {' '.join(cmd)}: {exc}")
        return 1


def suspend_watchdog(task: str = WATCHDOG_TASK) -> bool:
    """Снять текущий тик и ОТКЛЮЧИТЬ watchdog-задачу на время установки.

    Без этого сценарий воспроизводится как по часам: watchdog раз в 3 минуты
    поднимает демон, установка длится дольше — и ``uv tool install --force``
    падает с «failed to remove directory Scripts: os error 5».

    True ⇒ задачу выключили МЫ и обязаны включить обратно (см.
    :func:`restore_watchdog`). На не-Windows — no-op (там роль watchdog играют
    systemd Restart / launchd KeepAlive, файлы они не держат).
    """
    if not IS_WIN:
        return False
    _run_quiet(["schtasks", "/End", "/TN", task], timeout=30)
    rc = _run_quiet(["schtasks", "/Change", "/TN", task, "/DISABLE"], timeout=30)
    _log(f"suspend_watchdog {task} → rc={rc}")
    return rc == 0


def restore_watchdog(was_suspended: bool, task: str = WATCHDOG_TASK,
                     attempts: int = 3) -> bool:
    """Вернуть watchdog в строй. Вызывается ВСЕГДА, в т.ч. после провала.

    Watchdog — последняя линия обороны «устройство всегда на связи»: если он
    останется выключенным, а демон не поднимется, машина замолчит навсегда.
    Поэтому здесь повторы, а результат идёт в лог апгрейда.
    """
    if not was_suspended:
        return False
    for attempt in range(1, attempts + 1):
        rc = _run_quiet(["schtasks", "/Change", "/TN", task, "/ENABLE"], timeout=30)
        _log(f"restore_watchdog attempt {attempt}: rc={rc}")
        if rc == 0:
            return True
        time.sleep(2)
    return False


def stop_daemons(timeout: float = 10.0) -> list[int]:
    """Погасить всех и ДОЖДАТЬСЯ, пока отпустят файлы.

    Ждать обязательно: `uv tool install --force` падает с «os error 5», если
    хоть один процесс ещё держит `Scripts/`. Гасим не только ``daemon run``, но
    и ``daemon start``/watchdog-лаунчер — см. :func:`find_blocking_pids`.
    """
    killed: list[int] = []
    for pid in find_blocking_pids():
        try:
            if IS_WIN:
                import ctypes

                PROCESS_TERMINATE = 0x0001
                h = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                if h:
                    ctypes.windll.kernel32.TerminateProcess(h, 1)
                    ctypes.windll.kernel32.CloseHandle(h)
                    killed.append(pid)
            else:
                import signal

                os.kill(pid, signal.SIGTERM)
                killed.append(pid)
        except Exception:
            continue
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not find_blocking_pids():
            break
        time.sleep(0.3)
    return killed


# --------------------------------------------------------------------------
#  апгрейд + возврат демона
# --------------------------------------------------------------------------
def run_upgrade(commands: list[list[str]], retries: int = 6, delay: float = 15.0) -> bool:
    """Прогнать команды до первой удачной, с ПОВТОРАМИ на первой (пин версии).

    Первая команда — пин точной версии (её мы уже видели на PyPI). Сразу после
    релиза индекс ещё не разъехался по CDN и пин транзиентно отвечает «no
    version». Раньше мы на этом откатывались на голый `install latest` — но
    отставший edge-узел отдавал СТАРУЮ версию как latest, и апгрейд «успешно»
    ставил то же самое. Поэтому пин ПОВТОРЯЕМ с бэкоффом (CDN догоняет за
    секунды-минуты), и только если он так и не дался — пробуем прочие команды
    как последнее средство.
    """
    primary, rest = (commands[0], commands[1:]) if commands else (None, [])
    if primary is not None:
        for attempt in range(1, retries + 1):
            try:
                rc = subprocess.run(primary, timeout=600, **_no_window_kwargs()).returncode
                _log(f"upgrade attempt {attempt}/{retries}: {' '.join(primary)} → rc={rc}")
                if rc == 0:
                    return True
            except Exception as e:  # noqa: BLE001
                _log(f"upgrade attempt {attempt}/{retries} EXC: {e}")
            if attempt < retries:
                time.sleep(delay)
    for cmd in rest:
        try:
            rc = subprocess.run(cmd, timeout=600, **_no_window_kwargs()).returncode
            _log(f"upgrade fallback: {' '.join(cmd)} → rc={rc}")
            if rc == 0:
                return True
        except Exception as e:  # noqa: BLE001
            _log(f"upgrade fallback EXC: {e}")
    return False


def tool_venv_python() -> str | None:
    """Python внутри tool-venv CLI — ЗАПАСНОЙ путь запуска демона.

    Трамплин ``skillery.exe`` после неудачной установки бывает битым («uv
    trampoline failed to canonicalize script path») — именно в этом состоянии
    на живой машине переставал работать и ``daemon stop``. Модуль
    ``skillery_cli`` при этом на месте, и запустить демон можно напрямую
    интерпретатором tool-venv.
    """
    roots: list[Path] = []
    env_dir = os.environ.get("UV_TOOL_DIR")
    if env_dir:
        roots.append(Path(env_dir))
    if IS_WIN:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            roots.append(Path(local) / "uv" / "tools")
    else:
        roots.append(Path.home() / ".local" / "share" / "uv" / "tools")
    names = ("pythonw.exe", "python.exe") if IS_WIN else ("python3", "python")
    for root in roots:
        for name in names:
            for candidate in (
                root / "skillery-cli" / "Scripts" / name,
                root / "skillery-cli" / "bin" / name,
            ):
                try:
                    if candidate.exists():
                        return str(candidate)
                except OSError:
                    continue
    return None


def start_commands(binary: str) -> list[list[str]]:
    """Пути подъёма демона по убыванию предпочтения (штатный → запасной)."""
    cmds: list[list[str]] = []
    if binary:
        cmds.append([binary, "daemon", "start"])
    py = tool_venv_python()
    if py:
        cmds.append([py, "-m", "skillery_cli", "daemon", "start"])
    return cmds


def confirm_daemon(binary: str) -> dict:
    """ПОДТВЕРДИТЬ, что демон реально живой, а не «команда вернула 0».

    Две независимые проверки, потому что каждая по отдельности врёт:

    * ``pids`` — процессы ``daemon run`` в системе. Это и есть факт «демон
      работает»; ``daemon start`` мог вернуть 0 и тут же умереть.
    * ``status_rc`` — код ``skillery daemon status``. Он проверяет ТРАМПЛИН:
      если .exe после установки битый, команда падает, даже когда демон жив.
    """
    pids = find_daemon_pids()
    status_rc: int | None = None
    if binary:
        status_rc = _run_quiet([binary, "daemon", "status"], timeout=60)
    return {"pids": pids, "status_rc": status_rc, "alive": bool(pids)}


def ensure_daemon_back(binary: str, *, attempts: int = 5, delay: float = 3.0) -> dict:
    """Поднять демон и дождаться подтверждения. Возвращает отчёт confirm+.

    Инвариант #1399: сюда мы попадаем и после УДАЧНОЙ, и после ПРОВАЛЬНОЙ
    установки. Оставить устройство без демона нельзя ни в одном из случаев —
    очередь заданий перестанет применяться, и хаб потеряет машину навсегда.
    """
    cmds = start_commands(binary)
    if not cmds:
        _log("ensure_daemon_back: нечем запускать (нет бинаря и tool-venv)")
        return {"pids": [], "status_rc": None, "alive": False, "started_with": None}
    for cmd in cmds:
        for attempt in range(1, attempts + 1):
            rc = _run_quiet(cmd, timeout=60)
            _log(f"start_daemon [{cmd[0]}] attempt {attempt}: rc={rc}")
            report = confirm_daemon(binary)
            if report["alive"]:
                report["started_with"] = cmd[0]
                _log(f"daemon подтверждён: pids={report['pids']} "
                     f"status_rc={report['status_rc']}")
                return report
            time.sleep(delay)
    report = confirm_daemon(binary)
    report["started_with"] = None
    _log(f"ensure_daemon_back: НЕ подтверждён (pids={report['pids']}, "
         f"status_rc={report['status_rc']})")
    return report


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        return 2
    try:
        cfg = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    except Exception as e:
        _log(f"worker start: bad config: {e}")
        return 2

    lock = acquire_lock()
    if lock is None:
        _log("worker start: апгрейд уже идёт — выходим")
        return 0  # апгрейд уже идёт — второй worker не нужен

    _log(f"--- worker start pid={os.getpid()} ---")
    time.sleep(float(cfg.get("delay", 4.0)))

    # 1. Приглушить watchdog ДО остановки демона: иначе его тик поднимет демон
    #    обратно ровно между нашим kill'ом и заменой файлов.
    suspended = suspend_watchdog()
    ok = False
    error = ""
    restored = False
    daemon: dict = {"pids": [], "status_rc": None, "alive": False}
    try:
        killed = stop_daemons()
        _log(f"stop_daemons killed={killed}")
        ok = run_upgrade([list(c) for c in cfg.get("commands", [])])
        _log(f"run_upgrade → {ok}")
        if not ok:
            error = "команды обновления завершились с ошибкой"
    except Exception as exc:  # noqa: BLE001 — падение установки не отменяет возврат
        error = f"исключение при обновлении: {exc}"
        _log(f"run_upgrade EXC: {exc}")
    finally:
        # 2. ВОЗВРАТ — важнее самого апгрейда (#1399). Сначала watchdog (страховка
        #    на случай, если подъём демона ниже не удастся вовсе), затем демон.
        try:
            restored = restore_watchdog(suspended)
        except Exception as exc:  # noqa: BLE001
            _log(f"restore_watchdog EXC: {exc}")
        try:
            daemon = ensure_daemon_back(cfg.get("daemon_binary", ""))
        except Exception as exc:  # noqa: BLE001
            _log(f"ensure_daemon_back EXC: {exc}")
        _log(f"watchdog restored={restored} daemon={daemon}")

    if not daemon.get("alive") and ok:
        # Обновились, но демон не поднялся — для пользователя это НЕ «✓»:
        # устройство молчит, и он должен об этом узнать.
        error = "демон не поднялся после обновления — запустите: skillery daemon start"
    # Итог — для CLI: показать при следующем запуске, чтобы «запущено → тишина»
    # сменилось явным «✓ обновлён» / «✗ не удалось».
    write_result(cfg, ok and bool(daemon.get("alive")), error,
                 daemon=daemon, watchdog_restored=restored)
    _log(f"--- worker done ok={ok} daemon_alive={daemon.get('alive')} ---")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
