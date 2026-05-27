from __future__ import annotations

from skills_hub_cli.core.agents.base import IAgentTarget
from skills_hub_cli.core.agents.claude_code import ClaudeCodeTarget
from skills_hub_cli.core.agents.codex import CodexTarget


def detect_agent() -> str:
    """Возвращает имя первого найденного агента (claude_code | codex), default claude_code."""
    for target_cls in (ClaudeCodeTarget, CodexTarget):
        if target_cls().exists():
            return target_cls.name
    return ClaudeCodeTarget.name


def get_target(name: str | None) -> IAgentTarget:
    actual = name or detect_agent()
    if actual == "claude_code":
        return ClaudeCodeTarget()
    if actual == "codex":
        return CodexTarget()
    raise ValueError(f"Неизвестный агент: {actual}")
