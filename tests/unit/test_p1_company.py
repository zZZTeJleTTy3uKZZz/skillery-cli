"""Тесты P1-эпика C2 «company»: компании + invite-links + каталог.

Контракты сверены с backend-роутами:

- ``GET    /companies``                        — routes/companies.py:166
  (hub.admin; params page/size/sort/direction/q; ответ items/total/page/size).
- ``GET    /companies/{id}``                   — routes/companies.py:243
  (член компании или hub.admin).
- ``POST   /companies``                        — routes/companies.py:123
  (hub.company_create; slug опционален и требует hub.slug_manage).
- ``PATCH  /companies/{id}``                   — routes/companies.py:316
  (hub.admin | company.manage; merge-patch).
- ``POST   /me/active-company``                — routes/me.py:195
  (ответ LoginPasswordResponse = НОВАЯ пара токенов → CLI обязан их сохранить).
- ``GET/POST /companies/{id}/invite-links``    — routes/company_invite_links.py:120,138
  (гейт _require_company_admin = hub.admin | role.manage | company.manage;
  create: kind=member|manager, max_uses?, expires_in_days?; ответ + token).
- ``DELETE /companies/{id}/invite-links/{lid}``— routes/company_invite_links.py:179 (204).
- ``GET    /companies/{id}/catalog``           — routes/catalog.py:119.
- ``POST   /companies/{id}/catalog/skills``    — routes/catalog.py:154
  (body {skill_id} — ЧИСЛОВОЙ id, slug надо резолвить).
- ``DELETE /companies/{id}/catalog/skills/{slug}`` — routes/catalog.py:174
  (path принимает id-ИЛИ-slug).
- ``POST   /companies/{id}/catalog/collections``   — routes/catalog.py:203
  (body {collection_id} — числовой id).
- ``DELETE /companies/{id}/catalog/collections/{id}`` — routes/catalog.py:225
  (path — строго числовой id, slug надо резолвить).

Join-URL: Web строит ссылку как ``${origin}/join/${token}``
(web/src/widgets/company-invite-links/CompanyInviteLinks.tsx:33) — CLI
печатает ``cfg.effective_web_ui_url() + /join/<token>``.
"""
from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
import typer

from skills_hub_cli import output as output_module
from skills_hub_cli.config import ClientConfig


def _text_mode() -> None:
    output_module._mode = "text"


