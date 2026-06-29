"""Тесты модели стор+линк: материализация в стор и линковка в scope."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from skills_hub_cli.config import ClientConfig


def test_effective_store_dir_env_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SKILLS_HUB_STORE_DIR", str(tmp_path / "custom-store"))
    cfg = ClientConfig()
    assert cfg.effective_store_dir() == tmp_path / "custom-store"


def test_effective_store_dir_explicit_field(tmp_path: Path) -> None:
    cfg = ClientConfig(store_dir=str(tmp_path / "explicit"))
    assert cfg.effective_store_dir() == tmp_path / "explicit"


def test_effective_store_dir_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("SKILLS_HUB_STORE_DIR", raising=False)
    monkeypatch.delenv("SKILLS_HUB_CONFIG_DIR", raising=False)
    # Изолируем home: ни ~/.skillery, ни legacy ~/.skills-hub не существуют →
    # свежая установка должна резолвить НОВЫЙ бренд ~/.skillery/store.
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))  # Windows expanduser
    cfg = ClientConfig()
    # Дефолт (ребренд) — ~/.skillery/store (раскрытый).
    assert cfg.effective_store_dir().name == "store"
    assert ".skillery" in str(cfg.effective_store_dir())


def test_effective_store_dir_default_legacy_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Обратная совместимость: если есть старый ~/.skills-hub/store, а нового
    ~/.skillery нет — дефолт остаётся на legacy (не теряем стор установки)."""
    monkeypatch.delenv("SKILLS_HUB_STORE_DIR", raising=False)
    monkeypatch.delenv("SKILLS_HUB_CONFIG_DIR", raising=False)
    fake_home = tmp_path / "home"
    (fake_home / ".skills-hub" / "store").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    cfg = ClientConfig()
    assert ".skills-hub" in str(cfg.effective_store_dir())


from skills_hub_cli.core import linker
from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import SkillInstaller, read_meta

_MANIFEST = {"version": "1.0.0", "description": "x", "files": []}


def _installer(tmp_path: Path) -> tuple[SkillInstaller, ClaudeCodeTarget, Path]:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    return SkillInstaller(target, store_dir=store), target, store


def test_install_materializes_store_and_links(tmp_path: Path) -> None:
    inst, target, store = _installer(tmp_path)
    res = inst.install(
        slug="demo", version="1.0.0", commit_sha="aaaa111111",
        repo_url=None, manifest=_MANIFEST,
    )
    # Контент материализован в стор.
    store_dir = store / "demo"
    assert (store_dir / "SKILL.md").exists()
    assert read_meta(store_dir) is not None
    # В scope — ссылка на стор (на Windows junction, POSIX symlink).
    link = target.slug_dir("demo")
    assert res.target_dir == link
    assert res.store_dir == store_dir
    assert res.linked is True
    assert linker.is_link(link)
    assert (link / "SKILL.md").read_text(encoding="utf-8").startswith("---")


def test_install_into_two_scopes_shares_one_store(tmp_path: Path) -> None:
    inst, target, store = _installer(tmp_path)
    inst.install(slug="demo", version="1.0.0", commit_sha="a1", repo_url=None, manifest=_MANIFEST)
    project = tmp_path / "proj"
    inst.install(slug="demo", version="1.0.0", commit_sha="a1", repo_url=None,
                 manifest=_MANIFEST, project=project)
    # Один стор, две ссылки.
    assert (store / "demo").exists()
    assert linker.is_link(target.slug_dir("demo"))
    assert linker.is_link(target.slug_dir("demo", project=project))


def test_install_fallback_to_copy_when_link_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inst, target, store = _installer(tmp_path)
    monkeypatch.setattr(
        linker, "create_link",
        lambda link, tgt: (_ for _ in ()).throw(OSError("no perms")),
    )
    res = inst.install(slug="demo", version="1.0.0", commit_sha="a1",
                       repo_url=None, manifest=_MANIFEST)
    assert res.linked is False
    assert res.link_kind == "copy"
    link = target.slug_dir("demo")
    assert not linker.is_link(link)
    assert (link / "SKILL.md").exists()  # реальная копия
    assert (link / "_skill_meta.json").exists()


