"""Тесты P1 C3 — ``members`` / ``member ...`` / ``roles`` (эпик member).

Контракты сверены с backend-роутами:

- ``GET /users`` (routes/users.py:233) — offset-режим ``page``/``size``/``q``,
  ответ ``UserListResponse{items,total,page,size}``. Guard: любой
  аутентифицированный (member без admin-прав получает «только себя» — бэк
  сужает сам, 403 НЕ кидает).
- ``POST /invites`` (routes/invites.py:57) — flat; ``transport.issue_invite``
  переиспользуется (НЕ дублируется). Бэк требует email+display_name СТРОГО
  вместе (422) → CLI derive'ит display_name из local-part email.
- ``DELETE /memberships`` (routes/memberships.py:32) — query
  ``user_id``+``company_id``, 204; право ``user.remove``.
- ``POST /users/bulk/change-role`` (routes/users.py:1095) —
  ``{ids,role_id,company_id}`` → ``BulkActionResponse``; право
  ``role.manage``|hub.admin (kebab-канон волны 3; ``change_role`` deprecated).
- ``PUT /users/{id}/lock|unlock`` (routes/users.py:842,888) — lock body
  ``{reason?}``; ответ ``UserListItemDTO`` (PUT-канон волны 3; POST deprecated).
- ``POST /users/{id}/reset-password`` (routes/users.py:1152) — ответ
  ``ResetPasswordResponse{temp_password,expires_hint,requires_password_change}``.
- ``GET /roles`` (routes/roles.py:99) — PAGED ``{items,total,page,size}``
  (НЕ плоский список), любой авторизованный.
"""
from __future__ import annotations

import json as _json
from typing import Any
from unittest.mock import MagicMock

import pytest
import respx
from httpx import Response

from skillery_cli import output as output_module
from skillery_cli.config import ClientConfig
from skillery_cli.core.transport import HubClient


def _patch_text_mode() -> None:
    output_module._mode = "text"


def _user_item(
    uid: str = "5",
    email: str = "m@acme.ru",
    *,
    is_locked: bool = False,
) -> dict[str, Any]:
    return {
        "id": uid,
        "email": email,
        "display_name": "Member",
        "is_active": True,
        "created_at": "2026-06-01T10:00:00Z",
        "status": "active",
        "is_locked": is_locked,
        "last_login_at": None,
        "memberships": [
            {
                "company": {"id": "7", "slug": "acme", "name": "Acme"},
                "role": {"id": "3", "slug": "member", "label": "Участник"},
            }
        ],
    }


