"""Skills Hub CLI — модульный typer entrypoint.

Команды видны в --help только если у залогиненного пользователя есть
соответствующий permission в JWT (получен от backend при login).

Базовый набор (без auth): login, set-tokens, status.
После login → добавляются list/show/install/update/report (по permissions).
Для skill-creator → publish.
Для hub-admin / company-admin → admin sub-app с подкомандами.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.json import JSON as RichJSON
from rich.prompt import Prompt
from rich.table import Table

from skills_hub_cli.config import (
    ClientConfig,
    clear_tokens,
    load_tokens,
    populate_from_jwt,
    save_tokens,
    set_active_profile,
)
from skills_hub_cli.core import linker, project_manifest
from skills_hub_cli.core.agents import detect_agent, get_target
from skills_hub_cli.core.installer import SkillInstaller, read_meta
from skills_hub_cli.core.manifest_builder import build_manifest, git_commit_sha
from skills_hub_cli.core.secret_scan import scan_dir as secret_scan_dir
from skills_hub_cli.daemon.instrumentation import track_skill_event
from skills_hub_cli.core.transport import ApiError, HubClient
from skills_hub_cli.output import (
    emit_data,
    emit_error,
    emit_message,
    init_output_mode,
    is_json,
)

console = Console()


# ---------- pre-pass argv: --profile / --json активируются ДО построения app ----------
def _parse_profile_arg() -> Optional[str]:
    for i, a in enumerate(sys.argv):
        if a in ("--profile", "-P") and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith("--profile="):
            return a.split("=", 1)[1]
    return None


def _parse_json_flag() -> bool:
    return "--json" in sys.argv or "-J" in sys.argv


set_active_profile(_parse_profile_arg())
_cfg_for_output = ClientConfig.load()
init_output_mode(
    json_flag=_parse_json_flag(),
    config_format=_cfg_for_output.output_format,
)


# ---------- helpers ----------
def _run(coro) -> None:  # noqa: ANN001
    """Запуск async-команды с ЕДИНЫМ контрактом ошибок.

    json-режим: ожидаемые ошибки (ApiError / RuntimeError) → emit_error =
    {"event":"error","code":...,"message":...} одной JSON-строкой в stderr,
    stdout не засоряется plain-текстом, traceback не печатается.
    text-режим: прежнее читабельное «Ошибка API: ...» (ApiError) /
    «RUNTIME: ...» (RuntimeError). В обоих случаях exit 1.
    """
    try:
        asyncio.run(coro)
    except ApiError as e:
        if is_json():
            emit_error(e.code or "API", e.message or str(e), status_code=e.status_code)
        else:
            console.print(f"[red]Ошибка API:[/] {e}")
        sys.exit(1)
    except (typer.Exit, typer.Abort):
        # click.exceptions.Exit/Abort наследуют RuntimeError — это штатное
        # завершение команды (emit_error уже сделан), пропускаем насквозь.
        raise
    except RuntimeError as e:
        # Например installer._clone_version: RuntimeError('git clone failed: ...')
        # — короткое сообщение вместо многоэкранного Rich-traceback.
        emit_error("RUNTIME", str(e))
        sys.exit(1)


def _strip_invite_url(value: str) -> str:
    m = re.match(r".*/(?:auth/)?invite/([A-Za-z0-9_\-]+)/?$", value)
    if m:
        return m.group(1)
    return value


def _get_access_token() -> str:
    cfg = ClientConfig.load()
    if not cfg.user_email:
        console.print("[red]Не авторизован.[/] Сначала: skills-hub login <invite>")
        raise typer.Exit(1)
    access, _ = load_tokens(cfg.user_email)
    if not access:
        console.print("[red]Локальный access-токен не найден.[/] Сделайте login заново.")
        raise typer.Exit(1)
    return access


def _make_refresh_callback(cfg: ClientConfig) -> object:
    """Возвращает callback который CLI передаёт в HubClient.

    При 401 HubClient вызывает callback → callback дёргает /auth/refresh
    с сохранённым refresh-токеном, получает новый pair, сохраняет в keyring,
    возвращает (access, refresh). Если refresh не сработал — None и
    пользователю показывается 401-ошибка как обычно.
    """

    async def _refresh() -> tuple[str, str] | None:
        if not cfg.user_email:
            return None
        _, refresh = load_tokens(cfg.user_email)
        if not refresh:
            return None
        sub = HubClient(base_url=cfg.base_url, access_token=None)
        try:
            data = await sub.refresh(refresh)
        except ApiError:
            return None
        finally:
            await sub.close()
        new_access = data["access_token"]
        new_refresh = data["refresh_token"]
        save_tokens(cfg.user_email, new_access, new_refresh)
        populate_from_jwt(cfg, new_access)
        cfg.save()
        return (new_access, new_refresh)

    return _refresh


def _parse_version(raw: str) -> tuple[int, ...] | None:
    """Парсит semver-подобную строку в кортеж int-сегментов для сравнения.

    Толерантно к:
    - префиксу ``v``/``V`` (``v1.2.3`` → ``(1, 2, 3)``);
    - нечисловым хвостам/pre-release (``1.2.0-rc1`` → ``(1, 2, 0)`` — берём
      только ведущие числовые сегменты);
    Возвращает ``None``, если ни одного числового сегмента распарсить нельзя
    (вызывающий код тогда падает на строковое сравнение ``!=``).
    """
    s = raw.strip()
    if s[:1] in ("v", "V"):
        s = s[1:]
    parts: list[int] = []
    for seg in s.split("."):
        m = re.match(r"\d+", seg.strip())
        if not m:
            break  # первый не-числовой сегмент обрывает разбор (хвост игнор)
        parts.append(int(m.group()))
    return tuple(parts) if parts else None


def _is_newer(candidate: str, current: str) -> bool:
    """True, только если ``candidate`` СТРОГО новее ``current`` (semver-like).

    Баг B8: раньше апдейт гейтился ``bundle["version"] != current`` — downgrade
    воспринимался как апдейт. Сравниваем числовые сегменты; недостающие
    сегменты добиваются нулями (``1.2`` == ``1.2.0``). Если хотя бы одна из
    версий нераспарсиваема — fallback на строковое ``!=`` (не хуже прежнего
    поведения, но и не лучше — зато не маскирует баг для валидных версий).
    """
    cand = _parse_version(candidate)
    cur = _parse_version(current)
    if cand is None or cur is None:
        return candidate != current
    width = max(len(cand), len(cur))
    cand_p = cand + (0,) * (width - len(cand))
    cur_p = cur + (0,) * (width - len(cur))
    return cand_p > cur_p


def _maybe_auto_update(cfg: ClientConfig) -> None:
    """Тихо обновляет установленные skills до latest published если cooldown прошёл."""
    if not cfg.auto_update or not cfg.is_logged_in():
        return
    cooldown = timedelta(minutes=cfg.auto_update_cooldown_min)
    if cfg.last_auto_update_at:
        try:
            last = datetime.fromisoformat(cfg.last_auto_update_at)
            if datetime.now(UTC) - last < cooldown:
                return
        except ValueError:
            pass
    target = get_target(cfg.agent)
    base = target.base_dir()
    if not base.exists():
        return
    installed: list[tuple[str, str]] = []
    for slug_dir in base.iterdir():
        meta = read_meta(slug_dir)
        if meta:
            installed.append((meta["slug"], meta.get("version", "0.0.0")))
    if not installed:
        cfg.last_auto_update_at = datetime.now(UTC).isoformat()
        cfg.save()
        return
    access, _ = load_tokens(cfg.user_email or "")
    if not access:
        return

    # Прогресс auto-update — ТОЛЬКО в stderr: stdout — машинный канал
    # (--json), его нельзя засорять (живой факт: строка «↑ auto-update»
    # ломала парсинг JSON-вывода).
    err_console = Console(stderr=True)

    async def _do() -> None:
        client = HubClient(base_url=cfg.base_url, access_token=access, on_token_refresh=_make_refresh_callback(cfg))
        try:
            for slug, current in installed:
                try:
                    bundle = await client.install_bundle(slug)
                except Exception:
                    continue
                # P0: bundle без repo_url = stub-источник — обновлять нечем
                # (живой инцидент: такой «апдейт» затирал реальный контент
                # 112-байтовым stub'ом). Пропускаем кандидата целиком.
                if not bundle.get("repo_url"):
                    continue
                # B8: апдейтим ТОЛЬКО если опубликованная версия строго новее
                # установленной — downgrade/равные пропускаем.
                if _is_newer(bundle["version"], current):
                    installer = SkillInstaller(target, cfg.effective_store_dir())
                    res = installer.install(
                        slug=slug,
                        version=bundle["version"],
                        commit_sha=bundle["commit_sha"],
                        repo_url=bundle.get("repo_url"),
                        manifest=bundle["manifest"],
                    )
                    if getattr(res, "skipped", False):
                        continue  # guard отказал (stub-would-clobber и т.п.)
                    err_console.print(
                        f"[dim cyan]↑ auto-update[/] {slug}: {current} → {bundle['version']}"
                    )
            # Cooldown-таймстамп двигаем всегда после успешного прохода (даже
            # если ничего не обновилось) — иначе фон-проверка зациклится без
            # учёта cooldown. Раньше это маскировал мёртвый `if … or True`.
            cfg.last_auto_update_at = datetime.now(UTC).isoformat()
            cfg.save()
        finally:
            await client.close()

    try:
        asyncio.run(_do())
    except Exception:
        pass  # auto-update не должен ломать команду


# ======================================================
#                  COMMAND IMPLEMENTATIONS
# ======================================================
def cmd_login(
    invite: Optional[str] = typer.Argument(
        None,
        help=(
            "Invite-токен или URL (для invite-flow). Опускайте если хотите "
            "залогиниться через --email/--password."
        ),
    ),
    email: Optional[str] = typer.Option(None),
    name: Optional[str] = typer.Option(None),
    password: Optional[str] = typer.Option(
        None,
        "--password",
        help=(
            "Если указан и invite опущен → POST /auth/login (email + password). "
            "Если только --email указан без --password — пароль запрошен интерактивно."
        ),
    ),
    base_url: Optional[str] = typer.Option(None),
) -> None:
    """Логин: либо invite-token (invite-flow), либо email + password.

    Если передан positional `invite` — invite-flow (как раньше). Если invite
    опущен — email+password flow (POST /auth/login).
    """
    cfg = ClientConfig.load()
    if base_url:
        cfg.base_url = base_url

    if invite is None:
        # --- email + password flow ---
        if email is None:
            if is_json():
                emit_error(
                    "VALIDATION",
                    "В json-режиме --email обязателен для password-flow",
                )
                raise typer.Exit(1)
            email = Prompt.ask("Email")
        if password is None:
            if is_json():
                emit_error(
                    "VALIDATION",
                    "В json-режиме --password обязателен",
                )
                raise typer.Exit(1)
            password = typer.prompt("Пароль", hide_input=True)
        _do_password_login(cfg, email=email, password=password)
        return

    # --- invite-flow (как раньше) ---
    if email is None:
        email = "" if is_json() else Prompt.ask("Email")
    if name is None:
        name = "" if is_json() else Prompt.ask("Имя для отображения")
    token = _strip_invite_url(invite)

    async def _do() -> None:
        client = HubClient(base_url=cfg.base_url)
        try:
            data = await client.login_invite(
                invite_token=token, email=email, display_name=name
            )
        finally:
            await client.close()
        save_tokens(email, data["access_token"], data["refresh_token"])
        cfg.user_email = email
        populate_from_jwt(cfg, data["access_token"])
        cfg.save()
        result = {
            "event": "logged_in",
            "user_email": email,
            "is_new_user": bool(data.get("is_new_user")),
            "is_hub_admin": cfg.is_hub_admin(),
            "is_skill_creator": cfg.is_skill_creator(),
            "permissions": cfg.permissions,
            "company_id": cfg.company_id,
            "role_id": cfg.role_id,
            "access_expires_at": cfg.access_expires_at,
        }

        def _render(_: dict) -> None:
            console.print(f"[green]✓[/] Авторизован как {email}")
            if data.get("is_new_user"):
                console.print("  (новый пользователь, аккаунт создан)")
            roles_descr = []
            if cfg.is_hub_admin():
                roles_descr.append("hub-admin")
            if cfg.is_skill_creator():
                roles_descr.append("skill-creator")
            if cfg.permissions and not roles_descr:
                roles_descr.append("member")
            console.print(f"  Роли:        {', '.join(roles_descr) or '—'}")
            console.print(f"  Permissions: {len(cfg.permissions)} прав")
            console.print(
                "[dim]Доступные команды зависят от прав — `skills-hub --help`[/]"
            )

        emit_data(result, text_renderer=_render)

    _run(_do())


def _do_password_login(cfg: ClientConfig, *, email: str, password: str) -> None:
    async def _do() -> None:
        client = HubClient(base_url=cfg.base_url)
        try:
            data = await client.login_password(email=email, password=password)
        finally:
            await client.close()
        save_tokens(email, data["access_token"], data["refresh_token"])
        cfg.user_email = email
        populate_from_jwt(cfg, data["access_token"])
        cfg.save()
        result = {
            "event": "logged_in",
            "method": "password",
            "user_email": email,
            "is_hub_admin": cfg.is_hub_admin(),
            "is_skill_creator": cfg.is_skill_creator(),
            "permissions": cfg.permissions,
            "company_id": cfg.company_id,
            "role_id": cfg.role_id,
            "access_expires_at": cfg.access_expires_at,
        }

        def _render(_: dict) -> None:
            console.print(f"[green]✓[/] Авторизован как {email} (password)")
            roles_descr = []
            if cfg.is_hub_admin():
                roles_descr.append("hub-admin")
            if cfg.is_skill_creator():
                roles_descr.append("skill-creator")
            if cfg.permissions and not roles_descr:
                roles_descr.append("member")
            console.print(f"  Роли:        {', '.join(roles_descr) or '—'}")
            console.print(f"  Permissions: {len(cfg.permissions)} прав")

        emit_data(result, text_renderer=_render)

    _run(_do())


def cmd_passwd() -> None:
    """Сменить (или установить) пароль текущего user'а.

    Требует залогиненную сессию. Запрашивает новый пароль интерактивно,
    с подтверждением. В JSON-режиме — необходима env-переменная
    SKILLS_HUB_NEW_PASSWORD (для скриптов).
    """
    cfg = ClientConfig.load()
    if not cfg.is_logged_in():
        emit_error("NOT_LOGGED_IN", "Сначала залогиньтесь: skills-hub login")
        raise typer.Exit(1)
    access, _ = load_tokens(cfg.user_email or "")
    if not access:
        emit_error("NO_TOKEN", "Токен не найден. Сделайте login заново.")
        raise typer.Exit(1)

    if is_json():
        new_pw = os.environ.get("SKILLS_HUB_NEW_PASSWORD")
        if not new_pw:
            emit_error(
                "VALIDATION",
                "В json-режиме новый пароль через env SKILLS_HUB_NEW_PASSWORD",
            )
            raise typer.Exit(1)
    else:
        new_pw = typer.prompt("Новый пароль", hide_input=True)
        confirm = typer.prompt("Повторите пароль", hide_input=True)
        if new_pw != confirm:
            emit_error("VALIDATION", "Пароли не совпадают")
            raise typer.Exit(1)

    if len(new_pw) < 8:
        emit_error("VALIDATION", "Пароль должен быть не короче 8 символов")
        raise typer.Exit(1)

    async def _do() -> None:
        client = HubClient(
            base_url=cfg.base_url,
            access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        try:
            await client.set_password(new_password=new_pw)
        finally:
            await client.close()
        emit_data(
            {"event": "password_changed"},
            text_renderer=lambda _: console.print(
                "[green]✓[/] Пароль обновлён. Теперь логин: "
                f"`skills-hub login --email {cfg.user_email} --password ***`"
            ),
        )

    _run(_do())


def cmd_logout() -> None:
    """Очистить локальные токены и permissions."""
    cfg = ClientConfig.load()
    if cfg.user_email:
        clear_tokens(cfg.user_email)
    cfg.user_email = None
    cfg.permissions = []
    cfg.company_id = None
    cfg.role_id = None
    cfg.access_expires_at = None
    cfg.save()
    emit_data(
        {"event": "logged_out"},
        text_renderer=lambda _: console.print(
            "[green]✓[/] Вышли. Доступна только команда login."
        ),
    )


def cmd_whoami() -> None:
    """Кто я и что доступно."""
    cfg = ClientConfig.load()
    if not cfg.user_email:
        emit_error("NOT_AUTHENTICATED", "Не авторизован")
        raise typer.Exit(1)
    payload = {
        "user_email": cfg.user_email,
        "backend": cfg.base_url,
        "agent": cfg.agent or detect_agent(),
        "is_hub_admin": cfg.is_hub_admin(),
        "is_skill_creator": cfg.is_skill_creator(),
        "company_id": cfg.company_id,
        "role_id": cfg.role_id,
        "permissions": cfg.permissions,
        "auto_update": cfg.auto_update,
        "output_format": cfg.output_format,
    }

    def _render(p: dict) -> None:
        console.print(f"[bold]{p['user_email']}[/]")
        console.print(f"  Backend:    {p['backend']}")
        console.print(f"  Agent:      {p['agent']}")
        roles = []
        if p["is_hub_admin"]:
            roles.append("hub-admin")
        if p["is_skill_creator"]:
            roles.append("skill-creator")
        if p["company_id"]:
            # PK-миграция: company_id теперь числовой id (строкой), обрезка
            # бессмысленна — показываем полностью.
            roles.append(f"company={p['company_id']}")
        console.print(f"  Роли:       {', '.join(roles) or 'member'}")
        console.print(
            f"  Permissions ({len(p['permissions'])}): {', '.join(p['permissions']) or '—'}"
        )
        console.print(f"  Auto-update: {'on' if p['auto_update'] else 'off'}")
        console.print(f"  Output:      {p['output_format']}")

    emit_data(payload, text_renderer=_render)


def _render_web_text(url: str, expires_at: str, no_browser: bool) -> None:
    console.print(f"[bold green]Открываю Web UI:[/bold green] {url}")
    console.print(f"[dim]Код действует до:[/dim] {expires_at}")
    if no_browser:
        console.print(
            "[yellow]Браузер не открыт (флаг --no-browser). "
            "Откройте URL вручную.[/yellow]"
        )
    else:
        console.print(
            "[dim]Если браузер не открылся автоматически — "
            "скопируйте URL вручную.[/dim]"
        )


def cmd_web(
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Не открывать браузер, только напечатать URL"
    ),
) -> None:
    """Открыть Web UI в браузере с автоматической авторизацией (handoff из CLI).

    В режиме `--json` браузер НЕ открывается автоматически — JSON режим
    считается scripting-режимом (subagent / CI), вывод используется
    программно. Чтобы всё-таки открыть из JSON-режима — пользуйся
    `opened_browser` полем в payload и обработай его на стороне caller'а.
    """
    cfg = ClientConfig.load()
    if not cfg.is_logged_in():
        emit_error(
            "NOT_LOGGED_IN",
            "Сначала залогиньтесь: skills-hub login <invite-token>",
        )
        raise typer.Exit(1)
    access, _refresh = load_tokens(cfg.user_email or "")
    if not access:
        emit_error("NO_TOKEN", "Токен не найден. Сделайте login заново.")
        raise typer.Exit(1)

    async def _do() -> dict[str, str]:
        client = HubClient(
            base_url=cfg.base_url,
            access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        try:
            return await client.exchange_create()
        finally:
            await client.close()

    result = asyncio.run(_do())
    code = result["code"]
    expires_at = result["expires_at"]
    web_base = cfg.effective_web_ui_url().rstrip("/")
    target_url = f"{web_base}/login?code={code}"

    # Auto-suppress browser в JSON-режиме (scripting / subagent context).
    effective_no_browser = no_browser or is_json()

    emit_data(
        {
            "url": target_url,
            "code": code,
            "expires_at": expires_at,
            "web_base_url": web_base,
            "opened_browser": not effective_no_browser,
        },
        text_renderer=lambda _: _render_web_text(
            target_url, expires_at, effective_no_browser
        ),
    )

    if not effective_no_browser:
        import webbrowser

        webbrowser.open(target_url)


def cmd_status(
    project: Optional[Path] = typer.Option(None, "--project"),
) -> None:
    """Локальный статус: agent + что установлено в global + project scope."""
    cfg = ClientConfig.load()
    target = get_target(cfg.agent)
    actual_project = (
        project
        or (Path(cfg.default_project_dir) if cfg.default_project_dir else None)
        or Path.cwd()
    )
    global_items = _scan_installed(target, project=None)
    project_items = _scan_installed(target, project=actual_project)
    payload = {
        "agent": target.name,
        "global_skills_dir": str(target.base_dir()),
        "project_skills_dir": str(target.base_dir(project=actual_project)),
        "project_root": str(actual_project),
        "user_email": cfg.user_email,
        "logged_in": bool(cfg.user_email),
        "default_install_scope": cfg.default_install_scope,
        "installed_global": global_items,
        "installed_project": project_items,
    }

    def _render(p: dict) -> None:
        console.print(f"Agent:           [bold]{p['agent']}[/]")
        console.print(f"Global dir:      {p['global_skills_dir']}")
        console.print(f"Project root:    {p['project_root']}")
        console.print(f"Project dir:     {p['project_skills_dir']}")
        console.print(f"Default scope:   {p['default_install_scope']}")
        if p["user_email"]:
            console.print(f"User:            {p['user_email']}")
        else:
            console.print("[yellow]Не авторизован[/]")
        console.print(
            f"Installed:       global={len(p['installed_global'])}  "
            f"project={len(p['installed_project'])}"
        )

    emit_data(payload, text_renderer=_render)


def cmd_set_tokens(
    email: str = typer.Argument(...),
    access: str = typer.Option(..., help="Access JWT"),
    refresh: str = typer.Option(..., help="Refresh token"),
    base_url: Optional[str] = typer.Option(None),
) -> None:
    """[ADVANCED] Положить токены напрямую (минуя backend login)."""
    cfg = ClientConfig.load()
    if base_url:
        cfg.base_url = base_url
    save_tokens(email, access, refresh)
    cfg.user_email = email
    populate_from_jwt(cfg, access)
    cfg.save()
    emit_data(
        {
            "event": "tokens_saved",
            "user_email": email,
            "permissions": cfg.permissions,
        },
        text_renderer=lambda p: console.print(
            f"[green]✓[/] Токены и permissions сохранены для {p['user_email']}"
        ),
    )


def _scan_installed(
    target,  # type: ignore[no-untyped-def]
    *,
    project: Path | None,
) -> list[dict]:
    """Сканирует scope-папку, возвращает meta + признак ссылки (linked/copied)."""
    base = target.base_dir(project=project)
    items: list[dict] = []
    if base.exists():
        for d in base.iterdir():
            meta = read_meta(d)
            if meta:
                ref = meta.get("slug") or meta.get("skill_id") or d.name
                linked = linker.is_link(d)
                tgt = linker.link_target(d) if linked else None
                items.append({
                    "slug": meta.get("slug"),
                    "skill_id": meta.get("skill_id"),
                    "ref": ref,
                    "version": meta.get("version"),
                    "commit_sha": meta.get("commit_sha"),
                    "agent": meta.get("agent"),
                    "scope": "project" if project else "global",
                    "project": str(project) if project else None,
                    "path": str(d),
                    "linked": linked,
                    "link_target": str(tgt) if tgt else None,
                })
    return items


def cmd_list(
    channel: str = typer.Option("published"),
    installed: bool = typer.Option(False, "--installed"),
    project: Optional[Path] = typer.Option(
        None, "--project", help="Project root для скана project-scope установок"
    ),
    scope: Optional[str] = typer.Option(
        None, "--scope", help="global | project | all (default: all для --installed)"
    ),
) -> None:
    """Список доступных skills (RBAC), либо --installed (global + project)."""
    cfg = ClientConfig.load()
    if installed:
        target = get_target(cfg.agent)
        wanted_scope = scope or "all"
        actual_project = (
            project
            or (Path(cfg.default_project_dir) if cfg.default_project_dir else None)
            or Path.cwd()
        )
        items: list[dict] = []
        if wanted_scope in ("global", "all"):
            items.extend(_scan_installed(target, project=None))
        if wanted_scope in ("project", "all"):
            items.extend(_scan_installed(target, project=actual_project))

        def _render(rows: list) -> None:
            if not rows:
                console.print(
                    f"[yellow]Ничего не установлено[/] "
                    f"(scope={wanted_scope}, project={actual_project})"
                )
                return
            table = Table(title=f"Установленные ({target.name})")
            table.add_column("id-или-slug")
            table.add_column("version")
            table.add_column("scope")
            table.add_column("mount")
            table.add_column("path", overflow="fold")
            for s in rows:
                table.add_row(
                    # slug может быть None у slug-less skill — показываем ref (id).
                    s.get("slug") or s.get("ref") or "—",
                    s["version"] or "—",
                    s["scope"],
                    "📎 link" if s.get("linked") else "📄 copy",
                    s["path"],
                )
            console.print(table)

        emit_data(items, text_renderer=_render)
        return

    _maybe_auto_update(cfg)
    access = _get_access_token()

    async def _do() -> None:
        client = HubClient(
            base_url=cfg.base_url,
            access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        try:
            skills = await client.list_skills(channel=channel)
        finally:
            await client.close()

        def _render(rows: list) -> None:
            table = Table(title=f"Доступные skills (channel={channel})")
            table.add_column("slug")
            table.add_column("title")
            table.add_column("tags")
            table.add_column("versions")
            for s in rows:
                table.add_row(
                    s["slug"],
                    s["title"],
                    ", ".join(s.get("tags", [])),
                    ", ".join(v["semver"] for v in s.get("versions", [])),
                )
            if not rows:
                console.print(
                    "[yellow]Ничего доступного.[/] Скиллы видны только если "
                    "у вас есть доступ через группы; админ должен опубликовать "
                    "skill или Sync с GitLab."
                )
            else:
                console.print(table)

        emit_data(skills, text_renderer=_render)

    _run(_do())


def cmd_show(
    slug: str = typer.Argument(
        ..., metavar="ID_ИЛИ_SLUG", help="id-или-slug скилла (backend принимает оба)"
    ),
) -> None:
    """Детали skill'а (по id-или-slug)."""
    cfg = ClientConfig.load()
    _maybe_auto_update(cfg)
    access = _get_access_token()

    async def _do() -> None:
        client = HubClient(
            base_url=cfg.base_url,
            access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        try:
            data = await client.get_skill(slug)
        finally:
            await client.close()

        def _render(d: dict) -> None:
            console.print(f"[bold]{d['title']}[/] ({d['slug']})")
            console.print(f"  description: {d['description']}")
            console.print(f"  tags:        {', '.join(d['tags']) or '—'}")
            console.print(f"  repo:        {d.get('repo_url') or '—'}")
            console.print(f"  is_super:    {d['is_super']}")
            console.print("  versions:")
            for v in d["versions"]:
                console.print(
                    f"    • {v['semver']:10} channel={v['channel']:10} commit={v['commit_sha'][:8]}"
                )

        emit_data(data, text_renderer=_render)

    _run(_do())


def _resolve_install_scope(
    cfg: ClientConfig,
    scope: Optional[str],
    project: Optional[Path],
) -> tuple[str, Optional[Path]]:
    actual_scope = scope or cfg.default_install_scope
    if actual_scope not in ("global", "project"):
        emit_error("VALIDATION", f"scope должен быть global|project, получено: {actual_scope}")
        raise typer.Exit(1)
    if actual_scope == "global":
        return "global", None
    actual_project = (
        project
        or (Path(cfg.default_project_dir) if cfg.default_project_dir else None)
        or Path.cwd()
    )
    return "project", actual_project.resolve()


def _read_skill_md_version(skill_dir: Path) -> str:
    """Версия локального навыка: из SKILL.md frontmatter (`version:`) или
    _skill_meta.toml (`version`), иначе "0.0.0-local"."""
    from skills_hub_cli.core.manifest_builder import (
        _read_frontmatter,
        _read_meta_toml,
    )

    meta_toml = _read_meta_toml(skill_dir)
    ver = meta_toml.get("version")
    if not ver:
        fm = _read_frontmatter(skill_dir / "SKILL.md")
        ver = fm.get("version")
    ver = str(ver).strip() if ver else ""
    return ver or "0.0.0-local"


def _has_skill_md(skill_dir: Path) -> bool:
    """True если папка похожа на навык (есть SKILL.md либо его frontmatter)."""
    md = skill_dir / "SKILL.md"
    return md.is_file()


async def _install_local_source(
    cfg: ClientConfig,
    *,
    source: dict,
    scope: str,
    project_path: Path | None,
    force: bool,
    agent_target,  # IAgentTarget
) -> list[dict]:
    """Материализует навык из локального источника (path / git-url) БЕЗ сети.

    source: {"kind":"path","slug":..,"path":Path}
          | {"kind":"git","slug":..,"url":str,"ref":str|None}
    Возвращает installed_chain того же формата, что и hub-режим.
    """
    installer = SkillInstaller(agent_target, cfg.effective_store_dir())
    slug = source["slug"]
    if source["kind"] == "path":
        skill_dir = Path(source["path"]).expanduser().resolve()
        if not skill_dir.is_dir():
            emit_error("VALIDATION", f"Папка не найдена: {skill_dir}")
            raise typer.Exit(1)
        if not _has_skill_md(skill_dir):
            emit_error(
                "VALIDATION",
                f"В папке нет SKILL.md — это не похоже на навык: {skill_dir}",
            )
            raise typer.Exit(1)
        version = _read_skill_md_version(skill_dir)
        result = installer.install(
            slug=slug, version=version, commit_sha="",
            repo_url=None, local_src=skill_dir, manifest={"version": version, "files": []},
            project=project_path, force=force,
        )
    else:  # git
        version = "0.0.0-local"
        # "" → дефолтная ветка репо (без --ref); иначе явный ref.
        ref = source.get("ref") or ""
        result = installer.install(
            slug=slug, version=version, commit_sha="",
            repo_url=source["url"], git_ref=ref,
            manifest={"version": version, "files": []},
            project=project_path, force=force,
        )
    track_skill_event(
        "skill.update" if result.is_update else "skill.install",
        slug=slug, version=result.version, scope=result.scope,
    )
    return [{
        "slug": slug, "skill_id": result.skill_id,
        "version": result.version, "is_update": result.is_update,
        "target_dir": str(result.target_dir), "scope": result.scope,
        "linked": result.linked, "link_kind": result.link_kind,
        "source": source["kind"],
    }]


async def _install_chain(
    cfg: ClientConfig,
    access: str,
    *,
    slug: str,
    channel: str,
    scope: str,
    project_path: Optional[Path],
    force: bool,
    agent_target,  # IAgentTarget
    source: dict | None = None,
) -> list[dict]:
    """Качает bundle (+deps), материализует в стор, линкует в scope.

    source (стратегия источника):
    - None / {"kind":"hub"} → backend bundle + git (как раньше; нужен access);
    - {"kind":"path",...} / {"kind":"git",...} → локальная материализация без
      сети (делегирует в _install_local_source; access игнорируется).

    Возвращает installed_chain (list dict с slug/version/scope/linked/...).
    Используется и cmd_install, и cmd_enable.
    """
    if source is not None and source.get("kind") in ("path", "git"):
        return await _install_local_source(
            cfg, source=source, scope=scope, project_path=project_path,
            force=force, agent_target=agent_target,
        )
    client = HubClient(
        base_url=cfg.base_url, access_token=access,
        on_token_refresh=_make_refresh_callback(cfg),
    )
    try:
        bundle = await client.install_bundle(slug, channel=channel)
        installer = SkillInstaller(agent_target, cfg.effective_store_dir())
        chain = bundle.get(
            "dependencies_chain",
            [[bundle["skill_slug"], bundle["version"], bundle.get("repo_url")]],
        )
        installed_chain: list[dict] = []
        for dep_slug, dep_version, dep_repo in chain:
            if dep_slug == bundle["skill_slug"]:
                dep_bundle = bundle
            else:
                sub = HubClient(
                    base_url=cfg.base_url, access_token=access,
                    on_token_refresh=_make_refresh_callback(cfg),
                )
                try:
                    dep_bundle = await sub.install_bundle(dep_slug, channel=channel)
                finally:
                    await sub.close()
            dep_id = dep_bundle.get("skill_id")
            result = installer.install(
                slug=dep_slug or None, version=dep_version,
                commit_sha=dep_bundle["commit_sha"],
                repo_url=dep_repo or dep_bundle.get("repo_url"),
                manifest=dep_bundle["manifest"], project=project_path,
                force=force, skill_id=dep_id,
            )
            entry = {
                "slug": dep_slug, "skill_id": result.skill_id,
                "version": dep_version, "is_update": result.is_update,
                "target_dir": str(result.target_dir), "scope": result.scope,
                "linked": result.linked, "link_kind": result.link_kind,
            }
            # Фикс 5в: stub-установка помечается в JSON-ответе явно.
            if result.content == "stub":
                entry["content"] = "stub"
            if result.skipped:
                entry["skipped"] = True
                entry["skip_reason"] = result.skip_reason
            installed_chain.append(entry)
            if not result.skipped:
                track_skill_event(
                    "skill.update" if result.is_update else "skill.install",
                    slug=dep_slug or (str(dep_id) if dep_id is not None else ""),
                    version=dep_version, scope=result.scope,
                )
        return installed_chain
    finally:
        await client.close()


def cmd_install(
    slug: str = typer.Argument(
        ..., metavar="ID_ИЛИ_SLUG", help="id-или-slug скилла (backend принимает оба)"
    ),
    channel: str = typer.Option("published"),
    agent: Optional[str] = typer.Option(None),
    scope: Optional[str] = typer.Option(
        None, "--scope", help="global | project (default из config.default_install_scope)"
    ),
    project: Optional[Path] = typer.Option(
        None, "--project", help="Если scope=project — путь к корню проекта (default: cwd)"
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Перезаписать существующую папку (если в ней нет _skill_meta.json — например, старая ручная установка)",
    ),
    path: Optional[Path] = typer.Option(
        None, "--path",
        help="Локальная папка-источник навыка (автономно, без хаба и сети). "
             "Должна содержать SKILL.md.",
    ),
    from_git: Optional[str] = typer.Option(
        None, "--from-git",
        help="URL git-репозитория навыка (автономно, без backend bundle). "
             "Ветку/тег задаёт --ref.",
    ),
    ref: Optional[str] = typer.Option(
        None, "--ref",
        help="Git-ref (ветка/тег/sha) для --from-git (default: HEAD репозитория).",
    ),
) -> None:
    """Установить skill из хаба, локальной папки (--path) или git-url (--from-git).

    Источники (взаимоисключающие):
    - по умолчанию (без --path/--from-git) — из хаба по id-или-slug (нужен login);
    - `--path ./skill` — скопировать локальную папку как навык (БЕЗ login/сети);
    - `--from-git <url> [--ref <branch/tag>]` — clone произвольного репо (БЕЗ хаба).

    global  → ~/.claude/skills/<id-или-slug>/  (видны во всех Claude Code сессиях)
    project → <project>/.claude/skills/<id-или-slug>/  (только в данном проекте)
    """
    cfg = ClientConfig.load()
    actual_scope, project_path = _resolve_install_scope(cfg, scope, project)

    # --- разбор источника + взаимоисключение флагов ---
    if path is not None and from_git is not None:
        emit_error("VALIDATION", "--path и --from-git взаимоисключающие")
        raise typer.Exit(1)
    if ref is not None and from_git is None:
        emit_error("VALIDATION", "--ref имеет смысл только вместе с --from-git")
        raise typer.Exit(1)

    source: dict | None = None
    if path is not None:
        source = {"kind": "path", "slug": slug, "path": path}
    elif from_git is not None:
        source = {"kind": "git", "slug": slug, "url": from_git, "ref": ref}

    target = get_target(agent or cfg.agent)
    _ = actual_scope  # передаётся через project_path

    # Hub-режим (источник не задан) требует login+токен; локальные — нет.
    access = ""
    if source is None:
        _maybe_auto_update(cfg)
        access = _get_access_token()

    async def _do() -> None:
        installed_chain = await _install_chain(
            cfg, access, slug=slug, channel=channel, scope=actual_scope,
            project_path=project_path, force=force, agent_target=target,
            source=source,
        )
        # project scope → фиксируем набор в манифесте проекта.
        if project_path is not None:
            for item in installed_chain:
                ref = item["slug"] or item.get("skill_id")
                if ref:
                    project_manifest.add(project_path, str(ref))

        # Фикс 5в/1: stub и skip — явные предупреждения (json-режим: stderr,
        # stdout остаётся чистым машинным каналом).
        for item in installed_chain:
            label = item.get("slug") or item.get("skill_id") or "?"
            if item.get("skipped"):
                emit_message(
                    f"«{label}»: установка пропущена ({item.get('skip_reason')}) — "
                    "stub не может заменить существующую непустую установку.",
                    level="warn",
                )
            elif item.get("content") == "stub":
                emit_message(
                    f"«{label}» установлен как stub: у скилла в хабе нет "
                    "git-репозитория, контент — заглушка SKILL.md.",
                    level="warn",
                )

        def _render(items: list) -> None:
            for item in items:
                if item.get("skipped"):
                    console.print(
                        f"[yellow]→ Пропущен[/] ({item['scope']}) "
                        f"{item['slug']}@{item['version']}: {item.get('skip_reason')}"
                    )
                    continue
                action = "Обновлён" if item["is_update"] else "Установлен"
                mount = "📎" if item["linked"] else "📄"
                console.print(
                    f"[green]✓[/] {action} ({item['scope']}) {mount} "
                    f"{item['slug']}@{item['version']} → {item['target_dir']}"
                )

        emit_data(installed_chain, text_renderer=_render)

    _run(_do())


def _resolve_project(cfg: ClientConfig, project: Optional[Path]) -> Path:
    return (
        project
        or (Path(cfg.default_project_dir) if cfg.default_project_dir else None)
        or Path.cwd()
    ).resolve()


def _path_within(base: Path, candidate: Path) -> bool:
    try:
        base_abs = os.path.abspath(base)
        cand_abs = os.path.abspath(candidate)
        return os.path.commonpath([base_abs, cand_abs]) == base_abs
    except ValueError:
        return False


def cmd_enable(
    slug: str = typer.Argument(..., metavar="ID_ИЛИ_SLUG"),
    project: Optional[Path] = typer.Option(None, "--project", help="Корень проекта (default: cwd)"),
    agent: Optional[str] = typer.Option(None),
    force: bool = typer.Option(False, "--force"),
    channel: str = typer.Option("published"),
) -> None:
    """Включить навык в наборе проекта: стор + ссылка в project scope + манифест.

    Store-first: если навык уже материализован в сторе — локальный re-link
    БЕЗ сети и логина (работает оффлайн для любого источника: hub /
    local-path / git-url). Докачка из хаба нужна только когда навыка в сторе
    нет (требует login).
    """
    cfg = ClientConfig.load()
    project_path = _resolve_project(cfg, project)
    target = get_target(agent or cfg.agent)

    # --- store-first: навык уже в сторе → re-link + манифест, без сети ---
    installer = SkillInstaller(target, cfg.effective_store_dir())
    local = installer.link_existing(slug, project=project_path, force=force)
    if local is not None:
        linked, link_kind = local
        store_meta = read_meta(cfg.effective_store_dir() / slug) or {}
        project_manifest.add(project_path, slug)
        track_skill_event(
            "skill.install", slug=slug,
            version=store_meta.get("version") or "", scope="project",
        )
        item = {
            "slug": store_meta.get("slug") or slug,
            "skill_id": store_meta.get("skill_id"),
            "version": store_meta.get("version"),
            "is_update": False,
            "target_dir": str(target.slug_dir(slug, project=project_path)),
            "scope": "project", "linked": linked, "link_kind": link_kind,
            "source": "store",
        }

        def _render_local(_: dict) -> None:
            mount = "📎" if item["linked"] else "📄"
            console.print(
                f"[green]✓[/] Включён в проект {mount} "
                f"{item['slug']}@{item['version']} → {item['target_dir']} "
                "[dim](из стора, без сети)[/]"
            )
            console.print(f"[dim]Манифест: {project_manifest.manifest_path(project_path)}[/]")

        emit_data(
            {"event": "enabled", "project": str(project_path), "skills": [item]},
            text_renderer=_render_local,
        )
        return

    # --- в сторе нет → докачка из хаба (нужен login) ---
    if not cfg.is_logged_in():
        emit_error(
            "NOT_LOGGED_IN",
            f"Навыка «{slug}» нет в локальном сторе; для докачки из хаба "
            "залогиньтесь: skills-hub login",
        )
        raise typer.Exit(1)
    access = _get_access_token()

    async def _do() -> None:
        installed_chain = await _install_chain(
            cfg, access, slug=slug, channel=channel, scope="project",
            project_path=project_path, force=force, agent_target=target,
        )
        for item in installed_chain:
            ref = item["slug"] or item.get("skill_id")
            if ref:
                project_manifest.add(project_path, str(ref))

        def _render(_: dict) -> None:
            for item in installed_chain:
                mount = "📎" if item["linked"] else "📄"
                console.print(
                    f"[green]✓[/] Включён в проект {mount} "
                    f"{item['slug']}@{item['version']} → {item['target_dir']}"
                )
            console.print(f"[dim]Манифест: {project_manifest.manifest_path(project_path)}[/]")

        emit_data(
            {"event": "enabled", "project": str(project_path), "skills": installed_chain},
            text_renderer=_render,
        )

    _run(_do())


def cmd_disable(
    slug: str = typer.Argument(..., metavar="ID_ИЛИ_SLUG"),
    project: Optional[Path] = typer.Option(None, "--project"),
    agent: Optional[str] = typer.Option(None),
) -> None:
    """Выключить навык из набора проекта: снять ссылку + убрать из манифеста (стор цел)."""
    cfg = ClientConfig.load()
    project_path = _resolve_project(cfg, project)
    target = get_target(agent or cfg.agent)
    installer = SkillInstaller(target, cfg.effective_store_dir())
    result = installer.remove(slug=slug, project=project_path)
    in_manifest = project_manifest.remove(project_path, slug)
    track_skill_event("skill.uninstall", slug=slug, scope="project")

    def _render(_: dict) -> None:
        if result.removed:
            console.print(f"[green]✓[/] Выключен из проекта: {slug} [dim](стор сохранён)[/]")
        else:
            console.print(f"[yellow]Не был включён[/]: {slug}")
        if in_manifest:
            console.print("[dim]Убран из .skills-hub/skills.toml[/]")

    emit_data(
        {"event": "disabled", "slug": slug, "project": str(project_path),
         "unlinked": result.removed, "manifest_removed": in_manifest},
        text_renderer=_render,
    )


def cmd_sync(
    project: Optional[Path] = typer.Option(None, "--project", help="Корень проекта (default: cwd)"),
    prune: bool = typer.Option(True, "--prune/--no-prune",
                               help="Удалять наши ссылки, которых нет в манифесте"),
    agent: Optional[str] = typer.Option(None),
    channel: str = typer.Option("published"),
) -> None:
    """Привести project scope в соответствие .skills-hub/skills.toml.

    Линкует навыки из стора; отсутствующие в сторе — докачивает; --prune убирает
    наши (на стор) ссылки, которых нет в манифесте. Чужие папки и внешние ссылки
    не трогаются.
    """
    cfg = ClientConfig.load()
    project_path = _resolve_project(cfg, project)
    target = get_target(agent or cfg.agent)
    installer = SkillInstaller(target, cfg.effective_store_dir())
    manifest = project_manifest.load(project_path)
    store_root = cfg.effective_store_dir()
    report: dict[str, list] = {
        "linked": [], "downloaded": [], "pruned": [], "missing": [],
    }
    access_holder: dict[str, str] = {}

    async def _do() -> None:
        for slug in sorted(manifest):
            out = installer.link_existing(slug, project=project_path, force=True)
            if out is not None:
                report["linked"].append(slug)
                continue
            # Нет в сторе → докачать из хаба (ленивый access). Без логина НЕ
            # падаем целиком: что есть в сторе — уже слинковано, недостающее
            # уходит в missing с подсказкой залогиниться (фикс 3).
            if "tok" not in access_holder:
                if not cfg.is_logged_in():
                    report["missing"].append(slug)
                    continue
                try:
                    access_holder["tok"] = _get_access_token()
                except typer.Exit:
                    report["missing"].append(slug)
                    continue
            try:
                await _install_chain(
                    cfg, access_holder["tok"], slug=slug, channel=channel,
                    scope="project", project_path=project_path, force=True,
                    agent_target=target,
                )
                report["downloaded"].append(slug)
            except Exception:
                report["missing"].append(slug)

        if prune:
            base = target.base_dir(project=project_path)
            if base.exists():
                for d in list(base.iterdir()):
                    if d.name in manifest:
                        continue
                    if not linker.is_link(d):
                        continue  # чужая папка-копия — не трогаем
                    tgt = linker.link_target(d)
                    if tgt is not None and _path_within(store_root, tgt):
                        linker.remove_link(d)  # только НАШИ (на стор) ссылки
                        report["pruned"].append(d.name)

        payload: dict = {**report}
        if report["missing"] and not cfg.is_logged_in():
            payload["hint"] = (
                "вы не залогинены — докачка из хаба недоступна; что уже в "
                "сторе — слинковано. Для докачки: skills-hub login"
            )

        def _render(r: dict) -> None:
            console.print(
                f"[green]sync[/] {project_path}: "
                f"+linked {len(r['linked'])}  ↓downloaded {len(r['downloaded'])}  "
                f"-pruned {len(r['pruned'])}  ?missing {len(r['missing'])}"
            )
            for s in r["missing"]:
                console.print(f"  [yellow]✗ не удалось получить:[/] {s}")
            if r.get("hint"):
                console.print(f"  [dim]{r['hint']}[/]")

        emit_data(payload, text_renderer=_render)

    _run(_do())


def cmd_migrate(
    scope: str = typer.Option("all", "--scope", help="all | global | project"),
    project: Optional[Path] = typer.Option(None, "--project"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Показать план, ничего не меняя"),
    agent: Optional[str] = typer.Option(None),
) -> None:
    """Перевести существующие copy-установки на модель стор+ссылка.

    Чужие папки (без _skill_meta.json) и внешние ссылки не трогаются.
    """
    cfg = ClientConfig.load()
    target = get_target(agent or cfg.agent)
    installer = SkillInstaller(target, cfg.effective_store_dir())
    project_path = _resolve_project(cfg, project)

    reports: dict[str, dict] = {}
    if scope in ("global", "all"):
        reports["global"] = installer.migrate_scope(project=None, dry_run=dry_run)
    if scope in ("project", "all"):
        reports["project"] = installer.migrate_scope(project=project_path, dry_run=dry_run)

    # Фикс 5б: мигрированный в project scope навык обязан попасть в
    # .skills-hub/skills.toml — иначе следующий `sync --prune` снимет его
    # ссылку как «не из манифеста» (живой факт).
    manifest_added: list[str] = []
    if not dry_run:
        for name in reports.get("project", {}).get("migrated", []):
            project_manifest.add(project_path, name)
            manifest_added.append(name)

    def _render(payload: dict) -> None:
        prefix = "[yellow]dry-run[/] " if dry_run else ""
        for sc, r in payload["reports"].items():
            console.print(
                f"{prefix}migrate {sc}: "
                f"→стор {len(r['migrated'])}  пропущено(чужое) {len(r['skipped_foreign'])}  "
                f"пропущено(ссылки) {len(r['skipped_linked'])}  ошибок {len(r['failed'])}"
            )
            for name in r["migrated"]:
                console.print(f"  [green]→[/] {name}")
            for f in r["failed"]:
                console.print(f"  [red]✗[/] {f['name']}: {f['error']}")
        if payload["manifest_added"]:
            console.print(
                f"[dim]Дописано в {project_manifest.manifest_path(project_path)}: "
                f"{', '.join(payload['manifest_added'])}[/]"
            )

    emit_data(
        {"dry_run": dry_run, "reports": reports, "manifest_added": manifest_added},
        text_renderer=_render,
    )


def cmd_store_list() -> None:
    """Что лежит в центральном сторе (имя, версия, путь)."""
    cfg = ClientConfig.load()
    store_root = cfg.effective_store_dir()
    items: list[dict] = []
    if store_root.exists():
        for d in sorted(store_root.iterdir(), key=lambda p: p.name):
            if not d.is_dir():
                continue
            meta = read_meta(d) or {}
            items.append({
                "name": d.name, "slug": meta.get("slug"),
                "version": meta.get("version"), "path": str(d),
            })

    def _render(rows: list) -> None:
        if not rows:
            console.print(f"[yellow]Стор пуст[/] ({store_root})")
            return
        table = Table(title=f"Стор ({store_root})")
        table.add_column("навык")
        table.add_column("version")
        table.add_column("path", overflow="fold")
        for s in rows:
            table.add_row(s["name"], s["version"] or "—", s["path"])
        console.print(table)

    emit_data(items, text_renderer=_render)


def cmd_store_path() -> None:
    """Печатает путь центрального стора."""
    cfg = ClientConfig.load()
    emit_data(
        {"store_dir": str(cfg.effective_store_dir())},
        text_renderer=lambda d: console.print(d["store_dir"]),
    )


def cmd_store_gc(
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="[deprecated] Алиас дефолта: только показать кандидатов "
             "(дефолт и так ничего не удаляет).",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="РЕАЛЬНО удалить кандидатов из стора. Без --force gc только "
             "показывает список.",
    ),
) -> None:
    """Показать (и под --force удалить) навыки стора без ссылок в GLOBAL scope.

    По умолчанию НИЧЕГО не удаляет — только список кандидатов (фикс B9:
    дефолтный gc удалял скиллы, на которые ссылались project-junction'ы).

    ВНИМАНИЕ: project-scope ссылки НЕ сканируются (реестр проектов не ведётся) —
    навык, на который ссылается только проект, будет сочтён orphan. Удаление —
    ТОЛЬКО осознанно через --force.
    """
    from skills_hub_cli.core.installer import _force_rmtree

    # Прямые вызовы (тесты/скрипты) могут передать OptionInfo-дефолты typer —
    # они truthy; нормализуем, чтобы это НИКОГДА не включило удаление.
    if not isinstance(dry_run, bool):
        dry_run = False
    if not isinstance(force, bool):
        force = False
    do_delete = force and not dry_run  # явный --dry-run сильнее --force

    cfg = ClientConfig.load()
    target = get_target(cfg.agent)
    store_root = cfg.effective_store_dir()

    referenced: set[str] = set()
    base = target.base_dir()  # global
    if base.exists():
        for d in base.iterdir():
            if linker.is_link(d):
                tgt = linker.link_target(d)
                if tgt is not None:
                    referenced.add(os.path.normcase(str(tgt)))

    candidates: list[str] = []
    if store_root.exists():
        for d in sorted(store_root.iterdir(), key=lambda p: p.name):
            if not d.is_dir():
                continue
            key = os.path.normcase(os.path.abspath(d))
            if key not in referenced:
                candidates.append(d.name)
                if do_delete:
                    _force_rmtree(d)

    def _render(p: dict) -> None:
        verb = "Удалено из стора" if p["deleted"] else "Кандидаты на удаление"
        console.print(f"[yellow]{verb}[/] ({len(p['candidates'])}): {', '.join(p['candidates']) or '—'}")
        if p["deleted"]:
            console.print("[dim]project-scope ссылки не учитывались — проверьте проекты.[/]")
        else:
            console.print(
                "[dim]project-scope ссылки не учитываются; ничего не удалено — "
                "для удаления используйте --force.[/]"
            )

    emit_data(
        {"candidates": candidates, "dry_run": not do_delete, "deleted": do_delete},
        text_renderer=_render,
    )


def cmd_update(
    slug: Optional[str] = typer.Argument(
        None, metavar="[ID_ИЛИ_SLUG]", help="id-или-slug скилла; без аргумента — все"
    ),
    all_: bool = typer.Option(False, "--all"),
    channel: str = typer.Option("published"),
    project: Optional[Path] = typer.Option(None, "--project"),
    scope: Optional[str] = typer.Option(
        None, "--scope", help="global | project | all (default: all)"
    ),
) -> None:
    """Обновить установленные skills (по умолчанию во всех scope: global + project)."""
    cfg = ClientConfig.load()
    target = get_target(cfg.agent)
    wanted_scope = scope or "all"
    actual_project = (
        project
        or (Path(cfg.default_project_dir) if cfg.default_project_dir else None)
        or Path.cwd()
    )

    # Собираем список (ref, project | None) для апдейта. `ref` — id-или-slug,
    # совпадает с именем папки на диске (PK-миграция §3.E: slug может быть None,
    # тогда папка/ref = числовой id).
    targets: list[tuple[str, Path | None]] = []
    if slug and not all_:
        # Если ref передан явно — обновим в указанном scope (или auto-detect)
        if wanted_scope in ("global", "all"):
            if target.slug_dir(slug).exists():
                targets.append((slug, None))
        if wanted_scope in ("project", "all"):
            if target.slug_dir(slug, project=actual_project).exists():
                targets.append((slug, actual_project))
    else:
        if wanted_scope in ("global", "all"):
            for s in _scan_installed(target, project=None):
                targets.append((s["ref"], None))
        if wanted_scope in ("project", "all"):
            for s in _scan_installed(target, project=actual_project):
                targets.append((s["ref"], actual_project))

    if not targets:
        emit_data(
            [],
            text_renderer=lambda _: console.print("[yellow]Нечего обновлять[/]"),
        )
        return
    access = _get_access_token()

    async def _do() -> None:
        client = HubClient(
            base_url=cfg.base_url,
            access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        results: list[dict] = []
        try:
            installer = SkillInstaller(target, cfg.effective_store_dir())
            for ref, proj in targets:
                meta = read_meta(target.slug_dir(ref, project=proj))
                current_version = (meta or {}).get("version", "0.0.0")
                # На диске identity = slug ?? skill_id; reconstruct, чтобы папка
                # совпала (slug-less skill хранится под id).
                meta_slug = (meta or {}).get("slug")
                meta_skill_id = (meta or {}).get("skill_id")
                bundle = await client.install_bundle(ref, channel=channel)
                scope_label = "project" if proj else "global"
                if bundle["version"] == current_version:
                    results.append(
                        {
                            "slug": meta_slug,
                            "skill_id": meta_skill_id,
                            "ref": ref,
                            "scope": scope_label,
                            "project": str(proj) if proj else None,
                            "from": current_version,
                            "to": current_version,
                            "updated": False,
                        }
                    )
                    continue
                up = installer.install(
                    slug=meta_slug,
                    skill_id=meta_skill_id,
                    version=bundle["version"],
                    commit_sha=bundle["commit_sha"],
                    repo_url=bundle.get("repo_url"),
                    manifest=bundle["manifest"],
                    project=proj,
                )
                results.append(
                    {
                        "slug": meta_slug,
                        "skill_id": up.skill_id or meta_skill_id,
                        "ref": ref,
                        "scope": scope_label,
                        "project": str(proj) if proj else None,
                        "from": current_version,
                        "to": bundle["version"],
                        "updated": True,
                        "diff": up.update_diff,
                    }
                )
                # E23: track skill.update event.
                track_skill_event(
                    "skill.update",
                    slug=ref,
                    version=bundle["version"],
                    scope=scope_label,
                )
        finally:
            await client.close()
        cfg.last_auto_update_at = datetime.now(UTC).isoformat()
        cfg.save()

        def _render(rows: list) -> None:
            for r in rows:
                # slug может быть None у slug-less skill — показываем ref (id).
                label = r.get("slug") or r.get("ref")
                if r["updated"]:
                    d = r.get("diff")
                    diff_suffix = ""
                    if d:
                        diff_suffix = (
                            f" [dim](+{d['added']} ~{d['changed']} -{d['removed']})[/]"
                        )
                    console.print(
                        f"[green]↑[/] {label} ({r['scope']}): "
                        f"{r['from']} → {r['to']}{diff_suffix}"
                    )
                else:
                    console.print(
                        f"[dim]= {label}@{r['from']} ({r['scope']}, актуально)[/]"
                    )

        emit_data(results, text_renderer=_render)

    _run(_do())


def cmd_remove(
    slug: str = typer.Argument(
        ...,
        metavar="ID_ИЛИ_SLUG",
        help="id-или-slug установленного скилла (= имя папки на диске)",
    ),
    scope: Optional[str] = typer.Option(
        None, "--scope", help="global | project (default из config.default_install_scope)"
    ),
    project: Optional[Path] = typer.Option(
        None, "--project", help="Если scope=project — путь к корню проекта (default: cwd)"
    ),
    keep_local: bool = typer.Option(
        False, "--keep-local",
        help="Сохранить _local/ и прочие preserved_paths (пользовательский state).",
    ),
    purge: bool = typer.Option(
        False, "--purge",
        help="Удалить навык и из центрального стора (а не только ссылку из scope).",
    ),
    agent: Optional[str] = typer.Option(None),
) -> None:
    """Удалить установленный skill (global или project scope).

    Аргумент — id-или-slug, совпадает с именем папки на диске (slug, либо
    числовой id для slug-less skill). По умолчанию удаляет всю папку.
    `--keep-local` сохраняет preserved-пути (`_local/`, `browser_profiles/`,
    ...) — например, чтобы не потерять накопленный state при переустановке.
    `--purge` дополнительно удаляет навык из центрального стора.
    """
    cfg = ClientConfig.load()
    actual_scope, project_path = _resolve_install_scope(cfg, scope, project)
    _ = actual_scope  # передаётся через project_path
    target = get_target(agent or cfg.agent)
    installer = SkillInstaller(target, cfg.effective_store_dir())
    result = installer.remove(
        slug=slug, project=project_path, keep_local=keep_local, purge=purge
    )

    # Фикс 5а: симметрия с disable — снятый из project scope навык убираем и
    # из .skills-hub/skills.toml, иначе следующий sync вернёт его обратно.
    manifest_removed = False
    if project_path is not None:
        manifest_removed = project_manifest.remove(project_path, slug)

    if not result.removed:
        emit_data(
            {
                "slug": slug,
                "scope": result.scope,
                "removed": False,
                "kept_local": False,
                "purged": result.purged,
                "manifest_removed": manifest_removed,
                "path": str(result.target_dir),
            },
            text_renderer=lambda _: console.print(
                f"[yellow]Не установлен[/] ({result.scope}): {slug} "
                f"(нет папки {result.target_dir})"
            ),
        )
        return

    # E23/E46: telemetry — silent track skill.uninstall event.
    track_skill_event(
        "skill.uninstall",
        slug=slug,
        scope=result.scope,
        extra={"kept_local": result.kept_local},
    )

    def _render(_: dict) -> None:
        if result.kept_local:
            console.print(
                f"[green]✓[/] Удалён ({result.scope}): {slug} "
                f"[dim](preserved_paths сохранены в {result.target_dir})[/]"
            )
        else:
            console.print(f"[green]✓[/] Удалён ({result.scope}): {slug}")
        if manifest_removed:
            console.print("[dim]Убран из .skills-hub/skills.toml[/]")

    emit_data(
        {
            "slug": slug,
            "scope": result.scope,
            "removed": True,
            "kept_local": result.kept_local,
            "purged": result.purged,
            "manifest_removed": manifest_removed,
            "path": str(result.target_dir),
        },
        text_renderer=_render,
    )


def cmd_report(
    slug: str = typer.Argument(
        ..., metavar="ID_ИЛИ_SLUG", help="id-или-slug скилла (backend принимает оба)"
    ),
    kind: str = typer.Option("bug"),
    title: str = typer.Option(...),
    description: str = typer.Option(...),
    version: Optional[str] = typer.Option(None),
) -> None:
    """Отправить bug-report / feature-request создателю skill'а."""
    cfg = ClientConfig.load()
    access = _get_access_token()

    async def _do() -> None:
        client = HubClient(
            base_url=cfg.base_url,
            access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        try:
            result = await client.submit_issue(
                slug, kind=kind, title=title, description=description, version=version
            )
        finally:
            await client.close()
        emit_data(
            result,
            text_renderer=lambda r: console.print(
                f"[green]✓[/] Issue: {r['issue_id']}"
            ),
        )

    _run(_do())


def _run_publish_secret_scan(
    skill_dir: Path, *, force: bool, strict: bool
) -> None:
    """ТЗ §10: скан секретов перед publish.

    - находки → печать (file:line + rule, маскированный snippet) + abort,
      кроме `--force` (тогда warning + продолжаем);
    - gitleaks нет → warning «regex fallback», в `--strict` — fatal.
    Печатает в text-режиме через rich, в json-режиме — structured в stderr.
    """
    result = secret_scan_dir(skill_dir)

    # gitleaks отсутствует → деградация на regex.
    if not result.gitleaks_available:
        if strict:
            emit_error(
                "secret_scan_strict",
                "gitleaks не установлен, а указан --strict — abort.",
                backend=result.backend,
            )
            raise typer.Exit(1)
        emit_message(
            "secret-scan via regex fallback (gitleaks not installed)",
            level="warn",
            backend=result.backend,
        )

    if not result.findings:
        emit_message(
            f"secret-scan: чисто ({result.backend}, 0 находок)",
            level="info",
            backend=result.backend,
            findings=0,
        )
        return

    # Есть находки — печатаем (маскированно) и решаем abort/override.
    findings_payload = [
        {"file": f.file, "line": f.line, "rule": f.rule, "snippet": f.snippet}
        for f in result.findings
    ]
    if is_json():
        emit_error(
            "secret_scan_failed" if not force else "secret_scan_override",
            f"Обнаружено секретов: {len(result.findings)}",
            backend=result.backend,
            forced=force,
            findings=findings_payload,
        )
    else:
        console.print(
            f"[red]✗ secret-scan ({result.backend}): "
            f"найдено {len(result.findings)} потенциальных секрет(ов)[/]"
        )
        for f in result.findings:
            console.print(
                f"  [yellow]{f.file}:{f.line}[/] "
                f"[dim]({f.rule})[/] {f.snippet}"
            )

    if force:
        emit_message(
            "publish продолжен несмотря на находки (--force)",
            level="warn",
            forced=True,
        )
        return

    if not is_json():
        console.print(
            "[red]Publish прерван.[/] Удалите секреты или используйте "
            "[bold]--force[/] для override."
        )
    raise typer.Exit(1)


def cmd_publish(
    slug: str = typer.Argument(
        ...,
        metavar="ID_ИЛИ_SLUG",
        help="id-или-slug скилла (backend принимает оба; slug при создании задаёт hub-admin)",
    ),
    tag: str = typer.Option(..., "--tag"),
    path: Optional[Path] = typer.Option(None, "--path"),
    channel: str = typer.Option("published"),
    title: Optional[str] = typer.Option(None),
    description: Optional[str] = typer.Option(None),
    tags: Optional[str] = typer.Option(None),
    repo_url: Optional[str] = typer.Option(None),
    is_super: bool = typer.Option(False),
    commit_sha: Optional[str] = typer.Option(None, "--commit-sha"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    force: bool = typer.Option(
        False, "--force",
        help="Опубликовать несмотря на найденные секреты (с warning).",
    ),
    strict: bool = typer.Option(
        False, "--strict",
        help="Считать отсутствие gitleaks fatal (а не warning).",
    ),
    skip_secret_scan: bool = typer.Option(
        False, "--skip-secret-scan",
        help="Полностью пропустить скан секретов (не рекомендуется).",
    ),
) -> None:
    """Опубликовать новую версию skill'а (skill.publish required).

    ТЗ §10: перед сборкой manifest скан секретов (gitleaks --no-git +
    regex-fallback). Находки → abort (exit 1), `--force` для override.
    gitleaks нет → warning; `--strict` делает это fatal.
    """
    cfg = ClientConfig.load()
    access = _get_access_token()
    target = get_target(cfg.agent)
    skill_dir = path or target.slug_dir(slug)
    if not skill_dir.exists():
        console.print(f"[red]Папка skill не найдена:[/] {skill_dir}")
        raise typer.Exit(1)

    # ТЗ §10: secret-scan ПЕРЕД сборкой/отправкой.
    if not skip_secret_scan:
        _run_publish_secret_scan(skill_dir, force=force, strict=strict)

    version = tag.lstrip("v")
    manifest = build_manifest(skill_dir, version=version)
    actual_commit = commit_sha or git_commit_sha(skill_dir) or ("0" * 7)
    payload = {
        "slug": slug,
        "title": title or manifest.description.split("\n", 1)[0][:255] or slug,
        "description": description or manifest.description or slug,
        "semver": version,
        "channel": channel,
        "commit_sha": actual_commit,
        "tags": (
            [t.strip() for t in tags.split(",") if t.strip()]
            if tags else manifest.tags
        ),
        "repo_url": repo_url,
        "is_super": is_super,
        "manifest": {
            "version": manifest.version,
            "description": manifest.description,
            "triggers": manifest.triggers,
            "tags": manifest.tags,
            "files": manifest.files,
            "dependencies": manifest.dependencies,
            "preserved_paths": manifest.preserved_paths,
        },
    }
    if dry_run:
        emit_data(
            {"event": "publish_dry_run", "files_count": len(manifest.files), "payload": payload},
            text_renderer=lambda p: (
                console.print(f"[yellow]Dry-run[/]: {p['files_count']} файлов"),
                console.print(RichJSON.from_data(p["payload"])),
            ),
        )
        return

    async def _do() -> None:
        client = HubClient(
            base_url=cfg.base_url,
            access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        try:
            result = await client.publish_skill(payload)
        finally:
            await client.close()
        emit_data(
            {**result, "files_count": len(manifest.files), "slug": slug, "version": version},
            text_renderer=lambda r: console.print(
                f"[green]✓[/] {r['slug']}@{r['version']} опубликован "
                f"(is_new={r['is_new_skill']}, files={r['files_count']})"
            ),
        )

    _run(_do())


def cmd_admin_sync(
    slug: str = typer.Argument(
        ..., metavar="ID_ИЛИ_SLUG", help="id-или-slug скилла (backend принимает оба)"
    ),
    channel: str = typer.Option("published"),
) -> None:
    """[hub.admin] Backend сам подтягивает новые GitLab tags (по id-или-slug)."""
    cfg = ClientConfig.load()
    access = _get_access_token()

    async def _do() -> None:
        client = HubClient(
            base_url=cfg.base_url,
            access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        try:
            r = await client.sync_skill(slug, channel=channel)
        finally:
            await client.close()

        def _render(p: dict) -> None:
            console.print(f"[green]✓[/] Sync {p['skill_slug']}:")
            console.print(f"  Новые:        {p['new_versions'] or '—'}")
            console.print(f"  Существовали: {p['existing_versions'] or '—'}")

        emit_data(r, text_renderer=_render)

    _run(_do())


def cmd_admin_company_create(
    name: str = typer.Option(...),
    owner_email: str = typer.Option(..., "--owner-email"),
    owner_name: str = typer.Option(..., "--owner-name"),
    slug: Optional[str] = typer.Option(
        None,
        "--slug",
        help="Slug компании (требует hub.slug_manage; опусти → backend создаст slug-less)",
    ),
) -> None:
    """[hub.company_create] Создать новую компанию + invite owner'у."""
    cfg = ClientConfig.load()
    access = _get_access_token()

    async def _do() -> None:
        client = HubClient(
            base_url=cfg.base_url,
            access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        payload: dict[str, object] = {
            "name": name,
            "owner_email": owner_email,
            "owner_display_name": owner_name,
        }
        if slug:
            payload["slug"] = slug  # пустой slug не шлём → backend сделает slug-less
        try:
            r = await client.create_company(payload)
        finally:
            await client.close()

        def _render(p: dict) -> None:
            label = slug or p.get("company_id")
            console.print(f"[green]✓[/] Компания {label} (id={p['company_id']})")
            console.print(f"  Owner invite: {p['owner_invite_token']}")
            console.print(f"  URL:          {p['owner_invite_url']}")

        emit_data(r, text_renderer=_render)

    _run(_do())


def cmd_admin_invite(
    company_id: str = typer.Option(..., "--company-id"),
    role_id: str = typer.Option(..., "--role-id"),
    email: Optional[str] = typer.Option(
        None, "--email", help="Email приглашаемого (pre-emptive User+Membership)"
    ),
    name: Optional[str] = typer.Option(
        None, "--name", help="Display-name приглашаемого (вместе с --email)"
    ),
) -> None:
    """[invite.manage] Выдать invite member/manager'у в свою компанию.

    Flat POST /invites (E1): nested /companies/{id}/invites удалён. ``--email``
    + ``--name`` опциональны — если заданы, backend сразу заводит
    User(invited)+Membership (invitee виден в списке пользователей).
    """
    cfg = ClientConfig.load()
    access = _get_access_token()

    async def _do() -> None:
        client = HubClient(
            base_url=cfg.base_url,
            access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        try:
            r = await client.issue_invite(
                company_id, role_id, email=email, display_name=name
            )
        finally:
            await client.close()

        def _render(p: dict) -> None:
            console.print(f"[green]✓[/] Invite: {p['invite_token']}")
            console.print(f"  URL: {p['invite_url']}")

        emit_data(r, text_renderer=_render)

    _run(_do())


def cmd_config(
    output: Optional[str] = typer.Option(
        None, "--output", help="text|json — формат вывода по умолчанию"
    ),
    auto_update: Optional[bool] = typer.Option(
        None, "--auto-update/--no-auto-update", help="Тихо обновлять навыки"
    ),
    auto_update_cooldown_min: Optional[int] = typer.Option(
        None, "--auto-update-cooldown-min", help="Минут между фон-проверками"
    ),
    install_scope: Optional[str] = typer.Option(
        None, "--install-scope", help="global|project — куда install ставит skill"
    ),
    project_dir: Optional[Path] = typer.Option(
        None, "--project-dir", help="Дефолтный project root для scope=project"
    ),
    show: bool = typer.Option(False, "--show", help="Просто показать текущий config"),
) -> None:
    """Настройки CLI (output, auto-update, default install scope/project)."""
    cfg = ClientConfig.load()
    changes = False
    if output is not None:
        if output not in ("text", "json"):
            emit_error("VALIDATION", "output должен быть 'text' или 'json'")
            raise typer.Exit(1)
        cfg.output_format = output
        changes = True
    if auto_update is not None:
        cfg.auto_update = auto_update
        changes = True
    if auto_update_cooldown_min is not None:
        cfg.auto_update_cooldown_min = auto_update_cooldown_min
        changes = True
    if install_scope is not None:
        if install_scope not in ("global", "project"):
            emit_error("VALIDATION", "install-scope должен быть 'global' или 'project'")
            raise typer.Exit(1)
        cfg.default_install_scope = install_scope
        changes = True
    if project_dir is not None:
        cfg.default_project_dir = str(project_dir.resolve())
        changes = True
    cfg.save()
    payload = {
        "output_format": cfg.output_format,
        "auto_update": cfg.auto_update,
        "auto_update_cooldown_min": cfg.auto_update_cooldown_min,
        "default_install_scope": cfg.default_install_scope,
        "default_project_dir": cfg.default_project_dir,
    }

    def _render(p: dict) -> None:
        console.print(f"  output_format:            {p['output_format']}")
        console.print(f"  auto_update:              {p['auto_update']}")
        console.print(f"  auto_update_cooldown_min: {p['auto_update_cooldown_min']}")
        console.print(f"  default_install_scope:    {p['default_install_scope']}")
        console.print(f"  default_project_dir:      {p['default_project_dir'] or '—'}")
        if not show and changes:
            console.print("[green]✓[/] Config обновлён")

    emit_data(payload, text_renderer=_render)


# ======================================================
#                  BUILD APP DYNAMICALLY
# ======================================================
def _version_callback(value: bool) -> None:
    """Eager-callback для глобального ``--version``: печать версии + выход.

    ``__version__`` живёт в ``skills_hub_cli/__init__.py`` — пробрасываем его
    в typer (раньше флаг отсутствовал). Печать уважает json-режим.
    """
    if not value:
        return
    from skills_hub_cli import __version__

    emit_data(
        {"version": __version__},
        text_renderer=lambda p: console.print(p["version"]),
    )
    raise typer.Exit()


def build_app() -> typer.Typer:
    cfg = ClientConfig.load()
    is_logged_in = cfg.is_logged_in()

    description_lines = ["Skills Hub CLI"]
    if is_logged_in:
        is_hub_admin = cfg.is_hub_admin()
        is_skill_creator = cfg.is_skill_creator()
        roles = [
            r for r in (
                "hub-admin" if is_hub_admin else None,
                "skill-creator" if is_skill_creator else None,
                "member" if not (is_hub_admin or is_skill_creator) else None,
            ) if r
        ]
        description_lines.append(
            f"Вы вошли как [bold]{cfg.user_email}[/] ({', '.join(roles)})."
        )
    else:
        description_lines.append("[dim]Не авторизован — доступна только login.[/]")

    app = typer.Typer(
        no_args_is_help=True,
        add_completion=True,
        help="\n".join(description_lines),
        rich_markup_mode="rich",
    )

    @app.callback()
    def _root(
        profile: Optional[str] = typer.Option(
            None, "--profile", "-P", help="Использовать профиль (admin/test/...)"
        ),
        json_output: bool = typer.Option(
            False, "--json", "-J",
            help="Вывод в JSON (для AI-агентов и скриптов). По умолчанию из config.output_format.",
        ),
        version: bool = typer.Option(
            False, "--version", "-V",
            help="Показать версию skills-hub CLI и выйти.",
            callback=_version_callback,
            is_eager=True,
        ),
    ) -> None:
        """Корневой callback (профиль + json считаны до построения app)."""
        _ = profile, json_output, version

    # === Always-on ===
    app.command(name="login")(cmd_login)
    app.command(name="set-tokens", hidden=True)(cmd_set_tokens)
    app.command(name="status")(cmd_status)
    app.command(name="logout")(cmd_logout)
    app.command(name="whoami")(cmd_whoami)
    app.command(name="config")(cmd_config)
    app.command(name="web")(cmd_web)
    # install — ALWAYS-ON: автономные источники (--path/--from-git) работают без
    # login; hub-режим (без этих флагов) внутри cmd_install сам требует токен.
    app.command(name="install")(cmd_install)
    # lifecycle — ALWAYS-ON (фикс 3): enable/disable/remove/sync/migrate/store
    # работают с ЛОКАЛЬНЫМ стором без login. Сеть нужна только sync-докачке и
    # hub-enable — эти ветки сами отвечают NOT_LOGGED_IN / missing-подсказкой.
    app.command(name="enable")(cmd_enable)
    app.command(name="disable")(cmd_disable)
    app.command(name="remove")(cmd_remove)
    app.command(name="sync")(cmd_sync)
    app.command(name="migrate")(cmd_migrate)
    store_app = typer.Typer(no_args_is_help=True, help="Центральный стор навыков")
    app.add_typer(store_app, name="store")
    store_app.command("list")(cmd_store_list)
    store_app.command("path")(cmd_store_path)
    store_app.command("gc")(cmd_store_gc)

    if not is_logged_in:
        return app

    # === Requires authenticated session ===
    app.command(name="passwd")(cmd_passwd)

    # === Permission-gated user commands ===
    if cfg.has_permission("skill.read"):
        app.command(name="list")(cmd_list)
        app.command(name="show")(cmd_show)
        # E23 — collections: read-only (skill.read) + bulk-install под
        # skill.install (`collection install` ставит все навыки коллекции).
        from skills_hub_cli.commands import collection as _coll_mod

        _coll_mod.register(app, can_install=cfg.has_permission("skill.install"))
        # E23 — contributors (public-аналог skill.read).
        from skills_hub_cli.commands import contrib as _contrib_mod

        _contrib_mod.register(app)
        # E23 — comments list (public via skill.read).
        from skills_hub_cli.commands import comment as _comment_mod

        # post-команду регистрируем отдельно ниже (нужен comment.post).
        app.command(name="comments")(_comment_mod.cmd_comments_list)
    if cfg.has_permission("skill.install"):
        # install зарегистрирован в always-on блоке (см. выше): автономные
        # источники --path/--from-git не требуют login.
        # enable/disable/remove/sync/migrate/store(list/path/gc) — тоже в
        # always-on блоке (фикс 3): lifecycle локального стора живёт без login.
        app.command(name="update")(cmd_update)
    if cfg.has_permission("skill.report_issue"):
        app.command(name="report")(cmd_report)

    # === E23 — Skill review: ratings + comments post ===
    if cfg.has_permission("skill.rate"):
        from skills_hub_cli.commands import rate as _rate_mod

        _rate_mod.register(app)
    if cfg.has_permission("comment.post"):
        from skills_hub_cli.commands import comment as _comment_mod

        app.command(name="comment")(_comment_mod.cmd_comment_post)
    # comment-edit / comment-delete — независимые per-permission гейты
    # (PATCH/DELETE /comments/{id} адресуют comment по числовому id).
    if cfg.has_permission("comment.edit_own"):
        from skills_hub_cli.commands import comment as _comment_mod

        app.command(name="comment-edit")(_comment_mod.cmd_comment_edit)
    if cfg.has_permission("comment.delete_own"):
        from skills_hub_cli.commands import comment as _comment_mod

        app.command(name="comment-delete")(_comment_mod.cmd_comment_delete)

    # === E23 — Support tickets ===
    if cfg.has_permission("ticket.create") or cfg.has_permission("ticket.read"):
        from skills_hub_cli.commands import ticket as _ticket_mod

        # `status` (PATCH) — под ticket.update_status; reply — под ticket.create.
        _ticket_mod.register_ticket(
            app, can_update=cfg.has_permission("ticket.update_status")
        )

    # === E23 — Event tracking + daemon (always-on для залогиненного user'а) ===
    from skills_hub_cli.commands import daemon as _daemon_mod
    from skills_hub_cli.commands import event as _event_mod

    _event_mod.register(app)
    _daemon_mod.register(app)

    # === Creator ===
    if cfg.has_permission("skill.publish"):
        app.command(name="publish")(cmd_publish)

    # === Admin sub-app (если есть хотя бы одно admin-право) ===
    can_sync = cfg.has_permission("hub.admin")
    can_company_create = cfg.has_permission("hub.company_create")
    can_invite = cfg.has_permission("invite.manage") or cfg.is_hub_admin()
    if can_sync or can_company_create or can_invite:
        admin_app = typer.Typer(
            no_args_is_help=True,
            help="Admin команды (зависят от ваших прав)",
        )
        app.add_typer(admin_app, name="admin")
        if can_sync:
            admin_app.command("sync-skill")(cmd_admin_sync)
        if can_company_create:
            admin_app.command("company-create")(cmd_admin_company_create)
        if can_invite:
            admin_app.command("invite")(cmd_admin_invite)

    return app


app = build_app()


if __name__ == "__main__":
    app()
