"""Тонкий HTTP-клиент к backend.

- `trust_env=False` по умолчанию (system HTTP_PROXY игнорируется — CLI
  ходит к своему backend напрямую).
- Auto-refresh при 401: если есть `on_token_refresh` callback, при
  первом 401-ответе один раз пробует refresh + повторяет запрос.

cli-kits W3: сетевой choke-point — ``librarykit.transport.HttpxTransport``
(вместо прямого ``httpx.AsyncClient``). Транспорт кита несёт ОДНУ сетевую
попытку (его собственный stamina-retry выключен политикой ``total=0``), а
method-aware retry (W1: ретраим только идемпотентные методы), 401-refresh и
маппинг ошибок в :class:`ApiError` остаются ЗДЕСЬ — это CLI-специфика, которую
тонкий REST-клиент кита не покрывает (метод-осведомлённость, tuple-refresh,
наш подкласс ошибки, multipart, ``get_text``). Сетевой сбой транспорт кита
оборачивает в доменный ``librarykit.errors.TransportError``.
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from librarykit.errors import CliError as _LkCliError
from librarykit.errors import TransportError as _LkTransportError
from librarykit.retry import RetryPolicy, SimpleRetryPolicy
from librarykit.transport import HttpxTransport

from skills_hub_cli import __version__ as _CLI_VERSION

RefreshCallback = Callable[[], Awaitable[tuple[str, str] | None]]
"""() -> (new_access, new_refresh) | None — None если refresh не получился."""

# H-5: User-Agent CLI — backend различает WEB/CLI-сессии по этому префиксу
# (``skills-hub-cli`` ⇒ client_type='cli'). Версия — из метаданных пакета.
USER_AGENT = f"skills-hub-cli/{_CLI_VERSION}"

# cli-kits W1: только эти HTTP-методы идемпотентны → их безопасно повторять.
# Мутации (POST/PATCH/PUT/DELETE) НЕ ретраим — повтор рискует двойным эффектом.
_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Единая политика повторов к backend (канон librarykit/clikit): фиксированный
# backoff, ретраебельные статусы 5xx+429, повтор на сетевых ошибках.
# Бюджет попыток = len(backoff)+1 (первичная + повторы).
#
# cli-kits W3: сетевые сбои теперь приходят как ``librarykit.errors.
# TransportError`` (транспорт кита оборачивает httpx-ошибку), а не как голый
# ``httpx.TransportError`` — поэтому добавляем доменный тип в ``exceptions``
# политики, чтобы ``should_retry`` по-прежнему признавал сетевой сбой
# ретраебельным (оба типа — на случай прямого httpx-исключения).
RETRY_POLICY = SimpleRetryPolicy(
    exceptions=(_LkTransportError, httpx.TransportError)
)

# Только для разбора заголовков паузы (Retry-After / *-RateLimit-Reset) —
# header-driven логика живёт в librarykit.RetryPolicy, не дублируем парсер.
_HEADER_POLICY = RetryPolicy()

# cli-kits W3: транспорт кита делает ОДНУ сетевую попытку — повторы драйвит наш
# method-aware ``_send`` (W1), а не stamina внутри транспорта (он не различает
# идемпотентность метода). ``total=0`` ⇒ ``stamina_attempts()==1`` (без повторов).
_TRANSPORT_NO_RETRY = RetryPolicy(total=0)


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Рекомендованная пауза из заголовков ответа (или ``None``).

    Делегирует разбор ``Retry-After`` / rate-limit-reset в librarykit, чтобы не
    плодить собственный парсер HTTP-date/секунд.
    """
    return _HEADER_POLICY.retry_after_delay(resp)


