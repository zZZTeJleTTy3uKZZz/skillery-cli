"""Коллизии имён в PATH: чужой бинарь перекрывает shim навыка (#1249).

ЗАЧЕМ. С #1221 факт вызова навыка фиксирует КОД: shim ``<bin>/<cmd>[.cmd]``
зовёт ``skillery run <slug> --command <cmd>``. Но выигрывает не тот, кто «наш»,
а тот, кто в PATH РАНЬШЕ. Реальный случай: пользователь ставит одноимённый пакет
через ``pipx``/``uv tool`` в ``~/.local/bin``, и этот каталог стоит выше
``~/.skillery/bin``. Пользователь набирает ``atlas`` — попадает мимо навыка и
мимо учёта, причём МОЛЧА: метрика занижена, и по данным это неотличимо от «навык
не запускали».

ЧТО ДЕЛАЕМ И ЧЕГО НЕ ДЕЛАЕМ.

* НЕ трогаем чужой каталог. ``~/.local/bin`` — территория pipx/uv, перезаписать
  там файл значило бы угнать чужую команду. Никогда.
* НЕ переставляем PATH сам собой. Одноимённая программа вполне может быть ДРУГИМ
  инструментом, который пользователь поставил осознанно; тихо задвинуть её —
  такая же порча окружения, как и тихая потеря учёта. Порядок правится только по
  явной команде (``skillery doctor --fix-path-order``) и с бэкапом.
* ЗАТО не молчим. Коллизия видна в ``doctor``, в ``status`` и сразу при
  установке навыка — то есть ДО того, как пользователь начнёт гадать, почему
  «запусков ноль».

НИКОГДА НЕ БРОСАЕМ. Диагностика не имеет права уронить ни CLI, ни демона:
:func:`detect` ловит всё и отдаёт пустой список — предупреждение, а не
исключение.
"""
from __future__ import annotations

import contextlib
import logging
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_LOG = logging.getLogger(__name__)

#: Расширения исполняемых на Windows (когда ``PATHEXT`` пуст/недоступен).
_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"

IS_WINDOWS = sys.platform == "win32"


@dataclass(frozen=True)
class Collision:
    """Одна коллизия: команда навыка, перекрытая посторонним исполняемым.

    ``winner`` — что реально запустится, когда пользователь наберёт ``command``.
    ``shim`` — наш shim, до которого дело не доходит (может быть ``None``, если
    шим ещё не выложен, но sidecar уже есть).
    """

    command: str
    skill_slug: str
    winner: str
    winner_dir: str
    shim: str | None = None

    def as_dict(self) -> dict[str, str]:
        return {
            "command": self.command,
            "skill_slug": self.skill_slug,
            "winner": self.winner,
            "winner_dir": self.winner_dir,
            "shim": self.shim or "",
        }


