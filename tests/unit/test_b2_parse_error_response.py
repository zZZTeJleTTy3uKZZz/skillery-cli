"""B2 (CLI-сторона): единый разбор ошибочных ответов в transport.

Раньше `_request()` корректно разворачивал тело ошибки FastAPI
(`{"detail": dict|str|list}` → машинные `code`/`message`/`details`), а
multipart-методы (`post_comment_multipart`, `reply_ticket_multipart`)
имели СВОЙ дублированный разбор, читавший только top-level
`{code,message,details}` → при контракте `{detail:{code,message}}` они
давали `code=UNKNOWN`.

Эти тесты фиксируют единый разбор:
- `_parse_error_response` покрывает все формы detail (dict/str/list/нет);
- 401 → SESSION_EXPIRED;
- 429 → code из тела (RATE_LIMITED) при detail-dict И при top-level;
- обе multipart-ветки теперь дают тот же ApiError, что и `_request`.
"""
from __future__ import annotations

import pytest
import respx
from httpx import Response

from skillery_cli.core.transport import ApiError, HubClient


# ----------------------------- _parse_error_response (unit) ------------------
@pytest.mark.asyncio
async def test_parse_detail_dict_unwraps_code_message_details() -> None:
    client = HubClient(base_url="http://localhost:8000", access_token="t")
    try:
        resp = Response(
            403,
            json={
                "detail": {
                    "code": "PERMISSION_DENIED",
                    "message": "нет прав",
                    "details": {"need": "skill.manage"},
                }
            },
        )
        err = client._parse_error_response(resp)
    finally:
        await client.close()
    assert isinstance(err, ApiError)
    assert err.status_code == 403
    assert err.code == "PERMISSION_DENIED"
    assert err.message == "нет прав"
    assert err.details == {"need": "skill.manage"}


@pytest.mark.asyncio
async def test_parse_detail_str_becomes_message() -> None:
    client = HubClient(base_url="http://localhost:8000", access_token="t")
    try:
        resp = Response(404, json={"detail": "Skill не найден"})
        err = client._parse_error_response(resp)
    finally:
        await client.close()
    assert err.code == "UNKNOWN"
    assert err.message == "Skill не найден"


@pytest.mark.asyncio
async def test_parse_detail_list_422_validation() -> None:
    client = HubClient(base_url="http://localhost:8000", access_token="t")
    try:
        resp = Response(
            422,
            json={
                "detail": [
                    {"loc": ["body", "email"], "msg": "value is not a valid email"}
                ]
            },
        )
        err = client._parse_error_response(resp)
    finally:
        await client.close()
    assert err.code == "VALIDATION"
    assert "body.email" in err.message
    assert "value is not a valid email" in err.message


@pytest.mark.asyncio
async def test_parse_top_level_code_no_detail() -> None:
    client = HubClient(base_url="http://localhost:8000", access_token="t")
    try:
        resp = Response(409, json={"code": "EMAIL_TAKEN", "message": "занят"})
        err = client._parse_error_response(resp)
    finally:
        await client.close()
    assert err.code == "EMAIL_TAKEN"
    assert err.message == "занят"


@pytest.mark.asyncio
async def test_parse_non_json_body_falls_back_to_text() -> None:
    client = HubClient(base_url="http://localhost:8000", access_token="t")
    try:
        resp = Response(500, text="Internal Server Error")
        err = client._parse_error_response(resp)
    finally:
        await client.close()
    assert err.code == "UNKNOWN"
    assert err.message == "Internal Server Error"


@pytest.mark.asyncio
async def test_parse_401_session_expired() -> None:
    client = HubClient(base_url="http://localhost:8000", access_token="t")
    try:
        resp = Response(401, json={"detail": "token invalid"})
        err = client._parse_error_response(resp)
    finally:
        await client.close()
    assert err.status_code == 401
    assert err.code == "SESSION_EXPIRED"


@pytest.mark.asyncio
async def test_parse_401_no_structured_body_falls_back_to_session_hint() -> None:
    """401 без машинного кода (не-JSON тело) → прежний SESSION_EXPIRED-хинт."""
    client = HubClient(base_url="http://localhost:8000", access_token="t")
    try:
        resp = Response(401, text="Unauthorized")
        err = client._parse_error_response(resp)
    finally:
        await client.close()
    assert err.status_code == 401
    assert err.code == "SESSION_EXPIRED"
    assert "login заново" in err.message


