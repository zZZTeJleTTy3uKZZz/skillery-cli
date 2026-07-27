"""``skillery installed`` — какие навыки установлены на этом устройстве.

#1146. Раньше команда сканировала ТОЛЬКО центральный стор, а ``--scope``
принимала и молча игнорировала (``_ = scope``). Из-за этого выдача расходилась
с реальностью самым неприятным способом: ``skillery remove vk`` снимал
project-ссылку и печатал «✓ Удалён», а следующий ``installed`` показывал vk —
потому что в сторе он и остался, а про scope команда ничего не знала.

Теперь ``--scope`` работает по-настоящему:

* ``global`` — навыки центрального стора (материализованы один раз; глобальный
  каталог агента ссылается именно на них);
* ``project`` — навыки, включённые в ТЕКУЩИЙ проект (каталог таргет-агента в
  корне проекта), с признаком ``linked`` (ссылка на стор или отдельная копия);
* ``all`` (дефолт) — и то и другое, каждая запись со своим ``scope``.

Сканеры не дублируются: обе выборки берутся из ``commands.analytics``
(``_scan_store`` / ``_scan_project``) — ровно потому, что два независимых
сканера в прошлый раз и разъехались.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import typer

from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data, emit_error

_SCOPES = ("global", "project", "all")


def _project_root(cfg: ClientConfig, project: Optional[Path]) -> Path:
    """Корень проекта для scope=project — та же логика, что у install/remove."""
    if project is not None:
        return Path(project).resolve()
    if cfg.default_project_dir:
        return Path(cfg.default_project_dir).resolve()
    return Path.cwd()


def cmd_installed(
    scope: str = typer.Option(
        "all", "--scope", help="global | project | all (по умолчанию all)"
    ),
    project: Optional[Path] = typer.Option(
        None, "--project", help="Корень проекта для --scope project (default: cwd)"
    ),
    agent: Optional[str] = typer.Option(
        None, "--agent", help="Таргет-агент (default: из конфига)"
    ),
) -> None:
    """Список установленных навыков (центральный стор + project scope).

    Показывает имя, версию, источник, scope и путь. Для project-записей ещё и
    ``linked``: ссылка на стор (норма) или отдельная копия (copy-fallback там,
    где ФС не даёт symlink/junction) — у копии обновление стора само не
    подхватится. В ``--json`` — структурой для скриптов/агентов.
    """
    from rich.console import Console

    from skillery_cli.commands.analytics import _scan_project, _scan_store
    from skillery_cli.core.agents import get_target

    console = Console()

    # Прямой вызов из тестов приносит sentinel typer.OptionInfo вместо значения.
    if not isinstance(scope, str):
        scope = "all"
    if scope not in _SCOPES:
        emit_error(
            "VALIDATION", f"scope должен быть global|project|all, получено: {scope}"
        )
        raise typer.Exit(1)
    if not isinstance(project, Path):
        project = None
    if not isinstance(agent, str):
        agent = None

    cfg = ClientConfig.load()
    items: list[dict[str, Any]] = []

    if scope in ("global", "all"):
        for it in _scan_store(cfg.effective_store_dir()):
            # scope записи — по факту КАТАЛОГА, а не по метке в мете: мета
            # хранит scope последней установки, а папка стора глобальна всегда.
            items.append({**it, "scope": "global", "linked": None})

    project_root: Optional[Path] = None
    if scope in ("project", "all"):
        project_root = _project_root(cfg, project)
        target = get_target(agent or cfg.agent)
        for it in _scan_project(target, project_root):
            items.append(
                {**it, "name": it.get("slug") or it.get("ref"), "scope": "project"}
            )

    payload = {
        "installed": items,
        "count": len(items),
        "scope": scope,
        "project": str(project_root) if project_root is not None else None,
    }

    def _render(p: dict) -> None:
        rows = p["installed"]
        if not rows:
            console.print("[dim]Навыки не установлены.[/]")
            return
        console.print(f"[bold]Установлено навыков: {len(rows)}[/]")
        for it in rows:
            name = it.get("slug") or it.get("name") or it.get("ref")
            ver = it.get("version") or "—"
            src = it.get("source") or ""
            row_scope = it.get("scope") or ""
            link = ""
            if row_scope == "project":
                link = " [dim](ссылка)[/]" if it.get("linked") else " [yellow](копия)[/]"
            console.print(
                f"  • [bold]{name}[/] v{ver} [cyan]{row_scope}[/] [dim]{src}[/]{link}"
            )

    emit_data(payload, text_renderer=_render)
