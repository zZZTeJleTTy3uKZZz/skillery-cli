"""D-CLI — тесты команд (member M-3/M-5, collection M-2/M-6, permission M-4).

Мокаем ``_common.HubClient`` фейком (как в ``test_p1_member.py``); проверяем,
что команда дёргает нужный transport-метод с верными аргументами, и что
RBAC-гейты в ``build_app`` регистрируют/скрывают подкоманды правильно.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from skillery_cli import output as output_module
from skillery_cli.config import ClientConfig


def _patch_text_mode() -> None:
    output_module._mode = "text"


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


async def _noop_close() -> None:
    return None


def _bulk_ok() -> dict[str, Any]:
    return {
        "updated_count": 1,
        "skipped_ids": [],
        "results": [{"id": "5", "outcome": "updated"}],
        "affected_count": 1,
        "dry_run": False,
    }


# ============================================================
# M-3 — member suspend / activate / revoke-sessions
# ============================================================
def test_cmd_member_suspend_calls_bulk(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.lock"])
    captured: dict[str, Any] = {}

    async def _bulk(*, user_ids):  # noqa: ANN001
        captured["user_ids"] = user_ids
        return _bulk_ok()

    fc = MagicMock()
    fc.bulk_suspend = _bulk
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    member_mod.cmd_member_suspend(user_id="5")
    assert captured == {"user_ids": ["5"]}


def test_cmd_member_activate_calls_bulk(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.lock"])
    captured: dict[str, Any] = {}

    async def _bulk(*, user_ids):  # noqa: ANN001
        captured["user_ids"] = user_ids
        return _bulk_ok()

    fc = MagicMock()
    fc.bulk_activate = _bulk
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    member_mod.cmd_member_activate(user_id="5")
    assert captured == {"user_ids": ["5"]}


def test_cmd_member_revoke_sessions_calls_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.lock"])
    captured: dict[str, Any] = {}

    async def _revoke(user_id):  # noqa: ANN001
        captured["user_id"] = user_id
        return {"id": user_id, "email": "m@acme.ru"}

    fc = MagicMock()
    fc.revoke_user_sessions = _revoke
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    member_mod.cmd_member_revoke_sessions(user_id="5")
    assert captured == {"user_id": "5"}


# ============================================================
# M-5 — member create / edit / delete / transfer / export
# ============================================================
def test_cmd_member_create_passes_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.create"], company_id="7")
    captured: dict[str, Any] = {}

    async def _create(**kw):  # noqa: ANN003
        captured.update(kw)
        return {"user_id": "9", "is_new_user": True}

    fc = MagicMock()
    fc.create_user = _create
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    member_mod.cmd_member_create(
        email="new@acme.ru",
        name="Новый",
        role_id="3",
        first_name=None,
        last_name=None,
        set_password=None,
        company=None,
    )
    assert captured["email"] == "new@acme.ru"
    assert captured["display_name"] == "Новый"
    assert captured["company_id"] == "7"  # default из cfg
    assert captured["role_id"] == "3"


def test_cmd_member_create_derives_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.create"], company_id="7")
    captured: dict[str, Any] = {}

    async def _create(**kw):  # noqa: ANN003
        captured.update(kw)
        return {"user_id": "9", "is_new_user": True}

    fc = MagicMock()
    fc.create_user = _create
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    member_mod.cmd_member_create(
        email="ivan.petrov@acme.ru",
        name=None,
        role_id=None,
        first_name=None,
        last_name=None,
        set_password=None,
        company=None,
    )
    assert captured["display_name"] == "ivan.petrov"


def test_cmd_member_create_requires_company(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.create"], company_id=None)
    monkeypatch.setattr(
        "skillery_cli.commands._common.load_tokens", lambda email: ("a", "r")
    )
    with pytest.raises(typer.Exit) as exc:
        member_mod.cmd_member_create(
            email="x@y.ru",
            name=None,
            role_id=None,
            first_name=None,
            last_name=None,
            set_password=None,
            company=None,
        )
    assert exc.value.exit_code == 1


def test_cmd_member_edit_builds_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.update"])
    captured: dict[str, Any] = {}

    async def _update(user_id, payload):  # noqa: ANN001
        captured["user_id"] = user_id
        captured["payload"] = payload
        return {"id": user_id, "email": "m@acme.ru"}

    fc = MagicMock()
    fc.update_user = _update
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    member_mod.cmd_member_edit(
        user_id="5", name="Renamed", first_name=None, last_name=None, status="suspended"
    )
    assert captured["user_id"] == "5"
    assert captured["payload"] == {"display_name": "Renamed", "status": "suspended"}


def test_cmd_member_edit_rejects_empty_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.update"])
    monkeypatch.setattr(
        "skillery_cli.commands._common.load_tokens", lambda email: ("a", "r")
    )
    with pytest.raises(typer.Exit):
        member_mod.cmd_member_edit(
            user_id="5", name=None, first_name=None, last_name=None, status=None
        )


def test_cmd_member_edit_rejects_bad_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.update"])
    monkeypatch.setattr(
        "skillery_cli.commands._common.load_tokens", lambda email: ("a", "r")
    )
    with pytest.raises(typer.Exit):
        member_mod.cmd_member_edit(
            user_id="5", name=None, first_name=None, last_name=None, status="banned"
        )


def test_cmd_member_delete_calls_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["user.delete"])
    captured: dict[str, Any] = {}

    async def _delete(user_id):  # noqa: ANN001
        captured["user_id"] = user_id
        return None

    fc = MagicMock()
    fc.delete_user = _delete
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    member_mod.cmd_member_delete(user_id="5")
    assert captured == {"user_id": "5"}


def test_cmd_member_transfer_passes_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["hub.admin"])
    captured: dict[str, Any] = {}

    async def _transfer(user_id, *, new_company_id, new_role_id, keep_old_membership):  # noqa: ANN001
        captured.update(
            {
                "user_id": user_id,
                "new_company_id": new_company_id,
                "new_role_id": new_role_id,
                "keep_old_membership": keep_old_membership,
            }
        )
        return {"id": user_id, "email": "m@acme.ru"}

    fc = MagicMock()
    fc.transfer_user = _transfer
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    member_mod.cmd_member_transfer(
        user_id="5", new_company="9", new_role_id="2", keep_old=True
    )
    assert captured == {
        "user_id": "5",
        "new_company_id": "9",
        "new_role_id": "2",
        "keep_old_membership": True,
    }


def test_cmd_member_export_writes_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from skillery_cli.commands import member as member_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["hub.admin"])
    csv_text = "id,email\n5,m@acme.ru\n"
    captured: dict[str, Any] = {}

    async def _export(*, company_id=None, q=None, status=None):  # noqa: ANN001
        captured.update({"company_id": company_id, "q": q, "status": status})
        return csv_text

    fc = MagicMock()
    fc.export_users = _export
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    out = tmp_path / "users.csv"
    member_mod.cmd_member_export(
        company=None, q="acme", status=None, output=str(out)
    )
    assert out.read_text(encoding="utf-8") == csv_text
    assert captured["q"] == "acme"


# ============================================================
# M-2 — collection server CRUD (create / add / remove / tags)
# ============================================================
def _enable_collection_server(can_manage: bool = True) -> None:
    from skillery_cli.commands import collection as coll_mod

    coll_mod._SERVER_ENABLED = True
    coll_mod._CAN_MANAGE = can_manage


def test_cmd_collection_create_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli.commands import collection as coll_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["catalog.manage"])
    _enable_collection_server()
    captured: dict[str, Any] = {}

    async def _create(**kw):  # noqa: ANN003
        captured.update(kw)
        return {"id": "10", "slug": "kit", "title": "Kit", "type": "static"}

    fc = MagicMock()
    fc.create_collection = _create
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    coll_mod.cmd_collection_create(
        name="kit",
        local=False,
        title="Kit",
        type_="static",
        description=None,
        company=None,
    )
    assert captured["slug"] == "kit"
    assert captured["title"] == "Kit"
    assert captured["type"] == "static"


def test_cmd_collection_create_server_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без catalog.manage серверный create → exit 1 (подсказка про --local)."""
    import typer

    from skillery_cli.commands import collection as coll_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["skill.read"])
    _enable_collection_server(can_manage=False)
    monkeypatch.setattr(
        "skillery_cli.commands._common.load_tokens", lambda email: ("a", "r")
    )
    with pytest.raises(typer.Exit) as exc:
        coll_mod.cmd_collection_create(
            name="kit",
            local=False,
            title=None,
            type_="static",
            description=None,
            company=None,
        )
    assert exc.value.exit_code == 1


