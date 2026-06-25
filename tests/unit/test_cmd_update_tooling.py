"""gap A (cmd_update): обновление версии навыка обязано переустановить tooling.

Раньше cmd_update обновлял КОНТЕНТ навыка (installer.install), но НЕ вызывал
_apply_tooling → runtime_dependencies/CLI-шимы/MCP оставались от старой версии.
Теперь после УСПЕШНОГО (НЕ skipped) обновления версии cmd_update вызывает
_apply_tooling(res, manifest=bundle["manifest"], agent_target=target, project=proj).

Все вызовы сети/installer/tooling мокаются — тест не трогает реальную ФС/PATH.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import InstallResult


def _setup_update_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    install_result: InstallResult,
    bundle_version: str = "9.9.9",
    repo_url: str | None = "https://git.example/demo.git",
    tooling_calls: list | None = None,
):
    import skills_hub_cli.__main__ as main_mod
    from skills_hub_cli.config import ClientConfig

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    skill_dir = target.slug_dir("demo")
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("hi", encoding="utf-8")
    (skill_dir / "_skill_meta.json").write_text(
        json.dumps({"slug": "demo", "version": "1.0.0"}), encoding="utf-8"
    )

    cfg = ClientConfig(store_dir=str(tmp_path / "store"), base_url="http://x")
    cfg.user_email = "x@y.io"
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "tok")
    monkeypatch.setattr(main_mod, "_make_refresh_callback", lambda c: None)

    bundle_manifest = {"version": bundle_version, "files": []}

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def install_bundle(self, slug: str, *a, **k):
            return {
                "version": bundle_version,
                "commit_sha": "deadbeef",
                "repo_url": repo_url,
                "manifest": bundle_manifest,
            }

        async def close(self):
            return None

    monkeypatch.setattr(main_mod, "HubClient", _FakeClient)
    monkeypatch.setattr(
        main_mod.SkillInstaller, "install", lambda self, **kw: install_result
    )
    monkeypatch.setattr(main_mod, "track_skill_event", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "emit_data", lambda data, **kw: None)

    if tooling_calls is not None:
        def _spy(result, manifest, *, agent_target, project):
            tooling_calls.append(
                {
                    "result": result,
                    "manifest": manifest,
                    "agent_target": agent_target,
                    "project": project,
                }
            )

        monkeypatch.setattr(main_mod, "_apply_tooling", _spy)

    return main_mod, target, bundle_manifest


def test_cmd_update_applies_tooling_after_real_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gap A: реальное обновление версии → _apply_tooling вызван с manifest
    бандла, agent_target=target и project=None (global scope)."""
    tooling_calls: list = []
    real = InstallResult(
        slug="demo", version="9.9.9", target_dir=tmp_path / "x",
        is_update=True, scope="global",
        update_diff={"added": 1, "changed": 0, "removed": 0},
    )
    main_mod, target, bundle_manifest = _setup_update_env(
        tmp_path, monkeypatch, install_result=real, tooling_calls=tooling_calls
    )

    main_mod.cmd_update(slug="demo", all_=False, channel="published",
                        project=None, scope="global")

    assert len(tooling_calls) == 1, "_apply_tooling должен вызваться при обновлении"
    call = tooling_calls[0]
    assert call["manifest"] == bundle_manifest
    assert call["agent_target"] is target
    assert call["project"] is None  # global scope
    assert call["result"] is real


def test_cmd_update_skips_tooling_when_install_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gap A: install вернул skipped (stub-guard) → _apply_tooling НЕ зовётся."""
    tooling_calls: list = []
    skipped = InstallResult(
        slug="demo", version="1.0.0", target_dir=tmp_path / "x",
        is_update=False, scope="global",
        skipped=True, skip_reason="stub-would-clobber", content="stub",
    )
    main_mod, _, _ = _setup_update_env(
        tmp_path, monkeypatch, install_result=skipped, tooling_calls=tooling_calls
    )

    main_mod.cmd_update(slug="demo", all_=False, channel="published",
                        project=None, scope="global")

    assert tooling_calls == [], "skipped install не должен переустанавливать tooling"


def test_cmd_update_skips_tooling_when_version_equal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gap A: опубликованная версия == установленной → install/​tooling не зовём."""
    tooling_calls: list = []
    # install_result не важен — до install не дойдёт (версия равна).
    noop = InstallResult(
        slug="demo", version="1.0.0", target_dir=tmp_path / "x",
        is_update=False, scope="global",
    )
    main_mod, _, _ = _setup_update_env(
        tmp_path, monkeypatch, install_result=noop,
        bundle_version="1.0.0", tooling_calls=tooling_calls,
    )

    main_mod.cmd_update(slug="demo", all_=False, channel="published",
                        project=None, scope="global")

    assert tooling_calls == []


def test_cmd_update_applies_tooling_with_project_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gap A: project-scope обновление → _apply_tooling получает project=<proj>."""
    import skills_hub_cli.__main__ as main_mod  # noqa: F401  (импорт ниже через setup)
    tooling_calls: list = []
    real = InstallResult(
        slug="demo", version="9.9.9", target_dir=tmp_path / "x",
        is_update=True, scope="project",
    )
    proj = tmp_path / "proj"
    proj.mkdir()

    main_mod, target, bundle_manifest = _setup_update_env(
        tmp_path, monkeypatch, install_result=real, tooling_calls=tooling_calls
    )
    # Установленный навык в project-scope (помимо global из setup).
    proj_skill = target.slug_dir("demo", project=proj)
    proj_skill.mkdir(parents=True)
    (proj_skill / "SKILL.md").write_text("hi", encoding="utf-8")
    (proj_skill / "_skill_meta.json").write_text(
        json.dumps({"slug": "demo", "version": "1.0.0"}), encoding="utf-8"
    )

    main_mod.cmd_update(slug="demo", all_=False, channel="published",
                        project=proj, scope="project")

    assert len(tooling_calls) == 1
    assert tooling_calls[0]["project"] == proj
    assert tooling_calls[0]["manifest"] == bundle_manifest
