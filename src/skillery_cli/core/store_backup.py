"""Резерв каталога навыка в сторе + откат (#1405).

ЗАЧЕМ. Установка из Хаба обязана становиться ГЛАВНОЙ: если целевое имя уже
занято навыком ИЗ ДРУГОГО ИСТОЧНИКА (``skillery install --path`` автора,
``--from-git``), хабовая версия всё равно должна поехать. Но «поехать» ≠
«затереть»: до этой правки хаб-установка входила в ветку инкрементального
обновления и МОЛЧА перезаписывала чужое дерево поверх — вернуть было нечего.

Здесь — механика «не затираем молча»: прежний каталог целиком уезжает в резерв
ВНУТРИ стора, после чего хаб ставится с чистого листа, а пользователю остаётся
команда отката.

ЧТО ТАКОЕ «ДАННЫЕ ПОЛЬЗОВАТЕЛЯ» И ПОЧЕМУ РЕЗЕРВ — ЦЕЛИКОМ. Единого правила
хранения состояния у навыков НЕТ (проверено на живой машине владельца):

* ``atlas`` держит портфель задач в ``~/.atlas/atlas.db`` — СНАРУЖИ каталога
  навыка; подмена каталога его не касается вовсе;
* ``telegram-content-cli`` держит ``.env`` (секреты) ВНУТРИ каталога;
* ``gemini-chat-cli`` — ``_local/``; браузерные навыки — ``browser_profiles/`` и
  ``.browser_profile_<имя>/``; почти у всех внутри лежит ``.venv/``.

Выборочно «сохранить нужное» здесь нельзя — список того, что нужно, знает
только сам навык. Поэтому инвариант простой и проверяемый:

1. НИЧЕГО СНАРУЖИ каталога навыка не трогаем (``~/.atlas/atlas.db`` и подобное
   переживает замену по построению — мы туда не ходим);
2. ВСЁ, что было ВНУТРИ, уезжает в резерв целиком (ничего не удаляется);
3. известные пути состояния (``.env`` / ``_local/`` / профили браузера +
   ``preserved_paths`` манифеста и таргета) ПЕРЕНОСЯТСЯ в свежую установку,
   чтобы навык продолжил работать сразу, а не после ручного отката.

Модуль не ходит в сеть и не запускает подпроцессы.
"""
from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

#: Каталог резервов внутри стора. Точка в начале — служебная зона: сканеры
#: стора перечисляют навыки через :func:`iter_store_skill_dirs` и её пропускают
#: (иначе ``store list`` показал бы резерв навыком, а ``store gc`` — снёс его
#: как «никем не используемый»).
BACKUPS_DIR_NAME = ".backups"

#: Имя подпапки резерва, в которую переезжает сам каталог навыка.
_PAYLOAD_DIR = "skill"
_RECORD_FILE = "backup.json"

#: Пути состояния ПОЛЬЗОВАТЕЛЯ внутри каталога навыка — переносятся в свежую
#: установку. Список намеренно узкий: сюда попадает только то, что навык не
#: может восстановить сам (секреты, профили браузера, локальное состояние).
#: ``.venv`` / ``__pycache__`` / ``.pytest_cache`` / ``.pkgsrc`` — производные
#: артефакты, их пересоздаёт установка, и тащить их в новое дерево вредно.
USER_STATE_NAMES: tuple[str, ...] = (".env", "_local", "browser_profiles")
#: Префиксные правила (``.browser_profile_banks`` и т.п. — имя задаёт навык).
USER_STATE_PREFIXES: tuple[str, ...] = (".browser_profile",)


class BackupError(RuntimeError):
    """Резерв/откат не выполнен (нет резерва, нет прав, занят каталог)."""


def backups_root(store_dir: Path) -> Path:
    """Каталог резервов рядом с навыками (та же ФС ⇒ переезд — переименование)."""
    return Path(store_dir) / BACKUPS_DIR_NAME


def iter_store_skill_dirs(store_dir: Path) -> Iterator[Path]:
    """Каталоги НАВЫКОВ стора: без служебных (``.backups`` и прочих dot-папок).

    Единая точка перечисления: любой сканер стора обязан ходить через неё,
    иначе служебная зона снова попадёт в выдачу и под ``store gc``.
    """
    root = Path(store_dir)
    if not root.is_dir():
        return
    for d in sorted(root.iterdir(), key=lambda p: p.name):
        if not d.is_dir() or d.name.startswith("."):
            continue
        yield d


def is_user_state_name(name: str) -> bool:
    """``True`` — это состояние пользователя, а не код навыка."""
    if name in USER_STATE_NAMES:
        return True
    return any(name.startswith(p) for p in USER_STATE_PREFIXES)


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _unique_backup_dir(base: Path) -> Path:
    """Каталог резерва с уникальным id (две замены в одну секунду — не редкость)."""
    stamp = _stamp()
    candidate = base / stamp
    n = 1
    while candidate.exists():
        candidate = base / f"{stamp}-{n}"
        n += 1
    return candidate


