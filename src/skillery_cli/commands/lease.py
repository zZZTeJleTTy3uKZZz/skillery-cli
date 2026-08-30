"""``skillery lease …`` — какие способности действуют СЕЙЧАС и до когда (#2447).

Роуты ``GET /me/leases`` и ``GET /me/leases/{id}`` до этой группы не звал никто:
CLI умел лизы только ВЫПИСЫВАТЬ (``PUT /me/leases`` в такте демона), а
посмотреть на выданное человек не мог нигде. Из-за этого любая проблема с
правом выглядела одинаково — «нет доступа» — и не отличалась от соседних:

* право не выдано вовсе (нет гранта) — лечится в вебе владельцем навыка;
* грант есть, но лиз не доехал на ЭТУ машину (демон стоит, сеть, другое
  устройство) — лечится ``skillery capability install`` / перезапуском демона;
* лиз был, но истёк — лечится сам на следующем такте.

``lease list`` показывает журнал хаба (чужой источник истины), а колонку «на
этой машине» даёт ``capability list``: две разные правды намеренно не
смешиваются в одной команде — иначе непонятно, кому не верить при расхождении.

Только чтение. Выдача лиза — следствие гранта, и «выписать лиз руками» значило
бы обещать право, которого хаб не давал.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.output import emit_data

console = Console()


def _parse_dt(value: Any) -> datetime | None:
    """ISO-8601 хаба → aware datetime; мусор — ``None``.

    Разбор намеренно не валит команду: формат даты — забота бэкенда, а
    диагностическая колонка «сколько осталось» не стоит того, чтобы человек
    вместо журнала лизов увидел трейсбек.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def remaining(expires_at: Any, *, now: datetime | None = None) -> str:
    """Человеческое «сколько осталось» — главный ответ этой группы.

    Голая дата истечения заставляет считать в уме, а именно на этом вопросе
    («лиз ещё живой или уже нет?») человек и застревает. Истёкшие строки хаб не
    отдаёт, но часы машины могут убежать вперёд — тогда честнее сказать
    «истёк», чем показать отрицательный срок.
    """
    dt = _parse_dt(expires_at)
    if dt is None:
        return "—"
    delta = dt - (now or datetime.now(timezone.utc))
    seconds = int(delta.total_seconds())
    if seconds <= 0:
        return "истёк"
    hours, minutes = divmod(seconds // 60, 60)
    if hours >= 24:
        return f"{hours // 24} дн {hours % 24} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{minutes} мин"


def _table(items: list[dict[str, Any]]) -> Table:
    table = Table(title=f"Действующие лизы (всего: {len(items)})")
    table.add_column("способность")
    table.add_column("устройство")
    table.add_column("действует до")
    table.add_column("осталось")
    table.add_column("jti")
    for item in items:
        table.add_row(
            str(item.get("capability") or item.get("capability_id") or "—"),
            str(item.get("device_id") or "—"),
            str(item.get("expires_at") or "—"),
            remaining(item.get("expires_at")),
            str(item.get("jti") or "—"),
        )
    return table


def cmd_lease_list(
    device: str = typer.Option(
        None,
        "--device",
        help="Сузить до одного устройства (client_device_id). "
             "По умолчанию — весь парк.",
    ),
    this_device: bool = typer.Option(
        False,
        "--this-device",
        help="Только лизы ЭТОЙ машины (подставляет её client_device_id).",
    ),
) -> None:
    """Какие способности мне сейчас разрешены и до когда (GET /me/leases).

    ``--this-device`` — ради частого случая «у меня тут не работает»: id машины
    человек наизусть не помнит, а без сужения журнал парка из десятка устройств
    не отвечает на его вопрос.
    """
    if this_device and device:
        from skillery_cli.output import emit_error

        emit_error("VALIDATION", "--device и --this-device взаимоисключающи")
        raise typer.Exit(2)
    target = device
    if this_device:
        from skillery_cli.core.identity import device_uid

        target = device_uid()

    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            data = await client.list_my_leases(device_id=target)
        finally:
            await client.close()
        items = list(data.get("items") or [])
        payload = {"items": items, "total": int(data.get("total") or len(items))}

        def _render(p: dict[str, Any]) -> None:
            if not p["items"]:
                console.print(
                    "[yellow]Действующих лизов нет[/] — либо права на "
                    "способности не выданы, либо демон ещё не сверялся "
                    "(`skillery capability list` покажет, что разрешено)"
                )
                return
            console.print(_table(p["items"]))

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


def cmd_lease_get(
    jti: str = typer.Argument(..., metavar="JTI", help="Идентификатор лиза (jti)"),
) -> None:
    """Карточка одного лиза (GET /me/leases/{id}).

    Адрес из ``Location`` после выдачи ведёт именно сюда; чужой лиз хаб отдаёт
    как 404 — ``jti`` предъявительский, и 403 подтвердил бы его существование.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            lease = await client.get_my_lease(jti)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            console.print(f"[bold]{p.get('capability') or '—'}[/] (лиз {p.get('jti')})")
            console.print(f"  способность id: {p.get('capability_id') or '—'}")
            console.print(f"  навык id:       {p.get('skill_id') or '—'}")
            console.print(f"  устройство:     {p.get('device_id') or '—'}")
            console.print(f"  выдан:          {p.get('issued_at') or '—'}")
            console.print(
                f"  действует до:   {p.get('expires_at') or '—'} "
                f"(осталось {remaining(p.get('expires_at'))})"
            )

        emit_data(lease, text_renderer=_render)

    _common.run(_do())


def register(app: typer.Typer) -> None:
    """Регистрирует группу ``lease`` — без гейта прав.

    Это ответ на вопрос «что у МЕНЯ сейчас действует» (``/me/…``, актор из
    claims): прятать его не от кого, а спрятанная диагностика ровно там, где
    человек застрял, стоит дороже любой экономии на правах.
    """
    lease_app = typer.Typer(
        no_args_is_help=True,
        help="Лизы: какие способности действуют сейчас и до когда.",
    )
    lease_app.command("list")(cmd_lease_list)
    lease_app.command("get")(cmd_lease_get)
    app.add_typer(lease_app, name="lease")
