"""Install-plan продукта — ровно тот JSON, что отдаёт backend (ADR-0001).

План — исполняемый контракт, а не подсказка: компоненты приходят уже
отфильтрованными по ОС/архитектуре и упорядоченными топологически, а то, что
под эту ОС не поставляется, лежит в ``skipped``. Поэтому парсер ничего не
додумывает — он проверяет состав и падает до скачивания, если контракт
нарушен. Ошибка чтения — ``PlanError`` с человеческим текстом.

Схема компонента и артефакта — ОДНА на манифест и на план, поэтому проверку
состава делает ``manifest.validate_product``: плановый компонент оборачивается
в одноимённый продукт-манифест. Второй валидатор не заводим — иначе правила
(какие поля обязательны, какие значения допустимы) разъедутся, и план начнёт
пропускать то, что публикация уже запрещает.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .manifest import COMPONENT_TYPES, validate_product


class PlanError(ValueError):
    """Install-plan не читается или нарушает контракт ADR."""


@dataclass(frozen=True)
class Artifact:
    """Артефакт релиза: как доставить байты и чем проверить."""

    kind: str
    os: str
    arch: str
    url: str = ""
    sha256: str = ""
    size: int | None = None
    skill_ref: str = ""
    install: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Component:
    """Компонент продукта: обработчик выбирается по ``type``."""

    key: str
    type: str
    version: str = ""
    artifact: Artifact | None = None
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class Product:
    """Продукт плана: компоненты исполняются по порядку."""

    slug: str
    version: str = ""
    components: tuple[Component, ...] = ()
    skipped: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class InstallPlan:
    """Весь install-plan устройства."""

    products: tuple[Product, ...] = ()


def load_install_plan(path: Path) -> InstallPlan:
    """Прочитать план из файла; ошибки называют файл, а не только JSON."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PlanError(f"план не найден: {path}") from exc
    except OSError as exc:
        raise PlanError(f"план не читается ({path}): {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanError(f"план не разобран как JSON ({path}): {exc}") from exc
    return parse_install_plan(data)


def parse_install_plan(data: Mapping[str, Any]) -> InstallPlan:
    """Проверить и разобрать install-plan (без сети и файловых операций)."""
    if not isinstance(data, Mapping):
        raise PlanError("план должен быть объектом JSON")
    products = data.get("products")
    if not isinstance(products, Sequence) or isinstance(products, (str, bytes)):
        raise PlanError("в плане нет списка products")

    parsed: list[Product] = []
    for index, raw_product in enumerate(products):
        parsed.append(_parse_product(raw_product, f"products[{index}]"))
    return InstallPlan(products=tuple(parsed))


def _parse_product(raw: Any, where: str) -> Product:
    if not isinstance(raw, Mapping):
        raise PlanError(f"{where}: продукт должен быть объектом")
    slug = _required_str(raw, "slug", where)
    version = _optional_str(raw, "version")

    components_raw = raw.get("components", [])
    if not isinstance(components_raw, Sequence) or isinstance(
        components_raw, (str, bytes)
    ):
        raise PlanError(f"{where}.components: ожидался список")
    components: list[Component] = []
    for index, component_raw in enumerate(components_raw):
        components.append(
            _parse_component(
                component_raw,
                f"{where}.components[{index}]",
                slug=slug,
                product_version=version,
            )
        )

    skipped_raw = raw.get("skipped", [])
    if not isinstance(skipped_raw, Sequence) or isinstance(skipped_raw, (str, bytes)):
        raise PlanError(f"{where}.skipped: ожидался список")
    skipped: list[Mapping[str, Any]] = []
    for entry in skipped_raw:
        if not isinstance(entry, Mapping):
            raise PlanError(f"{where}.skipped: запись должна быть объектом")
        skipped.append(entry)

    return Product(
        slug=slug,
        version=version,
        components=tuple(components),
        skipped=tuple(skipped),
    )


def _parse_component(
    raw: Any,
    where: str,
    *,
    slug: str,
    product_version: str,
) -> Component:
    if not isinstance(raw, Mapping):
        raise PlanError(f"{where}: компонент должен быть объектом")

    key = _required_str(raw, "key", where)
    component_type = _required_str(raw, "type", where)
    version = _optional_str(raw, "version")

    artifact_raw = raw.get("artifact")
    artifact = _parse_artifact(artifact_raw, f"{where}.artifact")
    _validate_against_manifest(
        slug=slug,
        product_version=product_version or "0.0.0",
        key=key,
        component_type=component_type,
        artifact_raw=artifact_raw,
        where=where,
    )

    depends_on_raw = raw.get("depends_on", [])
    if not isinstance(depends_on_raw, Sequence) or isinstance(
        depends_on_raw, (str, bytes)
    ):
        raise PlanError(f"{where}.depends_on: ожидался список")
    depends_on = tuple(str(item) for item in depends_on_raw)

    return Component(
        key=key,
        type=component_type,
        version=version,
        artifact=artifact,
        depends_on=depends_on,
    )


def _validate_against_manifest(
    *,
    slug: str,
    product_version: str,
    key: str,
    component_type: str,
    artifact_raw: Any,
    where: str,
) -> None:
    """Проверить компонент тем же валидатором, что и ``product.toml``."""
    if component_type not in COMPONENT_TYPES:  # быстрый отказ до обёртки
        raise PlanError(
            f"{where}.type: недопустимый type {component_type!r}; "
            f"разрешены {', '.join(COMPONENT_TYPES)}"
        )

    if artifact_raw is None:
        # Компонент без артефакта — то, что backend не смог подобрать под ОС;
        # устанавливать нечего, но ключ/тип обязаны быть годными.
        return

    artifact = dict(artifact_raw)
    # size — поле контракта ПЛАНА, в манифесте его нет: это метаданные
    # релиза, а не свойство доставки.
    artifact.pop("size", None)
    wrapped = {
        "slug": slug,
        "title": slug,
        "version": product_version,
        "components": [{"key": key, "type": component_type, "artifacts": [artifact]}],
    }
    errors = validate_product(wrapped)
    if errors:
        cleaned = [
            error.replace("components[0].", "").replace("components[0]: ", "")
            for error in errors
        ]
        raise PlanError(f"{where}: " + "; ".join(cleaned))


def _parse_artifact(raw: Any, where: str) -> Artifact | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise PlanError(f"{where}: артефакт должен быть объектом")

    install_raw = raw.get("install", {})
    if install_raw is None:
        install_raw = {}
    if not isinstance(install_raw, Mapping):
        raise PlanError(f"{where}.install: ожидался объект")

    size_raw = raw.get("size")
    size = int(size_raw) if isinstance(size_raw, int) else None

    return Artifact(
        kind=_optional_str(raw, "kind"),
        os=_optional_str(raw, "os"),
        arch=_optional_str(raw, "arch"),
        url=_optional_str(raw, "url"),
        sha256=_optional_str(raw, "sha256"),
        size=size,
        skill_ref=_optional_str(raw, "skill_ref"),
        install=install_raw,
    )


def _required_str(raw: Mapping[str, Any], field: str, where: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{where}.{field}: обязательное поле отсутствует или пустое")
    return value


def _optional_str(raw: Mapping[str, Any], field: str) -> str:
    value = raw.get(field)
    return value if isinstance(value, str) else ""
