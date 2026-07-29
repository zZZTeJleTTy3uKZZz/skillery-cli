"""``skillery tag …`` — теги и их иерархия (#1224).

Матрица функционала (``docs/ops/functional-canon.md``) числила 13 tag-ручек
за CLI, но в коде не было НИ ОДНОГО обращения к ``/tags`` — группа появляется
здесь впервые. Паритет с backend ``routes/tags.py``.

Глаголы:

- ``tag list`` — плоский список (``GET /tags``) с фильтрами;
- ``tag tree`` — иерархия (``GET /tags/tree``);
- ``tag count`` — только количество (``GET /tags/count``);
- ``tag show`` — карточка тега (``GET /tags/{id}``);
- ``tag create`` / ``tag edit`` / ``tag move`` / ``tag delete`` — CRUD;
- ``tag bulk-delete`` / ``tag bulk-move`` — пачками по явному списку id;
- ``tag assignments`` / ``tag assign`` — теги сущности (``/tags/assignments``).

Права: чтение — любой залогиненный; мутации — ``tag.create``/``tag.manage``
(``hub.admin`` их перекрывает). Гейт в :func:`register` только прячет команды
из справки — финально режет всё равно backend (403).
"""
from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data

console = Console()

#: Сущности, которым можно навешивать теги (зеркало backend-валидации).
ENTITY_TYPES = ("skill", "collection", "company", "user")


def _split_csv(raw: str | None) -> list[str]:
    """``"a, b ,c"`` → ``["a","b","c"]``; пустая строка → ``[]``.

    Пустой список — ЗНАЧИМОЕ значение (снять все теги / ничего не выбрать), а
    не «параметр не задан», поэтому фильтруем только пробельный мусор.
    """
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _tag_table(items: list[dict[str, Any]], *, total: Any = None) -> Table:
    title = "Теги" if total is None else f"Теги (всего: {total})"
    table = Table(title=title)
    table.add_column("id")
    table.add_column("name", overflow="fold")
    table.add_column("parent_id")
    table.add_column("depth")
    table.add_column("детей")
    table.add_column("использований")
    for tag in items:
        table.add_row(
            str(tag.get("id") or "—"),
            str(tag.get("name") or "—"),
            str(tag.get("parent_id") or "—"),
            str(tag.get("depth", 0)),
            str(tag.get("child_count", 0)),
            str(tag.get("usage_count", 0)),
        )
    return table


