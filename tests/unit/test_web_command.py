"""Тесты команды `skillery web` — handoff в Web UI."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from skillery_cli import output as output_module
from skillery_cli.config import ClientConfig


# ---------------------- effective_web_ui_url ----------------------
def test_effective_web_ui_url_replaces_api_subdomain() -> None:
    cfg = ClientConfig(base_url="https://api.hub.example")
    assert cfg.effective_web_ui_url() == "https://hub.example"


def test_effective_web_ui_url_localhost_default() -> None:
    cfg = ClientConfig(base_url="http://localhost:8000")
    assert cfg.effective_web_ui_url() == "http://localhost:3000"


def test_effective_web_ui_url_prod_default(monkeypatch) -> None:
    """Регресс: свежий CLI (base=прод-дефолт api.skillery.ru) → hub.skillery.ru,
    НЕ skillery.ru (деривация «убрать api.» неверна для этой топологии) и НЕ
    localhost. Иначе browser-flow логин открывал localhost:3000/skillery.ru."""
    monkeypatch.delenv("SKILLERY_BASE_URL", raising=False)
    monkeypatch.delenv("SKILLERY_WEB_UI_URL", raising=False)
    # Свежий конфиг без base — __post_init__ ставит прод-дефолт.
    assert ClientConfig().effective_web_ui_url() == "https://hub.skillery.ru"
    assert (
        ClientConfig(base_url="https://api.skillery.ru").effective_web_ui_url()
        == "https://hub.skillery.ru"
    )


def test_effective_web_ui_url_env_override(monkeypatch) -> None:
    """Dev/бренд-оверрайд: SKILLERY_WEB_UI_URL перебивает прод-дефолт."""
    monkeypatch.setenv("SKILLERY_WEB_UI_URL", "https://staging.example")
    assert (
        ClientConfig(base_url="https://api.skillery.ru").effective_web_ui_url()
        == "https://staging.example"
    )


def test_effective_web_ui_url_explicit_override() -> None:
    cfg = ClientConfig(
        base_url="https://api.hub.example", web_ui_url="https://custom.ui"
    )
    assert cfg.effective_web_ui_url() == "https://custom.ui"


def test_effective_web_ui_url_fallback_returns_base_url() -> None:
    cfg = ClientConfig(base_url="https://hub.example")
    assert cfg.effective_web_ui_url() == "https://hub.example"


# ---------------------- config persist web_ui_url ----------------------
def test_config_save_and_load_web_ui_url(tmp_path: Path) -> None:
    cfg_path = tmp_path / "config.toml"
    cfg = ClientConfig(base_url="https://api.hub.example", web_ui_url="https://hub.example")
    cfg.save(path=cfg_path)
    loaded = ClientConfig.load(path=cfg_path)
    assert loaded.web_ui_url == "https://hub.example"
    # backwards-compat: missing web_ui_url loads as None
    cfg2 = ClientConfig(base_url="https://api.hub.example")
    cfg2.save(path=cfg_path)
    loaded2 = ClientConfig.load(path=cfg_path)
    assert loaded2.web_ui_url is None


# ---------------------- HubClient.exchange_create ----------------------
@pytest.mark.asyncio
async def test_hubclient_exchange_create_posts_correct_endpoint() -> None:
    import respx
    from httpx import Response

    from skillery_cli.core.transport import HubClient

    with respx.mock(base_url="http://localhost:8000") as router:
        route = router.post("/auth/exchanges").mock(
            return_value=Response(
                201,
                json={"code": "ABC123XYZ", "expires_at": "2026-05-25T12:00:00Z"},
            )
        )
        client = HubClient(base_url="http://localhost:8000", access_token="t")
        try:
            result = await client.exchange_create()
        finally:
            await client.close()
        assert route.called
        assert result == {"code": "ABC123XYZ", "expires_at": "2026-05-25T12:00:00Z"}


# ---------------------- web command ----------------------
def _make_cfg_logged_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ClientConfig:
    """Сборка конфигурации залогиненного пользователя без записи на диск."""
    cfg = ClientConfig(
        base_url="https://api.hub.example",
        user_email="user@example.com",
        permissions=["skill.read"],
    )
    return cfg


def _patch_output_text_mode() -> None:
    """Сбросить глобальный output mode в 'text' (на случай если предыдущий тест поменял)."""
    output_module._mode = "text"


def test_web_requires_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без user_email/permissions — emit_error NOT_LOGGED_IN + typer.Exit(1)."""
    import typer

    from skillery_cli import __main__ as main_mod

    _patch_output_text_mode()
    empty_cfg = ClientConfig(base_url="https://api.hub.example")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: empty_cfg))

    with pytest.raises(typer.Exit) as exc_info:
        main_mod.cmd_web(no_browser=True)
    assert exc_info.value.exit_code == 1


