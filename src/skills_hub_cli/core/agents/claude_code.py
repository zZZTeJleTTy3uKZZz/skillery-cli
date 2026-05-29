from __future__ import annotations

from skills_hub_cli.core.agents.base import BaseAgentTarget


class ClaudeCodeTarget(BaseAgentTarget):
    name = "claude_code"
    dirname = ".claude"
