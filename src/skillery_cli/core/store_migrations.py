"""Одноразовые идемпотентные починки меты локального стора навыков (#1145).

ПОЧЕМУ ЭТО ВООБЩЕ НУЖНО. Автообновление хаб-навыков отбирает кандидатов строго
по метке источника::

    hub_skills = [s for s in _collect_store_skills(root) if s.get("source") == "hub"]

До ``s-skillkit`` 0.3.3 установка ИЗ СНАПШОТА хаба помечалась ``source
= "local-path"`` (кит видел на входе распакованный каталог и честно писал «из
локального пути»), и такие навыки МОЛЧА выпадали из автообновления: команд
никто не отменял, ошибок никто не печатал — навык просто навсегда застывал на
той версии, с которой приехал. Кит 0.3.3 это починил для НОВЫХ установок, но
уже лежащие на диске меты он задним числом не переписывает.

ПРИЗНАК, ПО КОТОРОМУ МИГРИРУЕМ, И ПОЧЕМУ ИМЕННО ОН. ``skill_id`` в мете
проставляется ТОЛЬКО когда навык приехал из хаба (у хаба он и берётся). Значит
пара «``source == "local-path"`` И непустой ``skill_id``» однозначно опознаёт
хаб-установку, ошибочно помеченную локальной.

Чего делать НЕЛЬЗЯ:

* мигрировать по ``repo_url`` — на снапшот-ветке его нет (``None``), признак
  просто не сработал бы;
* трогать записи БЕЗ ``skill_id`` — это настоящие ``skillery install
  --local-path`` авторов навыков. Пометить их ``hub`` значило бы отдать чужой
  рабочий каталог фоновому авто-апдейту, который перезапишет его содержимым из
  хаба. Поэтому ``skill_id`` — не «удобная эвристика», а гарантия невредимости.

Миграция идемпотентна (второй прогон находит 0 записей) и никогда не валит
вызывающего: стор пользователя важнее любой починки меты.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from skillkit.installer import read_meta, write_meta

#: Источник, ошибочно проставленный снапшот-установкам до s-skillkit 0.3.3.
_WRONG_SOURCE = "local-path"
#: Правильный источник хаб-установки (его же пишет кит 0.3.3+).
_HUB_SOURCE = "hub"


def _needs_migration(meta: dict[str, Any] | None) -> bool:
    """``True`` — это хаб-установка, ошибочно помеченная ``local-path``.

    Оба условия обязательны: метка источника И наличие ``skill_id``. Ослабить
    любое — значит начать переписывать чужие local-path установки.
    """
    if not meta:
        return False
    if meta.get("source") != _WRONG_SOURCE:
        return False
    skill_id = meta.get("skill_id")
    return bool(str(skill_id).strip()) if skill_id is not None else False


def migrate_local_path_to_hub(store_dir: Path) -> list[str]:
    """Переписать ``source`` на ``"hub"`` у ошибочно помеченных навыков стора.

    Возвращает список ``ref`` (slug либо имя каталога) мигрированных записей —
    вызывающий логирует их количество. Битая мета отдельной папки пропускается,
    а не роняет проход по остальным.
    """
    migrated: list[str] = []
    store = Path(store_dir)
    if not store.is_dir():
        return migrated
    for slug_dir in sorted(store.iterdir(), key=lambda p: p.name):
        if not slug_dir.is_dir():
            continue
        try:
            meta = read_meta(slug_dir)
        except Exception:  # noqa: BLE001 — битый JSON соседа не наше дело
            continue
        if not _needs_migration(meta):
            continue
        assert meta is not None  # гарантировано _needs_migration
        meta["source"] = _HUB_SOURCE
        try:
            write_meta(slug_dir, meta)
        except Exception:  # noqa: BLE001 — нет прав на файл → пропускаем
            continue
        migrated.append(str(meta.get("slug") or slug_dir.name))
    return migrated


def ensure_store_meta_migrated(cfg: Any) -> list[str]:
    """Прогнать миграцию ОДИН раз на профиль; повторные вызовы — no-op.

    Маркер выполнения — поле конфига ``store_meta_migrated`` (а не файл в
    сторе): стор может быть переехавшим/общим, а конфиг привязан к профилю,
    который и решает, каким стором пользоваться.

    Никогда не бросает: миграция меты — удобство, а не условие работы CLI.
    Ошибка записи конфига оставляет маркер невыставленным, и следующий запуск
    просто попробует снова (сама переписка меты идемпотентна).
    """
    try:
        if getattr(cfg, "store_meta_migrated", False):
            return []
        migrated = migrate_local_path_to_hub(cfg.effective_store_dir())
        cfg.store_meta_migrated = True
        cfg.save()
    except Exception:  # noqa: BLE001 — не роняем ни CLI, ни демон
        return []
    if migrated:
        try:
            from skillery_cli.core.logging_setup import get_logger

            get_logger("install").info(
                "миграция меты стора: source local-path → hub у %d навык(ов): %s",
                len(migrated),
                ", ".join(migrated),
            )
        except Exception:  # noqa: BLE001 — лог не должен валить миграцию
            pass
    return migrated


__all__ = ["ensure_store_meta_migrated", "migrate_local_path_to_hub"]
