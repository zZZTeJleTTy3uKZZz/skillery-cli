"""Закрытие гэпов матрицы функционала (#1224).

Матрица ``docs/ops/functional-canon.md`` числила за CLI теги, access-grants,
system-config, CRUD ролей, сессии и часть обвязки навыков — а обращений к
этим ручкам в коде не было ни одного. Тесты держат ТРИ гарантии:

а) **транспорт бьёт в реальный путь с реальным телом.** Сеть — через respx, а
   не фейковый клиент: фейк не ловит ни опечатку в URL, ни ошибку транспорта
   (на этом проект уже проезжал баг с ``timeout=`` в вызове метода);
б) **команды зарегистрированы в канон-форме** ``skillery <ресурс> <глагол>``
   и не потеряли исторические глаголы своей группы;
в) **контракт вывода:** под ``--json`` stdout остаётся валидным JSON, всё
   остальное — в stderr.

⚠️ Ширину терминала не трогаем: проверки идут по ``registered_commands``/
``registered_groups``, а не по отрендеренной справке (rich переносит строки
по ``COLUMNS``, и такой тест уже падал в #1223).
"""
from __future__ import annotations

import json

import pytest
import respx
import typer
from httpx import Response
from typer.testing import CliRunner

from skillery_cli import _grouping
from skillery_cli.config import ClientConfig
from skillery_cli.core.transport import HubClient

_BASE = "http://localhost:8000"


# ============================================================
# (а) транспорт — путь, метод, тело
# ============================================================
pytestmark_async = pytest.mark.asyncio


def _tag_dto(tag_id: str = "3", name: str = "crm") -> dict:
    return {
        "id": tag_id,
        "name": name,
        "description": None,
        "icon": None,
        "icon_color": None,
        "usage_count": 4,
        "child_count": 0,
        "parent_id": None,
        "depth": 0,
    }


@pytest.mark.asyncio
async def test_list_tags_without_paging_sends_no_page_size() -> None:
    """Без --page/--size backend отдаёт весь каталог — параметры не шлём.

    Если бы CLI всегда слал ``page``/``size``, backend уходил бы в
    paged-режим и «весь каталог» пришлось бы собирать циклом.
    """
    with respx.mock(base_url=_BASE) as router:
        route = router.get("/tags").mock(
            return_value=Response(200, json={"items": [_tag_dto()], "total": 1})
        )
        client = HubClient(base_url=_BASE)
        try:
            resp = await client.list_tags()
        finally:
            await client.close()
    assert resp["items"][0]["name"] == "crm"
    query = route.calls[0].request.url.params
    assert "page" not in query and "size" not in query
    assert query["sort"] == "name"


@pytest.mark.asyncio
async def test_move_tag_sends_explicit_null_parent() -> None:
    """``--root`` обязан уехать как ``null``, а не как отсутствующий ключ.

    Для backend «ключа нет» = не менять родителя, а ``null`` = в корень.
    Пропусти CLI ключ — команда молча ничего бы не делала.
    """
    with respx.mock(base_url=_BASE) as router:
        route = router.patch("/tags/3").mock(
            return_value=Response(200, json=_tag_dto())
        )
        client = HubClient(base_url=_BASE)
        try:
            await client.move_tag("3", new_parent_id=None)
        finally:
            await client.close()
    body = json.loads(route.calls[0].request.content)
    assert body == {"parent_id": None}


@pytest.mark.asyncio
async def test_set_entity_tags_is_replace_set() -> None:
    """PUT /entity-tags — полная замена набора (пустой список = снять всё)."""
    with respx.mock(base_url=_BASE) as router:
        route = router.put("/entity-tags").mock(
            return_value=Response(
                200,
                json={"entity_type": "skill", "entity_id": "9", "tags": []},
            )
        )
        client = HubClient(base_url=_BASE)
        try:
            await client.set_entity_tags(
                entity_type="skill", entity_id="9", tag_ids=[]
            )
        finally:
            await client.close()
    body = json.loads(route.calls[0].request.content)
    assert body == {"entity_type": "skill", "entity_id": "9", "tag_ids": []}


