"""Тесты CLI-команды ``skills-hub suggest`` (discovery поверх core/suggest).

Команда always-on (read-only): свободный запрос → нормализация в terms →
локальные кандидаты из стора (score_skill) + bounded hub-поиск (если залогинен,
size≤20 на term) → merge (DRY: переиспользует merge_suggestions онбординга) →
сортировка по score desc (local выше при равенстве). Ничего не ставит, только
отдаёт install_cmd.

replay без сети: hub-ветка мокается через ``_common.HubClient`` (паттерн
test_p1_onboard), локальный стор — реальный (tmp store), линковка — реальная.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import typer

from skills_hub_cli import output as out_mod
from skills_hub_cli.commands import _common
from skills_hub_cli.commands import suggest as suggest_mod
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.core import project_manifest as pm
from skills_hub_cli.core.agents import ClaudeCodeTarget
from skills_hub_cli.core.installer import SkillInstaller, write_meta


# ======================================================
#  фикстуры — стор/проект/конфиг (зеркало test_p1_onboard)
# ======================================================
class _ExplodingClient:
    def __init__(self, *a: Any, **k: Any) -> None:
        raise AssertionError("HubClient НЕ должен создаваться без логина")


def _store_skill(
    store: Path,
    name: str,
    *,
    tags: tuple[str, ...] = (),
    description: str = "",
    title: str | None = None,
    version: str = "1.0.0",
) -> None:
    """Минимальный навык в сторе: _skill_meta.json + SKILL.md."""
    d = store / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    write_meta(
        d,
        {
            "slug": name,
            "skill_id": None,
            "title": title or name,
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
        monkeypatch.setattr(suggest_mod, "load_tokens", lambda email: ("tok", "r"))
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(suggest_mod, "get_target", lambda name: target)
    monkeypatch.setattr(out_mod, "_mode", "json")
    return cfg, target, store, project


def _last_payload(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


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


# ======================================================
#  не залогинен → только локальный стор, сеть не дёргается
# ======================================================
def test_suggest_local_only_no_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cfg, _target, store, project = _wire(tmp_path, monkeypatch, logged_in=False)
    _store_skill(store, "stripe-pay", tags=("payments", "stripe"), title="Stripe")
    _store_skill(store, "unrelated", tags=("marketing",))
    monkeypatch.setattr(_common, "HubClient", _ExplodingClient)

    suggest_mod.cmd_suggest(query="нужен stripe для платежей", project=project,
                            limit=10)

    payload = _last_payload(capsys)
    assert payload["logged_in"] is False
    assert payload["query"] == "нужен stripe для платежей"
    assert "stripe" in payload["terms"]
    slugs = [s["slug"] for s in payload["suggestions"]]
    assert slugs == ["stripe-pay"]
    s = payload["suggestions"][0]
    assert s["source"] == "local"
    assert s["status"] == "in_store"
    assert s["already_enabled"] is False
    assert s["score"] > 0
    assert s["matched_on"]  # объяснимость присутствует
    assert s["install_cmd"] == f"skillery enable stripe-pay --project {project.resolve()}"
    # notes сообщает что сеть выключена (не залогинен)
    assert any("login" in n.lower() or "залог" in n.lower() for n in payload["notes"])


def test_suggest_no_query_terms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Пустой/стоп-словный запрос → terms пуст, suggestions пуст, note."""
    _wire(tmp_path, monkeypatch, logged_in=False)
    suggest_mod.cmd_suggest(query="как мне это для", project=None, limit=10)
    payload = _last_payload(capsys)
    assert payload["terms"] == []
    assert payload["suggestions"] == []
    assert payload["notes"]


# ======================================================
#  залогинен → bounded hub-поиск size≤20, merge с локальным
# ======================================================
def test_suggest_hub_search_logged_in_size_capped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cfg, _target, _store, project = _wire(tmp_path, monkeypatch, logged_in=True)
    calls = _fake_hub(
        monkeypatch,
        {"stripe": [{"slug": "stripe-hub", "title": "Stripe Hub",
                     "tags": ["payments"], "description": "stripe sdk"}]},
    )

    # --limit 50 не пробивает size>20 (канон bounded search)
    suggest_mod.cmd_suggest(query="stripe", project=project, limit=50)

    assert calls == [("stripe", 20)]
    payload = _last_payload(capsys)
    assert payload["logged_in"] is True
    by_slug = {s["slug"]: s for s in payload["suggestions"]}
    assert "stripe-hub" in by_slug
    assert by_slug["stripe-hub"]["source"] == "hub"
    # hub-кандидата нет в сторе → status not_installed
    assert by_slug["stripe-hub"]["status"] == "not_installed"


