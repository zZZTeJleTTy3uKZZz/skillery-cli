"""Продукты Skillery: манифест, install-plan и установка компонентов.

Две половины одного контракта ADR-0001:

* ``manifest`` — схема и рендер ``product.toml`` (публикация);
* ``plan``/``state``/``handlers``/``download`` — исполнение install-plan на
  устройстве: скачивание с проверкой ``sha256``, обработчики по
  ``Component.type``, состояние в ``~/.skillery/products.json``.

Проверка состава у обеих половин общая — ``manifest.validate_product``:
второго валидатора не заводим, иначе план и публикация разойдутся в правилах.
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
from .plan import (
    Artifact,
    Component,
    InstallPlan,
    PlanError,
    Product,
    load_install_plan,
    parse_install_plan,
)
from .state import InstalledState, StateError, state_path

__all__ = [
    "ARCH_VALUES",
    "ARTIFACT_KINDS",
    "COMPONENT_TYPES",
    "OS_VALUES",
    "Artifact",
    "Component",
    "InstallPlan",
    "InstalledState",
    "PlanError",
    "Product",
    "StateError",
    "load_install_plan",
    "parse_install_plan",
    "render_product",
    "state_path",
    "validate_product",
]
