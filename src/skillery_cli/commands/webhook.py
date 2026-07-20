"""``skillery webhook`` — автообновления навыка из его git-репозитория.

Ресурс ``webhook`` навыка, глаголы как у соседей (``role``/``collection``):

- ``webhook register <ID_ИЛИ_SLUG>`` — включить автосинк: зарегистрировать
  webhook на хабе (``POST /skills/{slug}/webhook``) И, если хаб не смог сделать
  hook сам, создать его У ПРОВАЙДЕРА локальным ``gh``/``glab``;
- ``webhook status <ID_ИЛИ_SLUG>`` — состояние (``GET .../webhook``),
  с ``--probe`` ещё и глазами провайдера (есть ли hook, когда была доставка);
- ``webhook revoke <ID_ИЛИ_SLUG>`` — отозвать на хабе и снять hook у провайдера.

Почему две стороны, а не одна. Хаб создаёт hook сам только если у НЕГО есть
токен с правами на hooks репо; иначе он возвращает ``mode="manual"`` + url +
секрет, и дальше — ручная работа человека. Именно её закрывает
:mod:`skillery_cli.core.webhook_setup`: локальные ``gh``/``glab`` уже
авторизованы правами владельца репо (тот же приём, что в ``repo_connect``).

Дисциплина секретов: секрет webhook'а НИКОГДА не попадает в ``emit_data``
(машинный канал для агентов), в лог и в файлы. В payload идёт только факт
выдачи и отпечаток ``sha256:<12 hex>``. Сам секрет печатается ОДИН раз и
только в text-режиме — человеку, которому его руками вставлять в настройки
репо, когда авто-настройка не удалась.
"""
from __future__ import annotations

import subprocess
from typing import Any

import typer
from rich.console import Console

from skillery_cli.commands import _common
from skillery_cli.config import ClientConfig
from skillery_cli.core import repo_connect, webhook_setup
from skillery_cli.core.transport import HubClient
from skillery_cli.output import emit_data, is_json

console = Console()

# Раннер провайдерских CLI (gh/glab). Модульный атрибут — точка инъекции для
# тестов: ``monkeypatch.setattr(webhook_mod, "RUNNER", fake)``; сети и
# подпроцессов в тестах нет.
RUNNER: Any = subprocess.run


async def _load_skill(client: HubClient, id_or_slug: str) -> tuple[str, str | None]:
    """``(skill_id, repo_url)`` навыка.

    Берём именно карточку (а не ``resolve_skill_id``), потому что для настройки
    hook'а у провайдера нужен ``repo_url`` — из одного id его не вывести.
    """
    data: dict[str, Any] = await client.get_skill(id_or_slug) or {}
    return str(data.get("id") or id_or_slug), data.get("repo_url")


def _provider_and_repo(
    repo_url: str | None, hub_provider: str | None
) -> tuple[str | None, repo_connect.RepoSlug | None]:
    """Провайдер + разобранный slug репо. Провайдер хаба — приоритетнее.

    Хаб уже решил, кем считает репо (``github``/``gitlab``), — уважаем его
    ответ; из URL достаём только owner/name/host.
    """
    if not repo_url:
        return hub_provider, None
    slug = repo_connect.parse_repo_slug(repo_url)
    provider = hub_provider or repo_connect.infer_provider(repo_url)
    return provider, slug


