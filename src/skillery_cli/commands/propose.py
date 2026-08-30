"""``skillery propose …`` — предложить владельцу навыка свои правки (#2455).

Линия «правка чужого навыка без форка». До неё улучшение чужого навыка не имело
ни одного маршрута: человек правил установленную копию у себя, копия уезжала
при первом ``skillery update``, а владелец о правке не узнавал никогда.

────────────────────────────────────────────────────────────────────────────
ПОЧЕМУ БАНДЛ, А НЕ ССЫЛКА НА ФОРК

Предложение — это ``tar.gz`` папки навыка плюс ПИН БАЗЫ (версия + слепок
содержимого). Форк на git-хостинге потребовал бы от предлагающего аккаунт и
публичный репозиторий, а от хаба — ходить по произвольному внешнему URL под
своими сетевыми правами. Бандл же даёт принимающей стороне ровно три вещи,
ради которых всё и делается: показать diff, проверить, что база не уехала, и
применить изменение как целое.

────────────────────────────────────────────────────────────────────────────
ЧТО КОМАНДА ДЕЛАЕТ ЗА ОДИН ВЫЗОВ

1. Находит установленную копию и БАЗУ — версию, которой навык ставили. База
   берётся из локального состояния, а не спрашивается у человека: он ответит
   неточно, и не со зла — он просто не обязан помнить semver.
2. Скачивает слепок базы с хаба и сравнивает с копией ПО СОДЕРЖИМОМУ. Список
   изменённых файлов показывается ДО отправки: человек должен видеть, что
   именно уезжает владельцу.
3. Гоняет ``s-skillgate`` локально. Найденный секрет — стоп на этой машине: до
   хаба он не доезжает вообще. Это и дешевле, и честнее, чем ``blocked`` на той
   стороне, где файл уже лежит в чужом хранилище.
4. ``POST`` метаданных → ``PUT`` бандла → ``POST`` подачи.

Порядок шагов не переставляется: сеть — последней, потому что всё, что можно
отклонить локально, обязано быть отклонено до неё.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.core.proposal_bundle import (
    MAX_BUNDLE_BYTES,
    BundleChanges,
    ProposalBundleError,
    check_limits,
    collect_files,
    diff_files,
    make_bundle,
    tree_digest,
    unpack_snapshot,
)
from skillery_cli.output import emit_data, emit_error, emit_message

console = Console()

#: Статусы предложения (зеркало ``ProposalStatus`` бэкенда) — для ``--status``.
PROPOSAL_STATUSES = (
    "draft",
    "scanning",
    "submitted",
    "blocked",
    "changes_requested",
    "accepted",
    "declined",
    "withdrawn",
    "stale",
)

#: Человеческие подписи статусов. Голый ``changes_requested`` в терминале — это
#: вопрос в поддержку, а не ответ автору.
STATUS_LABELS: dict[str, str] = {
    "draft": "черновик",
    "scanning": "проверяется",
    "submitted": "на рассмотрении",
    "blocked": "заблокировано сканером",
    "changes_requested": "нужны правки",
    "accepted": "принято",
    "declined": "отклонено",
    "withdrawn": "отозвано",
    "stale": "база устарела",
}

#: Подсказка для устаревшей базы. Её место здесь, а не в тексте таблицы:
#: ``stale`` — самый частый исход у долгого предложения, и человек обязан
#: сразу знать, что делать, а не искать в документации.
STALE_HINT = (
    "База устарела: у навыка вышла версия новее. Пересоберите предложение — "
    "`skillery skill update <навык> && skillery propose <навык>`"
)

#: Шаблон для ``$EDITOR``, когда заголовок не передан флагом.
EDITOR_TEMPLATE = """\
# Первая строка — заголовок предложения, дальше — «зачем».
# Строки, начинающиеся с #, отбрасываются. Пустой заголовок = отмена.