# --------------------------------------------------------------------------
#  Разбор PATH
# --------------------------------------------------------------------------
def _norm(p: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _path_dirs(path_value: str | None = None) -> list[str]:
    raw = os.environ.get("PATH", "") if path_value is None else path_value
    return [p for p in raw.split(os.pathsep) if p.strip()]


def _pathext() -> list[str]:
    if not IS_WINDOWS:
        return [""]
    raw = os.environ.get("PATHEXT") or _DEFAULT_PATHEXT
    exts = [e for e in (x.strip() for x in raw.split(os.pathsep)) if e]
    # Пустое расширение тоже проверяем: в PATH встречаются файлы без него.
    return [*exts, ""]


def _real_names(directory: Path) -> dict[str, str]:
    """``{ключ_поиска: реальное_имя_файла}`` для каталога.

    Ключ регистронезависим только на Windows — там же и берём НАСТОЯЩЕЕ имя с
    диска. Собирать путь из шаблона ``command + ext`` нельзя: ``PATHEXT`` пишет
    расширения капсом, и пользователю показался бы несуществующий
    ``atlas.CMD`` вместо лежащего рядом ``atlas.cmd``.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return {}
    if IS_WINDOWS:
        return {n.lower(): n for n in names}
    return {n: n for n in names}


def _executable_in(directory: str, command: str) -> Path | None:
    """Первый исполняемый файл с именем ``command`` в каталоге (или ``None``).

    Windows — перебор ``PATHEXT`` в его же порядке (так же ищет и cmd.exe).
    POSIX — файл с битом ``x``.
    """
    base = Path(directory)
    names = _real_names(base)
    if not names:
        return None
    for ext in _pathext():
        key = f"{command}{ext}"
        real = names.get(key.lower() if IS_WINDOWS else key)
        if real is None:
            continue
        candidate = base / real
        try:
            if not candidate.is_file():
                continue
            if IS_WINDOWS or os.access(candidate, os.X_OK):
                return candidate
        except OSError:  # битая запись PATH не должна валить обход
            continue
    return None


# --------------------------------------------------------------------------
#  Детект
# --------------------------------------------------------------------------
def _installed_commands() -> list[tuple[str, str]]:
    """``[(command_name, skill_slug), …]`` для установленных CLI навыков."""
    from skillery_cli.core import path_store

    out: list[tuple[str, str]] = []
    for entry in path_store.list_clis():
        name = str(entry.get("command_name") or "").strip()
        if not name:
            continue
        out.append((name, str(entry.get("skill_slug") or "").strip()))
    return out


def _shim_file(bin_dir: Path, command: str) -> str | None:
    shim = bin_dir / (f"{command}.cmd" if IS_WINDOWS else command)
    with contextlib.suppress(OSError):
        if shim.exists():
            return str(shim)
    return None


def detect(
    *,
    commands: list[tuple[str, str]] | None = None,
    bin_dir: Path | None = None,
    path_value: str | None = None,
) -> list[Collision]:
    """Коллизии для установленных CLI навыков. НИКОГДА не бросает.

    Каталог шимов ищется в PATH; всё, что стоит ДО него, проверяется на
    одноимённый исполняемый файл. Если каталога шимов в PATH нет вовсе, любой
    найденный одноимённый файл — коллизия: shim не выиграет ниоткуда.

    Аргументы существуют ради тестов и переиспользования; в проде все три
    берутся из окружения.
    """
    try:
        return _detect_unsafe(
            commands=commands, bin_dir=bin_dir, path_value=path_value
        )
    except Exception as exc:  # noqa: BLE001 — диагностика не роняет команду
        _LOG.debug("проверка PATH-коллизий не выполнена: %s", exc)
        return []


def _detect_unsafe(
    *,
    commands: list[tuple[str, str]] | None,
    bin_dir: Path | None,
    path_value: str | None,
) -> list[Collision]:
    if bin_dir is None:
        # Импорт ИМЕННО CLI-алиаса, а не голого кита: он тянет ``_kit_config``,
        # который инъектирует в кит bin_dir-провайдер бренда. Без этого каталог
        # уехал бы в standalone-дефолт кита — и коллизии искались бы не там.
        from skillery_cli.core import path_store

        bin_dir = path_store.bin_dir()
    pairs = _installed_commands() if commands is None else list(commands)
    if not pairs:
        return []

    dirs = _path_dirs(path_value)
    want = _norm(bin_dir)
    # Первое вхождение нашего каталога; нет в PATH → «бесконечно далеко».
    try:
        limit = next(i for i, d in enumerate(dirs) if _norm(d) == want)
    except StopIteration:
        limit = len(dirs)

    out: list[Collision] = []
    for command, slug in pairs:
        for directory in dirs[:limit]:
            if _norm(directory) == want:
                continue
            found = _executable_in(directory, command)
            if found is None:
                continue
            out.append(
                Collision(
                    command=command,
                    skill_slug=slug or command,
                    winner=str(found),
                    winner_dir=str(Path(directory)),
                    shim=_shim_file(bin_dir, command),
                )
            )
            break  # важен только ПЕРВЫЙ победитель — он и запустится
    return out


def detect_for_command(command: str, skill_slug: str = "") -> list[Collision]:
    """Коллизии для ОДНОЙ команды (точка вызова — сразу после установки навыка).

    Слаг, если не передан, берётся из sidecar'а: имя команды ему не равно
    (``nlm`` → навык ``notebooklm``), а рецепт «зовите skillery run <slug>»
    обязан называть НАСТОЯЩИЙ слаг, иначе он не сработает.
    """
    slug = skill_slug
    if not slug:
        try:
            slug = next(
                (s for name, s in _installed_commands() if name == command and s), ""
            )
        except Exception as exc:  # noqa: BLE001 — стор мог не прочитаться
            _LOG.debug("слаг для команды %s не определён: %s", command, exc)
    return detect(commands=[(command, slug or command)])


# --------------------------------------------------------------------------
#  Текст для пользователя
# --------------------------------------------------------------------------
def describe(collision: Collision) -> str:
    """Дословное предупреждение пользователю (одно и то же во всех точках)."""
    return (
        f"Команда «{collision.command}» перехвачена посторонним файлом "
        f"{collision.winner}: его каталог стоит в PATH раньше каталога команд "
        f"навыков. Вызов «{collision.command}» уходит мимо навыка "
        f"«{collision.skill_slug}» и НЕ учитывается как запуск. "
        f"Починить: skillery doctor --fix-path-order (поднимет каталог навыков "
        f"выше в PATH пользователя; чужой каталог не трогаем, прежний PATH "
        f"сохраняется в бэкап). Оставить как есть — зовите навык явно: "
        f"skillery run {collision.skill_slug}"
    )


def summary(collisions: list[Collision]) -> str:
    """Короткая сводка для ``doctor``/``status`` (когда коллизий несколько)."""
    if not collisions:
        return ""
    head = ", ".join(f"{c.command} → {c.winner}" for c in collisions)
    return (
        f"перехвачено команд: {len(collisions)} ({head}). Эти вызовы идут мимо "
        f"навыка и НЕ учитываются. Починить: skillery doctor --fix-path-order"
    )


# --------------------------------------------------------------------------
#  Правка порядка PATH (только по явной команде, с бэкапом)
# --------------------------------------------------------------------------
def _backup_dir(bin_dir: Path) -> Path:
    """Куда класть бэкап прежнего PATH — рядом со стором, в нашей зоне."""
    return bin_dir.parent


def fix_path_order(*, bin_dir: Path | None = None) -> dict[str, str]:
    """Поднять каталог шимов на ПЕРВОЕ место в PATH пользователя.

    Обратимость — обязательна: прежнее значение User PATH пишется в файл
    ``path-backup-<utc>.txt`` рядом со стором, путь к нему возвращается в
    ``backup`` и печатается пользователю. Откат — вернуть содержимое файла в
    переменную Path пользователя.

    Чужие каталоги мы не удаляем и их содержимое не трогаем: меняется ТОЛЬКО
    порядок в собственной переменной пользователя.

    ``status``: ``reordered`` | ``already`` | ``manual-needed`` (+ ``instruction``).
    """
    from skillery_cli.core import path_store

    if bin_dir is None:
        bin_dir = path_store.bin_dir()
    base = {"bin_dir": str(bin_dir)}

    if not IS_WINDOWS:
        # POSIX: rc-строка кита уже ПРЕПЕНДИТ каталог (`export PATH="<bin>:$PATH"`).
        # Раз коллизия всё же есть — чужой export идёт НИЖЕ по rc и перебивает
        # наш. Молча переписывать пользовательский rc не будем.
        return {
            **base,
            "status": "manual-needed",
            "instruction": (
                f'перенесите строку export PATH="{bin_dir}:$PATH" в КОНЕЦ '
                f"вашего ~/.zshrc / ~/.bashrc / ~/.profile (ниже строк, "
                f"добавляющих другие каталоги), затем перезапустите терминал"
            ),
        }

    read = getattr(path_store, "_win_read_user_path", None)
    write = getattr(path_store, "_win_write_user_path", None)
    if not callable(read) or not callable(write):
        return {**base, "status": "manual-needed", "instruction": _manual_hint(bin_dir)}

    current = str(read() or "")
    parts = [p for p in current.split(";") if p.strip()]
    want = _norm(bin_dir)
    if parts and _norm(parts[0]) == want:
        return {**base, "status": "already"}

    rest = [p for p in parts if _norm(p) != want]
    new_value = ";".join([str(bin_dir), *rest])

    backup = ""
    with contextlib.suppress(OSError):
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = _backup_dir(bin_dir) / f"path-backup-{stamp}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(current, encoding="utf-8")
        backup = str(target)
    if not backup:
        # Без бэкапа операция перестаёт быть обратимой — не делаем её вовсе.
        return {**base, "status": "manual-needed", "instruction": _manual_hint(bin_dir)}

    try:
        write(new_value)
    except OSError as exc:
        return {
            **base,
            "status": "manual-needed",
            "instruction": _manual_hint(bin_dir),
            "error": str(exc),
            "backup": backup,
        }
    return {**base, "status": "reordered", "backup": backup}


def _manual_hint(bin_dir: Path) -> str:
    return (
        f"откройте «Изменение переменных среды текущего пользователя» → Path и "
        f"переместите {bin_dir} выше остальных каталогов, затем перезапустите "
        f"терминал"
    )


__all__ = [
    "Collision",
    "describe",
    "detect",
    "detect_for_command",
    "fix_path_order",
    "summary",
]
