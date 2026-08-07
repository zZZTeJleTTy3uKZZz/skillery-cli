"""``skillery capability …`` — способности как самостоятельный ресурс (#1489).

Линия «выдаём СПОСОБНОСТЬ, а не навык целиком». До этой группы слова
``capability`` в CLI не было вовсе: способность можно было выдать в вебе, но
на устройстве она никак не называлась — человек видел только навык-носитель
(``grok-chat``) и не мог сказать, что именно ему разрешено.

────────────────────────────────────────────────────────────────────────────
ИМЯ СПОСОБНОСТИ — ОДНО НА ВСЮ СИСТЕМУ

``grok_transcriber`` — это ключ entry-point группы ``skillery.plugins``, тот
же самый, по которому gateway резолвит способность в ``load_capability``, и
тот же, что стоит в ``cap`` лиза. Второго нейминга (человеческого «названия
для CLI») не заводим — инвариант §10.9 контракта лиза: разошедшиеся словари
означают, что выданное право и исполняемый код перестают совпадать, и никто
этого не замечает.

Отсюда же следует, ПОЧЕМУ имя обязано быть глобально уникальным и почему
конфликт ловится на публикации (409 ``CAPABILITY_NAME_CONFLICT``): gateway про
``skill_id`` не знает, и два навыка с одинаковым именем делают лиз неразрешимым
— какой из двух кодов исполнится, решил бы случайный порядок entry-point'ов на
машине.

────────────────────────────────────────────────────────────────────────────
ГЛАГОЛЫ И ГРАНИЦА «ОТЗЫВ ≠ СНЯТИЕ»

``list`` / ``get`` — что мне разрешено и что это такое.
``install`` / ``remove`` — ФАЙЛЫ навыка-носителя на этой машине.
``grant`` / ``revoke`` — ПРАВО другого субъекта (это про хаб, не про диск).

Две последние пары намеренно разведены, и это главное решение модуля.
``remove`` не отзывает право, а ``revoke`` не удаляет файлы: способность живёт
в дистрибутиве вместе с соседями, и снятие носителя из-за одной отозванной
способности сломало бы соседнюю, право на которую никто не отзывал (§6.3
контракта). Поэтому ``capability remove`` снимает навык ТОЛЬКО когда его не
держит ни одна другая способность с действующим правом — решение принимает
единственная реализация правила, :func:`skillery_cli.core.leases.removal_blockers`,
общая с заданием ``action=remove`` из очереди устройства.
"""
from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.core.transport import ApiError
from skillery_cli.output import emit_data, emit_error, emit_message

console = Console()

#: Кому можно выдать доступ (зеркало ``AccessGrantTargetType`` бэкенда).
TARGET_TYPES = ("user", "company", "tag")

#: Уровни доступа гранта (зеркало backend-валидации).
ROLES = ("viewer", "editor", "admin")

#: Текст конфликта имени — ЕДИНСТВЕННОЕ место, где он объясняется человеку.
#:
#: Хаб отвечает ``409 CAPABILITY_NAME_CONFLICT``, и голый код в терминале —
#: это тикет в поддержку: автор навыка не знает ни что имя глобальное, ни что
#: делать дальше. Поэтому здесь и причина, и следующее действие.
NAME_CONFLICT_HINT = (
    "Имя способности уже занято другим навыком. Имя = ключ entry-point "
    "skillery.plugins и обязано быть уникальным на весь хаб: потребитель "
    "(gateway) резолвит способность ТОЛЬКО по имени и про навык не знает. "
    "Переименуйте способность в блоке [[capabilities]] файла _skill_meta.toml "
    "(например, добавьте префикс сервиса: grok_transcriber) и опубликуйте "
    "версию заново."
)


def explain_api_error(exc: ApiError) -> str | None:
    """Человеческий текст для кодов, которые сами по себе ничего не говорят.

    ``None`` ⇒ штатное сообщение хаба и так понятно, подменять его не надо:
    переводить ВСЕ ошибки в свои формулировки значило бы завести вторую,
    отстающую от бэкенда копию словаря причин.
    """
    if exc.code == "CAPABILITY_NAME_CONFLICT":
        occupied = str((exc.details or {}).get("name") or "").strip()
        head = f"Способность «{occupied}»: " if occupied else ""
        return f"{head}{NAME_CONFLICT_HINT}"
    return None


