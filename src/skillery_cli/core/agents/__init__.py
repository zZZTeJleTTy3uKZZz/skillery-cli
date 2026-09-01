"""Тонкий re-export shim: install-targets переехали в кит ``skillkit.targets``.

cli-kits W7: протокол цели + детект агента теперь в ките. Здесь — реэкспорт,
чтобы прежние импорты ``from skillery_cli.core.agents import ...`` (и
подмодульные ``...agents.claude_code`` / ``...agents.codex``) не ломались.
"""
from skillkit.targets import (
    AgentTarget,
    AntigravityTarget,
    BaseAgentTarget,
    ClaudeCodeTarget,
    CodexTarget,
    detect_agent,
    get_target,
)

#: Имя до переименования в ките (lib#667). Держим ради чужого кода, который
#: импортирует его отсюда; свой код зовёт `AgentTarget`.
IAgentTarget = AgentTarget

__all__ = [
    "AgentTarget",
    "AntigravityTarget",
    "BaseAgentTarget",
    "ClaudeCodeTarget",
    "CodexTarget",
    "IAgentTarget",
    "detect_agent",
    "get_target",
]
