from __future__ import annotations

from skills_hub_cli.core.agents.antigravity import AntigravityTarget
from skills_hub_cli.core.agents.base import IAgentTarget
from skills_hub_cli.core.agents.claude_code import ClaudeCodeTarget
from skills_hub_cli.core.agents.codex import CodexTarget

# Порядок приоритета detection: Claude Code → Codex → Antigravity → fallback.
_CHAIN = (ClaudeCodeTarget, CodexTarget, AntigravityTarget)
_BY_NAME = {cls.name: cls for cls in _CHAIN}


def detect_agent() -> str:
    """Имя первого найденного агента в chain; fallback claude_code."""
    for target_cls in _CHAIN:
        if target_cls().exists():
            return target_cls.name
    return ClaudeCodeTarget.name


def get_target(name: str | None) -> IAgentTarget:
    actual = name or detect_agent()
    cls = _BY_NAME.get(actual)
    if cls is None:
        raise ValueError(f"Неизвестный агент: {actual}")
    return cls()
