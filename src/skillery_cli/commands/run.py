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
  ``<command_name>.json`` — см. ``skillkit.path_store``. ЭТО и есть исполняемая
  сущность навыка: «запустить навык» = запустить его shim;
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

Запуск процесса — через ``librarykit.proc.popen`` (единая политика win32:
``CREATE_NO_WINDOW``, консольные окна дочерних процессов не всплывают). Прямой
``subprocess`` в CLI запрещён гейтом ``tests/test_no_raw_subprocess.py``.

Таймаута здесь намеренно НЕТ (в отличие от ``core.proc_runner``): это
приоритетный процесс у терминала пользователя, а не фоновая работа демона —
прервать его есть кому (Ctrl-C), и убивать долгий легитимный прогон навыка по
произвольному лимиту нельзя.
"""
from __future__ import annotations

import logging
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


def _shim_path(command_name: str) -> Path | None:
    """Путь к установленному shim'у команды навыка (или ``None``, если его нет).

    Каталог берём у ``skillkit.path_store`` — того самого модуля, который shim и
    кладёт; второго мнения о раскладке bin-каталога здесь не заводим.
    """
    try:
        from skillkit import path_store

        shim = path_store.bin_dir() / (
            f"{command_name}.cmd" if sys.platform == "win32" else command_name
        )
        return shim if shim.exists() else None
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("не определён путь shim'а для %s: %s", command_name, exc)
        return None


def _resolve_executable(entry: dict[str, Any], slug: str) -> list[str]:
    """argv[0..] для запуска команды навыка.

    Приоритет — shim стора: он единственный знает, каким интерпретатором
    (venv навыка) звать entrypoint. Если shim'а нет, но команда уже на PATH
    (навык ставился иначе / PATH настроен вручную) — берём её оттуда. Иначе
    честная ошибка с готовым рецептом, а не падение ``FileNotFoundError``.
    """
    name = str(entry["command_name"]).strip()
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
        f"устройстве (нет shim'а в bin-каталоге стора и нет команды в PATH).\n"
        f"Починить: skillery install {slug} --force",
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
def _spawn(argv: list[str], *, cwd: Path | None) -> int:
    """Запустить дочерний процесс со СКВОЗНЫМ stdio и дождаться его кода.

    ``librarykit.proc.popen``, а не ``proc.run``: ``run`` захватывает потоки и
    закрывает stdin (правильно для фоновых вызовов демона, но здесь убило бы
    интерактивность и живой вывод навыка). Явные ``None`` в stdio — наследование
    дескрипторов CLI. Политика окон win32 (``CREATE_NO_WINDOW``) остаётся той
    же: она в ``popen``, а не в ``run``.
    """
    from librarykit import proc

    child = proc.popen(argv, cwd=cwd, stdin=None, stdout=None, stderr=None)
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

    cfg = ClientConfig.load()
    skill_dir = _skill_dir(cfg, slug)
    meta = _read_meta(skill_dir)
    component_id, version = _identity(skill_dir, slug, meta)
    entries = _cli_entries(meta)

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


__all__ = ["EXIT_INTERRUPTED", "KIND_SKILL_RUN", "cmd_run", "register"]
