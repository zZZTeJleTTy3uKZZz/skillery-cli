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
    """asyncio.run + перевод ApiError в console-friendly exit(1)."""
    try:
        asyncio.run(coro)
    except ApiError as e:
        console.print(f"[red]Ошибка API:[/] {e}")
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


async def resolve_skill_id(client: HubClient, slug_or_id: str) -> str:
    """Если передан slug — резолвим в skill_id через GET /skills/{slug}.

    Если уже похоже на id (Stripe-style ``slk_*``) — возвращаем как есть.
    """
    if slug_or_id.startswith("slk_"):
        return slug_or_id
    data: dict[str, Any] = await client.get_skill(slug_or_id)
    return str(data["id"])
