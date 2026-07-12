"""E3 фаза 2 — РЕАЛЬНЫЙ e2e install-flow tooling (без моков leaf-модулей).

Ставит локальный tooling-навык с ``_skill_meta.toml`` ([[cli]] + [[mcp]] +
runtime_dependencies) через ``cmd_install --path`` и проверяет фактический
эффект: shim CLI лёг в bin-стор, MCP-сервер записан в конфиг агента. Затем
``cmd_remove --purge`` снимает и CLI, и MCP. deps мокаем (не трогаем настоящий
pip), но path_store/mcp_register работают по-настоящему — это доказывает, что
вся цепочка собирается на реальных путях win/mac/linux.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import skillery_cli.__main__ as main_mod
from skillery_cli import output as out_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core import mcp_register, path_store
from skillery_cli.core.agents import ClaudeCodeTarget


class _ExplodingClient:
    def __init__(self, *a, **k) -> None:
        raise AssertionError("HubClient НЕ должен создаваться в автономном install")


def _tooling_skill_with_meta_toml(tmp_path: Path) -> Path:
    src = tmp_path / "bx-src"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text(
        "---\nname: bx\nversion: 2.0.0\nkind: tooling\n---\n\n# bx\n", encoding="utf-8"
    )
    (src / "_skill_meta.toml").write_text(
        'description = "bx tooling"\n'
        'version = "2.0.0"\n'
        'kind = "tooling"\n'
        'runtime_dependencies = [{ kind = "pip", spec = "httpx>=0.27" }]\n'
        "\n"
        "[[cli]]\n"
        'command_name = "bx"\n'
        'entrypoint = "bx_cli.main:run"\n'
        "\n"
        "[[mcp]]\n"
        'server_name = "bx-mcp"\n'
        'transport = "stdio"\n'
        "config = { command = \"bx-mcp\", args = [\"--stdio\"] }\n",
        encoding="utf-8",
    )
    return src


def _wire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    target = ClaudeCodeTarget(root=home / ".claude")
    store = tmp_path / "store"
    bin_dir = tmp_path / "bin"
    cfg = ClientConfig(store_dir=str(store), default_install_scope="global")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "_maybe_auto_update", lambda c, **k: None)
    monkeypatch.setattr(main_mod, "_maybe_notify_cli_update", lambda c: None)
    monkeypatch.setattr(main_mod, "track_skill_event", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(main_mod, "HubClient", _ExplodingClient)
    monkeypatch.setattr(main_mod, "_get_access_token", lambda: "unused")
    monkeypatch.setattr(out_mod, "_mode", "json")
    # Изолируем bin-стор и home (для ~/.claude.json и PATH-операций).
    monkeypatch.setenv("SKILLERY_BIN_DIR", str(bin_dir))
    monkeypatch.setattr(mcp_register.Path, "home", staticmethod(lambda: home))
    # PATH-запись не трогает реальный реестр/rc — мокаем точечно.
    monkeypatch.setattr(path_store, "_already_on_path", lambda target: True)
    # deps: не зовём настоящий pip.
    monkeypatch.setattr(
        main_mod.tooling_install.deps_installer, "install_runtime_dependencies",
        lambda deps: {"installed": list(deps), "skipped": [], "failed": []},
    )
    return cfg, target, home, bin_dir


def test_real_install_tooling_lays_cli_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, home, bin_dir = _wire(tmp_path, monkeypatch)
    src = _tooling_skill_with_meta_toml(tmp_path)

    main_mod.cmd_install(
        slug="bx", channel="published", agent=None, scope="global",
        project=None, force=False, path=src, from_git=None, ref=None,
    )

    # CLI shim реально лёг в bin-стор (с привязкой к навыку bx).
    clis = path_store.list_clis()
    assert any(c["command_name"] == "bx" and c["skill_slug"] == "bx" for c in clis)
    # shim-файл существует.
    shim = next(c["shim"] for c in clis if c["command_name"] == "bx")
    assert Path(shim).exists()

    # MCP реально записан в ~/.claude.json.
    claude_cfg = home / ".claude.json"
    assert claude_cfg.exists()
    data = json.loads(claude_cfg.read_text(encoding="utf-8"))
    assert "bx-mcp" in data["mcpServers"]
    assert data["mcpServers"]["bx-mcp"]["command"] == "bx-mcp"
    assert data["mcpServers"]["bx-mcp"]["args"] == ["--stdio"]


def test_real_remove_purge_reverts_cli_and_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg, target, home, bin_dir = _wire(tmp_path, monkeypatch)
    src = _tooling_skill_with_meta_toml(tmp_path)

    main_mod.cmd_install(
        slug="bx", channel="published", agent=None, scope="global",
        project=None, force=False, path=src, from_git=None, ref=None,
    )
    # sanity: поставилось
    assert any(c["command_name"] == "bx" for c in path_store.list_clis())

    main_mod.cmd_remove(
        slug="bx", scope="global", project=None, keep_local=False,
        purge=True, agent=None,
    )
    # CLI снят из стора.
    assert not any(c["command_name"] == "bx" for c in path_store.list_clis())
    # MCP снят из конфига агента.
    data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    assert "bx-mcp" not in data.get("mcpServers", {})
