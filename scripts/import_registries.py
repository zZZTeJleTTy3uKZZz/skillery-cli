#!/usr/bin/env python3
"""Импорт существующих реестров в манифесты ``catalog/products`` (задача #5986).

Что делает:

1. читает реестры dsh-plugins — ``registry.json`` (плагины Harness и Claude
   Code) и ``.claude-plugin/marketplace.json`` (перекрёстная проверка);
2. добавляет CLI-продукты из снимка фактов ``products/importers/cli_catalog``
   и продукт «Диктовка» (служба + плагин voice-input);
3. пишет ``catalog/products/<slug>/product.toml`` и ``catalog/products/GAPS.md``
   только если содержимое изменилось.

Повторный запуск обязан не менять ни байта: это же проверяет ``--check``.

Примеры:

    uv run python scripts/import_registries.py
    uv run python scripts/import_registries.py --check
    uv run python scripts/import_registries.py --dsh-plugins ~/projects/dsh-plugins
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from skillery_cli.products.importers.builder import build_catalog  # noqa: E402
from skillery_cli.products.importers.dsh_registry import (  # noqa: E402
    RegistrySourceError,
    load_json,
)
from skillery_cli.products.importers.writer import (  # noqa: E402
    diff_catalog,
    write_catalog,
)
from skillery_cli.products.manifest import validate_product  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "catalog" / "products"


def default_dsh_plugins() -> Path:
    """Каталог dsh-plugins: env, затем известные раскладки этой машины."""
    env_value = os.environ.get("DSH_PLUGINS_DIR")
    if env_value:
        return Path(env_value).expanduser()
    candidates = (
        Path.home() / "projects" / "dsh-plugins",
        Path("/workspaces/dsh-plugins"),
    )
    for candidate in candidates:
        if (candidate / "registry.json").is_file():
            return candidate
    return candidates[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Импорт реестров в манифесты продуктов (ADR-0001).",
    )
    parser.add_argument(
        "--dsh-plugins",
        type=Path,
        default=default_dsh_plugins(),
        help="каталог репозитория dsh-plugins с registry.json (по умолчанию — env DSH_PLUGINS_DIR)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help="каталог продуктов (по умолчанию catalog/products)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="не писать, а только проверить, что каталог совпадает с источниками",
    )
    args = parser.parse_args(argv)

    try:
        registry = load_json(args.dsh_plugins / "registry.json")
        marketplace_path = args.dsh_plugins / ".claude-plugin" / "marketplace.json"
        marketplace = load_json(marketplace_path) if marketplace_path.is_file() else None
        catalog = build_catalog(registry, marketplace)
    except RegistrySourceError as exc:
        print(f"импорт не выполнен: {exc}", file=sys.stderr)
        return 2

    broken = {
        product["slug"]: validate_product(product) for product in catalog.products
    }
    broken = {slug: errors for slug, errors in broken.items() if errors}
    if broken:
        for slug, errors in broken.items():
            print(f"{slug}: {'; '.join(errors)}", file=sys.stderr)
        return 2

    components = sum(len(product["components"]) for product in catalog.products)
    summary = (
        f"продуктов: {len(catalog.products)}, компонентов: {components}, "
        f"пробелов: {len(catalog.gaps)}"
    )

    if args.check:
        changed = diff_catalog(args.out, catalog)
        if changed:
            print("каталог разошёлся с источниками:", file=sys.stderr)
            for path in changed:
                print(f"  {path}", file=sys.stderr)
            return 1
        print(f"каталог актуален ({summary})")
        return 0

    changed = write_catalog(args.out, catalog)
    print(f"каталог записан: изменено файлов {len(changed)} ({summary})")
    for path in changed:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
