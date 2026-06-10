"""Общие helpers для E23-команд.

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

from skills_hub_cli.config import ClientConfig, load_tokens, populate_from_jwt, save_tokens
from skills_hub_cli.core.transport import ApiError, HubClient
from skills_hub_cli.output import emit_error

console = Console()


def run(coro) -> None:  # noqa: ANN001
    """asyncio.run + канон ошибок (зеркало ``__main__._run``).

    В json-режиме ApiError/RuntimeError уходят единым событием
    ``{"event":"error",...}`` в stderr (stdout остаётся машинным каналом);
    в text-режиме — прежнее читабельное сообщение. ``typer.Exit``/``Abort``
    наследуют RuntimeError через click — пропускаем их наверх как есть.
    """
    from skills_hub_cli.output import is_json

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


def get_access_token() -> str:
    """Получить access-token либо exit(1) с сообщением."""
    cfg = ClientConfig.load()
    if not cfg.user_email:
        emit_error("NOT_LOGGED_IN", "Сначала залогиньтесь: skills-hub login")
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

    PK-миграция (см. ``PK_MIGRATION_DESIGN.md`` §2.3/§3.E): id теперь —
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
