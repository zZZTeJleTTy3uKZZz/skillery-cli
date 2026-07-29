"""Дополнительные глаголы группы ``skill`` (#1224).

Матрица функционала числила за CLI правку/удаление навыка, регистрацию
версии, обвязку репозитория (credential, tree/file/readme), статус
sync-джобы, ``/skills/{slug}/collections`` и ``/me/skills`` — ни одного
обращения к ним в коде не было. Здесь они появляются.

Команды регистрируются прямо в sub-app ``skill``; ``_grouping`` доливает в ту
же группу исторические плоские глаголы (``install``, ``list``, …). Плоских
алиасов у НОВЫХ команд нет и быть не должно — обратную совместимость ломать
нечему, а плоское пространство имён мы как раз расселяем.

Вложенные под-группы (``skill repo …``, ``skill credential …``,
``skill version …``) — по образцу ``company invite-links …``: у ресурса есть
свои под-ресурсы, и плоские ``repo-tree``/``credential-set`` были бы тем же
слипшимся неймингом, от которого уходили в #1223.
"""
from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data

console = Console()

#: Поля карточки навыка, которые правит ``skill edit`` (зеркало
#: ``UpdateSkillRequest``). Значение ``None`` = «не трогать».
_EDITABLE = (
    "title",
    "description",
    "short_description",
    "category",
    "license",
    "channel",
    "access_level",
    "kind",
    "repo_url",
    "skill_path",
)


def cmd_skill_edit(
    slug: str = typer.Argument(..., help="Slug навыка"),
    title: str | None = typer.Option(None, "--title", help="Заголовок"),
    description: str | None = typer.Option(None, "--description", help="Описание"),
    short_description: str | None = typer.Option(
        None, "--short-description", help="Краткое описание (до 140 символов)"
    ),
    category: str | None = typer.Option(
        None,
        "--category",
        help=(
            "crm | automation | marketing | analytics | devops | hr | "
            "finance | sales | productivity | other"
        ),
    ),
    license_: str | None = typer.Option(None, "--license", help="Лицензия"),
    channel: str | None = typer.Option(None, "--channel", help="published | staging"),
    access_level: str | None = typer.Option(
        None, "--access-level", help="public | restricted | private"
    ),
    kind: str | None = typer.Option(
        None, "--kind", help="prompt | comprehensive | tooling"
    ),
    repo_url: str | None = typer.Option(None, "--repo-url", help="URL репозитория"),
    skill_path: str | None = typer.Option(
        None, "--skill-path", help="Путь навыка внутри монорепозитория"
    ),
    tags: str | None = typer.Option(
        None,
        "--tags",
        help="Теги через запятую (ПОЛНАЯ замена набора; пусто — снять все)",
    ),
    visible: bool | None = typer.Option(
        None, "--visible/--hidden", help="Видимость в каталоге"
    ),
) -> None:
    """Изменить карточку навыка (PATCH /skills/{slug}).

    Отправляем только явно заданные опции: у backend отсутствие поля значит
    «не менять», и слать весь набор со значениями по умолчанию значило бы
    затирать чужие правки.
    """
    values = {
        "title": title,
        "description": description,
        "short_description": short_description,
        "category": category,
        "license": license_,
        "channel": channel,
        "access_level": access_level,
        "kind": kind,
        "repo_url": repo_url,
        "skill_path": skill_path,
    }
    payload: dict[str, Any] = {
        key: values[key] for key in _EDITABLE if values.get(key) is not None
    }
    if tags is not None:
        payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if visible is not None:
        payload["visible"] = visible
    if not payload:
        console.print(
            "[yellow]Нечего менять:[/] задайте хотя бы одну опцию "
            "(см. `skillery skill edit --help`)"
        )
        raise typer.Exit(1)

    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.update_skill(slug, payload)
        finally:
            await client.close()

        emit_data(
            resp,
            text_renderer=lambda s: console.print(
                f"[green]✓[/] Навык обновлён: {s.get('slug') or slug} "
                f"({', '.join(payload)})"
            ),
        )

    _common.run(_do())