def _capability_table(items: list[dict[str, Any]], leases: dict[str, str]) -> Table:
    table = Table(title=f"Мои способности (всего: {len(items)})")
    table.add_column("имя")
    table.add_column("id")
    table.add_column("вид")
    table.add_column("навык-носитель")
    table.add_column("лиз")
    for item in items:
        name = str(item.get("name") or "—")
        table.add_row(
            name,
            str(item.get("id") or "—"),
            str(item.get("kind") or "—"),
            str(item.get("skill") or item.get("skill_id") or "—"),
            leases.get(name, "—"),
        )
    return table


def _local_lease_state() -> dict[str, str]:
    """Состояние лиза каждой способности НА ЭТОЙ машине (без сети).

    Витрина хаба отвечает на вопрос «что мне разрешено», а человек, у которого
    что-то не запускается, спрашивает другое: «а на моей машине оно сейчас
    действует». Без этой колонки ``capability list`` показывал бы зелёный
    список при мёртвом локальном лизе.
    """
    out: dict[str, str] = {}
    try:
        from leasekit import JwkSet, LeaseVerdict, inspect_lease

        from skillery_cli.core.leases import (
            RequirementsIndex,
            current_subject,
            lease_store,
            state_dir,
        )

        subject = current_subject()
        if subject is None:
            return out
        store = lease_store()
        index = RequirementsIndex.load()
        keys = JwkSet.from_file(state_dir() / "hub_keys.json")
        clock = store.clock(offset=index.hub_offset)
        for name in store.capabilities():
            check = inspect_lease(
                store.token(name), subject=subject, capability=name,
                clock=clock, keys=keys,
            )
            out[name] = (
                "действует" if check.verdict is LeaseVerdict.VALID
                else str(check.verdict.value)
            )
    except Exception:  # noqa: BLE001 — диагностическая колонка не валит команду
        return out
    return out


