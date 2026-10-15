"""A downloaded release serves several real agent scopes without copying it."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from skillery_cli.config import ClientConfig
from skillery_cli.core.agents import ClaudeCodeTarget, DeepSeekTarget
from skillery_cli.core.installer import SkillInstaller
from skillery_cli.core.shared_install import link_installed_chain, resolve_agent_targets


def seed(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    content = b"---\nname: sample\ndescription: Shared instructions\n---\nRelease one\n"
    (source / "SKILL.md").write_bytes(content)
    first = ClaudeCodeTarget(root=tmp_path / "claude")
    second = DeepSeekTarget(root=tmp_path / "deepseek")
    cfg = ClientConfig(store_dir=str(tmp_path / "store"), skill_projection="link")
    manifest = {"files": [{"path": "SKILL.md", "sha256": hashlib.sha256(content).hexdigest()}]}
    SkillInstaller(first, cfg.effective_store_dir(), projection="link").install(
        slug="sample", version="1", commit_sha="one", local_src=source, manifest=manifest,
    )
    return cfg, first, second


def test_secondary_links_same_payload_and_sees_source_update(tmp_path):
    cfg, first, second = seed(tmp_path)
    rows, refused = link_installed_chain(
        cfg, [{"slug": "sample", "version": "1", "agent_visible": True}],
        targets=[second], project=None,
    )
    assert not refused and rows[0]["linked"] is True
    assert first.slug_dir("sample").samefile(second.slug_dir("sample"))
    source = cfg.effective_store_dir() / "sample" / "SKILL.md"
    source.write_text("Updated centrally", encoding="utf-8")
    assert (second.slug_dir("sample") / "SKILL.md").read_text() == "Updated centrally"


def test_dependency_and_skipped_release_never_become_agent_skills(tmp_path):
    cfg, first, second = seed(tmp_path)
    rows, refused = link_installed_chain(
        cfg, [{"slug": "sample", "agent_visible": False},
              {"slug": "sample", "skipped": True}], targets=[second], project=None,
    )
    assert not rows and not refused and not second.base_dir().exists()


@pytest.mark.parametrize("kind", ["edited", "runtime", "foreign"])
def test_secondary_conflict_preserves_every_byte(tmp_path, kind):
    cfg, first, second = seed(tmp_path)
    slot = second.slug_dir("sample")
    if kind == "foreign":
        slot.mkdir(parents=True)
        (slot / "SKILL.md").write_text("Foreign instructions")
    else:
        SkillInstaller(second, cfg.effective_store_dir(), projection="copy").link_existing("sample")
        if kind == "edited":
            (slot / "SKILL.md").write_text("Local changes")
        else:
            (slot / "_local").mkdir()
            (slot / "_local" / "state").write_text("Private state")
    before = {p.relative_to(slot): p.read_bytes() for p in slot.rglob("*") if p.is_file()}
    rows, refused = link_installed_chain(
        cfg, [{"slug": "sample"}], targets=[second], project=None,
    )
    assert not rows and len(refused) == 1
    assert before == {p.relative_to(slot): p.read_bytes() for p in slot.rglob("*") if p.is_file()}


def test_clean_owned_copy_becomes_link_and_repeat_is_safe(tmp_path):
    cfg, first, second = seed(tmp_path)
    SkillInstaller(second, cfg.effective_store_dir(), projection="copy").link_existing("sample")
    for _ in range(2):
        rows, refused = link_installed_chain(cfg, [{"slug": "sample"}], targets=[second], project=None)
        assert len(rows) == 1 and not refused
        assert second.slug_dir("sample").samefile(first.slug_dir("sample"))


def test_target_arguments_validate_before_installation():
    assert [t.name for t in resolve_agent_targets("deepseek, codex,deepseek", None, "claude_code")] == ["deepseek", "codex"]
    with pytest.raises(ValueError, match="exclusive"):
        resolve_agent_targets("deepseek", "codex", "claude_code")
    with pytest.raises(ValueError):
        resolve_agent_targets("codex,unknown", None, "claude_code")
    with pytest.raises(ValueError, match="empty"):
        resolve_agent_targets(" , ", None, "claude_code")


def test_server_collection_materializes_once_then_links_other_agents(tmp_path, monkeypatch):
    from skillery_cli.commands import _common, collection
    from skillery_cli.core import shared_install

    cfg, first, second = seed(tmp_path)
    targets = {"claude_code": first, "deepseek": second}
    monkeypatch.setattr(shared_install, "get_target", targets.__getitem__)
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(_common, "get_access_token", lambda: "not-a-real-token")
    calls = []
    outputs = []

    class Client:
        async def get_collection(self, ref):
            return {"collection": {"id": "1", "slug": ref}, "skills": [{"slug": "sample"}]}

        async def close(self):
            pass

    async def install(*args, **kwargs):
        calls.append(kwargs["agent_target"].name)
        return [{"slug": "sample", "version": "1", "agent_visible": True, "scope": "global"}]

    monkeypatch.setattr(_common, "make_client", lambda *args: Client())
    monkeypatch.setattr(collection, "_install_chain", install)
    monkeypatch.setattr(collection, "_resolve_install_scope", lambda *args: ("global", None))
    monkeypatch.setattr(collection, "emit_data", lambda value, **kwargs: outputs.append(value))
    collection._install_server("all", "global", None, "published", False, None,
                               "claude_code,deepseek", "link")
    assert calls == ["claude_code"]
    assert outputs[0]["failed"] == []
    assert [row["agent"] for row in outputs[0]["installed"]] == ["claude_code", "deepseek"]
    assert first.slug_dir("sample").samefile(second.slug_dir("sample"))
