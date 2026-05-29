"""E48 §8.1 — IAgentTarget.install_layout + AntigravityTarget stub + detect chain."""
from __future__ import annotations

from pathlib import Path

import pytest

from skills_hub_cli.core.agents import (
    AntigravityTarget,
    ClaudeCodeTarget,
    CodexTarget,
    get_target,
)


@pytest.mark.parametrize("cls", [ClaudeCodeTarget, CodexTarget])
def test_install_layout_copies_source_into_slug_dir(cls, tmp_path: Path) -> None:
    src = tmp_path / "cloned"
    (src / "sub").mkdir(parents=True)
    (src / "SKILL.md").write_text("hi", encoding="utf-8")
    (src / "sub" / "a.py").write_text("code", encoding="utf-8")

    target = cls(root=tmp_path / f".{cls.name}")
    target.install_layout("demo", src)

    slug_dir = target.slug_dir("demo")
    assert (slug_dir / "SKILL.md").read_text(encoding="utf-8") == "hi"
    assert (slug_dir / "sub" / "a.py").read_text(encoding="utf-8") == "code"


def test_install_layout_into_project_scope(tmp_path: Path) -> None:
    src = tmp_path / "cloned"
    src.mkdir()
    (src / "SKILL.md").write_text("hi", encoding="utf-8")

    project = tmp_path / "proj"
    project.mkdir()
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    target.install_layout("demo", src, project=project)

    assert (project / ".claude" / "skills" / "demo" / "SKILL.md").exists()


def test_install_layout_rejects_symlink_escape(tmp_path: Path) -> None:
    from skills_hub_cli.core.installer import PathTraversalError

    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    src = tmp_path / "cloned"
    src.mkdir()
    (src / "SKILL.md").write_text("hi", encoding="utf-8")
    try:
        (src / "evil").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    with pytest.raises(PathTraversalError):
        target.install_layout("demo", src)


def test_antigravity_layout_paths(tmp_path: Path) -> None:
    t = AntigravityTarget(root=tmp_path / ".antigravity")
    assert t.name == "antigravity"
    assert t.base_dir() == tmp_path / ".antigravity" / "skills"
    assert t.slug_dir("foo") == tmp_path / ".antigravity" / "skills" / "foo"
    proj_slug = t.slug_dir("foo", project=tmp_path / "p")
    assert proj_slug == tmp_path / "p" / ".antigravity" / "skills" / "foo"
    assert "_local/" in t.preserved_paths()


def test_antigravity_install_layout_works(tmp_path: Path) -> None:
    """Stub-таргет всё же умеет копировать layout (общий путь установки)."""
    src = tmp_path / "cloned"
    src.mkdir()
    (src / "SKILL.md").write_text("hi", encoding="utf-8")
    t = AntigravityTarget(root=tmp_path / ".antigravity")
    t.install_layout("demo", src)
    assert (t.slug_dir("demo") / "SKILL.md").exists()


def test_get_target_antigravity() -> None:
    assert get_target("antigravity").name == "antigravity"


def test_detect_chain_includes_antigravity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """detect: Claude → Codex → Antigravity → fallback Claude."""
    import skills_hub_cli.core.agents.detect as detect_mod

    # Никого нет → fallback claude_code.
    monkeypatch.setattr(detect_mod.ClaudeCodeTarget, "exists", lambda self: False)
    monkeypatch.setattr(detect_mod.CodexTarget, "exists", lambda self: False)
    monkeypatch.setattr(detect_mod.AntigravityTarget, "exists", lambda self: False)
    assert detect_mod.detect_agent() == "claude_code"

    # Только antigravity установлен.
    monkeypatch.setattr(detect_mod.AntigravityTarget, "exists", lambda self: True)
    assert detect_mod.detect_agent() == "antigravity"

    # Codex имеет приоритет над antigravity.
    monkeypatch.setattr(detect_mod.CodexTarget, "exists", lambda self: True)
    assert detect_mod.detect_agent() == "codex"
