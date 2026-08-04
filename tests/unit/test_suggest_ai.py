"""#242 — ``skillery suggest --ai``: семантический подбор по хабу.

``--ai`` переключает hub-ветку на POST /skill-searches (один вызов,
не per-term), несёт ``reason`` от бэка в выдачу; локальный стор остаётся на
лексике (оффлайн-фолбэк). Без логина / при ошибке эндпоинта — деградирует на
локальные кандидаты, команда не валится.

replay без сети: ``_common.make_client`` мокается фейком (паттерн
test_discovery_suggest_cmd).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from skillery_cli import output as out_mod
from skillery_cli.commands import _common
from skillery_cli.commands import suggest as suggest_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core.agents import ClaudeCodeTarget
from skillery_cli.core.installer import write_meta


def _store_skill(store: Path, name: str, *, tags: tuple[str, ...] = ()) -> None:
    d = store / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    write_meta(
        d,
        {
            "slug": name, "skill_id": None, "title": name, "version": "1.0.0",
            "commit_sha": "", "manifest": {"version": "1.0.0", "files": [],
            "tags": list(tags), "description": ""}, "agent": "claude-code",
            "scope": "global", "project": None, "source": "hub", "repo_url": None,
        },
    )


def _wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, logged_in: bool
) -> tuple[ClientConfig, Path, Path]:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    cfg = ClientConfig(store_dir=str(store))
    if logged_in:
        cfg.user_email = "x@y.io"
        cfg.permissions = ["skill.read"]
        monkeypatch.setattr(suggest_mod, "load_tokens", lambda email: ("tok", "r"))
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(suggest_mod, "get_target", lambda name: target)
    monkeypatch.setattr(out_mod, "_mode", "json")
    return cfg, store, project


def _last_payload(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _fake_semantic(
    monkeypatch: pytest.MonkeyPatch,
    matches: list[dict[str, Any]],
    *,
    boom: bool = False,
) -> list[dict[str, Any]]:
    """Мок make_client: search_skills_semantic отдаёт matches (или падает)."""
    calls: list[dict[str, Any]] = []
    fake = MagicMock()

    async def _sem(*, query: str, top_k: int = 10) -> dict[str, Any]:
        calls.append({"query": query, "top_k": top_k})
        if boom:
            raise RuntimeError("token expired")
        return {"query": query, "matches": matches}

    async def _close() -> None:
        return None

    fake.search_skills_semantic = _sem
    fake.close = _close
    monkeypatch.setattr(_common, "make_client", lambda cfg, access: fake)
    return calls


def test_ai_uses_semantic_endpoint_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cfg, _store, project = _wire(tmp_path, monkeypatch, logged_in=True)
    calls = _fake_semantic(
        monkeypatch,
        [
            {"skill_id": 1, "slug": "stripe-pay", "score": 0.9,
             "reason": "Совпадение по теги: payments", "title": "Stripe"},
        ],
    )

    suggest_mod.cmd_suggest(
        query="нужен сервис для приёма платежей", project=project, limit=10,
        ai=True,
    )

    # один семантический вызов по СЫРОМУ query (не per-term).
    assert len(calls) == 1
    assert calls[0]["query"] == "нужен сервис для приёма платежей"
    payload = _last_payload(capsys)
    by_slug = {s["slug"]: s for s in payload["suggestions"]}
    assert "stripe-pay" in by_slug
    assert by_slug["stripe-pay"]["source"] == "hub"
    assert by_slug["stripe-pay"]["reason"] == "Совпадение по теги: payments"


def test_ai_without_login_degrades_to_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cfg, store, project = _wire(tmp_path, monkeypatch, logged_in=False)
    _store_skill(store, "stripe-pay", tags=("stripe",))

    # make_client не должен вызываться без логина.
    def _boom_client(*a: Any, **k: Any) -> Any:
        raise AssertionError("make_client не должен звонить без логина")

    monkeypatch.setattr(_common, "make_client", _boom_client)

    suggest_mod.cmd_suggest(query="stripe", project=project, limit=10, ai=True)

    payload = _last_payload(capsys)
    assert [s["slug"] for s in payload["suggestions"]] == ["stripe-pay"]
    assert any("--ai" in n or "логин" in n.lower() for n in payload["notes"])


def test_ai_endpoint_error_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _cfg, store, project = _wire(tmp_path, monkeypatch, logged_in=True)
    _store_skill(store, "stripe-pay", tags=("stripe",))
    _fake_semantic(monkeypatch, [], boom=True)

    suggest_mod.cmd_suggest(query="stripe", project=project, limit=10, ai=True)

    payload = _last_payload(capsys)
    # Команда не упала: лексический локальный кандидат на месте.
    assert [s["slug"] for s in payload["suggestions"]] == ["stripe-pay"]
    assert any(
        "семантич" in n.lower() or "хаб" in n.lower() for n in payload["notes"]
    )


def test_non_ai_still_lexical_no_reason_key_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Без --ai: лексическая выдача, reason пуст (обратная совместимость)."""
    _cfg, store, project = _wire(tmp_path, monkeypatch, logged_in=False)
    _store_skill(store, "stripe-pay", tags=("stripe",))

    def _boom_client(*a: Any, **k: Any) -> Any:
        raise AssertionError("сеть не нужна без логина")

    monkeypatch.setattr(_common, "make_client", _boom_client)

    suggest_mod.cmd_suggest(query="stripe", project=project, limit=10)

    payload = _last_payload(capsys)
    s = payload["suggestions"][0]
    assert s["slug"] == "stripe-pay"
    assert s.get("reason") == ""  # лексический режим reason не несёт