def cmd_tag_list(
    parent_id: str | None = typer.Option(
        None, "--parent", help="Только дети этого тега"
    ),
    roots_only: bool = typer.Option(
        False, "--roots", help="Только корневые теги"
    ),
    include_descendants: bool = typer.Option(
        False, "--descendants", help="Вместе со всеми потомками --parent"
    ),
    q: str | None = typer.Option(None, "--q", help="Поиск по имени (подстрока)"),
    page: int | None = typer.Option(None, "--page", help="Страница (с 1)"),
    size: int | None = typer.Option(
        None, "--size", help="Размер страницы (макс. 500)"
    ),
    sort: str = typer.Option("name", "--sort", help="name | created | usage"),
    direction: str = typer.Option("asc", "--direction", help="asc | desc"),
) -> None:
    """Плоский список тегов (GET /tags).

    Без ``--page``/``--size`` backend отдаёт весь каталог одним ответом (до
    500 тегов) — для CLI это обычно и нужно.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.list_tags(
                parent_id=parent_id,
                include_descendants=include_descendants,
                roots_only=roots_only,
                q=q,
                page=page,
                size=size,
                sort=sort,
                direction=direction,
            )
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            items = payload.get("items") or []
            if not items:
                console.print("[yellow]Тегов не найдено[/]")
                return
            console.print(_tag_table(items, total=payload.get("total")))

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_tag_tree() -> None:
    """Иерархия тегов целиком (GET /tags/tree)."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_tag_tree()
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            nodes = payload.get("items") or []
            if not nodes:
                console.print("[yellow]Тегов не найдено[/]")
                return
            root = Tree("Теги")

            def _walk(node: dict[str, Any], parent: Tree) -> None:
                tag = node.get("tag") or {}
                label = (
                    f"{tag.get('name', '—')} "
                    f"[dim](id={tag.get('id')}, "
                    f"использований: {tag.get('usage_count', 0)})[/]"
                )
                branch = parent.add(label)
                for child in node.get("children") or []:
                    _walk(child, branch)

            for node in nodes:
                _walk(node, root)
            console.print(root)

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_tag_count(
    parent_id: str | None = typer.Option(None, "--parent", help="Внутри тега"),
    roots_only: bool = typer.Option(False, "--roots", help="Только корневые"),
    include_descendants: bool = typer.Option(
        False, "--descendants", help="Вместе с потомками --parent"
    ),
    q: str | None = typer.Option(None, "--q", help="Поиск по имени"),
) -> None:
    """Сколько тегов подходит под фильтр (GET /tags/count)."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.count_tags(
                parent_id=parent_id,
                include_descendants=include_descendants,
                roots_only=roots_only,
                q=q,
            )
        finally:
            await client.close()

        emit_data(
            resp,
            text_renderer=lambda p: console.print(
                f"Тегов: [bold]{p.get('count', 0)}[/]"
            ),
        )

    _common.run(_do())


def cmd_tag_show(
    tag_id: str = typer.Argument(..., help="ID тега"),
) -> None:
    """Карточка тега (GET /tags/{id})."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_tag(tag_id)
        finally:
            await client.close()

        def _render(tag: dict[str, Any]) -> None:
            console.print(f"[bold]{tag.get('name', '—')}[/] (id={tag.get('id')})")
            console.print(f"  Описание: {tag.get('description') or '—'}")
            console.print(f"  Родитель: {tag.get('parent_id') or '— (корень)'}")
            console.print(f"  Глубина: {tag.get('depth', 0)}")
            console.print(f"  Детей: {tag.get('child_count', 0)}")
            console.print(f"  Использований: {tag.get('usage_count', 0)}")

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_tag_create(
    name: str = typer.Argument(..., help="Имя тега"),
    description: str | None = typer.Option(None, "--description", help="Описание"),
    parent_id: str | None = typer.Option(
        None, "--parent", help="ID родителя (без него — корневой тег)"
    ),
    icon: str | None = typer.Option(None, "--icon", help="Имя иконки"),
    icon_color: str | None = typer.Option(None, "--icon-color", help="Цвет иконки"),
) -> None:
    """Создать тег (POST /tags). Право tag.create/tag.manage."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.create_tag(
                name=name,
                description=description,
                icon=icon,
                icon_color=icon_color,
                parent_id=parent_id,
            )
        finally:
            await client.close()

        emit_data(
            resp,
            text_renderer=lambda t: console.print(
                f"[green]✓[/] Тег создан: {t.get('name')} (id={t.get('id')})"
            ),
        )

    _common.run(_do())


def cmd_tag_edit(
    tag_id: str = typer.Argument(..., help="ID тега"),
    name: str | None = typer.Option(None, "--name", help="Новое имя"),
    description: str | None = typer.Option(None, "--description", help="Описание"),
    icon: str | None = typer.Option(None, "--icon", help="Имя иконки"),
    icon_color: str | None = typer.Option(None, "--icon-color", help="Цвет иконки"),
) -> None:
    """Обновить тег (PATCH /tags/{id}).

    Родителя эта команда НЕ меняет — для этого ``tag move`` (у backend это
    отдельная ручка с проверкой циклов и глубины).
    """
    if name is None and description is None and icon is None and icon_color is None:
        console.print(
            "[yellow]Нечего менять:[/] задайте хотя бы один из "
            "--name/--description/--icon/--icon-color"
        )
        raise typer.Exit(1)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.update_tag(
                tag_id,
                name=name,
                description=description,
                icon=icon,
                icon_color=icon_color,
            )
        finally:
            await client.close()

        emit_data(
            resp,
            text_renderer=lambda t: console.print(
                f"[green]✓[/] Тег обновлён: {t.get('name')} (id={t.get('id')})"
            ),
        )

    _common.run(_do())


def cmd_tag_move(
    tag_id: str = typer.Argument(..., help="ID тега"),
    parent_id: str | None = typer.Option(
        None, "--parent", help="ID нового родителя"
    ),
    to_root: bool = typer.Option(
        False, "--root", help="Перенести в корень (взаимоисключимо с --parent)"
    ),
) -> None:
    """Сменить родителя тега (PATCH /tags/{id}/move).

    ``--root`` и ``--parent`` — разные намерения, и молчаливое «ничего не
    задано = в корень» слишком легко срабатывает по ошибке, поэтому одно из
    двух требуется явно.
    """
    if to_root and parent_id is not None:
        console.print("[red]--root и --parent взаимоисключимы[/]")
        raise typer.Exit(1)
    if not to_root and parent_id is None:
        console.print("[red]Укажите --parent <id> или --root[/]")
        raise typer.Exit(1)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.move_tag(tag_id, new_parent_id=parent_id)
        finally:
            await client.close()

        emit_data(
            resp,
            text_renderer=lambda t: console.print(
                f"[green]✓[/] Тег {t.get('name')} перемещён "
                f"(parent_id={t.get('parent_id') or 'корень'})"
            ),
        )

    _common.run(_do())


def cmd_tag_delete(
    tag_id: str = typer.Argument(..., help="ID тега"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Без подтверждения"),
) -> None:
    """Удалить тег (DELETE /tags/{id})."""
    if not yes:
        typer.confirm(f"Удалить тег {tag_id}?", abort=True)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            await client.delete_tag(tag_id)
        finally:
            await client.close()

        emit_data(
            {"deleted": True, "tag_id": tag_id},
            text_renderer=lambda _p: console.print(
                f"[green]✓[/] Тег {tag_id} удалён"
            ),
        )

    _common.run(_do())


def cmd_tag_bulk_delete(
    ids: str = typer.Option(..., "--ids", help="ID тегов через запятую"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Без подтверждения"),
) -> None:
    """Удалить несколько тегов (POST /tags/bulk/delete).

    Только по ЯВНОМУ списку id: удаление «по фильтру» backend тоже умеет, но
    из терминала это слишком лёгкий способ снести половину каталога.
    """
    tag_ids = _split_csv(ids)
    if not tag_ids:
        console.print("[red]--ids пуст[/]")
        raise typer.Exit(1)
    if not yes:
        typer.confirm(f"Удалить тегов: {len(tag_ids)}?", abort=True)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.bulk_delete_tags(ids=tag_ids)
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            console.print(
                f"[green]✓[/] Обработано: {payload.get('processed', 0)}, "
                f"удалено: {payload.get('updated', 0)}"
            )
            for err in payload.get("errors") or []:
                console.print(
                    f"  [yellow]![/] id={err.get('id')}: {err.get('reason')}"
                )

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_tag_bulk_move(
    ids: str = typer.Option(..., "--ids", help="ID тегов через запятую"),
    parent_id: str | None = typer.Option(None, "--parent", help="ID нового родителя"),
    to_root: bool = typer.Option(False, "--root", help="Перенести в корень"),
) -> None:
    """Перевесить несколько тегов под нового родителя (POST /tags/bulk/move)."""
    if to_root and parent_id is not None:
        console.print("[red]--root и --parent взаимоисключимы[/]")
        raise typer.Exit(1)
    if not to_root and parent_id is None:
        console.print("[red]Укажите --parent <id> или --root[/]")
        raise typer.Exit(1)
    tag_ids = _split_csv(ids)
    if not tag_ids:
        console.print("[red]--ids пуст[/]")
        raise typer.Exit(1)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.bulk_move_tags(
                ids=tag_ids, new_parent_id=parent_id
            )
        finally:
            await client.close()

        emit_data(
            resp,
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Обработано: {p.get('processed', 0)}, "
                f"перемещено: {p.get('updated', 0)}"
            ),
        )

    _common.run(_do())


def cmd_tag_assignments(
    entity_type: str = typer.Option(
        ..., "--type", help=f"Тип сущности: {' | '.join(ENTITY_TYPES)}"
    ),
    entity_id: str = typer.Option(..., "--id", help="ID сущности"),
) -> None:
    """Теги, навешенные на сущность (GET /tags/assignments)."""
    if entity_type not in ENTITY_TYPES:
        console.print(f"[red]--type должен быть одним из: {', '.join(ENTITY_TYPES)}[/]")
        raise typer.Exit(1)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_entity_tags(
                entity_type=entity_type, entity_id=entity_id
            )
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            tags = payload.get("tags") or []
            if not tags:
                console.print("[yellow]Тегов не назначено[/]")
                return
            names = ", ".join(
                f"{t.get('name')} (id={t.get('id')})" for t in tags
            )
            console.print(f"{entity_type} {entity_id}: {names}")

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_tag_assign(
    entity_type: str = typer.Option(
        ..., "--type", help=f"Тип сущности: {' | '.join(ENTITY_TYPES)}"
    ),
    entity_id: str = typer.Option(..., "--id", help="ID сущности"),
    tags: str = typer.Option(
        ...,
        "--tags",
        help="ID тегов через запятую. ПОЛНАЯ ЗАМЕНА набора; пусто — снять все",
    ),
) -> None:
    """Установить теги сущности (PUT /tags/assignments).

    Это replace-set, а не добавление: переданный список ПОЛНОСТЬЮ заменяет
    текущий. ``--tags ""`` снимает все теги.
    """
    if entity_type not in ENTITY_TYPES:
        console.print(f"[red]--type должен быть одним из: {', '.join(ENTITY_TYPES)}[/]")
        raise typer.Exit(1)
    tag_ids = _split_csv(tags)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.set_entity_tags(
                entity_type=entity_type, entity_id=entity_id, tag_ids=tag_ids
            )
        finally:
            await client.close()

        emit_data(
            resp,
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Теги обновлены ({len(p.get('tags') or [])} шт.)"
            ),
        )

    _common.run(_do())


def register(app: typer.Typer, *, can_manage: bool = False) -> None:
    """Регистрирует группу ``tag``.

    Чтение доступно любому залогиненному, поэтому list/tree/count/show/
    assignments регистрируются всегда. Мутации прячем за ``can_manage``
    (``tag.create``/``tag.manage``/``hub.admin``) — как и остальные
    admin-команды CLI: гейт лишь убирает их из справки, финальное «нельзя»
    всё равно приходит от backend.
    """
    tag_app = typer.Typer(
        no_args_is_help=True,
        help="Теги: каталог, иерархия, назначение на сущности.",
    )
    tag_app.command("list")(cmd_tag_list)
    tag_app.command("tree")(cmd_tag_tree)
    tag_app.command("count")(cmd_tag_count)
    tag_app.command("show")(cmd_tag_show)
    tag_app.command("assignments")(cmd_tag_assignments)
    if can_manage:
        tag_app.command("create")(cmd_tag_create)
        tag_app.command("edit")(cmd_tag_edit)
        tag_app.command("move")(cmd_tag_move)
        tag_app.command("delete")(cmd_tag_delete)
        tag_app.command("bulk-delete")(cmd_tag_bulk_delete)
        tag_app.command("bulk-move")(cmd_tag_bulk_move)
        tag_app.command("assign")(cmd_tag_assign)
    app.add_typer(tag_app, name="tag")