def test_cmd_collection_add_server_resolves_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skillery_cli.commands import collection as coll_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["catalog.manage"])
    _enable_collection_server()
    captured: dict[str, Any] = {}

    async def _get_skill(slug):  # noqa: ANN001
        return {"id": 42}

    async def _add(slug, skill_id):  # noqa: ANN001
        captured.update({"collection": slug, "skill_id": skill_id})
        return {
            "collection_slug": slug,
            "collection_id": "10",
            "skill_id": skill_id,
        }

    fc = MagicMock()
    fc.get_skill = _get_skill
    fc.add_skill_to_collection = _add
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    coll_mod.cmd_collection_add(name="kit", skill_slug="my-skill", local=False)
    # slug навыка резолвится в числовой id (resolve_skill_id → GET /skills/{slug})
    assert captured == {"collection": "kit", "skill_id": "42"}


def test_cmd_collection_remove_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli.commands import collection as coll_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["catalog.manage"])
    _enable_collection_server()
    captured: dict[str, Any] = {}

    async def _remove(slug, skill_ref):  # noqa: ANN001
        captured.update({"collection": slug, "skill": skill_ref})
        return None

    fc = MagicMock()
    fc.remove_skill_from_collection = _remove
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    coll_mod.cmd_collection_remove(name="kit", skill_slug="my-skill", local=False)
    assert captured == {"collection": "kit", "skill": "my-skill"}


