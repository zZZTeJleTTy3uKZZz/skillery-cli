"""E7 доп. метрики — `skillery analytics local` (read-only локальная картина).

Показывает БЕЗ сети и логина:
- что материализовано в сторе (slug, версия, КАКОЙ агент ставил, source);
- что включено в проект (project scope, через таргет-агента);
- состояние очереди событий (сколько в очереди, разбивка по типам/агентам).

Команда строго read-only: ничего не пишет, не удаляет, сеть не трогает.
JSON-режим даёт машинно-читаемую структуру для AI-агентов/скриптов.
"""
from __future__ import annotations

import json as _json
from pathlib import Path

import pytest

from skillery_cli import output as out_mod
from skillery_cli.commands import analytics as an_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core.agents import CodexTarget
from skillery_cli.core.installer import SkillInstaller
from skillery_cli.daemon.event_collector import EventCollector


def _make_skill_dir(tmp_path: Path, name: str, version: str) -> Path:
    src = tmp_path / f"src-{name}"
    src.mkdir(parents=True, exist_ok=True)
    (src / "SKILL.md").write_text(
        f"---\nname: {name}\nversion: {version}\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return src


def _wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ClientConfig, CodexTarget, Path]:
    target = CodexTarget(root=tmp_path / ".codex")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    cfg = ClientConfig(
        store_dir=str(store),
        default_install_scope="project",
        default_project_dir=str(project),
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(ClientConfig, "save", lambda self: None)
    monkeypatch.setattr(an_mod, "get_target", lambda name: target)
    queue = tmp_path / "events.queue.json"
    monkeypatch.setattr(an_mod, "default_queue_path", lambda: queue)
    return cfg, target, project


def _capsys_json(capsys: pytest.CaptureFixture[str]) -> dict:
    out = capsys.readouterr().out
    return _json.loads(out)


def test_analytics_local_reports_store_skills_with_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(out_mod, "_mode", "json")
    cfg, target, project = _wire(tmp_path, monkeypatch)
    installer = SkillInstaller(target, cfg.effective_store_dir())
    src = _make_skill_dir(tmp_path, "demo", "1.2.3")
    installer.install(slug="demo", version="1.2.3", commit_sha="",
                      repo_url=None, local_src=src,
                      manifest={"version": "1.2.3", "files": []},
                      project=None, force=False)

    an_mod.cmd_analytics_local(project=None)
    data = _capsys_json(capsys)

    assert "store" in data
    names = {s["name"]: s for s in data["store"]}
    assert "demo" in names
    assert names["demo"]["version"] == "1.2.3"
    # КАКОЙ агент материализовал навык — из meta стора.
    assert names["demo"]["agent"] == "codex"
    assert names["demo"]["source"] == "local-path"


def test_analytics_local_reports_project_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(out_mod, "_mode", "json")
    cfg, target, project = _wire(tmp_path, monkeypatch)
    installer = SkillInstaller(target, cfg.effective_store_dir())
    src = _make_skill_dir(tmp_path, "demo", "1.0.0")
    # install в project scope → попадает и в стор, и в project-папку
    installer.install(slug="demo", version="1.0.0", commit_sha="",
                      repo_url=None, local_src=src,
                      manifest={"version": "1.0.0", "files": []},
                      project=project, force=False)

    an_mod.cmd_analytics_local(project=project)
    data = _capsys_json(capsys)

    assert "project" in data
    proj = data["project"]
    assert proj["root"] == str(project)
    proj_names = {s["ref"] for s in proj["skills"]}
    assert "demo" in proj_names


def test_analytics_local_reports_queue_breakdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(out_mod, "_mode", "json")
    cfg, target, project = _wire(tmp_path, monkeypatch)
    queue = EventCollector(an_mod.default_queue_path())
    queue.append("skill.install", resource_type="skill", resource_id="1",
                 payload={"agent": "codex"})
    queue.append("skill.enable", resource_type="skill", resource_id="1",
                 payload={"agent": "codex"})
    queue.append("skill.enable", resource_type="skill", resource_id="2",
                 payload={"agent": "claude_code"})

    an_mod.cmd_analytics_local(project=None)
    data = _capsys_json(capsys)

    assert "queue" in data
    q = data["queue"]
    assert q["size"] == 3
    # разбивка по типу события
    assert q["by_type"]["skill.enable"] == 2
    assert q["by_type"]["skill.install"] == 1
    # разбивка по агенту
    assert q["by_agent"]["codex"] == 2
    assert q["by_agent"]["claude_code"] == 1


def test_analytics_local_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Команда ничего не пишет: размер очереди и стор не меняются."""
    monkeypatch.setattr(out_mod, "_mode", "json")
    cfg, target, project = _wire(tmp_path, monkeypatch)
    queue = EventCollector(an_mod.default_queue_path())
    queue.append("skill.install", resource_type="skill", resource_id="1")
    before = queue.size()

    an_mod.cmd_analytics_local(project=None)
    capsys.readouterr()

    # очередь не тронута (read-only)
    assert EventCollector(an_mod.default_queue_path()).size() == before


def test_analytics_local_empty_state_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Пустой стор + пустая очередь → валидная структура, без падений."""
    monkeypatch.setattr(out_mod, "_mode", "json")
    cfg, target, project = _wire(tmp_path, monkeypatch)
    an_mod.cmd_analytics_local(project=None)
    data = _capsys_json(capsys)
    assert data["store"] == []
    assert data["queue"]["size"] == 0
    assert data["agent"] == "codex"


def test_analytics_command_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`analytics local` зарегистрирована в app (always-on)."""
    import typer

    app = typer.Typer()
    an_mod.register(app)
    # есть под-приложение analytics с командой local
    names = [
        getattr(g, "name", None) for g in getattr(app, "registered_groups", [])
    ]
    assert "analytics" in names
