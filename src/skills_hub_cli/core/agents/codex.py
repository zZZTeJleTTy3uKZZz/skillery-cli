from __future__ import annotations

from pathlib import Path


class CodexTarget:
    name = "codex"

    def __init__(self, *, root: Path | None = None) -> None:
        self._root = root or Path.home() / ".codex"

    def base_dir(self, *, project: Path | None = None) -> Path:
        if project is not None:
            return project / ".codex" / "skills"
        return self._root / "skills"

    def slug_dir(self, slug: str, *, project: Path | None = None) -> Path:
        return self.base_dir(project=project) / slug

    def preserved_paths(self) -> tuple[str, ...]:
        return ("_local/", "browser_profiles/", ".env")

    def exists(self) -> bool:
        return self._root.exists()