# ---------------------------------------------------------------------------
#  Чтение
# ---------------------------------------------------------------------------
def cmd_capability_list() -> None:
    """Что мне разрешено (GET /me/capabilities) — каноническими именами.

    ``supports_lease=true`` уходит всегда: способность с ``requires_lease`` хаб
    fail-closed не отдаёт клиенту, который не заявил, что умеет проверять лиз
    (§8 контракта). Без флага платная способность просто не доехала бы, и
    список молча врал бы.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            items = await client.list_my_capabilities(supports_lease=True)
        finally:
            await client.close()
        leases = _local_lease_state()
        payload = {
            "items": items,
            "total": len(items),
            "leases": leases,
        }

        def _render(p: dict[str, Any]) -> None:
            if not p["items"]:
                console.print(
                    "[yellow]Способностей не выдано[/] — их выдаёт владелец "
                    "навыка (`skillery capability grant`)"
                )
                return
            console.print(_capability_table(p["items"], p["leases"]))

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


def cmd_capability_get(
    capability: str = typer.Argument(
        ..., metavar="ИМЯ_ИЛИ_ID", help="Имя способности (grok_transcriber) или её id"
    ),
) -> None:
    """Карточка способности (GET /capabilities/{id}); принимает имя или id."""
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            item = await client.get_capability(capability)
        finally:
            await client.close()

        def _render(p: dict[str, Any]) -> None:
            console.print(f"[bold]{p.get('name')}[/] (id={p.get('id')})")
            console.print(f"  навык-носитель: {p.get('skill_id')}")
            console.print(f"  вид:            {p.get('kind') or '—'}")
            console.print(f"  entry-point:    {p.get('entry_point') or '—'}")
            console.print(f"  доступ:         {p.get('access_level') or '—'}")
            console.print(
                f"  требует лиз:    {'да' if p.get('requires_lease') else 'нет'}"
            )
            console.print(
                f"  выдана мне:     {'да' if p.get('granted') else 'нет'}"
            )

        emit_data(item, text_renderer=_render)

    _common.run(_do())


# ---------------------------------------------------------------------------
#  Файлы на этой машине
# ---------------------------------------------------------------------------
def cmd_capability_install(
    capability: str = typer.Argument(
        ..., metavar="ИМЯ_ИЛИ_ID", help="Имя способности (grok_transcriber) или её id"
    ),
    channel: str = typer.Option("published", "--channel"),
    agent: str = typer.Option(None, "--agent"),
) -> None:
    """Поставить навык-носитель способности и ЗАПОМНИТЬ, зачем он поставлен.

    Три шага, и третий — не украшательство:

    1. резолв способности → её навык-носитель (у CLI нет второго способа
       узнать, в каком дистрибутиве живёт ``grok_transcriber``);
    2. обычная установка навыка (та же цепочка, что у ``skillery install`` —
       второй установки не заводим);
    3. запись в реестр требований ЗАЧЕМ он поставлен + немедленная выдача
       лиза.

    Без шага 3 навык лежал бы на диске «просто так»: снятие не знало бы, что
    его держит эта способность, а гейт запуска до ближайшего такта демона (до
    трёх минут) отвечал бы «нет доступа» на только что установленное.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        from skillery_cli.__main__ import _install_chain
        from skillery_cli.core.agents import get_target
        from skillery_cli.core.identity import device_uid
        from skillery_cli.core.leases import (
            CapabilityRequirement,
            RequirementsIndex,
            lease_store,
        )

        client = _common.make_client(cfg, access)
        try:
            item = await client.get_capability(capability)
            name = str(item.get("name") or capability)
            cap_id = str(item.get("id") or "")
            skill_id = str(item.get("skill_id") or "")
            if not skill_id:
                raise RuntimeError(
                    f"Способность «{name}» не называет навык-носитель — "
                    "устанавливать нечего. Сообщите владельцу навыка."
                )
            if not item.get("granted", True):
                raise RuntimeError(
                    f"Способность «{name}» вам не выдана. Попросите доступ у "
                    "владельца навыка — он выдаёт его командой "
                    f"`skillery capability grant {cap_id} --to user --target-id <ваш id>`"
                )
            skill = await client.get_skill(skill_id)
            slug = str(skill.get("slug") or skill_id)

            await _install_chain(
                cfg, access, slug=slug, channel=channel, scope="global",
                project_path=None, force=False,
                agent_target=get_target(agent or cfg.agent),
                headless=False, initiator="cli",
            )

            # Реестр: ЗАЧЕМ навык лежит на диске. Пишем ДО выдачи лиза —
            # установка уже состоялась, и потерять причину из-за сетевой
            # ошибки выдачи нельзя: без строки снятие сочло бы навык ничьим.
            index = RequirementsIndex.load()
            index.merge([
                CapabilityRequirement(
                    name=name,
                    requires_lease=bool(item.get("requires_lease")),
                    capability_id=cap_id,
                    skill_id=skill_id,
                    skill=slug,
                    granted=True,
                )
            ])
            index.save()

            leased = False
            if item.get("requires_lease") and cap_id:
                # Контракт §3: после явного install лиз выдаём СРАЗУ, не ожидая
                # такта демона — иначе только что поставленная способность до
                # трёх минут отвечала бы «нет доступа».
                try:
                    issued = await client.issue_capability_lease(
                        cap_id, device_id=device_uid()
                    )
                    token = str(issued.get("token") or "")
                    if token:
                        lease_store().put(name, token)
                        leased = True
                except ApiError as exc:
                    emit_message(
                        f"Лиз пока не выдан ({exc.code}): демон обновит доступ "
                        "на ближайшем такте.",
                        level="warn",
                    )
        finally:
            await client.close()

        emit_data(
            {
                "capability": name,
                "capability_id": cap_id,
                "skill": slug,
                "installed": True,
                "leased": leased,
            },
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Способность «{p['capability']}» готова: "
                f"навык {p['skill']} установлен"
                + (", лиз выдан" if p["leased"] else "")
            ),
        )

    _common.run(_do())