def cmd_skill_delete(
    id_or_slug: str = typer.Argument(..., help="Slug или id навыка"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Без подтверждения"),
) -> None:
    """Удалить навык в хабе (DELETE /skills/{id_or_slug}).

    Это удаление В ХАБЕ (soft-delete, архив), а НЕ снятие навыка с этой
    машины — для локального снятия есть ``skillery skill remove``.
    """
    if not yes:
        typer.confirm(
            f"Удалить навык {id_or_slug} в хабе? "
            "(локальную установку это не трогает)",
            abort=True,
        )
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            await client.delete_skill(id_or_slug)
        finally:
            await client.close()

        emit_data(
            {"deleted": True, "skill": id_or_slug},
            text_renderer=lambda _p: console.print(
                f"[green]✓[/] Навык {id_or_slug} удалён в хабе"
            ),
        )

    _common.run(_do())


def cmd_skill_mine(
    channel: str = typer.Option("published", "--channel", help="published | staging"),
    page: int = typer.Option(1, "--page", help="Страница (с 1)"),
    size: int = typer.Option(50, "--size", help="Размер страницы (макс. 200)"),
) -> None:
    """Мои навыки — те, где я автор (GET /me/skills).

    Это не то же, что ``skillery skill installed`` (установленное на машине)
    и не ``/me/installs`` (установленное мне) — здесь именно авторство.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.list_my_skills(
                channel=channel, page=page, size=size
            )
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            items = payload.get("items") or []
            if not items:
                console.print("[yellow]Своих навыков нет[/]")
                return
            table = Table(title=f"Мои навыки (всего: {payload.get('total', len(items))})")
            table.add_column("slug")
            table.add_column("название", overflow="fold")
            table.add_column("доступ")
            table.add_column("канал")
            for item in items:
                table.add_row(
                    str(item.get("slug") or "—"),
                    str(item.get("title") or "—"),
                    str(item.get("access_level") or "—"),
                    str(item.get("channel") or "—"),
                )
            console.print(table)

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_skill_usage(
    days: int = typer.Option(30, "--days", help="Окно в днях (1..365)"),
    limit: int = typer.Option(5, "--limit", help="Сколько навыков (1..20)"),
) -> None:
    """Статистика моих навыков (GET /me/skills/usage).

    ``count`` — установки по РАЗЛИЧНЫМ устройствам, а не сырые события: одна
    машина, переустановившая навык десять раз, даёт единицу.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_my_skills_usage(days=days, limit=limit)
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            items = payload.get("items") or []
            console.print(
                f"Активаций за {payload.get('range_days', days)} дн.: "
                f"[bold]{payload.get('activations_total', 0)}[/]"
            )
            if not items:
                console.print("[yellow]Данных по навыкам нет[/]")
                return
            table = Table(title="Топ навыков")
            table.add_column("slug")
            table.add_column("название", overflow="fold")
            table.add_column("устройств")
            for item in items:
                table.add_row(
                    str(item.get("slug") or "—"),
                    str(item.get("title") or "—"),
                    str(item.get("count", 0)),
                )
            console.print(table)

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_skill_collections(
    slug: str = typer.Argument(..., help="Slug навыка"),
) -> None:
    """В каких коллекциях лежит навык (GET /skills/{slug}/collections)."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.list_skill_collections(slug)
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            items = payload.get("items") or []
            if not items:
                console.print("[yellow]Навык не входит ни в одну коллекцию[/]")
                return
            table = Table(title=f"Коллекции навыка {slug} (всего: {len(items)})")
            table.add_column("slug")
            table.add_column("название", overflow="fold")
            table.add_column("доступ")
            for item in items:
                table.add_row(
                    str(item.get("slug") or "—"),
                    str(item.get("title") or "—"),
                    str(item.get("access_level") or "—"),
                )
            console.print(table)

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_skill_analytics(
    slug: str = typer.Argument(..., help="Slug навыка"),
    date_from: str | None = typer.Option(
        None, "--from", help="Начало периода (ISO 8601)"
    ),
    date_to: str | None = typer.Option(
        None, "--to", help="Конец периода (ISO 8601)"
    ),
) -> None:
    """Аналитика навыка (GET /skills/{slug}/analytics).

    ``top_companies`` придёт пустым, если прав не хватает: backend режет
    именно это поле, а не весь ответ — пустой список тут не значит «нет
    данных».
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_skill_analytics(
                slug, date_from=date_from, date_to=date_to
            )
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            console.print(f"[bold]Аналитика {payload.get('skill_slug') or slug}[/]")
            console.print(f"  Источник: {payload.get('source') or '—'}")
            stats = payload.get("run_stats") or {}
            console.print(
                f"  Запусков: {stats.get('total', 0)}, "
                f"средн. {stats.get('avg_ms', 0)} мс, "
                f"ошибок {stats.get('error_rate', 0)}"
            )
            installs = payload.get("installs_timeseries") or []
            console.print(f"  Точек по установкам: {len(installs)}")
            for row in payload.get("active_users") or []:
                console.print(
                    f"  {row.get('window') or row.get('kind') or '—'}: "
                    f"{row.get('count', 0)}"
                )

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_skill_star(
    id_or_slug: str = typer.Argument(..., help="Slug или id навыка"),
) -> None:
    """Поставить/снять звезду навыку (POST /skills/{id}/star).

    Это ПЕРЕКЛЮЧАТЕЛЬ: если звезда уже стоит, повторный вызов её снимет.
    Оценка «1-5» — другая история, она в ``skillery rating set``.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            skill_id = await _common.resolve_skill_id(client, id_or_slug)
            resp = await client.star_skill(skill_id)
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            mark = "поставлена" if payload.get("is_starred") else "снята"
            console.print(
                f"[green]✓[/] Звезда {mark}. Всего звёзд: "
                f"{payload.get('total_star_count', 0)} "
                f"(хаб: {payload.get('hub_star_count', 0)}, "
                f"репозиторий: {payload.get('repo_star_count', 0)})"
            )

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_skill_sync_status(
    slug: str = typer.Argument(..., help="Slug навыка"),
    job_id: str = typer.Argument(..., help="ID джобы (вернула `skill sync`)"),
) -> None:
    """Статус джобы синхронизации с Git (GET /skills/{slug}/sync-jobs/{id})."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_sync_job(slug, job_id)
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            status = payload.get("status") or "—"
            color = {"done": "green", "error": "red"}.get(status, "yellow")
            console.print(f"Джоба {payload.get('job_id')}: [{color}]{status}[/]")
            if payload.get("error"):
                console.print(f"  Ошибка: {payload['error']}")

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_skill_version_add(
    slug: str = typer.Argument(..., help="Slug навыка"),
    semver: str = typer.Option(..., "--semver", help="Версия, напр. 1.2.3"),
    commit_sha: str = typer.Option(..., "--commit", help="SHA коммита"),
    manifest: typer.FileText = typer.Option(
        ..., "--manifest", help="Путь к JSON-файлу манифеста навыка"
    ),
    channel: str = typer.Option("published", "--channel", help="published | staging"),
    tags: str | None = typer.Option(None, "--tags", help="Теги через запятую"),
) -> None:
    """Зарегистрировать версию навыка (POST /skills/{slug}/versions).

    Тело — JSON, файлы навыка тут НЕ загружаются: контент приезжает в хаб
    через git-sync/вебхук, а эта ручка фиксирует semver + коммит + манифест.
    Право ``skill.publish``.
    """
    import json

    try:
        manifest_data = json.load(manifest)
    except (ValueError, OSError) as exc:
        console.print(f"[red]Не читается манифест:[/] {exc}")
        raise typer.Exit(1) from exc
    if not isinstance(manifest_data, dict):
        console.print("[red]Манифест должен быть JSON-объектом[/]")
        raise typer.Exit(1)

    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.publish_skill_version(
                slug,
                semver=semver,
                commit_sha=commit_sha,
                manifest=manifest_data,
                channel=channel,
                tags=[t.strip() for t in (tags or "").split(",") if t.strip()],
            )
        finally:
            await client.close()

        emit_data(
            resp,
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Версия {semver} зарегистрирована "
                f"(skill_id={p.get('skill_id')}, version_id={p.get('version_id')})"
            ),
        )

    _common.run(_do())


