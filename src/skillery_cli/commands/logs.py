"""``skillery logs`` — журнал действий CLI/демона + управление уровнем."""
from __future__ import annotations

import typer

from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data


def cmd_logs(
    lines: int = typer.Option(50, "--lines", "-n", help="Сколько последних строк"),
    which: str = typer.Option(
        "cli", "--file", help="cli | daemon — какой журнал показать"
    ),
    level: str = typer.Option(
        None, "--set-level",
        help="Установить уровень: error|warning|info|debug|trace (по умолчанию error)",
    ),
) -> None:
    """Показать журнал (~/.skillery/logs/) и/или сменить уровень логирования.

    Уровни как в бэке/вебе: по умолчанию ERROR; DEBUG/TRACE — полный трейс для
    разбора. Логи пишутся структурой (JSON-строки).
    """
    from rich.console import Console
    console = Console()
    from skillery_cli.core.logging_setup import log_dir, normalize_level

    cfg = ClientConfig.load()

    # Смена уровня (персистится в конфиг).
    if level is not None:
        norm = normalize_level(level)
        cfg.log_level = norm.lower()
        cfg.save()
        emit_data(
            {"event": "log_level_set", "level": norm.lower()},
            text_renderer=lambda _: console.print(
                f"[green]✓[/] Уровень логов: [bold]{norm}[/] "
                f"(файлы в {log_dir()})"
            ),
        )
        return

    fname = "daemon.log" if which.startswith("d") else "cli.log"
    path = log_dir() / fname
    tail: list[str] = []
    if path.exists():
        try:
            all_lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            tail = all_lines[-max(1, lines):]
        except Exception:  # noqa: BLE001
            tail = []

    payload = {
        "file": str(path),
        "level": cfg.log_level,
        "lines": tail,
    }

    def _render(p: dict) -> None:
        console.print(
            f"[dim]{p['file']} · уровень {p['level']} · "
            f"сменить: skillery logs --set-level debug[/]"
        )
        if not p["lines"]:
            console.print("[dim]Журнал пуст (на уровне error пишутся только ошибки).[/]")
            return
        for line in p["lines"]:
            console.print(line)

    emit_data(payload, text_renderer=_render)
