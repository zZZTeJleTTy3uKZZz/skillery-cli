"""Тонкий re-export shim: install-targets переехали в кит ``skillkit.targets``.

cli-kits W7: ``IAgentTarget`` + детект агента теперь в ките. Здесь — реэкспорт,
чтобы прежние импорты ``from skillery_cli.core.agents import ...`` (и
подмодульные ``...agents.claude_code`` / ``...agents.codex``) не ломались.
"""
from skillkit.targets import (
    AntigravityTarget,
    BaseAgentTarget,
    ClaudeCodeTarget,
    CodexTarget,
    IAgentTarget,
    detect_agent,
    get_target,
)

__all__ = [
    "AntigravityTarget",
    "BaseAgentTarget",
    "ClaudeCodeTarget",
    "CodexTarget",
    "IAgentTarget",
    "detect_agent",
    "get_target",
]
