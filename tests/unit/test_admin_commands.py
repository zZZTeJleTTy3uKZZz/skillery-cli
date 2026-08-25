"""Контракт выдачи инвайта (B1) и создания компании (B2).

#2267: группа ``admin`` расформирована — эти действия живут в группах своих
сущностей (``member invite`` / ``company create`` / ``skill yank`` /
``skill sync-versions``), а прежние имена остались скрытыми алиасами. Тесты
здесь проверяют КОНТРАКТ действий на выживших реализациях; что имён ровно по
одному видимому — в ``test_2267_command_dedup.py``.


B1: `issue_invite` бил в УДАЛЁННЫЙ nested-роут `POST /companies/{cid}/invites`
с body `{role_id, group_ids}`. Реальный контракт (backend
`routes/invites.py`): flat `POST /invites` с body `{company_id, role_id,
email?, display_name?}` (без group_ids). Ответ `FlatInviteResponse`
(`invite_token`/`invite_url`/`invite_id`/`expires_at`/`is_new_user`).

B2: `create_company` слал убранное поле `max_users`. Реальный контракт
(`routes/companies.py` + `CreateCompanyRequest`): `{slug, name, owner_email,
owner_display_name}`. slug — опционален (если не задан, не слать).
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from skillery_cli import output as output_module
from skillery_cli.config import ClientConfig


def _patch_text_mode() -> None:
    output_module._mode = "text"


# ============================================================
# B1 — transport.issue_invite → flat POST /invites
# ============================================================
@pytest.mark.asyncio
async def test_issue_invite_posts_flat_invites_endpoint() -> None:
    import respx
    from httpx import Response

    from skillery_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/invites").mock(
            return_value=Response(
                201,
                json={
                    "invite_id": "42",
                    "invite_token": "TOK123",
                    "invite_url": "http://localhost:8000/auth/invite/TOK123",
                    "expires_at": "2026-06-20T12:00:00Z",
                    "is_new_user": False,
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            result = await client.issue_invite(company_id="7", role_id="3")
        finally:
            await client.close()

        assert route.called
        sent = route.calls.last.request
        import json as _json

        body = _json.loads(sent.content)
        # Flat body: company_id + role_id, БЕЗ group_ids.
        assert body == {"company_id": "7", "role_id": "3"}
        assert "group_ids" not in body
        # URL — плоский /invites, не nested.
        assert sent.url.path == "/invites"
        assert result["invite_token"] == "TOK123"


@pytest.mark.asyncio
async def test_issue_invite_includes_email_when_provided() -> None:
    """email+display_name — опциональные поля flat-роута (pre-emptive user)."""
    import json as _json

    import respx
    from httpx import Response

    from skillery_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/invites").mock(
            return_value=Response(
                201,
                json={
                    "invite_id": "9",
                    "invite_token": "T",
                    "invite_url": "http://x/auth/invite/T",
                    "expires_at": "2026-06-20T12:00:00Z",
                    "is_new_user": True,
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            await client.issue_invite(
                company_id="7",
                role_id="3",
                email="new@acme.ru",
                display_name="Новый",
            )
        finally:
            await client.close()
        body = _json.loads(route.calls.last.request.content)
        assert body == {
            "company_id": "7",
            "role_id": "3",
            "email": "new@acme.ru",
            "display_name": "Новый",
        }


def test_cmd_member_invite_calls_issue_invite_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2267: ЕДИНСТВЕННАЯ реализация выдачи инвайта — ``member invite``.

    Прежняя вторая копия ``cmd_admin_invite`` удалена (то же flat
    ``POST /invites``, тот же pre-emptive User+Membership), её имя осталось
    скрытым алиасом сюда. Регрессия B1 проверяется на выжившей функции:
    сигнатура без ``group_ids``, invite_token читается из flat-ответа.
    """
    from skillery_cli.commands import _common
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    cfg = ClientConfig(base_url="http://localhost:8000", company_id="7")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(_common, "get_access_token", lambda: "tok")

    invoked: dict[str, Any] = {}

    async def _fake_issue_invite(
        company_id: str,
        role_id: str,
        email: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        invoked.update(
            {
                "company_id": company_id,
                "role_id": role_id,
                "email": email,
                "display_name": display_name,
            }
        )
        return {
            "invite_id": "1",
            "invite_token": "FLAT-TOK",
            "invite_url": "http://localhost:8000/auth/invite/FLAT-TOK",
            "expires_at": "2026-06-20T12:00:00Z",
            "is_new_user": False,
        }

    async def _fake_close() -> None:
        return None

    fake_client = MagicMock()
    fake_client.issue_invite = _fake_issue_invite
    fake_client.close = _fake_close
    monkeypatch.setattr(_common, "make_client", lambda *a, **kw: fake_client)

    # Сигнатура без group_ids; --company-id (форма прежней `admin invite`)
    # маппится в тот же параметр company.
    member_mod.cmd_member_invite(role_id="3", email=None, name=None, company="7")

    assert invoked["company_id"] == "7"
    assert invoked["role_id"] == "3"
    # email опционален (прежняя `admin invite` умела выдавать голый токен).
    assert invoked["email"] is None
    assert invoked["display_name"] is None


def test_cmd_member_invite_has_no_groups_param() -> None:
    """Регрессия B1: параметра --groups нет у выжившей реализации."""
    import inspect

    from skillery_cli.commands import member as member_mod

    params = inspect.signature(member_mod.cmd_member_invite).parameters
    assert "group_ids" not in params
    assert "company" in params
    assert "role_id" in params


def test_admin_invite_function_is_gone() -> None:
    """#2267: вторая реализация удалена, а не оставлена «на всякий случай»."""
    from skillery_cli import __main__ as main_mod

    assert not hasattr(main_mod, "cmd_admin_invite")


# ============================================================
# B2 — transport.create_company без max_users + опциональный slug
# ============================================================
@pytest.mark.asyncio
async def test_create_company_body_has_no_max_users() -> None:
    import json as _json

    import respx
    from httpx import Response

    from skillery_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/companies").mock(
            return_value=Response(
                201,
                json={
                    "company_id": "11",
                    "owner_invite_id": "5",
                    "owner_invite_token": "OTOK",
                    "owner_invite_url": "http://x/auth/invite/OTOK",
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            await client.create_company(
                {
                    "slug": "acme",
                    "name": "Acme",
                    "owner_email": "o@acme.ru",
                    "owner_display_name": "Owner",
                }
            )
        finally:
            await client.close()
        body = _json.loads(route.calls.last.request.content)
        assert "max_users" not in body
        assert body["slug"] == "acme"


def test_cmd_company_create_omits_slug_when_not_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """slug опционален: если --slug не задан, ключ slug НЕ уходит в payload
    (backend создаст компанию slug-less)."""
    from skillery_cli.commands import _common
    from skillery_cli.commands import company as company_mod

    _patch_text_mode()
    cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(_common, "get_access_token", lambda: "tok")

    captured: dict[str, Any] = {}

    async def _fake_create_company(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {
            "company_id": "11",
            "owner_invite_id": "5",
            "owner_invite_token": "OTOK",
            "owner_invite_url": "http://x/auth/invite/OTOK",
        }

    async def _fake_close() -> None:
        return None

    fake_client = MagicMock()
    fake_client.create_company = _fake_create_company
    fake_client.close = _fake_close
    monkeypatch.setattr(_common, "make_client", lambda *a, **kw: fake_client)

    company_mod.cmd_company_create(
        slug=None,
        name="Acme",
        owner_email="o@acme.ru",
        owner_name="Owner",
    )

    assert "slug" not in captured  # не передаём пустой slug
    assert "max_users" not in captured  # B2: убрано
    assert captured["name"] == "Acme"
    assert captured["owner_email"] == "o@acme.ru"
    assert captured["owner_display_name"] == "Owner"


def test_cmd_company_create_sends_slug_when_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skillery_cli.commands import _common
    from skillery_cli.commands import company as company_mod

    _patch_text_mode()
    cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(_common, "get_access_token", lambda: "tok")

    captured: dict[str, Any] = {}

    async def _fake_create_company(payload: dict[str, Any]) -> dict[str, Any]:
        captured.update(payload)
        return {
            "company_id": "11",
            "owner_invite_id": "5",
            "owner_invite_token": "OTOK",
            "owner_invite_url": "http://x/auth/invite/OTOK",
        }

    async def _fake_close() -> None:
        return None

    fake_client = MagicMock()
    fake_client.create_company = _fake_create_company
    fake_client.close = _fake_close
    monkeypatch.setattr(_common, "make_client", lambda *a, **kw: fake_client)

    company_mod.cmd_company_create(
        slug="acme",
        name="Acme",
        owner_email="o@acme.ru",
        owner_name="Owner",
    )
    assert captured["slug"] == "acme"
    assert "max_users" not in captured


def test_cmd_company_create_has_no_max_users_param() -> None:
    """Регрессия B2: параметр max_users удалён из сигнатуры команды."""
    import inspect

    from skillery_cli.commands.company import cmd_company_create

    params = inspect.signature(cmd_company_create).parameters
    assert "max_users" not in params


def test_admin_company_create_function_is_gone() -> None:
    """#2267: `admin company-create` был вторым ИМЕНЕМ той же функции —
    осталось одно (`company create`), имя-дубль снято в скрытый алиас."""
    from skillery_cli import __main__ as main_mod

    assert not hasattr(main_mod, "cmd_admin_company_create")


# === #340: yank/unyank версии навыка ===


@pytest.mark.asyncio
async def test_yank_skill_version_posts_yank_endpoint() -> None:
    import respx
    from httpx import Response

    from skillery_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        # REST-20 (#1452): путь мутации — числовой id, slug резолвится.
        router.get("/skills/demo").mock(
            return_value=Response(200, json={"id": "3", "slug": "demo"})
        )
        route = router.put("/skills/3/versions/1.1.0/yanked").mock(
            return_value=Response(204)
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            await client.yank_skill_version(
                slug="demo", semver="1.1.0", yank=True
            )
        finally:
            await client.close()
        assert route.called
        assert (
            route.calls.last.request.url.path
            == "/skills/3/versions/1.1.0/yanked"
        )


@pytest.mark.asyncio
async def test_unyank_posts_unyank_endpoint() -> None:
    import respx
    from httpx import Response

    from skillery_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        router.get("/skills/demo").mock(
            return_value=Response(200, json={"id": "3", "slug": "demo"})
        )
        route = router.put("/skills/3/versions/1.1.0/yanked").mock(
            return_value=Response(204)
        )
        client = HubClient(base_url="http://localhost:8000")
        try:
            await client.yank_skill_version(
                slug="demo", semver="1.1.0", yank=False
            )
        finally:
            await client.close()
        assert route.called


def test_cmd_skill_yank_signature() -> None:
    """Команда yank зарегистрирована с ожидаемыми параметрами.

    #2267: живёт в группе ``skill`` (действие над навыком), функция
    переименована в ``cmd_skill_yank``; ``admin yank`` — скрытый алиас.
    """
    import inspect

    from skillery_cli import __main__ as main_mod

    params = inspect.signature(main_mod.cmd_skill_yank).parameters
    assert "slug" in params
    assert "version" in params
    assert "unyank" in params
    assert "slug" in params