"""


def _store_dir_for(cfg: ClientConfig, ref: str) -> tuple[Path, dict[str, Any]]:
    """Папка установленного навыка + его локальная мета.

    Ищем и по имени папки, и по ``slug``/``skill_id`` из меты: у slug-less
    навыка папка называется числовым id, и требовать от человека знать, какой
    именно из двух идентификаторов сегодня в ходу, — плохой обмен.
    """
    from skillery_cli.core.installer import read_meta
    from skillery_cli.core.store_backup import iter_store_skill_dirs

    root = cfg.effective_store_dir()
    needle = str(ref).strip()
    for directory in iter_store_skill_dirs(root):
        meta = read_meta(directory) or {}
        names = {
            directory.name,
            str(meta.get("slug") or ""),
            str(meta.get("skill_id") or ""),
        }
        if needle in names:
            return directory, meta
    raise LookupError(needle)


def _render_changes(changes: BundleChanges) -> Table:
    table = Table(title=f"Изменения (всего файлов: {len(changes.paths)})")
    table.add_column("что")
    table.add_column("файл")
    for path in changes.added:
        table.add_row("добавлен", path)
    for path in changes.modified:
        table.add_row("изменён", path)
    for path in changes.removed:
        table.add_row("удалён", path)
    return table


def _title_and_body(
    message: str | None, body_file: str | None
) -> tuple[str, str]:
    """Заголовок и «зачем». Без ``-m`` — редактор, как у ``git commit``.

    Пустой заголовок — отказ, а не «Update skill»: предложение без внятного
    «зачем» владелец не примет, а вежливая заглушка лишь заставит его
    выяснять это перепиской.
    """
    body = ""
    if body_file:
        body = Path(body_file).read_text(encoding="utf-8").strip()
    title = (message or "").strip()
    if not title:
        if not sys.stdin.isatty():
            raise ValueError(
                "Нужен заголовок: -m \"что предлагаете\" "
                "(редактор в неинтерактивном режиме открыть нельзя)"
            )
        edited = typer.edit(EDITOR_TEMPLATE) or ""
        lines = [
            line for line in edited.splitlines() if not line.startswith("#")
        ]
        title = (lines[0].strip() if lines else "")
        if not body:
            body = "\n".join(lines[1:]).strip()
    if not title:
        raise ValueError("Пустой заголовок — предложение не отправлено")
    # Тело обязательно на той стороне; когда автор ничего не дописал, честнее
    # повторить заголовок, чем выдумывать за него текст.
    return title, body or title


def cmd_propose(
    skill: str = typer.Argument(..., metavar="НАВЫК", help="Slug или id навыка"),
    message: str = typer.Option(
        None, "-m", "--message", help="Заголовок предложения"
    ),
    body_file: str = typer.Option(
        None, "--body-file", help="Файл с описанием «зачем»"
    ),
    path: str = typer.Option(
        None,
        "--path",
        help="Папка с правками (по умолчанию — установленная копия из стора)",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Не спрашивать подтверждения"
    ),
) -> None:
    """Отправить владельцу навыка свои правки установленной копии.

    Сначала локальные проверки (база, изменения, лимиты, сканер секретов) и
    только потом сеть: всё, что можно отклонить на этой машине, отклоняется до
    первого байта наружу.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    try:
        store_dir, meta = _store_dir_for(cfg, skill)
    except LookupError:
        emit_error(
            "NOT_FOUND",
            f"Навык «{skill}» на этой машине не установлен. Предлагать правки "
            "можно только к установленной копии: `skillery skill install "
            f"{skill}`",
        )
        raise typer.Exit(1) from None

    skill_dir = Path(path) if path else store_dir
    base_version = str(meta.get("version") or "").strip()
    if not base_version:
        emit_error(
            "VALIDATION",
            "В локальном состоянии нет версии установки — предложение не к "
            "чему пинить. Переустановите навык: `skillery skill update "
            f"{skill}`",
        )
        raise typer.Exit(1)

    # Папку читаем ДО заголовка: просить человека написать «зачем», а потом
    # сказать «эту папку всё равно нельзя отправить» — худший из возможных
    # порядков, и обиднее всего он выглядит, когда заголовок писали в редакторе.
    try:
        current = collect_files(skill_dir)
        check_limits(current)
    except ProposalBundleError as exc:
        emit_error("VALIDATION", str(exc))
        raise typer.Exit(2) from exc

    try:
        title, body = _title_and_body(message, body_file)
    except (ValueError, OSError) as exc:
        emit_error("VALIDATION", str(exc))
        raise typer.Exit(2) from exc

    skill_ref = str(meta.get("skill_id") or meta.get("slug") or skill)

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            base_archive = await client.download_snapshot(skill_ref, base_version)
            if base_archive is None:
                raise RuntimeError(
                    f"У версии {base_version} нет слепка на хабе — сравнить "
                    "правки не с чем, а предложение без пина базы владелец "
                    "применить не сможет. Обновите навык до версии со "
                    "слепком: `skillery skill update " + skill + "`"
                )
            base = unpack_snapshot(base_archive)
            changes = diff_files(base, current)
            if changes.empty:
                emit_message(
                    "Правок нет — установленная копия совпадает с версией "
                    f"{base_version}. Предлагать нечего.",
                    level="warning",
                )
                return

            bundle = make_bundle(current)
            if len(bundle) > MAX_BUNDLE_BYTES:
                raise RuntimeError(
                    f"Архив весит {len(bundle)} байт, потолок "
                    f"{MAX_BUNDLE_BYTES}. Уберите из папки лишнее."
                )

            console.print(_render_changes(changes))

            # Сканер — ПОСЛЕ показа списка и ДО сети: автор видит, что уезжает,
            # и только потом файлы вообще получают шанс уехать.
            from skillery_cli.__main__ import (
                _run_publish_denylist_gate,
                _run_publish_secret_scan,
            )

            _run_publish_secret_scan(skill_dir, force=False, strict=False)
            _run_publish_denylist_gate(skill_dir, force=False)

            if not yes and sys.stdin.isatty():
                if not typer.confirm(
                    f"Отправить {len(changes.paths)} файл(ов) владельцу навыка?"
                ):
                    emit_message("Отменено — ничего не отправлено", level="info")
                    return

            device_id = await _hub_device_id(client)
            from skillery_cli import __version__

            created = await client.create_proposal(
                skill_ref,
                {
                    "title": title,
                    "body": body,
                    "base_version": base_version,
                    # Пин базы — слепок СОДЕРЖИМОГО версии, а не байт архива:
                    # пересжатие той же версии не должно менять пин.
                    "base_digest": tree_digest(base),
                    "cli_version": __version__,
                    "base_channel": str(meta.get("channel") or ""),
                    "device_id": device_id,
                },
            )
            proposal_id = str(created.get("id"))
            await client.upload_proposal_bundle(skill_ref, proposal_id, bundle)
            submitted = await client.submit_proposal(skill_ref, proposal_id)
        finally:
            await client.close()

        payload = {
            "id": proposal_id,
            "skill": skill_ref,
            "status": submitted.get("status"),
            "title": title,
            "base_version": base_version,
            "changed": list(changes.paths),
        }

        def _render(p: dict[str, Any]) -> None:
            console.print(
                f"[green]✓[/] Предложение #{p['id']} отправлено "
                f"(статус: {STATUS_LABELS.get(str(p['status']), p['status'])})"
            )
            console.print(
                f"  Смотреть: `skillery proposal show {p['skill']} {p['id']}`"
            )

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


