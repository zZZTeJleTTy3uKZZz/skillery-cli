"""#943: подпапка навыка доезжает до установщика на ветке git clone.

Навык-монорепо (``skill_path='skills/atlas'``) снапшотом ставить нельзя —
снапшот собран от корня репозитория, SKILL.md в нём лежит не в корне архива.
Поэтому такой навык идёт на git clone... и раньше уходил туда БЕЗ skill_path:
клонировался весь репозиторий, в папку навыка попадали src/, migrations/,
pyproject.toml, а SKILL.md оказывался этажом ниже — Claude Code такой навык
не видит вообще.
"""
from __future__ import annotations

import pytest

from skillery_cli import __main__ as m


class _Installer:
    """Спай: запоминает аргументы install()."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def install(self, **kw):  # type: ignore[no-untyped-def]
        self.calls.append(kw)
        return type("R", (), {"skipped": False, "target_dir": "/tmp/x"})()

    def install_from_snapshot(self, **kw):  # type: ignore[no-untyped-def]
        self.calls.append({**kw, "_via": "snapshot"})
        return type("R", (), {"skipped": False, "target_dir": "/tmp/x"})()


class _Client:
    def __init__(self, snapshot: bytes | None = b"tar") -> None:
        self._snapshot = snapshot

    async def download_snapshot(self, ref: str, version: str) -> bytes | None:
        return self._snapshot


def _bundle(skill_path: str | None) -> dict:
    return {
        "commit_sha": "abc123",
        "manifest": {"version": "1.0.0", "files": []},
        "repo_url": "https://github.com/acme/mono.git",
        "skill_path": skill_path,
    }


class TestSkillPathReachesInstaller:
    async def test_subdir_skill_clones_with_skill_path(self) -> None:
        inst = _Installer()
        await m._materialize_from_bundle(
            inst,
            _Client(),  # снапшот есть, но для subdir-навыка он неприменим
            dep_slug="atlas",
            dep_version="1.0.0",
            dep_bundle=_bundle("skills/atlas"),
            dep_repo=None,
            dep_id=None,
            project_path=None,
            force=False,
        )
        assert len(inst.calls) == 1
        call = inst.calls[0]
        assert "_via" not in call, "subdir-навык нельзя ставить снапшотом"
        assert call["skill_path"] == "skills/atlas", (
            "без skill_path установщик разложит КОРЕНЬ монорепозитория"
        )

    async def test_root_skill_still_uses_snapshot(self) -> None:
        """Навык в корне репо по-прежнему ставится снапшотом (без git-кред)."""
        inst = _Installer()
        await m._materialize_from_bundle(
            inst,
            _Client(),
            dep_slug="hello",
            dep_version="1.0.0",
            dep_bundle=_bundle(None),
            dep_repo=None,
            dep_id=None,
            project_path=None,
            force=False,
        )
        assert inst.calls[0].get("_via") == "snapshot"

    async def test_root_skill_falls_back_to_clone_without_snapshot(self) -> None:
        inst = _Installer()
        await m._materialize_from_bundle(
            inst,
            _Client(snapshot=None),  # снапшота нет → clone
            dep_slug="hello",
            dep_version="1.0.0",
            dep_bundle=_bundle(None),
            dep_repo=None,
            dep_id=None,
            project_path=None,
            force=False,
        )
        call = inst.calls[0]
        assert "_via" not in call
        assert call["skill_path"] is None
