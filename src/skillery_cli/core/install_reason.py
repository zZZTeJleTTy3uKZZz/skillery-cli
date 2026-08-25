"""#2282 — ПРИЧИНА установки навыка: явная или «приехал как зависимость».

Зачем это вообще. Установка навыка тянет объявленные им зависимости (эпик
skill→skill). До этой правки система не различала, ПОЧЕМУ навык оказался на
устройстве: и потребитель, и его зависимость ложились одинаково — в стор И в
зону агента. Владелец на живой проверке увидел последствие: ``hello-base``
приехал вместе с ``hello-consumer`` и появился в ``~/.claude/skills/`` как
самостоятельный навык — агент видел его в списке и мог позвать напрямую, хотя
это всего лишь расширение потребителя.

Модель — та же, что у менеджеров пакетов, а не своя выдумка:

* **apt / dpkg** — флаг ``auto`` / ``manual`` (``apt-mark``). Пакет, приехавший
  ради зависимости, помечен ``auto``; поставленный пользователем — ``manual``.
  ``apt autoremove`` сносит ``auto``-пакеты, которых больше никто не требует;
  ``manual`` не сносит НИКОГДА, даже когда его никто не требует.
* **pacman** — ``--asdeps`` при установке и ``-Qdt`` (orphans) при уборке;
* **Homebrew** — ``install --as-dependency`` + ``brew autoremove``.

Общее у всех трёх и у нас: (1) состояний ровно ДВА, (2) «явно» ПОБЕЖДАЕТ —
переход dependency→explicit возможен, обратный сам собой не происходит,
(3) уборка решается ОБРАТНЫМИ ссылками на момент удаления, а не хранением
forward-графа (граф успевает протухнуть, ссылки — нет).

Где хранится. В ``_skill_meta.json`` каталога стора — там же, где остальные
метаданные установки (версия, источник, agent, scope). Два поля:

* ``install_reason`` — ``"explicit"`` | ``"dependency"``;
* ``required_by`` — отсортированный список слагов потребителей, ради которых
  зависимость приехала (пусто у явной установки, которую никто не требует).

Совместимость с уже установленным. Запись БЕЗ ``install_reason`` читается как
``explicit`` — ровно как apt при вводе auto-флага считал всё уже стоящее
manual'ом. Это единственный безопасный дефолт: принять старую установку за
зависимость значит однажды снести её как «сироту».

Мету пишет кит (``skillkit``) при каждой материализации и наши поля не знает —
поэтому штамп накладывается ПОСЛЕ установки, поверх готового файла, и
повторяется при каждой переустановке (иначе update стирал бы причину).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Пользователь попросил этот навык сам — он полноценный, живёт в зоне агента.
EXPLICIT = "explicit"
#: Навык приехал только потому, что его требует другой — расширение, не навык.
DEPENDENCY = "dependency"

_META_FILE = "_skill_meta.json"
REASON_KEY = "install_reason"
REQUIRED_BY_KEY = "required_by"

#: Человеческая расшифровка для выдачи ``skillery skill installed``.
REASON_TITLES = {
    EXPLICIT: "явная установка",
    DEPENDENCY: "зависимость",
}


def _meta_path(store_dir: Path) -> Path:
    return Path(store_dir) / _META_FILE


def _load(store_dir: Path) -> dict[str, Any] | None:
    p = _meta_path(store_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _save(store_dir: Path, meta: dict[str, Any]) -> None:
    _meta_path(store_dir).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def reason_of(meta: dict[str, Any] | None) -> str:
    """Причина установки по мете. Нет поля ⇒ ``explicit`` (см. модуль-docstring)."""
    if not isinstance(meta, dict):
        return EXPLICIT
    value = meta.get(REASON_KEY)
    return DEPENDENCY if value == DEPENDENCY else EXPLICIT


def required_by_of(meta: dict[str, Any] | None) -> list[str]:
    """Кто требует навык (слаги потребителей). Пусто — никто не требует."""
    if not isinstance(meta, dict):
        return []
    raw = meta.get(REQUIRED_BY_KEY)
    if not isinstance(raw, list):
        return []
    return sorted({str(x) for x in raw if str(x)})


def read_reason(store_dir: Path) -> tuple[str, list[str]]:
    """``(причина, кто требует)`` для каталога стора. Нет каталога ⇒ explicit/[]."""
    meta = _load(store_dir)
    return reason_of(meta), required_by_of(meta)


def is_agent_visible(store_dir: Path) -> bool:
    """Должен ли навык быть виден агенту как самостоятельный.

    Виден ровно тогда, когда установлен ЯВНО. Приехавший только как зависимость
    лежит в сторе и доступен потребителю, но в зону агента не линкуется.
    """
    return read_reason(store_dir)[0] == EXPLICIT


def stamp(
    store_dir: Path,
    *,
    reason: str,
    required_by: str | None = None,
) -> str:
    """Проставить причину установки поверх меты кита. Возвращает ИТОГОВУЮ причину.

    Правило старшинства (apt: ``manual`` побеждает ``auto``): если навык уже
    помечен ``explicit``, повторный приезд в роли зависимости причину НЕ
    понижает — только добавляет потребителя в ``required_by``. Обратный переход
    (dependency → explicit) выполняется, как только пользователь ставит навык
    сам: это и есть «повышение до полноценного».

    Идемпотентно: повторный вызов с теми же аргументами ничего не меняет.
    """
    meta = _load(store_dir)
    if meta is None:
        # Меты нет — писать причину некуда и незачем (каталог не наш либо
        # установка не состоялась). Молчим: причина установки не важнее самой
        # установки.
        return reason
    previous = reason_of(meta)
    effective = EXPLICIT if EXPLICIT in (previous, reason) else DEPENDENCY
    consumers = set(required_by_of(meta))
    if required_by:
        consumers.add(str(required_by))
    meta[REASON_KEY] = effective
    meta[REQUIRED_BY_KEY] = sorted(consumers)
    _save(store_dir, meta)
    return effective


def forget_consumer(store_root: Path, consumer: str) -> list[str]:
    """Убрать ``consumer`` из ``required_by`` всех навыков стора.

    Возвращает слаги (= имена каталогов) навыков, которые после этого стали
    СИРОТАМИ: приехали как зависимость и больше никем не требуются. Явные
    (``explicit``) в выдачу не попадают НИКОГДА — пользователь ставил их сам,
    и уход потребителя не повод их сносить (инвариант ``apt autoremove``).
    """
    from skillery_cli.core.store_backup import iter_store_skill_dirs

    orphans: list[str] = []
    for d in iter_store_skill_dirs(Path(store_root)):
        if d.name == consumer:
            continue
        meta = _load(d)
        if meta is None:
            continue
        consumers = required_by_of(meta)
        if consumer in consumers:
            consumers = [c for c in consumers if c != consumer]
            meta[REQUIRED_BY_KEY] = consumers
            _save(d, meta)
        if reason_of(meta) == DEPENDENCY and not consumers:
            orphans.append(d.name)
    return orphans


__all__ = [
    "DEPENDENCY",
    "EXPLICIT",
    "REASON_TITLES",
    "REQUIRED_BY_KEY",
    "REASON_KEY",
    "forget_consumer",
    "is_agent_visible",
    "read_reason",
    "reason_of",
    "required_by_of",
    "stamp",
]
