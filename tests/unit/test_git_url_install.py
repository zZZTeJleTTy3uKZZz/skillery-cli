"""Автономная установка навыка из ПРОИЗВОЛЬНОГО git-репо (installer git_ref).

Источник git-url: clone url@ref (НЕ ``v<version>``-тег hub-релиза) в стор +
ссылка. Backend не участвует. Версия из frontmatter/_skill_meta.toml либо
"0.0.0-local". В meta source="git-url" + repo_url=<url>.

Git-тесты используют ЛОКАЛЬНЫЙ bare-repo как url (без сети). На окружении без
git они пропускаются.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from skillery_cli.core import linker
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import SkillInstaller, read_meta

_GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(_GIT is None, reason="git не установлен")

_MANIFEST = {"version": "0.0.0-local", "description": "x", "files": []}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run([_GIT, *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_bare_repo(tmp_path: Path) -> tuple[Path, str]:
    """Создаёт work-repo с SKILL.md, коммитит на ветке main, отдаёт bare-clone + ref."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@t.io")
    _git(work, "config", "user.name", "t")
    (work / "SKILL.md").write_text(
        "---\nname: gitskill\nversion: 2.0.0\n---\n\n# Git skill\n", encoding="utf-8"
    )
    (work / "run.sh").write_text("echo hi\n", encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    return bare, "main"


def _installer(tmp_path: Path) -> tuple[SkillInstaller, ClaudeCodeTarget, Path]:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    return SkillInstaller(target, store_dir=store), target, store


def test_git_url_ref_clone_materializes_and_links(tmp_path: Path) -> None:
    inst, target, store = _installer(tmp_path)
    bare, ref = _make_bare_repo(tmp_path)
    res = inst.install(
        slug="gitskill", version="0.0.0-local", commit_sha="",
        repo_url=bare.as_uri(), git_ref=ref, manifest=_MANIFEST,
    )
    store_dir = store / "gitskill"
    assert (store_dir / "SKILL.md").exists()
    assert (store_dir / "run.sh").read_text(encoding="utf-8") == "echo hi\n"
    # .git клона НЕ протекает в стор (safe_copy_tree пропускает).
    assert not (store_dir / ".git").exists()
    assert res.linked is True
    assert linker.is_link(target.slug_dir("gitskill"))
    meta = read_meta(store_dir)
    assert meta is not None
    assert meta["source"] == "git-url"
    assert meta["repo_url"] == bare.as_uri()


def test_git_url_default_branch_when_ref_empty(tmp_path: Path) -> None:
    """git_ref='' (нет --ref) → клон дефолтной ветки репо (без --branch HEAD)."""
    inst, target, store = _installer(tmp_path)
    bare, _ref = _make_bare_repo(tmp_path)
    res = inst.install(
        slug="gitskill", version="0.0.0-local", commit_sha="",
        repo_url=bare.as_uri(), git_ref="", manifest=_MANIFEST,
    )
    store_dir = store / "gitskill"
    assert (store_dir / "SKILL.md").exists()
    assert res.linked is True
    meta = read_meta(store_dir)
    assert meta["source"] == "git-url"


def test_git_url_bad_ref_raises(tmp_path: Path) -> None:
    inst, _target, _store = _installer(tmp_path)
    bare, _ref = _make_bare_repo(tmp_path)
    with pytest.raises(RuntimeError, match="git clone"):
        inst.install(
            slug="gitskill", version="0.0.0-local", commit_sha="",
            repo_url=bare.as_uri(), git_ref="no-such-ref", manifest=_MANIFEST,
        )
