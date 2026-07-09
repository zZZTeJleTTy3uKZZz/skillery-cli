"""cmd_install автономные источники: --path / --from-git (без login, без сети).

Ключевые гарантии:
- install --path копирует папку в стор, линкует в scope, пишет манифест проекта,
  и НЕ инстанцирует HubClient (нет сети / backend bundle / login);
- install --path на папке без SKILL.md → ошибка, ничего не создано;
- взаимоисключение флагов (--path + --from-git, hub vs local);
- hub-режим без токена → внятная ошибка.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import typer

import skillery_cli.__main__ as main_mod
from skillery_cli import output as out_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core import project_manifest as pm
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import read_meta


class _ExplodingClient:
    """Любая попытка создать HubClient в автономном режиме = провал теста."""

    def __init__(self, *a, **k) -> None:
        raise AssertionError("HubClient НЕ должен создаваться в автономном install")


def _make_skill_dir(tmp_path: Path, *, with_skill_md: bool = True) -> Path:
    src = tmp_path / "my-skill-src"
    src.mkdir(parents=True)
    if with_skill_md:
        (src / "SKILL.md").write_text(
            "---\nname: my-skill\nversion: 3.1.4\n---\n\n# My skill\n",
            encoding="utf-8",
        )
    (src / "helper.py").write_text("x = 1\n", encoding="utf-8")
    return src


def _wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, logged_in: bool, json_mode: bool = True
) -> tuple[ClientConfig, ClaudeCodeTarget, Path]:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    cfg = ClientConfig(
        store_dir=str(store),
        default_install_scope="project",
        default_project_dir=str(project),
    )
    if logged_in:
        cfg.user_email = "x@y.io"
        cfg.permissions = ["skill.install"]
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "_maybe_auto_update", lambda c: None)
    monkeypatch.setattr(main_mod, "track_skill_event", lambda *a, **k: None, raising=False)
    # HubClient взрыв — если автономный режим вдруг полезет в сеть.
    monkeypatch.setattr(main_mod, "HubClient", _ExplodingClient)
    if json_mode:
        monkeypatch.setattr(out_mod, "_mode", "json")
    return cfg, target, project


def test_install_path_copies_links_manifest_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project = _wire(tmp_path, monkeypatch, logged_in=False)
    src = _make_skill_dir(tmp_path)

    # logged_in=False → доказываем, что login НЕ нужен. Токен-геттер тоже взрывается.
    monkeypatch.setattr(
        main_mod, "_get_access_token",
        lambda: (_ for _ in ()).throw(AssertionError("токен не нужен для --path")),
    )

    main_mod.cmd_install(
        slug="my-skill", channel="published", agent=None, scope="project",
        project=project, force=False, path=src, from_git=None, ref=None,
    )

    store_dir = cfg.effective_store_dir() / "my-skill"
    assert (store_dir / "SKILL.md").exists()
    assert (store_dir / "helper.py").read_text(encoding="utf-8") == "x = 1\n"
    meta = read_meta(store_dir)
    assert meta is not None
    assert meta["source"] == "local-path"
    # Версия извлечена из frontmatter.
    assert meta["version"] == "3.1.4"
    # Манифест проекта обновлён.
    assert pm.load(project) == {"my-skill": "*"}


def test_install_path_version_fallback_when_no_frontmatter_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project = _wire(tmp_path, monkeypatch, logged_in=False)
    src = tmp_path / "noversion"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: noversion\n---\n\n# x\n", encoding="utf-8")
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")

    main_mod.cmd_install(
        slug="noversion", channel="published", agent=None, scope="project",
        project=project, force=False, path=src, from_git=None, ref=None,
    )
    meta = read_meta(cfg.effective_store_dir() / "noversion")
    assert meta["version"] == "0.0.0-local"


def test_install_path_without_skill_md_errors_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, project = _wire(tmp_path, monkeypatch, logged_in=False)
    src = _make_skill_dir(tmp_path, with_skill_md=False)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")

    with pytest.raises(typer.Exit) as exc:
        main_mod.cmd_install(
            slug="my-skill", channel="published", agent=None, scope="project",
            project=project, force=False, path=src, from_git=None, ref=None,
        )
    assert exc.value.exit_code == 1
    # Ничего не материализовано.
    assert not (cfg.effective_store_dir() / "my-skill").exists()
    assert pm.load(project) == {}


def test_install_path_and_from_git_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cfg, _target, project = _wire(tmp_path, monkeypatch, logged_in=False)
    src = _make_skill_dir(tmp_path)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")
    with pytest.raises(typer.Exit) as exc:
        main_mod.cmd_install(
            slug="my-skill", channel="published", agent=None, scope="project",
            project=project, force=False, path=src,
            from_git="https://example.com/x.git", ref=None,
        )
    assert exc.value.exit_code == 1


def test_install_ref_without_from_git_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--ref имеет смысл только с --from-git."""
    _cfg, _target, project = _wire(tmp_path, monkeypatch, logged_in=False)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")
    with pytest.raises(typer.Exit) as exc:
        main_mod.cmd_install(
            slug="x", channel="published", agent=None, scope="project",
            project=project, force=False, path=None, from_git=None, ref="main",
        )
    assert exc.value.exit_code == 1


