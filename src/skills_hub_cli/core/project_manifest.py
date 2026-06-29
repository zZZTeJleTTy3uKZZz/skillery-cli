"""Тонкий re-export shim: проектный манифест переехал в кит ``skillkit.project``.

cli-kits W7: ``.skillery/skills.toml`` ридер/райтер теперь в ките. Здесь —
реэкспорт, чтобы прежние импорты ``from skills_hub_cli.core import
project_manifest`` не ломались.

Ребренд skills-hub→skillery: проектный манифест переехал ``.skills-hub`` →
``.skillery``. Кит остаётся брендо-нейтральным (его дефолт исторический
``.skills-hub``); CLI инъектирует НОВЫЙ путь ``.skillery/skills.toml`` как
канонический (write) + legacy ``.skills-hub/skills.toml`` как fallback для
чтения старых проектов. Существующие репозитории с ``.skills-hub/skills.toml``
продолжают читаться; запись (add/save) идёт уже в ``.skillery``.
"""
from __future__ import annotations

from pathlib import Path

from skillkit import project as _kit_project

# Канонический (write) путь — новый бренд; чтение fallback'ит на legacy.
_NEW_MANIFEST_REL = Path(".skillery") / "skills.toml"
_LEGACY_MANIFEST_REL = Path(".skills-hub") / "skills.toml"

_kit_project.set_manifest_rel_provider(lambda: _NEW_MANIFEST_REL)
_kit_project.set_manifest_fallbacks((_LEGACY_MANIFEST_REL,))

from skillkit.project import (  # noqa: E402, F401
    MANIFEST_REL,
    add,
    list_,
    load,
    manifest_path,
    remove,
    save,
)
