"""Регистрация MCP-серверов навыка в конфиг target-агента (E3 фаза 2).

Навык типа ``tooling`` (E6) несёт ``[[mcp]]`` — список серверов в форме, ЕДИНОЙ
с clikit ``McpServer``: ``server_name`` + ``transport`` (stdio|sse|http) +
``config`` (для stdio — ``command``/``args``/``env``; для remote — ``url``/
``headers``; ``enabled``). E3 кладёт каждую запись в конфиг агента, чьи
расположение и формат зависят от агента:

- ``claude_code``: global → ``~/.claude.json`` (JSON, ключ ``mcpServers``);
  project → ``<project>/.mcp.json`` (JSON, ключ ``mcpServers``);
- ``codex``: ``~/.codex/config.toml`` (TOML, таблицы ``[mcp_servers.<name>]``);
- агент с НЕИЗВЕСТНЫМ форматом (нет дескриптора) → не падаем: возвращаем
  ``status=manual`` + готовая инструкция (graceful degradation).

Идемпотентно (ключ = ``server_name``, перезапись, не дубль). ``unregister_mcp``
снимает запись при disable/remove навыка. ``list_mcp`` — что зарегистрировано
(для doctor/status). Запись атомарная (tmp + ``os.replace``). Кроссплатформа:
пути берутся от ``Path.home()`` / project, TOML через ``tomli_w``.

Все функции возвращают dict со ``status``; НИКОГДА не бросают — ошибка чтения/
записи конфига становится ``status=error`` (install самого навыка не ломается).
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
from typing import Any

import tomli_w

try:  # py311+ stdlib; не должен отсутствовать, но мягко
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
#  Маппинг E6 [[mcp]] → запись конфига агента
# --------------------------------------------------------------------------
def _build_entry(server: dict[str, Any]) -> dict[str, Any]:
    """E6/clikit-форма сервера → dict-запись для ``mcpServers``/``mcp_servers``.

    Прямой mapping (форма едина с clikit McpServer): для stdio берём
    ``command``/``args``/``env``; для remote (sse/http) — ``url``/``headers`` +
    проставляем ``type`` (Claude различает remote-транспорты по нему). Пустые
    поля не пишем, чтобы конфиг был чистым.
    """
    config = server.get("config") or {}
    transport = str(server.get("transport") or "stdio")
    entry: dict[str, Any] = {}
    if transport == "stdio":
        command = config.get("command")
        if command:
            entry["command"] = str(command)
        args = config.get("args")
        if args:
            entry["args"] = list(args)
        env = config.get("env")
        if env:
            entry["env"] = dict(env)
    else:  # sse | http (remote)
        entry["type"] = transport
        url = config.get("url")
        if url:
            entry["url"] = str(url)
        headers = config.get("headers")
        if headers:
            entry["headers"] = dict(headers)
    # enabled=False переносим как есть (агент сам решает, как трактовать).
    if config.get("enabled") is False:
        entry["enabled"] = False
    return entry


def _server_name(server: dict[str, Any]) -> str:
    return str(server.get("server_name") or "").strip()


# --------------------------------------------------------------------------
#  Дескрипторы формата конфига по агенту
# --------------------------------------------------------------------------
def _agent_name(agent_target: Any) -> str:
    return str(getattr(agent_target, "name", "") or "")


def _claude_config_path(agent_target: Any, project: Path | None) -> Path:
    """Где Claude Code хранит mcpServers: project → .mcp.json; global → ~/.claude.json."""
    if project is not None:
        return Path(project) / ".mcp.json"
    return Path.home() / ".claude.json"


def _codex_config_path(agent_target: Any, project: Path | None) -> Path:
    """Codex: <root>/config.toml (root = ~/.codex или project/.codex)."""
    if project is not None:
        return Path(project) / getattr(agent_target, "dirname", ".codex") / "config.toml"
    root = getattr(agent_target, "_root", None) or (
        Path.home() / getattr(agent_target, "dirname", ".codex")
    )
    return Path(root) / "config.toml"


# format: "json" (Claude) | "toml" (Codex). Ключ верхнего уровня + path-резолвер.
_DESCRIPTORS: dict[str, dict[str, Any]] = {
    "claude_code": {"format": "json", "key": "mcpServers", "path": _claude_config_path},
    "codex": {"format": "toml", "key": "mcp_servers", "path": _codex_config_path},
}


def _descriptor(agent_target: Any) -> dict[str, Any] | None:
    return _DESCRIPTORS.get(_agent_name(agent_target))


# --------------------------------------------------------------------------
#  Чтение/запись конфигов (атомарно)
# --------------------------------------------------------------------------
def _read_config(path: Path, fmt: str) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
        if fmt == "json":
            data = json.loads(text) if text.strip() else {}
        else:  # toml
            data = tomllib.loads(text) if tomllib is not None else {}
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # Битый/нечитаемый конфиг: не наследуем чужой мусор, но и не теряем —
        # вызывающая ветка обернёт это в status=error и предупреждение.
        raise


def _write_config(path: Path, fmt: str, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "json":
        payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    else:  # toml
        payload = tomli_w.dumps(data)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def _manual_instruction(server: dict[str, Any], agent_name: str) -> str:
    name = _server_name(server) or "<server>"
    return (
        f"MCP-сервер «{name}» нужно зарегистрировать в агенте «{agent_name}» "
        "вручную: формат его конфига не поддержан автоматически. "
        f"Параметры: transport={server.get('transport')}, "
        f"config={server.get('config')}."
    )


# --------------------------------------------------------------------------
#  Public: register / unregister / list
# --------------------------------------------------------------------------
def register_mcp(
    server: dict[str, Any], *, agent_target: Any, project: Path | None = None
) -> dict[str, Any]:
    """Зарегистрировать MCP-сервер навыка в конфиге агента. Не бросает.

    Возврат ``status`` ∈:
    - ``registered`` — записан (added/перезаписан, idempotent);
    - ``manual``     — формат агента неизвестен (есть ``instruction``);
    - ``error``      — пустой ``server_name`` или сбой записи (есть ``reason``).
    """
    name = _server_name(server)
    if not name:
        return {"status": "error", "reason": "server_name пуст"}

    desc = _descriptor(agent_target)
    if desc is None:
        return {
            "status": "manual",
            "server_name": name,
            "instruction": _manual_instruction(server, _agent_name(agent_target)),
        }

    path: Path = desc["path"](agent_target, project)
    key: str = desc["key"]
    fmt: str = desc["format"]
    try:
        data = _read_config(path, fmt)
        servers = data.get(key)
        if not isinstance(servers, dict):
            servers = {}
        servers[name] = _build_entry(server)
        data[key] = servers
        _write_config(path, fmt, data)
    except (OSError, ValueError) as exc:
        return {"status": "error", "server_name": name, "reason": str(exc)}
    return {"status": "registered", "server_name": name, "config_path": str(path)}


def unregister_mcp(
    server_name: str, *, agent_target: Any, project: Path | None = None
) -> dict[str, Any]:
    """Снять MCP-сервер из конфига агента (disable/remove навыка). Не бросает.

    Возврат ``status`` ∈ ``unregistered`` | ``absent`` (нечего снимать) |
    ``manual`` (формат неизвестен) | ``error``.
    """
    name = str(server_name or "").strip()
    if not name:
        return {"status": "error", "reason": "server_name пуст"}
    desc = _descriptor(agent_target)
    if desc is None:
        return {"status": "manual", "server_name": name}

    path: Path = desc["path"](agent_target, project)
    key: str = desc["key"]
    fmt: str = desc["format"]
    if not path.exists():
        return {"status": "absent", "server_name": name}
    try:
        data = _read_config(path, fmt)
        servers = data.get(key)
        if not isinstance(servers, dict) or name not in servers:
            return {"status": "absent", "server_name": name}
        del servers[name]
        data[key] = servers
        _write_config(path, fmt, data)
    except (OSError, ValueError) as exc:
        return {"status": "error", "server_name": name, "reason": str(exc)}
    return {"status": "unregistered", "server_name": name, "config_path": str(path)}


def list_mcp(*, agent_target: Any, project: Path | None = None) -> list[str]:
    """Имена MCP-серверов, зарегистрированных в конфиге агента (для doctor/status)."""
    desc = _descriptor(agent_target)
    if desc is None:
        return []
    path: Path = desc["path"](agent_target, project)
    if not path.exists():
        return []
    with contextlib.suppress(OSError, ValueError):
        data = _read_config(path, desc["format"])
        servers = data.get(desc["key"])
        if isinstance(servers, dict):
            return sorted(servers.keys())
    return []