def test_suggest_both_source_and_local_ranks_higher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Навык в сторе+хабе → source=both; при равном score local выше hub-only."""
    _cfg, _target, store, project = _wire(tmp_path, monkeypatch, logged_in=True)
    _store_skill(store, "stripe-pay", tags=("stripe",), title="Stripe")
    _fake_hub(
        monkeypatch,
        {"stripe": [
            {"slug": "stripe-pay", "title": "Stripe", "tags": ["stripe"]},
            {"slug": "stripe-hub", "title": "Other", "tags": ["stripe"]},
        ]},
    )

    suggest_mod.cmd_suggest(query="stripe", project=project, limit=10)

    payload = _last_payload(capsys)
    by_slug = {s["slug"]: s for s in payload["suggestions"]}
    assert by_slug["stripe-pay"]["source"] == "both"
    # both/local идёт первым в выдаче (равный score → local-приоритет)
    assert payload["suggestions"][0]["slug"] == "stripe-pay"


def test_suggest_hub_token_error_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ошибка hub-поиска не валит команду — отдаём локальные + note."""
    _cfg, _target, store, project = _wire(tmp_path, monkeypatch, logged_in=True)
    _store_skill(store, "stripe-pay", tags=("stripe",), title="Stripe")

    fake = MagicMock()

    async def _boom(*, q: str, size: int = 20, **kw: Any) -> dict[str, Any]:
        raise RuntimeError("token expired")

    async def _close() -> None:
        return None

    fake.search_skills = _boom
    fake.close = _close
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake)

    suggest_mod.cmd_suggest(query="stripe", project=project, limit=10)

    payload = _last_payload(capsys)
    # Команда не упала: локальный кандидат на месте.
    assert [s["slug"] for s in payload["suggestions"]] == ["stripe-pay"]
    assert any("хаб" in n.lower() or "hub" in n.lower() for n in payload["notes"])


# ======================================================
#  статусы: already_enabled / installed_global / installed_project
# ======================================================
def test_suggest_already_enabled_in_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cfg, target, store, project = _wire(tmp_path, monkeypatch, logged_in=False)
    _store_skill(store, "stripe-pay", tags=("stripe",), title="Stripe")
    pm.add(project, "stripe-pay")
    # реальная линковка в project scope → installed_project
    SkillInstaller(target, store).link_existing("stripe-pay", project=project)
    monkeypatch.setattr(_common, "HubClient", _ExplodingClient)

    suggest_mod.cmd_suggest(query="stripe", project=project, limit=10)

    payload = _last_payload(capsys)
    s = payload["suggestions"][0]
    assert s["already_enabled"] is True
    assert s["status"] == "installed_project"


def test_suggest_installed_global_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cfg, target, store, project = _wire(tmp_path, monkeypatch, logged_in=False)
    _store_skill(store, "stripe-pay", tags=("stripe",), title="Stripe")
    # глобальная установка (link в global scope)
    SkillInstaller(target, store).link_existing("stripe-pay", project=None)
    monkeypatch.setattr(_common, "HubClient", _ExplodingClient)

    suggest_mod.cmd_suggest(query="stripe", project=project, limit=10)

    payload = _last_payload(capsys)
    s = payload["suggestions"][0]
    assert s["status"] == "installed_global"
    assert s["already_enabled"] is False  # в проект не включён


# ======================================================
#  read-only: команда ничего не ставит
# ======================================================
def test_suggest_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cfg, target, store, project = _wire(tmp_path, monkeypatch, logged_in=False)
    _store_skill(store, "stripe-pay", tags=("stripe",), title="Stripe")
    monkeypatch.setattr(_common, "HubClient", _ExplodingClient)

    suggest_mod.cmd_suggest(query="stripe", project=project, limit=10)
    _last_payload(capsys)

    # Ничего не слинковано, манифест не тронут.
    assert not target.slug_dir("stripe-pay", project=project).exists()
    assert pm.load(project) == {}


def test_suggest_json_contract_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _cfg, _target, store, project = _wire(tmp_path, monkeypatch, logged_in=False)
    _store_skill(store, "stripe-pay", tags=("stripe",), title="Stripe", version="2.0.0")
    monkeypatch.setattr(_common, "HubClient", _ExplodingClient)

    suggest_mod.cmd_suggest(query="stripe", project=project, limit=10)
    payload = _last_payload(capsys)

    assert set(payload) >= {"query", "logged_in", "project", "terms",
                            "suggestions", "notes"}
    s = payload["suggestions"][0]
    assert set(s) >= {"slug", "title", "source", "score", "matched_on",
                      "status", "already_enabled", "version", "install_cmd"}
    assert s["version"] == "2.0.0"


# ======================================================
#  регистрация always-on
# ======================================================
def test_suggest_registered_always_on(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skills_hub_cli.__main__ import build_app

    app = build_app()
    names = [c.name for c in app.registered_commands]
    assert "suggest" in names
