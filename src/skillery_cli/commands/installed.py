"""``skillery installed`` — какие навыки установлены на этом устройстве."""
from __future__ import annotations

from typing import Any

import typer

from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data


def cmd_installed(
    scope: str = typer.Option(
        "all", "--scope", help="global | project | all (по умолчанию all)"
    ),
) -> None:
    """Список установленных навыков (из центрального стора + project scope).

    Показывает имя, версию, источник и путь. В ``--json`` — структурой для
    скриптов/агентов.
    """
    from skillery_cli.commands.analytics import _scan_store
    from rich.console import Console
    console = Console()

    cfg = ClientConfig.load()
    items: list[dict[str, Any]] = _scan_store(cfg.effective_store_dir())

    payload = {"installed": items, "count": len(items)}

    def _render(p: dict) -> None:
        rows = p["installed"]
        if not rows:
            console.print("[dim]Навыки не установлены.[/]")
            return
        console.print(f"[bold]Установлено навыков: {len(rows)}[/]")
        for it in rows:
            name = it.get("slug") or it.get("name")
            ver = it.get("version") or "—"
            src = it.get("source") or ""
            console.print(f"  • [bold]{name}[/] v{ver} [dim]{src}[/]")

    _ = scope  # стор — единый источник; project-скан добавим при необходимости
    emit_data(payload, text_renderer=_render)
