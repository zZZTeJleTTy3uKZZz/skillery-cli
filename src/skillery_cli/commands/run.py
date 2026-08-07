"""``skillery run <slug> [args...]`` — обёртка запуска навыка (#1221).

ЗАЧЕМ. Событий «навыком воспользовались» в системе фактически не было: метрика
«запуски» равнялась числу установок. Просить LLM отчитаться о вызове ненадёжно
— отчёт зависит от того, вспомнит ли модель. Факт запуска обязан фиксировать
КОД, а не намерение агента. Поэтому вызов навыка идёт через эту обёртку: она
кладёт событие в общий outbox НЕЗАВИСИМО от того, что делает и о чём отчитается
модель.

ЧТО ЗНАЧИТ «ЗАПУСТИТЬ НАВЫК» (факты из архитектуры стора):

* навык материализуется в ``<store>/<slug>/`` (``SKILL.md`` + мета
  ``_skill_meta.json``);
* если это ``kind="tooling"``-навык, его мета несёт ``manifest.cli[]``
  (``command_name`` + ``entrypoint``), и установка кладёт исполняемый **shim**
  в bin-каталог стора (``<store>/../bin/<command_name>[.cmd]``) + sidecar
  ``<command_name>.json`` — см. ``skillkit.path_store``;
* prompt-/comprehensive-навык исполняемых артефактов НЕ несёт. Запускать нечего
  — но факт использования всё равно существует и должен быть зафиксирован
  (агент прочитал инструкцию и применил её). Для таких навыков команда работает
  как чекпоинт: пишет событие и завершается с кодом 0.

КОНТРАКТ ОБЁРТКИ (три обещания, ровно как у ``telemetrykit.track_run``):

1. **Код возврата принадлежит навыку.** Обёртка отдаёт его наружу НЕИЗМЕННЫМ —
   иначе она сломает всё, что на него смотрит (скрипты, CI, оркестратор).
2. **Телеметрия не роняет запуск.** Любая ошибка учёта — это запись в лог, а не
   падение: пользовательская команда важнее метрики.
3. **stdio прозрачно.** Дочерний процесс наследует stdin/stdout/stderr — навык
   остаётся интерактивным, а обёртка ничего не печатает в его канал.

ЧТО ЗАПУСКАЕМ И ПОЧЕМУ ИМЕННО ЭТО (антирекурсия — главное здесь).

С ``s-skillkit`` 0.3.5 shim навыка сам зовёт ``skillery run <slug> --command
<name> …`` — именно так учёт перестал зависеть от того, вспомнит ли модель
позвать обёртку: он срабатывает при ЛЮБОМ вызове (``vk …``, ``atlas …``),
включая уже опубликованные навыки. Отсюда жёсткое следствие: **обёртка НЕ имеет
права запускать shim**. Пара «shim → run → shim» — бесконечный цикл, который
положит машину пользователя.

Поэтому исполняемое резолвится из sidecar'а ``<bin>/<command_name>.json``, поле
``entrypoint`` — там лежит ПРЯМОЙ вызов (``<venv>/Scripts/python.exe -c "…"``).
Shim остаётся лишь крайним fallback'ом для старых установок, где sidecar'а нет.

Второй, независимый предохранитель — маркер-переменная
``SKILLERY_RUN_ACTIVE``. Обёртка выставляет её дочернему процессу, а увидев её
на СВОЁМ входе, честно падает с ``RUN_RECURSION``: значит цикл всё-таки
замкнулся (старый shim, ручной PATH, entrypoint зовёт обёртку сам). Крутиться
до исчерпания памяти вместо понятной ошибки — недопустимо. Тот же маркер
проверяет и сам shim, уходя на прямой вызов.

Запуск процесса — через ``librarykit.proc.popen`` (единая политика win32:
``CREATE_NO_WINDOW``, консольные окна дочерних процессов не всплывают). Прямой
``subprocess`` в CLI запрещён гейтом ``tests/test_no_raw_subprocess.py``.

Таймаута здесь намеренно НЕТ (в отличие от ``core.proc_runner``): это
приоритетный процесс у терминала пользователя, а не фоновая работа демона —
прервать его есть кому (Ctrl-C), и убивать долгий легитимный прогон навыка по
произвольному лимиту нельзя.
"""
from __future__ import annotations