@pytest.mark.asyncio
async def test_grant_skill_access_body_and_path() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.put("/skills/12/access-grants").mock(
            return_value=Response(
                201,
                json={
                    "id": "1",
                    "skill_id": "12",
                    "target_type": "user",
                    "target_id": "5",
                    "role": "editor",
                    "granted_at": "2026-07-29T10:00:00Z",
                },
            )
        )
        client = HubClient(base_url=_BASE)
        try:
            resp = await client.grant_skill_access(
                "12", target_type="user", target_id="5", role="editor"
            )
        finally:
            await client.close()
    assert resp["role"] == "editor"
    assert json.loads(route.calls[0].request.content) == {
        "target_type": "user",
        "target_id": "5",
        "role": "editor",
    }


@pytest.mark.asyncio
async def test_revoke_collection_access_deletes_grant() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.delete("/collections/4/access-grants/7").mock(
            return_value=Response(204)
        )
        client = HubClient(base_url=_BASE)
        try:
            assert await client.revoke_collection_access("4", "7") is None
        finally:
            await client.close()
    assert route.called


@pytest.mark.asyncio
async def test_system_config_uses_slash_path_not_hyphen() -> None:
    """Путь — ``/system/config``, а НЕ ``/system-config`` (ошибка в матрице).

    Именно здесь фейковый клиент был бы бесполезен: опечатку в URL ловит
    только реальный роутинг.
    """
    with respx.mock(base_url=_BASE) as router:
        route = router.patch("/system/config/feature.x").mock(
            return_value=Response(
                200,
                json={
                    "key": "feature.x",
                    "value_type": "bool",
                    "value": "true",
                    "default": "false",
                    "label": "Фича X",
                    "is_secret": False,
                    "updated_at": "2026-07-29T10:00:00Z",
                },
            )
        )
        client = HubClient(base_url=_BASE)
        try:
            resp = await client.set_system_config("feature.x", value="true")
        finally:
            await client.close()
    assert resp["key"] == "feature.x"
    assert json.loads(route.calls[0].request.content) == {"value": "true"}
    assert "/system/config/" in str(route.calls[0].request.url)


@pytest.mark.asyncio
async def test_sessions_list_and_revoke_all() -> None:
    with respx.mock(base_url=_BASE) as router:
        listed = router.get("/me/sessions").mock(
            return_value=Response(
                200,
                json={
                    "items": [
                        {
                            "id": "1",
                            "created_at": "2026-07-01T00:00:00Z",
                            "expires_at": "2026-08-01T00:00:00Z",
                            "is_current": True,
                            "client_type": "cli",
                        }
                    ],
                    "total": 1,
                    "page": 1,
                    "size": 50,
                },
            )
        )
        revoked = router.delete("/me/sessions").mock(return_value=Response(204))
        client = HubClient(base_url=_BASE)
        try:
            resp = await client.list_sessions()
            await client.revoke_all_sessions()
        finally:
            await client.close()
    assert resp["total"] == 1
    assert listed.calls[0].request.url.params["size"] == "50"
    assert revoked.called


@pytest.mark.asyncio
async def test_logout_sends_refresh_token_in_body() -> None:
    """CLI ходит по Bearer без куки — без тела ручка бы ничего не отозвала."""
    with respx.mock(base_url=_BASE) as router:
        route = router.post("/auth/logout").mock(return_value=Response(204))
        client = HubClient(base_url=_BASE)
        try:
            await client.logout("rt-123")
        finally:
            await client.close()
    assert json.loads(route.calls[0].request.content) == {"refresh_token": "rt-123"}


@pytest.mark.asyncio
async def test_update_me_omits_unset_fields() -> None:
    """Незаданные поля НЕ уезжают: у backend ``None`` = «не менять»."""
    with respx.mock(base_url=_BASE) as router:
        route = router.patch("/me").mock(
            return_value=Response(200, json={"user": {"display_name": "Новое"}})
        )
        client = HubClient(base_url=_BASE)
        try:
            await client.update_me(display_name="Новое")
        finally:
            await client.close()
    assert json.loads(route.calls[0].request.content) == {"display_name": "Новое"}


@pytest.mark.asyncio
async def test_assign_role_posts_pair() -> None:
    with respx.mock(base_url=_BASE) as router:
        route = router.post("/role-assignments").mock(
            return_value=Response(
                201, json={"user_id": "5", "role_id": "2", "created": "true"}
            )
        )
        client = HubClient(base_url=_BASE)
        try:
            resp = await client.assign_role(user_id="5", role_id="2")
        finally:
            await client.close()
    # `created` приходит СТРОКОЙ — команда обязана сравнивать как строку.
    assert resp["created"] == "true"
    assert json.loads(route.calls[0].request.content) == {
        "user_id": "5",
        "role_id": "2",
    }