async def _hub_device_id(client: Any) -> int | None:
    """Числовой id ЭТОГО устройства на хабе — best-effort.

    Провенанс предложения («с какой машины прислано») полезен владельцу, но не
    обязателен, и ради него нельзя ронять отправку: не нашли — шлём ``None``.
    """
    try:
        from skillery_cli.core.identity import device_uid

        uid = device_uid()
        for device in await client.list_devices():
            if str(device.get("client_device_id")) == uid:
                return int(device.get("id"))
    except Exception:  # noqa: BLE001 — провенанс не стоит отправки
        return None
    return None


def cmd_proposal_list(
    skill: str = typer.Argument(..., metavar="НАВЫК", help="Slug или id навыка"),
    status: str = typer.Option(
        None, "--status", help=f"Фильтр: {' | '.join(PROPOSAL_STATUSES)}"
    ),
) -> None:
    """Очередь предложений навыка (GET /skills/{id}/proposals).

    Автор видит свои, владелец навыка и модератор — все: разделение делает
    бэкенд, здесь его не дублируем.
    """
    if status and status not in PROPOSAL_STATUSES:
        emit_error(
            "VALIDATION",
            f"--status должен быть одним из: {', '.join(PROPOSAL_STATUSES)}",
        )
        raise typer.Exit(2)
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            data = await client.list_proposals(skill, status=status)
        finally:
            await client.close()
        items = list(data.get("items") or [])
        payload = {"items": items, "total": int(data.get("total") or len(items))}

        def _render(p: dict[str, Any]) -> None:
            if not p["items"]:
                console.print("[yellow]Предложений нет[/]")
                return
            table = Table(title=f"Предложения (всего: {p['total']})")
            table.add_column("id")
            table.add_column("статус")
            table.add_column("заголовок")
            table.add_column("файлов")
            for item in p["items"]:
                state = str(item.get("status") or "")
                table.add_row(
                    str(item.get("id") or "—"),
                    STATUS_LABELS.get(state, state),
                    str(item.get("title") or "—"),
                    str(len((item.get("provenance") or {}).get("changed_paths") or [])),
                )
            console.print(table)
            if any(str(i.get("status")) == "stale" for i in p["items"]):
                console.print(f"[yellow]{STALE_HINT}[/]")

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