@pytest.mark.asyncio
async def test_parse_401_invalid_credentials_uses_backend_code() -> None:
    """401 при отказе логина (`POST /auth/login`, неверный пароль) — реальный
    бэкенд отдаёт detail-dict с машинным кодом. Разворачиваем его, а НЕ
    подменяем фиксированным SESSION_EXPIRED-хинтом про «протухший токен»."""
    client = HubClient(base_url="http://localhost:8000", access_token="t")
    try:
        resp = Response(
            401,
            json={
                "detail": {
                    "code": "INVALID_CREDENTIALS",
                    "message": "Неверный email или пароль",
                    "details": {},
                }
            },
        )
        err = client._parse_error_response(resp)
    finally:
        await client.close()
    assert err.status_code == 401
    assert err.code == "INVALID_CREDENTIALS"
    assert err.message == "Неверный email или пароль"


@pytest.mark.asyncio
async def test_parse_401_auth_method_not_available_uses_backend_code() -> None:
    """401 AUTH_METHOD_NOT_AVAILABLE (у юзера нет пароля) — тоже из тела."""
    client = HubClient(base_url="http://localhost:8000", access_token="t")
    try:
        resp = Response(
            401,
            json={
                "detail": {
                    "code": "AUTH_METHOD_NOT_AVAILABLE",
                    "message": "У пользователя не установлен пароль",
                }
            },
        )
        err = client._parse_error_response(resp)
    finally:
        await client.close()
    assert err.code == "AUTH_METHOD_NOT_AVAILABLE"
    assert err.message == "У пользователя не установлен пароль"


@pytest.mark.asyncio
async def test_parse_401_token_revoked_keeps_backend_message() -> None:
    """Истёкшая сессия на авторизованном запросе: бэкенд (`deps.py`) отдаёт
    свой машинный код с понятным сообщением — отдаём его, не подменяя."""
    client = HubClient(base_url="http://localhost:8000", access_token="t")
    try:
        resp = Response(
            401,
            json={
                "detail": {
                    "code": "TOKEN_REVOKED",
                    "message": "Сессия завершена сменой пароля; войдите заново",
                }
            },
        )
        err = client._parse_error_response(resp)
    finally:
        await client.close()
    assert err.code == "TOKEN_REVOKED"
    assert err.message == "Сессия завершена сменой пароля; войдите заново"


@pytest.mark.asyncio
async def test_parse_429_rate_limited_from_detail_dict() -> None:
    client = HubClient(base_url="http://localhost:8000", access_token="t")
    try:
        resp = Response(
            429,
            json={"detail": {"code": "RATE_LIMITED", "message": "слишком часто"}},
        )
        err = client._parse_error_response(resp)
    finally:
        await client.close()
    assert err.status_code == 429
    assert err.code == "RATE_LIMITED"
    assert err.message == "слишком часто"


@pytest.mark.asyncio
async def test_parse_429_rate_limited_from_top_level() -> None:
    client = HubClient(base_url="http://localhost:8000", access_token="t")
    try:
        resp = Response(429, json={"code": "RATE_LIMITED", "message": "wait"})
        err = client._parse_error_response(resp)
    finally:
        await client.close()
    assert err.code == "RATE_LIMITED"


# ------------------- multipart-ветки используют единый разбор -----------------
@pytest.mark.asyncio
async def test_post_comment_multipart_unwraps_detail_dict() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        router.post("/skills/5/comments/multipart").mock(
            return_value=Response(
                403,
                json={
                    "detail": {
                        "code": "PERMISSION_DENIED",
                        "message": "нет прав на комментарий",
                    }
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            with pytest.raises(ApiError) as exc:
                await client.post_comment_multipart(
                    "5", body="hi", screenshots=[("a.png", b"x")]
                )
        finally:
            await client.close()
    assert exc.value.code == "PERMISSION_DENIED"
    assert exc.value.message == "нет прав на комментарий"


@pytest.mark.asyncio
async def test_reply_ticket_multipart_unwraps_detail_dict() -> None:
    with respx.mock(base_url="http://localhost:8000") as router:
        router.post("/support/tickets/42/messages/multipart").mock(
            return_value=Response(
                429,
                json={
                    "detail": {"code": "RATE_LIMITED", "message": "слишком часто"}
                },
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            with pytest.raises(ApiError) as exc:
                await client.reply_ticket_multipart(
                    "42", body="hi", screenshots=[("a.png", b"x")]
                )
        finally:
            await client.close()
    assert exc.value.code == "RATE_LIMITED"
    assert exc.value.status_code == 429
