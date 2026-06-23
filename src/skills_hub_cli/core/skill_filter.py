"""Тонкий re-export shim: фильтр навыков переехал в кит ``skillkit.filter``.

cli-kits W7: .skillignore / files-allowlist фильтр теперь в ките. Здесь —
реэкспорт, чтобы прежние импорты ``from skills_hub_cli.core.skill_filter import
...`` не ломались.
"""
from __future__ import annotations

from skillkit.filter import (  # noqa: F401
    SKILLIGNORE_FILENAME,
    apply_skill_filter,
    parse_skill_md_files_allowlist,
)
