"""``skills-hub register`` / ``skills-hub join`` — самостоятельный онбординг (P1 C1, E6).

- ``register --email --password [--name]`` — POST /auth/register: регистрация
  без инвайта (юзер БЕЗ компании, доступ к публичным навыкам) → локальная
  сессия сохраняется как у password-login.
- ``join <token-или-URL>`` — вступление в компанию по переиспользуемой
  пригласительной ссылке (web строит её как ``{origin}/join/{token}``):

  * залогинен → POST /invite-links/accept (добавляет membership);
  * не залогинен → нужны ``--email``/``--password`` (+опц. ``--name``) →
    POST /auth/register-with-link → юзер + membership + сессия.

Обе команды always-on (регистрируются в build_app до login-гейта).
Контракты сверены с routes/auth.py (137, 188) и
routes/company_invite_links.py (207) — детали в docstring'ах transport.
"""
from __future__ import annotations

import asyncio
import re
import sys
from typing import Any, Optional

import typer
from rich.console import Console
from rich.prompt import Prompt

from skills_hub_cli.commands import _common
from skills_hub_cli.config import ClientConfig, populate_from_jwt, save_tokens
from skills_hub_cli.core.transport import ApiError
from skills_hub_cli.output import emit_data, emit_error, is_json

console = Console()

_MIN_PASSWORD_LEN = 8  # = schemas.RegisterRequest.password min_length на бэке


def _run(coro) -> None:  # noqa: ANN001
    """Локальный json-aware запуск корутины (канон ``__main__._run``).

    Не импортируем ``__main__`` (риск двойной загрузки модуля при
    ``python -m``), а ``_common.run`` не json-aware — поэтому короткая
    копия канона: ApiError → emit_error в json / читабельно в text,
    exit 1; typer.Exit/Abort пролетают насквозь.
    """
    try:
        asyncio.run(coro)
    except ApiError as e:
        if is_json():
            emit_error(e.code or "API", e.message or str(e), status_code=e.status_code)
        else:
            console.print(f"[red]Ошибка API:[/] {e}")
        sys.exit(1)


def _strip_join_url(value: str) -> str:
    """Вычленяет токен из join-URL (``…/join/<token>`` → ``<token>``).

    Web формирует ссылку как ``{origin}/join/{token}``
    (web/src/widgets/company-invite-links) — берём последний path-сегмент.
    Голый токен возвращается как есть.
    """
    cleaned = value.strip()
    m = re.match(r".*/join/([A-Za-z0-9_\-]+)/?$", cleaned)
    if m:
        return m.group(1)
    return cleaned


def _default_display_name(email: str) -> str:
    """display_name обязателен на бэке (min_length=1) — дефолт из local-part."""
    local = email.split("@", 1)[0].strip()
    return local or email


def _require_email_password(
    email: Optional[str], password: Optional[str], *, context: str
) -> tuple[str, str]:
    """Доводит email+password до значений: json-режим требует флаги (без
    интерактива), text-режим спрашивает интерактивно. Пароль ≥ 8 символов."""
    if email is None:
        if is_json():
            emit_error("VALIDATION", f"В json-режиме --email обязателен для {context}")
            raise typer.Exit(1)
        email = Prompt.ask("Email")
    if password is None:
        if is_json():
            emit_error("VALIDATION", f"В json-режиме --password обязателен для {context}")
            raise typer.Exit(1)
        password = typer.prompt(
            f"Пароль (мин. {_MIN_PASSWORD_LEN} символов)", hide_input=True
        )
    if len(password) < _MIN_PASSWORD_LEN:
        emit_error(
            "VALIDATION",
            f"Пароль должен быть не короче {_MIN_PASSWORD_LEN} символов",
        )
        raise typer.Exit(1)
    return email, password


def _save_session(cfg: ClientConfig, email: str, data: dict[str, Any]) -> None:
    """Сохраняет сессию после register/register-with-link (= _do_password_login):
    токены в keyring + permissions/company_id/role_id из JWT + config.toml."""
    save_tokens(email, data["access_token"], data["refresh_token"])
    cfg.user_email = email
    populate_from_jwt(cfg, data["access_token"])
    cfg.save()


def _session_payload(
    cfg: ClientConfig, *, event: str, **extra: Any
) -> dict[str, Any]:
    return {
        "event": event,
        "user_email": cfg.user_email,
        "is_hub_admin": cfg.is_hub_admin(),
        "is_skill_creator": cfg.is_skill_creator(),
        "permissions": cfg.permissions,
        "company_id": cfg.company_id,
        "role_id": cfg.role_id,
        "access_expires_at": cfg.access_expires_at,
        **extra,
    }


