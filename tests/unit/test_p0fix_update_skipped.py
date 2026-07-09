"""P0 follow-up: cmd_update обязан отражать skipped-результат installer'а.

Stub-guard (test_p0fix_stub_guard) заставляет installer.install вернуть
skipped=True вместо затирания живого контента. cmd_update раньше слепо
рапортовал updated=True + событие skill.update — мисрепорт: пользователь
видит «обновлено», хотя установка не тронута.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import InstallResult


def _setup_update_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    install_result: InstallResult,
):
    import skillery_cli.__main__ as main_mod
    from skillery_cli.config import ClientConfig

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

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def install_bundle(self, slug: str, *a, **k):
            return {
                "version": "9.9.9",
                "commit_sha": "deadbeef",
                "repo_url": None,
                "manifest": {"version": "9.9.9", "files": []},
            }

        async def close(self):
            return None

    monkeypatch.setattr(main_mod, "HubClient", _FakeClient)
    monkeypatch.setattr(
        main_mod.SkillInstaller, "install", lambda self, **kw: install_result
    )

    events: list = []
    monkeypatch.setattr(
        main_mod, "track_skill_event", lambda *a, **k: events.append((a, k))
    )
    emitted: list = []
    monkeypatch.setattr(
        main_mod, "emit_data", lambda data, **kw: emitted.append(data)
    )
    return main_mod, emitted, events


def test_update_reports_skipped_not_updated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """installer вернул skipped (stub-guard) → запись updated=False +
    skipped/skip_reason, и событие skill.update НЕ трекается."""
    skipped = InstallResult(
        slug="demo", version="1.0.0", target_dir=tmp_path / "x",
        is_update=False, scope="global",
        skipped=True, skip_reason="stub-would-clobber", content="stub",
    )
    main_mod, emitted, events = _setup_update_env(
        tmp_path, monkeypatch, install_result=skipped
    )

    main_mod.cmd_update(slug="demo", all_=False, channel="published",
                        project=None, scope="global")

    assert len(emitted) == 1
    rows = emitted[0]
    assert len(rows) == 1
    row = rows[0]
    assert row["updated"] is False
    assert row["skipped"] is True
    assert row["skip_reason"] == "stub-would-clobber"
    assert events == []  # skill.update не отправляется за пропуск


def test_update_reports_updated_for_real_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = InstallResult(
        slug="demo", version="9.9.9", target_dir=tmp_path / "x",
        is_update=True, scope="global", update_diff={"added": 1, "changed": 0, "removed": 0},
    )
    main_mod, emitted, events = _setup_update_env(
        tmp_path, monkeypatch, install_result=real
    )

    main_mod.cmd_update(slug="demo", all_=False, channel="published",
                        project=None, scope="global")

    row = emitted[0][0]
    assert row["updated"] is True
    assert row.get("skipped") is not True
    assert len(events) == 1
