"""CLI enable/disable: линковка в project scope + манифест."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import skillery_cli.__main__ as main_mod
    from skillery_cli.config import ClientConfig
    from skillery_cli.core.agents import ClaudeCodeTarget
    from skillery_cli import output as out_mod

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir()
    cfg = ClientConfig(store_dir=str(store))
    cfg.permissions = ["skill.install"]
    cfg.user_email = "x@y.io"
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "tok")
    monkeypatch.setattr(main_mod, "track_skill_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(out_mod, "_mode", "json")

    async def fake_chain(cfg_, access, *, slug, channel, scope, project_path, force, agent_target):
        inst = main_mod.SkillInstaller(agent_target, cfg_.effective_store_dir())
        res = inst.install(slug=slug, version="1.0.0", commit_sha="a1",
                           repo_url=None, manifest={"version": "1.0.0", "files": []},
                           project=project_path, force=force)
        return [{"slug": slug, "skill_id": None, "version": "1.0.0",
                 "is_update": res.is_update, "target_dir": str(res.target_dir),
                 "scope": res.scope, "linked": res.linked, "link_kind": res.link_kind}]

    monkeypatch.setattr(main_mod, "_install_chain", fake_chain)
    return main_mod, target, store, project


def test_enable_links_and_writes_manifest(tmp_path, monkeypatch, capsys):
    main_mod, target, store, project = _setup(tmp_path, monkeypatch)
    from skillery_cli.core import linker, project_manifest as pm

    main_mod.cmd_enable(slug="bitrix24", project=project, agent=None,
                        force=False, channel="published")
    assert linker.is_link(target.slug_dir("bitrix24", project=project))
    assert pm.load(project) == {"bitrix24": "*"}
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["event"] == "enabled"


def test_disable_unlinks_and_removes_manifest(tmp_path, monkeypatch, capsys):
    main_mod, target, store, project = _setup(tmp_path, monkeypatch)
    from skillery_cli.core import linker, project_manifest as pm

    main_mod.cmd_enable(slug="bitrix24", project=project, agent=None,
                        force=False, channel="published")
    capsys.readouterr()  # очистить

    main_mod.cmd_disable(slug="bitrix24", project=project, agent=None)
    assert not linker.is_link(target.slug_dir("bitrix24", project=project))
    assert pm.load(project) == {}
    # стор цел
    assert (store / "bitrix24" / "SKILL.md").exists()
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["event"] == "disabled"
    assert payload["unlinked"] is True
