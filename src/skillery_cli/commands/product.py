"""``skillery product …`` — установка продуктов по install-plan (ADR-0001).

Три глагола и ни одного лишнего:

* ``install --plan-file`` — исполнить план backend'а; ``--dry-run`` показывает
  шаги, ничего не трогая;
* ``list`` — что стоит на этой машине (из ``~/.skillery/products.json``);
* ``remove <slug>`` — снять ровно то, что записано в состоянии.

Вывод — общий контракт CLI: по умолчанию текст, ``--json`` — машинный
результат в stdout, сообщения в stderr (см. ``skillery_cli.output``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skillery_cli.commands import _common
from skillery_cli.output import emit_data, emit_error
from skillery_cli.products.engine import (
    STATUS_FAILED,
    PlanRun,
    install_plan,
    remove_product,
)
from skillery_cli.products.handlers.base import InstallContext
from skillery_cli.products.paths import home_paths
from skillery_cli.products.plan import PlanError, load_install_plan
from skillery_cli.products.runner import SubprocessRunner
from skillery_cli.products.state import InstalledState, StateError

console = Console()

NOT_INSTALLED_HINT = (
    "продукт не установлен на этой машине: `skillery product list` показывает, "
    "что стоит"
)


def _context(*, dry_run: bool = False) -> InstallContext:
    return InstallContext(
        paths=home_paths(),
        runner=SubprocessRunner(),
        state=InstalledState.load(),
        dry_run=dry_run,
    )


def _render_plan(payload: dict[str, Any]) -> None:
    table = Table(title="Продукты по плану")
    table.add_column("продукт")
    table.add_column("компонент")
    table.add_column("тип")
    table.add_column("статус")
    table.add_column("что сделано")
    for product in payload.get("products", []):
        for component in product.get("components", []):
            table.add_row(
                str(product.get("slug") or ""),
                str(component.get("key") or ""),
                str(component.get("type") or ""),
                str(component.get("status") or ""),
                str(component.get("detail") or ""),
            )
    console.print(table)
    summary = payload.get("summary") or {}
    console.print(
        "Итого: "
        f"продуктов {summary.get('products', 0)}, "
        f"компонентов {summary.get('components', 0)}, "
        f"установлено {summary.get('installed', 0)}, "
        f"уже стояло {summary.get('already_installed', 0)}, "
        f"пропущено {summary.get('skipped', 0)}, "
        f"ошибок {summary.get('failed', 0)}"
    )


def _render_list(payload: dict[str, Any]) -> None:
    table = Table(title=f"Установленные продукты (всего: {payload.get('count', 0)})")
    table.add_column("продукт")
    table.add_column("версия")
    table.add_column("компоненты")
    for product in payload.get("products", []):
        components = ", ".join(
            f"{component.get('key')} ({component.get('status')})"
            for component in product.get("components", [])
        )
        table.add_row(
            str(product.get("slug") or ""),
            str(product.get("version") or ""),
            components,
        )
    console.print(table)


def _render_removed(payload: dict[str, Any]) -> None:
    table = Table(title=f"Снятие продукта {payload.get('slug', '')}")
    table.add_column("компонент")
    table.add_column("тип")
    table.add_column("статус")
    for component in payload.get("components", []):
        table.add_row(
            str(component.get("key") or ""),
            str(component.get("type") or ""),
            str(component.get("status") or ""),
        )
    console.print(table)


def cmd_product_install(
    plan_file: Path = typer.Option(
        ...,
        "--plan-file",
        help="JSON install-plan (ответ backend: products → components → artifact).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Показать шаги установки, ничего не скачивая и не запуская.",
    ),
) -> None:
    """Поставить продукты по install-plan (идемпотентно)."""
    try:
        plan = load_install_plan(plan_file)
    except PlanError as exc:
        emit_error("VALIDATION", str(exc))
        raise typer.Exit(1) from exc

    ctx = _context(dry_run=dry_run)
    try:
        result: PlanRun = _common.run(install_plan(plan, ctx))
    except StateError as exc:
        emit_error("STATE", str(exc))
        raise typer.Exit(1) from exc

    payload = result.to_dict()
    emit_data(payload, text_renderer=_render_plan)
    if not dry_run and result.summary.get("failed", 0):
        raise typer.Exit(1)


def cmd_product_list() -> None:
    """Показать установленные продукты и их компоненты."""
    try:
        state = InstalledState.load()
    except StateError as exc:
        emit_error("STATE", str(exc))
        raise typer.Exit(1) from exc

    products: list[dict[str, Any]] = []
    for slug in sorted(state.products):
        entry = state.products[slug]
        components = entry.get("components", {})
        products.append(
            {
                "slug": slug,
                "version": str(entry.get("version") or ""),
                "components": [
                    {
                        "key": key,
                        "type": str(meta.get("type") or ""),
                        "status": str(meta.get("status") or ""),
                    }
                    for key, meta in sorted(components.items())
                ],
            }
        )
    emit_data(
        {"products": products, "count": len(products)}, text_renderer=_render_list
    )


def cmd_product_remove(
    slug: str = typer.Argument(..., metavar="SLUG", help="slug продукта"),
) -> None:
    """Снять продукт: обработчики удаляют то, что сами поставили."""
    ctx = _context()
    try:
        result = _common.run(remove_product(slug, ctx))
    except StateError as exc:
        emit_error("STATE", str(exc))
        raise typer.Exit(1) from exc

    if result.status == "absent":
        emit_error("NOT_INSTALLED", f"продукт {slug!r}: {NOT_INSTALLED_HINT}")
        raise typer.Exit(1)

    payload = result.to_dict()
    emit_data(payload, text_renderer=_render_removed)
    if result.status == STATUS_FAILED:
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    """Регистрирует группу ``product`` (установка продуктов)."""
    product_app = typer.Typer(
        no_args_is_help=True,
        help="Продукты: поставить по install-plan, список, снять.",
    )
    product_app.command("install")(cmd_product_install)
    product_app.command("list")(cmd_product_list)
    product_app.command("remove")(cmd_product_remove)
    app.add_typer(product_app, name="product")

