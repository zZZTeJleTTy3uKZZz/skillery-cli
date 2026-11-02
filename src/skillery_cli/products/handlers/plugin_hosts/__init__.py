"""Реализации плагинов по харнесам (ADR-0001: плагин — ОДНА сущность).

Тип компонента один — ``plugin``. Харнес выбирается полем ``hosts``, а здесь
лежат только те реализации, которые этот харнес реально умеют ставить.
"""
from __future__ import annotations

from .claude_code import ClaudeCodeHost
from .dsh import DshHost

#: Харнесы, для которых установка плагинов уже реализована.
HOST_HANDLERS = {
    ClaudeCodeHost.host: ClaudeCodeHost(),
    DshHost.host: DshHost(),
}

__all__ = ["HOST_HANDLERS", "ClaudeCodeHost", "DshHost"]
