"""Запись каталога на диск: только изменения, только UTF-8 и ``\\n``.

Повторный запуск импортёра обязан давать побайтово тот же результат — тогда
``git status`` после прогона чист, а ревью видит только настоящие правки.
``diff_catalog`` — та же проверка без записи: ею пользуется ``--check``.
"""
from __future__ import annotations

from pathlib import Path

from ..manifest import render_product
from .builder import Catalog
from .gaps import render_gaps_markdown

GAPS_FILENAME = "GAPS.md"


def diff_catalog(out_dir: Path, catalog: Catalog) -> list[Path]:
    """Вернуть файлы каталога, которые на диске отсутствуют или отличаются."""
    changed: list[Path] = []
    for relative, content in _planned_files(catalog):
        path = out_dir / relative
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            changed.append(path)
    return sorted(changed)


def write_catalog(out_dir: Path, catalog: Catalog) -> list[Path]:
    """Записать ``<slug>/product.toml`` и ``GAPS.md``; вернуть изменённые файлы."""
    changed = diff_catalog(out_dir, catalog)
    planned = dict(_planned_files(catalog))
    for path in changed:
        relative = path.relative_to(out_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(planned[relative], encoding="utf-8", newline="\n")
    return changed


def _planned_files(catalog: Catalog) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = [
        (Path(str(product["slug"])) / "product.toml", render_product(product))
        for product in catalog.products
    ]
    files.append((Path(GAPS_FILENAME), render_gaps_markdown(catalog)))
    return files
