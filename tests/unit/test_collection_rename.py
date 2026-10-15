"""Collection identity changes preserve members and live payload bindings."""
import json
from pathlib import Path

import pytest
import respx
from httpx import Response
from typer.testing import CliRunner

from skillery_cli.config import ClientConfig
from skillery_cli.core import local_collections, payload_views
from skillery_cli.core.installer import read_meta, write_meta
from skillery_cli.core.transport import HubClient
from tests.unit.test_agent_reconciliation import seed


@pytest.mark.asyncio
@pytest.mark.parametrize("ref", ["fleet-shared-workflow", "12"])
async def test_remote_rename_patches_resolved_identity(ref):
    with respx.mock(base_url="http://localhost:8000") as router:
        if ref != "12":
            router.get("/collections/fleet-shared-workflow").respond(
                200, json={"collection": {"id": 12, "slug": ref}})
        route = router.patch("/collections/12").respond(
            200, json={"id": 12, "slug": "my-skills", "access_level": "private"})
        client = HubClient(base_url="http://localhost:8000")
        try:
            result = await client.update_collection(ref, new_slug="my-skills")
        finally:
            await client.close()
    assert json.loads(route.calls[0].request.content) == {"slug": "my-skills"}
    assert result["id"] == 12 and result["access_level"] == "private"


def test_remote_rename_cli_accepts_slug_only_change(monkeypatch):
    from skillery_cli.__main__ import build_app
    from skillery_cli.commands import _common

    cfg = ClientConfig(base_url="http://localhost:8000", user_email="fixture@example.com",
                       permissions=["skill.read", "catalog.manage"])
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(_common, "get_access_token", lambda: "fixture-token")
    with respx.mock(base_url=cfg.base_url) as router:
        router.get("/collections/fleet-shared-workflow").respond(
            200, json={"collection": {"id": 12, "slug": "fleet-shared-workflow"}})
        route = router.patch("/collections/12").respond(200, json={"id": 12, "slug": "my-skills"})
        result = CliRunner().invoke(build_app(), [
            "--json", "collection", "edit", "fleet-shared-workflow", "--new-slug", "my-skills"])
    assert result.exit_code == 0, result.output
    assert json.loads(route.calls[0].request.content) == {"slug": "my-skills"}


def local_view(tmp_path):
    target, _ = seed(tmp_path)
    source = tmp_path / "store" / "sample"
    write_meta(source, {**read_meta(source), "source": "hub"})
    local_collections.save_all({"fleet-shared-workflow": {
        "title": "My skills", "skills": ["sample"]}})
    cfg = ClientConfig(store_dir=str(tmp_path / "store"))
    view = tmp_path / "view"
    payload_views.expose_payload_view(cfg, collection="fleet-shared-workflow", path=view)
    return cfg, target, source, view


def test_local_rename_keeps_live_directories_and_refreshes_next_release(tmp_path):
    cfg, target, source, view = local_view(tmp_path)
    before = view.stat().st_ino, (view / "sample").stat().st_ino
    payload_inode = (view / "sample" / "SKILL.md").stat().st_ino
    result = payload_views.rename_local_collection(cfg, "fleet-shared-workflow", "my-skills")
    assert result["name"] == "my-skills"
    assert local_collections.get("fleet-shared-workflow") is None
    assert local_collections.get("my-skills") == {"title": "My skills", "skills": ["sample"]}
    assert before == (view.stat().st_ino, (view / "sample").stat().st_ino)
    assert payload_inode == (view / "sample" / "SKILL.md").stat().st_ino
    backup = json.loads(Path(result["backup"]).read_text())
    assert backup["collections"]["fleet-shared-workflow"]["skills"] == ["sample"]
    seed(tmp_path, version="2", target=target)
    write_meta(source, {**read_meta(source), "source": "hub"})
    refreshed = payload_views.refresh_payload_views(cfg)
    assert refreshed[0]["accepted"] is True
    assert refreshed[0]["collection"] == "my-skills"
    assert read_meta(view / "sample")["version"] == "2"
    assert (view / "sample" / "SKILL.md").samefile(source / "SKILL.md")


@pytest.mark.parametrize("failure", ["occupied", "invalid", "edited", "unknown", "identity"])
def test_local_rename_refuses_conflicts_without_partial_changes(tmp_path, failure):
    cfg, _, _, view = local_view(tmp_path)
    name = "../invalid" if failure == "invalid" else "my-skills"
    if failure == "occupied":
        local_collections.create(name)
    elif failure == "edited":
        (view / "sample" / "SKILL.md").write_text("personal edit")
    elif failure == "unknown":
        (view / "notes").write_text("personal notes")
    elif failure == "identity":
        marker = view / payload_views.MARKER
        data = json.loads(marker.read_text())
        data["identity"] = "foreign-owner"
        marker.write_text(json.dumps(data))
    before = local_collections.collections_path().read_bytes()
    registry = payload_views._registry()[0]
    registry_before = registry.read_bytes()
    with pytest.raises(ValueError):
        payload_views.rename_local_collection(cfg, "fleet-shared-workflow", name)
    assert before == local_collections.collections_path().read_bytes()
    assert registry_before == registry.read_bytes()


def test_local_rename_cli_works_without_login_and_preserves_title():
    from skillery_cli.__main__ import build_app

    local_collections.save_all({"old": {"title": "My skills", "skills": ["sample"]}})
    result = CliRunner().invoke(build_app(), [
        "--json", "collection", "edit", "old", "--local", "--new-slug", "my-skills"])
    assert result.exit_code == 0, result.output
    assert local_collections.get("my-skills") == {"title": "My skills", "skills": ["sample"]}


def test_interrupted_retarget_retains_original_and_durable_recovery(tmp_path, monkeypatch):
    cfg, _, _, _ = local_view(tmp_path)
    registry = payload_views._registry()[0]
    write = payload_views._write

    def interrupted(path, data):
        if path == registry:
            raise OSError("simulated interrupted registry write")
        write(path, data)

    monkeypatch.setattr(payload_views, "_write", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        payload_views.rename_local_collection(cfg, "fleet-shared-workflow", "my-skills")
    assert local_collections.get("fleet-shared-workflow")["skills"] == ["sample"]
    assert local_collections.get("my-skills")["skills"] == ["sample"]
    assert payload_views.refresh_payload_views(cfg)[0]["accepted"] is True
    backups = list((registry.parent / "collection-renames").glob("*.json"))
    assert len(backups) == 1
    original = json.loads(backups[0].read_text())["collections"]
    assert list(original) == ["fleet-shared-workflow"]

