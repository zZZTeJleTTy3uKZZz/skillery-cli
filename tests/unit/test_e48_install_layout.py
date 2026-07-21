"""E48 §8.1 — раскладка навыка в проект + AntigravityTarget stub + detect chain.

cli-kits W7: ФС-механика раскладки переехала из ``IAgentTarget.install_layout``
(метод удалён) в ``skillkit.SkillStore`` (стор+линк-модель). Тесты дёргают
текущий путь установки — ``SkillInstaller(target, store_dir).install(local_src=…)``
— проверяя ту же семантику: копия дерева в стор/скоуп, project-scope, guard от
symlink-escape, работоспособность stub-таргета antigravity.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli.core.agents import (
    AntigravityTarget,
    ClaudeCodeTarget,
    CodexTarget,
    get_target,
)
from skillery_cli.core.installer import SkillInstaller

_MANIFEST = {"version": "1.0.0", "description": "x", "files": []}


def _install(target, store: Path, src: Path, *, project: Path | None = None):
    inst = SkillInstaller(target, store_dir=store)
    return inst.install(
        slug="demo",
        version="1.0.0",
        commit_sha="a1",
        repo_url=None,
        manifest=_MANIFEST,
        local_src=src,
        project=project,
    )


@pytest.mark.parametrize("cls", [ClaudeCodeTarget, CodexTarget])
def test_install_copies_source_tree_into_store(cls, tmp_path: Path) -> None:
    src = tmp_path / "cloned"
    (src / "sub").mkdir(parents=True)
    (src / "SKILL.md").write_text("hi", encoding="utf-8")
    (src / "sub" / "a.py").write_text("code", encoding="utf-8")

    store = tmp_path / "store"
    target = cls(root=tmp_path / f".{cls.name}")
    _install(target, store, src)

    # Материализовано в стор с сохранением поддеревьев.
    assert (store / "demo" / "SKILL.md").read_text(encoding="utf-8") == "hi"
    assert (store / "demo" / "sub" / "a.py").read_text(encoding="utf-8") == "code"


def test_install_into_project_scope(tmp_path: Path) -> None:
    src = tmp_path / "cloned"
    src.mkdir()
    (src / "SKILL.md").write_text("hi", encoding="utf-8")

    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "store"
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    _install(target, store, src, project=project)

    # Навык доступен в проектном скоупе (через линк или copy-fallback).
    assert (project / ".claude" / "skills" / "demo" / "SKILL.md").exists()


def test_install_rejects_symlink_escape(tmp_path: Path) -> None:
    from skillery_cli.core.installer import PathTraversalError

    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    src = tmp_path / "cloned"
    src.mkdir()
    (src / "SKILL.md").write_text("hi", encoding="utf-8")
    try:
        (src / "evil").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported")

    store = tmp_path / "store"
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    with pytest.raises(PathTraversalError):
        _install(target, store, src)


def test_antigravity_layout_paths(tmp_path: Path) -> None:
    t = AntigravityTarget(root=tmp_path / ".antigravity")
    assert t.name == "antigravity"
    assert t.base_dir() == tmp_path / ".antigravity" / "skills"
    assert t.slug_dir("foo") == tmp_path / ".antigravity" / "skills" / "foo"
    # Проектный scope пишет в `.agents/skills` — путь `.antigravity` убран в
    # ките (0.2.3+): агент его просто не читал, навык туда клали впустую.
    proj_slug = t.slug_dir("foo", project=tmp_path / "p")
    assert proj_slug == tmp_path / "p" / ".agents" / "skills" / "foo"
    assert "_local/" in t.preserved_paths()


def test_antigravity_install_works(tmp_path: Path) -> None:
    """Stub-таргет всё же умеет ставить навык (общий путь установки)."""
    src = tmp_path / "cloned"
    src.mkdir()
    (src / "SKILL.md").write_text("hi", encoding="utf-8")
    store = tmp_path / "store"
    t = AntigravityTarget(root=tmp_path / ".antigravity")
    _install(t, store, src)
    assert (store / "demo" / "SKILL.md").exists()


def test_get_target_antigravity() -> None:
    assert get_target("antigravity").name == "antigravity"


def test_detect_chain_includes_antigravity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """detect: Claude → Codex → Antigravity → fallback Claude."""
    import skillery_cli.core.agents.detect as detect_mod

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