@pytest.mark.asyncio
async def test_role_crud_paths() -> None:
    role = {"id": "2", "slug": "auditor", "name": "Аудитор", "permission_keys": []}
    with respx.mock(base_url=_BASE) as router:
        get_route = router.get("/roles/2").mock(return_value=Response(200, json=role))
        post_route = router.post("/roles").mock(return_value=Response(201, json=role))
        patch_route = router.patch("/roles/2").mock(
            return_value=Response(200, json=role)
        )
        del_route = router.delete("/roles/2").mock(return_value=Response(204))
        client = HubClient(base_url=_BASE)
        try:
            await client.get_role("2")
            await client.create_role(slug="auditor", name="Аудитор")
            await client.update_role("2", name="Аудитор+")
            await client.delete_role("2")
        finally:
            await client.close()
    assert get_route.called and post_route.called and del_route.called
    # slug неизменяем — в PATCH его быть не должно.
    assert "slug" not in json.loads(patch_route.calls[0].request.content)


@pytest.mark.asyncio
async def test_skill_analytics_uses_from_to_query_names() -> None:
    """Query-параметры называются ``from``/``to`` (зарезервированные слова)."""
    with respx.mock(base_url=_BASE) as router:
        route = router.get("/skills/foo/analytics").mock(
            return_value=Response(200, json={"skill_slug": "foo", "source": "postgres"})
        )
        client = HubClient(base_url=_BASE)
        try:
            await client.get_skill_analytics(
                "foo", date_from="2026-07-01", date_to="2026-07-29"
            )
        finally:
            await client.close()
    params = route.calls[0].request.url.params
    assert params["from"] == "2026-07-01" and params["to"] == "2026-07-29"


@pytest.mark.asyncio
async def test_publish_skill_version_sends_json_not_multipart() -> None:
    """Версия регистрируется JSON'ом: файлы приезжают git-sync'ом, не сюда."""
    with respx.mock(base_url=_BASE) as router:
        # REST-20 (#1452): мутация адресуется числовым id — транспорт сам
        # резолвит переданный slug одним ``GET /skills/{slug}``.
        router.get("/skills/foo").mock(
            return_value=Response(200, json={"id": "7", "slug": "foo"})
        )
        route = router.post("/skills/7/versions").mock(
            return_value=Response(
                201, json={"skill_id": "1", "version_id": "9", "is_new_skill": False}
            )
        )
        client = HubClient(base_url=_BASE)
        try:
            await client.publish_skill_version(
                "foo", semver="1.0.0", commit_sha="abc", manifest={"name": "foo"}
            )
        finally:
            await client.close()
    request = route.calls[0].request
    assert request.headers["content-type"].startswith("application/json")
    body = json.loads(request.content)
    assert body["semver"] == "1.0.0" and body["manifest"] == {"name": "foo"}


@pytest.mark.asyncio
async def test_repo_credential_response_never_carries_secret() -> None:
    """PUT отдаёт только метаданные — значение секрета не возвращается."""
    with respx.mock(base_url=_BASE) as router:
        router.get("/skills/foo").mock(
            return_value=Response(200, json={"id": "7", "slug": "foo"})
        )
        route = router.put("/skills/7/repo-credential").mock(
            return_value=Response(
                200,
                json={
                    "provider": "github",
                    "secret_type": "token",
                    "has_credential": True,
                    "created_at": "2026-07-29T10:00:00Z",
                    "created_by": "1",
                },
            )
        )
        client = HubClient(base_url=_BASE)
        try:
            resp = await client.set_repo_credential(
                "foo", provider="github", secret_type="token", secret="ghp_x"
            )
        finally:
            await client.close()
    assert "secret" not in resp
    assert json.loads(route.calls[0].request.content)["secret"] == "ghp_x"