def _make_jwt(claims: dict) -> str:
    """Unsigned JWT — CLI читает payload без верификации подписи."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    )
    return f"{header}.{payload}.sig"


# ============================================================
# Transport — контракты HTTP (respx)
# ============================================================
@pytest.mark.asyncio
async def test_list_companies_sends_pagination_params() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/companies").mock(
            return_value=Response(
                200,
                json={"items": [], "total": 0, "page": 2, "size": 10},
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            resp = await client.list_companies(q="acme", page=2, size=10)
        finally:
            await client.close()
        assert route.called
        params = route.calls.last.request.url.params
        assert params["q"] == "acme"
        assert params["page"] == "2"
        assert params["size"] == "10"
        assert resp["total"] == 0


@pytest.mark.asyncio
async def test_list_companies_omits_unset_params() -> None:
    """Без q/size — параметры не шлются (backend применит свои дефолты)."""
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/companies").mock(
            return_value=Response(
                200, json={"items": [], "total": 0, "page": 1, "size": 25}
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            await client.list_companies()
        finally:
            await client.close()
        params = route.calls.last.request.url.params
        assert "q" not in params
        assert "size" not in params


@pytest.mark.asyncio
async def test_get_company_detail_path() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/companies/7").mock(
            return_value=Response(
                200,
                json={
                    "id": "7",
                    "slug": "acme",
                    "name": "Acme",
                    "is_active": True,
                    "created_at": "2026-06-10T12:00:00Z",
                    "members_count": 3,
                    "roles_count": 3,
                    "plan": "free",
                    "status": "active",
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            data = await client.get_company("7")
        finally:
            await client.close()
        assert route.called
        assert data["name"] == "Acme"


@pytest.mark.asyncio
async def test_update_company_sends_merge_patch_body() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.patch("/companies/7").mock(
            return_value=Response(
                200,
                json={
                    "id": "7",
                    "slug": None,
                    "name": "Renamed",
                    "is_active": True,
                    "created_at": "2026-06-10T12:00:00Z",
                    "members_count": 3,
                    "roles_count": 3,
                    "plan": "free",
                    "status": "active",
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            await client.update_company("7", {"name": "Renamed", "owner_id": "5"})
        finally:
            await client.close()
        body = json.loads(route.calls.last.request.content)
        assert body == {"name": "Renamed", "owner_id": "5"}


@pytest.mark.asyncio
async def test_switch_active_company_posts_me_active_company() -> None:
    """POST /me/active-company {company_id} → LoginPasswordResponse (пара токенов)."""
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/me/active-company").mock(
            return_value=Response(
                200,
                json={
                    "access_token": "newA",
                    "access_expires_at": "2026-06-11T12:00:00Z",
                    "refresh_token": "newR",
                    "refresh_expires_at": "2026-07-11T12:00:00Z",
                    "user_id": "1",
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            data = await client.switch_active_company("99")
        finally:
            await client.close()
        body = json.loads(route.calls.last.request.content)
        assert body == {"company_id": "99"}
        assert data["access_token"] == "newA"
        assert data["refresh_token"] == "newR"


@pytest.mark.asyncio
async def test_list_invite_links_path() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/companies/7/invite-links").mock(
            return_value=Response(200, json={"links": []})
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            data = await client.list_invite_links("7")
        finally:
            await client.close()
        assert route.called
        assert data == {"links": []}


@pytest.mark.asyncio
async def test_create_invite_link_full_body() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/companies/7/invite-links").mock(
            return_value=Response(
                201,
                json={
                    "id": "3",
                    "kind": "manager",
                    "is_active": True,
                    "max_uses": 5,
                    "used_count": 0,
                    "expires_at": "2026-07-10T12:00:00Z",
                    "created_at": "2026-06-10T12:00:00Z",
                    "token": "LINKTOK",
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            data = await client.create_invite_link(
                "7", kind="manager", max_uses=5, expires_in_days=30
            )
        finally:
            await client.close()
        body = json.loads(route.calls.last.request.content)
        assert body == {"kind": "manager", "max_uses": 5, "expires_in_days": 30}
        assert data["token"] == "LINKTOK"


@pytest.mark.asyncio
async def test_create_invite_link_omits_optional_fields() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/companies/7/invite-links").mock(
            return_value=Response(
                201,
                json={
                    "id": "4",
                    "kind": "member",
                    "is_active": True,
                    "max_uses": None,
                    "used_count": 0,
                    "expires_at": None,
                    "created_at": "2026-06-10T12:00:00Z",
                    "token": "T2",
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            await client.create_invite_link("7")
        finally:
            await client.close()
        body = json.loads(route.calls.last.request.content)
        assert body == {"kind": "member"}
        assert "max_uses" not in body
        assert "expires_in_days" not in body


@pytest.mark.asyncio
async def test_revoke_invite_link_delete_204() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.delete("/companies/7/invite-links/3").mock(
            return_value=Response(204)
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            result = await client.revoke_invite_link("7", "3")
        finally:
            await client.close()
        assert route.called
        assert result is None


@pytest.mark.asyncio
async def test_get_company_catalog_path() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/companies/7/catalog").mock(
            return_value=Response(
                200,
                json={
                    "company_id": "7",
                    "skills": [],
                    "collections": [],
                    "effective_skills": [],
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            data = await client.get_company_catalog("7")
        finally:
            await client.close()
        assert route.called
        assert data["company_id"] == "7"


@pytest.mark.asyncio
async def test_grant_catalog_skill_posts_numeric_skill_id() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/companies/7/catalog/skills").mock(
            return_value=Response(204)
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            await client.grant_catalog_skill("7", "5")
        finally:
            await client.close()
        body = json.loads(route.calls.last.request.content)
        assert body == {"skill_id": "5"}


@pytest.mark.asyncio
async def test_revoke_catalog_skill_path_accepts_slug() -> None:
    """DELETE /catalog/skills/{slug} — path принимает id-ИЛИ-slug (catalog.py:174)."""
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.delete("/companies/7/catalog/skills/demo-test").mock(
            return_value=Response(204)
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            await client.revoke_catalog_skill("7", "demo-test")
        finally:
            await client.close()
        assert route.called


@pytest.mark.asyncio
async def test_grant_and_revoke_catalog_collection_contract() -> None:
    import respx
    from httpx import Response

    from skills_hub_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        post_route = router.post("/companies/7/catalog/collections").mock(
            return_value=Response(204)
        )
        del_route = router.delete("/companies/7/catalog/collections/9").mock(
            return_value=Response(204)
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            await client.grant_catalog_collection("7", "9")
            await client.revoke_catalog_collection("7", "9")
        finally:
            await client.close()
        body = json.loads(post_route.calls.last.request.content)
        assert body == {"collection_id": "9"}
        assert del_route.called


# ============================================================
# Команды — fakes через _common (monkeypatch)
# ============================================================
def _cfg(
    monkeypatch: pytest.MonkeyPatch,
    perms: list[str],
    *,
    company_id: str | None = "7",
    web_ui_url: str | None = None,
) -> ClientConfig:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=perms,
        company_id=company_id,
        web_ui_url=web_ui_url,
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    return cfg


def _fake_factory(monkeypatch: pytest.MonkeyPatch, fake_client: MagicMock) -> None:
    from skills_hub_cli.commands import _common

    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))


async def _noop_close() -> None:
    return None


def test_cmd_company_switch_saves_new_token_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """switch: ответ несёт новую пару токенов → save_tokens +
    populate_from_jwt + cfg.save; вывод активной компании из нового JWT."""
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    cfg = _cfg(monkeypatch, ["skill.read"], company_id="7")

    new_access = _make_jwt(
        {
            "permissions": ["skill.read", "company.manage"],
            "company_id": "99",
            "role_id": "2",
            "exp": 9999999999,
        }
    )
    fake_client = MagicMock()

    async def _switch(company_id: str) -> dict[str, Any]:
        assert company_id == "99"
        return {
            "access_token": new_access,
            "access_expires_at": "2026-06-11T12:00:00Z",
            "refresh_token": "newR",
            "refresh_expires_at": "2026-07-11T12:00:00Z",
            "user_id": "1",
        }

    async def _new_perms() -> list[str]:
        # JWT-slim: после switch права роли целевой компании приходят из
        # /me/permissions (токен их не несёт).
        return ["company.manage", "skill.read"]

    fake_client.switch_active_company = _switch
    fake_client.close = _noop_close
    fake_client.get_me_permissions = _new_perms
    _fake_factory(monkeypatch, fake_client)

    saved_tokens: dict[str, str] = {}
    monkeypatch.setattr(
        company_mod,
        "save_tokens",
        lambda email, access, refresh: saved_tokens.update(
            {"email": email, "access": access, "refresh": refresh}
        ),
    )
    saved_cfg: list[bool] = []
    monkeypatch.setattr(
        ClientConfig, "save", lambda self, path=None: saved_cfg.append(True)
    )

    company_mod.cmd_company_switch(company_id="99")

    assert saved_tokens["email"] == "u@example.com"
    assert saved_tokens["access"] == new_access
    assert saved_tokens["refresh"] == "newR"
    # populate_from_jwt применён: активная компания из НОВОГО JWT.
    assert cfg.company_id == "99"
    assert "company.manage" in cfg.permissions
    assert saved_cfg, "cfg.save() не вызван — конфиг не зафиксировал смену компании"


def test_cmd_invite_links_create_prints_join_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """create печатает ГОТОВЫЙ join-URL: <web_ui>/join/<token> (как Web)."""
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(
        monkeypatch,
        ["company.manage"],
        company_id="7",
        web_ui_url="https://hub.example",
    )

    fake_client = MagicMock()

    async def _create(
        company_id: str,
        *,
        kind: str = "member",
        max_uses: int | None = None,
        expires_in_days: int | None = None,
    ) -> dict[str, Any]:
        assert company_id == "7"
        assert kind == "member"
        return {
            "id": "3",
            "kind": kind,
            "is_active": True,
            "max_uses": max_uses,
            "used_count": 0,
            "expires_at": None,
            "created_at": "2026-06-10T12:00:00Z",
            "token": "LINKTOK",
        }

    fake_client.create_invite_link = _create
    fake_client.close = _noop_close
    _fake_factory(monkeypatch, fake_client)

    emitted: dict[str, Any] = {}
    monkeypatch.setattr(
        company_mod,
        "emit_data",
        lambda payload, **kw: emitted.update(payload),
    )

    company_mod.cmd_invite_links_create(
        kind="member", max_uses=None, expires_in_days=None, company=None
    )
    assert emitted["join_url"] == "https://hub.example/join/LINKTOK"
    assert emitted["token"] == "LINKTOK"


def test_cmd_invite_links_list_defaults_company_from_cfg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["company.manage"], company_id="7")

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _list(company_id: str) -> dict[str, Any]:
        captured["company_id"] = company_id
        return {"links": []}

    fake_client.list_invite_links = _list
    fake_client.close = _noop_close
    _fake_factory(monkeypatch, fake_client)

    company_mod.cmd_invite_links_list(company=None)
    assert captured["company_id"] == "7"


def test_company_option_missing_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Нет --company и нет company_id в cfg → внятная ошибка + exit 1."""
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["company.manage"], company_id=None)

    with pytest.raises(typer.Exit):
        company_mod.cmd_invite_links_list(company=None)


