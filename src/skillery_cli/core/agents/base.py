"""Re-export shim: ``AgentTarget`` / ``BaseAgentTarget`` → ``skillkit.targets.base``.

``IAgentTarget`` — имя до переименования в ките (lib#667), оставлено для чужого кода.
"""
from __future__ import annotations

from skillkit.targets.base import (  # noqa: F401
    AgentTarget,
    BaseAgentTarget,
    IAgentTarget,
)
