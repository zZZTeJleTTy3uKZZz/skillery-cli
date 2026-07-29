"""#1227 — авторизация CLI: один флоу обновления токена, один слой хранения.

Обновление сессии жило в ДВУХ копиях: ``__main__._make_refresh_callback`` (с
диагностикой причины в ``_REFRESH_FAILURE``) и ``commands._common
.make_refresh_callback`` (без неё). Клиентов большинство команд собирает через
``_common.make_client`` → на протухшей сессии причина терялась и наружу летело
сырое «Signature has expired». Тесты фиксируют, что реализация одна.

Хранение токенов уже стоит на ките (``librarykit.SecretStore``) — это тоже
закреплено тестом, чтобы не отъехало обратно на свою копию keyring-логики.
"""
from __future__ import annotations

import pytest
from librarykit.secret_store import SecretStore

from skillery_cli import __main__ as m
from skillery_cli.commands import _common
from skillery_cli.config import _HubSecretStore
from skillery_cli.core.transport import ApiError, HubClient

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _reset_reason():
    m._REFRESH_FAILURE["reason"] = None
    yield
    m._REFRESH_FAILURE["reason"] = None


def _cfg():
    from skillery_cli.config import ClientConfig

    cfg = ClientConfig(base_url="http://localhost:8000")
    cfg.user_email = "u@e.io"
    return cfg


async def test_common_refresh_records_failure_reason(monkeypatch) -> None:
    """Callback из ``_common`` пишет причину так же, как из ``__main__``."""
    cfg = _cfg()
    monkeypatch.setattr(m, "load_tokens", lambda email: ("acc", None))

    cb = _common.make_refresh_callback(cfg)
    assert await cb() is None  # type: ignore[operator]
    assert "refresh-токен" in (m._REFRESH_FAILURE["reason"] or "")


async def test_common_refresh_rotates_pair(monkeypatch) -> None:
    """Успешный refresh: новая ПАРА сохраняется и возвращается кортежем."""
    cfg = _cfg()
    saved: dict[str, tuple[str, str, str]] = {}
    monkeypatch.setattr(m, "load_tokens", lambda email: ("acc", "ref"))
    monkeypatch.setattr(
        m, "save_tokens", lambda e, a, r: saved.update(pair=(e, a, r))
    )
    monkeypatch.setattr(m, "populate_from_jwt", lambda cfg, tok: None)
    monkeypatch.setattr(type(cfg), "save", lambda self: None)

    class _Sub:
        async def refresh(self, token: str):
            assert token == "ref"
            return {"access_token": "acc2", "refresh_token": "ref2"}

        async def close(self) -> None:
            return None

    monkeypatch.setattr(m, "HubClient", lambda **kw: _Sub())

    cb = _common.make_refresh_callback(cfg)
    assert await cb() == ("acc2", "ref2")  # type: ignore[operator]
    assert saved["pair"] == ("u@e.io", "acc2", "ref2")
    assert m._REFRESH_FAILURE["reason"] is None


async def test_common_refresh_reports_server_rejection(monkeypatch) -> None:
    cfg = _cfg()
    monkeypatch.setattr(m, "load_tokens", lambda email: ("acc", "ref"))

    class _Sub:
        async def refresh(self, token: str):
            raise ApiError(status_code=401, code="TOKEN_REVOKED", message="нет")

        async def close(self) -> None:
            return None

    monkeypatch.setattr(m, "HubClient", lambda **kw: _Sub())

    cb = _common.make_refresh_callback(cfg)
    assert await cb() is None  # type: ignore[operator]
    assert "TOKEN_REVOKED" in (m._REFRESH_FAILURE["reason"] or "")


def test_token_storage_is_kit_secret_store() -> None:
    """Хранение токенов — SecretStore кита, а не своя копия keyring-логики."""
    assert issubclass(_HubSecretStore, SecretStore)


def test_client_timeout_is_set_on_construction() -> None:
    """Грабля проекта: таймаут задаётся на КОНСТРУКЦИИ клиента.

    Попытка передать ``timeout=`` в вызов метода транспорта кита роняла демона в
    офлайн молчаливым TypeError. Конструктор ``HubClient`` его принимает.
    """
    client = HubClient(base_url="http://localhost:8000", timeout=5.0)
    assert client._transport is not None
