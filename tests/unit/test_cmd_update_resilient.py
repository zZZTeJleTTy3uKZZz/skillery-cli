"""RE4: cmd_update устойчив к навыкам НЕ из хаба.

Раньше первый же установленный навык, которого нет в хабе (локальный/ручной —
bitrix24, *-local), бросал ApiError(404) из install_bundle и валил ВЕСЬ update
(«Ошибка API: [404] Skill bitrix24 не найден» — остальные навыки не
обновлялись). Теперь такой навык пропускается поштучно (skipped/not_in_hub), а
остальные обновляются.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import InstallResult
from skillery_cli.core.transport import ApiError


def _install_skill(target: ClaudeCodeTarget, slug: str, version: str) -> None:
    d = target.slug_dir(slug)
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("hi", encoding="utf-8")
    (d / "_skill_meta.json").write_text(
        json.dumps({"slug": slug, "version": version}), encoding="utf-8"
    )


def test_cmd_update_skips_non_hub_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import skillery_cli.__main__ as main_mod
    from skillery_cli.config import ClientConfig

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    _install_skill(target, "hub-skill", "1.0.0")
    _install_skill(target, "local-skill", "0.0.0-local")

    cfg = ClientConfig(store_dir=str(tmp_path / "store"), base_url="http://x")
    cfg.user_email = "x@y.io"
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "tok")
    monkeypatch.setattr(main_mod, "_make_refresh_callback", lambda c: None)
    monkeypatch.setattr(main_mod, "track_skill_event", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "_apply_tooling", lambda *a, **k: None)

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def install_bundle(self, slug: str, *a, **k):
            if slug == "local-skill":
                # навык не из хаба → 404
                raise ApiError(404, "NOT_FOUND", "Skill local-skill не найден", {})
            return {
                "version": "2.0.0",
                "commit_sha": "deadbeef",
                "repo_url": "https://git.example/hub.git",
                "manifest": {"version": "2.0.0", "files": []},
            }

        async def close(self):
            return None

    monkeypatch.setattr(main_mod, "HubClient", _FakeClient)
    monkeypatch.setattr(
        main_mod.SkillInstaller,
        "install",
        lambda self, **kw: InstallResult(
            slug=kw.get("slug"),
            skill_id=kw.get("skill_id"),
            version=kw.get("version"),
            target_dir=target.slug_dir(kw.get("slug") or "hub-skill"),
            scope="global",
            is_update=True,
            linked=True,
            link_kind="link",
        ),
    )

    captured: dict = {}
    monkeypatch.setattr(
        main_mod, "emit_data", lambda data, **kw: captured.update({"rows": data})
    )

    # НЕ должно бросить (раньше валилось ApiError на local-skill).
    main_mod.cmd_update(slug=None, all_=False, channel="published", project=None, scope="global")

    rows = captured.get("rows") or []
    by_ref = {r["ref"]: r for r in rows}
    assert by_ref["local-skill"]["skipped"] is True
    assert by_ref["local-skill"]["skip_reason"] == "not_in_hub"
    assert by_ref["hub-skill"]["updated"] is True
    assert by_ref["hub-skill"]["to"] == "2.0.0"
