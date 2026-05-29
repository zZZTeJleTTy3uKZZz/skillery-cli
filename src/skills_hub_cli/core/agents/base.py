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

    def install_layout(
        self, slug: str, source_dir: Path, *, project: Path | None = None
    ) -> Path:
        """Распаковывает/копирует layout skill'а из source_dir в slug_dir.

        `source_dir` — папка с уже cloned-содержимым skill'а (без .git мусора).
        Инкапсулирует копирование с path-traversal guard (ТЗ §8.1/§10) + любые
        agent-специфичные adapter-патчи. Возвращает путь slug_dir.
        """


class BaseAgentTarget:
    """Общая реализация IAgentTarget. Конкретные агенты задают `name` и
    `dirname` (имя dot-папки, напр. `.claude`) и опционально переопределяют
    `preserved_paths` / adapter-патчи в `install_layout`.
    """

    name: str = ""
    dirname: str = ""

    def __init__(self, *, root: Path | None = None) -> None:
        self._root = root or Path.home() / self.dirname

    def base_dir(self, *, project: Path | None = None) -> Path:
        if project is not None:
            return project / self.dirname / "skills"
        return self._root / "skills"

    def slug_dir(self, slug: str, *, project: Path | None = None) -> Path:
        return self.base_dir(project=project) / slug

    def preserved_paths(self) -> tuple[str, ...]:
        return ("_local/", "browser_profiles/", ".env")

    def exists(self) -> bool:
        return self._root.exists()

    def install_layout(
        self, slug: str, source_dir: Path, *, project: Path | None = None
    ) -> Path:
        # Ленивый импорт во избежание цикла installer ↔ agents.
        from skills_hub_cli.core.installer import safe_copy_tree

        slug_dir = self.slug_dir(slug, project=project)
        safe_copy_tree(Path(source_dir), slug_dir)
        return slug_dir