import json
import logging
import os
import shlex
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from clikit.command_kit import command
from librarykit.errors import CliError

from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data

_LOG = logging.getLogger(__name__)

#: Тип конверта в общем outbox'е. Приёмник уже разбирает его в ``skill.run``.
KIND_SKILL_RUN = "skill_run"

#: Код возврата для прерывания пользователем — общепринятый 128+SIGINT.
EXIT_INTERRUPTED = 130

#: Дефолт маркера антирекурсии на случай, если кит его не отдаёт (старая версия).
#: Реальное имя спрашиваем у ``skillkit.path_store`` — оно ОДНО и там же его
#: пишет в shim; захардкодить второе значение = разъехаться с shim'ом молча.
_GUARD_ENV_FALLBACK = "SKILLERY_RUN_ACTIVE"


def guard_env_name() -> str:
    """Имя маркер-переменной антирекурсии (общее с shim'ом кита)."""
    try:
        from skillkit import path_store

        name = str(path_store.guard_env_name()).strip()
        return name or _GUARD_ENV_FALLBACK
    except Exception as exc:  # noqa: BLE001 — старый кит без раннера
        _LOG.debug("имя маркера антирекурсии не получено у кита: %s", exc)
        return _GUARD_ENV_FALLBACK


def _guard_against_recursion() -> None:
    """Упасть понятно, если обёртка вызвана ИЗ СВОЕГО ЖЕ дочернего процесса.

    Штатно этого быть не может: ``run`` зовёт прямой entrypoint из sidecar'а, а
    shim при виде маркера уходит на прямой вызов. Но цикл может замкнуться
    иначе — старый shim без проверки маркера, ручной PATH, сам entrypoint зовёт
    ``skillery run``. Крутиться до исчерпания памяти вместо честной ошибки —
    недопустимо: это ляжет на машину пользователя, а не на метрику.
    """
    if os.environ.get(guard_env_name()):
        raise CliError(
            "RUN_RECURSION",
            "Обнаружен цикл запуска: «skillery run» вызван из процесса, который "
            "сам запущен «skillery run».\nОбычно это устаревший shim навыка. "
            "Починить: skillery install <slug> --force",
        )


# ---------------------------------------------------------------------------
#  Разрешение навыка и его исполняемой сущности
# ---------------------------------------------------------------------------
def _skill_dir(cfg: ClientConfig, slug: str) -> Path:
    """Каталог навыка в сторе. Отсутствие каталога — понятная ошибка, не traceback."""
    store = cfg.effective_store_dir()
    path = store / slug
    if not path.is_dir():
        raise CliError(
            "SKILL_NOT_FOUND",
            f"Навык «{slug}» не установлен на этом устройстве "
            f"(нет каталога {path}).\n"
            f"Поставить: skillery install {slug}   ·   "
            f"что установлено: skillery installed",
        )
    return path


def _read_meta(skill_dir: Path) -> dict[str, Any]:
    """Мета навыка. Битая/отсутствующая мета — не повод падать: вернём пусто."""
    try:
        from skillkit.installer import read_meta

        return read_meta(skill_dir) or {}
    except Exception as exc:  # noqa: BLE001 — мета best-effort
        _LOG.debug("не прочитана мета навыка %s: %s", skill_dir, exc)
        return {}


def _cli_entries(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """``manifest.cli[]`` навыка — список исполняемых команд, которые он несёт."""
    manifest = meta.get("manifest")
    if not isinstance(manifest, dict):
        return []
    raw = manifest.get("cli")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, dict) and str(c.get("command_name") or "").strip()]