def test_cmd_collection_tags_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli.commands import collection as coll_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["catalog.manage"])
    _enable_collection_server()
    captured: dict[str, Any] = {}

    async def _set_tags(slug, *, tag_ids):  # noqa: ANN001
        captured.update({"collection": slug, "tag_ids": tag_ids})
        return None

    fc = MagicMock()
    fc.set_collection_tags = _set_tags
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    coll_mod.cmd_collection_tags(name="kit", tag_ids="1, 2, 3")
    assert captured == {"collection": "kit", "tag_ids": ["1", "2", "3"]}


def test_cmd_collection_tags_rejects_non_numeric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    from skillery_cli.commands import collection as coll_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["catalog.manage"])
    _enable_collection_server()
    monkeypatch.setattr(
        "skillery_cli.commands._common.load_tokens", lambda email: ("a", "r")
    )
    with pytest.raises(typer.Exit):
        coll_mod.cmd_collection_tags(name="kit", tag_ids="1,abc")


# ============================================================
# M-4 — permissions list / role show / role set-permissions
# ============================================================
def test_cmd_permissions_list(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli.commands import permission as perm_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["hub.admin"])
    captured: dict[str, Any] = {}

    async def _list(*, q=None):  # noqa: ANN001
        captured["q"] = q
        return {
            "items": [
                {"key": "skill.publish", "label": "Публиковать", "scope": "tenant"}
            ],
            "total": 1,
            "page": 1,
            "size": 0,
        }

    fc = MagicMock()
    fc.list_permissions = _list
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    perm_mod.cmd_permissions_list(q="skill")
    assert captured["q"] == "skill"


def test_cmd_role_show(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli.commands import permission as perm_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["hub.admin"])
    captured: dict[str, Any] = {}

    async def _list_role(role_id):  # noqa: ANN001
        captured["role_id"] = role_id
        return [{"key": "skill.read", "label": "Видеть", "scope": "tenant"}]

    fc = MagicMock()
    fc.list_role_permissions = _list_role
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    perm_mod.cmd_role_show(role_id="3")
    assert captured == {"role_id": "3"}