class ApiError(_LkCliError):
    """HTTP-ошибка CLI-транспорта (обратносовместимый публичный контракт).

    cli-kits W1: теперь подкласс ``librarykit.errors.CliError`` (== librarykit
    ``ApiError``) — попадает в общую иерархию ошибок китов, оставаясь при этом
    100% совместимым со СВОИМ прежним контрактом, на который завязаны команды:
    конструктор ``ApiError(status_code=, code=, message=, details=)``, атрибуты
    ``.status_code`` / ``.code`` / ``.message`` / ``.details`` и
    ``str(err) == "[<status>/<code>] <message>"``.

    ``details`` зеркалится в librarykit-поле ``data`` (и наоборот), чтобы общий
    код китов, читающий ``.data``, видел те же детали.
    """

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        details = details or {}
        super().__init__(
            code,
            message,
            status_code=status_code,
            data=details,
        )
        # `status_code`/`code`/`message`/`data` уже выставлены базовым CliError;
        # `details` — наш исторический алиас для `data`.
        self.details = details

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
        # cli-kits W3: сетевой слой — librarykit ``HttpxTransport``. Готовый
        # ``http_client`` (для тестов) внедряем в транспорт; иначе транспорт сам
        # поднимает ``AsyncClient`` под наш ``base_url``/``timeout``/``trust_env``.
        # Retry транспорта выключен (``_TRANSPORT_NO_RETRY``) — повторы драйвит
        # наш method-aware ``_send`` (W1).
        if http_client is not None:
            self._transport = HttpxTransport(
                retry=_TRANSPORT_NO_RETRY, http_client=http_client
            )
        else:
            self._transport = HttpxTransport(
                base_url=base_url,
                retry=_TRANSPORT_NO_RETRY,
                timeout=timeout,
                trust_env=trust_env,
            )

    def _auth_headers(self) -> dict[str, str]:
        # H-5: всегда шлём CLI User-Agent — backend помечает сессию client_type=
        # 'cli' (логин/refresh идут этим же клиентом, поэтому UA попадает в
        # session-запись). Перебивает дефолтный httpx UA.
        h = {"Accept": "application/json", "User-Agent": USER_AGENT}
        if self._access_token:
            h["Authorization"] = f"Bearer {self._access_token}"
        return h

    def set_access_token(self, token: str) -> None:
        """Авторизовать клиент уже выписанным access-токеном (после login).

        JWT-slim: нужен, чтобы ТЕМ ЖЕ клиентом сходить за эффективными правами
        в ``/me/permissions`` (токен развёрнутый список прав больше не несёт).
        """
        self._access_token = token

    async def close(self) -> None:
        await self._transport.aclose()

    def _parse_error_response(self, resp: httpx.Response) -> ApiError:
        """Единый разбор ошибочного ответа (>=400) → ApiError.

        Покрывает все формы тела:
        - FastAPI ``{"detail": dict}`` — наши use-case коды (EMAIL_TAKEN,
          PERMISSION_DENIED, RATE_LIMITED, INVALID_CREDENTIALS, ...) →
          разворачиваем в top-level ``code``/``message``/``details``;
        - ``{"detail": str}`` (HTTPException) → message;
        - ``{"detail": list}`` (422 pydantic) → ``code=VALIDATION`` + склейка
          ``loc: msg``;
        - отсутствие ``detail`` → читаем top-level ``{code,message,details}``;
        - не-JSON тело → ``code=UNKNOWN``, message = сырой текст.

        401: НЕ подменяем тело вслепую. Сначала разворачиваем ответ как любой
        другой статус — чтобы отказ логина отдал свой машинный код
        (``INVALID_CREDENTIALS``/``AUTH_METHOD_NOT_AVAILABLE``), а истёкший
        токен — свой (``TOKEN_REVOKED`` и т.п.). Спец-хинт «сделайте login
        заново» оставляем ТОЛЬКО как fallback, когда машинного кода в теле
        нет (реально протухшая/невалидная подпись на авторизованном вызове).

        429 (RATE_LIMITED) разбирается на общих основаниях — code берётся из
        тела (detail-dict ИЛИ top-level), что не ломает RetryPolicy-ретрай.
        """
        try:
            data = resp.json()
        except Exception:
            data = {"code": "UNKNOWN", "message": resp.text, "details": {}}
        if not isinstance(data, dict):
            data = {"code": "UNKNOWN", "message": str(data), "details": {}}
        # FastAPI заворачивает ошибки в {"detail": ...}: dict (наши
        # use-case коды), str (HTTPException) или list (422 pydantic).
        # Разворачиваем, чтобы code/message были машинно-доступны
        # (EMAIL_TAKEN, PERMISSION_DENIED, RATE_LIMITED, ...), а не сырой JSON.
        detail = data.get("detail")
        if isinstance(detail, dict):
            data = {**data, **detail}
        elif isinstance(detail, str):
            data = {**data, "message": detail}
        elif isinstance(detail, list):
            parts = []
            for err in detail:
                if isinstance(err, dict):
                    loc = ".".join(str(x) for x in err.get("loc", []))
                    parts.append(f"{loc}: {err.get('msg', '')}".strip(": "))
            data = {
                **data,
                "code": "VALIDATION",
                "message": "; ".join(parts) or resp.text,
            }
        code = data.get("code", "UNKNOWN")
        # 401 без машинного кода = протухший/невалидный токен на авторизованном
        # вызове (бэкенд отдаёт detail-строку «Невалидный токен: …»). Тут даём
        # понятный хинт. Если же код есть (отказ логина / TOKEN_REVOKED) —
        # отдаём его как есть, не подменяя.
        if resp.status_code == 401 and code == "UNKNOWN":
            return ApiError(
                status_code=401,
                code="SESSION_EXPIRED",
                message=(
                    "Сессия устарела или подпись токена не валидна. "
                    "Сделайте login заново: `skillery login <invite-token>` "
                    "(или попросите админа выписать новый invite)."
                ),
                details={"upstream": resp.text[:300]},
            )
        return ApiError(
            status_code=resp.status_code,
            code=code,
            message=data.get("message", resp.text),
            details=data.get("details", {}),
        )

    async def _send(
        self, method: str, url: str, headers: dict[str, str], **kwargs: Any
    ) -> httpx.Response:
        """Один HTTP-вызов с ретраями librarykit (cli-kits W1).

        Повторяем ТОЛЬКО идемпотентные методы (``GET``/``HEAD``/``OPTIONS``) и
        ТОЛЬКО на ретраебельных причинах: ретраебельный статус (5xx/429) ИЛИ
        сетевая ошибка httpx (``TransportError``). Мутации (POST/PATCH/PUT/
        DELETE) и 4xx-кроме-429 не повторяются. На 429 уважаем ``Retry-After``
        (политика читает заголовок), иначе — табличный backoff. По исчерпании
        бюджета: пробрасываем последнее сетевое исключение либо отдаём последний
        ответ (его разберёт ``_parse_error_response`` выше).
        """
        idempotent = method.upper() in _IDEMPOTENT_METHODS
        policy = RETRY_POLICY
        attempt = 0
        last_resp: httpx.Response | None = None
        while True:
            try:
                resp = await self._transport.request(
                    method, url, headers=headers, **kwargs
                )
            except _LkTransportError as exc:
                # Сетевой сбой: транспорт кита обернул httpx-ошибку в доменный
                # ``librarykit.errors.TransportError``. Идемпотентные методы
                # повторяем (W1), иначе — пробрасываем доменную ошибку наверх.
                if idempotent and policy.should_retry(attempt, None, exc):
                    await asyncio.sleep(policy.delay(attempt))
                    attempt += 1
                    continue
                raise
            last_resp = resp
            if (
                idempotent
                and resp.status_code >= 400
                and policy.should_retry(attempt, resp.status_code, None)
            ):
                # 429/5xx — пауза из Retry-After (если есть), иначе backoff.
                retry_after = _retry_after_seconds(resp)
                delay = retry_after if retry_after is not None else policy.delay(attempt)
                await asyncio.sleep(delay)
                attempt += 1
                continue
            return resp
        return last_resp  # pragma: no cover — недостижимо (цикл всегда return/raise)

    async def _request(self, method: str, url: str, **kwargs: Any) -> Any:
        extra_headers = kwargs.pop("headers", None) or {}

        def _merged_headers() -> dict[str, str]:
            h = self._auth_headers()
            h.update(extra_headers)
            return h

        resp = await self._send(method, url, _merged_headers(), **kwargs)
        if resp.status_code == 401 and self._on_refresh is not None:
            # Auto-refresh: попробуем обменять refresh → повторить запрос
            new_tokens = await self._on_refresh()
            if new_tokens is not None:
                self._access_token = new_tokens[0]
                resp = await self._send(method, url, _merged_headers(), **kwargs)
            # Если refresh не сработал — оставим 401, ниже выбросим понятный ApiError.
        if resp.status_code >= 400:
            raise self._parse_error_response(resp)
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

    async def get_me_permissions(self) -> list[str]:
        """GET /me/permissions — authoritative эффективные права актора (из БД).

        JWT-slim: единый источник прав для CLI-гейтинга (раньше читались из
        JWT-claim ``permissions``, который убран из токена). Требует Bearer —
        вызывать после :meth:`set_access_token`. Ответ — ``{permissions:[...],
        company, role}``; берём только ``permissions``.
        """
        data = await self._request("GET", "/me/permissions")
        perms = data.get("permissions") or []
        return [str(p) for p in perms]

    # --- P1 account ---
    async def register(
        self, *, email: str, password: str, display_name: str
    ) -> dict[str, Any]:
        """POST /auth/register — самостоятельная регистрация (без инвайта).

        Сверено с ``routes/auth.py::register`` (line 137): body =
        ``{email, password, display_name}`` — display_name ОБЯЗАТЕЛЕН
        (schemas.RegisterRequest: min_length=1), пароль ≥ 8 символов.
        Ответ 201 ``LoginPasswordResponse`` = ``{access_token,
        access_expires_at, refresh_token, refresh_expires_at, user_id}``.
        Errors: 409 EMAIL_TAKEN; 422 VALIDATION (слабый пароль).
        """
        return await self._request(
            "POST",
            "/auth/register",
            json={
                "email": email,
                "password": password,
                "display_name": display_name,
            },
        )

    async def register_with_link(
        self, *, email: str, password: str, display_name: str, token: str
    ) -> dict[str, Any]:
        """POST /auth/register-with-link — регистрация по пригласительной ссылке.

        Сверено с ``routes/auth.py::register_with_link`` (line 188): body =
        ``{email, password, display_name, token}`` (token ≥ 8 символов).
        Создаёт юзера + membership в компании ссылки → токены с company_id.
        Ответ 201 ``LoginPasswordResponse``. Errors: 404 LINK_NOT_FOUND;
        409 CONFLICT (email занят / ссылка исчерпана или отозвана);
        422 VALIDATION.
        """
        return await self._request(
            "POST",
            "/auth/register-with-link",
            json={
                "email": email,
                "password": password,
                "display_name": display_name,
                "token": token,
            },
        )

    async def accept_invite_link(self, *, token: str) -> None:
        """POST /invite-links/accept — залогиненный вступает в компанию по ссылке.

        Сверено с ``routes/company_invite_links.py::accept_invite_link``
        (line 207): body = ``{token}``, требуется auth (Bearer). Ответ —
        204 No Content (``_request`` вернёт None). Errors: 404 NOT_FOUND
        (невалидная ссылка); 409 LINK_UNUSABLE (исчерпана/отозвана).
        После вступления переключение компании — POST /me/active-company.
        """
        await self._request(
            "POST", "/invite-links/accept", json={"token": token}
        )

    async def accept_invite(self, *, token: str) -> None:
        """POST /invites/accept — залогиненный принимает ОДНОРАЗОВЫЙ invite.

        Сверено с ``routes/invites.py::accept_invite``: body ``{token}``,
        auth (Bearer), 204. Errors: 404 NOT_FOUND; 409 INVITE_UNUSABLE.
        """
        await self._request("POST", "/invites/accept", json={"token": token})

    async def register_device(
        self, *, name: str, platform: str
    ) -> dict[str, Any]:
        """POST /me/devices — регистрация CLI-устройства (E-D web↔CLI мост).

        Сверено с ``routes/me.py::register_device``: body ``{name, platform}``,
        auth, 201. Upsert по (user, name)."""
        return await self._request(
            "POST", "/me/devices", json={"name": name, "platform": platform}
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
        """POST /auth/exchanges — выписывает короткоживущий code для handoff в Web UI.

        Канон (волна 3): создание обменника = POST /auth/exchanges (отдаёт 201).
        Старый POST /auth/exchange/create сохранён как deprecated-алиас (200).
        Returns: {"code": "...", "expires_at": "..."}.
        Backend выписывает code привязанным к текущему access-токену; Web UI
        затем редеемит его через POST /auth/exchanges/{code}/redeem и получает
        свою сессию.
        """
        return await self._request("POST", "/auth/exchanges")

    async def list_skills(self, channel: str = "published") -> list[dict[str, Any]]:
        return await self._request("GET", "/skills", params={"channel": channel})

    async def install_bundle(self, slug: str, channel: str = "published") -> dict[str, Any]:
        return await self._request(
            "GET", f"/skills/{slug}/install-bundle", params={"channel": channel}
        )

    async def install_skill(
        self, slug: str, channel: str = "published"
    ) -> dict[str, Any]:
        """POST /skills/{slug}/install — пометить навык установленным для актора.

        Сверено с ``routes/skills.py::install_skill``: эмитит install-событие
        (тот же путь, что install-bundle) + проверяет доступ (закрытый навык без
        гранта → 403). Файлы НЕ качает — это делает CLI отдельно через
        :meth:`install_bundle`. Ответ — ``{install_state: {...}}``. Право
        ``skill.install``.
        """
        return await self._request(
            "POST", f"/skills/{slug}/install", params={"channel": channel}
        )

    async def list_my_installs(self) -> list[dict[str, Any]]:
        """GET /me/installs — навыки, помеченные актором установленными.

        Сверено с ``routes/me.py::list_my_installs``: ответ —
        ``{items: [{slug, skill_id, installed_version}]}``. Источник — install-
        события (та же истина, что install_state). Используется
        ``skills-hub pull`` и демоном для reconcile (знать ЧТО тянуть). Берём
        только ``items``.
        """
        data = await self._request("GET", "/me/installs")
        items = data.get("items") if isinstance(data, dict) else None
        return list(items) if items else []

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

    async def yank_skill_version(
        self, *, slug: str, semver: str, yank: bool = True
    ) -> None:
        """#340: снять/вернуть версию навыка (yank/unyank). 204 без тела."""
        action = "yank" if yank else "unyank"
        await self._request(
            "POST", f"/skills/{slug}/versions/{semver}/{action}"
        )

    async def create_company(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/companies", json=payload)

    # --- P1 company ---
    async def list_companies(
        self,
        *,
        q: str | None = None,
        page: int | None = None,
        size: int | None = None,
    ) -> dict[str, Any]:
        """GET /companies — список компаний (hub.admin).

        Сверено с ``routes/companies.py::list_companies`` (W5): server-side
        offset-пагинация ``page/size/q`` → ``{items,total,page,size}``.
        Неуказанные параметры не шлём — backend применит свои дефолты.
        """
        params: dict[str, Any] = {}
        if q:
            params["q"] = q
        if page is not None:
            params["page"] = page
        if size is not None:
            params["size"] = size
        return await self._request("GET", "/companies", params=params)

    async def get_company(self, company_id: str) -> dict[str, Any]:
        """GET /companies/{id} — детали компании.

        Сверено с ``routes/companies.py::get_company``: доступ — член ЭТОЙ
        компании или hub.admin (tenant-изоляция через require_same_company).
        Ответ — ``CompanyDetailResponse`` (counts, plan/status, owner UserRef).
        """
        return await self._request("GET", f"/companies/{company_id}")

    async def update_company(
        self, company_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /companies/{id} — merge-patch компании.

        Сверено с ``routes/companies.py::update_company``: право — hub.admin
        ИЛИ company.manage в своей компании; plan/status меняет только
        hub.admin. Шлём только изменяемые поля (None = «не менять»).
        """
        return await self._request(
            "PATCH", f"/companies/{company_id}", json=payload
        )

    async def switch_active_company(self, company_id: str) -> dict[str, Any]:
        """POST /me/active-company — переключить активную компанию.

        Сверено с ``routes/me.py::switch_active_company``: переключение
        возможно ТОЛЬКО в компанию с membership (иначе 403). Ответ —
        ``LoginPasswordResponse`` с НОВОЙ парой токенов (re-issue под
        permissions роли в целевой компании) — caller обязан сохранить пару.
        """
        return await self._request(
            "POST", "/me/active-company", json={"company_id": company_id}
        )

    async def list_invite_links(self, company_id: str) -> dict[str, Any]:
        """GET /companies/{id}/invite-links — переиспользуемые ссылки.

        Сверено с ``routes/company_invite_links.py::list_invite_links``:
        гейт — hub.admin ИЛИ company-admin (role.manage|company.manage в этой
        компании). Ответ ``{"links": [InviteLinkDTO]}``.
        """
        return await self._request(
            "GET", f"/companies/{company_id}/invite-links"
        )

    async def create_invite_link(
        self,
        company_id: str,
        *,
        kind: str = "member",
        max_uses: int | None = None,
        expires_in_days: int | None = None,
    ) -> dict[str, Any]:
        """POST /companies/{id}/invite-links — создать ссылку (201).

        Сверено с ``routes/company_invite_links.py::create_invite_link``:
        body ``{kind: member|manager, max_uses?, expires_in_days?}``;
        manager-ссылку создаёт только владелец компании или hub.admin.
        Ответ ``InviteLinkCreatedResponse`` — plaintext ``token`` отдаётся
        ОДИН раз (из него строится join-URL).
        """
        body: dict[str, Any] = {"kind": kind}
        if max_uses is not None:
            body["max_uses"] = max_uses
        if expires_in_days is not None:
            body["expires_in_days"] = expires_in_days
        return await self._request(
            "POST", f"/companies/{company_id}/invite-links", json=body
        )

    async def revoke_invite_link(self, company_id: str, link_id: str) -> None:
        """DELETE /companies/{id}/invite-links/{link_id} — отозвать (204)."""
        return await self._request(
            "DELETE", f"/companies/{company_id}/invite-links/{link_id}"
        )

    async def get_company_catalog(self, company_id: str) -> dict[str, Any]:
        """GET /companies/{id}/catalog — granted-каталог компании.

        Сверено с ``routes/catalog.py::get_company_catalog``: чтение —
        hub.admin / catalog.manage / catalog.view_all в своей компании.
        Ответ ``{company_id, skills, collections, effective_skills}``.
        """
        return await self._request("GET", f"/companies/{company_id}/catalog")

    async def grant_catalog_skill(
        self, company_id: str, skill_id: str
    ) -> None:
        """POST /companies/{id}/catalog/skills — выдать навык компании (204).

        Сверено с ``routes/catalog.py::grant_skill``: body ``{skill_id}`` —
        строго ЧИСЛОВОЙ id (``_int_id``, иначе 404) — slug caller резолвит
        заранее (``GET /skills/{slug}``).
        """
        return await self._request(
            "POST",
            f"/companies/{company_id}/catalog/skills",
            json={"skill_id": skill_id},
        )

    async def revoke_catalog_skill(
        self, company_id: str, id_or_slug: str
    ) -> None:
        """DELETE /companies/{id}/catalog/skills/{slug} — отозвать навык (204).

        Сверено с ``routes/catalog.py::revoke_skill``: path-сегмент принимает
        id-ИЛИ-slug (backend резолвит через get_by_id_or_slug).
        """
        return await self._request(
            "DELETE", f"/companies/{company_id}/catalog/skills/{id_or_slug}"
        )

    async def grant_catalog_collection(
        self, company_id: str, collection_id: str
    ) -> None:
        """POST /companies/{id}/catalog/collections — выдать коллекцию (204).

        Сверено с ``routes/catalog.py::grant_collection``: body
        ``{collection_id}`` — числовой id.
        """
        return await self._request(
            "POST",
            f"/companies/{company_id}/catalog/collections",
            json={"collection_id": collection_id},
        )

    async def revoke_catalog_collection(
        self, company_id: str, collection_id: str
    ) -> None:
        """DELETE /companies/{id}/catalog/collections/{id} — отозвать (204).

        Сверено с ``routes/catalog.py::revoke_collection``: path — строго
        ЧИСЛОВОЙ collection_id (slug caller резолвит заранее).
        """
        return await self._request(
            "DELETE",
            f"/companies/{company_id}/catalog/collections/{collection_id}",
        )

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
        """DELETE /companies/{company_id}/members/{user_id} — убрать из компании.

        M-1: канон — path-форма ``DELETE /companies/{cid}/members/{uid}``
        (``routes/companies.py``). Старый flat-эндпоинт
        ``DELETE /memberships?user_id=&company_id=`` (deprecated) принимается
        бэкендом до сих пор, поведение идентично — но CLI шлёт канон.
        Право ``user.remove`` (hub-admin bypass). Ответ 204 → None.
        Side-effects бэка: refresh-токены target user'а revoked + audit
        ``user.remove``.
        """
        return await self._request(
            "DELETE",
            f"/companies/{company_id}/members/{user_id}",
        )

    async def bulk_change_role(
        self,
        *,
        user_ids: list[str],
        role_id: str,
        company_id: str,
    ) -> dict[str, Any]:
        """POST /users/bulk/change-role — смена membership.role_id.

        Канон (волна 3): kebab-путь ``/users/bulk/change-role``. Старый
        ``/users/bulk/change_role`` остаётся deprecated-алиасом.
        Сверено с ``routes/users.py::bulk_change_role`` (:1095): body
        ``BulkChangeRoleRequest`` = ``{user_ids, role_id, company_id}``;
        право hub.admin ИЛИ role.manage в этой company. Ответ
        ``BulkActionResponse`` = ``{updated_count, skipped_ids,
        results:[{id,outcome}], affected_count, dry_run}``. Не-assignable
        роль для company-admin → 422 ROLE_NOT_ASSIGNABLE_BY_COMPANY.
        """
        return await self._request(
            "POST",
            "/users/bulk/change-role",
            json={
                "user_ids": user_ids,
                "role_id": role_id,
                "company_id": company_id,
            },
        )

    async def lock_user(
        self, user_id: str, *, reason: str | None = None
    ) -> dict[str, Any]:
        """PUT /users/{id}/lock — заблокировать вход (E12).

        Канон (волна 3): метод PUT. Старый POST остаётся deprecated-алиасом.
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
            "PUT", f"/users/{user_id}/lock", json=body
        )

    async def unlock_user(self, user_id: str) -> dict[str, Any]:
        """PUT /users/{id}/unlock — снять блокировку (E12).

        Канон (волна 3): метод PUT. Старый POST остаётся deprecated-алиасом.
        Сверено с ``routes/users.py::unlock_user`` (:888): без body, права
        те же, что у /lock. Ответ — ``UserListItemDTO``.
        """
        return await self._request("PUT", f"/users/{user_id}/unlock")

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

    # --- D-CLI M-3: bulk suspend/activate + revoke-sessions ---
    async def bulk_suspend(self, *, user_ids: list[str]) -> dict[str, Any]:
        """POST /users/bulk/suspend — массово suspend + revoke сессий.

        Сверено с ``routes/users.py::bulk_suspend`` (:1250): body
        ``BulkUserIdsRequest`` = ``{user_ids}`` (selection-by-id; ``filters``/
        ``dry_run`` — не используем из CLI). Право hub.admin ИЛИ company-admin
        (``user.lock``/``user.update``/``company.manage``) над своими.
        Ответ ``BulkActionResponse`` = ``{updated_count, skipped_ids,
        results:[{id,outcome}], affected_count, dry_run}``.
        """
        return await self._request(
            "POST", "/users/bulk/suspend", json={"user_ids": user_ids}
        )

    async def bulk_activate(self, *, user_ids: list[str]) -> dict[str, Any]:
        """POST /users/bulk/activate — массово активировать (status=active).

        Сверено с ``routes/users.py::bulk_activate`` (:1277): те же body/права/
        ответ, что и у :meth:`bulk_suspend`.
        """
        return await self._request(
            "POST", "/users/bulk/activate", json={"user_ids": user_ids}
        )

    async def revoke_user_sessions(self, user_id: str) -> dict[str, Any]:
        """POST /users/{id}/revoke-sessions — «выйти со всех устройств».

        Сверено с ``routes/users.py::revoke_user_sessions`` (:1009): без body;
        бампает session-эпоху (живые access → 401) + отзывает refresh-токены,
        статус НЕ меняется. Права: hub.admin (любого) / company-admin (member
        своей компании) / self. Ответ — ``UserListItemDTO``.
        """
        return await self._request(
            "POST", f"/users/{user_id}/revoke-sessions"
        )

    # --- D-CLI M-5: CRUD пользователей + transfer + export ---
    async def create_user(
        self,
        *,
        email: str,
        display_name: str,
        company_id: str,
        role_id: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        set_password: str | None = None,
    ) -> dict[str, Any]:
        """POST /users — создать пользователя (E12).

        Сверено с ``routes/users.py::create_user`` (:708) + ``CreateUserRequest``:
        обязательны ``email``/``display_name``/``company_id``; ``role_id``
        опционален (без него бэк назначит роль «Участник»). hub-admin создаёт
        в любой компании; company-admin — только в своей. Если email уже есть —
        добавляет membership (``is_new_user=False``). Ответ 201
        ``CreateUserResponse`` = ``{user_id, is_new_user}``.
        Опциональные поля (None) не шлём — pydantic применит свои дефолты.
        """
        body: dict[str, Any] = {
            "email": email,
            "display_name": display_name,
            "company_id": company_id,
        }
        if role_id is not None:
            body["role_id"] = role_id
        if first_name is not None:
            body["first_name"] = first_name
        if last_name is not None:
            body["last_name"] = last_name
        if set_password is not None:
            body["set_password"] = set_password
        return await self._request("POST", "/users", json=body)

    async def update_user(
        self, user_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /users/{id} — merge-patch пользователя (E12, RFC 7396).

        Сверено с ``routes/users.py::update_user`` (:777) + ``UpdateUserRequest``:
        изменяемые поля — ``display_name``/``first_name``/``last_name``/
        ``status`` (active|invited|suspended)/``user_metadata``;
        ``app_metadata`` — только hub-admin. Шлём только переданные поля
        (caller собирает dict без None). Ответ — ``UserListItemDTO``.
        """
        return await self._request("PATCH", f"/users/{user_id}", json=payload)

    async def delete_user(self, user_id: str) -> None:
        """DELETE /users/{id} — soft-delete пользователя (E12).

        Сверено с ``routes/users.py::delete_user`` (:837): без body; право
        hub.admin (любого) / company-admin (member своей компании); self-delete
        запрещён (409). Ответ 204 → None. Side-effect: refresh-токены revoked.
        """
        return await self._request("DELETE", f"/users/{user_id}")

    async def transfer_user(
        self,
        user_id: str,
        *,
        new_company_id: str,
        new_role_id: str,
        keep_old_membership: bool = False,
    ) -> dict[str, Any]:
        """POST /users/{id}/transfer — перенести в другую компанию (E12).

        Сверено с ``routes/users.py::transfer_user`` (:881) +
        ``TransferUserRequest``: body
        ``{new_company_id, new_role_id, keep_old_membership}``; hub-admin only.
        По умолчанию старые memberships удаляются (``keep_old_membership=False``).
        Ответ — ``UserListItemDTO``.
        """
        return await self._request(
            "POST",
            f"/users/{user_id}/transfer",
            json={
                "new_company_id": new_company_id,
                "new_role_id": new_role_id,
                "keep_old_membership": keep_old_membership,
            },
        )

    async def export_users(
        self,
        *,
        company_id: str | None = None,
        q: str | None = None,
        status: str | None = None,
        ids: list[str] | None = None,
    ) -> str:
        """GET /users/export.csv — CSV-выгрузка пользователей (hub.admin).

        Сверено с ``routes/users.py::export_users_csv`` (:1498): hub.admin only;
        фильтры зеркалят ``GET /users`` (``email``→``q`` по подстроке тут не
        поддержан — у export свой ``email`` query, поэтому ``q`` шлём как
        ``email``), ``status`` (alias), ``company_id``; ``ids`` (непустой) —
        режим «выгрузить выбранных» (прочие фильтры игнорируются). Ответ —
        ``text/csv`` (НЕ JSON) → возвращаем сырой текст, а не dict.
        Columns: id, email, first_name, last_name, status, last_login_at,
        created_at.
        """
        params: dict[str, Any] = {}
        if q:
            params["email"] = q
        if status:
            params["status"] = status
        if company_id:
            params["company_id"] = company_id
        if ids:
            params["ids"] = ids
        resp = await self._transport.request(
            "GET",
            "/users/export.csv",
            params=params,
            headers=self._auth_headers(),
        )
        if resp.status_code >= 400:
            raise self._parse_error_response(resp)
        return resp.text

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

    # --- D-CLI M-4: каталог permissions + права роли ---
    async def list_permissions(
        self, *, q: str | None = None
    ) -> dict[str, Any]:
        """GET /permissions — каталог всех прав (любой авторизованный).

        Сверено с ``routes/permissions.py::list_permissions`` (:111): ответ —
        канон-обёртка ``{items,total,page,size}`` (REST-07). ``q`` — фильтр по
        slug/label/описанию. ``size`` не шлём — backend default ``size=0`` =
        «без пагинации, отдать весь каталог одним запросом» (нужно для
        выбора прав роли). Item: ``key``/``slug``/``label``/``description``/
        ``scope``/``is_system``/``used_by_roles_count``.
        """
        params: dict[str, Any] = {}
        if q is not None:
            params["q"] = q
        return await self._request("GET", "/permissions", params=params)

    async def list_role_permissions(
        self, role_id: str
    ) -> list[dict[str, Any]]:
        """GET /roles/{id}/permissions — права, привязанные к роли (E5).

        Сверено с ``routes/permissions.py::list_role_permissions`` (:169):
        любой авторизованный; ответ — **плоский** ``list[PermissionDTO]`` (НЕ
        обёрнут в ``{items}``). 404 NOT_FOUND если роли нет.
        """
        return await self._request("GET", f"/roles/{role_id}/permissions")

    async def set_role_permissions(
        self, role_id: str, *, permission_slugs: list[str]
    ) -> list[dict[str, Any]]:
        """PUT /roles/{id}/permissions — replace-set прав роли (REST-16 канон).

        Сверено с ``routes/permissions.py::put_role_permissions`` (:253) +
        ``UpdateRolePermissionsRequest``: body принимает ``permission_ids`` ИЛИ
        ``permission_slugs`` — CLI оперирует человекочитаемыми slug'ами
        (``skill.publish`` и т.п.), бэк резолвит их в ids. Идемпотентная полная
        замена набора. Право: hub.admin (bypass) ИЛИ role.manage в своей
        компании (нельзя выдавать system-scope права / править global-роли).
        Ответ — **плоский** ``list[PermissionDTO]`` (новый набор).
        """
        return await self._request(
            "PUT",
            f"/roles/{role_id}/permissions",
            json={"permission_slugs": permission_slugs},
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
        resp = await self._transport.request(
            "POST",
            f"/skills/{skill_id}/comments/multipart",
            data=data,
            files=files,
            headers=self._auth_headers(),
        )
        if resp.status_code >= 400:
            raise self._parse_error_response(resp)
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
        resp = await self._transport.request(
            "POST",
            f"/support/tickets/{ticket_id}/messages/multipart",
            data=data,
            files=files,
            headers=self._auth_headers(),
        )
        if resp.status_code >= 400:
            raise self._parse_error_response(resp)
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
        page: int | None = None,
        size: int | None = None,
        sort: str | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        """GET /collections — список коллекций (server-paged, канон /tags).

        M-6: добавлены ``page``/``size``/``sort``/``q`` — server-side offset-
        пагинация (``routes/collections.py::list_collections``, :247) → ответ
        ``{items,total,page,size}``. ``sort`` — title|created|updated; ``q`` —
        подстрока по названию/slug. Неуказанные параметры не шлём — backend
        применит свои дефолты (page=1, size=DEFAULT, sort=id ASC).
        """
        params: dict[str, Any] = {"include_global": str(include_global).lower()}
        if company_id:
            params["company_id"] = company_id
        if type:
            params["type"] = type
        if owner_id:
            params["owner_id"] = owner_id
        if page is not None:
            params["page"] = page
        if size is not None:
            params["size"] = size
        if sort is not None:
            params["sort"] = sort
        if q is not None:
            params["q"] = q
        return await self._request("GET", "/collections", params=params)

    async def get_collection(self, slug: str) -> dict[str, Any]:
        """GET /collections/{slug} — детали + skills."""
        return await self._request("GET", f"/collections/{slug}")

    # --- D-CLI M-2: серверный CRUD коллекций ---
    async def create_collection(
        self,
        *,
        title: str,
        type: str = "static",
        slug: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        company_id: str | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /collections — создать серверную коллекцию (201).

        Сверено с ``routes/collections.py::create_collection`` (:473) +
        ``CreateCollectionRequest``: обязателен ``title`` + ``type``
        (static|dynamic); ``slug`` опционален (None ⇒ адресация по id);
        ``company_id`` None ⇒ global, иначе tenant-scoped (member может только
        в своей компании). Право ``collection.create``/hub.admin. Ответ —
        ``CollectionDTO``. Опциональные поля (None) не шлём.
        """
        body: dict[str, Any] = {"title": title, "type": type}
        if slug is not None:
            body["slug"] = slug
        if description is not None:
            body["description"] = description
        if icon is not None:
            body["icon"] = icon
        if company_id is not None:
            body["company_id"] = company_id
        if parent_id is not None:
            body["parent_id"] = parent_id
        return await self._request("POST", "/collections", json=body)

    async def add_skill_to_collection(
        self, slug: str, skill_id: str
    ) -> dict[str, Any]:
        """POST /collections/{slug}/skills — добавить навык в (static) коллекцию.

        Сверено с ``routes/collections.py::add_skill_to_collection`` (:778) +
        ``AddSkillToCollectionRequest``: body ``{skill_id}`` — числовой id
        (caller резолвит slug заранее). Право ``collection.update``/owner/
        hub.admin (через ``_ensure_visible``). Ответ 201
        ``CollectionSkillLinkResponse`` = ``{collection_slug, collection_id,
        skill_id}``.
        """
        return await self._request(
            "POST",
            f"/collections/{slug}/skills",
            json={"skill_id": skill_id},
        )

    async def remove_skill_from_collection(
        self, slug: str, skill_id: str
    ) -> None:
        """DELETE /collections/{slug}/skills/{skill_id} — убрать навык (204).

        Сверено с ``routes/collections.py::remove_skill_from_collection``
        (:831): path-сегмент ``{skill_id}`` принимает id-ИЛИ-slug (backend
        резолвит); idempotent (нет навыка → no-op 204). Ответ 204 → None.
        """
        return await self._request(
            "DELETE", f"/collections/{slug}/skills/{skill_id}"
        )

    async def set_collection_tags(
        self, slug: str, *, tag_ids: list[str]
    ) -> None:
        """PUT /collections/{slug}/tags — задать набор тегов коллекции (204).

        Сверено с ``routes/collections.py::set_collection_tags`` (:869) +
        ``SetCollectionTagsRequest``: body ``{tag_ids}`` — СТРОГО числовые id
        (бэк 422 на нечисловые). Replace-set (полная замена). Право
        ``collection.update``/owner/hub.admin. Ответ 204 → None.
        """
        return await self._request(
            "PUT",
            f"/collections/{slug}/tags",
            json={"tag_ids": tag_ids},
        )

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

    # --- P1 onboard ---
    async def search_skills(
        self,
        *,
        q: str,
        size: int = 20,
        page: int = 1,
        channel: str = "published",
    ) -> dict[str, Any]:
        """GET /skills?q=…&size=…&page=… — bounded-поиск каталога.

        Сверено с ``routes/skills.py::list_skills`` (W5-пагинация, :234):
        ``q`` — подстрока title/slug/description (case-insensitive), ``page``
        1-based, ``size`` капится бэком на 200 (422 PAGE_SIZE_TOO_LARGE),
        ``channel`` как в ``list_skills``. Ответ — ``SkillListResponse``
        ``{items, total, page, size}``.
        """
        return await self._request(
            "GET",
            "/skills",
            params={"q": q, "size": size, "page": page, "channel": channel},
        )

    async def search_skills_semantic(
        self, *, query: str, top_k: int = 10
    ) -> dict[str, Any]:
        """POST /skills/search-semantic — семантический подбор навыков (#242).

        Сверено с ``routes/skills.py::search_skills_semantic``: тело
        ``{query, top_k}`` (``top_k`` капится бэком на 50), ответ —
        ``{query, matches: [{skill_id, slug, score, reason, title}]}``. RBAC
        тот же, что у ``GET /skills`` (видимость несёт backend); требует логина
        для restricted-видимости, анонимно отдаёт только public.
        """
        return await self._request(
            "POST",
            "/skills/search-semantic",
            json={"query": query, "top_k": top_k},
        )
