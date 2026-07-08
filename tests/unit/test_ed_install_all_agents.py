"""E-D: ``install --all-agents`` — оркестрация мульти-таргета.

ЦКП: один вызов ставит навык под claude_code+codex+antigravity. Тестируем
именно оркестрацию (fan-out по всем таргетам) — материализацию/линковку
покрывает test_cmd_install_local. ``_install_chain`` подменяем спаем, источник
= ``--path`` (без login/сети).
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
import typer

import skillery_cli.__main__ as main_mod
from skillery_cli import output as out_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core import project_manifest as pm


def _wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, json_mode: bool = True
) -> tuple[ClientConfig, Path, list[str]]:
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    cfg = ClientConfig(
        store_dir=str(store),
        default_install_scope="project",
        default_project_dir=str(project),
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    # get_target(name) → лёгкий стаб с .name (реальные таргеты не нужны).
    monkeypatch.setattr(
        main_mod, "get_target", lambda name: types.SimpleNamespace(name=name)
    )
    monkeypatch.setattr(main_mod, "_maybe_auto_update", lambda c: None)

    calls: list[str] = []

    async def _fake_chain(cfg_, access, *, slug, channel, scope, project_path,
                          force, agent_target, source=None):
        calls.append(agent_target.name)
        return [{
            "slug": slug, "skill_id": None, "version": "1.0.0",
            "is_update": False, "target_dir": f"/x/{agent_target.name}",
            "scope": scope, "linked": True, "link_kind": "symlink",
        }]

    monkeypatch.setattr(main_mod, "_install_chain", _fake_chain)
    if json_mode:
        monkeypatch.setattr(out_mod, "_mode", "json")
    return cfg, project, calls


def _make_skill_dir(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "SKILL.md").write_text(
        "---\nname: my-skill\nversion: 1.0.0\n---\n# x\n", encoding="utf-8"
    )
    return src


def test_all_agents_installs_under_every_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg, project, calls = _wire(tmp_path, monkeypatch)
    src = _make_skill_dir(tmp_path)

    main_mod.cmd_install(
        slug="my-skill", channel="published", agent=None, all_agents=True,
        scope="project", project=project, force=False, path=src,
        from_git=None, ref=None,
    )

    # Fan-out по всем трём агентам в каноническом порядке.
    assert calls == ["claude_code", "codex", "antigravity"]

    # JSON-ответ: 3 записи, каждая помечена своим агентом.
    out = capsys.readouterr().out.strip()
    payload = json.loads(out.splitlines()[-1])
    data = payload["data"] if isinstance(payload, dict) else payload
    agents = sorted(item["agent"] for item in data)
    assert agents == ["antigravity", "claude_code", "codex"]
    # Манифест проекта содержит навык (идемпотентно, не дублируется).
    assert pm.load(project) == {"my-skill": "*"}


def test_all_agents_and_agent_mutually_exclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _cfg, project, _calls = _wire(tmp_path, monkeypatch)
    src = _make_skill_dir(tmp_path)
    with pytest.raises(typer.Exit) as exc:
        main_mod.cmd_install(
            slug="my-skill", channel="published", agent="codex",
            all_agents=True, scope="project", project=project, force=False,
            path=src, from_git=None, ref=None,
        )
    assert exc.value.exit_code == 1


def test_single_agent_has_no_agent_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Без --all-agents (одиночный таргет) записи НЕ помечаются агентом."""
    cfg, project, calls = _wire(tmp_path, monkeypatch)
    src = _make_skill_dir(tmp_path)

    main_mod.cmd_install(
        slug="my-skill", channel="published", agent=None, all_agents=False,
        scope="project", project=project, force=False, path=src,
        from_git=None, ref=None,
    )
    assert len(calls) == 1
    out = capsys.readouterr().out.strip()
    payload = json.loads(out.splitlines()[-1])
    data = payload["data"] if isinstance(payload, dict) else payload
    assert "agent" not in data[0]