def test_cmd_invite_links_revoke_passes_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["company.manage"], company_id="7")

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _revoke(company_id: str, link_id: str) -> None:
        captured.update({"company_id": company_id, "link_id": link_id})
        return None

    fake_client.revoke_invite_link = _revoke
    fake_client.close = _noop_close
    _fake_factory(monkeypatch, fake_client)

    company_mod.cmd_invite_links_revoke(link_id="3", company="42")
    assert captured == {"company_id": "42", "link_id": "3"}


def test_cmd_catalog_grant_skill_resolves_slug_to_numeric_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /catalog/skills требует ЧИСЛОВОЙ skill_id (catalog.py:154 →
    _int_id) — slug резолвится через GET /skills/{slug}."""
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["catalog.manage"], company_id="7")

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _get_skill(ref: str) -> dict[str, Any]:
        assert ref == "demo-test"
        return {"id": "5", "slug": "demo-test", "title": "Demo"}

    async def _grant(company_id: str, skill_id: str) -> None:
        captured.update({"company_id": company_id, "skill_id": skill_id})
        return None

    fake_client.get_skill = _get_skill
    fake_client.grant_catalog_skill = _grant
    fake_client.close = _noop_close
    _fake_factory(monkeypatch, fake_client)

    company_mod.cmd_catalog_grant(ref="demo-test", collection=False, company=None)
    assert captured == {"company_id": "7", "skill_id": "5"}


def test_cmd_catalog_grant_skill_numeric_id_skips_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["catalog.manage"], company_id="7")

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _get_skill(ref: str) -> dict[str, Any]:
        raise AssertionError("get_skill не должен звониться для числового id")

    async def _grant(company_id: str, skill_id: str) -> None:
        captured["skill_id"] = skill_id
        return None

    fake_client.get_skill = _get_skill
    fake_client.grant_catalog_skill = _grant
    fake_client.close = _noop_close
    _fake_factory(monkeypatch, fake_client)

    company_mod.cmd_catalog_grant(ref="5", collection=False, company=None)
    assert captured["skill_id"] == "5"


def test_cmd_catalog_grant_collection_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--collection переключает на collections-эндпоинт; slug коллекции
    резолвится через GET /collections/{slug} → collection.id."""
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["catalog.manage"], company_id="7")

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _get_collection(ref: str) -> dict[str, Any]:
        assert ref == "starter-pack"
        return {
            "collection": {"id": "9", "slug": "starter-pack", "title": "Starter"},
            "skills": [],
            "tags": [],
        }

    async def _grant(company_id: str, collection_id: str) -> None:
        captured.update(
            {"company_id": company_id, "collection_id": collection_id}
        )
        return None

    fake_client.get_collection = _get_collection
    fake_client.grant_catalog_collection = _grant
    fake_client.close = _noop_close
    _fake_factory(monkeypatch, fake_client)

    company_mod.cmd_catalog_grant(
        ref="starter-pack", collection=True, company=None
    )
    assert captured == {"company_id": "7", "collection_id": "9"}


