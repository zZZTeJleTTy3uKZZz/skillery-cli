"""Сборка каталога продуктов из всех источников.

Порядок: CLI-продукты (снимок фактов) → «Диктовка» (служба) → плагины реестра.
Совпавшие slug сливаются: плагин, обёртывающий продукт (``atlas``), становится
его компонентом, а ``voice-input`` — компонентом продукта ``dictation`` (ровно
так ADR описывает диктовку).

Зависимости разрешаются после слияния: ``requires`` плагина ссылается то на
продукт (``cli: atlas``), то на службу внутри своего продукта
(``dictation: 1.10.0``). Всё, что разрешить не удалось, попадает в пробелы, а не
превращается в висячую ссылку.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from . import cli_catalog, dictation, dsh_registry
from .gaps import Gap, artifact_gaps, sorted_gaps

#: Плагины, которые не заводят отдельный продукт, а входят компонентом.
PRODUCT_SLUG_OVERRIDES: dict[str, str] = {"voice-input": dictation.DICTATION_SLUG}

SERVICE_GAP_REASON = (
    "установщик собирается локально, CI-релиза с sha256 нет "
    "(ADR: диктовке нужен CI-релиз установщика)"
)
DEFAULT_BINARY_GAP_REASON = "релиза нет: URL и sha256 неизвестны"


@dataclass(frozen=True)
class Catalog:
    """Готовый каталог: манифесты продуктов и перечень пробелов."""

    products: tuple[dict[str, Any], ...]
    gaps: tuple[Gap, ...]


def build_catalog(
    registry: Mapping[str, Any],
    marketplace: Mapping[str, Any] | None = None,
    *,
    cli_products: Sequence[Mapping[str, Any]] | None = None,
) -> Catalog:
    """Собрать каталог; ``cli_products`` подменяется только в тестах."""
    source = cli_catalog.build_cli_products() if cli_products is None else cli_products
    products: list[dict[str, Any]] = [deepcopy(dict(product)) for product in source]

    dictation_product, gaps = dictation.build_dictation_product(registry)
    products.append(deepcopy(dictation_product))

    plugin_sources, marketplace_gaps = dsh_registry.build_plugin_products(
        registry, marketplace
    )
    gaps.extend(marketplace_gaps)

    pending: list[tuple[str, str, tuple[tuple[str, str], ...]]] = []
    for plugin_source in plugin_sources:
        plugin_product = deepcopy(plugin_source.product)
        component = plugin_product["components"][0]
        slug = PRODUCT_SLUG_OVERRIDES.get(plugin_product["slug"], plugin_product["slug"])
        existing = _find(products, slug)
        if existing is None:
            plugin_product["slug"] = slug
            products.append(plugin_product)
            existing = plugin_product
        else:
            existing["components"].append(component)
            if plugin_product["version"] != existing["version"]:
                gaps.append(
                    Gap(
                        section="version",
                        product=slug,
                        component=component["key"],
                        detail=f"{component['key']}={plugin_product['version']}",
                        reason=(
                            "в манифесте одна версия продукта; версия компонента "
                            "осталась в реестре плагинов"
                        ),
                    )
                )
        pending.append((slug, component["key"], plugin_source.requirements))

    slug_set = {product["slug"] for product in products}
    for slug, component_key, requirements in pending:
        component = _component(products, slug, component_key)
        depends_on: set[str] = set()
        for requirement, value in requirements:
            target, detail, reason = _resolve_dependency(
                requirement, value, slug, slug_set, products
            )
            if target is None:
                gaps.append(
                    Gap(
                        section="dependency",
                        product=slug,
                        component=component_key,
                        detail=detail,
                        reason=reason,
                    )
                )
            else:
                depends_on.add(target)
        if depends_on:
            component["depends_on"] = sorted(depends_on)

    for slug, note in cli_catalog.INDEX_NOTES.items():
        product = _find(products, slug)
        if product is None:
            continue
        package = _uv_tool_package(product)
        if package:
            gaps.append(
                Gap(
                    section="index",
                    product=slug,
                    component=f"{slug}-cli",
                    detail=package,
                    reason=note,
                )
            )

    gaps.extend(
        artifact_gaps(products, lambda slug, key, kind: _gap_reason(slug, kind))
    )

    _sort_catalog(products)
    return Catalog(products=tuple(products), gaps=sorted_gaps(gaps))


def _resolve_dependency(
    requirement: str,
    value: str,
    own_slug: str,
    slug_set: set[str],
    products: list[dict[str, Any]],
) -> tuple[str | None, str, str]:
    """Вернуть (цель depends_on | None, что искали, причина пробела)."""
    candidates: list[str] = []
    for candidate in (requirement, _first_token(value)):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        if candidate not in slug_set:
            continue
        if candidate != own_slug:
            return candidate, candidate, ""
        service_key = _service_component_key(products, own_slug)
        if service_key is not None:
            return service_key, service_key, ""
        return (
            None,
            candidate,
            "требование указывает на сам продукт: отдельного компонента-цели нет",
        )

    detail = candidates[-1] if candidates else requirement
    return None, detail, "в каталоге нет продукта или компонента с таким slug"


def _first_token(value: str) -> str:
    token = value.strip().split(maxsplit=1)[0] if value.strip() else ""
    return token.strip(",;.:")


def _service_component_key(
    products: list[dict[str, Any]], slug: str
) -> str | None:
    product = _find(products, slug)
    if product is None:
        return None
    for component in product["components"]:
        if component["type"] == "service":
            return str(component["key"])
    return None


def _uv_tool_package(product: Mapping[str, Any]) -> str:
    for component in product["components"]:
        for artifact in component["artifacts"]:
            if artifact["kind"] == "uv-tool":
                package = artifact.get("install", {}).get("package")
                return str(package) if package else ""
    return ""


def _gap_reason(slug: str, kind: str) -> str:
    if kind == "installer":
        return SERVICE_GAP_REASON
    if kind == "archive":
        return dsh_registry.PLUGIN_GAP_REASON
    return cli_catalog.GAP_REASONS.get(slug, DEFAULT_BINARY_GAP_REASON)


def _find(products: Sequence[dict[str, Any]], slug: str) -> dict[str, Any] | None:
    for product in products:
        if product["slug"] == slug:
            return product
    return None


def _component(
    products: Sequence[dict[str, Any]], slug: str, key: str
) -> dict[str, Any]:
    product = _find(products, slug)
    if product is None:  # pragma: no cover — защита от рассинхрона сборки
        raise KeyError(f"нет продукта {slug!r}")
    for component in product["components"]:
        if component["key"] == key:
            return component
    raise KeyError(f"нет компонента {key!r} в продукте {slug!r}")  # pragma: no cover


def _sort_catalog(products: list[dict[str, Any]]) -> None:
    products.sort(key=lambda product: product["slug"])
    for product in products:
        product["components"].sort(key=lambda component: component["key"])
        for component in product["components"]:
            component["artifacts"].sort(
                key=lambda artifact: (artifact["os"], artifact["arch"], artifact["kind"])
            )
            depends_on = component.get("depends_on")
            if depends_on:
                component["depends_on"] = sorted(set(depends_on))
