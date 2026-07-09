"""Тесты P1 эпика C5 — ``skillery onboard`` (онбординг проекта, E11).

Чистые функции (``core/onboarding.py``): ``detect_signals`` /
``match_store`` / ``merge_suggestions`` — на Path-фикстурах, без CLI и сети.

CLI-команда — replay без сети: hub-ветка мокается через ``_common.HubClient``
(паттерн ``test_p0_collection_install``), установка — через
``onboard._install_chain``, линковка из стора — реальная (tmp store).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import typer

from skillery_cli import output as out_mod
from skillery_cli.commands import _common
from skillery_cli.commands import onboard as onboard_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core import linker, project_manifest as pm
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import write_meta
from skillery_cli.core.onboarding import (
    detect_signals,
    match_store,
    merge_suggestions,
)


# ======================================================
#  detect_signals — чистая эвристика по файлам проекта
# ======================================================
def test_detect_signals_python_via_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    assert detect_signals(tmp_path) == ["python"]


def test_detect_signals_python_via_requirements(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("httpx\n", encoding="utf-8")
    assert detect_signals(tmp_path) == ["python"]


def test_detect_signals_nodejs_plus_nextjs_react(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"dependencies": {"next": "^14.0.0", "react": "^18.0.0"}}),
        encoding="utf-8",
    )
    signals = detect_signals(tmp_path)
    assert signals == ["nodejs", "nextjs", "react"]


def test_detect_signals_react_from_dev_dependencies(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"react": "^18.0.0"}}), encoding="utf-8"
    )
    signals = detect_signals(tmp_path)
    assert "nodejs" in signals
    assert "react" in signals
    assert "nextjs" not in signals


def test_detect_signals_nodejs_broken_package_json(tmp_path: Path) -> None:
    """Битый package.json — сигнал nodejs остаётся, deps-сигналы нет."""
    (tmp_path / "package.json").write_text("{ это не json", encoding="utf-8")
    assert detect_signals(tmp_path) == ["nodejs"]


def test_detect_signals_docker_via_dockerfile(tmp_path: Path) -> None:
    (tmp_path / "Dockerfile").write_text("FROM python:3.11\n", encoding="utf-8")
    assert detect_signals(tmp_path) == ["docker"]


def test_detect_signals_docker_via_compose(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.prod.yml").write_text("services: {}\n", encoding="utf-8")
    assert detect_signals(tmp_path) == ["docker"]


def test_detect_signals_terraform_go_rust_claude(tmp_path: Path) -> None:
    (tmp_path / "main.tf").write_text("", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
    (tmp_path / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    assert detect_signals(tmp_path) == ["terraform", "go", "rust", "claude-code"]


def test_detect_signals_empty_project(tmp_path: Path) -> None:
    assert detect_signals(tmp_path) == []


def test_detect_signals_no_duplicates(tmp_path: Path) -> None:
    """pyproject + requirements — python один раз."""
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("httpx\n", encoding="utf-8")
    assert detect_signals(tmp_path) == ["python"]


# ======================================================
#  match_store — кандидаты из локального стора
# ======================================================
def _store_skill(
    store: Path,
    name: str,
    *,
    tags: tuple[str, ...] = (),
    description: str = "",
    version: str = "1.0.0",
) -> None:
    """Раскладывает в стор минимальный навык: _skill_meta.json + SKILL.md."""
    d = store / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    write_meta(
        d,
        {
            "slug": name,
            "skill_id": None,
            "version": version,
            "commit_sha": "",
            "manifest": {
                "version": version,
                "files": [],
                "tags": list(tags),
                "description": description,
            },
            "agent": "claude-code",
            "scope": "global",
            "project": None,
            "source": "hub",
            "repo_url": None,
        },
    )


def test_match_store_by_tag(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _store_skill(store, "py-helper", tags=("python", "backend"))
    out = match_store(store, ["python"])
    assert len(out) == 1
    assert out[0]["slug"] == "py-helper"
    assert out[0]["signals"] == ["python"]


def test_match_store_by_slug_substring(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _store_skill(store, "docker-tools")
    out = match_store(store, ["docker"])
    assert [m["slug"] for m in out] == ["docker-tools"]
    assert out[0]["signals"] == ["docker"]


def test_match_store_by_description_case_insensitive(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _store_skill(store, "frontend-kit", description="Best practices for NextJS apps")
    out = match_store(store, ["nextjs"])
    assert [m["slug"] for m in out] == ["frontend-kit"]
    assert out[0]["signals"] == ["nextjs"]


def test_match_store_excludes_unrelated(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _store_skill(store, "py-helper", tags=("python",))
    _store_skill(store, "unrelated", tags=("marketing",), description="продажи")
    out = match_store(store, ["python", "docker"])
    assert [m["slug"] for m in out] == ["py-helper"]


def test_match_store_empty_signals(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _store_skill(store, "py-helper", tags=("python",))
    assert match_store(store, []) == []


def test_match_store_missing_dir(tmp_path: Path) -> None:
    assert match_store(tmp_path / "no-store", ["python"]) == []


# ======================================================
#  merge_suggestions — объединение local + hub + already
# ======================================================
def test_merge_suggestions_sources_and_already() -> None:
    local = [{"slug": "a", "signals": ["python"]}]
    hub = [
        {"slug": "a", "signals": ["python", "nodejs"], "title": "A"},
        {"slug": "b", "signals": ["docker"], "title": "B"},
    ]
    out = merge_suggestions(local, hub, {"b"})
    by_slug = {s["slug"]: s for s in out}
    assert set(by_slug) == {"a", "b"}
    assert by_slug["a"]["source"] == "both"
    assert by_slug["a"]["signals"] == ["python", "nodejs"]
    assert by_slug["a"]["already"] is False
    assert by_slug["b"]["source"] == "hub"
    assert by_slug["b"]["already"] is True


def test_merge_suggestions_local_only() -> None:
    out = merge_suggestions([{"slug": "x", "signals": ["go"]}], [], set())
    assert out == [{"slug": "x", "source": "local", "signals": ["go"], "already": False}]


# ======================================================
#  CLI ``onboard`` — replay без сети
# ======================================================
class _ExplodingClient:
    def __init__(self, *a: Any, **k: Any) -> None:
        raise AssertionError("HubClient НЕ должен создаваться без логина")


def _wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, logged_in: bool
) -> tuple[ClientConfig, ClaudeCodeTarget, Path, Path]:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    cfg = ClientConfig(store_dir=str(store))
    if logged_in:
        cfg.user_email = "x@y.io"
        cfg.permissions = ["skill.read", "skill.install"]
        monkeypatch.setattr(onboard_mod, "load_tokens", lambda email: ("tok", "r"))
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(onboard_mod, "get_target", lambda name: target)
    monkeypatch.setattr(
        onboard_mod, "track_skill_event", lambda *a, **k: None, raising=False
    )
    monkeypatch.setattr(out_mod, "_mode", "json")
    return cfg, target, store, project


def _last_payload(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_cmd_onboard_show_only_local_store_no_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Не залогинен → работаем по локальному стору (это норм, не ошибка); сеть не дёргается."""
    _cfg, target, store, project = _wire(tmp_path, monkeypatch, logged_in=False)
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _store_skill(store, "py-skill", tags=("python",))
    monkeypatch.setattr(_common, "HubClient", _ExplodingClient)

    onboard_mod.cmd_onboard(project=project, yes=False, limit=10, agent=None)

    payload = _last_payload(capsys)
    assert payload["signals"] == ["python"]
    assert payload["suggestions"] == [
        {"slug": "py-skill", "source": "local", "signals": ["python"], "already": False}
    ]
    # Только показ: ничего не слинковано, манифест не тронут.
    assert not target.slug_dir("py-skill", project=project).exists()
    assert pm.load(project) == {}


