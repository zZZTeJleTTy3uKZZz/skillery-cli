"""Пробелы импорта: что не удалось перенести в манифест и почему.

Пробел — не ошибка импортёра, а факт о состоянии релизов: манифест фиксирует
его пустой строкой (``url``/``sha256``), а ``GAPS.md`` объясняет человеку
причину. Поэтому пробелы собираются в одном месте, сортируются и попадают в
отчёт детерминированно.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .builder import Catalog

SECTION_TITLES: dict[str, str] = {
    "artifact": "Артефакты без url/sha256",
    "index": "uv-tool без публичного индекса",
    "dependency": "Зависимости без продукта",
    "version": "Версии компонентов, не отражённые в манифесте",
    "marketplace": "Расхождения реестра и marketplace",
}

_BYTES_KINDS = frozenset({"binary", "archive", "installer"})


@dataclass(frozen=True, order=True)
class Gap:
    """Один пробел каталога: где, чего нет и почему."""

    section: str
    product: str
    component: str
    detail: str
    reason: str


def artifact_gaps(
    products: Sequence[Mapping[str, Any]],
    reason_for: Callable[[str, str, str], str],
) -> list[Gap]:
    """Собрать пробелы по артефактам, у которых пуст url или sha256.

    ``reason_for(slug, component_key, kind)`` возвращает причину: она зависит от
    источника (плагин реестра, установщик службы, бинарь CLI).
    """
    gaps: list[Gap] = []
    for product in products:
        slug = str(product["slug"])
        for component in product["components"]:
            key = str(component["key"])
            for artifact in component["artifacts"]:
                kind = str(artifact["kind"])
                if kind not in _BYTES_KINDS:
                    continue
                missing = [
                    field
                    for field in ("url", "sha256")
                    if not str(artifact.get(field, "")).strip()
                ]
                if not missing:
                    continue
                detail = (
                    f"{artifact['os']}/{artifact['arch']}/{kind}: "
                    f"не хватает {', '.join(missing)}"
                )
                gaps.append(
                    Gap(
                        section="artifact",
                        product=slug,
                        component=key,
                        detail=detail,
                        reason=reason_for(slug, key, kind),
                    )
                )
    return gaps


def sorted_gaps(gaps: Iterable[Gap]) -> tuple[Gap, ...]:
    return tuple(sorted(gaps))


def render_gaps_markdown(catalog: Catalog) -> str:
    """Отрендерить ``GAPS.md``: заголовок со счётом, затем разделы по видам."""
    products = catalog.products
    components = sum(len(product["components"]) for product in products)
    lines = [
        "# Пробелы каталога продуктов",
        "",
        "Сгенерировано `scripts/import_registries.py`; руками не правится.",
        "",
        f"- Продуктов: {len(products)}",
        f"- Компонентов: {components}",
        f"- Пробелов: {len(catalog.gaps)}",
        "",
    ]

    by_section: dict[str, list[Gap]] = {}
    for gap in catalog.gaps:
        by_section.setdefault(gap.section, []).append(gap)

    for section, title in SECTION_TITLES.items():
        entries = by_section.get(section)
        if not entries:
            continue
        lines.extend(
            [
                f"## {title}",
                "",
                "| Продукт | Компонент | Что именно | Почему |",
                "| --- | --- | --- | --- |",
            ]
        )
        for gap in entries:
            lines.append(
                "| "
                + " | ".join(
                    _cell(part)
                    for part in (gap.product, gap.component, gap.detail, gap.reason)
                )
                + " |"
            )
        lines.append("")

    unknown = sorted(set(by_section) - set(SECTION_TITLES))
    if unknown:  # pragma: no cover — защита от нового вида пробела без раздела
        raise ValueError(f"неизвестные разделы пробелов: {', '.join(unknown)}")

    return "\n".join(lines).rstrip() + "\n"


def _cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")
