"""Локальные коллекции навыков — БЕЗ хаба и сети (P1 C4 / E10).

Хранилище: ``<config_dir>/collections.toml`` — та же папка, что ``config.toml``
(через ``config._default_config_dir`` уважает ``SKILLS_HUB_CONFIG_DIR`` и
профили ``--profile``/``SKILLS_HUB_PROFILE``).

Формат:
    [collections.<name>]
    title = "…"
    skills = ["slug1", "slug2"]

Имя коллекции — slug-подобное (буквы/цифры/``-``/``_``): оно становится ключом
toml-таблицы. Слаги навыков НЕ валидируются против хаба — коллекция может
ссылаться на навыки, которых (ещё) нет в локальном сторе; команды показывают
warning, ``install-local`` докачивает недостающее из хаба (если залогинен).
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import tomli_w

from skills_hub_cli.config import _default_config_dir

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


class LocalCollectionError(ValueError):
    """Ожидаемая ошибка операции: ``code`` + ``message`` для ``emit_error``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def collections_path() -> Path:
    """Путь файла локальных коллекций (рядом с config.toml профиля)."""
    return _default_config_dir() / "collections.toml"


def _normalize(body: dict[str, Any], name: str) -> dict[str, Any]:
    """Приводит запись к канону {title: str, skills: [str]} (дедуп слагов)."""
    skills: list[str] = []
    for s in body.get("skills") or []:
        s_str = str(s).strip()
        if s_str and s_str not in skills:
            skills.append(s_str)
    return {"title": str(body.get("title") or name), "skills": skills}


def load_all() -> dict[str, dict[str, Any]]:
    """{name: {"title": str, "skills": [slug, ...]}}; пусто если файла нет."""
    p = collections_path()
    if not p.exists():
        return {}
    try:
        # utf-8-sig — толерантность к BOM (как ClientConfig.load).
        data = tomllib.loads(p.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as e:
        raise LocalCollectionError("PARSE", f"{p} повреждён: {e}") from e
    raw = data.get("collections") or {}
    out: dict[str, dict[str, Any]] = {}
    for name, body in raw.items():
        if isinstance(body, dict):
            out[str(name)] = _normalize(body, str(name))
    return out


def save_all(collections: dict[str, dict[str, Any]]) -> None:
    """Пишет весь набор (ключи сортируются для стабильного diff)."""
    p = collections_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    ordered = {n: _normalize(collections[n], n) for n in sorted(collections)}
    p.write_text(tomli_w.dumps({"collections": ordered}), encoding="utf-8")


def validate_name(name: str) -> None:
    if not _NAME_RE.match(name or ""):
        raise LocalCollectionError(
            "VALIDATION",
            f"Имя коллекции «{name}» некорректно: допустимы буквы/цифры/«-»/«_» "
            "(без пробелов), первый символ — буква или цифра",
        )


def get(name: str) -> dict[str, Any] | None:
    """Коллекция по имени либо None."""
    return load_all().get(name)


def create(name: str, *, title: str | None = None) -> dict[str, Any]:
    """Создаёт пустую коллекцию. ALREADY_EXISTS если имя занято."""
    validate_name(name)
    collections = load_all()
    if name in collections:
        raise LocalCollectionError(
            "ALREADY_EXISTS", f"Локальная коллекция «{name}» уже существует"
        )
    collections[name] = {"title": title or name, "skills": []}
    save_all(collections)
    return collections[name]


def delete(name: str) -> dict[str, Any]:
    """Удаляет коллекцию (установленные навыки не трогает). NOT_FOUND если нет."""
    collections = load_all()
    if name not in collections:
        raise LocalCollectionError(
            "NOT_FOUND", f"Локальная коллекция «{name}» не найдена"
        )
    removed = collections.pop(name)
    save_all(collections)
    return removed


def add_skill(name: str, slug: str) -> tuple[dict[str, Any], bool]:
    """(коллекция, added). added=False если слаг уже был (идемпотентно)."""
    slug = str(slug or "").strip()
    if not slug:
        raise LocalCollectionError("VALIDATION", "Слаг навыка пуст")
    collections = load_all()
    if name not in collections:
        raise LocalCollectionError(
            "NOT_FOUND", f"Локальная коллекция «{name}» не найдена"
        )
    coll = collections[name]
    if slug in coll["skills"]:
        return coll, False
    coll["skills"].append(slug)
    save_all(collections)
    return coll, True


def remove_skill(name: str, slug: str) -> tuple[dict[str, Any], bool]:
    """(коллекция, removed). removed=False если слага не было."""
    collections = load_all()
    if name not in collections:
        raise LocalCollectionError(
            "NOT_FOUND", f"Локальная коллекция «{name}» не найдена"
        )
    coll = collections[name]
    if slug not in coll["skills"]:
        return coll, False
    coll["skills"] = [s for s in coll["skills"] if s != slug]
    save_all(collections)
    return coll, True


def missing_in_store(skills: list[str], store_dir: Path) -> list[str]:
    """Слаги, которых нет в локальном сторе.

    Критерий «есть в сторе» тот же, что у ``SkillInstaller.link_existing``:
    существует каталог ``<store>/<slug>`` (для slug-less навыка — числовой id).
    """
    root = Path(store_dir)
    return [s for s in skills if not (root / s).exists()]
