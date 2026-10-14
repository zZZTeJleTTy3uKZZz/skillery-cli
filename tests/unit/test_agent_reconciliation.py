"""Real managed copies converge without adopting or overwriting other scopes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest
from skillery_cli.config import ClientConfig
from skillery_cli.core.agents import ClaudeCodeTarget, CodexTarget
from skillery_cli.core.installer import SkillInstaller, read_meta


def seed(tmp_path: Path, *, version: str = "1", target=None):
    target = target or ClaudeCodeTarget(root=tmp_path / "claude")
    source = tmp_path / f"source-{version}"
    source.mkdir(exist_ok=True)
    payload = f"---\nname: sample\n---\nRelease {version}\n"
    (source / "SKILL.md").write_text(payload, encoding="utf-8")
    manifest = {"files": [{"path": "SKILL.md", "sha256": hashlib.sha256(
        (source / "SKILL.md").read_bytes()).hexdigest()}], "preserved_paths": ["_local/"]}
    installer = SkillInstaller(target, tmp_path / "store", projection="copy")
    result = installer.install(slug="sample", version=version, commit_sha=version,
                               local_src=source, manifest=manifest)
    assert not result.skipped
    return target, installer


def setup_copies(tmp_path: Path, monkeypatch):
    from skillery_cli.core import agent_reconciliation as reconcile

    claude, owner = seed(tmp_path)
    codex = CodexTarget(root=tmp_path / "codex")
    SkillInstaller(codex, tmp_path / "store", projection="copy").link_existing("sample")
    targets = {"claude_code": claude, "codex": codex}
    monkeypatch.setattr(reconcile, "get_target", targets.__getitem__)
    cfg = ClientConfig(agent="claude_code", sync_agents=["claude_code", "codex"],
                       store_dir=str(tmp_path / "store"), skill_projection="copy")
    return reconcile, cfg, claude, codex, owner


def test_configured_agents_round_trip_and_legacy_default(tmp_path: Path):
    config = tmp_path / "config.toml"
    ClientConfig(agent="claude_code").save(config)
    assert ClientConfig.load(config).sync_agents == []
    assert "sync_agents" not in config.read_text(encoding="utf-8")
    ClientConfig(sync_agents=["claude_code", "codex", "codex"]).save(config)
    assert ClientConfig.load(config).sync_agents == ["claude_code", "codex"]
    with pytest.raises(ValueError, match="sync_agents"):
        ClientConfig(sync_agents=["unknown"])


def test_home_fixture_removes_ambient_agent_roots_before_target_resolution(tmp_path, monkeypatch):
    from skillery_cli.core.agents import DeepSeekTarget

    from tests.conftest import isolated_home

    for variable in ("CODEX_HOME", "DSH_HOME", "DSH_AGENTS_HOME"):
        monkeypatch.setenv(variable, str(tmp_path / "ambient" / variable))
    safe_home = isolated_home.__wrapped__(tmp_path / "fresh", monkeypatch)
    for target in (CodexTarget(), DeepSeekTarget()):
        assert target.base_dir().is_relative_to(safe_home)
        config = target.mcp_config()
        if config is not None:
            assert config.path.is_relative_to(safe_home)
    assert not (tmp_path / "ambient").exists()


def test_clean_copies_update_and_repeat_is_unchanged(tmp_path: Path, monkeypatch):
    reconcile, cfg, claude, codex, _owner = setup_copies(tmp_path, monkeypatch)
    state = codex.slug_dir("sample") / "_local" / "state.json"
    state.parent.mkdir()
    state.write_text("private-state", encoding="utf-8")
    seed(tmp_path, version="2", target=claude)
    report = reconcile.reconcile_managed_scopes(cfg)
    assert [entry["agent"] for entry in report["updated"]] == ["codex"]
    assert not report["refused"]
    assert read_meta(codex.slug_dir("sample"))["version"] == "2"
    assert state.read_text(encoding="utf-8") == "private-state"
    before = (codex.slug_dir("sample") / "_skill_meta.json").stat().st_mtime_ns
    repeat = reconcile.reconcile_managed_scopes(cfg)
    assert not repeat["updated"]
    assert len(repeat["unchanged"]) == 2
    assert before == (codex.slug_dir("sample") / "_skill_meta.json").stat().st_mtime_ns


def test_dirty_secondary_reports_partial_and_preserves_edit(tmp_path: Path, monkeypatch):
    reconcile, cfg, claude, codex, _owner = setup_copies(tmp_path, monkeypatch)
    (codex.slug_dir("sample") / "SKILL.md").write_text("local edit", encoding="utf-8")
    seed(tmp_path, version="2", target=claude)
    report = reconcile.reconcile_managed_scopes(cfg)
    assert read_meta(tmp_path / "store" / "sample")["version"] == "2"
    assert read_meta(claude.slug_dir("sample"))["version"] == "2"
    assert report["partial"] is True
    assert report["refused"][0]["reason"] == "projection-local-modifications"
    assert (codex.slug_dir("sample") / "SKILL.md").read_text() == "local edit"


def test_missing_and_foreign_scopes_are_never_adopted(tmp_path: Path, monkeypatch):
    from skillery_cli.core import agent_reconciliation as reconcile

    claude, _owner = seed(tmp_path)
    codex = CodexTarget(root=tmp_path / "codex")
    monkeypatch.setattr(
        reconcile, "get_target", {"claude_code": claude, "codex": codex}.__getitem__,
    )
    cfg = ClientConfig(agent="claude_code", sync_agents=["claude_code", "codex"],
                       store_dir=str(tmp_path / "store"), skill_projection="copy")
    reconcile.reconcile_managed_scopes(cfg)
    assert not codex.base_dir().exists()
    foreign = codex.slug_dir("sample")
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("foreign", encoding="utf-8")
    report = reconcile.reconcile_managed_scopes(cfg)
    assert (foreign / "SKILL.md").read_text() == "foreign"
    assert not (foreign / "_skill_meta.json").exists()
    assert report["refused"][0]["reason"] == "unmanaged-scope"


def test_only_registered_project_scopes_are_reconciled(tmp_path: Path, monkeypatch):
    reconcile, cfg, claude, codex, _owner = setup_copies(tmp_path, monkeypatch)
    registered = tmp_path / "registered"
    unrelated = tmp_path / "unrelated"
    from skillery_cli.core import project_manifest

    for project in (registered, unrelated):
        project_manifest.add(project, "sample")
        SkillInstaller(codex, tmp_path / "store", projection="copy").link_existing(
            "sample", project=project)
    monkeypatch.setattr(reconcile, "registered_project_roots", lambda: [registered])
    seed(tmp_path, version="2", target=claude)
    report = reconcile.reconcile_managed_scopes(cfg)
    assert read_meta(codex.slug_dir("sample", project=registered))["version"] == "2"
    assert read_meta(codex.slug_dir("sample", project=unrelated))["version"] == "1"
    assert any(entry["project"] == str(registered) for entry in report["updated"])


def test_unverifiable_copy_is_refused_without_changing_its_metadata(tmp_path, monkeypatch):
    reconcile, cfg, claude, codex, _owner = setup_copies(tmp_path, monkeypatch)
    path = codex.slug_dir("sample") / "_skill_meta.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    meta.pop("projection")
    meta["manifest"] = {"files": []}
    path.write_text(json.dumps(meta), encoding="utf-8")
    before = path.read_bytes()
    seed(tmp_path, version="2", target=claude)
    report = reconcile.reconcile_managed_scopes(cfg)
    assert report["refused"][0]["reason"] == "projection-unverifiable-baseline"
    assert path.read_bytes() == before


@pytest.mark.parametrize("project_scope", [False, True])
@pytest.mark.parametrize("dirty", [False, True])
def test_existing_legacy_codex_copy_converges_without_moving_or_losing_edits(
    tmp_path, monkeypatch, project_scope, dirty,
):
    from skillery_cli.core import agent_reconciliation as reconcile
    from skillery_cli.core import project_manifest

    claude, _owner = seed(tmp_path)
    codex = CodexTarget(root=tmp_path / ".agents")
    project = tmp_path / "registered" if project_scope else None
    if project is not None:
        project_manifest.add(project, "sample")
    legacy_base = codex.legacy_base_dirs(project=project)[0]
    # A custom global root models an existing installation at this exact path.
    legacy = CodexTarget(root=legacy_base.parent)
    SkillInstaller(legacy, tmp_path / "store", projection="copy").link_existing("sample")
    slot = legacy_base / "sample"
    runtime = slot / "_local" / "state.json"
    runtime.parent.mkdir()
    runtime.write_text("private-state", encoding="utf-8")
    if dirty:
        (slot / "SKILL.md").write_text("local edit", encoding="utf-8")
    before_meta = (slot / "_skill_meta.json").read_bytes()
    foreign = legacy_base / "foreign"
    foreign.mkdir()
    (foreign / "SKILL.md").write_text("foreign", encoding="utf-8")
    monkeypatch.setattr(reconcile, "get_target", {"codex": codex}.__getitem__)
    monkeypatch.setattr(reconcile, "registered_project_roots",
                        lambda: [project] if project is not None else [])
    seed(tmp_path, version="2", target=claude)
    cfg = ClientConfig(agent="codex", sync_agents=["codex"],
                       store_dir=str(tmp_path / "store"), skill_projection="copy")

    report = reconcile.reconcile_managed_scopes(cfg)

    assert read_meta(slot)["version"] == ("1" if dirty else "2")
    assert runtime.read_text(encoding="utf-8") == "private-state"
    assert not codex.base_dir(project=project).exists()
    assert (foreign / "SKILL.md").read_text(encoding="utf-8") == "foreign"
    assert not (foreign / "_skill_meta.json").exists()
    if dirty:
        assert report["refused"][0]["reason"] == "projection-local-modifications"
        assert (slot / "SKILL.md").read_text(encoding="utf-8") == "local edit"
        assert (slot / "_skill_meta.json").read_bytes() == before_meta
    else:
        assert len(report["updated"]) == 1 and not report["refused"]


def test_explicit_codex_home_does_not_reconcile_sibling_legacy_root(tmp_path, monkeypatch):
    from skillery_cli.core import agent_reconciliation as reconcile

    claude, _owner = seed(tmp_path)
    legacy = CodexTarget(root=tmp_path / ".codex")
    SkillInstaller(legacy, tmp_path / "store", projection="copy").link_existing("sample")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "isolated-runtime"))
    codex = CodexTarget()
    monkeypatch.setattr(reconcile, "get_target", {"codex": codex}.__getitem__)
    seed(tmp_path, version="2", target=claude)
    cfg = ClientConfig(agent="codex", sync_agents=["codex"],
                       store_dir=str(tmp_path / "store"), skill_projection="copy")

    report = reconcile.reconcile_managed_scopes(cfg)

    assert not report["updated"] and not report["refused"]
    assert read_meta(legacy.slug_dir("sample"))["version"] == "1"
    assert not codex.base_dir().exists()


def test_junction_scope_uses_public_copy_gate_without_reading_blocked_link(tmp_path, monkeypatch):
    from skillkit import linker

    reconcile, cfg, claude, codex, _owner = setup_copies(tmp_path, monkeypatch)
    installer = SkillInstaller(codex, tmp_path / "store", projection="link")
    installer.remove(slug="sample")
    installer.link_existing("sample")
    slot = codex.slug_dir("sample")
    assert linker.is_link(slot)
    seed(tmp_path, version="2", target=claude)
    original = reconcile.read_meta
    blocked_reads = []

    def guarded_read(path):
        if linker.is_link(path):
            blocked_reads.append(str(path))
            error = PermissionError("Untrusted mount point")
            error.winerror = 448
            raise error
        return original(path)

    monkeypatch.setattr(reconcile, "read_meta", guarded_read)
    result = reconcile.reconcile_managed_scopes(cfg)
    assert not result["refused"] and not blocked_reads
    assert not linker.is_link(slot)
    assert read_meta(slot)["version"] == "2"


def test_registry_is_explicit_bounded_and_does_not_claim_invalid_state(tmp_path, monkeypatch):
    from skillery_cli.core import agent_reconciliation as reconcile
    from skillery_cli.core import project_manifest

    registry = tmp_path / "config" / "managed-scopes.json"
    monkeypatch.setattr(reconcile, "_registry_path", lambda: registry)
    project = tmp_path / "project"
    reconcile.register_project_scope(project)
    assert not registry.exists()
    project_manifest.add(project, "sample")
    reconcile.register_project_scope(project)
    before = registry.read_bytes()
    reconcile.register_project_scope(project)
    assert registry.read_bytes() == before
    assert reconcile.registered_project_roots() == [project]
    registry.write_text('{"schema":1,"projects":["relative"]}', encoding="utf-8")
    with pytest.raises(ValueError, match="absolute"):
        reconcile.register_project_scope(project)
    assert registry.read_text() == '{"schema":1,"projects":["relative"]}'


@pytest.mark.parametrize("dirty", [False, True])
def test_requested_primary_update_propagates_and_exposes_partial(tmp_path, monkeypatch, dirty):
    import skillery_cli.__main__ as main

    _reconcile, cfg, claude, codex, _owner = setup_copies(tmp_path, monkeypatch)
    source = tmp_path / "requested-source"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: sample\nversion: 2.0.0\n---\nNew source\n", encoding="utf-8")
    monkeypatch.setattr(main, "_apply_tooling", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_emit_onboarding", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "track_skill_event", lambda *args, **kwargs: None)
    if dirty:
        (codex.slug_dir("sample") / "SKILL.md").write_text("local edit", encoding="utf-8")
    chain = asyncio.run(main._install_local_source(
        cfg, source={"kind": "path", "slug": "sample", "path": source},
        scope="global", project_path=None, force=False, agent_target=claude))
    assert chain[0]["version"] == "2.0.0" and not chain[0].get("skipped")
    assert chain[0]["partial"] is dirty
    if dirty:
        assert chain[0]["scope_reconciliation"]["refused"][0]["agent"] == "codex"
    assert read_meta(claude.slug_dir("sample"))["version"] == "2.0.0"
    assert read_meta(codex.slug_dir("sample"))["version"] == ("1" if dirty else "2.0.0")


def test_foreground_cooldown_still_reconciles_existing_store_and_reports_refusal(
    tmp_path, monkeypatch, capsys,
):
    from datetime import UTC, datetime

    import skillery_cli.__main__ as main

    _reconcile, cfg, claude, codex, _owner = setup_copies(tmp_path, monkeypatch)
    seed(tmp_path, version="2", target=claude)
    monkeypatch.setattr(ClientConfig, "is_logged_in", lambda self: True)
    cfg.last_auto_update_at = datetime.now(UTC).isoformat()
    (codex.slug_dir("sample") / "SKILL.md").write_text("local edit", encoding="utf-8")
    main._maybe_auto_update(cfg)
    assert "projection-local-modifications" in capsys.readouterr().err
    assert read_meta(codex.slug_dir("sample"))["version"] == "1"
    cfg.auto_update = False
    monkeypatch.setattr(
        _reconcile, "reconcile_managed_scopes", lambda *args, **kwargs: pytest.fail("Disabled"),
    )
    main._maybe_auto_update(cfg)


@pytest.mark.parametrize("dirty", [False, True])
def test_daemon_latest_update_converges_existing_copies_with_truthful_report(
    tmp_path, monkeypatch, dirty,
):
    import skillery_cli.__main__ as main
    from skillery_cli.core import store_migrations
    from skillery_cli.core.installer import write_meta

    _reconcile, cfg, claude, codex, _owner = setup_copies(tmp_path, monkeypatch)
    source_meta = read_meta(tmp_path / "store" / "sample")
    source_meta["source"] = "hub"
    write_meta(tmp_path / "store" / "sample", source_meta)
    if dirty:
        (codex.slug_dir("sample") / "SKILL.md").write_text("local edit", encoding="utf-8")

    class Hub:
        def __init__(self, **kwargs):
            pass

        async def install_bundle(self, *args, **kwargs):
            return {"version": "2", "repo_url": "https://example.invalid/repository"}

        async def close(self):
            pass

    async def update(*args, **kwargs):
        seed(tmp_path, version="2", target=claude)
        return [{"slug": "sample", "version": "2"}]

    async def report(*args, **kwargs):
        pass

    monkeypatch.setattr(main, "HubClient", Hub)
    monkeypatch.setattr(main, "_install_chain", update)
    monkeypatch.setattr(main, "_report_auto_updates", report)
    monkeypatch.setattr(main, "_touch_auto_update_cooldown", lambda config: None)
    monkeypatch.setattr(store_migrations, "ensure_store_meta_migrated", lambda config: None)
    monkeypatch.setattr(store_migrations, "ensure_shims_route_through_runner", lambda: None)
    result = asyncio.run(main._auto_update_hub_installs(cfg, "fixture", agent_target=claude))
    assert result["updated"] == ["sample"]
    assert result["partial"] is dirty
    assert read_meta(codex.slug_dir("sample"))["version"] == ("1" if dirty else "2")
