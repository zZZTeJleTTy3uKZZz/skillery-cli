"""Тесты команды `skillery passwd` — смена пароля."""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from skillery_cli import output as output_module
from skillery_cli.config import ClientConfig


def _patch_output_text_mode() -> None:
    output_module._mode = "text"


# ---------------------- HubClient.set_password ----------------------
@pytest.mark.asyncio
async def test_hubclient_set_password_posts_correct_endpoint() -> None:
    import respx
    from httpx import Response

    from skillery_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/me/password").mock(return_value=Response(204))
        client = HubClient(
            base_url="http://localhost:8000", access_token="acc-tok"
        )
        try:
            await client.set_password(new_password="brand-new-pw")
        finally:
            await client.close()
        assert route.called
        request_body = route.calls[0].request.read()
        # JSON содержит new_password
        assert b"brand-new-pw" in request_body


# ---------------------- passwd command ----------------------
def test_passwd_requires_login(monkeypatch: pytest.MonkeyPatch) -> None:
    import typer

    from skillery_cli import __main__ as main_mod

    _patch_output_text_mode()
    empty_cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: empty_cfg))

    with pytest.raises(typer.Exit) as exc:
        main_mod.cmd_passwd()
    assert exc.value.exit_code == 1


def test_passwd_no_token_emits_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import typer

    from skillery_cli import __main__ as main_mod

    _patch_output_text_mode()
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "load_tokens", lambda email: (None, None))

    with pytest.raises(typer.Exit) as exc:
        main_mod.cmd_passwd()
    assert exc.value.exit_code == 1


def test_passwd_happy_path_calls_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from skillery_cli import __main__ as main_mod

    _patch_output_text_mode()
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "load_tokens", lambda email: ("access", "refresh"))

    # Подменяем typer.prompt чтобы вернуть новый пароль и подтверждение
    prompts: list[Any] = ["brand-new-pw", "brand-new-pw"]

    def fake_prompt(*args: Any, **kwargs: Any) -> str:
        return prompts.pop(0)

    monkeypatch.setattr("typer.prompt", fake_prompt)

    fake_client = MagicMock()
    captured: dict[str, Any] = {}

    async def _fake_set_password(*, new_password: str) -> None:
        captured["new_password"] = new_password

    async def _fake_close() -> None:
        return None

    fake_client.set_password = _fake_set_password
    fake_client.close = _fake_close
    monkeypatch.setattr(main_mod, "HubClient", lambda **kw: fake_client)

    main_mod.cmd_passwd()
    assert captured["new_password"] == "brand-new-pw"


def test_passwd_rejects_mismatched_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import typer

    from skillery_cli import __main__ as main_mod

    _patch_output_text_mode()
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "load_tokens", lambda email: ("access", "refresh"))

    prompts: list[Any] = ["new-secret", "different-secret"]

    def fake_prompt(*args: Any, **kwargs: Any) -> str:
        return prompts.pop(0)

    monkeypatch.setattr("typer.prompt", fake_prompt)

    with pytest.raises(typer.Exit) as exc:
        main_mod.cmd_passwd()
    assert exc.value.exit_code == 1


def test_passwd_rejects_short_password(monkeypatch: pytest.MonkeyPatch) -> None:
    import typer

    from skillery_cli import __main__ as main_mod

    _patch_output_text_mode()
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "load_tokens", lambda email: ("access", "refresh"))

    prompts: list[Any] = ["short", "short"]

    def fake_prompt(*args: Any, **kwargs: Any) -> str:
        return prompts.pop(0)

    monkeypatch.setattr("typer.prompt", fake_prompt)

    with pytest.raises(typer.Exit) as exc:
        main_mod.cmd_passwd()
    assert exc.value.exit_code == 1


def test_passwd_registered_in_app_when_logged_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "passwd" in names


def test_passwd_not_registered_when_not_logged_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """passwd доступен только после login (требует access-токен)."""
    cfg = ClientConfig(base_url="http://localhost:8000")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "passwd" not in names