def _pick_entry(
    entries: list[dict[str, Any]], slug: str, wanted: str | None
) -> dict[str, Any]:
    """Какую из команд навыка запускать.

    Явный ``--command`` сильнее всего. Иначе предпочитаем одноимённую слагу
    (типовой случай ``vk`` → команда ``vk``), а при отсутствии совпадения —
    первую объявленную: у подавляющего большинства навыков она одна.
    """
    if wanted:
        for entry in entries:
            if str(entry.get("command_name")) == wanted:
                return entry
        names = ", ".join(str(e.get("command_name")) for e in entries)
        raise CliError(
            "COMMAND_NOT_FOUND",
            f"У навыка «{slug}» нет команды «{wanted}». Доступные: {names}",
        )
    for entry in entries:
        if str(entry.get("command_name")) == slug:
            return entry
    return entries[0]


def _bin_dir() -> Path | None:
    """Bin-каталог стора у ``skillkit.path_store`` — того, кто shim'ы и кладёт.

    Второго мнения о раскладке каталога здесь не заводим: разъехавшись, оно
    молча увело бы запуск мимо установленного навыка.
    """
    try:
        from skillkit import path_store

        return path_store.bin_dir()
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("не определён bin-каталог стора: %s", exc)
        return None


def _split_entrypoint(entrypoint: str) -> list[str]:
    """Разобрать строку entrypoint в argv. Пусто — если разобрать не удалось.

    Реальный entrypoint выглядит так::

        C:\\...\\Scripts\\python.exe -c "import sys; from atlas.cli import app; …"

    и ломает оба режима ``shlex`` поодиночке: ``posix=True`` на Windows съел бы
    обратные слэши путей (``C:\\venv`` → ``C:venv``), а ``posix=False`` оставил
    бы кавычки ВНУТРИ токена — и ``CreateProcess`` получил бы аргумент
    ``"import sys; …"`` вместе с кавычками. Поэтому на win32 режим
    non-posix + ручное снятие обрамляющих кавычек, на POSIX — штатный posix.
    """
    if not entrypoint:
        return []
    try:
        if sys.platform != "win32":
            return shlex.split(entrypoint, posix=True)
        tokens = shlex.split(entrypoint, posix=False)
    except ValueError as exc:  # незакрытая кавычка в мете навыка
        _LOG.debug("entrypoint %r не разобран: %s", entrypoint, exc)
        return []
    out: list[str] = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
            token = token[1:-1]
        out.append(token)
    return out


def _sidecar_entrypoint(command_name: str) -> list[str] | None:
    """argv прямого запуска команды из sidecar'а ``<bin>/<command_name>.json``.

    Sidecar — ИСТОЧНИК ПРАВДЫ о том, чем на самом деле запускается навык
    (``<venv>/Scripts/python.exe -c "…"``). Именно его, а не shim, обязана
    звать обёртка: shim с кита 0.3.5 сам ведёт в ``skillery run``, и вызов
    shim'а отсюда замкнул бы бесконечный цикл.
    """
    d = _bin_dir()
    if d is None:
        return None
    sidecar = d / f"{command_name}.json"
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _LOG.debug("sidecar %s не прочитан: %s", sidecar, exc)
        return None
    if not isinstance(data, dict):
        return None
    entrypoint = str(data.get("entrypoint") or "").strip()
    if not entrypoint:
        return None
    return _split_entrypoint(entrypoint) or None


def _shim_path(command_name: str) -> Path | None:
    """Путь к установленному shim'у команды навыка (или ``None``, если его нет).

    ВНИМАНИЕ: это КРАЙНИЙ fallback (старые установки без sidecar'а). Shim кита
    0.3.5+ ведёт в ``skillery run`` — запускать его отсюда штатно нельзя.
    """
    d = _bin_dir()
    if d is None:
        return None
    shim = d / (f"{command_name}.cmd" if sys.platform == "win32" else command_name)
    return shim if shim.exists() else None


