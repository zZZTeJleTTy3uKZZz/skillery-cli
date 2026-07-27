"""E-D: personal skills sync между устройствами через хаб — push/pull round-trip.

`pull` (существующий) тянет ``/me/installs`` → локальный стор. `push` (новый) —
зеркало: локальный стор → пометить установленным в хабе
(``POST /skills/{slug}/install``). Round-trip: устройство A (push) → хаб →
устройство B (pull) получает тот же набор.

Contracts:
- ``POST /skills/{slug}/install`` (routes/skills.py:2304) — пометить установленным,
  эмитит install-событие; 404 если навыка нет в хабе.
- ``GET /me/installs`` (routes/me.py:812) — набор, помеченный актором.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import skillery_cli.__main__ as main_mod
from skillery_cli import output as out_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core.installer import write_meta
from skillery_cli.core.transport import ApiError


class _Hub:
    """Разделяемое состояние хаба (истина ``/me/installs``)."""

    def __init__(self) -> None:
        self.installs: dict[str, str] = {}  # ref -> installed_version


class _FakeClient:
    """HubClient-заглушка над разделяемым ``_Hub`` (без сети)."""

    def __init__(self, hub: _Hub, missing: set[str] | None = None) -> None:
        self._hub = hub
        self._missing = missing or set()

    def set_access_token(self, _t: str) -> None:  # noqa: D401
        pass

    async def install_skill(self, ref: str, channel: str = "published") -> dict:
        if ref in self._missing:
            raise ApiError(404, "NOT_FOUND", "навыка нет в хабе", {})
        self._hub.installs[ref] = "1.0.0"
        return {"install_state": {"slug": ref}}

    async def list_my_installs(self) -> list[dict]:
        return [
            {"slug": r, "skill_id": None, "installed_version": v}
            for r, v in self._hub.installs.items()
        ]

    async def close(self) -> None:
        pass


def _seed_store(store: Path, *slugs: str) -> None:
    """Материализовать навыки в сторе (только мета — контент не нужен для sync)."""
    for s in slugs:
        write_meta(
            store / s,
            {"slug": s, "version": "1.0.0", "source": "hub", "manifest": {}},
        )


def _cfg(tmp: Path, store_name: str) -> ClientConfig:
    store = tmp / store_name
    return ClientConfig(
        store_dir=str(store), user_email="a@b.io",
        permissions=["skill.install"],  # is_logged_in = user_email AND permissions
    )


@pytest.fixture(autouse=True)
def _text_mode():
    out_mod._mode = "json"
    yield
    out_mod._mode = "text"


# ==================== push: помечает локальный стор в хабе ===============
def test_push_marks_local_store_installed_in_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = _Hub()
    cfg = _cfg(tmp_path, "store_a")
    _seed_store(cfg.effective_store_dir(), "alpha", "beta")

    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "HubClient", lambda **kw: _FakeClient(hub))
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "acc")
    monkeypatch.setattr(main_mod, "_make_refresh_callback", lambda c: None)

    main_mod.cmd_push(channel="published", dry_run=False)

    assert hub.installs == {"alpha": "1.0.0", "beta": "1.0.0"}


def test_push_skips_skills_not_in_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Авторский навык, не опубликованный (404), → skipped, не failed."""
    import json

    hub = _Hub()
    cfg = _cfg(tmp_path, "store_a")
    _seed_store(cfg.effective_store_dir(), "alpha", "local-only")

    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(
        main_mod, "HubClient",
        lambda **kw: _FakeClient(hub, missing={"local-only"}),
    )
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "acc")
    monkeypatch.setattr(main_mod, "_make_refresh_callback", lambda c: None)

    main_mod.cmd_push(channel="published", dry_run=False)

    assert hub.installs == {"alpha": "1.0.0"}
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    assert data["pushed"] == ["alpha"]
    assert data["skipped"] == ["local-only"]


def test_push_requires_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    cfg = ClientConfig(store_dir=str(tmp_path / "s"))  # без user_email
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    with pytest.raises(typer.Exit) as exc:
        main_mod.cmd_push(channel="published", dry_run=False)
    assert exc.value.exit_code == 1


def test_push_dry_run_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    hub = _Hub()
    cfg = _cfg(tmp_path, "store_a")
    _seed_store(cfg.effective_store_dir(), "alpha")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    def _boom(**kw):
        raise AssertionError("dry-run не должен создавать HubClient")

    monkeypatch.setattr(main_mod, "HubClient", _boom)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "acc")
    main_mod.cmd_push(channel="published", dry_run=True)
    assert hub.installs == {}


# ==================== round-trip: A (push) → хаб → B (pull) =============
def test_roundtrip_push_then_pull_syncs_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Устройство A пушит набор → хаб → устройство B пуллит тот же набор."""
    hub = _Hub()

    # --- устройство A: стор с 2 навыками, push ---
    cfg_a = _cfg(tmp_path, "store_a")
    _seed_store(cfg_a.effective_store_dir(), "alpha", "beta")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg_a))
    monkeypatch.setattr(main_mod, "HubClient", lambda **kw: _FakeClient(hub))
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "acc")
    monkeypatch.setattr(main_mod, "_make_refresh_callback", lambda c: None)
    main_mod.cmd_push(channel="published", dry_run=False)
    assert hub.installs == {"alpha": "1.0.0", "beta": "1.0.0"}

    # --- устройство B: пустой стор, pull; _install_chain — спай ---
    import types

    cfg_b = _cfg(tmp_path, "store_b")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg_b))
    monkeypatch.setattr(
        main_mod, "get_target", lambda name: types.SimpleNamespace(name="claude_code")
    )
    pulled: list[str] = []

    # Двойник принимает ЛЮБЫЕ keyword-аргументы: у `_install_chain` они
    # прибавлялись со временем (`headless`, `initiator`), и жёсткая сигнатура
    # спая роняла тест TypeError'ом на каждом расширении контракта (#1148).
    # Смысл теста — НАБОР подтянутых slug'ов, а не форма вызова.
    async def _spy_chain(cfg_, access, *, slug, **_kwargs):
        pulled.append(slug)
        return [{"slug": slug, "version": "1.0.0"}]

    monkeypatch.setattr(main_mod, "_install_chain", _spy_chain)
    main_mod.cmd_pull(agent=None, channel="published", force=False)

    # B подтянул РОВНО набор, запушенный с A.
    assert sorted(pulled) == ["alpha", "beta"]