def test_cmd_catalog_revoke_skill_passes_ref_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELETE /catalog/skills/{slug} принимает id-или-slug — резолв не нужен."""
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["catalog.manage"], company_id="7")

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _revoke(company_id: str, id_or_slug: str) -> None:
        captured.update({"company_id": company_id, "ref": id_or_slug})
        return None

    fake_client.revoke_catalog_skill = _revoke
    fake_client.close = _noop_close
    _fake_factory(monkeypatch, fake_client)

    company_mod.cmd_catalog_revoke(ref="demo-test", collection=False, company=None)
    assert captured == {"company_id": "7", "ref": "demo-test"}


def test_cmd_catalog_revoke_collection_resolves_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DELETE /catalog/collections/{id} — строго числовой id (catalog.py:225)."""
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["catalog.manage"], company_id="7")

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _get_collection(ref: str) -> dict[str, Any]:
        return {
            "collection": {"id": "9", "slug": "starter-pack", "title": "Starter"},
            "skills": [],
            "tags": [],
        }

    async def _revoke(company_id: str, collection_id: str) -> None:
        captured.update(
            {"company_id": company_id, "collection_id": collection_id}
        )
        return None

    fake_client.get_collection = _get_collection
    fake_client.revoke_catalog_collection = _revoke
    fake_client.close = _noop_close
    _fake_factory(monkeypatch, fake_client)

    company_mod.cmd_catalog_revoke(
        ref="starter-pack", collection=True, company=None
    )
    assert captured == {"company_id": "7", "collection_id": "9"}


