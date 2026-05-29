"""Cross-agent install targets."""
from skills_hub_cli.core.agents.antigravity import AntigravityTarget
from skills_hub_cli.core.agents.base import BaseAgentTarget, IAgentTarget
from skills_hub_cli.core.agents.claude_code import ClaudeCodeTarget
from skills_hub_cli.core.agents.codex import CodexTarget
from skills_hub_cli.core.agents.detect import detect_agent, get_target

__all__ = [
    "AntigravityTarget",
    "BaseAgentTarget",
    "ClaudeCodeTarget",
    "CodexTarget",
    "IAgentTarget",
    "detect_agent",
    "get_target",
]
