"""AntigravityTarget — stub-таргет для агента Antigravity (ТЗ §8.1).

Layout: `~/.antigravity/skills/<id-или-slug>/` (global) или
`<project>/.antigravity/skills/<id-или-slug>/` (project), по аналогии с
Claude/Codex. Имя папки — slug, либо числовой id для slug-less skill.

Базовая install_layout (копирование cloned-содержимого) работает; любые
agent-специфичные adapter-патчи пока не реализованы (NotImplementedError, если
понадобятся отдельным методом). Достаточно для install/update/remove флоу.
"""
from __future__ import annotations

from skills_hub_cli.core.agents.base import BaseAgentTarget


class AntigravityTarget(BaseAgentTarget):
    name = "antigravity"
    dirname = ".antigravity"
