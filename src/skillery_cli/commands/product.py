"""``skillery product …`` — установка продуктов по install-plan (ADR-0001).

Четыре глагола и ни одного лишнего:

* ``install --plan-file`` — исполнить план backend'а; ``--dry-run`` показывает
  шаги, ничего не трогая;
* ``sync`` — поставить всё назначенное устройству и подтянуть новые версии
  (``--dry-run``, ``--prune`` — снять не назначенное);
* ``list`` — что стоит на этой машине (из ``~/.skillery/products.json``);
* ``remove <slug>`` — снять ровно то, что записано в состоянии.

Вывод — JSON по умолчанию и это осознанное исключение из общего контракта CLI:
команды читает установщик (``skillery sync``, backend, агент), а не человек,
и таблица в stdout ломала бы разбор. Человеку — ``--text``: таблицы, те же
данные, читаемый вид (см. ``skillery_cli.output``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig, load_tokens
from skillery_cli.core.transport import HubClient
from skillery_cli.output import emit_data, emit_error, init_output_mode
from skillery_cli.products.engine import (
    STATUS_FAILED,
    PlanRun,
    install_plan,
    remove_product,
)
from skillery_cli.products.handlers.base import InstallContext
from skillery_cli.products.plan import PlanError, load_install_plan
from skillery_cli.products.state import InstalledState, StateError
from skillery_cli.products.sync import (
    InstallPlanUnavailableError,
    ProductSyncError,
    build_install_context,
    sync_products,
)

console = Console()

NOT_INSTALLED_HINT = (
    "продукт не установлен на этой машине: `skillery product list` показывает, "
    "что стоит"
)

TEXT_HELP = "Таблицы для человека вместо JSON (данные те же)."


def _apply_output_mode(*, text: bool) -> None:
    """JSON по умолчанию; ``--text`` переключает на таблицы.

    Флаг передаётся явно (``text_flag``), а не через конфиг: у этих команд
    машина — основной потребитель, и молчаливая подмена режима настройкой
    человека ломала бы разбор. ``--text`` сильнее глобального ``--json``:
    это осознанный выбор конкретной команды (см. ``output.init_output_mode``).
    """
    init_output_mode(
        json_flag=not text,
        config_format="text" if text else "json",
        text_flag=text,
    )


def _context(*, dry_run: bool = False) -> InstallContext:
    return build_install_context(dry_run=dry_run)


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
    text: bool = typer.Option(False, "--text", help=TEXT_HELP),
) -> None:
    """Поставить продукты по install-plan (идемпотентно)."""
    _apply_output_mode(text=text)
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


def cmd_product_list(
    text: bool = typer.Option(False, "--text", help=TEXT_HELP),
) -> None:
    """Показать установленные продукты и их компоненты."""
    _apply_output_mode(text=text)
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
    text: bool = typer.Option(False, "--text", help=TEXT_HELP),
) -> None:
    """Снять продукт: обработчики удаляют то, что сами поставили."""
    _apply_output_mode(text=text)
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


def _render_sync(payload: dict[str, Any]) -> None:
    console.print(
        "[green]product sync[/]: "
        f"+установлено {len(payload.get('installed', []))}  "
        f"↑обновлено {len(payload.get('updated', []))}  "
        f"=уже стоит {len(payload.get('already', []))}  "
        f"?пропущено {len(payload.get('skipped', []))}  "
        f"×оставлено {len(payload.get('kept', []))}  "
        f"-снято {len(payload.get('pruned', []))}  "
        f"✗ошибок {len(payload.get('failed', []))}"
    )
    for slug in payload.get("kept", []):
        console.print(f"  [dim]{slug}: не назначен — оставлен (снять: --prune)[/]")
    for warning in payload.get("warnings", []):
        console.print(f"  [yellow]![/] {warning}")
    if payload.get("dry_run"):
        console.print("[dim]dry-run: ничего не изменено[/]")


def cmd_product_sync(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Показать, что будет поставлено, ничего не меняя."
    ),
    prune: bool = typer.Option(
        False,
        "--prune",
        help="Снять установленные продукты, которых нет в назначении.",
    ),
    channel: str = typer.Option(
        "",
        "--channel",
        help="Канал релизов (пусто — канал backend'а, stable).",
    ),
    text: bool = typer.Option(False, "--text", help=TEXT_HELP),
) -> None:
    """Поставить назначенное устройству и подтянуть новые версии продуктов."""
    _apply_output_mode(text=text)
    cfg = ClientConfig.load()
    if not cfg.is_logged_in():
        emit_error("NOT_LOGGED_IN", "Сначала: skillery login <invite>")
        raise typer.Exit(1)
    access, _refresh = load_tokens(cfg.user_email or "")
    if not access:
        emit_error("NO_TOKEN", "Локальный access-токен не найден. Сделайте login заново.")
        raise typer.Exit(1)

    # Ленивый импорт: commands.product не должен тянуть __main__ на импорте.
    from skillery_cli.__main__ import _make_refresh_callback

    ctx = _context(dry_run=dry_run)
    client = HubClient(
        base_url=cfg.base_url,
        access_token=access,
        on_token_refresh=_make_refresh_callback(cfg),
    )

    async def _do() -> Any:
        try:
            return await sync_products(
                client,
                ctx,
                dry_run=dry_run,
                prune=prune,
                channel=channel,
            )
        except (InstallPlanUnavailableError, ProductSyncError) as exc:
            # ``_common.run`` переводит RuntimeError в общий RUNTIME; здесь
            # нужен свой код — человеку важен не «сбой», а «backend старый».
            emit_error("PRODUCT_SYNC", str(exc))
            raise typer.Exit(1) from exc
        finally:
            await client.close()

    report = _common.run(_do())

    payload = report.to_dict()
    emit_data(payload, text_renderer=_render_sync)
    if report.failed:
        raise typer.Exit(1)


def register(app: typer.Typer) -> None:
    """Регистрирует группу ``product`` (установка продуктов)."""
    product_app = typer.Typer(
        no_args_is_help=True,
        help="Продукты: поставить по install-plan/назначению, список, снять.",
    )
    product_app.command("install")(cmd_product_install)
    product_app.command("sync")(cmd_product_sync)
    product_app.command("list")(cmd_product_list)
    product_app.command("remove")(cmd_product_remove)
    app.add_typer(product_app, name="product")