def test_install_from_git_local_bare_repo_no_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install --from-git <local-bare-repo> материализует навык без login/хаба."""
    import shutil
    import subprocess

    git = shutil.which("git")
    if git is None:
        pytest.skip("git не установлен")

    cfg, target, project = _wire(tmp_path, monkeypatch, logged_in=False)
    monkeypatch.setattr(
        main_mod, "_get_access_token",
        lambda: (_ for _ in ()).throw(AssertionError("токен не нужен для --from-git")),
    )

    def _g(cwd: Path, *args: str) -> None:
        subprocess.run([git, *args], cwd=str(cwd), check=True, capture_output=True, text=True)

    work = tmp_path / "work"
    work.mkdir()
    _g(work, "init", "-q", "-b", "main")
    _g(work, "config", "user.email", "t@t.io")
    _g(work, "config", "user.name", "t")
    (work / "SKILL.md").write_text(
        "---\nname: gitskill\nversion: 9.9.9\n---\n\n# git\n", encoding="utf-8"
    )
    _g(work, "add", "-A")
    _g(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    _g(tmp_path, "clone", "-q", "--bare", str(work), str(bare))

    # БЕЗ --ref → должна клонироваться дефолтная ветка репо.
    main_mod.cmd_install(
        slug="gitskill", channel="published", agent=None, scope="project",
        project=project, force=False, path=None, from_git=bare.as_uri(), ref=None,
    )
    store_dir = cfg.effective_store_dir() / "gitskill"
    assert (store_dir / "SKILL.md").exists()
    meta = read_meta(store_dir)
    assert meta["source"] == "git-url"
    assert meta["repo_url"] == bare.as_uri()
    # Фикс l2: версия из frontmatter клона, а не хардкод '0.0.0-local'.
    assert meta["version"] == "9.9.9"
    assert pm.load(project) == {"gitskill": "*"}


def test_install_hub_mode_without_login_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без --path/--from-git (hub) и без логина → внятная ошибка, exit 1."""
    _cfg, _target, project = _wire(tmp_path, monkeypatch, logged_in=False)

    # _get_access_token реалистично кидает typer.Exit(1) когда нет сессии.
    def _no_token() -> str:
        raise typer.Exit(1)

    monkeypatch.setattr(main_mod, "_get_access_token", _no_token)
    with pytest.raises(typer.Exit) as exc:
        main_mod.cmd_install(
            slug="bitrix24", channel="published", agent=None, scope="project",
            project=project, force=False, path=None, from_git=None, ref=None,
        )
    assert exc.value.exit_code == 1