def cmd_capability_remove(
    capability: str = typer.Argument(
        ..., metavar="ИМЯ", help="Имя способности (grok_transcriber)"
    ),
    agent: str = typer.Option(None, "--agent"),
) -> None:
    """Убрать способность с машины — но НЕ трогать навык, нужный другой.

    Ровно §6.3 контракта: способность перестаёт числиться востребованной и её
    лиз удаляется всегда, а файлы навыка-носителя снимаются ТОЛЬКО когда его не
    держит ни одна другая способность с действующим правом. Иначе отказ от
    транскрибации ломал бы чат из того же дистрибутива.

    Сети не требует: это решение про локальный диск, и принимать его в офлайне
    надо так же уверенно, как онлайн.
    """
    from skillery_cli.__main__ import _revert_tooling
    from skillery_cli.core.agents import get_target
    from skillery_cli.core.installer import SkillInstaller
    from skillery_cli.core.leases import RequirementsIndex, lease_store

    cfg = ClientConfig.load()
    index = RequirementsIndex.load()
    row = index.items.get(capability)
    if row is None:
        emit_error(
            "NOT_FOUND",
            f"Способность «{capability}» на этом устройстве не числится. "
            "Что установлено — `skillery capability list`",
        )
        raise typer.Exit(1)

    slug = row.skill or row.skill_id
    blockers = index.held_by(slug, row.skill_id, exclude=(capability,))

    # Лиз удаляем ВСЕГДА: право исполнять эту способность мы больше не
    # заявляем, независимо от судьбы файлов.
    lease_store().drop(capability)
    index.items.pop(capability, None)
    index.save()

    removed = False
    if not blockers and slug:
        target = get_target(agent or cfg.agent)
        _revert_tooling(
            slug, agent_target=target, project=None,
            store_dir=cfg.effective_store_dir() / slug,
        )
        removed = bool(
            SkillInstaller(target, cfg.effective_store_dir())
            .remove(slug=slug, project=None, keep_local=False, purge=True)
            .removed
        )

    payload = {
        "capability": capability,
        "skill": slug,
        "skill_removed": removed,
        "held_by": list(blockers),
    }

    def _render(p: dict[str, Any]) -> None:
        if p["skill_removed"]:
            console.print(
                f"[green]✓[/] Способность «{p['capability']}» снята "
                f"вместе с навыком {p['skill']}"
            )
            return
        if p["held_by"]:
            console.print(
                f"[green]✓[/] Способность «{p['capability']}» снята; навык "
                f"{p['skill']} оставлен — его держат: {', '.join(p['held_by'])}"
            )
            return
        console.print(
            f"[green]✓[/] Способность «{p['capability']}» снята "
            f"(навык {p['skill'] or '—'} на диске не найден)"
        )

    emit_data(payload, text_renderer=_render)


# ---------------------------------------------------------------------------
#  Право другого субъекта (это про хаб, не про диск)
# ---------------------------------------------------------------------------
def cmd_capability_grant(
    capability: str = typer.Argument(..., metavar="ID", help="ID способности"),
    target_type: str = typer.Option(
        ..., "--to", help=f"Кому: {' | '.join(TARGET_TYPES)}"
    ),
    target_id: str = typer.Option(..., "--target-id", help="ID субъекта"),
    role: str = typer.Option("viewer", "--role", help=f"Уровень: {' | '.join(ROLES)}"),
    until: str = typer.Option(
        None,
        "--until",
        help="Срок действия ГРАНТА (ISO-8601, напр. 2026-12-31T00:00:00Z). "
             "По умолчанию бессрочно.",
    ),
) -> None:
    """Выдать доступ к способности (PUT /capabilities/{id}/access-grants).

    ``--until`` — срок ГРАНТА, а не лиза. Лиз всегда живёт сутки и лишь следует
    за грантом: путать их значило бы обещать пользователю мгновенный отзыв,
    которого офлайн-машина дать не может (§6.2).

    Мутация ⇒ только числовой id (имя хаб здесь не принимает): цена ошибки —
    «выдали право не на ту способность». Узнать id: `skillery capability get
    <имя>`.
    """
    if target_type not in TARGET_TYPES:
        emit_error("VALIDATION", f"--to должен быть одним из: {', '.join(TARGET_TYPES)}")
        raise typer.Exit(2)
    if role not in ROLES:
        emit_error("VALIDATION", f"--role должен быть одним из: {', '.join(ROLES)}")
        raise typer.Exit(2)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            grant = await client.grant_capability_access(
                capability,
                target_type=target_type,
                target_id=target_id,
                role=role,
                expires_at=until,
            )
        finally:
            await client.close()
        emit_data(
            grant,
            text_renderer=lambda g: console.print(
                f"[green]✓[/] Доступ к способности {capability} выдан: "
                f"{g.get('target_type')} {g.get('target_id')} → {g.get('role')}"
                + (f", до {g.get('expires_at')}" if g.get("expires_at") else "")
                + f" (grant_id={g.get('id')})"
            ),
        )

    _common.run(_do())


