"""D-CLI — тесты НОВЫХ transport-методов (паритет CLI с backend, M-1..M-6).

Контракты сверены с реальными backend-роутами:

- M-1: ``DELETE /companies/{cid}/members/{uid}`` (канон path-форма,
  ``routes/memberships.py::delete_company_member`` :55) вместо deprecated
  query-формы ``DELETE /memberships``.
- M-2: ``POST /collections`` / ``POST /collections/{slug}/skills`` /
  ``DELETE /collections/{slug}/skills/{id}`` / ``PUT /collections/{slug}/tags``
  (``routes/collections.py``).
- M-3: ``POST /users/bulk/suspend`` / ``/bulk/activate`` (body
  ``BulkUserIdsRequest{user_ids}``) + ``POST /users/{id}/revoke-sessions``
  (``routes/users.py`` :1250/:1277/:1009).
- M-4: ``GET /permissions`` / ``GET /roles/{id}/permissions`` (плоский list) /
  ``PUT /roles/{id}/permissions`` (body ``{permission_slugs}``,
  ``routes/permissions.py`` :111/:169/:253).
- M-5: ``POST /users`` / ``PATCH /users/{id}`` / ``DELETE /users/{id}`` /
  ``POST /users/{id}/transfer`` / ``GET /users/export.csv`` (CSV-текст).
- M-6: ``GET /collections`` с ``page/size/sort/q``.
"""
from __future__ import annotations

import json as _json

import pytest
import respx
from httpx import Response

from skills_hub_cli.core.transport import HubClient

pytestmark = pytest.mark.asyncio

_BASE = "http://localhost:8000"


def _user_dto(uid: str = "5", email: str = "m@acme.ru") -> dict:
    return {
        "id": uid,
        "email": email,
        "display_name": "Member",
        "is_active": True,
        "status": "active",
        "is_locked": False,
        "created_at": "2026-06-01T10:00:00Z",
        "memberships": [],
    }


def _bulk_resp() -> dict:
    return {
        "updated_count": 1,
        "skipped_ids": [],
        "results": [{"id": "5", "outcome": "updated"}],
        "affected_count": 1,
        "dry_run": False,
    }


# ===================== M-1 — remove_membership (canon path) =====================
async def test_remove_membership_uses_canon_path_no_query() -> None:
    """M-1: канон — DELETE /companies/{cid}/members/{uid} (path), без query."""
    with respx.mock(base_url=_BASE) as router:
        route = router.delete("/companies/7/members/5").mock(
            return_value=Response(204)
        )
        client = HubClient(base_url=_BASE)
        try:
            result = await client.remove_membership(user_id="5", company_id="7")
        finally:
            await client.close()
        assert route.called
        assert route.calls.last.request.method == "DELETE"
        # query-параметры на канон-пути не передаются
        assert "user_id" not in route.calls.last.request.url.params
        assert result is None


