"""E3 фаза 2 — core/mcp_register: регистрация MCP-серверов навыка в конфиг агента.

Навык типа ``tooling`` (E6) несёт ``[[mcp]]`` — список серверов в форме,
ЕДИНОЙ с clikit ``McpServer`` (``transport`` + ``command``/``args``/``env`` для
stdio | ``url``/``headers`` для sse/http + ``enabled``). E3 кладёт каждую запись
``server_name → {...}`` в конфиг target-агента:

- ``claude_code`` global → ``~/.claude.json`` (JSON, ключ ``mcpServers``),
  project → ``<project>/.mcp.json`` (JSON, ключ ``mcpServers``);
- ``codex`` → ``~/.codex/config.toml`` (TOML, таблицы ``[mcp_servers.<name>]``);
- агент с НЕИЗВЕСТНЫМ форматом → НЕ падаем: возвращаем status=manual + инструкция.

Идемпотентно (повторная регистрация не дублирует). ``unregister_mcp`` снимает
запись при disable/remove навыка. Кроссплатформа: пути конфигов берутся от
agent target (``Path.home()`` / project), запись атомарная.
"""
from __future__ import annotations

import json

import pytest

from skills_hub_cli.core import mcp_register as mr
from skills_hub_cli.core.agents.claude_code import ClaudeCodeTarget
from skills_hub_cli.core.agents.codex import CodexTarget


# --------------------------------------------------------------------------
#  Манифест-форма [[mcp]] (server_name + transport + config{...})
# --------------------------------------------------------------------------
def _stdio_server() -> dict:
    return {
        "server_name": "bitrix-mcp",
        "transport": "stdio",
        "config": {"command": "bx-mcp", "args": ["--stdio"], "env": {"TOKEN": "x"}},
    }


def _http_server() -> dict:
    return {
        "server_name": "remote-mcp",
        "transport": "http",
        "config": {"url": "https://mcp.example/sse", "headers": {"X-Key": "k"}},
    }


# --------------------------------------------------------------------------
#  claude_code — global ~/.claude.json (JSON, mcpServers)
# --------------------------------------------------------------------------
def test_register_claude_global_writes_mcp_servers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    # global конфиг лежит рядом с ~/.claude → подменяем home на tmp.
    monkeypatch.setattr(mr.Path, "home", staticmethod(lambda: tmp_path))
    res = mr.register_mcp(_stdio_server(), agent_target=target, project=None)
    assert res["status"] == "registered"
    cfg = tmp_path / ".claude.json"
    assert cfg.exists()
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "bitrix-mcp" in data["mcpServers"]
    entry = data["mcpServers"]["bitrix-mcp"]
    # stdio-форма Claude Code: command/args/env.
    assert entry["command"] == "bx-mcp"
    assert entry["args"] == ["--stdio"]
    assert entry["env"] == {"TOKEN": "x"}