@pytest.mark.asyncio
async def test_set_skill_star_uses_idempotent_verbs() -> None:
    """#1437: поставить — PUT, снять — DELETE; повтор ничего не переключает."""
    with respx.mock(base_url=_BASE) as router:
        put = router.put("/skills/12/star").mock(
            return_value=Response(
                200,
                json={
                    "is_starred": True,
                    "hub_star_count": 4,
                    "repo_star_count": 0,
                    "total_star_count": 4,
                },
            )
        )
        delete = router.delete("/skills/12/star").mock(
            return_value=Response(
                200,
                json={
                    "is_starred": False,
                    "hub_star_count": 3,
                    "repo_star_count": 0,
                    "total_star_count": 3,
                },
            )
        )
        client = HubClient(base_url=_BASE)
        try:
            first = await client.set_skill_star("12", starred=True)
            # Ретрай ТОГО ЖЕ намерения — состояние не инвертируется.
            second = await client.set_skill_star("12", starred=True)
            off = await client.set_skill_star("12", starred=False)
        finally:
            await client.close()
    assert first["is_starred"] is True
    assert second["is_starred"] is True
    assert off["is_starred"] is False
    assert len(put.calls) == 2 and len(delete.calls) == 1


@pytest.mark.asyncio
async def test_list_events_always_sends_page_and_size() -> None:
    """Offset-режим обязателен: в курсорном ``total`` считает лишь страницу."""
    with respx.mock(base_url=_BASE) as router:
        route = router.get("/events").mock(
            return_value=Response(
                200, json={"items": [], "total": 0, "page": 1, "size": 50}
            )
        )
        client = HubClient(base_url=_BASE)
        try:
            await client.list_events()
        finally:
            await client.close()
    params = route.calls[0].request.url.params
    assert params["page"] == "1" and params["size"] == "50"


@pytest.mark.asyncio
async def test_move_collection_and_stats() -> None:
    with respx.mock(base_url=_BASE) as router:
        router.get("/collections/pack").mock(
            return_value=Response(
                200,
                json={"collection": {"id": "4", "slug": "pack"}, "skills": [], "tags": []},
            )
        )
        move = router.patch("/collections/4").mock(
            return_value=Response(200, json={"slug": "pack", "parent_id": None})
        )
        stats = router.get("/collections/pack/stats").mock(
            return_value=Response(
                200,
                json={
                    "skills_count": 2,
                    "companies_using_count": 1,
                    "installs_total": 7,
                    "timeline": [],
                    "days": 30,
                    "source": "postgres",
                },
            )
        )
        client = HubClient(base_url=_BASE)
        try:
            await client.move_collection("pack", new_parent_id=None)
            resp = await client.get_collection_stats("pack")
        finally:
            await client.close()
    assert json.loads(move.calls[0].request.content) == {"parent_id": None}
    assert resp["installs_total"] == 7
    assert stats.calls[0].request.url.params["days"] == "30"


@pytest.mark.asyncio
async def test_bulk_delete_tags_sends_only_explicit_ids() -> None:
    """Никакого ``all``/``filter``: массовое удаление — только по списку id."""
    with respx.mock(base_url=_BASE) as router:
        route = router.post("/tags/bulk/delete").mock(
            return_value=Response(
                200,
                json={"processed": 2, "updated": 2, "skipped_ids": [], "errors": []},
            )
        )
        client = HubClient(base_url=_BASE)
        try:
            await client.bulk_delete_tags(ids=["1", "2"])
        finally:
            await client.close()
    body = json.loads(route.calls[0].request.content)
    assert body == {"ids": ["1", "2"]}
    assert "all" not in body and "filter" not in body


# ============================================================
# (б) регистрация команд в канон-форме
# ============================================================
def _admin_app(monkeypatch: pytest.MonkeyPatch) -> typer.Typer:
    """``build_app`` с hub.admin — видны все permission-гейтные группы."""
    cfg = ClientConfig(
        base_url=_BASE,
        user_email="u@example.com",
        permissions=["hub.admin"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    return build_app()


def _group(app: typer.Typer, name: str) -> typer.Typer:
    return next(g.typer_instance for g in app.registered_groups if g.name == name)


def _verbs(app: typer.Typer, *path: str) -> set[str]:
    current = app
    for part in path:
        current = _group(current, part)
    return {c.name for c in current.registered_commands if c.name}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            ("tag",),
            {
                "list", "tree", "count", "show", "assignments",
                "create", "edit", "move", "delete",
                "bulk-delete", "bulk-move", "assign",
            },
        ),
        (("access",), {"list", "grant", "revoke"}),
        (("system", "config"), {"list", "show", "set"}),
        (("session",), {"list", "revoke", "revoke-all"}),
        (("skill", "repo"), {"tree", "file", "readme"}),
        (("skill", "credential"), {"show", "set", "delete"}),
        (("skill", "version"), {"add"}),
    ],
)
def test_new_resource_groups_registered(
    monkeypatch: pytest.MonkeyPatch, path: tuple[str, ...], expected: set[str]
) -> None:
    """Новые группы существуют и несут ровно ожидаемые глаголы."""
    app = _admin_app(monkeypatch)
    assert _verbs(app, *path) == expected


