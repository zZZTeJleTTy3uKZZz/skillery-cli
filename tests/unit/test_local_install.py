"""Автономная установка навыка из ЛОКАЛЬНОЙ папки (installer.install local_src).

Источник `local_src` материализует папку как навык в стор через тот же
safe_copy_tree (traversal-guard), что и git-источник — без сети и без backend.
Версия берётся из frontmatter / _skill_meta.toml, иначе "0.0.0-local".
В meta фиксируется source="local-path" (repo_url=None).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skills_hub_cli.core import linker
from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import (
    PathTraversalError,
    SkillInstaller,
    read_meta,
)

_MANIFEST = {"version": "0.0.0-local", "description": "x", "files": []}


def _installer(tmp_path: Path) -> tuple[SkillInstaller, ClaudeCodeTarget, Path]:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    return SkillInstaller(target, store_dir=store), target, store


def _make_skill_src(tmp_path: Path, *, name: str = "my-skill") -> Path:
    src = tmp_path / name
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\nname: my-skill\nversion: 1.2.3\n---\n\n# My skill\n",
        encoding="utf-8",
    )
    (src / "extra.py").write_text("print('hi')\n", encoding="utf-8")
    return src


def test_local_src_materializes_store_and_links(tmp_path: Path) -> None:
    inst, target, store = _installer(tmp_path)
    src = _make_skill_src(tmp_path)
    res = inst.install(
        slug="my-skill", version="0.0.0-local", commit_sha="",
        repo_url=None, local_src=src, manifest=_MANIFEST,
    )
    store_dir = store / "my-skill"
    # Контент реальной папки скопирован в стор (не stub!).
    assert (store_dir / "SKILL.md").exists()
    assert (store_dir / "extra.py").read_text(encoding="utf-8") == "print('hi')\n"
    # SKILL.md — наш исходник, НЕ сгенерированный stub.
    assert "Stub" not in (store_dir / "SKILL.md").read_text(encoding="utf-8")
    # Ссылка в scope.
    assert res.linked is True
    assert linker.is_link(target.slug_dir("my-skill"))
    assert res.store_dir == store_dir


def test_local_src_meta_records_local_source(tmp_path: Path) -> None:
    inst, _target, store = _installer(tmp_path)
    src = _make_skill_src(tmp_path)
    inst.install(
        slug="my-skill", version="0.0.0-local", commit_sha="",
        repo_url=None, local_src=src, manifest=_MANIFEST,
    )
    meta = read_meta(store / "my-skill")
    assert meta is not None
    assert meta["source"] == "local-path"
    assert meta["slug"] == "my-skill"
    # repo_url остаётся None — у локального навыка нет origin-репо.
    assert meta.get("commit_sha") in ("", None)


def test_local_src_rejects_traversal_via_symlink_escape(tmp_path: Path) -> None:
    """Symlink внутри папки наружу источника → PathTraversalError (как git)."""
    inst, _target, _store = _installer(tmp_path)
    src = _make_skill_src(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    bad = src / "escape"
    try:
        bad.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlink не поддерживается окружением")
    with pytest.raises(PathTraversalError):
        inst.install(
            slug="my-skill", version="0.0.0-local", commit_sha="",
            repo_url=None, local_src=src, manifest=_MANIFEST,
        )
