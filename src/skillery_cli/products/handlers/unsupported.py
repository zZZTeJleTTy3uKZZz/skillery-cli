"""Типы, которые CLI пока не ставит: ``rules``, ``hook``, ``agent-profile``.

Это НЕ ошибка и не пробел данных: ADR-0001 знает эти компоненты, а установщик
их ещё не умеет. Возвращаем ``skipped`` с понятной причиной — так продукт с
такими компонентами остаётся установленным, а человек видит, что именно
отложено.
"""
from __future__ import annotations

from ..plan import Component
from .base import ComponentResult, InstallContext

UNSUPPORTED_REASON = "не поддерживается в этой версии CLI"


class UnsupportedHandler:
    """Обработчик типа, который заведомо пропускается."""

    def __init__(self, component_type: str) -> None:
        self.component_type = component_type

    def plan(self, component: Component, ctx: InstallContext) -> list[str]:
        return [f"пропустить {component.key}: {UNSUPPORTED_REASON}"]

    def verify(self, component: Component, ctx: InstallContext) -> bool:
        return True

    async def install(
        self, component: Component, ctx: InstallContext
    ) -> ComponentResult:
        return ComponentResult(
            key=component.key,
            type=self.component_type,
            status="skipped",
            detail=UNSUPPORTED_REASON,
        )

    async def remove(
        self, component: Component, ctx: InstallContext
    ) -> ComponentResult:
        return ComponentResult(
            key=component.key,
            type=self.component_type,
            status="absent",
            detail=UNSUPPORTED_REASON,
        )


__all__ = ["UNSUPPORTED_REASON", "UnsupportedHandler"]
