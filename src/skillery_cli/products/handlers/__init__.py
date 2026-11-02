"""Реестр обработчиков по ``Component.type`` (ADR-0001).

Один тип — один обработчик. Новый тип добавляется здесь и больше нигде:
движок и команды спрашивают обработчик по типу, а не знают про конкретные
реализации.
"""
from __future__ import annotations

from .base import ComponentHandler
from .cli import CliHandler
from .mcp import McpHandler
from .plugin import PluginHandler
from .service import ServiceHandler
from .skill import SkillHandler
from .unsupported import UnsupportedHandler

_HANDLERS: dict[str, ComponentHandler] = {
    "skill": SkillHandler(),
    "cli": CliHandler(),
    "mcp": McpHandler(),
    "plugin": PluginHandler(),
    "service": ServiceHandler(),
    "rules": UnsupportedHandler("rules"),
    "hook": UnsupportedHandler("hook"),
    "agent-profile": UnsupportedHandler("agent-profile"),
}


def get_handler(component_type: str) -> ComponentHandler:
    """Обработчик типа; неизвестный тип — ``LookupError``, а не молчание."""
    try:
        return _HANDLERS[component_type]
    except KeyError as exc:
        raise LookupError(
            f"для типа компонента {component_type!r} нет обработчика"
        ) from exc


def handler_types() -> tuple[str, ...]:
    return tuple(sorted(_HANDLERS))


__all__ = [
    "ComponentHandler",
    "get_handler",
    "handler_types",
]
