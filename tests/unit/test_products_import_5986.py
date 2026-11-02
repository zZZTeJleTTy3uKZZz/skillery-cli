"""Импортёр реестров в манифесты продуктов (ADR-0001, задача #5986).

Источники — ``dsh-plugins/registry.json`` и ``.claude-plugin/marketplace.json``:
плагины Harness и Claude Code становятся продуктами, ``requires`` — связями
``depends_on``, ``peerRange`` — метаданными установки. CLI-продукты приходят из
рукописного каталога фактов (``cli_catalog``): версия, пакет/URL и навык с тем
же slug.

Тесты намеренно работают на фикстурах, а не на живых реестрах: прогон в CI не
должен зависеть от чужого рабочего дерева. Коммитнутый каталог проверяется
отдельным тестом — он читает ``catalog/products`` из репозитория.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from skillery_cli.products.importers import cli_catalog, dsh_registry
from skillery_cli.products.importers.builder import Catalog, build_catalog
from skillery_cli.products.importers.gaps import Gap, render_gaps_markdown
from skillery_cli.products.importers.writer import diff_catalog, write_catalog
from skillery_cli.products.manifest import validate_product

REGISTRY: dict[str, Any] = {
    "registry": 1,
    "updated": "2026-11-02",
    "plugins": [
        {
            "name": "voice-input",
            "version": "1.4.1",
            "path": "plugins/voice-input",
            "summary": "диктовка в Harness",
            "requires": {"dictation": "1.10.0"},
            "peerRange": "не объявлено — сцепка установщиком не проверяется",
            "enabled": True,
        },
        {
            "name": "atlas",
            "version": "0.1.0",
            "path": "plugins/atlas",
            "summary": "Atlas внутри Harness",
            "requires": {"cli": "atlas"},
            "peerRange": "^0.2.0",
            "enabled": True,
        },
        {
            "name": "limits",
            "version": "0.5.0",
            "path": "plugins/limits",
            "summary": "лимиты",
            "requires": {"router": "9router 0.5.99", "service": "subs — реестр подписок"},
            "enabled": True,
        },
    ],
    "claudePlugins": [
        {
            "name": "limits-cache",
            "version": "1.0.0",
            "path": "claude/limits-cache",
            "summary": "кеш лимитов",
        }
    ],
}

MARKETPLACE: dict[str, Any] = {
    "name": "dsh-plugins",
    "plugins": [
        {
            "name": "limits-cache",
            "source": "./claude/limits-cache",
            "description": "Keeps the shared rate-limit cache fresh",
        }
    ],
}

CLI_PRODUCTS: list[dict[str, Any]] = [
    {
        "slug": "atlas",
        "title": "Atlas — портфель и задачи",
        "version": "0.7.8",
        "components": [
            {
                "key": "atlas-cli",
                "type": "cli",
                "artifacts": [
                    {
                        "os": "linux",
                        "arch": "amd64",
                        "kind": "binary",
                        "url": "https://api.atlas.pragmati.ru/dl/atlas-linux-amd64",
                        "sha256": "",
                        "install": {"bin": "atlas"},
                    }
                ],
            }
        ],
    },
    {
        "slug": "skillery",
        "title": "Skillery CLI",
        "version": "0.5.94",
        "components": [
            {
                "key": "skillery-cli",
                "type": "cli",
                "artifacts": [
                    {
                        "os": "any",
                        "arch": "any",
                        "kind": "uv-tool",
                        "install": {"package": "skillery-cli"},
                    }
                ],
            }
        ],
    },
]


def _by_slug(catalog: Catalog) -> dict[str, dict[str, Any]]:
    return {product["slug"]: product for product in catalog.products}


def _components(product: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {component["key"]: component for component in product["components"]}


def test_plugin_products_carry_type_os_and_install_metadata() -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    limits = _by_slug(catalog)["limits"]

    assert limits["title"]
    assert limits["version"] == "0.5.0"
    component = limits["components"][0]
    assert component["type"] == "plugin-dsh"
    artifact = component["artifacts"][0]
    assert artifact["os"] == "windows"
    assert artifact["arch"] == "any"
    assert artifact["kind"] == "archive"
    assert artifact["install"]["path"] == "plugins/limits"
    # peerRange попадает в install-метаданные; пустой не выдумываем.
    assert "peer_range" not in artifact["install"]


def test_claude_plugin_products_are_linux_any_and_claude_type() -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    limits_cache = _by_slug(catalog)["limits-cache"]
    component = limits_cache["components"][0]

    assert component["type"] == "plugin-claude"
    artifact = component["artifacts"][0]
    assert artifact["os"] == "linux"
    assert artifact["arch"] == "any"
    assert artifact["kind"] == "archive"


def test_peer_range_is_recorded_in_install_metadata() -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    atlas_plugin = _components(_by_slug(catalog)["atlas"])["atlas"]
    assert atlas_plugin["artifacts"][0]["install"]["peer_range"] == "^0.2.0"


def test_plugin_merges_into_existing_product_and_keeps_cli_version() -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    atlas = _by_slug(catalog)["atlas"]

    assert atlas["version"] == "0.7.8"
    assert set(_components(atlas)) == {"atlas-cli", "atlas"}


def test_voice_input_merges_into_dictation_and_depends_on_service() -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    dictation = _by_slug(catalog)["dictation"]
    components = _components(dictation)

    assert components["dictation-service"]["type"] == "service"
    voice = components["voice-input"]
    assert voice["type"] == "plugin-dsh"
    assert voice["depends_on"] == ["dictation-service"]


def test_dictation_service_installer_has_args_and_detect() -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    service = _components(_by_slug(catalog)["dictation"])["dictation-service"]
    artifact = service["artifacts"][0]

    assert artifact["kind"] == "installer"
    assert artifact["os"] == "windows"
    assert artifact["install"]["args"] == ["/VERYSILENT", "/SUPPRESSMSGBOXES"]
    assert artifact["install"]["detect"]["file"].endswith("dictation.exe")
    # Установщика в релизе ещё нет — фиксируем пустыми строками, не выдумываем.
    assert artifact["url"] == ""
    assert artifact["sha256"] == ""


def test_requires_cli_resolves_to_product_slug() -> None:
    registry = {
        "plugins": [
            {
                "name": "atlas-panel",
                "version": "0.1.0",
                "path": "plugins/atlas-panel",
                "summary": "панель",
                "requires": {"cli": "atlas"},
                "enabled": True,
            }
        ],
        "claudePlugins": [],
    }
    catalog = build_catalog(registry, {"plugins": []}, cli_products=CLI_PRODUCTS)
    panel = _components(_by_slug(catalog)["atlas-panel"])["atlas-panel"]
    assert panel["depends_on"] == ["atlas"]


def test_self_dependency_is_dropped_and_reported() -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    atlas = _components(_by_slug(catalog)["atlas"])["atlas"]
    assert "depends_on" not in atlas

    dependency_gaps = [gap for gap in catalog.gaps if gap.section == "dependency"]
    assert any(gap.detail == "atlas" for gap in dependency_gaps)


def test_unresolved_requirement_is_reported_as_gap() -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    dependency_gaps = [gap for gap in catalog.gaps if gap.section == "dependency"]
    details = {gap.detail for gap in dependency_gaps}
    assert "9router" in details
    assert "subs" in details


def test_marketplace_mismatch_is_reported() -> None:
    registry = {
        "plugins": [],
        "claudePlugins": [
            {"name": "ghost", "version": "1.0.0", "path": "claude/ghost", "summary": ""}
        ],
    }
    catalog = build_catalog(registry, {"plugins": []}, cli_products=CLI_PRODUCTS)
    missing = [gap for gap in catalog.gaps if gap.section == "marketplace"]
    assert any(gap.detail == "ghost" for gap in missing)


def test_products_and_components_are_sorted() -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    slugs = [product["slug"] for product in catalog.products]
    assert slugs == sorted(slugs)
    for product in catalog.products:
        keys = [component["key"] for component in product["components"]]
        assert keys == sorted(keys)


def test_build_is_deterministic() -> None:
    first = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    second = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    assert first == second


def test_every_built_product_passes_schema() -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    for product in catalog.products:
        assert validate_product(product) == [], product["slug"]


def test_empty_binary_artifacts_produce_artifact_gaps() -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    artifact_gaps = [gap for gap in catalog.gaps if gap.section == "artifact"]
    assert artifact_gaps
    assert all(gap.reason for gap in artifact_gaps)
    # У atlas url есть, sha256 нет — пробел назван поимённо.
    atlas_gap = next(gap for gap in artifact_gaps if gap.product == "atlas")
    assert "sha256" in atlas_gap.detail


def test_cli_catalog_matches_registry_local_skills() -> None:
    products = cli_catalog.build_cli_products()
    by_slug = {product["slug"]: product for product in products}
    expected = {
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
    }
    assert set(by_slug) == expected

    for slug, product in by_slug.items():
        component_types = {component["type"] for component in product["components"]}
        assert "cli" in component_types, slug
        assert validate_product(product) == [], slug


def test_cli_catalog_attaches_skill_ref_only_when_skill_exists() -> None:
    by_slug = {product["slug"]: product for product in cli_catalog.build_cli_products()}

    def skill_refs(slug: str) -> list[str]:
        refs = []
        for component in by_slug[slug]["components"]:
            if component["type"] != "skill":
                continue
            for artifact in component["artifacts"]:
                if artifact["kind"] == "skill-ref":
                    refs.append(artifact["skill_ref"])
        return refs

    assert skill_refs("atlas") == ["atlas"]
    assert skill_refs("skillery") == ["skillery"]
    assert skill_refs("b24") == []
    assert skill_refs("max") == []
    assert skill_refs("gws") == []


def test_atlas_artifacts_use_dl_endpoint_and_leave_sha256_empty() -> None:
    atlas = next(p for p in cli_catalog.build_cli_products() if p["slug"] == "atlas")
    artifacts = atlas["components"][0]["artifacts"]

    assert len(artifacts) == 5
    for artifact in artifacts:
        assert artifact["kind"] == "binary"
        assert artifact["url"].startswith("https://api.atlas.pragmati.ru/dl/atlas-")
        assert artifact["sha256"] == ""


def test_line_release_artifacts_have_url_and_sha256() -> None:
    line = next(p for p in cli_catalog.build_cli_products() if p["slug"] == "line")
    artifacts = line["components"][0]["artifacts"]

    assert len(artifacts) == 5
    for artifact in artifacts:
        assert artifact["url"].startswith(
            "https://github.com/zZZTeJleTTy3uKZZz/line-cli/releases/download/"
        )
        assert len(artifact["sha256"]) == 64
        int(artifact["sha256"], 16)


def test_uv_tool_products_describe_package() -> None:
    expected = {
        "transcribe": "s-transcribe",
        "beget": "beget-cli",
        "b24": "bitrix24-content-cli",
        "skillery": "skillery-cli",
        "gateway": "gateway",
        "max": "max-cli",
        "gws": "gws",
    }
    for product in cli_catalog.build_cli_products():
        artifact = product["components"][0]["artifacts"][0]
        if artifact["kind"] != "uv-tool":
            continue
        assert artifact["install"]["package"] == expected[product["slug"]]


def test_write_catalog_is_idempotent(tmp_path: Path) -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    first = write_catalog(tmp_path, catalog)
    second = write_catalog(tmp_path, catalog)

    assert first
    assert second == []
    for path in first:
        if path.suffix == ".toml":
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
            assert validate_product(parsed) == []
    assert (tmp_path / "GAPS.md").exists()


def test_diff_catalog_is_empty_after_write(tmp_path: Path) -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    assert diff_catalog(tmp_path, catalog)
    write_catalog(tmp_path, catalog)
    assert diff_catalog(tmp_path, catalog) == []


def test_gaps_markdown_names_missing_pieces_and_reasons() -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    text = render_gaps_markdown(catalog)

    assert text.startswith("# Пробелы каталога продуктов")
    assert "dictation" in text and "dictation-service" in text
    assert "sha256" in text
    assert "9router" in text
    assert "Продуктов:" in text


def test_gaps_entries_are_sorted_and_stable() -> None:
    catalog = build_catalog(REGISTRY, MARKETPLACE, cli_products=CLI_PRODUCTS)
    assert list(catalog.gaps) == sorted(catalog.gaps)


def test_missing_registry_file_raises_readable_error(tmp_path: Path) -> None:
    with pytest.raises(dsh_registry.RegistrySourceError, match="registry.json"):
        dsh_registry.load_json(tmp_path / "registry.json")


def test_gap_dataclass_is_orderable() -> None:
    first = Gap(section="artifact", product="a", component="a-cli", detail="x", reason="r")
    second = Gap(section="artifact", product="b", component="b-cli", detail="x", reason="r")
    assert sorted([second, first]) == [first, second]
