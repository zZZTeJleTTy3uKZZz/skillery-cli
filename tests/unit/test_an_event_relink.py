"""Аналитика-эпик W1a: re-link точки (collection install / onboard) шлют
skill.enable, а не skill.install.

Collection install re-link и onboard re-link линкуют УЖЕ материализованный в
сторе навык в проект (без сети). Это включение-в-проект → ``skill.enable``
(source из meta стора), НЕ повторная материализация ``skill.install``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from skills_hub_cli import output as out_mod
from skills_hub_cli.commands import _common
from skills_hub_cli.commands import onboard as onboard_mod
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import write_meta


def _events_of(events: list, etype: str) -> list:
    return [(a, k) for (a, k) in events if a and a[0] == etype]


def _store_skill(store: Path, name: str, *, source: str = "hub",
                 version: str = "1.0.0", tags: tuple[str, ...] = ()) -> None:
    d = store / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    write_meta(d, {
        "slug": name, "skill_id": None, "version": version, "commit_sha": "",
        "manifest": {"version": version, "files": [], "tags": list(tags),
                     "description": ""},
        "agent": "claude-code", "scope": "global", "project": None,
        "source": source, "repo_url": None,
    })


# ========== onboard re-link → skill.enable ==========
def test_onboard_relink_emits_enable_not_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _store_skill(store, "py-helper", source="local-path", version="2.0.0",
                 tags=("python",))
    cfg = ClientConfig(store_dir=str(store))
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(onboard_mod, "get_target", lambda name: target)
    monkeypatch.setattr(out_mod, "_mode", "json")
    events: list = []
    monkeypatch.setattr(
        onboard_mod, "track_skill_event", lambda *a, **k: events.append((a, k))
    )

    class _ExplodingClient:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise AssertionError("сеть не нужна для re-link")

    monkeypatch.setattr(_common, "HubClient", _ExplodingClient)

    onboard_mod.cmd_onboard(project=project, yes=True, limit=10, agent=None)

    enables = _events_of(events, "skill.enable")
    assert len(enables) == 1
    assert enables[0][1]["slug"] == "py-helper"
    assert enables[0][1]["scope"] == "project"
    assert enables[0][1]["source"] == "local-path"
    assert _events_of(events, "skill.install") == []


# ========== collection install re-link → skill.enable ==========
def test_collection_install_relink_emits_enable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from skills_hub_cli.commands import collection as coll_mod
    from skills_hub_cli.core import agents as agents_mod
    from skills_hub_cli.core import local_collections

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir()
    _store_skill(store, "demo", source="git-url", version="3.0.0")
    cfg = ClientConfig(
        store_dir=str(store), default_install_scope="project",
        default_project_dir=str(project),
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    # _install_local импортирует get_target из core.agents в рантайме.
    monkeypatch.setattr(agents_mod, "get_target", lambda name: target)
    monkeypatch.setattr(out_mod, "_mode", "json")
    # коллекция с одним навыком, уже лежащим в сторе
    monkeypatch.setattr(
        local_collections, "get",
        lambda name: {"name": name, "title": name, "skills": ["demo"]},
    )
    events: list = []
    monkeypatch.setattr(
        coll_mod, "track_skill_event", lambda *a, **k: events.append((a, k))
    )

    coll_mod.cmd_collection_install(
        ref="mycoll", local=True, scope="project", project=project,
        channel="published", force=False, agent=None,
    )

    enables = _events_of(events, "skill.enable")
    assert len(enables) == 1
    assert enables[0][1]["slug"] == "demo"
    assert enables[0][1]["source"] == "git-url"
    assert _events_of(events, "skill.install") == []