def _local_src(tmp_path: Path) -> Path:
    """Реальный (не stub) источник: P0-guard запрещает stub'у заменять
    непустые папки даже под --force, поэтому foreign-семантика проверяется
    на local-path источнике."""
    src = tmp_path / "real-src"
    src.mkdir(exist_ok=True)
    (src / "SKILL.md").write_text(
        "---\nname: demo\nversion: 1.0.0\n---\n\n# demo\n", encoding="utf-8"
    )
    return src


def test_install_refuses_foreign_scope_dir_without_force(tmp_path: Path) -> None:
    inst, target, store = _installer(tmp_path)
    link = target.slug_dir("demo")
    link.mkdir(parents=True)
    (link / "hand.txt").write_text("manual", encoding="utf-8")  # чужая папка, нет meta
    # CLI конфигурирует кит на бренд skillery (core/_kit_config) → сообщение
    # «не управляется skillery» (бренд-строка выводится из app_name).
    with pytest.raises(RuntimeError, match="не управляется skillery"):
        inst.install(slug="demo", version="1.0.0", commit_sha="a1",
                     repo_url=None, local_src=_local_src(tmp_path),
                     manifest=_MANIFEST)


def test_install_force_replaces_foreign_scope_dir_with_link(tmp_path: Path) -> None:
    inst, target, store = _installer(tmp_path)
    link = target.slug_dir("demo")
    link.mkdir(parents=True)
    (link / "hand.txt").write_text("manual", encoding="utf-8")
    res = inst.install(slug="demo", version="1.0.0", commit_sha="a1",
                       repo_url=None, local_src=_local_src(tmp_path),
                       manifest=_MANIFEST, force=True)
    assert res.linked is True
    assert linker.is_link(target.slug_dir("demo"))


def test_install_stub_refuses_foreign_scope_dir_even_with_force(tmp_path: Path) -> None:
    """P0-guard: stub-источник НЕ заменяет непустую чужую папку даже с --force."""
    inst, target, store = _installer(tmp_path)
    link = target.slug_dir("demo")
    link.mkdir(parents=True)
    (link / "hand.txt").write_text("manual", encoding="utf-8")
    res = inst.install(slug="demo", version="1.0.0", commit_sha="a1",
                       repo_url=None, manifest=_MANIFEST, force=True)
    assert res.skipped is True
    assert res.skip_reason == "stub-would-clobber"
    assert (link / "hand.txt").read_text(encoding="utf-8") == "manual"
    assert not linker.is_link(link)


def test_cmd_install_project_writes_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """cmd_install в project scope линкует И пишет .skills-hub/skills.toml."""
    import skills_hub_cli.__main__ as main_mod
    from skills_hub_cli.config import ClientConfig
    from skills_hub_cli.core import project_manifest as pm
    from skills_hub_cli.core.agents import ClaudeCodeTarget
    from skills_hub_cli import output as out_mod

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir()

    cfg = ClientConfig(store_dir=str(store), default_install_scope="project",
                       default_project_dir=str(project))
    cfg.permissions = ["skill.install", "skill.read"]
    cfg.user_email = "x@y.io"
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "tok")
    monkeypatch.setattr(main_mod, "_maybe_auto_update", lambda c: None)
    monkeypatch.setattr(main_mod, "track_skill_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(out_mod, "_mode", "json")

    async def fake_chain(cfg_, access, *, slug, channel, scope, project_path, force, agent_target, source=None):
        inst = main_mod.SkillInstaller(agent_target, cfg_.effective_store_dir())
        res = inst.install(slug=slug, version="1.0.0", commit_sha="a1",
                           repo_url=None, manifest={"version": "1.0.0", "files": []},
                           project=project_path, force=force)
        return [{"slug": slug, "skill_id": None, "version": "1.0.0",
                 "is_update": res.is_update, "target_dir": str(res.target_dir),
                 "scope": res.scope, "linked": res.linked, "link_kind": res.link_kind}]

    monkeypatch.setattr(main_mod, "_install_chain", fake_chain)

    main_mod.cmd_install(slug="bitrix24", channel="published", agent=None,
                         scope=None, project=None, force=False,
                         path=None, from_git=None, ref=None)

    assert pm.load(project) == {"bitrix24": "*"}
