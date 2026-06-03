"""sync: привести project scope к манифесту (линк из стора + prune чужого нет)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skills_hub_cli.core import linker, project_manifest as pm
from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import SkillInstaller

_MANIFEST = {"version": "1.0.0", "description": "x", "files": []}


def _seed_store(store: Path, name: str) -> Path:
    d = store / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("hi", encoding="utf-8")
    (d / "_skill_meta.json").write_text("{}", encoding="utf-8")
    return d


def test_link_existing_links_from_store(tmp_path: Path) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    _seed_store(store, "bitrix24")
    inst = SkillInstaller(target, store_dir=store)
    project = tmp_path / "proj"
    out = inst.link_existing("bitrix24", project=project)
    assert out is not None
    assert linker.is_link(target.slug_dir("bitrix24", project=project))


def test_link_existing_none_when_not_in_store(tmp_path: Path) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    inst = SkillInstaller(target, store_dir=store)
    assert inst.link_existing("ghost", project=tmp_path / "proj") is None


def test_cmd_sync_links_manifest_and_prunes_ours_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import skills_hub_cli.__main__ as main_mod
    from skills_hub_cli.config import ClientConfig
    from skills_hub_cli import output as out_mod

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir()
    # В сторе есть bitrix24 (в манифесте) и stale (наша ссылка, НЕ в манифесте).
    _seed_store(store, "bitrix24")
    _seed_store(store, "stale")
    pm.add(project, "bitrix24")

    inst = SkillInstaller(target, store_dir=store)
    # Предварительно слинкуем stale (наша ссылка на стор) — её sync должен убрать.
    inst.link_existing("stale", project=project)
    # И внешняя ссылка на чужой каталог — НЕ наша, prune не должен трогать.
    external_src = tmp_path / "external" / "foreign"
    external_src.mkdir(parents=True)
    (external_src / "SKILL.md").write_text("x", encoding="utf-8")
    linker.create_link(target.slug_dir("foreign", project=project), external_src)

    cfg = ClientConfig(store_dir=str(store))
    cfg.permissions = ["skill.install"]
    cfg.user_email = "x@y.io"
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(out_mod, "_mode", "json")

    main_mod.cmd_sync(project=project, prune=True, agent=None, channel="published")

    # bitrix24 слинкован, stale выпилен, foreign (внешний) цел.
    assert linker.is_link(target.slug_dir("bitrix24", project=project))
    assert not target.slug_dir("stale", project=project).exists()
    assert linker.is_link(target.slug_dir("foreign", project=project))
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "bitrix24" in payload["linked"]
    assert "stale" in payload["pruned"]
    assert "foreign" not in payload["pruned"]