def cmd_capability_revoke(
    capability: str = typer.Argument(..., metavar="ID", help="ID способности"),
    grant_id: str = typer.Option(None, "--grant-id", help="ID гранта"),
    target_type: str = typer.Option(None, "--to", help=f"Кому: {' | '.join(TARGET_TYPES)}"),
    target_id: str = typer.Option(None, "--target-id", help="ID субъекта"),
) -> None:
    """Отозвать доступ к способности (DELETE …/access-grants/{grant_id}).

    Грант адресуется либо своим ``--grant-id``, либо парой ``--to``/
    ``--target-id`` — тогда id находится по списку грантов. Вторая форма нужна
    потому, что человек помнит, КОМУ он выдавал доступ, а не суррогатный номер
    строки; заставлять его сперва искать номер значило бы гарантировать отзыв
    не того гранта.

    Отзыв — про ПРАВО, а не про файлы: навык-носитель у субъекта остаётся, и
    снимет его отдельное задание очереди (и то лишь если носителя не держит
    другая способность, §6.3).
    """
    if not grant_id and not (target_type and target_id):
        emit_error(
            "VALIDATION",
            "Укажите --grant-id либо пару --to/--target-id (кому был выдан доступ)",
        )
        raise typer.Exit(2)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        resolved = grant_id
        try:
            if not resolved:
                grants = (await client.list_capability_access_grants(capability)).get(
                    "grants"
                ) or []
                matches = [
                    g for g in grants
                    if str(g.get("target_type")) == target_type
                    and str(g.get("target_id")) == str(target_id)
                ]
                if not matches:
                    raise RuntimeError(
                        f"Гранта на способность {capability} для "
                        f"{target_type} {target_id} нет — отзывать нечего"
                    )
                if len(matches) > 1:
                    ids = ", ".join(str(g.get("id")) for g in matches)
                    raise RuntimeError(
                        f"Таких грантов несколько ({ids}) — укажите --grant-id"
                    )
                resolved = str(matches[0].get("id"))
            await client.revoke_capability_access(capability, str(resolved))
        finally:
            await client.close()
        emit_data(
            {"revoked": True, "capability_id": capability, "grant_id": resolved},
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Грант {p['grant_id']} на способность "
                f"{p['capability_id']} отозван"
            ),
        )

    _common.run(_do())


def register(app: typer.Typer, *, can_manage: bool = False) -> None:
    """Регистрирует группу ``capability``.

    Чтение и работа со своей машиной (``list``/``get``/``install``/``remove``)
    — всем: это ответ на вопрос «что мне разрешено», и прятать его не от кого.
    ``grant``/``revoke`` — под тем же гейтом, что гранты навыка-носителя
    (``skill.manage``/``hub.admin``): у способности своего владельца нет, она
    живёт ровно столько, сколько живёт навык.
    """
    capability_app = typer.Typer(
        no_args_is_help=True,
        help=(
            "Способности: что мне разрешено, поставить/снять носителя, "
            "выдать/отозвать доступ."
        ),
    )
    capability_app.command("list")(cmd_capability_list)
    capability_app.command("get")(cmd_capability_get)
    capability_app.command("install")(cmd_capability_install)
    capability_app.command("remove")(cmd_capability_remove)
    if can_manage:
        capability_app.command("grant")(cmd_capability_grant)
        capability_app.command("revoke")(cmd_capability_revoke)
    app.add_typer(capability_app, name="capability")
