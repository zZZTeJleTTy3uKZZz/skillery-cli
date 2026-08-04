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
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
from librarykit.errors import CliError as _LkCliError
from librarykit.errors import TransportError as _LkTransportError
from librarykit.retry import RetryPolicy, SimpleRetryPolicy
from librarykit.transport import HttpxTransport

from skillery_cli import __version__ as _CLI_VERSION
from skillery_cli import _branding

RefreshCallback = Callable[[], Awaitable[tuple[str, str] | None]]
"""() -> (new_access, new_refresh) | None — None если refresh не получился."""

def device_name() -> str:
    """Имя текущей машины (hostname) — ЕДИНЫЙ источник для User-Agent и
    регистрации устройства (/me/devices). Совпадение критично: backend
    связывает CLI-сессию с конкретным устройством по имени в UA (per-device
    «Переподключить»). Обрезка до 120 симв, дефолт ``cli`` — как при register.
    """
    import socket

    return (socket.gethostname() or "").strip()[:120] or "cli"


# H-5 + device-match: User-Agent = "skillery-cli/{ver} (id:{uid}; {hostname})".
# Префикс (dist-имя) сохранён ⇒ backend ставит client_type='cli'. В скобках —
# СТАБИЛЬНЫЙ client_device_id (``id:<uid>``) для связки сессия↔устройство (не
# рвётся при переименовании) + hostname для читабельности/legacy-фолбэка.
def _build_user_agent() -> str:
    from skillery_cli.core.identity import device_uid

    return f"{_branding.DIST_NAME}/{_CLI_VERSION} (id:{device_uid()}; {device_name()})"


USER_AGENT = _build_user_agent()


