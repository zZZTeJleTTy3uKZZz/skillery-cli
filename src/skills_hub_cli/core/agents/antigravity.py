"""AntigravityTarget — stub-таргет для агента Antigravity (ТЗ §8.1).

Layout: `~/.antigravity/skills/<slug>/` (global) или
`<project>/.antigravity/skills/<slug>/` (project), по аналогии с Claude/Codex.

Базовая install_layout (копирование cloned-содержимого) работает; любые
agent-специфичные adapter-патчи пока не реализованы (NotImplementedError, если
понадобятся отдельным методом). Достаточно для install/update/remove флоу.
"""
from __future__ import annotations

from skills_hub_cli.core.agents.base import BaseAgentTarget


class AntigravityTarget(BaseAgentTarget):
    name = "antigravity"
    dirname = ".antigravity"