def _resolve_executable(entry: dict[str, Any], slug: str) -> list[str]:
    """argv[0..] для запуска команды навыка.

    Приоритет — ПРЯМОЙ entrypoint из sidecar'а: только он гарантированно не
    заворачивает исполнение обратно в эту же обёртку. Дальше — shim и команда
    из PATH: обе ветки существуют ради установок, сделанных до кита 0.3.5, где
    shim ещё звал entrypoint напрямую. Ничего не нашли — честная ошибка с
    рецептом, а не ``FileNotFoundError`` из недр ``popen``.

    Чего здесь НЕТ намеренно: ``entrypoint`` из МЕТЫ навыка. Там лежит спека
    (``demo_skill.cli:main``), а не исполняемое — запустить её нельзя; резолвом
    спеки в интерпретатор venv занимается установщик, и результат он кладёт
    именно в sidecar.
    """
    name = str(entry["command_name"]).strip()

    argv = _sidecar_entrypoint(name)
    if argv:
        return argv

    shim = _shim_path(name)
    if shim is not None:
        return [str(shim)]
    import shutil

    found = shutil.which(name)
    if found:
        return [found]
    raise CliError(
        "CLI_NOT_REGISTERED",
        f"Команда «{name}» навыка «{slug}» не зарегистрирована на этом "
        f"устройстве (нет sidecar'а/shim'а в bin-каталоге стора и нет команды "
        f"в PATH).\nПочинить: skillery install {slug} --force",
    )


# ---------------------------------------------------------------------------
#  Телеметрия — учёт факта вызова
# ---------------------------------------------------------------------------
def _identity(skill_dir: Path, slug: str, meta: dict[str, Any]) -> tuple[str, str]:
    """(component_id, version) навыка для события.

    Идентификатор — slug (то, чем навык называется в хабе и в сторе). Версия —
    из меты навыка, а если её там нет, добираем каскадом ``telemetrykit.detect``
    по файлам меты САМОГО навыка (``_skill_meta.toml`` / frontmatter
    ``SKILL.md``). Хардкодить версию нельзя: второй источник правды о версии уже
    однажды дал бесконечную петлю самообновления CLI.
    """
    version = str(meta.get("version") or "").strip()
    if not version:
        try:
            from telemetrykit import detect

            found = detect.resolve(caller_file=skill_dir / "SKILL.md", module_name=None)
            version = (found.component_version or "").strip()
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("версия навыка %s не определена: %s", slug, exc)
    return slug, version or "unknown"


class _SafeTracker:
    """Обёртка над ``telemetrykit.track_run``, из которой телеметрия не вылетает.

    Сам кит уже гасит свои внутренние ошибки, но обёртка не вправе полагаться на
    это как на единственный барьер: если трекер не создался (кит не установлен,
    конфигурация сломана) или его ``__exit__`` всё же бросил, пользовательская
    команда обязана отработать. Исключение ВЫЗЫВАЮЩЕГО при этом проходит
    насквозь — код возврата принадлежит навыку.
    """

    def __init__(self, component_id: str, version: str, argv: list[str]) -> None:
        self._component_id = component_id
        self._version = version
        self._argv = argv
        self._inner: Any = None

    def __enter__(self) -> _SafeTracker:
        try:
            from telemetrykit import track_run

            # Идентичность передаём ЯВНО: каскад автоопределения резолвил бы
            # вызывающего — то есть сам CLI, — а нам нужен запущенный навык.
            self._inner = track_run(
                self._component_id,
                self._version,
                kind=KIND_SKILL_RUN,
                argv=self._argv,
            )
            self._inner.__enter__()
        except Exception as exc:  # noqa: BLE001
            self._inner = None
            _LOG.warning("телеметрия запуска не включилась: %s", exc)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if self._inner is not None:
            try:
                self._inner.__exit__(exc_type, exc, tb)
            except Exception as err:  # noqa: BLE001
                _LOG.warning("телеметрия запуска не записалась: %s", err)
        # False — исключение вызывающего летит дальше (True «съел» бы падение).
        return False


# ---------------------------------------------------------------------------
#  Запуск
# ---------------------------------------------------------------------------
#: Код отказа гейта лиза. Один на все вердикты кита осознанно: пользователю
#: причину объясняет ТЕКСТ (кит гарантирует, что в нём есть следующее действие),
#: а машинный код нужен скриптам лишь чтобы отличить «права нет» от «навык не
#: установлен». Вердикт уезжает в лог, не в код возврата.
LEASE_DENIED_CODE = "CAPABILITY_LEASE_DENIED"


