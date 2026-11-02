"""Репозиторий продуктов: импортёры и запись каталога."""
from __future__ import annotations

from .builder import Catalog, build_catalog
from .gaps import Gap, render_gaps_markdown
from .writer import diff_catalog, write_catalog

__all__ = [
    "Catalog",
    "Gap",
    "build_catalog",
    "diff_catalog",
    "render_gaps_markdown",
    "write_catalog",
]
