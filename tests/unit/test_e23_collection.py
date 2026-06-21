"""Тесты ``skills-hub collections`` / ``collection`` (E23 / E10)."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
import respx
from httpx import Response

from skills_hub_cli import output as output_module
from skills_hub_cli.commands import _common, collection as coll_mod
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.core.transport import HubClient


def _text_mode() -> None:
    output_module._mode = "text"


def _fake_factory(monkeypatch: pytest.MonkeyPatch, fake_client: MagicMock) -> None:
    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))


# ---------------------- HubClient ----------------------
@pytest.mark.asyncio
async def test_hubclient_list_collections_passes_filters() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/collections").mock(
            return_value=Response(200, json={"items": []})
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            await client.list_collections(
                company_id="cmp_y", type="static", include_global=False
            )
        finally:
            await client.close()
        params = dict(route.calls[0].request.url.params)
        assert params.get("company_id") == "cmp_y"
        assert params.get("type") == "static"
        assert params.get("include_global") == "false"


@pytest.mark.asyncio
async def test_hubclient_get_collection() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        router.get("/collections/popular").mock(
            return_value=Response(
                200,
                json={
                    "collection": {
                        "id": "col_1",
                        "slug": "popular",
                        "title": "Popular",
                        "description": None,
                        "icon": None,
                        "type": "static",
                        "owner": None,
                        "company": None,
                        "created_at": "2026-05-26T12:00:00Z",
                        "updated_at": "2026-05-26T12:00:00Z",
                        "skills_count": 0,
                    },
                    "skills": [],
                    "tags": [],
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            r = await client.get_collection("popular")
        finally:
            await client.close()
        assert r["collection"]["slug"] == "popular"


# ---------------------- cmd_collections_list ----------------------
def test_cmd_collections_list_renders_items(monkeypatch: pytest.MonkeyPatch) -> None:
    _text_mode()
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    fake_client = MagicMock()

    async def _list(  # noqa: ANN001
        *,
        company_id=None,
        type=None,
        owner_id=None,
        include_global=True,
        page=None,
        size=None,
        sort=None,
        q=None,
    ):
        return {
            "items": [
                {
                    "id": "col_1",
                    "slug": "starred",
                    "title": "Starred",
                    "type": "static",
                    "skills_count": 3,
                    "owner": {"display_name": "Ivan"},
                    "company": None,
                }
            ]
        }

    async def _close() -> None:
        return None

    fake_client.list_collections = _list
    fake_client.close = _close
    monkeypatch.setattr(coll_mod, "_SERVER_ENABLED", True)
    _fake_factory(monkeypatch, fake_client)

    coll_mod.cmd_collection_list(
        local=False, company_id=None, type_=None, owner_id=None, include_global=True
    )


# ---------------------- cmd_collection_show ----------------------
def test_cmd_collection_show_renders_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    _text_mode()
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _get(slug: str) -> dict[str, Any]:
        captured["slug"] = slug
        return {
            "collection": {
                "id": "col_1",
                "slug": slug,
                "title": "Collection 1",
                "description": "desc",
                "icon": None,
                "type": "dynamic",
                "owner": {"display_name": "Alice"},
                "company": {"name": "ACME"},
                "skills_count": 2,
            },
            "skills": [
                {"slug": "skill-a", "title": "Skill A"},
                {"slug": "skill-b", "title": "Skill B"},
            ],
            "tags": [{"slug": "frontend", "label": "Frontend"}],
        }

    async def _close() -> None:
        return None

    fake_client.get_collection = _get
    fake_client.close = _close
    monkeypatch.setattr(coll_mod, "_SERVER_ENABLED", True)
    _fake_factory(monkeypatch, fake_client)

    coll_mod.cmd_collection_show(ref="my-coll", local=False)
    assert captured["slug"] == "my-coll"


# ---------------------- registration ----------------------
def test_collection_subapp_single_with_skill_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """skill.read → единый collection sub-app, серверный режим включён, plural убран."""
    monkeypatch.setattr(coll_mod, "_SERVER_ENABLED", False)
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skills_hub_cli.__main__ import build_app

    app = build_app()
    typer_names = [t.name for t in app.registered_groups]
    assert "collection" in typer_names
    assert "collections" not in typer_names  # plural убран — единый глагол list
    # skill.read включил серверный режим, но не install.
    assert coll_mod._SERVER_ENABLED is True
    assert coll_mod._CAN_INSTALL is False
