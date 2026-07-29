"""``skillery session …`` + ``skillery auth profile`` — сессии и профиль (#1224).

Матрица помечала ``/me/sessions`` (три ручки) и ``PATCH /me`` как «нет CLI» —
и это была правда. Для CLI сессии особенно уместны: «разлогинить все машины»
— типовая реакция на утечку, и ради неё не должно приходиться открывать веб.

Глаголы:

- ``session list`` — активные сессии (``GET /me/sessions``);
- ``session revoke <id>`` — закрыть одну (``DELETE /me/sessions/{id}``);
- ``session revoke-all`` — закрыть все (``DELETE /me/sessions``);
- ``auth profile`` — правка своего профиля (``PATCH /me``).

Про ``revoke-all``: backend оставляет живой ТЕКУЩУЮ сессию, определяя её по
refresh-куке. CLI ходит по Bearer и куку не шлёт, поэтому для него это
«закрыть вообще все», включая себя, — команда честно предупреждает об этом и
требует подтверждения.

Настройки пользователя (``/me/preferences``) сюда сознательно НЕ вынесены:
это тема/локаль веб-интерфейса, у CLI для них нет ни потребителя, ни смысла.
"""
from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data

console = Console()


def cmd_session_list(
    page: int = typer.Option(1, "--page", help="Страница (с 1)"),
    size: int = typer.Option(50, "--size", help="Размер страницы (макс. 200)"),
) -> None:
    """Активные сессии моего аккаунта (GET /me/sessions).

    Backend схлопывает несколько refresh-записей одного устройства в одну
    строку, поэтому ``total`` — про устройства, а не про токены.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.list_sessions(page=page, size=size)
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            items = payload.get("items") or []
            if not items:
                console.print("[yellow]Активных сессий нет[/]")
                return
            table = Table(title=f"Сессии (всего: {payload.get('total', len(items))})")
            table.add_column("id")
            table.add_column("устройство", overflow="fold")
            table.add_column("клиент")
            table.add_column("IP")
            table.add_column("создана")
            table.add_column("истекает")
            table.add_column("текущая")
            for item in items:
                table.add_row(
                    str(item.get("id") or "—"),
                    str(item.get("device_name") or item.get("user_agent") or "—"),
                    str(item.get("client_type") or "—"),
                    str(item.get("ip_address") or "—"),
                    str(item.get("created_at") or "—"),
                    str(item.get("expires_at") or "—"),
                    "да" if item.get("is_current") else "",
                )
            console.print(table)

        emit_data(resp, text_renderer=_render)

    _common.run(_do())


def cmd_session_revoke(
    session_id: str = typer.Argument(..., help="ID сессии (см. `session list`)"),
) -> None:
    """Закрыть одну сессию (DELETE /me/sessions/{id})."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            await client.revoke_session(session_id)
        finally:
            await client.close()

        emit_data(
            {"revoked": True, "session_id": session_id},
            text_renderer=lambda _p: console.print(
                f"[green]✓[/] Сессия {session_id} закрыта"
            ),
        )

    _common.run(_do())


def cmd_session_revoke_all(
    yes: bool = typer.Option(False, "--yes", "-y", help="Без подтверждения"),
) -> None:
    """Закрыть ВСЕ сессии, включая текущую (DELETE /me/sessions).

    Backend щадит «текущую» сессию только если пришла refresh-кука; CLI
    авторизуется Bearer-токеном и куку не отправляет, поэтому под нож идёт и
    эта машина — после команды понадобится повторный ``skillery auth login``.
    """
    if not yes:
        typer.confirm(
            "Закрыть все сессии? Текущая сессия CLI тоже завершится, "
            "потребуется повторный вход.",
            abort=True,
        )
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            await client.revoke_all_sessions()
        finally:
            await client.close()

        emit_data(
            {"revoked_all": True},
            text_renderer=lambda _p: console.print(
                "[green]✓[/] Все сессии закрыты. Выполните "
                "`skillery auth login` для повторного входа."
            ),
        )

    _common.run(_do())


def cmd_auth_profile(
    display_name: str | None = typer.Option(
        None, "--display-name", help="Отображаемое имя"
    ),
    first_name: str | None = typer.Option(None, "--first-name", help="Имя"),
    last_name: str | None = typer.Option(None, "--last-name", help="Фамилия"),
) -> None:
    """Изменить свой профиль (PATCH /me).

    Без опций ничего не делаем: пустой PATCH — это лишний сетевой вызов и
    непонятное «✓» в ответ. Смотреть профиль — ``skillery auth whoami``.
    """
    if display_name is None and first_name is None and last_name is None:
        console.print(
            "[yellow]Нечего менять:[/] задайте хотя бы одну из "
            "--display-name/--first-name/--last-name "
            "(посмотреть профиль — `skillery auth whoami`)"
        )
        raise typer.Exit(1)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            resp = await client.update_me(
                display_name=display_name,
                first_name=first_name,
                last_name=last_name,
            )
        finally:
            await client.close()

        def _render(payload: dict[str, Any]) -> None:
            user = payload.get("user") if isinstance(payload.get("user"), dict) else payload
            console.print(
                f"[green]✓[/] Профиль обновлён: "
                f"{user.get('display_name') or user.get('email') or '—'}"
            )

        emit_data(resp, text_renderer=_render)

        # Локальный кэш имени показывается в `whoami`/`cli status` — без
        # синхронизации он бы ещё сутки врал старым значением.
        try:
            user = resp.get("user") if isinstance(resp.get("user"), dict) else resp
            name = (user.get("display_name") or "").strip()
            if name:
                cfg.user_display_name = name
                cfg.save()
        except Exception:  # noqa: BLE001 — кэш не важнее самой правки
            pass

    _common.run(_do())


def register(app: typer.Typer) -> None:
    """Регистрирует группу ``session`` и добавляет ``profile`` в группу ``auth``.

    Группа ``auth`` собирается позже — в ``_grouping.apply_resource_groups``
    из плоских команд; она переиспользует уже существующий sub-typer с тем же
    именем, поэтому создать его здесь безопасно и порядок регистрации ролей
    не играет.
    """
    session_app = typer.Typer(
        no_args_is_help=True,
        help="Сессии аккаунта: список, закрыть одну, закрыть все.",
    )
    session_app.command("list")(cmd_session_list)
    session_app.command("revoke")(cmd_session_revoke)
    session_app.command("revoke-all")(cmd_session_revoke_all)
    app.add_typer(session_app, name="session")

    existing = {
        group.name: group.typer_instance for group in app.registered_groups
    }
    auth_app = existing.get("auth")
    if auth_app is None:
        auth_app = typer.Typer(
            no_args_is_help=True,
            help="Аккаунт и сессия: вход/выход, регистрация, вступление, пароль.",
        )
        app.add_typer(auth_app, name="auth")
    auth_app.command("profile")(cmd_auth_profile)