def _gate_lease(slug: str, meta: dict[str, Any]) -> None:
    """Проверить право использовать способность — ЛОКАЛЬНО, до запуска (#1490).

    ЧТО ЭТО. До #1490 проверки при запуске не было вовсе: установленный навык
    работал вечно и офлайн, а отзыв права на хабе на машину не доезжал никак.
    Гейт закрывает третий (и единственный гарантирующий) рубеж отзыва —
    истечение лиза: даже машина, месяц не видевшая сети, перестаёт исполнять
    отозванную способность не позже ``exp`` выданного лиза.

    ТРИ СВОЙСТВА, КОТОРЫЕ ЗДЕСЬ НЕЛЬЗЯ НАРУШИТЬ:

    1. **В сеть не ходим.** Ни за ключами, ни за лизом, ни за «а вдруг
       отозвали». ЦКП задачи: «при живой сети и действующем праве запуск не
       ходит в сеть и не замедляется». Обновление — работа демона.
    2. **Гейт точечный.** Навык без способностей с ``requires_lease``
       запускается ровно как раньше — ни одного лишнего файла не читается.
       Ошибка здесь остановила бы людям всю работу, а не «платную часть».
    3. **Отказ объясняет, что делать.** Текст берём у кита
       (``leasekit.messages``): второй набор формулировок означал бы, что CLI и
       gateway объясняют одно событие по-разному.

    Проверка обёрнута так, что СВОЙ сбой (битый файл состояния, отсутствующий
    кит, что угодно неожиданное) запуск НЕ роняет: гарантию даёт истечение
    лиза, а не безошибочность этого кода, и превращать баг в «ничего не
    работает» нельзя. Отказ самого гейта — ``LeaseDenied`` — при этом проходит
    насквозь: он и есть решение.
    """
    try:
        from skillery_cli.core.leases import check_skill_run
    except Exception as exc:  # noqa: BLE001 — старое окружение без кита
        _LOG.warning("гейт лиза не подключён: %s", exc)
        return
    try:
        denied = check_skill_run(slug, skill_id=str(meta.get("skill_id") or "") or None)
    except Exception as exc:  # noqa: BLE001 — сбой гейта не имеет права ронять запуск
        _LOG.warning("гейт лиза не отработал (%s): %s", slug, exc)
        return
    if denied is None:
        return
    _LOG.warning(
        "запуск «%s» отклонён гейтом лиза: %s", slug, denied.verdict.value,
        extra={"context": {
            "slug": slug, "capability": denied.capability,
            "verdict": denied.verdict.value,
        }},
    )
    raise CliError(LEASE_DENIED_CODE, denied.message)


def _spawn(argv: list[str], *, cwd: Path | None) -> int:
    """Запустить дочерний процесс со СКВОЗНЫМ stdio и дождаться его кода.

    ``librarykit.proc.popen``, а не ``proc.run``: ``run`` захватывает потоки и
    закрывает stdin (правильно для фоновых вызовов демона, но здесь убило бы
    интерактивность и живой вывод навыка). Явные ``None`` в stdio — наследование
    дескрипторов CLI. Политика окон win32 (``CREATE_NO_WINDOW``) остаётся той
    же: она в ``popen``, а не в ``run``.
    """
    from librarykit import proc

    # Маркер антирекурсии наследуется всем поддеревом процесса: shim навыка,
    # увидев его, уйдёт на прямой вызов entrypoint, а вложенный «skillery run»
    # честно упадёт RUN_RECURSION вместо бесконечного цикла.
    env = {**os.environ, guard_env_name(): "1"}
    child = proc.popen(argv, cwd=cwd, env=env, stdin=None, stdout=None, stderr=None)
    return int(child.wait())