def cmd_webhook_register(
    slug: str = typer.Argument(
        ..., metavar="ID_ИЛИ_SLUG", help="id-или-slug навыка (backend принимает оба)"
    ),
    no_provider: bool = typer.Option(
        False,
        "--no-provider",
        help="Только регистрация на хабе: hook у провайдера не трогать "
        "(создадите сами по выданным шагам).",
    ),
) -> None:
    """Включить автообновления навыка (webhook на хабе + hook у провайдера).

    Повторный запуск безопасен: хаб снимает свою прежнюю запись, а hook у
    провайдера обновляется по месту (тот же id, новый секрет) — дублей нет.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            skill_id, repo_url = await _load_skill(client, slug)
            reg: dict[str, Any] = (
                await client.register_skill_webhook(skill_id) or {}
            )
        finally:
            await client.close()

        # Секрет вынимаем СРАЗУ и держим только в локальной переменной — в
        # payload (машинный вывод) он не попадёт ни при каких ветках.
        secret = reg.get("secret")
        mode = str(reg.get("mode") or "manual")
        provider = reg.get("provider") or None
        url = str(reg.get("url") or "")

        payload: dict[str, Any] = {
            "event": "webhook_register",
            "skill": slug,
            "skill_id": skill_id,
            "mode": mode,
            "provider": provider,
            "url": url,
            "repo_url": repo_url,
            "hub_secret_issued": bool(secret),
            "secret_fingerprint": webhook_setup.secret_fingerprint(secret),
            "secret_shown": False,
        }

        if mode == "auto":
            # Хаб сделал hook своим токеном — локально делать нечего.
            payload["provider_hook"] = {
                "action": "hub",
                "detail": "hook создан хабом (его токеном на репо)",
            }
            payload["auto_update"] = True
            emit_data(payload, text_renderer=_render_register)
            return

        provider_name, repo = _provider_and_repo(repo_url, provider)
        if no_provider:
            outcome = webhook_setup.HookOutcome(
                "skipped",
                reason="disabled_by_flag",
                detail="запрошен --no-provider: hook у провайдера не трогали",
            )
        elif repo is None or provider_name not in ("github", "gitlab"):
            outcome = webhook_setup.HookOutcome(
                "skipped",
                reason="provider_unsupported",
                detail=webhook_setup.tool_hint(
                    provider_name or "", "provider_unsupported"
                ),
            )
        elif not secret or not url:
            # Без секрета/URL создавать hook нечем: обычно значит, что у хаба не
            # задан публичный base URL (создавать hook некуда).
            outcome = webhook_setup.HookOutcome(
                "skipped",
                reason="callback_url_missing",
                detail=webhook_setup.tool_hint(
                    provider_name, "callback_url_missing"
                ),
            )
        else:
            outcome = webhook_setup.ensure_provider_hook(
                provider=provider_name,
                repo=repo,
                callback_url=url,
                skill_id=skill_id,
                secret=secret,
                runner=RUNNER,
            )

        payload["provider_hook"] = {
            "action": outcome.action,
            "hook_id": outcome.hook_id,
            "url": outcome.url,
            "reason": outcome.reason,
            "detail": outcome.detail,
            # Хаб на КАЖДОМ register выдаёт новый секрет и стирает прежнюю
            # запись. Если у провайдера hook есть, а обновить его не вышло —
            # там остался СТАРЫЙ секрет: доставки будут молча отбиваться 401,
            # пока секрет не поправят. Это надо сказать вслух.
            "stale_secret": bool(outcome.hook_id) and not outcome.ok,
        }
        payload["auto_update"] = outcome.ok
        if not outcome.ok:
            payload["manual_steps"] = webhook_setup.manual_instructions(
                provider_name or "github", url, skill_id
            )
            # Секрет нужен человеку для ручной вставки — покажем его ОДИН раз
            # и только в text-режиме (в json он не уходит принципиально).
            payload["secret_shown"] = bool(secret) and not is_json()

        emit_data(
            payload,
            text_renderer=lambda p: _render_register(p, secret=secret),
        )

    _common.run(_do())


def _render_register(payload: dict[str, Any], *, secret: str | None = None) -> None:
    """text-рендер регистрации. Секрет печатается здесь и только здесь."""
    hook = payload.get("provider_hook") or {}
    action = hook.get("action")
    provider = payload.get("provider") or "?"
    if payload.get("auto_update"):
        tool = webhook_setup.provider_tool(provider) or "CLI"
        where = {
            "hub": "хабом",
            "created": f"локальным {tool}",
            "updated": f"локальным {tool} (обновлён существующий)",
        }.get(str(action), "")
        console.print(
            f"[green]✓[/] Автообновления включены для [bold]{payload['skill']}[/] "
            f"({provider}): hook настроен {where}."
        )
        # Показываем адрес самого hook'а (приёмник + маркер навыка), если он
        # есть: именно его человек увидит в настройках репо.
        shown_url = hook.get("url") or payload.get("url")
        if shown_url:
            console.print(f"  Приёмник: [dim]{shown_url}[/]")
        if hook.get("hook_id"):
            console.print(f"  hook id у провайдера: [dim]{hook['hook_id']}[/]")
        return

    console.print(
        f"[yellow]![/] Webhook зарегистрирован на хабе, но hook у провайдера "
        f"НЕ создан автоматически: {hook.get('detail') or hook.get('reason')}"
    )
    if hook.get("stale_secret"):
        console.print(
            f"[red]Внимание:[/] у провайдера остался hook "
            f"[bold]{hook.get('hook_id')}[/] со СТАРЫМ секретом — хаб только что "
            "выдал новый. Пока секрет там не обновите, доставки будут "
            "отбиваться 401, и автообновления не работают."
        )
    console.print("[bold]Сделайте вручную:[/]")
    for i, step in enumerate(payload.get("manual_steps") or [], 1):
        console.print(f"  {i}. {step}")
    if secret:
        console.print(
            "\n[bold red]СЕКРЕТ WEBHOOK'А — показывается ОДИН раз, "
            "не сохраняйте в файлы/чаты:[/]"
        )
        # markup/highlight/перенос ВЫКЛЮЧЕНЫ: rich съел бы `[...]` внутри
        # секрета как разметку, а перенос строки — сломал бы копипасту; человек
        # вставил бы искажённый секрет и получил вечный 401 без объяснений.
        console.print(f"  {secret}", markup=False, highlight=False, soft_wrap=True)
        console.print(
            f"  [dim]отпечаток: {payload.get('secret_fingerprint')} — по нему "
            "потом сверяют, тот ли секрет стоит в репо[/]"
        )


def cmd_webhook_status(
    slug: str = typer.Argument(
        ..., metavar="ID_ИЛИ_SLUG", help="id-или-slug навыка (backend принимает оба)"
    ),
    probe: bool = typer.Option(
        False,
        "--probe",
        help="Дополнительно спросить провайдера (локальный gh/glab): есть ли "
        "hook и когда была последняя доставка.",
    ),
) -> None:
    """Состояние webhook'а навыка: настроен ли, каким провайдером, куда шлёт.

    Время последней доставки хаб не хранит — его знает только провайдер,
    поэтому оно появляется лишь с ``--probe`` (и только у GitHub: GitLab CE
    историю доставок не отдаёт).
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            skill_id, repo_url = await _load_skill(client, slug)
            st: dict[str, Any] = await client.get_skill_webhook(skill_id) or {}
        finally:
            await client.close()

        url = str(st.get("url") or "")
        provider = st.get("provider") or None
        payload: dict[str, Any] = {
            "event": "webhook_status",
            "skill": slug,
            "skill_id": skill_id,
            "status": st.get("status"),
            "provider": provider,
            "url": url or None,
            "repo_url": repo_url,
        }

        if probe:
            provider_name, repo = _provider_and_repo(repo_url, provider)
            if repo is None or provider_name not in ("github", "gitlab"):
                payload["provider_hook"] = {
                    "found": False,
                    "reason": "provider_unsupported",
                }
            else:
                state = webhook_setup.probe_provider_hook(
                    provider=provider_name,
                    repo=repo,
                    callback_url=url,
                    skill_id=skill_id,
                    runner=RUNNER,
                )
                payload["provider_hook"] = {
                    "found": state.found,
                    "hook_id": state.hook_id,
                    "url": state.url,
                    "active": state.active,
                    "events": list(state.events),
                    "last_delivery_at": state.last_delivery_at,
                    "last_response_code": state.last_response_code,
                    "reason": state.reason,
                }

        emit_data(payload, text_renderer=_render_status)

    _common.run(_do())