def cmd_proposal_show(
    skill: str = typer.Argument(..., metavar="НАВЫК", help="Slug или id навыка"),
    proposal_id: str = typer.Argument(..., metavar="ID", help="ID предложения"),
    diff: bool = typer.Option(
        False, "--diff", help="Показать разницу, посчитанную ХАБОМ"
    ),
) -> None:
    """Карточка предложения; с ``--diff`` — что именно изменится.

    Разницу считает хаб из базы и бандла: присланному клиентом diff'у верить
    нельзя, показывать надо ровно то, что будет применено.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            proposal = await client.get_proposal(skill, proposal_id)
            changes = (
                await client.get_proposal_diff(skill, proposal_id)
                if diff
                else None
            )
        finally:
            await client.close()
        payload = dict(proposal)
        if changes is not None:
            payload["diff"] = changes

        def _render(p: dict[str, Any]) -> None:
            state = str(p.get("status") or "")
            console.print(f"[bold]{p.get('title') or '—'}[/] (#{p.get('id')})")
            console.print(f"  статус:       {STATUS_LABELS.get(state, state)}")
            console.print(f"  база:         версия id {p.get('base_version_id')}")
            console.print(f"  бандл:        {p.get('bundle_size')} байт")
            if p.get("decision_note"):
                console.print(f"  решение:      {p['decision_note']}")
            if state == "blocked":
                # Содержимого находки нет и не будет: находка сама может быть
                # секретом. Автору достаточно знать, что скан её нашёл.
                console.print(
                    f"  [red]сканер нашёл находок: {p.get('scan_findings')}[/]"
                )
            if state == "stale":
                console.print(f"  [yellow]{STALE_HINT}[/]")
            if p.get("body"):
                console.print(f"\n{p['body']}")
            # Хаб отдаёт ГОТОВЫЙ unified diff строкой — печатаем как есть, без
            # своего разбора: переразметив его, мы показали бы не то, что
            # будет применено, а свою интерпретацию.
            text = (p.get("diff") or {}).get("diff")
            if text:
                console.print("\n[bold]Изменения[/]")
                console.print(text)

        emit_data(payload, text_renderer=_render)

    _common.run(_do())


def cmd_proposal_withdraw(
    skill: str = typer.Argument(..., metavar="НАВЫК", help="Slug или id навыка"),
    proposal_id: str = typer.Argument(..., metavar="ID", help="ID предложения"),
) -> None:
    """Забрать своё предложение (DELETE /skills/{id}/proposals/{pid}).

    Отзыв мягкий: строка остаётся ради истории — владелец должен видеть, что
    предложение было и что автор его снял, а не гадать, куда оно делось.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            await client.withdraw_proposal(skill, proposal_id)
        finally:
            await client.close()
        emit_data(
            {"withdrawn": True, "skill": skill, "id": proposal_id},
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Предложение #{p['id']} отозвано"
            ),
        )

    _common.run(_do())


def register(app: typer.Typer) -> None:
    """Группа ``proposal`` + плоское ``propose`` для самой частой операции.

    Группа — канон (``skillery <ресурс> <глагол>``), а плоское ``propose``
    остаётся видимым (не deprecated) по той же причине, что ``run`` и ``ask``:
    это ЕДИНИЧНОЕ действие, которое человек делает по имени навыка, и
    заставлять его писать ``proposal create`` ради симметрии — плохой обмен.

    Гейта прав нет: право ``skill.propose`` проверяет хаб. Прятать команду
    локально значило бы объяснять отсутствие права молчанием.
    """
    proposal_app = typer.Typer(
        no_args_is_help=True,
        help=(
            "Предложения-с-кодом: посмотреть очередь, карточку, отозвать. "
            "Отправить правки — `skillery propose <навык>`."
        ),
    )
    # Отправка есть и в группе — ради симметрии и скриптов, адресующих ресурс,
    # — но СПРЯТАНА из справки: одно действие с двумя видимыми именами лишает
    # человека канона (инвариант #2267). Видимое имя одно, и оно из спеки.
    proposal_app.command("create", hidden=True)(cmd_propose)
    proposal_app.command("list")(cmd_proposal_list)
    proposal_app.command("show")(cmd_proposal_show)
    proposal_app.command("withdraw")(cmd_proposal_withdraw)
    app.add_typer(proposal_app, name="proposal")
    app.command("propose")(cmd_propose)
