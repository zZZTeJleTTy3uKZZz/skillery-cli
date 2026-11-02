"""Сгенерированный каталог ``catalog/products`` — контракт для публикации.

Тест не ходит ни в чужие реестры, ни в сеть: он проверяет то, что лежит в
репозитории. Гарантии:

- каждый ``product.toml`` парсится ``tomllib`` и проходит схему ADR-0001;
- каталог каталога (``dir == slug``) не разъезжается с содержимым;
- в каталоге есть все продукты, которые импортёр обязан был завести;
- ``GAPS.md`` перечисляет каждый артефакт с пустым ``url``/``sha256`` —
  пробел в манифесте не может остаться незамеченным.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from skillery_cli.products.manifest import validate_product

CATALOG_DIR = Path(__file__).resolve().parents[2] / "catalog" / "products"

EXPECTED_SLUGS = {
    # CLI (снимок фактов cli_catalog)
    "atlas",
    "crew",
    "transcribe",
    "gateway",
    "line",
    "beget",
    "b24",
    "skillery",
    "max",
    "gws",
    "keyring",
    "herdr",
    # продукт, собранный из реестра: служба + плагин
    "dictation",
    # плагины Harness (registry.json: plugins)
    "scratchpad",
    "notify",
    "job-wait",
    "limits",
    "crew-integration",
    "dsh-interface-scale",
    "dsh-russian-locale",
    "dsh-window-controls",
    "wsl-bash",
    "sessions",
    "background-habits",
    "crew-devices",
    # плагины Claude Code (registry.json: claudePlugins)
    "limits-cache",
    "win-clipboard",
    "subagent-model",
    "sleep-to-background",
    "turn-actions",
    "delegation",
}


def _manifest_paths() -> list[Path]:
    assert CATALOG_DIR.is_dir(), f"нет каталога продуктов: {CATALOG_DIR}"
    return sorted(CATALOG_DIR.glob("*/product.toml"))


def test_catalog_has_expected_products() -> None:
    slugs = {path.parent.name for path in _manifest_paths()}
    assert slugs >= EXPECTED_SLUGS


@pytest.mark.parametrize("path", _manifest_paths(), ids=lambda p: p.parent.name)
def test_manifest_parses_and_validates(path: Path) -> None:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    errors = validate_product(data)
    assert errors == [], f"{path}: {errors}"
    assert data["slug"] == path.parent.name


def test_no_duplicate_product_slugs() -> None:
    slugs = [
        tomllib.loads(path.read_text(encoding="utf-8"))["slug"]
        for path in _manifest_paths()
    ]
    assert len(slugs) == len(set(slugs))


def test_every_product_has_component_and_artifact() -> None:
    for path in _manifest_paths():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        assert data["components"], path
        for component in data["components"]:
            assert component["artifacts"], f"{path}: {component['key']}"


def test_gaps_report_covers_every_empty_artifact() -> None:
    gaps_path = CATALOG_DIR / "GAPS.md"
    assert gaps_path.is_file(), "импортёр обязан писать catalog/products/GAPS.md"
    text = gaps_path.read_text(encoding="utf-8")
    assert text.startswith("# Пробелы каталога продуктов")

    missing: list[str] = []
    for path in _manifest_paths():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for component in data["components"]:
            for artifact in component["artifacts"]:
                if artifact["kind"] not in {"binary", "archive", "installer"}:
                    continue
                if artifact.get("url", "").strip() and artifact.get("sha256", "").strip():
                    continue
                row = f"| {data['slug']} | {component['key']} |"
                if row not in text:
                    missing.append(f"{data['slug']}/{component['key']}/{artifact['os']}")
    assert missing == []


def test_gaps_report_names_known_blockers() -> None:
    text = (CATALOG_DIR / "GAPS.md").read_text(encoding="utf-8")
    # У диктовки нет релиза установщика, у limits — зависимости 9router.
    assert "dictation-service" in text
    assert "9router" in text
    assert "sha256" in text
