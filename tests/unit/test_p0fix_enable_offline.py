"""Фикс 2 (MAJOR): enable — store-first (живой 404 на local-path скилле).

cmd_enable раньше всегда шёл в hub (_install_chain source=None) → для
local-path/git-url навыков получал 404, а без логина падал на токене.

Гарантии:
- навык уже в сторе → re-link в project scope + project_manifest.add,
  БЕЗ сети и БЕЗ токена (любой source: local-path / git-url / hub);
- навыка нет в сторе и нет логина → emit_error NOT_LOGGED_IN + exit 1;
- навыка нет в сторе, залогинен → hub-chain как раньше.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer

import skills_hub_cli.__main__ as main_mod
from skills_hub_cli import output as out_mod
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.core import linker, project_manifest as pm
from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import SkillInstaller


class _ExplodingClient:
    def __init__(self, *a, **k) -> None:
        raise AssertionError("HubClient НЕ должен создаваться при store-first enable")


def _wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, logged_in: bool):
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    cfg = ClientConfig(store_dir=str(store))
    if logged_in:
        cfg.user_email = "x@y.io"
        cfg.permissions = ["skill.install"]
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "track_skill_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(main_mod, "HubClient", _ExplodingClient)
    monkeypatch.setattr(out_mod, "_mode", "json")
    return cfg, target, store, project


def _seed_local_skill(
    tmp_path: Path, target: ClaudeCodeTarget, store: Path, project: Path
) -> SkillInstaller:
    """Материализует local-path навык в стор + project scope (как install --path)."""
    src = tmp_path / "src-skill"
    src.mkdir(exist_ok=True)
    (src / "SKILL.md").write_text(
        "---\nname: localskill\nversion: 1.2.3\n---\n\n# L\n", encoding="utf-8"
    )
    inst = SkillInstaller(target, store_dir=store)
    inst.install(
        slug="localskill", version="1.2.3", commit_sha="",
        repo_url=None, local_src=src,
        manifest={"version": "1.2.3", "files": []}, project=project,
    )
    pm.add(project, "localskill")
    return inst


def test_enable_after_disable_works_offline_for_local_path_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _cfg, target, store, project = _wire(tmp_path, monkeypatch, logged_in=False)
    _seed_local_skill(tmp_path, target, store, project)
    main_mod.cmd_disable(slug="localskill", project=project, agent=None)
    assert not target.slug_dir("localskill", project=project).exists()
    capsys.readouterr()

    # Токен и сеть «взрываются» — enable обязан пройти чисто локально.
    monkeypatch.setattr(
        main_mod, "_get_access_token",
        lambda: (_ for _ in ()).throw(AssertionError("store-first: токен не нужен")),
    )

    main_mod.cmd_enable(
        slug="localskill", project=project, agent=None, force=False, channel="published"
    )

    assert linker.is_link(target.slug_dir("localskill", project=project))
    assert pm.load(project) == {"localskill": "*"}
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["event"] == "enabled"
    assert payload["skills"][0]["version"] == "1.2.3"


def test_enable_missing_skill_without_login_emits_not_logged_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _cfg, _target, _store, project = _wire(tmp_path, monkeypatch, logged_in=False)
    with pytest.raises(typer.Exit) as exc:
        main_mod.cmd_enable(
            slug="ghost", project=project, agent=None, force=False, channel="published"
        )
    assert exc.value.exit_code == 1
    captured = capsys.readouterr()
    assert "NOT_LOGGED_IN" in captured.err  # json-режим: ошибка в stderr
    assert pm.load(project) == {}  # манифест не тронут


def test_enable_store_first_no_network_even_when_logged_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Залогинен, но навык уже в сторе → сеть всё равно не дёргаем."""
    _cfg, target, store, project = _wire(tmp_path, monkeypatch, logged_in=True)
    _seed_local_skill(tmp_path, target, store, project)
    main_mod.cmd_disable(slug="localskill", project=project, agent=None)
    capsys.readouterr()
    monkeypatch.setattr(
        main_mod, "_get_access_token",
        lambda: (_ for _ in ()).throw(AssertionError("store-first: токен не нужен")),
    )

    async def _exploding_chain(*a, **k):
        raise AssertionError("_install_chain не должен вызываться при store-first")

    monkeypatch.setattr(main_mod, "_install_chain", _exploding_chain)

    main_mod.cmd_enable(
        slug="localskill", project=project, agent=None, force=False, channel="published"
    )
    assert linker.is_link(target.slug_dir("localskill", project=project))
    assert pm.load(project) == {"localskill": "*"}


def test_enable_falls_back_to_hub_when_not_in_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Регрессия: навыка нет в сторе + залогинен → hub-chain как раньше."""
    cfg, target, _store, project = _wire(tmp_path, monkeypatch, logged_in=True)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "tok")
    calls: list[str] = []

    async def fake_chain(cfg_, access, *, slug, channel, scope, project_path,
                         force, agent_target):
        calls.append(slug)
        inst = SkillInstaller(agent_target, cfg_.effective_store_dir())
        res = inst.install(
            slug=slug, version="1.0.0", commit_sha="a1", repo_url=None,
            manifest={"version": "1.0.0", "files": []},
            project=project_path, force=force,
        )
        return [{"slug": slug, "skill_id": None, "version": "1.0.0",
                 "is_update": res.is_update, "target_dir": str(res.target_dir),
                 "scope": res.scope, "linked": res.linked,
                 "link_kind": res.link_kind}]

    monkeypatch.setattr(main_mod, "_install_chain", fake_chain)
    main_mod.cmd_enable(
        slug="hubskill", project=project, agent=None, force=False, channel="published"
    )
    assert calls == ["hubskill"]
    assert linker.is_link(target.slug_dir("hubskill", project=project))
    assert pm.load(project) == {"hubskill": "*"}
