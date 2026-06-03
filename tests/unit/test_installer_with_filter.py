"""Интеграция SkillInstaller + skill_filter: после git clone применяется фильтр."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core import installer as installer_mod
from skills_hub_cli.core.installer import SkillInstaller, read_meta


def _make_fake_clone(file_layout: dict[str, str]):
    """Возвращает callable, имитирующий `subprocess.run(["git", "clone", ..., target])`.

    file_layout: { "относительный/путь": "содержимое" }.
    Кладёт указанные файлы в target_dir.
    """

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any):  # noqa: ARG001
        assert cmd[0] == "git" and cmd[1] == "clone", f"unexpected cmd: {cmd}"
        target = Path(cmd[-1])
        target.mkdir(parents=True, exist_ok=True)
        for rel, content in file_layout.items():
            p = target / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    return fake_run


def test_install_applies_skillignore_after_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = {
        "SKILL.md": "---\nname: demo\nversion: 0.1.0\n---\n# demo",
        ".skillignore": "BACKLOG.md\ntests/\n_NOTES_FOR_*.md\n",
        "BACKLOG.md": "dev",
        "_NOTES_FOR_DOC_AGENT.md": "dev",
        "README.md": "user",
        "src/main.py": "code",
        "tests/test_x.py": "test",
    }
    monkeypatch.setattr(installer_mod.subprocess, "run", _make_fake_clone(layout))

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    inst = SkillInstaller(target, store_dir=tmp_path / "store")
    result = inst.install(
        slug="demo",
        version="0.1.0",
        commit_sha="abc1234567",
        repo_url="https://example.com/demo.git",
        manifest={"version": "0.1.0", "description": "x", "files": []},
    )

    assert not result.is_update
    assert result.filter_result is not None
    assert result.filter_result["removed"] >= 3  # BACKLOG.md, tests/, _NOTES_FOR_*.md

    d = result.target_dir
    assert (d / "SKILL.md").exists()
    assert (d / "README.md").exists()
    assert (d / "src" / "main.py").exists()
    assert not (d / "BACKLOG.md").exists()
    assert not (d / "tests").exists()
    assert not (d / "_NOTES_FOR_DOC_AGENT.md").exists()
    # meta пишется ПОСЛЕ filter, поэтому не удаляется
    assert read_meta(d) is not None


def test_install_applies_files_allowlist_from_skill_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_md = """---
name: demo
version: 0.1.0
files:
  - SKILL.md
  - src/**
---
# demo"""
    layout = {
        "SKILL.md": skill_md,
        "src/main.py": "code",
        "src/util.py": "util",
        "tests/test_x.py": "test",
        "BACKLOG.md": "dev",
        "README.md": "ignored too",
    }
    monkeypatch.setattr(installer_mod.subprocess, "run", _make_fake_clone(layout))

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    inst = SkillInstaller(target, store_dir=tmp_path / "store")
    result = inst.install(
        slug="demo",
        version="0.1.0",
        commit_sha="abc1234567",
        repo_url="https://example.com/demo.git",
        manifest={"version": "0.1.0", "description": "x", "files": []},
    )

    d = result.target_dir
    assert (d / "SKILL.md").exists()
    assert (d / "src" / "main.py").exists()
    assert (d / "src" / "util.py").exists()
    # Не в allowlist:
    assert not (d / "tests").exists()
    assert not (d / "BACKLOG.md").exists()
    assert not (d / "README.md").exists()
    assert result.filter_result is not None
    assert result.filter_result["removed"] >= 3


def test_install_no_skillignore_no_allowlist_keeps_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = {
        "SKILL.md": "---\nname: demo\nversion: 0.1.0\n---\n# demo",
        "README.md": "doc",
        "src/main.py": "code",
        "tests/test_x.py": "test",
    }
    monkeypatch.setattr(installer_mod.subprocess, "run", _make_fake_clone(layout))

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    inst = SkillInstaller(target, store_dir=tmp_path / "store")
    result = inst.install(
        slug="demo",
        version="0.1.0",
        commit_sha="abc1234567",
        repo_url="https://example.com/demo.git",
        manifest={"version": "0.1.0", "description": "x", "files": []},
    )

    d = result.target_dir
    assert (d / "SKILL.md").exists()
    assert (d / "README.md").exists()
    assert (d / "src" / "main.py").exists()
    assert (d / "tests" / "test_x.py").exists()
    assert result.filter_result == {"removed": 0, "kept": 4}


def test_install_stub_mode_no_filter_applied(tmp_path: Path) -> None:
    """Stub-режим (repo_url=None) НЕ вызывает filter."""
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    inst = SkillInstaller(target, store_dir=tmp_path / "store")
    result = inst.install(
        slug="demo",
        version="0.1.0",
        commit_sha="abc1234567",
        repo_url=None,
        manifest={"version": "0.1.0", "description": "x", "files": []},
    )
    assert result.filter_result is None
    assert (result.target_dir / "SKILL.md").exists()