def _render_status(payload: dict[str, Any]) -> None:
    status = payload.get("status")
    label = {
        "registered": "[green]зарегистрирован (hook создан хабом)[/]",
        "manual": "[yellow]запись есть, hook настраивается вне хаба[/]",
        "none": "[red]не зарегистрирован[/]",
    }.get(str(status), str(status))
    console.print(f"Webhook [bold]{payload['skill']}[/]: {label}")
    if payload.get("provider"):
        console.print(f"  Провайдер: [bold]{payload['provider']}[/]")
    if payload.get("url"):
        console.print(f"  Приёмник: [dim]{payload['url']}[/]")
    hook = payload.get("provider_hook")
    if not hook:
        return
    if not hook.get("found"):
        console.print(
            f"  У провайдера hook не найден "
            f"([dim]{hook.get('reason') or 'нет совпадающего URL'}[/])"
        )
        return
    console.print(f"  У провайдера: hook [bold]{hook.get('hook_id')}[/] найден")
    if hook.get("events"):
        console.print(f"    события: {', '.join(hook['events'])}")
    console.print(
        f"    последняя доставка: {hook.get('last_delivery_at') or '—'}"
        f" (код ответа: {hook.get('last_response_code') or '—'})"
    )


def cmd_webhook_revoke(
    slug: str = typer.Argument(
        ..., metavar="ID_ИЛИ_SLUG", help="id-или-slug навыка (backend принимает оба)"
    ),
    no_provider: bool = typer.Option(
        False,
        "--no-provider",
        help="Снять только запись на хабе; hook у провайдера оставить.",
    ),
) -> None:
    """Отключить автообновления: снять запись на хабе и hook у провайдера.

    Hook, созданный ЛОКАЛЬНЫМ gh/glab, хаб удалить не может (он не знает его
    id) — поэтому снимаем его отсюда, иначе провайдер продолжит стучаться.
    """
    cfg = ClientConfig.load()
    access = _common.get_access_token()

    async def _do() -> None:
        client = _common.make_client(cfg, access)
        try:
            skill_id, repo_url = await _load_skill(client, slug)
            # URL приёмника нужен ДО удаления записи — по нему опознаём свой hook.
            st: dict[str, Any] = await client.get_skill_webhook(skill_id) or {}
            await client.delete_skill_webhook(skill_id)
        finally:
            await client.close()

        url = str(st.get("url") or "")
        provider_name, repo = _provider_and_repo(repo_url, st.get("provider"))
        payload: dict[str, Any] = {
            "event": "webhook_revoke",
            "skill": slug,
            "skill_id": skill_id,
            "hub_removed": True,
            "provider": provider_name,
        }
        if no_provider or repo is None or provider_name not in ("github", "gitlab"):
            payload["provider_hook"] = {"action": "skipped", "reason": "not_attempted"}
        else:
            outcome = webhook_setup.delete_provider_hook(
                provider=provider_name,
                repo=repo,
                callback_url=url,
                skill_id=skill_id,
                runner=RUNNER,
            )
            payload["provider_hook"] = {
                "action": outcome.action,
                "hook_id": outcome.hook_id,
                "reason": outcome.reason,
                "detail": outcome.detail,
            }
        emit_data(
            payload,
            text_renderer=lambda p: console.print(
                f"[green]✓[/] Автообновления {p['skill']} отключены "
                f"(хаб: снято; провайдер: {(p['provider_hook'] or {}).get('action')})"
            ),
        )

    _common.run(_do())


def register(app: typer.Typer, *, can_manage: bool = False) -> None:
    """Регистрирует sub-app ``webhook``.

    Гейт ``can_manage`` — зеркало RBAC бэкенда на этих ручках (владелец навыка /
    ``skill.manage`` / ``hub.admin``); финально права режет backend.
    """
    if not can_manage:
        return
    webhook_app = typer.Typer(
        no_args_is_help=True,
        help="Автообновления навыка из git: register / status / revoke",
    )
    webhook_app.command("register")(cmd_webhook_register)
    webhook_app.command("status")(cmd_webhook_status)
    webhook_app.command("revoke")(cmd_webhook_revoke)
    app.add_typer(webhook_app, name="webhook")