def test_register_claude_remote_maps_url_headers(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    monkeypatch.setattr(mr.Path, "home", staticmethod(lambda: tmp_path))
    mr.register_mcp(_http_server(), agent_target=target, project=None)
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    entry = data["mcpServers"]["remote-mcp"]
    assert entry["url"] == "https://mcp.example/sse"
    assert entry["headers"] == {"X-Key": "k"}
    # remote транспорт → type зафиксирован (claude различает sse/http).
    assert entry.get("type") in ("http", "sse")


def test_register_claude_preserves_existing_keys(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Регистрация НЕ затирает чужие ключи и чужие mcpServers."""
    monkeypatch.setattr(mr.Path, "home", staticmethod(lambda: tmp_path))
    cfg = tmp_path / ".claude.json"
    cfg.write_text(
        json.dumps(
            {"numStartups": 7, "mcpServers": {"other": {"command": "x"}}}
        ),
        encoding="utf-8",
    )
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    mr.register_mcp(_stdio_server(), agent_target=target, project=None)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["numStartups"] == 7  # чужой ключ цел
    assert "other" in data["mcpServers"]  # чужой сервер цел
    assert "bitrix-mcp" in data["mcpServers"]  # наш добавлен


def test_register_claude_idempotent(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mr.Path, "home", staticmethod(lambda: tmp_path))
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    mr.register_mcp(_stdio_server(), agent_target=target, project=None)
    mr.register_mcp(_stdio_server(), agent_target=target, project=None)
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    # ровно одна запись (не дубль), ключ — server_name.
    assert list(data["mcpServers"]).count("bitrix-mcp") == 1


# --------------------------------------------------------------------------
#  claude_code — project .mcp.json
# --------------------------------------------------------------------------
def test_register_claude_project_uses_mcp_json(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    project = tmp_path / "proj"
    project.mkdir()
    res = mr.register_mcp(_stdio_server(), agent_target=target, project=project)
    assert res["status"] == "registered"
    cfg = project / ".mcp.json"
    assert cfg.exists()
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert "bitrix-mcp" in data["mcpServers"]


# --------------------------------------------------------------------------
#  codex — ~/.codex/config.toml ([mcp_servers.<name>])
# --------------------------------------------------------------------------
def test_register_codex_writes_toml_table(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tomllib

    target = CodexTarget(root=tmp_path / ".codex")
    res = mr.register_mcp(_stdio_server(), agent_target=target, project=None)
    assert res["status"] == "registered"
    cfg = tmp_path / ".codex" / "config.toml"
    assert cfg.exists()
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert "bitrix-mcp" in data["mcp_servers"]
    assert data["mcp_servers"]["bitrix-mcp"]["command"] == "bx-mcp"


# --------------------------------------------------------------------------
#  unknown agent format → graceful manual
# --------------------------------------------------------------------------
def test_register_unknown_agent_returns_manual(tmp_path) -> None:
    """Агент, чей формат конфига неизвестен → status=manual + инструкция, не падаем."""

    class _Weird:
        name = "weird-agent"
        dirname = ".weird"

        def __init__(self) -> None:
            self._root = tmp_path / ".weird"

    res = mr.register_mcp(_stdio_server(), agent_target=_Weird(), project=None)
    assert res["status"] == "manual"
    assert "instruction" in res
    assert "bitrix-mcp" in res["instruction"]


def test_register_invalid_server_returns_error_not_raise(tmp_path) -> None:
    """Пустой server_name → status=error, без исключения (graceful)."""
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    res = mr.register_mcp(
        {"server_name": "", "transport": "stdio", "config": {}},
        agent_target=target,
        project=None,
    )
    assert res["status"] == "error"


# --------------------------------------------------------------------------
#  unregister_mcp
# --------------------------------------------------------------------------
def test_unregister_claude_removes_only_that_server(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mr.Path, "home", staticmethod(lambda: tmp_path))
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    mr.register_mcp(_stdio_server(), agent_target=target, project=None)
    mr.register_mcp(_http_server(), agent_target=target, project=None)
    res = mr.unregister_mcp("bitrix-mcp", agent_target=target, project=None)
    assert res["status"] == "unregistered"
    data = json.loads((tmp_path / ".claude.json").read_text(encoding="utf-8"))
    assert "bitrix-mcp" not in data["mcpServers"]
    assert "remote-mcp" in data["mcpServers"]  # другой сервер цел


def test_unregister_absent_is_noop(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Снятие несуществующего сервера → status=absent, без ошибки."""
    monkeypatch.setattr(mr.Path, "home", staticmethod(lambda: tmp_path))
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    res = mr.unregister_mcp("nope", agent_target=target, project=None)
    assert res["status"] in ("absent", "unregistered")


def test_unregister_codex_toml(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tomllib

    target = CodexTarget(root=tmp_path / ".codex")
    mr.register_mcp(_stdio_server(), agent_target=target, project=None)
    mr.unregister_mcp("bitrix-mcp", agent_target=target, project=None)
    cfg = tmp_path / ".codex" / "config.toml"
    data = tomllib.loads(cfg.read_text(encoding="utf-8")) if cfg.exists() else {}
    assert "bitrix-mcp" not in data.get("mcp_servers", {})


def test_unregister_unknown_agent_manual(tmp_path) -> None:
    class _Weird:
        name = "weird-agent"
        dirname = ".weird"

        def __init__(self) -> None:
            self._root = tmp_path / ".weird"

    res = mr.unregister_mcp("bitrix-mcp", agent_target=_Weird(), project=None)
    assert res["status"] == "manual"


# --------------------------------------------------------------------------
#  list_mcp (для doctor/status — что зарегистрировано)
# --------------------------------------------------------------------------
def test_list_mcp_claude(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mr.Path, "home", staticmethod(lambda: tmp_path))
    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    mr.register_mcp(_stdio_server(), agent_target=target, project=None)
    names = mr.list_mcp(agent_target=target, project=None)
    assert "bitrix-mcp" in names