# --- skill repo … ---
def cmd_repo_tree(
    slug: str = typer.Argument(..., help="Slug навыка"),
    ref: str | None = typer.Option(None, "--ref", help="Ветка/тег/SHA"),
) -> None:
    """Файлы репозитория навыка (GET /skills/{slug}/repo/tree)."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_repo_tree(slug, ref=ref)
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            entries = payload.get("entries") or []
            console.print(f"[dim]ref: {payload.get('ref') or '—'}[/]")
            if not entries:
                console.print("[yellow]Файлов не найдено[/]")
                return
            for entry in entries:
                marker = "/" if entry.get("type") == "tree" else ""
                size = entry.get("size")
                suffix = f"  [dim]{size} Б[/]" if size is not None else ""
                console.print(f"  {entry.get('path')}{marker}{suffix}")

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_repo_file(
    slug: str = typer.Argument(..., help="Slug навыка"),
    path: str = typer.Option(..., "--path", help="Путь файла в репозитории"),
    ref: str | None = typer.Option(None, "--ref", help="Ветка/тег/SHA"),
) -> None:
    """Содержимое файла из репозитория (GET /skills/{slug}/repo/file).

    У бинарных файлов backend отдаёт ``content=null`` — печатать нечего,
    сообщаем об этом явно, а не роняем вывод сырыми байтами в терминал.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_repo_file(slug, path=path, ref=ref)
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            if payload.get("encoding") == "binary" or payload.get("content") is None:
                console.print(
                    f"[yellow]Бинарный файл[/] {payload.get('path')} "
                    f"({payload.get('size', 0)} Б) — содержимое не выводится"
                )
                return
            console.print(
                Syntax(
                    payload.get("content") or "",
                    "text",
                    line_numbers=False,
                    word_wrap=True,
                )
            )
            if payload.get("truncated"):
                console.print("[yellow]…содержимое обрезано backend'ом[/]")

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_repo_readme(
    slug: str = typer.Argument(..., help="Slug навыка"),
    ref: str | None = typer.Option(None, "--ref", help="Ветка/тег/SHA"),
) -> None:
    """README навыка из репозитория (GET /skills/{slug}/repo/readme)."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_repo_readme(slug, ref=ref)
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            console.print(f"[dim]{payload.get('path')} @ {payload.get('ref') or '—'}[/]")
            console.print(payload.get("content") or "")

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


# --- skill credential … ---
def cmd_credential_show(
    slug: str = typer.Argument(..., help="Slug навыка"),
) -> None:
    """Есть ли git-credential у навыка (GET /skills/{slug}/repo-credential).

    Значение секрета backend не возвращает НИКОГДА — только факт наличия,
    провайдер и тип.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.get_repo_credential(slug)
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            if not payload.get("has_credential"):
                console.print("[yellow]Credential не задан[/]")
                return
            console.print(
                f"[green]✓[/] Credential есть: "
                f"{payload.get('provider')} / {payload.get('secret_type')} "
                f"(с {payload.get('created_at') or '—'})"
            )

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_credential_set(
    slug: str = typer.Argument(..., help="Slug навыка"),
    provider: str = typer.Option(..., "--provider", help="github | gitlab"),
    secret_type: str = typer.Option(
        "token", "--type", help="token | deploy_key"
    ),
    secret: str = typer.Option(
        None,
        "--secret",
        prompt="Секрет (токен или deploy-key)",
        hide_input=True,
        help=(
            "Значение секрета. По умолчанию спрашивается интерактивно со "
            "скрытым вводом — чтобы не оседало в истории оболочки"
        ),
    ),
) -> None:
    """Положить git-credential навыка (PUT /skills/{slug}/repo-credential).

    Секрет нигде не логируется и не печатается обратно: ввод скрытый, в
    ответе backend его тоже нет.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.set_repo_credential(
                slug,
                provider=provider,
                secret_type=secret_type,
                secret=secret,
            )
        finally:
            await client.close()

        emit_data(
            resp,
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Credential сохранён: "
                f"{p.get('provider')} / {p.get('secret_type')}"
            ),
        )

    _common.run(_do())


def cmd_credential_delete(
    slug: str = typer.Argument(..., help="Slug навыка"),
) -> None:
    """Забыть git-credential навыка (DELETE /skills/{slug}/repo-credential)."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            await client.delete_repo_credential(slug)
        finally:
            await client.close()

        emit_data(
            {"deleted": True, "slug": slug},
            text_renderer=lambda _p: console.print(
                f"[green]✓[/] Credential навыка {slug} удалён"
            ),
        )

    _common.run(_do())