def test_cmd_onboard_marks_already(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Уже включённый в проект навык получает already=True."""
    _cfg, _target, store, project = _wire(tmp_path, monkeypatch, logged_in=False)
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _store_skill(store, "py-skill", tags=("python",))
    pm.add(project, "py-skill")

    onboard_mod.cmd_onboard(project=project, yes=False, limit=10, agent=None)

    payload = _last_payload(capsys)
    assert payload["suggestions"][0]["already"] is True


def _fake_hub(
    monkeypatch: pytest.MonkeyPatch, responses: dict[str, list[dict[str, Any]]]
) -> list[tuple[str, int]]:
    """Мок ``_common.HubClient``: search_skills отвечает из ``responses[q]``."""
    calls: list[tuple[str, int]] = []
    fake = MagicMock()

    async def _search(*, q: str, size: int = 20, **kw: Any) -> dict[str, Any]:
        calls.append((q, size))
        items = responses.get(q, [])
        return {"items": items, "total": len(items), "page": 1, "size": size}

    async def _close() -> None:
        return None

    fake.search_skills = _search
    fake.close = _close
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake)
    return calls


def test_cmd_onboard_hub_search_logged_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Залогинен → hub-поиск по каждому сигналу, дедуп по slug, source=hub."""
    _cfg, _target, _store, project = _wire(tmp_path, monkeypatch, logged_in=True)
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (project / "Dockerfile").write_text("FROM x\n", encoding="utf-8")
    calls = _fake_hub(
        monkeypatch,
        {
            "python": [{"slug": "py-hub", "title": "Py"}],
            "docker": [{"slug": "docker-hub", "title": "D"}, {"slug": "py-hub", "title": "Py"}],
        },
    )

    onboard_mod.cmd_onboard(project=project, yes=False, limit=10, agent=None)

    assert calls == [("python", 10), ("docker", 10)]
    payload = _last_payload(capsys)
    by_slug = {s["slug"]: s for s in payload["suggestions"]}
    assert set(by_slug) == {"py-hub", "docker-hub"}
    assert by_slug["py-hub"]["source"] == "hub"
    assert by_slug["py-hub"]["signals"] == ["python", "docker"]
    assert by_slug["docker-hub"]["signals"] == ["docker"]


def test_cmd_onboard_hub_size_capped_at_20(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Канон bounded search: --limit 50 не пробивает size>20."""
    _cfg, _target, _store, project = _wire(tmp_path, monkeypatch, logged_in=True)
    (project / "go.mod").write_text("module x\n", encoding="utf-8")
    calls = _fake_hub(monkeypatch, {"go": []})

    onboard_mod.cmd_onboard(project=project, yes=False, limit=50, agent=None)

    assert calls == [("go", 20)]
    _last_payload(capsys)  # flush


def test_cmd_onboard_hub_both_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Навык и в сторе, и в hub-выдаче → source=both."""
    _cfg, _target, store, project = _wire(tmp_path, monkeypatch, logged_in=True)
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _store_skill(store, "py-skill", tags=("python",))
    _fake_hub(monkeypatch, {"python": [{"slug": "py-skill", "title": "Py"}]})

    onboard_mod.cmd_onboard(project=project, yes=False, limit=10, agent=None)

    payload = _last_payload(capsys)
    assert payload["suggestions"][0]["slug"] == "py-skill"
    assert payload["suggestions"][0]["source"] == "both"


def test_cmd_onboard_yes_links_from_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--yes: навык из стора линкуется в project scope + пишется в манифест."""
    _cfg, target, store, project = _wire(tmp_path, monkeypatch, logged_in=False)
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _store_skill(store, "py-helper", tags=("python",), version="2.0.0")
    monkeypatch.setattr(_common, "HubClient", _ExplodingClient)

    onboard_mod.cmd_onboard(project=project, yes=True, limit=10, agent=None)

    assert linker.is_link(target.slug_dir("py-helper", project=project))
    assert pm.load(project) == {"py-helper": "*"}
    payload = _last_payload(capsys)
    assert payload["applied"]["linked"] == ["py-helper"]
    assert payload["applied"]["installed"] == []
    assert payload["applied"]["skipped"] == []


def test_cmd_onboard_yes_installs_missing_from_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--yes: hub-кандидата нет в сторе → докачка через _install_chain."""
    cfg, target, _store, project = _wire(tmp_path, monkeypatch, logged_in=True)
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _fake_hub(monkeypatch, {"python": [{"slug": "hub-skill", "title": "H"}]})

    chain_calls: list[dict[str, Any]] = []

    async def _fake_chain(cfg_, access, *, slug, channel, scope, project_path, force, agent_target):  # noqa: ANN001
        chain_calls.append({"slug": slug, "scope": scope, "project_path": project_path})
        return [{"slug": slug, "skill_id": None, "version": "1.0.0", "is_update": False,
                 "target_dir": "/x", "scope": scope, "linked": True, "link_kind": "junction"}]

    monkeypatch.setattr(onboard_mod, "_install_chain", _fake_chain)

    onboard_mod.cmd_onboard(project=project, yes=True, limit=10, agent=None)

    assert [c["slug"] for c in chain_calls] == ["hub-skill"]
    assert chain_calls[0]["scope"] == "project"
    assert chain_calls[0]["project_path"] == project.resolve()
    assert pm.load(project) == {"hub-skill": "*"}
    payload = _last_payload(capsys)
    assert payload["applied"]["installed"] == ["hub-skill"]


def test_cmd_onboard_yes_already_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--yes: already-навык не перелинковывается и не докачивается."""
    _cfg, target, store, project = _wire(tmp_path, monkeypatch, logged_in=False)
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _store_skill(store, "py-skill", tags=("python",))
    pm.add(project, "py-skill")
    monkeypatch.setattr(_common, "HubClient", _ExplodingClient)

    onboard_mod.cmd_onboard(project=project, yes=True, limit=10, agent=None)

    payload = _last_payload(capsys)
    assert payload["applied"]["already"] == ["py-skill"]
    assert payload["applied"]["linked"] == []
    # Ссылка не создана (already пропущен целиком).
    assert not target.slug_dir("py-skill", project=project).exists()


def test_cmd_onboard_yes_install_failure_goes_to_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--yes: упавшая докачка не валит команду — кандидат уходит в skipped."""
    _cfg, _target, _store, project = _wire(tmp_path, monkeypatch, logged_in=True)
    (project / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    _fake_hub(monkeypatch, {"python": [{"slug": "broken-skill", "title": "B"}]})

    async def _boom(*a: Any, **k: Any) -> list[dict[str, Any]]:
        raise RuntimeError("git clone failed")

    monkeypatch.setattr(onboard_mod, "_install_chain", _boom)

    onboard_mod.cmd_onboard(project=project, yes=True, limit=10, agent=None)

    payload = _last_payload(capsys)
    assert payload["applied"]["installed"] == []
    assert payload["applied"]["skipped"][0]["slug"] == "broken-skill"
    assert "git clone failed" in payload["applied"]["skipped"][0]["reason"]
    assert pm.load(project) == {}


def test_cmd_onboard_invalid_project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _wire(tmp_path, monkeypatch, logged_in=False)
    with pytest.raises(typer.Exit) as exc:
        onboard_mod.cmd_onboard(
            project=tmp_path / "no-such-dir", yes=False, limit=10, agent=None
        )
    assert exc.value.exit_code == 1
    assert "VALIDATION" in capsys.readouterr().err


def test_onboard_registered_always_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Команда onboard видна и БЕЗ логина (always-on блок build_app)."""
    cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [c.name for c in app.registered_commands]
    assert "onboard" in names