def cmd_run(
    slug: str = typer.Argument(..., help="Slug установленного навыка"),
    args: Optional[list[str]] = typer.Argument(
        None, help="Аргументы, передаваемые навыку как есть"
    ),
    command_name: Optional[str] = typer.Option(
        None, "--command", help="Какую команду навыка запустить (если их несколько)"
    ),
    cwd: Optional[Path] = typer.Option(
        None, "--cwd", help="Рабочий каталог для запуска (default: текущий)"
    ),
) -> None:
    """Запустить навык, зафиксировав факт вызова.

    Аргументы после slug уходят навыку без изменений, код возврата навыка —
    наружу без изменений. Событие ``skill_run`` кладётся в общий outbox всегда:
    и на успехе, и на ошибке, и на прерывании.

    Prompt-навык (без исполняемых артефактов) фиксируется как чекпоинт
    использования и завершается кодом 0 — запускать у него нечего.
    """
    # Прямой вызов из тестов приносит sentinel typer-объекты вместо значений.
    if not isinstance(args, list):
        args = []
    if not isinstance(command_name, str):
        command_name = None
    if not isinstance(cwd, Path):
        cwd = None

    # ПЕРВЫМ делом — до стора, меты и телеметрии: если цикл уже замкнулся,
    # каждый его виток не должен ещё и писать событие в outbox.
    _guard_against_recursion()

    cfg = ClientConfig.load()
    skill_dir = _skill_dir(cfg, slug)
    meta = _read_meta(skill_dir)
    component_id, version = _identity(skill_dir, slug, meta)
    entries = _cli_entries(meta)

    # #1490: право использовать способность — ЛОКАЛЬНО и до всего остального.
    # Раньше телеметрии: отклонённый запуск не должен считаться «запуском».
    # Раньше выбора команды: отказ не зависит от того, какую из команд навыка
    # просили, — право выдано на способность, а не на аргументы командной
    # строки. Контракт лиза ставит гейт «перед _spawn»; здесь он на несколько
    # строк выше, потому что prompt-навык (ветка «без исполняемых артефактов»)
    # тоже есть использование способности, а _spawn у него не вызывается.
    _gate_lease(slug, meta)

    # argv для события: значения аргументов в конверт не попадают (кит берёт из
    # argv только ИМЕНА флагов) — приватность здесь не ослабляем.
    tracked_argv = [slug, *[str(a) for a in args]]

    if not entries:
        with _SafeTracker(component_id, version, tracked_argv):
            emit_data(
                {
                    "slug": slug,
                    "version": version,
                    "executed": False,
                    "reason": "no-cli-artifacts",
                    "returncode": 0,
                },
                text_renderer=lambda p: print(
                    f"✓ Навык «{p['slug']}» ({p['version']}) — без исполняемых "
                    f"артефактов; факт использования зафиксирован."
                ),
            )
        return

    entry = _pick_entry(entries, slug, command_name)
    argv = [*_resolve_executable(entry, slug), *[str(a) for a in args]]

    try:
        with _SafeTracker(component_id, version, tracked_argv):
            code = _spawn(argv, cwd=cwd)
            if code != 0:
                # SystemExit, а не своё исключение: кит трактует его как код
                # возврата (0 → успех, иначе ошибка), а click отдаёт этот код
                # наружу как есть.
                raise SystemExit(code)
    except KeyboardInterrupt:
        # Трекер уже записал статус «прервано» (кит различает KeyboardInterrupt).
        raise SystemExit(EXIT_INTERRUPTED) from None


def register(app: typer.Typer) -> None:
    """Зарегистрировать ``skillery run``.

    ``ignore_unknown_options`` + ``allow_interspersed_args=False`` — чтобы флаги
    навыка (``--json``, ``-v``) доставались НАВЫКУ, а не разбирались typer'ом
    как флаги обёртки. Всё после slug — аргументы навыка.
    """
    app.command(
        name="run",
        context_settings={
            "ignore_unknown_options": True,
            "allow_interspersed_args": False,
        },
    )(command(cmd_run))


__all__ = [
    "EXIT_INTERRUPTED",
    "KIND_SKILL_RUN",
    "cmd_run",
    "guard_env_name",
    "register",
]
