"""Продукты Skillery: манифест ``product.toml`` и импортёры реестров.

Модель и формат манифеста — ADR-0001 ``docs/adr/products-and-components.md``.
Пакет намеренно не знает ни про backend, ни про установку: он только собирает,
проверяет и рендерит манифесты, которые затем публикует ``skillery product
publish``.
"""
from __future__ import annotations

from .manifest import (
    ARCH_VALUES,
    ARTIFACT_KINDS,
    COMPONENT_TYPES,
    OS_VALUES,
    render_product,
    validate_product,
)

__all__ = [
    "ARCH_VALUES",
    "ARTIFACT_KINDS",
    "COMPONENT_TYPES",
    "OS_VALUES",
    "render_product",
    "validate_product",
]