# ===================== M-2 — server collection CRUD =====================
async def test_create_collection_posts_body() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.post("/collections").mock(
            return_value=Response(
                201,
                json={
                    "id": "10",
                    "slug": "team-kit",
                    "title": "Team Kit",
                    "type": "static",
                },
            )
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            r = await client.create_collection(
                title="Team Kit", type="static", slug="team-kit"
            )
        finally:
            await client.close()
        body = _json.loads(route.calls.last.request.content)
        assert body == {"title": "Team Kit", "type": "static", "slug": "team-kit"}
        assert r["id"] == "10"


async def test_create_collection_omits_none_optionals() -> None:
    """Опциональные поля (description/icon/company_id/parent_id) не уходят."""
    with respx.mock(base_url=_BASE) as router:
        route = router.post("/collections").mock(
            return_value=Response(201, json={"id": "10", "title": "T", "type": "static"})
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            await client.create_collection(title="T")
        finally:
            await client.close()
        body = _json.loads(route.calls.last.request.content)
        assert body == {"title": "T", "type": "static"}


async def test_add_skill_to_collection_posts_skill_id() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.post("/collections/team-kit/skills").mock(
            return_value=Response(
                201,
                json={
                    "collection_slug": "team-kit",
                    "collection_id": "10",
                    "skill_id": "42",
                },
            )
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            r = await client.add_skill_to_collection("team-kit", "42")
        finally:
            await client.close()
        body = _json.loads(route.calls.last.request.content)
        assert body == {"skill_id": "42"}
        assert r["skill_id"] == "42"


async def test_remove_skill_from_collection_deletes() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.delete("/collections/team-kit/skills/42").mock(
            return_value=Response(204)
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            result = await client.remove_skill_from_collection("team-kit", "42")
        finally:
            await client.close()
        assert route.called
        assert route.calls.last.request.method == "DELETE"
        assert result is None


async def test_set_collection_tags_puts_tag_ids() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.put("/collections/team-kit/tags").mock(
            return_value=Response(204)
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            result = await client.set_collection_tags(
                "team-kit", tag_ids=["1", "2"]
            )
        finally:
            await client.close()
        assert route.calls.last.request.method == "PUT"
        body = _json.loads(route.calls.last.request.content)
        assert body == {"tag_ids": ["1", "2"]}
        assert result is None


# ===================== M-3 — bulk suspend/activate + revoke-sessions =====================
async def test_bulk_suspend_posts_user_ids() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.post("/users/bulk/suspend").mock(
            return_value=Response(200, json=_bulk_resp())
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            r = await client.bulk_suspend(user_ids=["5"])
        finally:
            await client.close()
        body = _json.loads(route.calls.last.request.content)
        assert body == {"user_ids": ["5"]}
        assert r["updated_count"] == 1


async def test_bulk_activate_posts_user_ids() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.post("/users/bulk/activate").mock(
            return_value=Response(200, json=_bulk_resp())
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            await client.bulk_activate(user_ids=["5", "6"])
        finally:
            await client.close()
        body = _json.loads(route.calls.last.request.content)
        assert body == {"user_ids": ["5", "6"]}


async def test_revoke_user_sessions_posts() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.post("/users/5/revoke-sessions").mock(
            return_value=Response(200, json=_user_dto())
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            r = await client.revoke_user_sessions("5")
        finally:
            await client.close()
        assert route.called
        assert route.calls.last.request.method == "POST"
        assert r["id"] == "5"


# ===================== M-4 — permissions / role permissions =====================
async def test_list_permissions_sends_q() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.get("/permissions").mock(
            return_value=Response(
                200,
                json={
                    "items": [
                        {
                            "key": "skill.publish",
                            "label": "Публиковать",
                            "scope": "tenant",
                            "used_by_roles_count": 2,
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "size": 0,
                },
            )
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            r = await client.list_permissions(q="skill")
        finally:
            await client.close()
        assert route.calls.last.request.url.params["q"] == "skill"
        assert r["items"][0]["key"] == "skill.publish"


async def test_list_role_permissions_returns_flat_list() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.get("/roles/3/permissions").mock(
            return_value=Response(
                200,
                json=[
                    {"key": "skill.read", "label": "Видеть", "scope": "tenant"},
                    {"key": "skill.install", "label": "Ставить", "scope": "tenant"},
                ],
            )
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            r = await client.list_role_permissions("3")
        finally:
            await client.close()
        assert route.called
        # ответ — ПЛОСКИЙ список (не {items})
        assert isinstance(r, list)
        assert r[0]["key"] == "skill.read"


async def test_set_role_permissions_puts_slugs() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.put("/roles/3/permissions").mock(
            return_value=Response(
                200,
                json=[{"key": "skill.publish", "label": "Публиковать", "scope": "tenant"}],
            )
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            r = await client.set_role_permissions(
                "3", permission_slugs=["skill.publish", "skill.manage"]
            )
        finally:
            await client.close()
        assert route.calls.last.request.method == "PUT"
        body = _json.loads(route.calls.last.request.content)
        assert body == {"permission_slugs": ["skill.publish", "skill.manage"]}
        assert isinstance(r, list)


# ===================== M-5 — user CRUD + transfer + export =====================
async def test_create_user_posts_required_fields() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.post("/users").mock(
            return_value=Response(201, json={"user_id": "9", "is_new_user": True})
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            r = await client.create_user(
                email="new@acme.ru", display_name="New", company_id="7"
            )
        finally:
            await client.close()
        body = _json.loads(route.calls.last.request.content)
        assert body == {
            "email": "new@acme.ru",
            "display_name": "New",
            "company_id": "7",
        }
        assert r["user_id"] == "9"


async def test_create_user_includes_optional_fields() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.post("/users").mock(
            return_value=Response(201, json={"user_id": "9", "is_new_user": True})
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            await client.create_user(
                email="new@acme.ru",
                display_name="New",
                company_id="7",
                role_id="3",
                set_password="hunter2-strong",
            )
        finally:
            await client.close()
        body = _json.loads(route.calls.last.request.content)
        assert body["role_id"] == "3"
        assert body["set_password"] == "hunter2-strong"


async def test_update_user_patches_payload() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.patch("/users/5").mock(
            return_value=Response(200, json=_user_dto())
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            await client.update_user("5", {"display_name": "Renamed"})
        finally:
            await client.close()
        assert route.calls.last.request.method == "PATCH"
        body = _json.loads(route.calls.last.request.content)
        assert body == {"display_name": "Renamed"}


async def test_delete_user_deletes() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.delete("/users/5").mock(return_value=Response(204))
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            result = await client.delete_user("5")
        finally:
            await client.close()
        assert route.calls.last.request.method == "DELETE"
        assert result is None


async def test_transfer_user_posts_body() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.post("/users/5/transfer").mock(
            return_value=Response(200, json=_user_dto())
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            await client.transfer_user(
                "5", new_company_id="9", new_role_id="2"
            )
        finally:
            await client.close()
        body = _json.loads(route.calls.last.request.content)
        assert body == {
            "new_company_id": "9",
            "new_role_id": "2",
            "keep_old_membership": False,
        }


async def test_export_users_returns_csv_text() -> None:
    """export.csv — text/csv, не JSON: метод возвращает СЫРОЙ текст."""
    csv_body = (
        "id,email,first_name,last_name,status,last_login_at,created_at\n"
        "5,m@acme.ru,,,active,,2026-06-01T10:00:00Z\n"
    )
    with respx.mock(base_url=_BASE) as router:
        route = router.get("/users/export.csv").mock(
            return_value=Response(
                200, text=csv_body, headers={"content-type": "text/csv"}
            )
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            text = await client.export_users(q="acme", status="active")
        finally:
            await client.close()
        params = route.calls.last.request.url.params
        assert params["email"] == "acme"  # q → email фильтр export'а
        assert params["status"] == "active"
        assert isinstance(text, str)
        assert "m@acme.ru" in text


# ===================== M-6 — list_collections paging =====================
async def test_list_collections_sends_paging_params() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.get("/collections").mock(
            return_value=Response(
                200,
                json={"items": [], "total": 0, "page": 2, "size": 10},
            )
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            await client.list_collections(
                page=2, size=10, sort="title", q="kit"
            )
        finally:
            await client.close()
        params = route.calls.last.request.url.params
        assert params["page"] == "2"
        assert params["size"] == "10"
        assert params["sort"] == "title"
        assert params["q"] == "kit"


async def test_list_collections_omits_paging_when_unset() -> None:
    """Без page/size/sort/q эти ключи не уходят (backend применит дефолты)."""
    with respx.mock(base_url=_BASE) as router:
        route = router.get("/collections").mock(
            return_value=Response(200, json={"items": []})
        )
        client = HubClient(base_url=_BASE, access_token="t")
        try:
            await client.list_collections()
        finally:
            await client.close()
        params = route.calls.last.request.url.params
        assert "page" not in params
        assert "size" not in params
        assert "sort" not in params
        assert "q" not in params