def test_new_skill_verbs_do_not_displace_historic_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Долив новых глаголов не должен выбить исторические из группы ``skill``.

    Группа ``skill`` собирается в два приёма: здесь и в ``_grouping``. Если
    порядок сломается, часть глаголов молча исчезнет из справки — а их зовут
    из SKILL.md навыков.
    """
    verbs = _verbs(_admin_app(monkeypatch), "skill")
    assert {"install", "list", "show", "publish", "update"} <= verbs
    assert {"mine", "usage", "collections", "star", "edit", "delete"} <= verbs


def test_role_group_keeps_legacy_verbs(monkeypatch: pytest.MonkeyPatch) -> None:
    """``role show``/``set-permissions`` остались — их формат парсят скрипты."""
    verbs = _verbs(_admin_app(monkeypatch), "role")
    assert {"show", "set-permissions"} <= verbs
    assert {"get", "create", "edit", "delete", "assign"} <= verbs


def test_auth_group_gains_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    verbs = _verbs(_admin_app(monkeypatch), "auth")
    assert "profile" in verbs
    assert {"login", "logout", "whoami"} <= verbs


def test_no_new_flat_commands_added(monkeypatch: pytest.MonkeyPatch) -> None:
    """Новые команды живут ТОЛЬКО в группах — плоское пространство не растёт.

    Плоские имена остаются лишь как back-compat алиасы старых команд (#1223),
    заводить новые незачем.

    Исключения перечислены в ``_grouping._KEEP_FLAT`` и обязаны иметь ПРИЧИНУ:
    горячий путь агента (``run``) или единичное действие, имя которого
    закреплено спекой и уехало в чужие инструкции (``propose``, #2455).
    """
    app = _admin_app(monkeypatch)
    visible_flat = {
        c.name for c in app.registered_commands if c.name and not c.hidden
    }
    assert visible_flat == set(_grouping._KEEP_FLAT)
    assert visible_flat == {"run", "web", "ask", "onboard", "propose"}


def test_gating_hides_mutations_without_rights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Без прав мутации не появляются, а чтение тегов — появляется."""
    cfg = ClientConfig(
        base_url=_BASE, user_email="u@example.com", permissions=["skill.read"]
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    tag_verbs = _verbs(app, "tag")
    assert {"list", "tree", "show"} <= tag_verbs
    assert not ({"create", "delete", "bulk-delete"} & tag_verbs)
    # access/system целиком под гейтом — групп быть не должно.
    names = {g.name for g in app.registered_groups}
    assert "access" not in names and "system" not in names
    # А про свои сессии спросить можно всегда.
    assert "session" in names


# ============================================================
# (в) контракт вывода: --json → чистый stdout
# ============================================================
@pytest.fixture
def json_mode(monkeypatch: pytest.MonkeyPatch):
    """Включить json-режим вывода на время теста.

    Флаг ``--json`` разбирается ПРЕ-ПРОХОДОМ по ``sys.argv`` в ``main()``, а не
    корневым callback'ом (тот его явно игнорирует), — под ``CliRunner``
    передавать флаг аргументом бесполезно. Поэтому режим ставим напрямую, как
    и остальные тесты вывода, и обязательно возвращаем обратно: ``_mode`` —
    глобал модуля, протёкший «json» ломал бы соседние тесты.
    """
    from skillery_cli import output as output_module

    monkeypatch.setattr(output_module, "_mode", "json")
    yield


# ⚠️ Команды ниже вызываются НЕ через корневой app, а через свою группу.
# Корневой callback тянет за собой глобальные побочные эффекты
# (``attach_log_sync`` + ``migrate_legacy_queue``) и кладёт конверт в ОБЩИЙ
# ``outbox.jsonl``, который переживает тест. Один такой конверт уже ронял
# соседний ``test_an_analytics_outbox``. Проверяем контракт вывода команды —
# корневой callback в него не входит.


def test_tag_list_json_stdout_is_pure_json(
    monkeypatch: pytest.MonkeyPatch, json_mode: None
) -> None:
    """В json-режиме stdout — валидный JSON без единой посторонней строки.

    Ровно на этом ломался разбор у тех, кто зовёт CLI из скриптов и SKILL.md:
    любая «полезная» строчка рядом с результатом делает stdout непарсибельным.
    """
    app = _admin_app(monkeypatch)
    monkeypatch.setattr(
        "skillery_cli.commands._common.get_access_token", lambda: "at"
    )

    runner = CliRunner()
    with respx.mock(base_url=_BASE) as router:
        router.get("/tags").mock(
            return_value=Response(200, json={"items": [_tag_dto()], "total": 1})
        )
        result = runner.invoke(_group(app, "tag"), ["list"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["items"][0]["name"] == "crm"


def test_api_error_keeps_stdout_clean(
    monkeypatch: pytest.MonkeyPatch, json_mode: None
) -> None:
    """Ошибка API уходит в stderr — stdout остаётся машинным каналом."""
    app = _admin_app(monkeypatch)
    monkeypatch.setattr(
        "skillery_cli.commands._common.get_access_token", lambda: "at"
    )

    runner = CliRunner()
    with respx.mock(base_url=_BASE) as router:
        router.get("/system/config").mock(
            return_value=Response(403, json={"code": "FORBIDDEN", "message": "нельзя"})
        )
        result = runner.invoke(_group(_group(app, "system"), "config"), ["list"])

    assert result.exit_code == 1
    assert result.stdout.strip() == ""
    assert json.loads(result.stderr.strip().splitlines()[-1])["event"] == "error"


# ——— #1479: строгие по id роуты не должны получать slug ————————————————


@pytest.mark.asyncio
async def test_list_skill_access_grants_resolves_slug_to_id() -> None:
    """GET /skills/{skill_id}/access-grants парсит id строго → slug = 422.

    Мутации (PUT/DELETE) резолв уже делали, а чтение и отзыв — нет: тот же
    ``access list --skill vk`` уходил slug'ом и возвращал 422 вместо списка.
    """
    with respx.mock(base_url=_BASE) as router:
        router.get("/skills/vk").mock(
            return_value=Response(200, json={"id": "12", "slug": "vk"})
        )
        route = router.get("/skills/12/access-grants").mock(
            return_value=Response(200, json={"skill_id": "12", "grants": []})
        )
        client = HubClient(base_url=_BASE)
        try:
            await client.list_skill_access_grants("vk")
        finally:
            await client.close()
    assert route.called


@pytest.mark.asyncio
async def test_revoke_skill_access_resolves_slug_to_id() -> None:
    with respx.mock(base_url=_BASE) as router:
        router.get("/skills/vk").mock(
            return_value=Response(200, json={"id": "12", "slug": "vk"})
        )
        route = router.delete("/skills/12/access-grants/7").mock(
            return_value=Response(204)
        )
        client = HubClient(base_url=_BASE)
        try:
            await client.revoke_skill_access("vk", "7")
        finally:
            await client.close()
    assert route.called


@pytest.mark.asyncio
async def test_list_collection_access_grants_resolves_slug_to_id() -> None:
    """У коллекции ровно та же строгость — и та же дыра в чтении."""
    with respx.mock(base_url=_BASE) as router:
        router.get("/collections/top").mock(
            return_value=Response(
                200, json={"collection": {"id": "4", "slug": "top"}}
            )
        )
        route = router.get("/collections/4/access-grants").mock(
            return_value=Response(200, json={"collection_id": "4", "grants": []})
        )
        client = HubClient(base_url=_BASE)
        try:
            await client.list_collection_access_grants("top")
        finally:
            await client.close()
    assert route.called


@pytest.mark.asyncio
async def test_numeric_ref_costs_no_extra_request() -> None:
    """Числовой ref — fast-path: лишнего GET на резолв нет."""
    with respx.mock(base_url=_BASE) as router:
        route = router.get("/skills/12/access-grants").mock(
            return_value=Response(200, json={"skill_id": "12", "grants": []})
        )
        client = HubClient(base_url=_BASE)
        try:
            await client.list_skill_access_grants("12")
        finally:
            await client.close()
        paths = [call.request.url.path for call in router.calls]
    assert route.called
    assert paths == ["/skills/12/access-grants"], (
        f"на числовом ref резолв обязан быть бесплатным, а ушло: {paths}"
    )
