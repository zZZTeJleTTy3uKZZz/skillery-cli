"""IAgentTarget — куда CLI ставит skill для конкретного агента.

Поддерживает два scope:
- global: `~/.claude/skills/<slug>/`  (видно во всех сессиях агента)
- project: `<project>/.claude/skills/<slug>/`  (видно только в project)

Project-scope skills имеют ПРИОРИТЕТ над global при одинаковом slug
(стандартное поведение Claude Code / Codex).
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol


class IAgentTarget(Protocol):
    name: str

    def base_dir(self, *, project: Path | None = None) -> Path:
        """Базовая папка skills.

        Если project=None — глобальная (`~/.<agent>/skills/`).
        Если project=Path — `<project>/.<agent>/skills/`.
        """

    def slug_dir(self, slug: str, *, project: Path | None = None) -> Path:
        """Папка конкретного skill'а в указанном scope."""

    def preserved_paths(self) -> tuple[str, ...]:
        """Подпути которые НЕ удаляются при update."""

    def exists(self) -> bool:
        """Установлен ли агент на этой машине (наличие глобального ~/.<agent>/)."""