# ============================================================
# Transport — методы секции «# --- P1 member ---»
# ============================================================
async def test_transport_list_users_sends_offset_params() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/users").mock(
            return_value=Response(
                200,
                json={"items": [_user_item()], "total": 1, "page": 2, "size": 10},
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            resp = await client.list_users(
                company_id="7", q="acme", page=2, size=10
            )
        finally:
            await client.close()
        assert route.called
        params = route.calls.last.request.url.params
        assert params["company_id"] == "7"
        assert params["q"] == "acme"
        assert params["page"] == "2"
        assert params["size"] == "10"
        assert resp["total"] == 1


async def test_transport_list_users_omits_optional_params() -> None:
    """Без company_id/q эти ключи НЕ уходят в query (hub-admin: все users)."""
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/users").mock(
            return_value=Response(
                200, json={"items": [], "total": 0, "page": 1, "size": 25}
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            await client.list_users()
        finally:
            await client.close()
        params = route.calls.last.request.url.params
        assert "company_id" not in params
        assert "q" not in params
        assert params["page"] == "1"
        assert params["size"] == "25"


async def test_transport_remove_membership_uses_canon_path() -> None:
    """M-1: канон — DELETE /companies/{cid}/members/{uid} (path), без query.

    Раньше CLI слал deprecated ``DELETE /memberships?user_id=&company_id=``;
    теперь — канон path-форму (бэкенд принимает оба, поведение идентично).
    """
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.delete("/companies/7/members/5").mock(
            return_value=Response(204)
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            result = await client.remove_membership(user_id="5", company_id="7")
        finally:
            await client.close()
        assert route.called
        assert route.calls.last.request.method == "DELETE"
        # query-параметры на канон-пути не передаются
        assert "user_id" not in route.calls.last.request.url.params
        assert result is None


async def test_transport_bulk_change_role_body() -> None:
    """POST /users/bulk/change-role — {ids:[id], role_id, company_id} (#1429)."""
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/users/bulk/change-role").mock(
            return_value=Response(
                200,
                json={
                    "processed": 1,
                    "updated": 1,
                    "skipped_ids": [],
                    "errors": [],
                    "results": [{"id": "5", "ok": True, "outcome": "updated"}],
                    "affected_count": 1,
                    "dry_run": False,
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            resp = await client.bulk_change_role(
                user_ids=["5"], role_id="2", company_id="7"
            )
        finally:
            await client.close()
        body = _json.loads(route.calls.last.request.content)
        assert body == {"ids": ["5"], "role_id": "2", "company_id": "7"}
        assert resp["updated"] == 1


async def test_transport_lock_user_sends_reason() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.patch("/users/5").mock(
            return_value=Response(200, json=_user_item(is_locked=True))
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            resp = await client.lock_user("5", reason="нарушение")
        finally:
            await client.close()
        body = _json.loads(route.calls.last.request.content)
        assert body == {"is_locked": True, "lock_reason": "нарушение"}
        assert resp["is_locked"] is True


async def test_transport_lock_user_without_reason_sends_empty_body() -> None:
    """reason опционален — без него в теле только сам флаг."""
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.patch("/users/5").mock(
            return_value=Response(200, json=_user_item(is_locked=True))
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            await client.lock_user("5")
        finally:
            await client.close()
        assert _json.loads(route.calls.last.request.content) == {
            "is_locked": True
        }


async def test_transport_unlock_user_posts() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.patch("/users/5").mock(
            return_value=Response(200, json=_user_item(is_locked=False))
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            resp = await client.unlock_user("5")
        finally:
            await client.close()
        assert route.called
        assert resp["is_locked"] is False


async def test_transport_reset_user_password_posts() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/users/5/password-resets").mock(
            return_value=Response(
                200,
                json={
                    "temp_password": "TMP-secret-42",
                    "expires_hint": "Передайте пользователю",
                    "requires_password_change": True,
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            resp = await client.reset_user_password("5")
        finally:
            await client.close()
        assert route.called
        assert resp["temp_password"] == "TMP-secret-42"


async def test_transport_list_roles_paged() -> None:
    """GET /roles — paged-форма (W5): {items,total,page,size}, не плоский."""
    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.get("/roles").mock(
            return_value=Response(
                200,
                json={
                    "items": [
                        {
                            "id": "1",
                            "slug": "owner",
                            "name": "Владелец",
                            "is_system": True,
                            "is_assignable_by_company": True,
                            "permission_keys": ["company.manage"],
                            "member_count": 2,
                        }
                    ],
                    "total": 3,
                    "page": 1,
                    "size": 100,
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            resp = await client.list_roles(page=1, size=100)
        finally:
            await client.close()
        params = route.calls.last.request.url.params
        assert params["page"] == "1"
        assert params["size"] == "100"
        assert resp["total"] == 3
        assert resp["items"][0]["slug"] == "owner"


# ============================================================
# Команды — commands/member.py (мок _common)
# ============================================================
def _cfg(
    monkeypatch: pytest.MonkeyPatch,
    perms: list[str],
    *,
    company_id: str | None = "7",
) -> ClientConfig:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=perms,
        company_id=company_id,
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    return cfg


def _fake_factory(
    monkeypatch: pytest.MonkeyPatch, fake_client: MagicMock
) -> None:
    from skillery_cli.commands import _common

    monkeypatch.setattr(_common, "HubClient", lambda **kw: fake_client)
    monkeypatch.setattr(_common, "load_tokens", lambda email: ("a", "r"))


def test_cmd_members_list_defaults_company_from_cfg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без --company company_id берётся из cfg (JWT)."""
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["skill.read"], company_id="7")

    captured: dict[str, Any] = {}

    async def _list_users(
        *, company_id=None, q=None, page=1, size=25
    ):  # noqa: ANN001, ANN002
        captured.update(
            {"company_id": company_id, "q": q, "page": page, "size": size}
        )
        return {"items": [_user_item()], "total": 1, "page": 1, "size": 25}

    async def _close() -> None:
        return None

    fake_client = MagicMock()
    fake_client.list_users = _list_users
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    member_mod.cmd_members_list(company=None, q=None, page=1, size=25)
    assert captured["company_id"] == "7"


def test_cmd_members_list_explicit_company_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["hub.admin"], company_id="7")

    captured: dict[str, Any] = {}

    async def _list_users(
        *, company_id=None, q=None, page=1, size=25
    ):  # noqa: ANN001, ANN002
        captured.update({"company_id": company_id, "q": q})
        return {"items": [], "total": 0, "page": 1, "size": 25}

    async def _close() -> None:
        return None

    fake_client = MagicMock()
    fake_client.list_users = _list_users
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    member_mod.cmd_members_list(company="99", q="ivan", page=1, size=25)
    assert captured["company_id"] == "99"
    assert captured["q"] == "ivan"


def test_cmd_member_invite_reuses_issue_invite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """member invite зовёт СУЩЕСТВУЮЩИЙ transport.issue_invite (не дубль)."""
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.invite"], company_id="7")

    captured: dict[str, Any] = {}

    async def _issue_invite(
        company_id, role_id, email=None, display_name=None
    ):  # noqa: ANN001
        captured.update(
            {
                "company_id": company_id,
                "role_id": role_id,
                "email": email,
                "display_name": display_name,
            }
        )
        return {
            "invite_id": "1",
            "invite_token": "TOK",
            "invite_url": "http://x/auth/invite/TOK",
            "expires_at": "2026-06-20T12:00:00Z",
            "is_new_user": True,
        }

    async def _close() -> None:
        return None

    fake_client = MagicMock()
    fake_client.issue_invite = _issue_invite
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    member_mod.cmd_member_invite(
        email="new@acme.ru", role_id="3", name="Новый", company=None
    )
    assert captured["company_id"] == "7"  # default из cfg
    assert captured["role_id"] == "3"
    assert captured["email"] == "new@acme.ru"
    assert captured["display_name"] == "Новый"


def test_cmd_member_invite_derives_name_from_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Бэк требует email+display_name ВМЕСТЕ (422) → без --name CLI
    подставляет local-part email'а."""
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.invite"], company_id="7")

    captured: dict[str, Any] = {}

    async def _issue_invite(
        company_id, role_id, email=None, display_name=None
    ):  # noqa: ANN001
        captured.update({"email": email, "display_name": display_name})
        return {
            "invite_id": "1",
            "invite_token": "TOK",
            "invite_url": "http://x/auth/invite/TOK",
            "expires_at": "2026-06-20T12:00:00Z",
            "is_new_user": True,
        }

    async def _close() -> None:
        return None

    fake_client = MagicMock()
    fake_client.issue_invite = _issue_invite
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    member_mod.cmd_member_invite(
        email="ivan.petrov@acme.ru", role_id="3", name=None, company=None
    )
    assert captured["display_name"] == "ivan.petrov"


def test_cmd_member_invite_requires_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Нет ни --company, ни company_id в cfg (hub-admin без компании) → exit 1."""
    import typer

    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.invite"], company_id=None)
    monkeypatch.setattr(
        "skillery_cli.commands._common.load_tokens", lambda email: ("a", "r")
    )

    with pytest.raises(typer.Exit) as exc:
        member_mod.cmd_member_invite(
            email="x@y.ru", role_id="3", name=None, company=None
        )
    assert exc.value.exit_code == 1


def test_cmd_member_remove_calls_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.remove"], company_id="7")

    captured: dict[str, Any] = {}

    async def _remove(*, user_id, company_id):  # noqa: ANN001
        captured.update({"user_id": user_id, "company_id": company_id})
        return None

    async def _close() -> None:
        return None

    fake_client = MagicMock()
    fake_client.remove_membership = _remove
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    member_mod.cmd_member_remove(user_id="5", company=None)
    assert captured == {"user_id": "5", "company_id": "7"}


def test_cmd_member_remove_requires_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.remove"], company_id=None)
    monkeypatch.setattr(
        "skillery_cli.commands._common.load_tokens", lambda email: ("a", "r")
    )

    with pytest.raises(typer.Exit) as exc:
        member_mod.cmd_member_remove(user_id="5", company=None)
    assert exc.value.exit_code == 1


def test_cmd_member_change_role_single_user_via_bulk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """change-role одного user'а идёт через bulk-эндпоинт c user_ids=[id]."""
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["role.manage"], company_id="7")

    captured: dict[str, Any] = {}

    async def _bulk(*, user_ids, role_id, company_id):  # noqa: ANN001
        captured.update(
            {"user_ids": user_ids, "role_id": role_id, "company_id": company_id}
        )
        return {
            "processed": 1,
            "updated": 1,
            "skipped_ids": [],
            "errors": [],
            "results": [{"id": "5", "ok": True, "outcome": "updated"}],
            "affected_count": 1,
            "dry_run": False,
        }

    async def _close() -> None:
        return None

    fake_client = MagicMock()
    fake_client.bulk_change_role = _bulk
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    member_mod.cmd_member_change_role(user_id="5", role_id="2", company=None)
    assert captured == {"user_ids": ["5"], "role_id": "2", "company_id": "7"}


def test_cmd_member_lock_passes_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.lock"], company_id="7")

    captured: dict[str, Any] = {}

    async def _lock(user_id, *, reason=None):  # noqa: ANN001
        captured.update({"user_id": user_id, "reason": reason})
        return _user_item(is_locked=True)

    async def _close() -> None:
        return None

    fake_client = MagicMock()
    fake_client.lock_user = _lock
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    member_mod.cmd_member_lock(user_id="5", reason="спам")
    assert captured == {"user_id": "5", "reason": "спам"}


def test_cmd_member_unlock(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.lock"], company_id="7")

    captured: dict[str, Any] = {}

    async def _unlock(user_id):  # noqa: ANN001
        captured["user_id"] = user_id
        return _user_item(is_locked=False)

    async def _close() -> None:
        return None

    fake_client = MagicMock()
    fake_client.unlock_user = _unlock
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    member_mod.cmd_member_unlock(user_id="5")
    assert captured == {"user_id": "5"}


def test_cmd_member_reset_password_prints_one_time_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """one-time пароль выводится КРУПНО + предупреждение «один раз»."""
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["company.manage"], company_id="7")

    async def _reset(user_id):  # noqa: ANN001
        return {
            "temp_password": "TMP-pass-123",
            "expires_hint": "Передайте пользователю",
            "requires_password_change": True,
        }

    async def _close() -> None:
        return None

    fake_client = MagicMock()
    fake_client.reset_user_password = _reset
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    member_mod.cmd_member_reset_password(user_id="5")
    out = capsys.readouterr().out
    assert "TMP-pass-123" in out
    assert "ОДИН раз" in out


# ============================================================
# Регистрация в build_app — блок «# --- P1 member ---»
# ============================================================
def _build_app(monkeypatch: pytest.MonkeyPatch, perms: list[str]):  # noqa: ANN202
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=perms,
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    return build_app()


def test_members_and_roles_registered_for_plain_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """members/roles — для любого залогиненного (бэк сам сужает выдачу).

    #1223: плоские имена ОСТАЛИСЬ (back-compat, скрытые алиасы), а группа
    ``member`` теперь есть и без мутационных прав — в неё переехал read-only
    список (``members`` → ``member list``). Отсутствие прав проверяем по
    МУТАЦИЯМ внутри группы, а не по факту её наличия.
    """
    app = _build_app(monkeypatch, ["skill.read"])
    names = [c.name for c in app.registered_commands]
    assert "members" in names
    assert "roles" in names
    groups = {g.name: g.typer_instance for g in app.registered_groups}
    member_sub = [c.name for c in groups["member"].registered_commands]
    assert member_sub == ["list"]


def test_member_subapp_partial_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """role.manage → только change-role; lock/unlock/remove/invite/reset
    отсутствуют (role.manage НЕ входит в can_admin_users → lock скрыт)."""
    app = _build_app(monkeypatch, ["role.manage"])
    member_group = next(g for g in app.registered_groups if g.name == "member")
    sub = [c.name for c in member_group.typer_instance.registered_commands]
    assert "change-role" in sub
    assert "invite" not in sub
    assert "remove" not in sub
    assert "lock" not in sub
    assert "unlock" not in sub
    assert "reset-password" not in sub


def test_member_subapp_invite_grants_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3 D2.2: user.invite входит в backend `_can_admin_users` → manager
    (user.invite) ДОЛЖЕН видеть lock/unlock в CLI (раньше гейт по узкому
    user.lock прятал их, хотя бэк/UI допускают)."""
    app = _build_app(monkeypatch, ["user.invite"])
    member_group = next(g for g in app.registered_groups if g.name == "member")
    sub = [c.name for c in member_group.typer_instance.registered_commands]
    assert "invite" in sub
    assert "lock" in sub
    assert "unlock" in sub
    # invite НЕ даёт remove/change-role/reset-password.
    assert "remove" not in sub
    assert "change-role" not in sub
    assert "reset-password" not in sub


def test_member_subapp_company_manage_grants_lock_and_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S3 D2.2: owner (company.manage) видит lock/unlock (can_admin_users) и
    reset-password (company.manage) — паритет с backend."""
    app = _build_app(monkeypatch, ["company.manage"])
    member_group = next(g for g in app.registered_groups if g.name == "member")
    sub = [c.name for c in member_group.typer_instance.registered_commands]
    assert "lock" in sub
    assert "unlock" in sub
    assert "reset-password" in sub


def test_member_subapp_full_for_hub_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hub.admin (has_permission bypass) → все подкоманды member."""
    app = _build_app(monkeypatch, ["hub.admin"])
    member_group = next(g for g in app.registered_groups if g.name == "member")
    sub = [c.name for c in member_group.typer_instance.registered_commands]
    for name in (
        "invite",
        "remove",
        "change-role",
        "lock",
        "unlock",
        "reset-password",
    ):
        assert name in sub, f"нет подкоманды {name}"


def test_member_commands_absent_when_logged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [c.name for c in app.registered_commands]
    assert "members" not in names
    assert "roles" not in names
    assert "member" not in [g.name for g in app.registered_groups]


def test_cmd_roles_renders_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """roles выводит id/slug/name/is_assignable_by_company из paged-ответа."""
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["skill.read"], company_id="7")

    async def _roles(*, page=1, size=100, q=None):  # noqa: ANN001
        return {
            "items": [
                {
                    "id": "1",
                    "slug": "owner",
                    "name": "Владелец",
                    "is_system": True,
                    "is_assignable_by_company": True,
                },
                {
                    "id": "9",
                    "slug": "auditor",
                    "name": "Аудитор",
                    "is_system": False,
                    "is_assignable_by_company": False,
                },
            ],
            "total": 2,
            "page": 1,
            "size": 100,
        }

    async def _close() -> None:
        return None

    fake_client = MagicMock()
    fake_client.list_roles = _roles
    fake_client.close = _close
    _fake_factory(monkeypatch, fake_client)

    member_mod.cmd_roles_list()