def backup_store_skill(
    store_dir: Path,
    dir_name: str,
    *,
    reason: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Убрать каталог навыка стора в резерв. ``None`` — убирать было нечего.

    Каталог ПЕРЕМЕЩАЕТСЯ (не копируется): резерв лежит на той же ФС, поэтому
    переезд атомарен и не удваивает место. Если ОС переименовать не дала
    (кросс-девайс, открытые файлы) — копируем и удаляем исходник; не смогли
    удалить исходник — резерв всё равно валиден, а вызывающий получит запись.
    """
    src = Path(store_dir) / dir_name
    if not src.is_dir():
        return None
    base = backups_root(store_dir) / dir_name
    base.mkdir(parents=True, exist_ok=True)
    slot = _unique_backup_dir(base)
    slot.mkdir(parents=True)
    payload = slot / _PAYLOAD_DIR
    try:
        shutil.move(str(src), str(payload))
    except Exception:  # noqa: BLE001 — переименование не вышло → копия+снос
        shutil.copytree(src, payload, symlinks=True, dirs_exist_ok=True)
        shutil.rmtree(src, ignore_errors=True)
    meta = meta or {}
    record = {
        "id": slot.name,
        "dir_name": dir_name,
        "slug": meta.get("slug") or dir_name,
        "source": meta.get("source"),
        "version": meta.get("version"),
        "reason": reason,
        "created_at": datetime.now(UTC).isoformat(),
        "path": str(slot),
        "skill_path": str(payload),
        "original_path": str(src),
    }
    (slot / _RECORD_FILE).write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return record


def _read_record(slot: Path) -> dict[str, Any] | None:
    try:
        record = json.loads((slot / _RECORD_FILE).read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — битая/отсутствующая запись
        return None
    if not isinstance(record, dict):
        return None
    # Пути могли переехать вместе со стором — пересчитываем от факта на диске.
    record["path"] = str(slot)
    record["skill_path"] = str(slot / _PAYLOAD_DIR)
    return record


def list_backups(
    store_dir: Path, dir_name: str | None = None
) -> list[dict[str, Any]]:
    """Резервы (свежие первыми). ``dir_name`` — только по одному навыку."""
    root = backups_root(store_dir)
    if not root.is_dir():
        return []
    names = [dir_name] if dir_name else sorted(p.name for p in root.iterdir() if p.is_dir())
    out: list[dict[str, Any]] = []
    for name in names:
        holder = root / str(name)
        if not holder.is_dir():
            continue
        for slot in sorted(holder.iterdir(), key=lambda p: p.name, reverse=True):
            if not slot.is_dir():
                continue
            record = _read_record(slot)
            if record is not None:
                out.append(record)
    return out


def find_backup(
    store_dir: Path, dir_name: str, backup_id: str | None = None
) -> dict[str, Any] | None:
    """Резерв навыка: конкретный по ``backup_id``, иначе САМЫЙ СВЕЖИЙ."""
    items = list_backups(store_dir, dir_name)
    if not items:
        return None
    if backup_id is None:
        return items[0]
    for record in items:
        if record.get("id") == backup_id:
            return record
    return None


def restore_backup(
    store_dir: Path, dir_name: str, backup_id: str | None = None
) -> dict[str, Any]:
    """Вернуть навык из резерва. Текущий каталог уезжает в НОВЫЙ резерв.

    Откат сам обратим: то, что стояло на момент отката (обычно хаб-версия),
    не удаляется, а сохраняется тем же механизмом — вернуться назад можно той
    же командой.
    """
    record = find_backup(store_dir, dir_name, backup_id)
    if record is None:
        raise BackupError(
            f"Резервной копии навыка «{dir_name}» нет"
            + (f" (id={backup_id})" if backup_id else "")
        )
    payload = Path(record["skill_path"])
    if not payload.is_dir():
        raise BackupError(f"Резерв повреждён: нет каталога {payload}")

    target = Path(store_dir) / dir_name
    replaced: dict[str, Any] | None = None
    if target.exists():
        from skillkit.installer import read_meta

        current_meta = None
        try:
            current_meta = read_meta(target)
        except Exception:  # noqa: BLE001 — битая мета не мешает откату
            current_meta = None
        replaced = backup_store_skill(
            store_dir, dir_name, reason="restore", meta=current_meta or {}
        )
    try:
        shutil.move(str(payload), str(target))
    except Exception:  # noqa: BLE001 — переименование не вышло → копия
        shutil.copytree(payload, target, symlinks=True, dirs_exist_ok=True)
        shutil.rmtree(payload, ignore_errors=True)
    # Слот резерва отдал содержимое — его метку убираем, чтобы список не врал.
    shutil.rmtree(Path(record["path"]), ignore_errors=True)
    return {"restored": record, "replaced": replaced, "path": str(target)}


def carry_over_user_state(
    backup_record: dict[str, Any],
    new_dir: Path,
    *,
    extra_preserved: tuple[str, ...] = (),
) -> list[str]:
    """Перенести состояние пользователя из резерва в свежую установку.

    Переносится ТОЛЬКО то, чего в новой установке нет (хабовая версия могла
    привезти свой ``_local/`` — её содержимое сильнее). Возвращает имена
    перенесённых путей; ошибка на одном пути не срывает перенос остальных.
    """
    src_root = Path(backup_record.get("skill_path") or "")
    dst_root = Path(new_dir)
    if not src_root.is_dir() or not dst_root.is_dir():
        return []
    wanted = {p.strip().strip("/").replace("\\", "/") for p in extra_preserved}
    wanted.discard("")
    moved: list[str] = []
    for item in sorted(src_root.iterdir(), key=lambda p: p.name):
        name = item.name
        if not (is_user_state_name(name) or name in wanted):
            continue
        dst = dst_root / name
        if dst.exists():
            continue
        try:
            if item.is_dir():
                shutil.copytree(item, dst, symlinks=True)
            else:
                shutil.copy2(item, dst)
        except Exception:  # noqa: BLE001 — один путь не срывает остальные
            continue
        moved.append(name)
    return moved


__all__ = [
    "BACKUPS_DIR_NAME",
    "BackupError",
    "USER_STATE_NAMES",
    "USER_STATE_PREFIXES",
    "backup_store_skill",
    "backups_root",
    "carry_over_user_state",
    "find_backup",
    "is_user_state_name",
    "iter_store_skill_dirs",
    "list_backups",
    "restore_backup",
]