def test_web_no_token_emits_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Залогинен по permissions, но keyring пуст → NO_TOKEN."""
    import typer

    from skillery_cli import __main__ as main_mod

    _patch_output_text_mode()
    cfg = ClientConfig(
        base_url="https://api.hub.example",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "load_tokens", lambda email: (None, None))

    with pytest.raises(typer.Exit) as exc_info:
        main_mod.cmd_web(no_browser=True)
    assert exc_info.value.exit_code == 1


def test_web_calls_exchange_and_opens_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Happy path: exchange_create → webbrowser.open с правильным URL."""
    from skillery_cli import __main__ as main_mod

    _patch_output_text_mode()
    cfg = ClientConfig(
        base_url="https://api.hub.example",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "load_tokens", lambda email: ("access-tok", "refresh-tok"))

    # Подменяем HubClient на mock
    fake_client = MagicMock()

    async def _fake_exchange() -> dict[str, Any]:
        return {"code": "ABC123", "expires_at": "2026-05-25T12:01:00Z"}

    async def _fake_close() -> None:
        return None

    fake_client.exchange_create = _fake_exchange
    fake_client.close = _fake_close

    def _fake_ctor(**kwargs: Any) -> MagicMock:
        return fake_client

    monkeypatch.setattr(main_mod, "HubClient", _fake_ctor)

    opened: list[str] = []

    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    main_mod.cmd_web(no_browser=False)

    assert len(opened) == 1
    assert opened[0] == "https://hub.example/login?code=ABC123"


def test_web_no_browser_flag_skips_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """--no-browser → webbrowser.open НЕ вызывается, но URL всё равно посчитан."""
    from skillery_cli import __main__ as main_mod

    _patch_output_text_mode()
    cfg = ClientConfig(
        base_url="https://api.hub.example",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "load_tokens", lambda email: ("access-tok", "refresh-tok"))

    fake_client = MagicMock()

    async def _fake_exchange() -> dict[str, Any]:
        return {"code": "Z9", "expires_at": "2026-05-25T12:01:00Z"}

    async def _fake_close() -> None:
        return None

    fake_client.exchange_create = _fake_exchange
    fake_client.close = _fake_close
    monkeypatch.setattr(main_mod, "HubClient", lambda **kw: fake_client)

    opened: list[str] = []
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    main_mod.cmd_web(no_browser=True)

    assert opened == []  # браузер не открыт


def test_web_json_mode_emits_structured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """В json-режиме stdout содержит JSON {url, code, expires_at, opened_browser}."""
    from skillery_cli import __main__ as main_mod

    output_module._mode = "json"
    try:
        cfg = ClientConfig(
            base_url="https://api.hub.example",
            user_email="u@example.com",
            permissions=["skill.read"],
        )
        monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
        monkeypatch.setattr(
            main_mod, "load_tokens", lambda email: ("access-tok", "refresh-tok")
        )

        fake_client = MagicMock()

        async def _fake_exchange() -> dict[str, Any]:
            return {"code": "C0DE", "expires_at": "2026-05-25T12:02:00Z"}

        async def _fake_close() -> None:
            return None

        fake_client.exchange_create = _fake_exchange
        fake_client.close = _fake_close
        monkeypatch.setattr(main_mod, "HubClient", lambda **kw: fake_client)

        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda url: True)

        main_mod.cmd_web(no_browser=True)
        captured = capsys.readouterr()
        # должна быть одна JSON-строка
        line = captured.out.strip().splitlines()[-1]
        payload = json.loads(line)
        assert payload["code"] == "C0DE"
        assert payload["url"] == "https://hub.example/login?code=C0DE"
        assert payload["expires_at"] == "2026-05-25T12:02:00Z"
        assert payload["opened_browser"] is False
    finally:
        output_module._mode = "text"


def test_web_command_registered_in_app() -> None:
    """build_app() должна регистрировать 'web' даже без логина (always-on)."""
    from skillery_cli.__main__ import build_app

    app = build_app()
    names = [cmd.name for cmd in app.registered_commands]
    assert "web" in names