def test_cmd_catalog_list_uses_company_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["catalog.view_all"], company_id="7")

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _catalog(company_id: str) -> dict[str, Any]:
        captured["company_id"] = company_id
        return {
            "company_id": company_id,
            "skills": [{"id": "5", "slug": "demo-test", "title": "Demo"}],
            "collections": [],
            "effective_skills": [{"id": "5", "slug": "demo-test", "title": "Demo"}],
        }

    fake_client.get_company_catalog = _catalog
    fake_client.close = _noop_close
    _fake_factory(monkeypatch, fake_client)

    company_mod.cmd_catalog_list(company=None)
    assert captured["company_id"] == "7"


def test_cmd_company_create_reuses_create_company_and_omits_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """company create — переиспользует transport.create_company; без --slug
    ключ slug не уходит (slug-less компания)."""
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["hub.company_create"], company_id=None)

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _create(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {
            "company_id": "11",
            "owner_invite_id": "5",
            "owner_invite_token": "OTOK",
            "owner_invite_url": "http://x/auth/invite/OTOK",
            "slug": None,
        }

    fake_client.create_company = _create
    fake_client.close = _noop_close
    _fake_factory(monkeypatch, fake_client)

    company_mod.cmd_company_create(
        name="Acme", owner_email="o@acme.ru", owner_name="Owner", slug=None
    )
    assert "slug" not in captured
    assert captured == {
        "name": "Acme",
        "owner_email": "o@acme.ru",
        "owner_display_name": "Owner",
    }


def test_cmd_company_edit_builds_patch_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["company.manage"], company_id="7")

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _update(company_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        captured.update({"company_id": company_id, "payload": payload})
        return {
            "id": company_id,
            "slug": None,
            "name": payload.get("name", "Acme"),
            "is_active": True,
            "created_at": "2026-06-10T12:00:00Z",
            "members_count": 1,
            "roles_count": 3,
            "plan": "free",
            "status": "active",
        }

    fake_client.update_company = _update
    fake_client.close = _noop_close
    _fake_factory(monkeypatch, fake_client)

    company_mod.cmd_company_edit(company_id="7", name="Renamed", owner_id="5")
    assert captured["company_id"] == "7"
    assert captured["payload"] == {"name": "Renamed", "owner_id": "5"}


def test_cmd_company_edit_requires_at_least_one_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["company.manage"], company_id="7")

    with pytest.raises(typer.Exit):
        company_mod.cmd_company_edit(company_id="7", name=None, owner_id=None)