def _device_id() -> str:
    """Стабильный ``client_device_id`` этой машины (``~/.skillery``).

    #1452: с переездом очереди в device-пространство идентификатор стал частью
    ПУТИ (``/devices/{cdid}/tasks``), а не маркером в User-Agent. Тот же
    источник, что и при регистрации устройства и в рапортах, — иначе демон
    ходил бы в очередь одной машины, а рапортовал за другую.
    """
    from skillery_cli.core.identity import device_uid

    return device_uid()

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
        # Храним для ДИАГНОСТИКИ сети: голый «ConnectError: » без хоста ничего не
        # говорит о том, куда именно не достучались (см. ``_network_error``).
        self._base_url = base_url or ""
        trust_env = bool(
            int(os.environ.get(_branding.env("USE_SYSTEM_PROXY"), "0") or "0")
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

    def _describe_target(self, method: str, url: str) -> str:
        """``METHOD scheme://host/path`` — БЕЗ query и без заголовков.

        Диагностика сетевого сбоя без утечки секретов: query может нести токены/
        коды, поэтому его отрезаем; путь и хост — то, чего не хватало в логах
        («ConnectError: » без адреса не позволял понять, куда не достучались).
        """
        from urllib.parse import urlsplit

        raw = url if "://" in url else f"{self._base_url.rstrip('/')}/{url.lstrip('/')}"
        try:
            parts = urlsplit(raw)
        except Exception:  # noqa: BLE001 — диагностика не должна падать сама
            return f"{method.upper()} {raw.split('?', 1)[0]}"
        host = parts.netloc or "?"
        scheme = parts.scheme or "https"
        return f"{method.upper()} {scheme}://{host}{parts.path}"

    def _network_error(
        self, method: str, url: str, exc: BaseException
    ) -> _LkTransportError:
        """Внятная сетевая ошибка: куда шли, какой тип, какая причина.

        Живой кейс: транспорт кита отдавал ``TransportError: ConnectError: `` —
        текст httpx-исключения ПУСТОЙ, хоста нет, тип причины не виден. По такому
        сообщению нельзя отличить «DNS не резолвится» от «TLS отвалился» и
        непонятно, к какому хосту это относится. Собираем всё в одну строку.
        """
        detail = str(exc).strip()
        # Пустой/обрубленный текст («ConnectError: ») → добавляем repr (он несёт
        # имя класса) — иначе в логе остаётся строка вообще без содержания.
        if not detail or detail.endswith(":"):
            detail = f"{detail} {exc!r}".strip()
        cause = exc.__cause__ or exc.__context__
        if cause is not None and cause is not exc:
            cause_text = str(cause).strip() or repr(cause)
            detail = f"{detail} (причина: {type(cause).__name__}: {cause_text})"
        return _LkTransportError(
            f"сеть недоступна: {self._describe_target(method, url)} — {detail}"
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
            except (_LkTransportError, httpx.TransportError) as exc:
                # Сетевой сбой: транспорт кита обернул httpx-ошибку в доменный
                # ``librarykit.errors.TransportError`` (голый httpx — если запрос
                # ушёл мимо кита). Идемпотентные методы повторяем (W1), иначе —
                # пробрасываем ОБОГАЩЁННУЮ доменную ошибку наверх: хост+путь+тип
                # причины, иначе в логе оставался бесполезный «ConnectError: ».
                if idempotent and policy.should_retry(attempt, None, exc):
                    await asyncio.sleep(policy.delay(attempt))
                    attempt += 1
                    continue
                raise self._network_error(method, url, exc) from exc
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

    async def get_me(self) -> dict:
        """GET /me — профиль актора (display_name, email, id, …).

        Нужен, чтобы в CLI показывать ИМЯ пользователя, а не числовой id: JWT
        несёт только ``sub`` (числовой user_id), поэтому имя/почту берём из /me.
        """
        return await self._request("GET", "/me")

    async def report_cli_log(
        self,
        *,
        level: str,
        message: str,
        logger: str,
        context: dict | None = None,
    ) -> None:
        """#1024: отправить лог действия CLI в веб /logs (source=cli).

        Best-effort: телеметрия не должна ломать команду. Actor резолвится
        бэкендом из Bearer — действие привязывается к пользователю в вебе.

        Исторический ОДИНОЧНЫЙ контракт (level/message/logger/context) не
        меняется — старые вызовы работают как раньше; батч-канал C3 живёт в
        :meth:`report_cli_logs`.
        """
        await self.report_cli_logs([
            {
                "level": level,
                "message": message,
                "logger": logger,
                "context": context or {},
            }
        ])

    async def report_cli_logs(self, items: list[dict]) -> dict:
        """C3 (#1099): батч-отправка буферизованных логов CLI (``POST /cli-logs``).

        Один запрос на пачку записей. Бэкенд принимает до 200 элементов; лимит
        батча держит клиент. Элемент может нести клиентский ``ts`` — время
        СОБЫТИЯ, а не приёма (важно для записей, пролежавших в офлайн-буфере).
        Старый бэкенд, не знающий ``ts``, просто игнорирует лишнее поле.

        ⚠️ #1174: CLI этим каналом БОЛЬШЕ НЕ ПОЛЬЗУЕТСЯ — логи едут конвертами
        ``kind="log"`` через общий outbox на :meth:`send_telemetry_batch`. Метод
        (и одиночный :meth:`report_cli_log`) оставлен как есть ради обратной
        совместимости: на нём сидят уже установленные старые CLI.
        """
        return await self._request(
            "POST", "/cli-logs", json={"items": list(items)}
        )

    async def send_telemetry_batch(self, envelopes: list[dict]) -> dict:
        """#1174: батч ОБЩЕГО outbox'а одним запросом (``POST /telemetry/batch``).

        Единый канал исходящего для ВСЕХ локальных продюсеров: телеметрия
        вызовов навыков (``kind="skill_run"``, пишет ``telemetrykit``), логи CLI
        (``kind="log"``, пишет ``core.log_sync``) и то, что появится дальше.
        Конверт на проводе — ДОСЛОВНО контракт бэкенда::

            {"envelopes": [{"id", "kind", "ts", "schema_version", "payload"}]}
            → {"accepted": ["id", …], "rejected": [{"id": …, "reason": …}]}

        Семантику ответа разбирает :mod:`skillery_cli.core.outbox_worker`: из
        очереди удаляются И ``accepted``, И ``rejected`` (отклонённый конверт от
        повтора валидным не станет, а вставшая очередь теряет всё остальное).

        Не путать с :meth:`report_cli_logs` — тот исторический канал
        ``POST /cli-logs`` остаётся ради обратной совместимости со старыми CLI.
        """
        data = await self._request(
            "POST", "/telemetry/batch", json={"envelopes": list(envelopes)}
        )
        # 204/пустое тело у старого бэкенда → пустой словарь: воркер тогда
        # ничего не удалит и повторит батч, а не потеряет его молча.
        return data if isinstance(data, dict) else {}

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
        self, *, name: str, platform: str, client_device_id: str | None = None
    ) -> dict[str, Any]:
        """POST /me/devices — регистрация CLI-устройства (E-D web↔CLI мост).

        Сверено с ``routes/me.py::register_device``: body
        ``{name, platform, client_device_id?}``, auth, 201. Upsert по
        client_device_id (стабильный, переживает ренейм); name — визуальный
        лейбл. Legacy backend без поля client_device_id просто его игнорирует."""
        body: dict[str, Any] = {"name": name, "platform": platform}
        if client_device_id:
            body["client_device_id"] = client_device_id
        return await self._request("POST", "/me/devices", json=body)

    async def fetch_device_queue_full(
        self,
        *,
        auto_update: bool | None = None,
        wait: int = 0,
        supports_removal: bool = True,
    ) -> dict[str, Any]:
        """GET /devices/{cdid}/tasks — ПОЛНЫЙ ответ очереди устройства (#905/#1102).

        Ответ: ``{"items": [...skill-очередь...], "device_tasks": [...]}``.

        ``items`` — задания навыков ``{skill_id, slug, desired_version,
        applied_version, status, action}`` (как раньше). ``device_tasks`` —
        обобщённые задачи устройства ``{id, task_type, payload, status}``
        (``cli_upgrade``/``skill_update``/``generic``); для ``cli_upgrade``
        ``payload = {"target_version": "x.y.z"}``. Задача переотдаётся, пока не
        отрапортован терминальный статус (см. :meth:`report_device_task`).

        #1452: устройство — СЕГМЕНТ ПУТИ, а не догадка сервера по ``id:{cdid}``
        в User-Agent. Очередь принадлежит машине, а не человеку: машин
        несколько, у каждой своя, и прежний ``/me/device-queue`` вообще не умел
        адресовать вторую. ``client_device_id`` демон и так знает — им же он
        регистрируется и рапортует.

        #919: заодно сообщаем, включено ли автообновление НА ЭТОЙ машине.

        #13: ``supports_removal`` заявляет серверу, что этот CLI УМЕЕТ снимать
        навыки — иначе removal-задания (``action=remove``) сервер нам не отдаёт
        (старый CLI без флага переустановил бы навык вместо снятия). Дефолт True:
        любая версия с этим методом снятие поддерживает.

        ``wait>0`` — LONG-POLL: сервер держит запрос открытым до <wait> сек, пока
        не появится задание (мгновенная доставка). Таймаут HTTP-клиента поднимаем
        выше wait (иначе клиент отвалится РАНЬШЕ ответа сервера). Старый сервер
        без поддержки ?wait просто вернёт очередь сразу (обратно совместимо), а
        без поля ``device_tasks`` — вернём пустой список (обратно совместимо).
        """
        from urllib.parse import urlencode

        params: dict[str, str] = {}
        if auto_update is not None:
            params["auto_update"] = "true" if auto_update else "false"
        if wait > 0:
            params["wait"] = str(int(wait))
        if supports_removal:
            params["supports_removal"] = "true"
        path = f"/devices/{_device_id()}/tasks"
        if params:
            path += "?" + urlencode(params)
        # ⚠️ per-request timeout НЕ пробрасываем через транспорт кита
        # (HttpxTransport.request его не принимает → TypeError, long-poll падал бы
        # молча). Таймаут задаётся при СОЗДАНИИ HubClient (см. вызов в демоне:
        # timeout=wait+буфер), поэтому здесь ничего не передаём.
        data = await self._request("GET", path)
        if not isinstance(data, dict):
            # Совсем старый backend мог вернуть голый список заданий.
            return {"items": list(data or []), "device_tasks": []}
        return {
            "items": list(data.get("items", []) or []),
            "device_tasks": list(data.get("device_tasks", []) or []),
        }

    async def fetch_device_queue(
        self,
        *,
        auto_update: bool | None = None,
        wait: int = 0,
        supports_removal: bool = True,
    ) -> list[dict[str, Any]]:
        """GET /me/device-queue — ТОЛЬКО skill-очередь (тонкая обёртка, #905).

        Обратная совместимость: возвращает список ``items`` (device_tasks
        игнорирует). Демон использует :meth:`fetch_device_queue_full`, чтобы из
        ОДНОГО ответа получить и skill-очередь, и device_tasks.
        """
        data = await self.fetch_device_queue_full(
            auto_update=auto_update, wait=wait, supports_removal=supports_removal
        )
        return list(data.get("items", []))

    async def stream_device_queue(
        self,
        *,
        last_event_id: str | None = None,
        auto_update: bool | None = None,
        supports_removal: bool = True,
    ) -> AsyncIterator[tuple[str, dict[str, Any], str | None]]:
        """GET /devices/{cdid}/tasks/stream (SSE) → ``(event, data, event_id)`` (#1191).

        PUSH-канал очереди устройства вместо постоянного long-poll'а:

        - события ``queue`` — тело идентично ответу ``/me/device-queue``
          (``items`` + ``device_tasks``), у каждого есть ``id:`` — КУРСОР;
        - события ``ping`` — heartbeat (не реже 20с), задачей НЕ является;
        - первое ``queue`` приходит сразу при подключении.

        ``last_event_id`` уезжает заголовком ``Last-Event-ID`` — сервер добирает
        из БД всё, что мы пропустили за время разрыва (задачи не теряются).

        ⚠️ Токен идёт ТОЛЬКО заголовком ``Authorization`` — в query его класть
        нельзя (утечёт в логи прокси/сервера).

        ⚠️ Таймаут чтения задан на КОНСТРУКЦИИ клиента (транспорт кита не
        принимает per-request ``timeout=`` — это уже приводило к молчаливому
        TypeError и «вечно офлайн» устройству). Для SSE его берут > heartbeat'а
        (см. ``daemon/queue_stream.py``): молчание дольше таймаута = мёртвый
        коннект, читатель уйдёт на реконнект.

        Старый backend без эндпоинта отдаст 404/405 — метод бросит
        :class:`ApiError` с этим статусом, вызывающий переключается на long-poll.
        """
        from urllib.parse import urlencode

        params: dict[str, str] = {}
        if auto_update is not None:
            params["auto_update"] = "true" if auto_update else "false"
        if supports_removal:
            params["supports_removal"] = "true"
        path = f"/devices/{_device_id()}/tasks/stream"
        if params:
            path += "?" + urlencode(params)
        client = self._transport._client  # httpx.AsyncClient кита (base_url задан)

        def _headers() -> dict[str, str]:
            h = self._auth_headers()
            h["Accept"] = "text/event-stream"
            # Прокси/CDN не должны буферизовать поток — иначе события копятся.
            h["Cache-Control"] = "no-store"
            if last_event_id:
                h["Last-Event-ID"] = str(last_event_id)
            return h

        # До 2 попыток: первая; при 401 с рабочим refresh — вторая с новым токеном.
        for attempt in range(2):
            try:
                async with client.stream("GET", path, headers=_headers()) as resp:
                    if (
                        resp.status_code == 401
                        and self._on_refresh is not None
                        and attempt == 0
                    ):
                        await resp.aread()
                        new_tokens = await self._on_refresh()
                        if new_tokens is not None:
                            self._access_token = new_tokens[0]
                            continue  # повтор с новым токеном
                    if resp.status_code >= 400:
                        await resp.aread()
                        raise self._parse_error_response(resp)
                    event: str | None = None
                    event_id: str | None = None
                    data_lines: list[str] = []
                    async for raw in resp.aiter_lines():
                        line = raw.rstrip("\r")
                        if not line:
                            # Пустая строка — конец SSE-кадра: отдаём собранное.
                            if data_lines:
                                payload = "\n".join(data_lines)
                                try:
                                    data = json.loads(payload)
                                except json.JSONDecodeError:
                                    data = {}
                                if not isinstance(data, dict):
                                    data = {}
                                yield (event or "message"), data, event_id
                            event, data_lines = None, []
                            continue
                        if line.startswith(":"):
                            continue  # SSE-комментарий (тоже keep-alive)
                        if line.startswith("event:"):
                            event = line[len("event:"):].strip()
                        elif line.startswith("id:"):
                            # Курсор «липкий» по спеке SSE — держим до смены.
                            event_id = line[len("id:"):].strip() or event_id
                        elif line.startswith("data:"):
                            data_lines.append(line[len("data:"):].lstrip())
                    return  # стрим закрыт сервером — читатель решит про реконнект
            except (httpx.TransportError, _LkTransportError) as e:
                # Обрыв/недоступность стрима — доменная ошибка (её ловит
                # SSE-клиент демона и уходит на реконнект/fallback), а не сырой
                # httpx-traceback.
                raise self._network_error("GET", path, e) from e

    async def report_device_apply(
        self,
        *,
        slug: str,
        ok: bool,
        version: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """POST /devices/{cdid}/skills/{slug}/report — рапорт о ФАКТЕ (#905).

        Успех обязан нести версию, которая реально легла на диск; провал —
        текст ошибки (задание останется в очереди и повторится).

        #1452: переехал из ``/me/device-queue/report`` в device-пространство
        вместе с очередью, навык — из тела в путь.

        ⚠️ Это НЕ дубль :meth:`report_device_task` и объединять их нельзя:
        здесь рапортуется состояние НАВЫКА на машине (``device_skill_state`` —
        какая версия легла на диск), там — исход типизированной задачи
        (``device_task``: ``cli_upgrade`` и прочие, у них навыка нет вовсе).
        Демон зовёт оба.
        """
        body: dict[str, Any] = {"ok": bool(ok), "slug": slug}
        if version:
            body["version"] = version
        if error:
            body["error"] = error[:500]
        return await self._request(
            "POST", f"/devices/{_device_id()}/skills/{slug}/report", json=body
        )

    async def report_device_task(
        self,
        *,
        client_device_id: str,
        task_id: int,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        """POST /devices/{cdid}/tasks/{task_id}/report — рапорт о device-task (#1102).

        ``status`` — терминальный: ``applied`` (задача выполнена/запущена) либо
        ``failed`` (с краткой причиной). Пока терминальный статус не отрапортован,
        backend переотдаёт задачу в очереди. Причину обрезаем до 500 символов
        (секреты сюда не кладём — только текст ошибки шага).
        """
        body: dict[str, Any] = {"status": status}
        if error:
            body["error"] = error[:500]
        return await self._request(
            "POST",
            f"/devices/{client_device_id}/tasks/{task_id}/report",
            json=body,
        )

    async def list_devices(self) -> list[dict[str, Any]]:
        """GET /me/devices — список зарегистрированных устройств пользователя.

        Сверено с ``routes/me.py::list_my_devices``: требуется auth (Bearer),
        200. Возвращает список устройств с полями: id, client_device_id,
        device_name, platform, online, is_current, last_seen_at, session_active.

        Ответ обёрнут (``{"devices": [...]}``) — разворачиваем здесь, иначе
        вызывающий код итерирует по КЛЮЧАМ словаря (``skillery devices`` падал
        с «'str' object has no attribute 'get'»). Голый список тоже принимаем.
        """
        data = await self._request("GET", "/me/devices")
        if isinstance(data, dict):
            return list(data.get("devices") or [])
        return list(data or [])

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

    async def exchange_redeem(self, *, code: str) -> dict[str, Any]:
        """POST /auth/exchanges/{code}/redeem — редимит code на новую сессию.

        Для CLI (browser-flow): GET /cli-login?port=…&state=… запускает browser-flow,
        который редеемит code с include_refresh=true → получается refresh_token в body
        (не в cookie), для CLI-сохранения.

        Body: {"code": code, "include_refresh": true}.
        Returns: {"access_token": "...", "refresh_token": "...", ...}.
        """
        return await self._request(
            "POST",
            f"/auth/exchanges/{code}/redeem",
            json={"code": code, "include_refresh": True},
        )

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

    async def download_snapshot(self, ref: str, semver: str) -> bytes | None:
        """GET /skills/{ref}/versions/{semver}/snapshot — tar.gz слепок версии.

        Бэкенд отдаёт содержимое версии, ПРЕДварительно материализованное
        server-side (sync снимает архив в object_storage под токеном хаба).
        Значит установка не требует клиентских git-кред и прямого доступа к
        приватному репо. Возвращает сырые байты tar.gz либо ``None`` (404 — для
        этой версии слепка нет; вызывающий откатывается на git clone)."""
        headers = self._auth_headers()
        headers["Accept"] = "application/gzip"
        url = f"/skills/{ref}/versions/{semver}/snapshot"
        resp = await self._send("GET", url, headers)
        if resp.status_code == 401 and self._on_refresh is not None:
            new_tokens = await self._on_refresh()
            if new_tokens is not None:
                self._access_token = new_tokens[0]
                headers = self._auth_headers()
                headers["Accept"] = "application/gzip"
                resp = await self._send("GET", url, headers)
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise self._parse_error_response(resp)
        return resp.content

    async def advisor_stream(
        self, *, message: str, conversation_id: int | None = None
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """POST /advisor/messages (SSE) → поток ``(event, data)``.

        События: ``meta`` ({conversation_id}) → ``token`` ({text}) → ``skills``
        ({skills:[...]}) → ``done``. Тот же реальный LLM-адвайзер, что и в вебе
        (RAG по каталогу, RBAC-фильтрован). ``conversation_id=None`` ⇒ новая
        беседа (её id придёт в ``meta``)."""
        body = {"message": message, "conversation_id": conversation_id}
        client = self._transport._client  # httpx.AsyncClient кита (base_url задан)

        def _headers() -> dict[str, str]:
            h = self._auth_headers()
            h["Accept"] = "text/event-stream"
            return h

        # До 2 попыток: первая; при 401 с рабочим refresh — вторая с новым токеном.
        # ``async with`` гарантирует закрытие стрима на любом выходе (в т.ч. при
        # раннем break потребителя → GeneratorExit).
        for attempt in range(2):
            # Сетевой сбой (обрыв/недоступность) на открытии стрима или чтении
            # → чистая ApiError (её ловит команда через ``run``), а не сырой
            # httpx-traceback. ``_send``-обёртка тут не работает: SSE читаем
            # напрямую через httpx-клиент.
            try:
                async with client.stream(
                    "POST", "/advisor/messages", json=body, headers=_headers()
                ) as resp:
                    if (
                        resp.status_code == 401
                        and self._on_refresh is not None
                        and attempt == 0
                    ):
                        await resp.aread()
                        new_tokens = await self._on_refresh()
                        if new_tokens is not None:
                            self._access_token = new_tokens[0]
                            continue  # повтор с новым токеном
                    if resp.status_code >= 400:
                        await resp.aread()
                        raise self._parse_error_response(resp)
                    event: str | None = None
                    async for raw in resp.aiter_lines():
                        line = raw.rstrip("\r")
                        if not line:
                            event = None  # пустая строка — конец SSE-кадра
                            continue
                        if line.startswith("event:"):
                            event = line[len("event:"):].strip()
                        elif line.startswith("data:") and event:
                            payload = line[len("data:"):].strip()
                            try:
                                data = json.loads(payload)
                            except json.JSONDecodeError:
                                continue
                            yield event, data
                    return  # стрим успешно дочитан
            except (httpx.TransportError, _LkTransportError) as e:
                raise ApiError(
                    0, "NETWORK",
                    "Нет связи с бэкендом (адвайзер). Проверьте сеть/VPN.",
                ) from e

    async def list_my_installs(self) -> list[dict[str, Any]]:
        """GET /me/installs — навыки, помеченные актором установленными.

        Сверено с ``routes/me.py::list_my_installs``: ответ —
        ``{items: [{slug, skill_id, installed_version}]}``. Источник — install-
        события (та же истина, что install_state). Используется
        ``skillery pull`` и демоном для reconcile (знать ЧТО тянуть). Берём
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

    # --- auto-sync: git-webhook навыка ---
    async def register_skill_webhook(self, slug: str) -> dict[str, Any]:
        """POST /skills/{slug}/webhook — включить автосинк навыка.

        Ответ: ``{mode: auto|manual, provider, url, secret?}``. ``secret``
        приходит ТОЛЬКО в manual-режиме (в auto он уже у провайдера) и наружу
        из CLI не печатается — см. ``commands/webhook.py``.
        """
        return await self._request("POST", f"/skills/{slug}/webhook")

    async def get_skill_webhook(self, slug: str) -> dict[str, Any]:
        """GET /skills/{slug}/webhook — ``{status: registered|manual|none,
        provider, url}``. Секрет бэкенд не отдаёт никогда."""
        return await self._request("GET", f"/skills/{slug}/webhook")

    async def delete_skill_webhook(self, slug: str) -> None:
        """DELETE /skills/{slug}/webhook — отозвать (204, идемпотентно)."""
        await self._request("DELETE", f"/skills/{slug}/webhook")

    async def yank_skill_version(
        self, *, slug: str, semver: str, yank: bool = True
    ) -> None:
        """#340: снять/вернуть версию навыка (yank/unyank). 204 без тела.

        Путь пишется литералом на каждую ветку, а не собирается f-строкой с
        ``{action}``: контрактный тест (#1441) разбирает transport.py статически,
        и склеенный из переменной сегмент он видит как path-параметр — то есть
        именно этот вызов оставался бы вне сверки с OpenAPI.
        """
        if yank:
            await self._request("POST", f"/skills/{slug}/versions/{semver}/yank")
        else:
            await self._request("POST", f"/skills/{slug}/versions/{semver}/unyank")

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
        params: dict[str, Any] = {"format": "csv"}
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
        """POST /invites (flat).

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
        """PATCH /users/{id} ``{is_locked:true}`` — заблокировать вход.

        REST-27 (#1452): «заблокирован» — ПОЛЕ сущности; роутов-глаголов
        ``/lock`` и ``/unlock`` больше нет. Право hub.admin ИЛИ company-admin
        (``_can_admin_users``: user.lock/company.manage/...). Self-lock
        запрещён (409). Ответ — ``UserListItemDTO`` (``is_locked=True``);
        refresh-токены target'а revoked.
        """
        body: dict[str, Any] = {"is_locked": True}
        if reason is not None:
            body["lock_reason"] = reason
        return await self._request(
            "PATCH", f"/users/{user_id}", json=body
        )

    async def unlock_user(self, user_id: str) -> dict[str, Any]:
        """PATCH /users/{id} ``{is_locked:false}`` — снять блокировку.

        REST-27 (#1452): парная ветка ``lock_user``; права те же.
        Ответ — ``UserListItemDTO``.
        """
        return await self._request(
            "PATCH", f"/users/{user_id}", json={"is_locked": False}
        )

    async def reset_user_password(self, user_id: str) -> dict[str, Any]:
        """POST /users/{id}/password-resets — одноразовый пароль.

        REST-19 (#1452): ресурс «сброс пароля», не глагол в пути. Без
        body; право hub.admin ИЛИ company.manage (target должен состоять в
        компании актора). Ответ ``ResetPasswordResponse`` =
        ``{temp_password, expires_hint, requires_password_change}``.
        Пароль отдаётся ТОЛЬКО в этом ответе (в БД — хэш), все refresh-
        токены target'а revoked.
        """
        return await self._request(
            "POST", f"/users/{user_id}/password-resets"
        )

    # --- bulk suspend/activate + revoke-sessions ---
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
        """DELETE /users/{id}/sessions — «выйти со всех устройств».

        REST-19 (#1452): сессии — коллекция, «отозвать все» — её DELETE.
        Бампает session-эпоху (живые access → 401) + отзывает refresh-токены,
        статус НЕ меняется. Права: hub.admin (любого) / company-admin (member
        своей компании) / self. Ответ — 204 без тела.
        """
        return await self._request(
            "DELETE", f"/users/{user_id}/sessions"
        )

    # --- CRUD пользователей + transfer + export ---
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
        """POST /users — создать пользователя.

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
        """PATCH /users/{id} — merge-patch пользователя (RFC 7396).

        Сверено с ``routes/users.py::update_user`` (:777) + ``UpdateUserRequest``:
        изменяемые поля — ``display_name``/``first_name``/``last_name``/
        ``status`` (active|invited|suspended)/``user_metadata``;
        ``app_metadata`` — только hub-admin. Шлём только переданные поля
        (caller собирает dict без None). Ответ — ``UserListItemDTO``.
        """
        return await self._request("PATCH", f"/users/{user_id}", json=payload)

    async def delete_user(self, user_id: str) -> None:
        """DELETE /users/{id} — soft-delete пользователя.

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
        """POST /users/{id}/memberships — перенести в другую компанию.

        REST-19 (#1452): «перенести» — глагол; ресурс здесь членство.
        Body ``{company_id, role_id, keep_previous}``; hub-admin only.
        По умолчанию старые memberships удаляются (``keep_previous=False``).
        Ответ — ``UserListItemDTO``.
        """
        return await self._request(
            "POST",
            f"/users/{user_id}/memberships",
            json={
                "company_id": new_company_id,
                "role_id": new_role_id,
                "keep_previous": keep_old_membership,
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
        """GET /users?format=csv — CSV-выгрузка пользователей (hub.admin).

        REST-42a (#1452): расширение файла — не часть пути. hub.admin only;
        фильтры зеркалят ``GET /users`` (``email``→``q`` по подстроке тут не
        поддержан — у export свой ``email`` query, поэтому ``q`` шлём как
        ``email``), ``status`` (alias), ``company_id``; ``ids`` (непустой) —
        режим «выгрузить выбранных» (прочие фильтры игнорируются). Ответ —
        ``text/csv`` (НЕ JSON) → возвращаем сырой текст, а не dict.
        Columns: id, email, first_name, last_name, status, last_login_at,
        created_at.
        """
        params: dict[str, Any] = {"format": "csv"}
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
            "/users",
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

    # --- каталог permissions + права роли ---
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
        """GET /roles/{id}/permissions — права, привязанные к роли.

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

    # === Skill review (ratings / comments / contributors) ===
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

    # === Support tickets ===
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

    # === Collections ===
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

    # --- серверный CRUD коллекций ---
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

    # === Events ingestion ===
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

    # ------------------------------------------------------------------
    # #1224 — закрытие гэпов матрицы функционала (docs/ops/functional-canon.md)
    #
    # Ниже — методы под эндпоинты, которые матрица числила за CLI, но в коде
    # их не было ни одного вызова: теги, access-grants, system-config, сессии,
    # CRUD ролей, вспомогательные ручки навыков и коллекций.
    # ------------------------------------------------------------------

    # --- теги (routes/tags.py) ---
    async def list_tags(
        self,
        *,
        parent_id: str | None = None,
        include_descendants: bool = False,
        roots_only: bool = False,
        q: str | None = None,
        page: int | None = None,
        size: int | None = None,
        sort: str = "name",
        direction: str = "asc",
    ) -> dict[str, Any]:
        """GET /tags — плоский список тегов.

        Сверено с ``routes/tags.py::list_tags``: ответ — ``TagListResponse``
        ``{items,total,page,size}``. Если ``page``/``size`` не заданы, backend
        отдаёт legacy-режим (весь список до 500, ``page``/``size`` = ``null``),
        поэтому по умолчанию их НЕ шлём — CLI обычно нужен весь каталог.
        ``size`` капится бэком на 500 (422 PAGE_SIZE_TOO_LARGE).
        """
        params: dict[str, Any] = {"sort": sort, "direction": direction}
        if parent_id is not None:
            params["parent_id"] = parent_id
        if include_descendants:
            params["include_descendants"] = True
        if roots_only:
            params["roots_only"] = True
        if q is not None:
            params["q"] = q
        if page is not None:
            params["page"] = page
        if size is not None:
            params["size"] = size
        return await self._request("GET", "/tags", params=params)

    async def get_tag_tree(self) -> dict[str, Any]:
        """GET /tags/tree — иерархия тегов.

        Ответ ``TagTreeResponse{items: [{tag: TagDTO, children: [...]}]}`` —
        рекурсивно, корни на верхнем уровне. Параметров нет.
        """
        return await self._request("GET", "/tags/tree")

    async def get_tag(self, tag_id: str) -> dict[str, Any]:
        """GET /tags/{tag_id} — карточка тега (404 TAG_NOT_FOUND)."""
        return await self._request("GET", f"/tags/{tag_id}")

    async def count_tags(
        self,
        *,
        parent_id: str | None = None,
        include_descendants: bool = False,
        roots_only: bool = False,
        q: str | None = None,
    ) -> dict[str, Any]:
        """GET /tags/count — ``{count}`` по тем же фильтрам, что ``GET /tags``."""
        params: dict[str, Any] = {}
        if parent_id is not None:
            params["parent_id"] = parent_id
        if include_descendants:
            params["include_descendants"] = True
        if roots_only:
            params["roots_only"] = True
        if q is not None:
            params["q"] = q
        return await self._request("GET", "/tags/count", params=params)

    async def create_tag(
        self,
        *,
        name: str,
        description: str | None = None,
        icon: str | None = None,
        icon_color: str | None = None,
        parent_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /tags — создать тег (201, ``TagDTO``).

        Право: ``tag.create``/``tag.manage``/``hub.admin``. ``parent_id=None``
        = корневой тег.
        """
        payload: dict[str, Any] = {"name": name}
        if description is not None:
            payload["description"] = description
        if icon is not None:
            payload["icon"] = icon
        if icon_color is not None:
            payload["icon_color"] = icon_color
        if parent_id is not None:
            payload["parent_id"] = parent_id
        return await self._request("POST", "/tags", json=payload)

    async def update_tag(
        self,
        tag_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        icon_color: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /tags/{tag_id} — частичное обновление (``TagDTO``).

        Родителя тут менять НЕЛЬЗЯ — для этого отдельный
        :meth:`move_tag` (``PATCH /tags/{id}/move``), как в backend.
        """
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if icon is not None:
            payload["icon"] = icon
        if icon_color is not None:
            payload["icon_color"] = icon_color
        return await self._request("PATCH", f"/tags/{tag_id}", json=payload)

    async def move_tag(
        self, tag_id: str, *, new_parent_id: str | None
    ) -> dict[str, Any]:
        """PATCH /tags/{tag_id}/move — сменить родителя (``None`` = в корень).

        ``new_parent_id`` шлём ВСЕГДА, в том числе ``null``: пропуск ключа и
        ``null`` для backend значат разное (не менять / в корень).
        """
        return await self._request(
            "PATCH",
            f"/tags/{tag_id}/move",
            json={"new_parent_id": new_parent_id},
        )

    async def delete_tag(self, tag_id: str) -> None:
        """DELETE /tags/{tag_id} — удалить тег (204). Право ``tag.manage``."""
        await self._request("DELETE", f"/tags/{tag_id}")

    async def bulk_delete_tags(self, *, ids: list[str]) -> dict[str, Any]:
        """POST /tags/bulk/delete — удалить пачку тегов по id.

        Backend принимает ЛИБО ``ids``, ЛИБО ``filter`` (последний только
        вместе с ``all=true``). CLI сознательно отдаёт только явный список id:
        массовое удаление «по фильтру» из терминала слишком легко выстреливает
        в ногу. Ответ ``BulkResultResponse{processed,updated,skipped_ids,errors}``.
        """
        return await self._request(
            "POST", "/tags/bulk/delete", json={"ids": ids}
        )

    async def bulk_move_tags(
        self, *, ids: list[str], new_parent_id: str | None
    ) -> dict[str, Any]:
        """POST /tags/bulk/move — перевесить пачку тегов под нового родителя."""
        return await self._request(
            "POST",
            "/tags/bulk/move",
            json={"ids": ids, "new_parent_id": new_parent_id},
        )

    async def get_entity_tags(
        self, *, entity_type: str, entity_id: str
    ) -> dict[str, Any]:
        """GET /tags/assignments — теги, навешенные на сущность.

        ``entity_type`` ∈ ``skill|collection|company|user``. Ответ
        ``EntityTagsResponse{entity_type, entity_id, tags: [TagRef]}``.
        """
        return await self._request(
            "GET",
            "/tags/assignments",
            params={"entity_type": entity_type, "entity_id": entity_id},
        )

    async def set_entity_tags(
        self, *, entity_type: str, entity_id: str, tag_ids: list[str]
    ) -> dict[str, Any]:
        """PUT /tags/assignments — REPLACE-SET тегов сущности (не добавление).

        Пустой ``tag_ids`` снимает все теги. Право — как у создания тега.
        """
        return await self._request(
            "PUT",
            "/tags/assignments",
            json={
                "entity_type": entity_type,
                "entity_id": entity_id,
                "tag_ids": tag_ids,
            },
        )

    # --- access-grants (routes/access_grants.py) ---
    async def list_skill_access_grants(self, skill_id: str) -> dict[str, Any]:
        """GET /skills/{skill_id}/access-grants — кому выдан доступ к навыку.

        Право: ``skill.manage``/``hub.admin`` (эта GET-ручка гейтится, в
        отличие от большинства чтений). Ответ
        ``{skill_id, grants: [SkillAccessGrantDTO]}``.
        """
        return await self._request("GET", f"/skills/{skill_id}/access-grants")

    async def grant_skill_access(
        self,
        skill_id: str,
        *,
        target_type: str,
        target_id: str,
        role: str = "viewer",
    ) -> dict[str, Any]:
        """PUT /skills/{skill_id}/access-grants — выдать доступ (идемпотентно).

        ``target_type`` ∈ ``company|user|tag``; ``role`` ∈
        ``viewer|editor|admin``. Повторный PUT на тот же таргет возвращает
        существующий grant (тоже 201) — операция идемпотентна.
        """
        return await self._request(
            "PUT",
            f"/skills/{skill_id}/access-grants",
            json={
                "target_type": target_type,
                "target_id": target_id,
                "role": role,
            },
        )

    async def revoke_skill_access(self, skill_id: str, grant_id: str) -> None:
        """DELETE /skills/{skill_id}/access-grants/{grant_id} — отозвать (204)."""
        await self._request(
            "DELETE", f"/skills/{skill_id}/access-grants/{grant_id}"
        )

    async def list_collection_access_grants(
        self, collection_id: str
    ) -> dict[str, Any]:
        """GET /collections/{id}/access-grants — кому выдан доступ к коллекции."""
        return await self._request(
            "GET", f"/collections/{collection_id}/access-grants"
        )

    async def grant_collection_access(
        self,
        collection_id: str,
        *,
        target_type: str,
        target_id: str,
        role: str = "viewer",
    ) -> dict[str, Any]:
        """PUT /collections/{id}/access-grants — выдать доступ к коллекции.

        Гейт у мутаций коллекции ДРУГОЙ, чем у навыка: владелец коллекции
        ИЛИ ``collection.update.any`` ИЛИ ``hub.admin``.
        """
        return await self._request(
            "PUT",
            f"/collections/{collection_id}/access-grants",
            json={
                "target_type": target_type,
                "target_id": target_id,
                "role": role,
            },
        )

    async def revoke_collection_access(
        self, collection_id: str, grant_id: str
    ) -> None:
        """DELETE /collections/{id}/access-grants/{grant_id} — отозвать (204)."""
        await self._request(
            "DELETE", f"/collections/{collection_id}/access-grants/{grant_id}"
        )

    # --- system config (routes/system_config.py) ---
    #
    # ВНИМАНИЕ: путь — ``/system/config`` (через слэш), а НЕ ``/system-config``.
    # В матрице функционала он записан через дефис — это ошибка документа.
    async def list_system_config(self) -> dict[str, Any]:
        """GET /system/config — все конфиг-записи (``{items:[...]}``).

        Право: ``hub.admin`` либо мягкое ``system.config.read``. У секретных
        записей (``is_secret=true``) поле ``value`` приходит ``null``.
        """
        return await self._request("GET", "/system/config")

    async def get_system_config(self, key: str) -> dict[str, Any]:
        """GET /system/config/{key} — одна запись (``SystemConfigEntryDTO``)."""
        return await self._request("GET", f"/system/config/{key}")

    async def set_system_config(self, key: str, *, value: str) -> dict[str, Any]:
        """PATCH /system/config/{key} — записать значение. Только ``hub.admin``.

        ``value`` ВСЕГДА строка (≤8192): backend сам разбирает её по
        ``value_type`` записи (int/bool/json).
        """
        return await self._request(
            "PATCH", f"/system/config/{key}", json={"value": value}
        )

    # --- сессии и профиль (routes/me.py, routes/auth.py) ---
    async def list_sessions(
        self, *, page: int = 1, size: int = 50
    ) -> dict[str, Any]:
        """GET /me/sessions — активные сессии (``{items,total,page,size}``).

        ``size`` ≤ 200. Backend схлопывает несколько refresh-записей одного
        устройства в одну строку, поэтому ``total`` — про устройства, а не
        про токены. У текущей сессии ``is_current=true``.
        """
        return await self._request(
            "GET", "/me/sessions", params={"page": page, "size": size}
        )

    async def revoke_session(self, session_id: str) -> None:
        """DELETE /me/sessions/{id} — закрыть одну сессию (204).

        404 ``SESSION_NOT_FOUND`` — чужая/несуществующая;
        404 ``SESSION_ALREADY_REVOKED`` — уже закрыта.
        """
        await self._request("DELETE", f"/me/sessions/{session_id}")

    async def revoke_all_sessions(self) -> None:
        """DELETE /me/sessions — закрыть все сессии, кроме текущей (204).

        «Текущая» определяется по refresh-куке. CLI ходит по Bearer без куки,
        поэтому для него это «закрыть ВСЕ» — включая себя: после вызова
        локальные токены надо считать протухшими.
        """
        await self._request("DELETE", "/me/sessions")

    async def logout(self, refresh_token: str | None = None) -> None:
        """POST /auth/logout — серверная инвалидация refresh-токена (204).

        Идемпотентно: неизвестный/уже отозванный токен тоже даёт 204 (backend
        не подтверждает существование токена). Тело — сырое
        ``{"refresh_token": ...}``; без него backend читает куку, которой у
        CLI нет, поэтому передавать токен обязательно, иначе вызов бесполезен.
        """
        payload = {"refresh_token": refresh_token} if refresh_token else {}
        await self._request("POST", "/auth/logout", json=payload)

    async def update_me(
        self,
        *,
        display_name: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /me — обновить свой профиль (``MeResponse``).

        Передаём только явно заданные поля: ``None`` у backend означает «не
        менять», и слать его бессмысленно.
        """
        payload: dict[str, Any] = {}
        if display_name is not None:
            payload["display_name"] = display_name
        if first_name is not None:
            payload["first_name"] = first_name
        if last_name is not None:
            payload["last_name"] = last_name
        return await self._request("PATCH", "/me", json=payload)

    async def list_my_skills(
        self, *, channel: str = "published", page: int = 1, size: int = 50
    ) -> dict[str, Any]:
        """GET /me/skills — навыки, где я автор (``{items,total,page,size}``).

        Это НЕ ``/me/installs`` (установленное мне) — здесь авторство.
        ``size`` ≤ 200.
        """
        return await self._request(
            "GET",
            "/me/skills",
            params={"channel": channel, "page": page, "size": size},
        )

    async def get_my_skills_usage(
        self, *, days: int = 30, limit: int = 5
    ) -> dict[str, Any]:
        """GET /me/skills/usage — статистика моих навыков.

        ``days`` 1..365, ``limit`` 1..20. ``count`` в элементах — установки
        по РАЗЛИЧНЫМ устройствам, а не сырые события.
        """
        return await self._request(
            "GET", "/me/skills/usage", params={"days": days, "limit": limit}
        )

    # --- роли: CRUD + назначение (routes/roles.py, role_assignments.py) ---
    async def get_role(self, role_id: str) -> dict[str, Any]:
        """GET /roles/{role_id} — карточка роли (``RoleWithScopeDTO``)."""
        return await self._request("GET", f"/roles/{role_id}")

    async def create_role(
        self,
        *,
        slug: str,
        name: str,
        permission_keys: list[str] | None = None,
        description: str | None = None,
        is_default: bool = False,
        is_assignable_by_company: bool = False,
    ) -> dict[str, Any]:
        """POST /roles — создать глобальную роль (201). Право ``hub.admin``."""
        payload: dict[str, Any] = {
            "slug": slug,
            "name": name,
            "permission_keys": permission_keys or [],
            "is_default": is_default,
            "is_assignable_by_company": is_assignable_by_company,
        }
        if description is not None:
            payload["description"] = description
        return await self._request("POST", "/roles", json=payload)

    async def update_role(
        self,
        role_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        permission_keys: list[str] | None = None,
        is_default: bool | None = None,
        is_assignable_by_company: bool | None = None,
    ) -> dict[str, Any]:
        """PATCH /roles/{role_id} — обновить роль. Право ``hub.admin``.

        ``slug`` неизменяем (его нет в запросе backend'а).
        """
        payload: dict[str, Any] = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if permission_keys is not None:
            payload["permission_keys"] = permission_keys
        if is_default is not None:
            payload["is_default"] = is_default
        if is_assignable_by_company is not None:
            payload["is_assignable_by_company"] = is_assignable_by_company
        return await self._request("PATCH", f"/roles/{role_id}", json=payload)

    async def delete_role(self, role_id: str) -> None:
        """DELETE /roles/{role_id} — удалить роль (204). Право ``hub.admin``."""
        await self._request("DELETE", f"/roles/{role_id}")

    async def assign_role(
        self, *, user_id: str, role_id: str
    ) -> dict[str, Any]:
        """POST /role-assignments — назначить пользователю ГЛОБАЛЬНУЮ роль.

        Идемпотентно по паре (пользователь, NULL-компания): повторный вызов с
        другим ``role_id`` — это смена роли, а не второе назначение. Ответ —
        сырой словарь ``{user_id, role_id, created}``, где ``created`` —
        СТРОКА ``"true"``/``"false"`` (не bool). Право ``hub.admin``.
        """
        return await self._request(
            "POST",
            "/role-assignments",
            json={"user_id": user_id, "role_id": role_id},
        )

    # --- навыки: правка, удаление, версии, обвязка репозитория ---
    async def update_skill(
        self, slug: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """PATCH /skills/{slug} — частичное обновление карточки навыка.

        Словарь собирает вызывающая команда — полей много (title, description,
        tags, channel, visible, category, access_level, kind, license,
        short_description, icon/cover…), и дублировать их здесь именованными
        аргументами значило бы держать две расходящиеся копии схемы.
        """
        return await self._request("PATCH", f"/skills/{slug}", json=payload)

    async def delete_skill(self, id_or_slug: str) -> None:
        """DELETE /skills/{id_or_slug} — удалить навык (204, soft-delete).

        Принимает и числовой id, и slug. Право ``skill.manage``/``hub.admin``.
        """
        await self._request("DELETE", f"/skills/{id_or_slug}")

    async def publish_skill_version(
        self,
        slug: str,
        *,
        semver: str,
        commit_sha: str,
        manifest: dict[str, Any],
        channel: str = "published",
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /skills/{slug}/versions — зарегистрировать версию навыка.

        Тело — JSON (НЕ multipart): содержимое навыка приезжает через
        git-sync/webhook, а эта ручка регистрирует semver+commit+манифест.
        Право ``skill.publish``. Ответ ``{skill_id, version_id, is_new_skill}``.
        """
        return await self._request(
            "POST",
            f"/skills/{slug}/versions",
            json={
                "semver": semver,
                "commit_sha": commit_sha,
                "manifest": manifest,
                "channel": channel,
                "tags": tags or [],
            },
        )

    async def list_skill_collections(self, slug: str) -> dict[str, Any]:
        """GET /skills/{slug}/collections — в каких коллекциях лежит навык."""
        return await self._request("GET", f"/skills/{slug}/collections")

    async def get_skill_analytics(
        self, slug: str, *, date_from: str | None = None, date_to: str | None = None
    ) -> dict[str, Any]:
        """GET /skills/{slug}/analytics — установки/включения/DAU/ошибки.

        Query-параметры называются ``from``/``to`` (зарезервированные слова в
        Python — отсюда переименованные аргументы). ``top_companies`` придёт
        пустым, если прав не хватает: backend режет поле, а не весь ответ.
        """
        params: dict[str, Any] = {}
        if date_from is not None:
            params["from"] = date_from
        if date_to is not None:
            params["to"] = date_to
        return await self._request(
            "GET", f"/skills/{slug}/analytics", params=params
        )

    async def get_sync_job(self, slug: str, job_id: str) -> dict[str, Any]:
        """GET /skills/{slug}/sync-jobs/{job_id} — статус джобы синхронизации.

        ``status`` ∈ ``queued|running|done|error``. Право ``skill.publish``.
        """
        return await self._request("GET", f"/skills/{slug}/sync-jobs/{job_id}")

    async def get_repo_credential(self, slug: str) -> dict[str, Any]:
        """GET /skills/{slug}/repo-credential — ТОЛЬКО метаданные.

        Значение секрета не возвращается никогда; если credential не задан —
        ``has_credential=false`` с пустыми provider/secret_type.
        """
        return await self._request("GET", f"/skills/{slug}/repo-credential")

    async def set_repo_credential(
        self, slug: str, *, provider: str, secret_type: str, secret: str
    ) -> dict[str, Any]:
        """PUT /skills/{slug}/repo-credential — положить токен/deploy-key.

        ``provider`` ∈ ``github|gitlab``, ``secret_type`` ∈
        ``token|deploy_key``. Секрет шифруется на стороне backend; 503
        ``SECRETS_DISABLED``, если у хаба не настроен ключ шифрования.
        Значение секрета НЕ логируем и НЕ печатаем.
        """
        return await self._request(
            "PUT",
            f"/skills/{slug}/repo-credential",
            json={
                "provider": provider,
                "secret_type": secret_type,
                "secret": secret,
            },
        )

    async def delete_repo_credential(self, slug: str) -> None:
        """DELETE /skills/{slug}/repo-credential — забыть credential (204)."""
        await self._request("DELETE", f"/skills/{slug}/repo-credential")

    async def get_repo_tree(
        self, slug: str, *, ref: str | None = None
    ) -> dict[str, Any]:
        """GET /skills/{slug}/repo/tree — список файлов репозитория навыка.

        ``ref`` — sha/тег/ветка; по умолчанию backend берёт последнюю версию.
        """
        params: dict[str, Any] = {}
        if ref is not None:
            params["ref"] = ref
        return await self._request(
            "GET", f"/skills/{slug}/repo/tree", params=params
        )

    async def get_repo_file(
        self, slug: str, *, path: str, ref: str | None = None
    ) -> dict[str, Any]:
        """GET /skills/{slug}/repo/file — содержимое одного файла.

        Ответ ``{path, ref, encoding, size, truncated, content}``; у бинарных
        файлов ``content=null``, ``encoding="binary"``. Обход каталогов
        (``..``/абсолютный путь) backend отбивает 400.
        """
        params: dict[str, Any] = {"path": path}
        if ref is not None:
            params["ref"] = ref
        return await self._request(
            "GET", f"/skills/{slug}/repo/file", params=params
        )

    async def get_repo_readme(
        self, slug: str, *, ref: str | None = None
    ) -> dict[str, Any]:
        """GET /skills/{slug}/repo/readme — README навыка (404, если нет)."""
        params: dict[str, Any] = {}
        if ref is not None:
            params["ref"] = ref
        return await self._request(
            "GET", f"/skills/{slug}/repo/readme", params=params
        )

    async def set_skill_star(
        self, skill_id: str, *, starred: bool
    ) -> dict[str, Any]:
        """#1437: PUT/DELETE /skills/{skill_id}/star — УСТАНОВИТЬ состояние.

        Пришло на смену toggle-у под POST. Разница не косметическая: toggle
        читает текущее состояние на СЕРВЕРЕ и инвертирует его, поэтому
        ретрай по таймауту (ответ потерялся, запрос дошёл) ОТМЕНЯЛ действие
        пользователя. `PUT` (поставить) и `DELETE` (снять) идемпотентны:
        сколько раз ни повтори — результат один.

        Ответ ``{is_starred, hub_star_count, repo_star_count, total_star_count}``.
        """
        method = "PUT" if starred else "DELETE"
        return await self._request(method, f"/skills/{skill_id}/star")


    # --- коллекции: перемещение и статистика ---
    async def update_collection(
        self,
        slug: str,
        *,
        title: str | None = None,
        description: str | None = None,
        icon: str | None = None,
        icon_color: str | None = None,
        access_level: str | None = None,
    ) -> dict[str, Any]:
        """PATCH /collections/{slug} — обновить карточку коллекции."""
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if description is not None:
            payload["description"] = description
        if icon is not None:
            payload["icon"] = icon
        if icon_color is not None:
            payload["icon_color"] = icon_color
        if access_level is not None:
            payload["access_level"] = access_level
        return await self._request("PATCH", f"/collections/{slug}", json=payload)

    async def move_collection(
        self, slug: str, *, new_parent_id: str | None
    ) -> dict[str, Any]:
        """PATCH /collections/{slug}/move — сменить родителя (``None`` = корень).

        Цикл/превышение глубины backend отбивает 409
        ``COLLECTION_HIERARCHY_ERROR``.
        """
        return await self._request(
            "PATCH",
            f"/collections/{slug}/move",
            json={"new_parent_id": new_parent_id},
        )

    async def get_collection_stats(
        self, slug: str, *, days: int = 30
    ) -> dict[str, Any]:
        """GET /collections/{slug}/stats — навыки/компании/установки + таймлайн.

        ``days`` 1..365. ``source`` показывает, откуда взяты цифры
        (``clickhouse``/``postgres``/``none``).
        """
        return await self._request(
            "GET", f"/collections/{slug}/stats", params={"days": days}
        )

    # --- аналитические события (чтение) ---
    async def list_events(
        self,
        *,
        event_type: str | None = None,
        actor_id: str | None = None,
        company_id: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> dict[str, Any]:
        """GET /events — лента аналитических событий. Право ``hub.admin``.

        ВСЕГДА шлём ``page``/``size``: у backend два режима, и без них
        включается legacy-курсорный, где ``total`` — это количество строк на
        текущей странице, а не всего. Offset-режим даёт честный глобальный
        ``total``. ``size`` ≤ 200.
        """
        params: dict[str, Any] = {"page": page, "size": size}
        for key, value in (
            ("event_type", event_type),
            ("actor_id", actor_id),
            ("company_id", company_id),
            ("resource_type", resource_type),
            ("resource_id", resource_id),
            ("since", since),
            ("until", until),
        ):
            if value is not None:
                params[key] = value
        return await self._request("GET", "/events", params=params)
