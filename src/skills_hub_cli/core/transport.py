"""Тонкий HTTP-клиент к backend.

- `trust_env=False` по умолчанию (system HTTP_PROXY игнорируется — CLI
  ходит к своему backend напрямую).
- Auto-refresh при 401: если есть `on_token_refresh` callback, при
  первом 401-ответе один раз пробует refresh + повторяет запрос.
"""
from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

RefreshCallback = Callable[[], Awaitable[tuple[str, str] | None]]
"""() -> (new_access, new_refresh) | None — None если refresh не получился."""


@dataclass
class ApiError(Exception):
    status_code: int
    code: str
    message: str
    details: dict[str, Any]

    def __str__(self) -> str:
        return f"[{self.status_code}/{self.code}] {self.message}"


class HubClient:
    def __init__(
        self,
        *,
        base_url: str,
        access_token: str | None = None,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
        on_token_refresh: RefreshCallback | None = None,
    ) -> None:
        self._access_token = access_token
        self._on_refresh = on_token_refresh
        trust_env = bool(
            int(os.environ.get("SKILLS_HUB_USE_SYSTEM_PROXY", "0") or "0")
        )
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            trust_env=trust_env,
        )

    def _auth_headers(self) -> dict[str, str]:
        h = {"Accept": "application/json"}
        if self._access_token:
            h["Authorization"] = f"Bearer {self._access_token}"
        return h

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        resp = await self._client.request(
            method, url, headers=self._auth_headers(), **kwargs
        )
        if resp.status_code == 401 and self._on_refresh is not None:
            # Auto-refresh: попробуем обменять refresh → повторить запрос
            new_tokens = await self._on_refresh()
            if new_tokens is not None:
                self._access_token = new_tokens[0]
                resp = await self._client.request(
                    method, url, headers=self._auth_headers(), **kwargs
                )
            # Если refresh не сработал — оставим 401, ниже выбросим понятный ApiError.
        if resp.status_code == 401:
            # Чёткое сообщение: сессия истекла / была инвалидирована
            raise ApiError(
                status_code=401,
                code="SESSION_EXPIRED",
                message=(
                    "Сессия устарела или подпись токена не валидна. "
                    "Сделайте login заново: `skills-hub login <invite-token>` "
                    "(или попросите админа выписать новый invite)."
                ),
                details={"upstream": resp.text[:300]},
            )
        if resp.status_code >= 400:
            try:
                data = resp.json()
            except Exception:
                data = {"code": "UNKNOWN", "message": resp.text, "details": {}}
            raise ApiError(
                status_code=resp.status_code,
                code=data.get("code", "UNKNOWN"),
                message=data.get("message", resp.text),
                details=data.get("details", {}),
            )
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    async def login_invite(
        self, *, invite_token: str, email: str, display_name: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/auth/login-invite",
            json={
                "invite_token": invite_token,
                "email": email,
                "display_name": display_name,
            },
        )

    async def login_password(
        self, *, email: str, password: str
    ) -> dict[str, Any]:
        """POST /auth/login — email + password логин.

        Returns: {access_token, refresh_token, user_id, access_expires_at, ...}.
        Backend кладёт refresh в cookie + body; CLI берёт из body и сохраняет
        в keyring.
        """
        return await self._request(
            "POST",
            "/auth/login",
            json={"email": email, "password": password},
        )

    async def set_password(self, *, new_password: str) -> None:
        """POST /me/password — установка/смена пароля для текущего user'а."""
        await self._request(
            "POST", "/me/password", json={"new_password": new_password}
        )

    async def refresh(self, refresh_token: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/auth/refresh", json={"refresh_token": refresh_token}
        )

    async def exchange_create(self) -> dict[str, Any]:
        """POST /auth/exchange/create — выписывает короткоживущий code для handoff в Web UI.

        Returns: {"code": "...", "expires_at": "..."}.
        Backend выписывает code привязанным к текущему access-токену; Web UI
        затем редеемит его через /auth/exchange/redeem и получает свою сессию.
        """
        return await self._request("POST", "/auth/exchange/create")

    async def list_skills(self, channel: str = "published") -> list[dict[str, Any]]:
        return await self._request("GET", "/skills", params={"channel": channel})

    async def install_bundle(self, slug: str, channel: str = "published") -> dict[str, Any]:
        return await self._request(
            "GET", f"/skills/{slug}/install-bundle", params={"channel": channel}
        )

    async def get_skill(self, slug: str) -> dict[str, Any]:
        return await self._request("GET", f"/skills/{slug}")

    async def publish_skill(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/skills", json=payload)

    async def sync_skill(self, slug: str, channel: str = "published") -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/skills/{slug}/sync-from-git",
            params={"channel": channel},
        )

    async def create_company(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/companies", json=payload)

    async def issue_invite(
        self, company_id: str, role_id: str, group_ids: list[str]
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/companies/{company_id}/invites",
            json={"role_id": role_id, "group_ids": group_ids},
        )

    async def submit_issue(
        self,
        slug: str,
        *,
        kind: str,
        title: str,
        description: str,
        version: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/skills/{slug}/issues",
            json={
                "skill_slug": slug,
                "version": version,
                "kind": kind,
                "title": title,
                "description": description,
                "payload": payload,
            },
        )