def test_cmd_role_set_permissions_splits_perms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from skillery_cli.commands import permission as perm_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["hub.admin"])
    captured: dict[str, Any] = {}

    async def _set(role_id, *, permission_slugs):  # noqa: ANN001
        captured.update({"role_id": role_id, "slugs": permission_slugs})
        return [{"key": "skill.publish", "label": "Публиковать", "scope": "tenant"}]

    fc = MagicMock()
    fc.set_role_permissions = _set
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    perm_mod.cmd_role_set_permissions(role_id="3", perms="skill.publish, skill.manage")
    assert captured["role_id"] == "3"
    assert captured["slugs"] == ["skill.publish", "skill.manage"]


def test_cmd_role_set_permissions_empty_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--perms "" → пустой набор (снять все права)."""
    from skillery_cli.commands import permission as perm_mod

    _patch_text_mode()
    _cfg(monkeypatch, ["hub.admin"])
    captured: dict[str, Any] = {}

    async def _set(role_id, *, permission_slugs):  # noqa: ANN001
        captured["slugs"] = permission_slugs
        return []

    fc = MagicMock()
    fc.set_role_permissions = _set
    fc.close = _noop_close
    _fake_factory(monkeypatch, fc)

    perm_mod.cmd_role_set_permissions(role_id="3", perms="")
    assert captured["slugs"] == []


# ============================================================
# Регистрация в build_app — RBAC-гейты новых команд
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


def _member_subcommands(app) -> list[str]:  # noqa: ANN001
    member_group = next(
        g for g in app.registered_groups if g.name == "member"
    )
    return [c.name for c in member_group.typer_instance.registered_commands]


def test_m3_suspend_activate_revoke_under_can_admin_users(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """company.manage (can_admin_users) → suspend/activate/revoke-sessions есть."""
    app = _build_app(monkeypatch, ["company.manage"])
    sub = _member_subcommands(app)
    assert "suspend" in sub
    assert "activate" in sub
    assert "revoke-sessions" in sub


def test_m5_crud_under_user_perms(monkeypatch: pytest.MonkeyPatch) -> None:
    """user.create/update/delete → create/edit/delete; transfer/export — нет
    (только hub.admin)."""
    app = _build_app(
        monkeypatch, ["user.create", "user.update", "user.delete"]
    )
    sub = _member_subcommands(app)
    assert "create" in sub
    assert "edit" in sub
    assert "delete" in sub
    assert "transfer" not in sub
    assert "export" not in sub


def test_m5_transfer_export_hub_admin_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_app(monkeypatch, ["hub.admin"])
    sub = _member_subcommands(app)
    assert "transfer" in sub
    assert "export" in sub


def test_m4_role_subapp_and_permissions_hub_admin_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _build_app(monkeypatch, ["hub.admin"])
    names = [c.name for c in app.registered_commands]
    groups = [g.name for g in app.registered_groups]
    assert "permissions" in names
    assert "role" in groups
    role_group = next(g for g in app.registered_groups if g.name == "role")
    role_sub = [c.name for c in role_group.typer_instance.registered_commands]
    assert "show" in role_sub
    assert "set-permissions" in role_sub


def test_m4_role_management_absent_for_non_hub_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Управление правами — только hub.admin.

    #1223: сама группа ``role`` теперь ЕСТЬ и у не-админа, потому что в неё
    переехал read-only каталог (``roles`` → ``role list``, доступен любому
    залогиненному). Гейт проверяем там, где он и живёт — на МУТАЦИЯХ:
    ``show``/``set-permissions`` не должны быть зарегистрированы. Каталога
    прав (``permission list``) у не-админа нет вовсе.
    """
    app = _build_app(monkeypatch, ["role.manage", "skill.read"])
    groups = {g.name: g.typer_instance for g in app.registered_groups}
    assert "permission" not in groups
    role_sub = [c.name for c in groups["role"].registered_commands]
    assert "show" not in role_sub
    assert "set-permissions" not in role_sub
    assert role_sub == ["list"]


def test_collection_tags_command_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """M-2: collection tags — всегда зарегистрирована (гейт catalog.manage в
    рантайме команды)."""
    app = _build_app(monkeypatch, ["catalog.manage", "skill.read"])
    coll_group = next(
        g for g in app.registered_groups if g.name == "collection"
    )
    sub = [c.name for c in coll_group.typer_instance.registered_commands]
    assert "tags" in sub
