"""Container payload views share real files and exclude private scope state."""
import pytest
from skillkit import linker

from skillery_cli.core import local_collections, payload_views
from skillery_cli.core.installer import read_meta, write_meta
from tests.unit.test_agent_reconciliation import seed


def setup_view(tmp_path, monkeypatch):
    from skillery_cli.config import ClientConfig

    target, _ = seed(tmp_path)
    source = tmp_path / "store" / "sample"
    meta = read_meta(source)
    write_meta(source, {**meta, "source": "hub"})
    monkeypatch.setattr(local_collections, "get", lambda name: {"skills": ["sample"]})
    cfg = ClientConfig(store_dir=str(tmp_path / "store"))
    return cfg, target, source, tmp_path / "view"


def test_view_shares_payload_inodes_but_never_runtime_or_cache(tmp_path, monkeypatch):
    cfg, target, source, view = setup_view(tmp_path, monkeypatch)
    (source / "_local").mkdir()
    (source / "_local" / "account.json").write_text("private fixture state")
    (source / ".env").write_text("PLACEHOLDER=value")
    (source / ".pkgsrc").mkdir()
    (source / ".pkgsrc" / "private-cache").write_text("private fixture cache")
    result = payload_views.expose_payload_view(cfg, collection="private", path=view)
    assert result["payload_copied"] is False and result["runtime_files_shared"] is False
    assert (view / "sample" / "SKILL.md").samefile(source / "SKILL.md")
    assert (view / "sample" / "_skill_meta.json").samefile(source / "_skill_meta.json")
    for name in ("_local", ".env", ".pkgsrc"):
        assert not (view / "sample" / name).exists()
    assert payload_views.refresh_payload_views(cfg)[0]["hardlinks_created"] == 0


def test_source_update_refreshes_links_and_keeps_bind_directory_identity(tmp_path, monkeypatch):
    cfg, target, source, view = setup_view(tmp_path, monkeypatch)
    payload_views.expose_payload_view(cfg, collection="private", path=view)
    root_inode, slot_inode = view.stat().st_ino, (view / "sample").stat().st_ino
    seed(tmp_path, version="2", target=target)
    write_meta(source, {**read_meta(source), "source": "hub"})
    from skillery_cli.core import agent_reconciliation

    monkeypatch.setattr(agent_reconciliation, "get_target", lambda name: target)
    report = agent_reconciliation.reconcile_managed_scopes(cfg)
    assert report["payload_views"][0]["accepted"] is True
    assert read_meta(view / "sample")["version"] == "2"
    assert (view / "sample" / "_skill_meta.json").samefile(source / "_skill_meta.json")
    assert "Release 2" in (view / "sample" / "SKILL.md").read_text()
    assert root_inode == view.stat().st_ino and slot_inode == (view / "sample").stat().st_ino


@pytest.mark.parametrize("kind", ["shared-edit", "replaced-file", "extra-file", "directory-link"])
def test_edited_or_unknown_view_is_preserved(tmp_path, monkeypatch, kind):
    cfg, target, source, view = setup_view(tmp_path, monkeypatch)
    payload_views.expose_payload_view(cfg, collection="private", path=view)
    slot = view / "sample" / "SKILL.md"
    if kind == "shared-edit":
        slot.write_text("local modifications")
    elif kind == "replaced-file":
        extra = tmp_path / "replacement"
        extra.write_text("independent local modifications")
        extra.replace(slot)
    elif kind == "extra-file":
        (view / "personal-notes").write_text("local notes")
    else:
        linker.create_link(view / "outside", tmp_path)
    before = slot.read_bytes()
    report = payload_views.refresh_payload_views(cfg)
    assert report[0]["accepted"] is False
    assert slot.read_bytes() == before


def test_foreign_root_and_source_overlaps_are_refused_before_registration(tmp_path, monkeypatch):
    cfg, target, source, view = setup_view(tmp_path, monkeypatch)
    view.mkdir()
    (view / "personal").write_text("preserved")
    with pytest.raises(ValueError, match="unregistered"):
        payload_views.expose_payload_view(cfg, collection="private", path=view)
    with pytest.raises(ValueError, match="separate"):
        payload_views.expose_payload_view(cfg, collection="private", path=source)
    assert not payload_views.refresh_payload_views(cfg)
    assert (view / "personal").read_text() == "preserved"


def test_source_update_refreshes_views_when_agent_sync_is_disabled(tmp_path, monkeypatch):
    cfg, target, source, view = setup_view(tmp_path, monkeypatch)
    assert not cfg.sync_agents
    payload_views.expose_payload_view(cfg, collection="private", path=view)
    seed(tmp_path, version="2", target=target)
    write_meta(source, {**read_meta(source), "source": "hub"})
    from skillery_cli.core.agent_reconciliation import attach_scope_report

    chain = [{"slug": "sample"}]
    attach_scope_report(cfg, chain, project=None)
    assert chain[0]["payload_views"][0]["accepted"] is True
    assert read_meta(view / "sample")["version"] == "2"


def test_expose_command_is_available_without_hub_login():
    from typer.testing import CliRunner
    from skillery_cli.__main__ import build_app

    result = CliRunner().invoke(build_app(), ["store", "expose", "--help"])
    assert result.exit_code == 0
    assert "--collection" in result.stdout
