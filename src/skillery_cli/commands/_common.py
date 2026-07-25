"""Общие helpers для команд.

Не дублируем код из ``__main__`` — реэкспортируем нужные функции
(``_run``, ``_get_access_token``, ``_make_refresh_callback``,
``HubClient``). Команды импортируют отсюда чтобы было одно место для
mock'ов в тестах: ``monkeypatch.setattr(_common, "HubClient", fake)``.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any

import typer
from rich.console import Console

from skillery_cli.config import ClientConfig, load_tokens, populate_from_jwt, save_tokens
from skillery_cli.core.transport import ApiError, HubClient
from skillery_cli.output import emit_error

console = Console()


def run(coro) -> None:  # noqa: ANN001
    """asyncio.run + канон ошибок (зеркало ``__main__._run``).

    В json-режиме ApiError/RuntimeError уходят единым событием
    ``{"event":"error",...}`` в stderr (stdout остаётся машинным каналом);
    в text-режиме — прежнее читабельное сообщение. ``typer.Exit``/``Abort``
    наследуют RuntimeError через click — пропускаем их наверх как есть.
    """
    from skillery_cli.output import is_json

    try:
        asyncio.run(coro)
    except ApiError as e:
        if is_json():
            emit_error(e.code or "API", e.message, status_code=e.status_code)
        else:
            console.print(f"[red]Ошибка API:[/] {e}")
        sys.exit(1)
    except (typer.Exit, typer.Abort):
        raise
    except RuntimeError as e:
        emit_error("RUNTIME", str(e))
        sys.exit(1)


async def hydrate_session_permissions(
    client: HubClient, cfg: ClientConfig, access_token: str
) -> None:
    """JWT-slim: проставить ``cfg.permissions`` из ``/me/permissions`` (БД-авторитетно).

    Access-токен развёрнутый список прав больше не несёт — после login флоу
    авторизует ТОТ ЖЕ открытый клиент выписанным токеном и забирает эффективные
    права (единый источник с backend-enforcement и web-UI). На ошибке сети/API
    права остаются пустыми: логин уже состоялся, команду не валим — пользователь
    увидит «0 прав» и сможет повторить.
    """
    client.set_access_token(access_token)
    try:
        cfg.permissions = await client.get_me_permissions()
    except ApiError:
        cfg.permissions = []
    # Имя/почту берём из /me: JWT несёт только числовой user_id (sub), поэтому
    # без этого в CLI пользователь отображался как «1», а не как имя. Best-effort:
    # ошибка не валит логин (права уже проставлены выше).
    try:
        me = await client.get_me()
        # /me отдаёт {user, claims, memberships} — профиль вложен в ``user``.
        u = me.get("user") if isinstance(me.get("user"), dict) else me
        name = (u.get("display_name") or "").strip()
        email = (u.get("email") or "").strip()
        if name:
            cfg.user_display_name = name
        if email:
            cfg.user_email = email
    except Exception:  # noqa: BLE001 — профиль не критичен для логина
        pass
    # #1024: действие «вход» видно в вебе /logs с привязкой к пользователю.
    try:
        await client.report_cli_log(
            level="info", message="CLI: вход выполнен", logger="cli.login",
            context={"device": (email or name or "")},
        )
    except Exception:  # noqa: BLE001 — телеметрия не ломает логин
        pass
    # C3 (#1099): login — первый момент со свежим токеном, поэтому здесь же
    # досылаем всё, что накопилось в офлайн-буфере (в т.ч. ошибки, случившиеся
    # у разлогиненного/офлайн CLI). Best-effort, с таймаутом.
    try:
        from skillery_cli.core.log_sync import flush_log_sync_safe

        await flush_log_sync_safe(client, force=True)
    except Exception:  # noqa: BLE001 — синк не ломает логин
        pass


def local_device_identity() -> tuple[str, str, str]:
    """(name, platform, client_device_id) текущей машины для регистрации.

    name = hostname (визуальный лейбл по умолчанию; пользователь может
    переименовать в вебе); platform = ОС в нижнем регистре; client_device_id —
    СТАБИЛЬНЫЙ id машины (``core.identity.device_uid`` — тот же, что в
    User-Agent), по нему backend апсертит устройство и матчит сессию, поэтому
    ренейм лейбла связь не рвёт."""
    import platform as _pf

    from skillery_cli.core.identity import device_uid
    from skillery_cli.core.transport import device_name

    name = device_name()
    plat = (_pf.system() or "unknown").strip().lower() or "unknown"
    return name, plat, device_uid()


async def register_device_best_effort(client: HubClient) -> None:
    """E-D web↔CLI-мост: зарегистрировать эту машину как устройство.

    Зовётся ПОСЛЕ успешного login на уже авторизованном клиенте (после
    ``hydrate_session_permissions``, который проставил токен). Любую
    ошибку — сеть, API, старый backend без ``/me/devices`` — глотаем:
    логин уже состоялся, регистрацию устройства не даём его завалить.
    """
    name, plat, cdid = local_device_identity()
    try:
        await client.register_device(name=name, platform=plat, client_device_id=cdid)
    except Exception:  # noqa: BLE001 — best-effort, login важнее
        pass


def get_access_token() -> str:
    """Получить access-token либо exit(1) с сообщением."""
    cfg = ClientConfig.load()
    if not cfg.user_email:
        emit_error("NOT_LOGGED_IN", "Сначала залогиньтесь: skillery login")
        raise typer.Exit(1)
    access, _ = load_tokens(cfg.user_email)
    if not access:
        emit_error("NO_TOKEN", "Локальный access-токен не найден. Сделайте login заново.")
        raise typer.Exit(1)
    return access


def make_refresh_callback(cfg: ClientConfig) -> object:
    """Тот же callback что в ``__main__._make_refresh_callback``."""

    async def _refresh() -> tuple[str, str] | None:
        if not cfg.user_email:
            return None
        _, refresh = load_tokens(cfg.user_email)
        if not refresh:
            return None
        sub = HubClient(base_url=cfg.base_url, access_token=None)
        try:
            data = await sub.refresh(refresh)
        except ApiError:
            return None
        finally:
            await sub.close()
        new_access = data["access_token"]
        new_refresh = data["refresh_token"]
        save_tokens(cfg.user_email, new_access, new_refresh)
        populate_from_jwt(cfg, new_access)
        cfg.save()
        return (new_access, new_refresh)

    return _refresh


def make_client(cfg: ClientConfig, access: str) -> HubClient:
    """Стандартный HubClient с auto-refresh callback."""
    return HubClient(
        base_url=cfg.base_url,
        access_token=access,
        on_token_refresh=make_refresh_callback(cfg),
    )


async def resolve_skill_id(client: HubClient, id_or_slug: str) -> str:
    """Привести ``id-или-slug`` к каноничному строковому skill_id.

    PK-миграция: id теперь —
    auto-increment int (на проводе строкой), slug опционален и не может быть
    полностью числовым. Дискриминатор:

    - ``id_or_slug.isdigit()`` → это id, отдаём как есть (fast-path без
      сетевого запроса; backend сам отрежет несуществующий id 404-ответом
      в последующей команде).
    - иначе → это slug, резолвим через ``GET /skills/{slug}`` (backend
      принимает и id, и slug) и возвращаем числовой ``id`` строкой.

    404-fallback: если slug не нашёлся, ``get_skill`` бросит ``ApiError``
    (404), которую вызывающая команда переведёт в человекочитаемый exit(1)
    через :func:`run` — отдельной обработки тут не требуется.
    """
    if id_or_slug.isdigit():
        return id_or_slug
    data: dict[str, Any] = await client.get_skill(id_or_slug)
    return str(data["id"])
