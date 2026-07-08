"""Re-export shim: регистрация MCP-серверов навыка переехала в кит.

cli-kits W7 (добивка гибрида): ``register_mcp`` / ``unregister_mcp`` / ``list_mcp``
(claude_code JSON ~/.claude.json|.mcp.json, codex TOML config.toml, unknown →
manual) теперь в ките ``skillkit.mcp_register`` (0 завязок на сеть/auth/config —
пути берутся от agent target). Здесь — АЛИАС kit-модуля в ``sys.modules`` (как
``core/linker.py``), чтобы ``monkeypatch.setattr(mcp_register, "Path", ...)`` в
тестах и вызовы из ``skillkit.tooling`` указывали на один объект.
"""
from __future__ import annotations

import sys

from skillkit import mcp_register as _kit_mcp_register

sys.modules[__name__] = _kit_mcp_register