def register(app: typer.Typer, *, can_publish: bool = False) -> None:
    """Добавляет новые глаголы в группу ``skill``.

    Группу берём уже существующую, если она есть: ``_grouping`` доливает в
    неё исторические плоские команды и переиспользует sub-typer с тем же
    именем — так что порядок регистрации не важен.

    Гейт ``can_publish`` (``skill.publish``/``skill.manage``/``hub.admin``)
    закрывает только мутации и приватную обвязку репозитория; чтение
    (``mine``/``usage``/``collections``/``star``) доступно любому вошедшему.
    """
    existing = {
        group.name: group.typer_instance for group in app.registered_groups
    }
    skill_app = existing.get("skill")
    if skill_app is None:
        skill_app = typer.Typer(
            no_args_is_help=True,
            help="Навыки: поиск, установка, включение, обновление, публикация.",
        )
        app.add_typer(skill_app, name="skill")

    skill_app.command("mine")(cmd_skill_mine)
    skill_app.command("usage")(cmd_skill_usage)
    skill_app.command("collections")(cmd_skill_collections)
    skill_app.command("star")(cmd_skill_star)
    skill_app.command("analytics")(cmd_skill_analytics)

    if not can_publish:
        return

    skill_app.command("edit")(cmd_skill_edit)
    skill_app.command("delete")(cmd_skill_delete)
    skill_app.command("sync-status")(cmd_skill_sync_status)

    version_app = typer.Typer(no_args_is_help=True, help="Версии навыка.")
    version_app.command("add")(cmd_skill_version_add)
    skill_app.add_typer(version_app, name="version")

    repo_app = typer.Typer(
        no_args_is_help=True, help="Репозиторий навыка: файлы и README."
    )
    repo_app.command("tree")(cmd_repo_tree)
    repo_app.command("file")(cmd_repo_file)
    repo_app.command("readme")(cmd_repo_readme)
    skill_app.add_typer(repo_app, name="repo")

    cred_app = typer.Typer(
        no_args_is_help=True, help="Git-credential навыка (секрет не отображается)."
    )
    cred_app.command("show")(cmd_credential_show)
    cred_app.command("set")(cmd_credential_set)
    cred_app.command("delete")(cmd_credential_delete)
    skill_app.add_typer(cred_app, name="credential")
