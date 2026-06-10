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
        extra_headers = kwargs.pop("headers", None) or {}

        def _merged_headers() -> dict[str, str]:
            h = self._auth_headers()
            h.update(extra_headers)
            return h

        resp = await self._client.request(method, url, headers=_merged_headers(), **kwargs)
        if resp.status_code == 401 and self._on_refresh is not None:
            # Auto-refresh: попробуем обменять refresh → повторить запрос
            new_tokens = await self._on_refresh()
            if new_tokens is not None:
                self._access_token = new_tokens[0]
                resp = await self._client.request(
                    method, url, headers=_merged_headers(), **kwargs
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
        # T5: бэкенд по умолчанию выполняет sync-from-git ФОНОМ (202 + job_id).
        # CLI — разовый интерактивный вызов: форсим синхронный путь
        # (`wait=true`), чтобы сразу получить полный SyncSkillResponse (200) —
        # прежнее поведение/контракт сохранены без поллинга статус-эндпоинта.
        return await self._request(
            "POST",
            f"/skills/{slug}/sync-from-git",
            params={"channel": channel, "wait": "true"},
        )

    async def create_company(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/companies", json=payload)

    async def issue_invite(
        self,
        company_id: str,
        role_id: str,
        email: str | None = None,
        display_name: str | None = None,
    ) -> dict[str, Any]:
        """POST /invites (flat, E1).

        nested ``/companies/{cid}/invites`` удалён — backend ждёт плоский
        ресурс с ``company_id`` в body. Поля ``email``/``display_name``
        опциональны (pre-emptive создание User(invited)+Membership). Ответ —
        ``FlatInviteResponse`` (``invite_token``/``invite_url``/``invite_id``/
        ``expires_at``/``is_new_user``).
        """
        body: dict[str, Any] = {"company_id": company_id, "role_id": role_id}
        if email is not None:
            body["email"] = email
        if display_name is not None:
            body["display_name"] = display_name
        return await self._request("POST", "/invites", json=body)

    # --- P1 member ---
    async def list_users(
        self,
        *,
        company_id: str | None = None,
        q: str | None = None,
        page: int = 1,
        size: int = 25,
    ) -> dict[str, Any]:
        """GET /users — участники (offset-режим W5).

        Сверено с ``routes/users.py::list_users`` (:233): наличие
        ``page``/``size`` включает серверную offset-пагинацию — ответ
        ``UserListResponse`` = ``{items, total, page, size}`` (cap size=200,
        422 PAGE_SIZE_TOO_LARGE сверху). ``q`` — подстрока по email/имени,
        ``company_id`` — фильтр по компании. Guard: hub-admin видит всех;
        tenant с user.invite/user.remove/company.manage — своих; обычный
        member получает «только себя» (бэк сужает сам, 403 не кидает).
        """
        params: dict[str, Any] = {"page": page, "size": size}
        if company_id is not None:
            params["company_id"] = company_id
        if q is not None:
            params["q"] = q
        return await self._request("GET", "/users", params=params)

    async def remove_membership(
        self, *, user_id: str, company_id: str
    ) -> None:
        """DELETE /memberships?user_id=&company_id= — убрать из компании.

        Сверено с ``routes/memberships.py::delete_membership`` (:32): flat-
        форма (E1), оба query-параметра обязательны, право ``user.remove``
        (hub-admin bypass). Ответ 204 → None. Side-effects бэка: refresh-
        токены target user'а revoked + audit ``user.remove``.
        """
        return await self._request(
            "DELETE",
            "/memberships",
            params={"user_id": user_id, "company_id": company_id},
        )

    async def bulk_change_role(
        self,
        *,
        user_ids: list[str],
        role_id: str,
        company_id: str,
    ) -> dict[str, Any]:
        """POST /users/bulk/change_role — смена membership.role_id.

        Сверено с ``routes/users.py::bulk_change_role`` (:1095): body
        ``BulkChangeRoleRequest`` = ``{user_ids, role_id, company_id}``;
        право hub.admin ИЛИ role.manage в этой company. Ответ
        ``BulkActionResponse`` = ``{updated_count, skipped_ids,
        results:[{id,outcome}], affected_count, dry_run}``. Не-assignable
        роль для company-admin → 422 ROLE_NOT_ASSIGNABLE_BY_COMPANY.
        """
        return await self._request(
            "POST",
            "/users/bulk/change_role",
            json={
                "user_ids": user_ids,
                "role_id": role_id,
                "company_id": company_id,
            },
        )

    async def lock_user(
        self, user_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """POST /users/{id}/lock — заблокировать вход (E12).

        Сверено с ``routes/users.py::lock_user`` (:842): body
        ``LockUserRequest`` = ``{reason?}`` (опционален, ≤500 симв.; без
        причины шлём ``{}``). Право hub.admin ИЛИ company-admin
        (``_can_admin_users``: user.lock/company.manage/...). Self-lock
        запрещён (409). Ответ — ``UserListItemDTO`` (``is_locked=True``);
        refresh-токены target'а revoked.
        """
        body: dict[str, Any] = {}
        if reason is not None:
            body["reason"] = reason
        return await self._request(
            "POST", f"/users/{user_id}/lock", json=body
        )

    async def unlock_user(self, user_id: str) -> dict[str, Any]:
        """POST /users/{id}/unlock — снять блокировку (E12).

        Сверено с ``routes/users.py::unlock_user`` (:888): без body, права
        те же, что у /lock. Ответ — ``UserListItemDTO``.
        """
        return await self._request("POST", f"/users/{user_id}/unlock")

    async def reset_user_password(self, user_id: str) -> dict[str, Any]:
        """POST /users/{id}/reset-password — одноразовый пароль.

        Сверено с ``routes/users.py::reset_user_password`` (:1152): без
        body; право hub.admin ИЛИ company.manage (target должен состоять в
        компании актора). Ответ ``ResetPasswordResponse`` =
        ``{temp_password, expires_hint, requires_password_change}``.
        Пароль отдаётся ТОЛЬКО в этом ответе (в БД — хэш), все refresh-
        токены target'а revoked.
        """
        return await self._request(
            "POST", f"/users/{user_id}/reset-password"
        )

    async def list_roles(
        self,
        *,
        page: int = 1,
        size: int = 100,
        q: str | None = None,
    ) -> dict[str, Any]:
        """GET /roles — глобальный каталог ролей (W3, paged W5).

        Сверено с ``routes/roles.py::list_roles`` (:99): форма — PAGED
        (НЕ плоский список): ``RoleWithScopeListResponse`` = ``{items,
        total, page, size}`` (cap size=500). Любой авторизованный (для
        role-picker'а). Item: id/slug/name/is_system/permission_keys/
        is_assignable_by_company/member_count/...
        """
        params: dict[str, Any] = {"page": page, "size": size}
        if q is not None:
            params["q"] = q
        return await self._request("GET", "/roles", params=params)

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

    # === E7 — Skill review (ratings / comments / contributors) ===
    async def rate_skill(self, skill_id: str, score: int) -> dict[str, Any]:
        """POST /skills/{skill_id}/ratings — upsert (skill_id, user_id) → score."""
        return await self._request(
            "POST",
            f"/skills/{skill_id}/ratings",
            json={"score": score},
        )

    async def get_rating_summary(self, skill_id: str) -> dict[str, Any]:
        """GET /skills/{skill_id}/ratings/summary — avg / count / distribution."""
        return await self._request(
            "GET", f"/skills/{skill_id}/ratings/summary"
        )

    async def post_comment(
        self,
        skill_id: str,
        *,
        body: str,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /skills/{skill_id}/comments — JSON-вариант (без screenshots)."""
        return await self._request(
            "POST",
            f"/skills/{skill_id}/comments",
            json={"body": body, "parent_id": parent_id},
        )

    async def post_comment_multipart(
        self,
        skill_id: str,
        *,
        body: str,
        screenshots: list[tuple[str, bytes]],
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /skills/{skill_id}/comments/multipart — body + 0..N файлов.

        screenshots: список `(filename, content)`.
        """
        data: dict[str, str] = {"body": body}
        if parent_id:
            data["parent_id"] = parent_id
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            ("screenshots", (name, content, "application/octet-stream"))
            for name, content in screenshots
        ]
        resp = await self._client.request(
            "POST",
            f"/skills/{skill_id}/comments/multipart",
            data=data,
            files=files,
            headers=self._auth_headers(),
        )
        if resp.status_code >= 400:
            try:
                d = resp.json()
            except Exception:
                d = {"code": "UNKNOWN", "message": resp.text, "details": {}}
            raise ApiError(
                status_code=resp.status_code,
                code=d.get("code", "UNKNOWN"),
                message=d.get("message", resp.text),
                details=d.get("details", {}),
            )
        return resp.json()

    async def list_comments(
        self,
        skill_id: str,
        *,
        limit: int = 50,
        starting_after: str | None = None,
    ) -> dict[str, Any]:
        """GET /skills/{skill_id}/comments — Stripe cursor page (public)."""
        params: dict[str, Any] = {"limit": limit}
        if starting_after:
            params["starting_after"] = starting_after
        return await self._request(
            "GET", f"/skills/{skill_id}/comments", params=params
        )

    async def edit_comment(
        self, comment_id: str, *, body: str
    ) -> dict[str, Any]:
        """PATCH /comments/{id} — отредактировать свой comment.

        Сверено с ``routes/skill_review.py::edit_comment``:
        ``EditCommentRequest`` = ``{body}``; ответ — **bare** ``SkillCommentDTO``
        (НЕ обёрнут в ``{"comment": ...}``, в отличие от POST). Permission
        ``comment.edit_own`` (только автор). Comment адресуется по числовому id.
        """
        return await self._request(
            "PATCH",
            f"/comments/{comment_id}",
            json={"body": body},
        )

    async def delete_comment(self, comment_id: str) -> dict[str, Any]:
        """DELETE /comments/{id} — soft-delete своего comment'а.

        Сверено с ``routes/skill_review.py::delete_comment``: ответ — **bare**
        ``SkillCommentDTO`` (``is_deleted=True``, ``body="[deleted]"``).
        Permission ``comment.delete_own`` (автор) ИЛИ
        ``comment.delete_any``/``hub.admin``/``skill.manage`` (модератор).
        """
        return await self._request(
            "DELETE",
            f"/comments/{comment_id}",
        )

    async def list_contributors(
        self, skill_id: str, *, refresh: bool = False
    ) -> dict[str, Any]:
        """GET /skills/{skill_id}/contributors."""
        return await self._request(
            "GET",
            f"/skills/{skill_id}/contributors",
            params={"refresh": str(refresh).lower()},
        )

    # === E8 — Support tickets ===
    async def create_ticket(
        self,
        *,
        subject: str,
        body: str,
        kind: str = "other",
        priority: str = "normal",
        skill_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /support/tickets — создание тикета."""
        return await self._request(
            "POST",
            "/support/tickets",
            json={
                "subject": subject,
                "body": body,
                "kind": kind,
                "priority": priority,
                "skill_id": skill_id,
            },
        )

    async def list_tickets(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        priority: str | None = None,
        skill_id: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict[str, Any]:
        """GET /support/tickets — list (scope-aware filter)."""
        params: dict[str, Any] = {"page": page, "page_size": page_size}
        if status:
            params["status"] = status
        if kind:
            params["kind"] = kind
        if priority:
            params["priority"] = priority
        if skill_id:
            params["skill_id"] = skill_id
        return await self._request("GET", "/support/tickets", params=params)

    async def get_ticket(self, ticket_id: str) -> dict[str, Any]:
        """GET /support/tickets/{id} — detail."""
        return await self._request("GET", f"/support/tickets/{ticket_id}")

    async def reply_ticket(
        self,
        ticket_id: str,
        *,
        body: str,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /support/tickets/{id}/messages — add message (JSON, без файлов).

        Контракт сверен с ``routes/support_tickets.py::post_message``:
        ``PostTicketMessageRequest`` = ``{body, parent_id?}``; ответ
        ``PostTicketMessageResponse`` = ``{"message": {...}}``.
        Permission ``ticket.create`` (все участники треда).
        """
        payload: dict[str, Any] = {"body": body}
        if parent_id:
            payload["parent_id"] = parent_id
        return await self._request(
            "POST",
            f"/support/tickets/{ticket_id}/messages",
            json=payload,
        )

    async def reply_ticket_multipart(
        self,
        ticket_id: str,
        *,
        body: str,
        screenshots: list[tuple[str, bytes]],
    ) -> dict[str, Any]:
        """POST /support/tickets/{id}/messages/multipart — body + 0..N файлов.

        Сверено с ``post_message_multipart``: Form ``body`` + ``screenshots[]``
        файлы. ``parent_id`` в multipart-варианте бэкендом НЕ принимается
        (только в JSON-варианте) — поэтому отсутствует. ``screenshots``: список
        ``(filename, content)``.
        """
        data: dict[str, str] = {"body": body}
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            ("screenshots", (name, content, "application/octet-stream"))
            for name, content in screenshots
        ]
        resp = await self._client.request(
            "POST",
            f"/support/tickets/{ticket_id}/messages/multipart",
            data=data,
            files=files,
            headers=self._auth_headers(),
        )
        if resp.status_code >= 400:
            try:
                d = resp.json()
            except Exception:
                d = {"code": "UNKNOWN", "message": resp.text, "details": {}}
            raise ApiError(
                status_code=resp.status_code,
                code=d.get("code", "UNKNOWN"),
                message=d.get("message", resp.text),
                details=d.get("details", {}),
            )
        return resp.json()

    async def set_ticket_status(
        self, ticket_id: str, *, status: str
    ) -> dict[str, Any]:
        """PATCH /support/tickets/{id} — сменить статус.

        Сверено с ``routes/support_tickets.py::update_ticket`` +
        доменом ``TicketStatus``: допустимые статусы —
        ``new | in_progress | scheduled | done | rejected`` (НЕ
        open/resolved/closed — те значения дают 422). Permission
        ``ticket.update_status`` (owner/manager). Ответ — ``SupportTicketDTO``.
        """
        return await self._request(
            "PATCH",
            f"/support/tickets/{ticket_id}",
            json={"status": status},
        )

    # === E10 — Collections ===
    async def list_collections(
        self,
        *,
        company_id: str | None = None,
        type: str | None = None,
        owner_id: str | None = None,
        include_global: bool = True,
    ) -> dict[str, Any]:
        """GET /collections — список коллекций."""
        params: dict[str, Any] = {"include_global": str(include_global).lower()}
        if company_id:
            params["company_id"] = company_id
        if type:
            params["type"] = type
        if owner_id:
            params["owner_id"] = owner_id
        return await self._request("GET", "/collections", params=params)

    async def get_collection(self, slug: str) -> dict[str, Any]:
        """GET /collections/{slug} — детали + skills."""
        return await self._request("GET", f"/collections/{slug}")

    # === E6 — Events ingestion ===
    async def ingest_events(
        self,
        events: list[dict[str, Any]],
        *,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """POST /events — batch ingestion (требует Idempotency-Key header)."""
        return await self._request(
            "POST",
            "/events",
            json={"events": events},
            headers={"Idempotency-Key": idempotency_key},
        )
