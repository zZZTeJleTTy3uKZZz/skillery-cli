"""Тесты SkillInstaller (stub-режим без git clone)."""
from __future__ import annotations

from pathlib import Path

from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import SkillInstaller, read_meta


def test_install_without_repo_creates_stub(tmp_path: Path) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    installer = SkillInstaller(target, store_dir=tmp_path / "store")
    result = installer.install(
        slug="wb-api",
        version="0.1.0",
        commit_sha="abc1234567",
        repo_url=None,
        manifest={"version": "0.1.0", "description": "x", "files": []},
    )
    assert not result.is_update
    assert (result.target_dir / "SKILL.md").exists()
    meta = read_meta(result.target_dir)
    assert meta is not None
    assert meta["version"] == "0.1.0"
    assert meta["agent"] == "claude_code"


def test_reinstall_marks_as_update(tmp_path: Path) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    installer = SkillInstaller(target, store_dir=tmp_path / "store")
    installer.install(
        slug="wb-api",
        version="0.1.0",
        commit_sha="abc1234567",
        repo_url=None,
        manifest={"version": "0.1.0", "description": "x", "files": []},
    )
    r2 = installer.install(
        slug="wb-api",
        version="0.2.0",
        commit_sha="def1234567",
        repo_url=None,
        manifest={"version": "0.2.0", "description": "x", "files": []},
    )
    assert r2.is_update
    meta = read_meta(r2.target_dir)
    assert meta is not None
    assert meta["version"] == "0.2.0"
