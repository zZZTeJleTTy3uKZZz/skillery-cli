"""A skipped SDK update must not mutate CLI-owned metadata or tooling."""
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import skillery_cli.__main__ as main
from skillery_cli.config import ClientConfig
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import write_meta


@pytest.mark.asyncio
async def test_skipped_hub_update_has_no_success_side_effects(tmp_path: Path, monkeypatch):
    store = tmp_path / "store"
    payload = store / "demo"
    payload.mkdir(parents=True)
    body = b"# Existing edited skill\n"
    (payload / "SKILL.md").write_bytes(body)
    write_meta(payload, {"slug": "demo", "source": "hub", "version": "1.0.0",
                        "manifest": {"files": [{"path": "SKILL.md",
                                                 "sha256": hashlib.sha256(body).hexdigest()}]},
                        "install_reason": "dependency", "required_by": ["owner"]})
    metadata = (payload / "_skill_meta.json").read_bytes()
    cfg = ClientConfig(store_dir=str(store))
    target = ClaudeCodeTarget(root=tmp_path / ".claude")

    class Client:
        async def install_bundle(self, slug, **kwargs):
            return {"skill_slug": slug, "skill_id": 42, "version": "2.0.0",
                    "repo_url": "https://git.example/skill.git", "manifest": {}}

        async def close(self):
            pass

    monkeypatch.setattr(main, "HubClient", lambda **kwargs: Client())
    monkeypatch.setattr(main, "SkillInstaller", lambda *args, **kwargs: object())

    async def skipped(*args, **kwargs):
        return SimpleNamespace(skipped=True, skip_reason="local-modifications",
                               conflict_paths=("SKILL.md",), store_dir=payload,
                               target_dir=target.slug_dir("demo"), skill_id="42",
                               version="2.0.0", scope="global", linked=False,
                               link_kind="none", is_update=False, content="real")

    def forbidden(*args, **kwargs):
        raise AssertionError("Skipped update must have no success side effects")

    monkeypatch.setattr(main, "_materialize_from_bundle", skipped)
    monkeypatch.setattr(main, "_apply_tooling", forbidden)
    monkeypatch.setattr(main, "track_skill_event", forbidden)
    monkeypatch.setattr(main.install_reason, "stamp", forbidden)
    result = await main._install_chain(cfg, "fixture", slug="demo", channel="published",
                                       scope="global", project_path=None, force=True,
                                       agent_target=target, headless=True)
    assert result[0]["skipped"] is True
    assert result[0]["version"] == "1.0.0"
    assert result[0]["conflict_paths"] == ["SKILL.md"]
    assert (payload / "_skill_meta.json").read_bytes() == metadata
    assert (payload / "SKILL.md").read_bytes() == body
