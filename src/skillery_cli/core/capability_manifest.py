"""Блок ``[[capabilities]]`` манифеста навыка (#1489) — чтение и валидация.

Навык ОБЪЯВЛЯЕТ свои способности, а реестр хаба выводится из объявления при
публикации версии — ручного CRUD способностей нет и не будет: реестр, который
можно править отдельно от манифеста, гарантированно разъедется с реальными
entry-point'ами дистрибутива, и разъедется молча.

────────────────────────────────────────────────────────────────────────────
ПОЧЕМУ ПРОВЕРКА ЖИВЁТ И ЗДЕСЬ, ХОТЯ ОНА ЕСТЬ НА БЭКЕНДЕ

Не ради «второго мнения»: правила один в один зеркалят
``domain/skill/manifest.py::_parse_capabilities``, и расходиться им нельзя.
Дело в МОМЕНТЕ. Публикация — длинная: secret-scan, денилист, сборка списка
файлов, сеть. Узнать про опечатку в ``[[capabilities]]`` после всего этого, да
ещё сообщением о 422 с полем ``manifest.capabilities.1.name``, — значит
потерять проход и не понять, что чинить. Проверка до первого байта в сеть
стоит один разбор TOML и говорит на языке файла: строка, имя, что не так.

Второе: ошибку тут МОЖНО объяснить. Бэкенду про ``_skill_meta.toml``
пользователя ничего не известно — он видит DTO; здесь известен и файл, и
номер элемента блока.

────────────────────────────────────────────────────────────────────────────
ГЛОБАЛЬНАЯ УНИКАЛЬНОСТЬ ИМЕНИ — НЕ ЛОКАЛЬНАЯ ПРОВЕРКА

Дубль ВНУТРИ одного манифеста ловится здесь. А вот занятость имени ДРУГИМ
навыком локально не проверить: имя уникально на весь хаб, и знает об этом
только хаб (409 ``CAPABILITY_NAME_CONFLICT``). Задача CLI — не спрятать этот
ответ за кодом ошибки, а объяснить его человеку; текст живёт в
``commands/capability.py::NAME_CONFLICT_HINT``, чтобы не разъехаться на две
формулировки.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

#: Уровни доступа способности (зеркало ``_STRICTNESS`` бэкенда).
ACCESS_LEVELS = ("public", "restricted", "private")

#: Ключи, которые понимает бэкенд. Остальное — опечатка автора, и молчать о
#: ней нельзя: незнакомый ключ уедет в никуда, а автор будет уверен, что
#: объявил ``requires_lease``, написав ``require_lease``.
KNOWN_KEYS = frozenset(
    {
        "name",
        "entry_point",
        "kind",
        "title",
        "description",
        "access_level",
        "requires_lease",
    }
)


class CapabilityManifestError(ValueError):
    """Блок ``[[capabilities]]`` непригоден к публикации."""


def parse_capabilities(meta_toml: dict[str, Any]) -> list[dict[str, Any]]:
    """``[[capabilities]]`` → список DTO для публикации. Строго, без best-effort.

    В отличие от ``[[hooks]]``/``onboarding`` (там мусор молча отбрасывается,
    чтобы кривой необязательный блок не ронял установку всего навыка) здесь
    выбрана противоположная строгость, и по той же причине, что у бэкенда:
    способность без имени — это способность без адреса. Её нельзя ни выдать,
    ни отозвать, ни зарезолвить в лизе, и «молча пропустить» означало бы
    опубликовать версию, в которой обещанной способности просто нет.
    """
    raw = meta_toml.get("capabilities")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise CapabilityManifestError(
            "[[capabilities]] должен быть массивом таблиц: "
            "[[capabilities]] name = \"grok_transcriber\""
        )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for position, item in enumerate(raw, start=1):
        where = f"[[capabilities]] #{position}"
        if not isinstance(item, dict):
            raise CapabilityManifestError(f"{where}: элемент должен быть таблицей")
        unknown = sorted(set(item) - KNOWN_KEYS)
        if unknown:
            raise CapabilityManifestError(
                f"{where}: неизвестные ключи {', '.join(unknown)}; "
                f"допустимо: {', '.join(sorted(KNOWN_KEYS))}"
            )
        name = str(item.get("name") or "").strip()
        if not name:
            raise CapabilityManifestError(
                f"{where}: нет обязательного 'name'. Имя способности = ключ "
                "entry-point группы skillery.plugins (grok_transcriber)"
            )
        if name in seen:
            raise CapabilityManifestError(
                f"{where}: дубль имени {name!r} в одном манифесте — "
                "останется только одна строка, и неизвестно какая"
            )
        seen.add(name)
        access_level = item.get("access_level")
        if access_level is not None and str(access_level) not in ACCESS_LEVELS:
            raise CapabilityManifestError(
                f"{where}: access_level {access_level!r} невалиден; "
                f"допустимо: {', '.join(ACCESS_LEVELS)}"
            )
        entry: dict[str, Any] = {"name": name}
        for key in ("entry_point", "kind", "title", "description"):
            value = item.get(key)
            if value:
                entry[key] = str(value)
        if access_level:
            entry["access_level"] = str(access_level)
        entry["requires_lease"] = bool(item.get("requires_lease", False))
        out.append(entry)
    return out


def capabilities_of(skill_dir: Path | str) -> list[dict[str, Any]]:
    """Способности, объявленные навыком в ``_skill_meta.toml``.

    Нет файла (prompt/comprehensive-навык) ⇒ пусто: способности объявляет
    tooling, и требовать манифест от навыка-инструкции незачем. Битый TOML
    ⇒ ошибка: его всё равно не переживёт сборка манифеста, но здесь сообщение
    называет файл.
    """
    path = Path(skill_dir) / "_skill_meta.toml"
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        raise CapabilityManifestError(f"{path} не парсится: {exc}") from exc
    return parse_capabilities(data)


__all__ = [
    "ACCESS_LEVELS",
    "KNOWN_KEYS",
    "CapabilityManifestError",
    "capabilities_of",
    "parse_capabilities",
]
