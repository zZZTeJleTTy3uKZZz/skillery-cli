"""Оркестрация исполняемых артефактов навыка при установке (E3 фаза 2).

Связывает фазу 1 (``path_store`` — CLI-шимы + PATH) с регистрацией MCP
(``mcp_register``) и установкой runtime-зависимостей (``deps_installer``).
Точка входа install-flow после материализации навыка::

    apply_tooling_artifacts(install_result, agent_target=target, project=...,
                            manifest=bundle_manifest)

Делает (только если навык несёт артефакты — иначе полный no-op, prompt-навык не
затрагивается):
1. ``runtime_dependencies`` → ``deps_installer.install_runtime_dependencies``;
2. каждая ``[[cli]]`` → ``path_store.add_cli`` (entrypoint резолвится в
   исполняемую команду), затем ``ensure_on_path()`` РОВНО один раз;
3. каждый ``[[mcp]]`` → ``mcp_register.register_mcp`` в конфиг агента.

При disable/remove навыка — ``revert_tooling_artifacts(manifest, ...)``:
снимает CLI (``remove_cli`` по ``command_name``) и MCP (``unregister_mcp`` по
``server_name``).

КРИТИЧНО (graceful degradation, эталон reverse-factory): любое исключение в
любой ветке ловится и кладётся в отчёт со ``status=error`` — установка/снятие
самого навыка НЕ ломается. Манифест берётся из install-bundle (E6-поля
``kind``/``cli``/``mcp``/``runtime_dependencies``) либо из ``result.manifest``.
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any

from skills_hub_cli.core import deps_installer, mcp_register, path_store

# Манифест считаем «несущим артефакты», если есть kind=tooling ЛИБО любая из
# секций cli/mcp/runtime_dependencies непуста (локальная установка может не
# проставить kind, но нести секции — действуем по факту наличия артефактов).
_TOOLING_KIND = "tooling"


def _manifest_of(result: Any, manifest: dict | None) -> dict[str, Any]:
    if manifest is not None:
        return manifest
    m = getattr(result, "manifest", None)
    return m if isinstance(m, dict) else {}


def _skill_ref(result: Any) -> str:
    """Идентификатор навыка для привязки CLI: slug, иначе skill_id строкой."""
    slug = getattr(result, "slug", None)
    if slug:
        return str(slug)
    sid = getattr(result, "skill_id", None)
    return str(sid) if sid is not None else ""


def _cli_list(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    raw = manifest.get("cli")
    return [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []


def _mcp_list(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    raw = manifest.get("mcp")
    return [s for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []


def _deps_list(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    raw = manifest.get("runtime_dependencies")
    return [d for d in raw if isinstance(d, dict)] if isinstance(raw, list) else []


def _has_artifacts(manifest: dict[str, Any]) -> bool:
    if str(manifest.get("kind") or "") == _TOOLING_KIND:
        return True
    return bool(
        _cli_list(manifest) or _mcp_list(manifest) or _deps_list(manifest)
    )


# --------------------------------------------------------------------------
#  entrypoint → исполняемая команда для shim
# --------------------------------------------------------------------------
def _entrypoint_to_command(entrypoint: str | None, *, store_dir: Path | None) -> str | None:
    """Привести E6 ``entrypoint`` к команде для ``path_store.add_cli``.

    - ``None`` / пусто → ``None`` (CLI без энтрипоинта пропускаем);
    - ``module:callable`` (console-script спецификация) → python-команда,
      вызывающая callable текущим интерпретатором (кроссплатформенно, без
      зависимости от того, поставлен ли console-script в venv);
    - всё прочее (``python -m pkg`` / путь к exe) → как есть (path_store сам
      различит путь-к-файлу и команду).
    """
    if not entrypoint or not str(entrypoint).strip():
        return None
    ep = str(entrypoint).strip()
    # module:callable — ровно одно двоеточие, без пробелов, без разделителей пути.
    if (
        ":" in ep
        and " " not in ep
        and "/" not in ep
        and "\\" not in ep
        and ep.count(":") == 1
    ):
        module, _, func = ep.partition(":")
        if module and func:
            py = _quote(sys.executable)
            # sys.exit(func()) — корректный код возврата CLI.
            code = f"import sys; from {module} import {func}; sys.exit({func}())"
            return f'{py} -c "{code}"'
    return ep


def _quote(value: str) -> str:
    """Кавычки для пути с пробелами (POSIX shlex; на Windows — двойные)."""
    if " " not in value:
        return value
    if sys.platform == "win32":
        return f'"{value}"'
    return shlex.quote(value)


# --------------------------------------------------------------------------
#  apply
# --------------------------------------------------------------------------
def apply_tooling_artifacts(
    result: Any,
    *,
    agent_target: Any,
    project: Path | None = None,
    manifest: dict | None = None,
) -> dict[str, Any]:
    """Поставить CLI/MCP/deps навыка по его манифесту. Никогда не бросает.

    Возвращает отчёт ``{cli: [...], mcp: [...], deps: {...}}``. Для prompt-навыка
    (нет артефактов) — пустой отчёт, ни одна под-система не вызывается.
    """
    man = _manifest_of(result, manifest)
    report: dict[str, Any] = {"cli": [], "mcp": [], "deps": {}}
    if not _has_artifacts(man):
        return report

    store_dir = getattr(result, "store_dir", None)
    skill_ref = _skill_ref(result)

    # 1. runtime-зависимости (deps_installer сам graceful, но обернём на всякий).
    deps = _deps_list(man)
    if deps:
        try:
            report["deps"] = deps_installer.install_runtime_dependencies(deps)
        except Exception as exc:  # noqa: BLE001 — deps не валят install
            report["deps"] = {"error": str(exc)}

    # 2. CLI-шимы + PATH (ensure_on_path РОВНО один раз, после первого add_cli).
    clis = _cli_list(man)
    path_ensured = False
    for tool in clis:
        command_name = str(tool.get("command_name") or "").strip()
        if not command_name:
            report["cli"].append({"status": "error", "reason": "command_name пуст"})
            continue
        command = _entrypoint_to_command(tool.get("entrypoint"), store_dir=store_dir)
        if command is None:
            report["cli"].append(
                {
                    "command_name": command_name,
                    "status": "skipped",
                    "reason": "нет entrypoint — нечего класть в shim",
                }
            )
            continue
        try:
            shim = path_store.add_cli(command_name, command, skill_slug=skill_ref)
            entry = {
                "command_name": command_name,
                "status": "installed",
                "shim": str(shim),
            }
            if not path_ensured:
                try:
                    entry["path"] = path_store.ensure_on_path()
                except Exception as exc:  # noqa: BLE001
                    entry["path"] = {"status": "error", "reason": str(exc)}
                path_ensured = True
            report["cli"].append(entry)
        except Exception as exc:  # noqa: BLE001 — сбой CLI не валит install
            report["cli"].append(
                {"command_name": command_name, "status": "error", "reason": str(exc)}
            )

    # 3. MCP-серверы в конфиг агента.
    for server in _mcp_list(man):
        try:
            res = mcp_register.register_mcp(
                server, agent_target=agent_target, project=project
            )
        except Exception as exc:  # noqa: BLE001 — сбой MCP не валит install
            res = {
                "status": "error",
                "server_name": server.get("server_name"),
                "reason": str(exc),
            }
        report["mcp"].append(res)

    return report


# --------------------------------------------------------------------------
#  revert (disable / remove)
# --------------------------------------------------------------------------
def revert_tooling_artifacts(
    manifest: dict | None,
    *,
    agent_target: Any,
    project: Path | None = None,
) -> dict[str, Any]:
    """Снять CLI/MCP навыка (disable/remove). Никогда не бросает.

    runtime-зависимости НЕ удаляются (могут быть общими; деинсталляция пакетов —
    зона ответственности пользователя). Возвращает отчёт
    ``{cli_removed: [...], mcp_unregistered: [...], errors: [...]}``.
    """
    man = manifest if isinstance(manifest, dict) else {}
    report: dict[str, Any] = {"cli_removed": [], "mcp_unregistered": [], "errors": []}
    if not _has_artifacts(man):
        return report

    for tool in _cli_list(man):
        command_name = str(tool.get("command_name") or "").strip()
        if not command_name:
            continue
        try:
            if path_store.remove_cli(command_name):
                report["cli_removed"].append(command_name)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"command_name": command_name, "reason": str(exc)})

    for server in _mcp_list(man):
        server_name = str(server.get("server_name") or "").strip()
        if not server_name:
            continue
        try:
            res = mcp_register.unregister_mcp(
                server_name, agent_target=agent_target, project=project
            )
            if res.get("status") in ("unregistered", "absent", "manual"):
                report["mcp_unregistered"].append(server_name)
        except Exception as exc:  # noqa: BLE001
            report["errors"].append({"server_name": server_name, "reason": str(exc)})

    return report
