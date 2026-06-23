"""Тонкий re-export shim: проектный манифест переехал в кит ``skillkit.project``.

cli-kits W7: ``.skills-hub/skills.toml`` ридер/райтер теперь в ките. Здесь —
реэкспорт, чтобы прежние импорты ``from skills_hub_cli.core import
project_manifest`` не ломались.
"""
from __future__ import annotations

from skillkit.project import (  # noqa: F401
    MANIFEST_REL,
    add,
    list_,
    load,
    manifest_path,
    remove,
    save,
)