def test_cmd_company_list_passes_filters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["hub.admin"], company_id=None)

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _list(
        *,
        q: str | None = None,
        page: int | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        captured.update({"q": q, "page": page, "size": size})
        return {"items": [], "total": 0, "page": page or 1, "size": size or 25}

    fake_client.list_companies = _list
    fake_client.close = _noop_close
    _fake_factory(monkeypatch, fake_client)

    company_mod.cmd_company_list(q="acme", page=2, size=10)
    assert captured == {"q": "acme", "page": 2, "size": 10}


def test_cmd_company_show_fetches_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skills_hub_cli.commands import company as company_mod

    _text_mode()
    _cfg(monkeypatch, ["skill.read"], company_id="7")

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _get(company_id: str) -> dict[str, Any]:
        captured["company_id"] = company_id
        return {
            "id": company_id,
            "slug": "acme",
            "name": "Acme",
            "is_active": True,
            "created_at": "2026-06-10T12:00:00Z",
            "members_count": 3,
            "roles_count": 3,
            "plan": "free",
            "status": "active",
            "owner": {"id": "1", "display_name": "Owner"},
        }

    fake_client.get_company = _get
    fake_client.close = _noop_close
    _fake_factory(monkeypatch, fake_client)

    company_mod.cmd_company_show(company_id="7")
    assert captured["company_id"] == "7"


# ============================================================
# register() — гейтинг подкоманд
# ============================================================
def _company_subcommands(app: typer.Typer) -> set[str]:
    """Имена command'ов и вложенных групп sub-app'а ``company``."""
    for g in app.registered_groups:
        if g.name == "company":
            sub = g.typer_instance
            names = {c.name for c in sub.registered_commands}
            names.update(sg.name for sg in sub.registered_groups)
            return names
    raise AssertionError("sub-app company не зарегистрирован")


def test_register_minimal_member_gets_show_and_switch_only() -> None:
    from skills_hub_cli.commands import company as company_mod

    app = typer.Typer()
    company_mod.register(app)
    names = _company_subcommands(app)
    assert names == {"show", "switch"}


def test_register_full_gates_enable_all_subcommands() -> None:
    from skills_hub_cli.commands import company as company_mod

    app = typer.Typer()
    company_mod.register(
        app,
        can_list=True,
        can_create=True,
        can_edit=True,
        can_invite_links=True,
        can_catalog_view=True,
        can_catalog_manage=True,
    )
    names = _company_subcommands(app)
    assert names == {
        "show",
        "switch",
        "list",
        "create",
        "edit",
        "invite-links",
        "catalog",
    }


def test_register_catalog_view_without_manage_hides_grant_revoke() -> None:
    from skills_hub_cli.commands import company as company_mod

    app = typer.Typer()
    company_mod.register(app, can_catalog_view=True, can_catalog_manage=False)
    for g in app.registered_groups:
        if g.name == "company":
            for sg in g.typer_instance.registered_groups:
                if sg.name == "catalog":
                    cmds = {c.name for c in sg.typer_instance.registered_commands}
                    assert cmds == {"list"}
                    return
    raise AssertionError("catalog sub-app не найден")


def test_build_app_registers_company_for_logged_in_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """build_app: блок `# --- P1 company ---` регистрирует sub-app company
    для любого залогиненного (show/switch always-on; бэк сам режет)."""
    from skills_hub_cli import __main__ as main_mod

    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="member@example.com",
        permissions=["skill.read"],
        company_id="7",
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    app = main_mod.build_app()
    group_names = {g.name for g in app.registered_groups}
    assert "company" in group_names


def test_build_app_no_company_subapp_when_logged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skills_hub_cli import __main__ as main_mod

    cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    app = main_mod.build_app()
    group_names = {g.name for g in app.registered_groups}
    assert "company" not in group_names