def _render_session(cfg: ClientConfig, headline: str) -> None:
    console.print(f"[green]✓[/] {headline}")
    roles_descr = []
    if cfg.is_hub_admin():
        roles_descr.append("hub-admin")
    if cfg.is_skill_creator():
        roles_descr.append("skill-creator")
    if cfg.permissions and not roles_descr:
        roles_descr.append("member")
    console.print(f"  Роли:        {', '.join(roles_descr) or '—'}")
    console.print(f"  Permissions: {len(cfg.permissions)} прав")
    if cfg.company_id:
        console.print(f"  Компания:    {cfg.company_id}")
    console.print(
        "[dim]Доступные команды зависят от прав — `skillery --help`[/]"
    )


def cmd_register(
    email: Optional[str] = typer.Option(None, "--email"),
    password: Optional[str] = typer.Option(
        None, "--password", help=f"Минимум {_MIN_PASSWORD_LEN} символов"
    ),
    name: Optional[str] = typer.Option(
        None, "--name", help="Display-name (default: часть email до @)"
    ),
    base_url: Optional[str] = typer.Option(None),
) -> None:
    """Самостоятельная регистрация (без инвайта): email + password → сессия.

    Создаёт юзера БЕЗ компании (доступ к публичным навыкам + CLI). Позже
    можно вступить в компанию: ``skills-hub join <ссылка>``.
    """
    cfg = ClientConfig.load()
    if base_url:
        cfg.base_url = base_url
    email, password = _require_email_password(email, password, context="register")
    display_name = name or _default_display_name(email)

    async def _do() -> None:
        client = _common.HubClient(base_url=cfg.base_url)
        try:
            data = await client.register(
                email=email, password=password, display_name=display_name
            )
            # JWT-slim: права — из /me/permissions (токен их не несёт).
            await _common.hydrate_session_permissions(
                client, cfg, data["access_token"]
            )
        finally:
            await client.close()
        _save_session(cfg, email, data)
        result = _session_payload(
            cfg,
            event="registered",
            method="register",
            user_id=data.get("user_id"),
        )
        emit_data(
            result,
            text_renderer=lambda _: _render_session(
                cfg, f"Зарегистрирован и авторизован: {email}"
            ),
        )

    _run(_do())


def cmd_join(
    token_or_url: str = typer.Argument(
        ...,
        metavar="TOKEN_ИЛИ_URL",
        help="Пригласительная ссылка компании (…/join/<token>) или сам токен",
    ),
    email: Optional[str] = typer.Option(
        None, "--email", help="Для регистрации по ссылке (если не залогинены)"
    ),
    password: Optional[str] = typer.Option(
        None, "--password", help=f"Минимум {_MIN_PASSWORD_LEN} символов (если не залогинены)"
    ),
    name: Optional[str] = typer.Option(
        None, "--name", help="Display-name (default: часть email до @)"
    ),
    base_url: Optional[str] = typer.Option(None),
) -> None:
    """Вступить в компанию по пригласительной ссылке (``…/join/<token>``).

    Залогинены → POST /invite-links/accept (добавляет membership; ``--email``
    игнорируется). Не залогинены → регистрация по ссылке
    (POST /auth/register-with-link): нужны ``--email`` и ``--password``
    (+опц. ``--name``) → создаёт юзера + membership → локальная сессия.
    """
    cfg = ClientConfig.load()
    if base_url:
        cfg.base_url = base_url
    token = _strip_join_url(token_or_url)

    if cfg.is_logged_in():
        access = _common.get_access_token()

        async def _do_accept() -> None:
            client = _common.make_client(cfg, access)
            try:
                await client.accept_invite_link(token=token)
            finally:
                await client.close()
            emit_data(
                {
                    "event": "joined",
                    "method": "accept",
                    "user_email": cfg.user_email,
                },
                text_renderer=lambda _: (
                    console.print(
                        f"[green]✓[/] Вступили в компанию по ссылке ({cfg.user_email})"
                    ),
                    console.print(
                        "[dim]Переключиться на компанию: skillery company switch "
                        "(или POST /me/active-company)[/]"
                    ),
                ),
            )

        _run(_do_accept())
        return

    # --- не залогинен → регистрация по ссылке ---
    email, password = _require_email_password(
        email, password, context="join (вы не залогинены)"
    )
    display_name = name or _default_display_name(email)

    async def _do_register() -> None:
        client = _common.HubClient(base_url=cfg.base_url)
        try:
            data = await client.register_with_link(
                email=email,
                password=password,
                display_name=display_name,
                token=token,
            )
            # JWT-slim: права — из /me/permissions (токен их не несёт).
            await _common.hydrate_session_permissions(
                client, cfg, data["access_token"]
            )
        finally:
            await client.close()
        _save_session(cfg, email, data)
        result = _session_payload(
            cfg,
            event="joined",
            method="register_with_link",
            user_id=data.get("user_id"),
        )
        emit_data(
            result,
            text_renderer=lambda _: _render_session(
                cfg, f"Зарегистрирован по ссылке и авторизован: {email}"
            ),
        )

    _run(_do_register())


def register(app: typer.Typer) -> None:
    """Регистрирует always-on команды register/join (зовётся из ``build_app``)."""
    app.command(name="register")(cmd_register)
    app.command(name="join")(cmd_join)
