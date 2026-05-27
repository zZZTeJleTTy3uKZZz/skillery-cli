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
from skills_hub_cli.core.agents import detect_agent, get_target
from skills_hub_cli.core.installer import SkillInstaller, read_meta
from skills_hub_cli.core.manifest_builder import build_manifest, git_commit_sha
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
    try:
        asyncio.run(coro)
    except ApiError as e:
        console.print(f"[red]Ошибка API:[/] {e}")
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

    async def _do() -> None:
        client = HubClient(base_url=cfg.base_url, access_token=access, on_token_refresh=_make_refresh_callback(cfg))
        try:
            updated_any = False
            for slug, current in installed:
                try:
                    bundle = await client.install_bundle(slug)
                except Exception:
                    continue
                if bundle["version"] != current:
                    installer = SkillInstaller(target)
                    installer.install(
                        slug=slug,
                        version=bundle["version"],
                        commit_sha=bundle["commit_sha"],
                        repo_url=bundle.get("repo_url"),
                        manifest=bundle["manifest"],
                    )
                    console.print(
                        f"[dim cyan]↑ auto-update[/] {slug}: {current} → {bundle['version']}"
                    )
                    updated_any = True
            if updated_any or True:
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
            "is_hub_admin": cfg.is_hub_admin,
            "is_skill_creator": cfg.is_skill_creator,
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
            if cfg.is_hub_admin:
                roles_descr.append("hub-admin")
            if cfg.is_skill_creator:
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
            "is_hub_admin": cfg.is_hub_admin,
            "is_skill_creator": cfg.is_skill_creator,
            "permissions": cfg.permissions,
            "company_id": cfg.company_id,
            "role_id": cfg.role_id,
            "access_expires_at": cfg.access_expires_at,
        }

        def _render(_: dict) -> None:
            console.print(f"[green]✓[/] Авторизован как {email} (password)")
            roles_descr = []
            if cfg.is_hub_admin:
                roles_descr.append("hub-admin")
            if cfg.is_skill_creator:
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
    cfg.is_hub_admin = False
    cfg.is_skill_creator = False
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
        "is_hub_admin": cfg.is_hub_admin,
        "is_skill_creator": cfg.is_skill_creator,
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
            roles.append(f"company={p['company_id'][:8]}…")
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
    """Открыть Web UI в браузере с автоматической авторизацией (handoff из CLI)."""
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
    target_url = f"{cfg.effective_web_ui_url().rstrip('/')}/login?code={code}"

    emit_data(
        {
            "url": target_url,
            "code": code,
            "expires_at": expires_at,
            "opened_browser": not no_browser,
        },
        text_renderer=lambda _: _render_web_text(target_url, expires_at, no_browser),
    )

    if not no_browser:
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
    """Сканирует папку (global или project) и возвращает meta-инфу установленных skills."""
    base = target.base_dir(project=project)
    items: list[dict] = []
    if base.exists():
        for d in base.iterdir():
            meta = read_meta(d)
            if meta:
                items.append(
                    {
                        "slug": meta["slug"],
                        "version": meta.get("version"),
                        "commit_sha": meta.get("commit_sha"),
                        "agent": meta.get("agent"),
                        "scope": meta.get("scope") or ("project" if project else "global"),
                        "project": meta.get("project") or (str(project) if project else None),
                        "path": str(d),
                    }
                )
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
            table.add_column("slug")
            table.add_column("version")
            table.add_column("scope")
            table.add_column("path", overflow="fold")
            for s in rows:
                table.add_row(
                    s["slug"],
                    s["version"] or "—",
                    s["scope"],
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


def cmd_show(slug: str) -> None:
    """Детали skill'а."""
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


def cmd_install(
    slug: str,
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
) -> None:
    """Установить skill (global или в конкретный project).

    global  → ~/.claude/skills/<slug>/  (видны во всех Claude Code сессиях)
    project → <project>/.claude/skills/<slug>/  (только в данном проекте)
    """
    cfg = ClientConfig.load()
    actual_scope, project_path = _resolve_install_scope(cfg, scope, project)
    _maybe_auto_update(cfg)
    access = _get_access_token()
    target = get_target(agent or cfg.agent)
    _ = actual_scope  # передаётся через project_path

    async def _do() -> None:
        client = HubClient(
            base_url=cfg.base_url,
            access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        try:
            bundle = await client.install_bundle(slug, channel=channel)
        finally:
            await client.close()
        installer = SkillInstaller(target)
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
                    base_url=cfg.base_url,
                    access_token=access,
                    on_token_refresh=_make_refresh_callback(cfg),
                )
                try:
                    dep_bundle = await sub.install_bundle(dep_slug, channel=channel)
                finally:
                    await sub.close()
            result = installer.install(
                slug=dep_slug,
                version=dep_version,
                commit_sha=dep_bundle["commit_sha"],
                repo_url=dep_repo or dep_bundle.get("repo_url"),
                manifest=dep_bundle["manifest"],
                project=project_path,
                force=force,
            )
            installed_chain.append(
                {
                    "slug": dep_slug,
                    "version": dep_version,
                    "is_update": result.is_update,
                    "target_dir": str(result.target_dir),
                    "scope": result.scope,
                }
            )

        def _render(items: list) -> None:
            for item in items:
                action = "Обновлён" if item["is_update"] else "Установлен"
                console.print(
                    f"[green]✓[/] {action} ({item['scope']}): "
                    f"{item['slug']}@{item['version']} → {item['target_dir']}"
                )

        emit_data(installed_chain, text_renderer=_render)

    _run(_do())


def cmd_update(
    slug: Optional[str] = typer.Argument(None),
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

    # Собираем список (slug, project | None) для апдейта
    targets: list[tuple[str, Path | None]] = []
    if slug and not all_:
        # Если slug передан явно — обновим в указанном scope (или auto-detect)
        if wanted_scope in ("global", "all"):
            if target.slug_dir(slug).exists():
                targets.append((slug, None))
        if wanted_scope in ("project", "all"):
            if target.slug_dir(slug, project=actual_project).exists():
                targets.append((slug, actual_project))
    else:
        if wanted_scope in ("global", "all"):
            for s in _scan_installed(target, project=None):
                targets.append((s["slug"], None))
        if wanted_scope in ("project", "all"):
            for s in _scan_installed(target, project=actual_project):
                targets.append((s["slug"], actual_project))

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
            installer = SkillInstaller(target)
            for s, proj in targets:
                meta = read_meta(target.slug_dir(s, project=proj))
                current_version = (meta or {}).get("version", "0.0.0")
                bundle = await client.install_bundle(s, channel=channel)
                scope_label = "project" if proj else "global"
                if bundle["version"] == current_version:
                    results.append(
                        {
                            "slug": s,
                            "scope": scope_label,
                            "project": str(proj) if proj else None,
                            "from": current_version,
                            "to": current_version,
                            "updated": False,
                        }
                    )
                    continue
                installer.install(
                    slug=s,
                    version=bundle["version"],
                    commit_sha=bundle["commit_sha"],
                    repo_url=bundle.get("repo_url"),
                    manifest=bundle["manifest"],
                    project=proj,
                )
                results.append(
                    {
                        "slug": s,
                        "scope": scope_label,
                        "project": str(proj) if proj else None,
                        "from": current_version,
                        "to": bundle["version"],
                        "updated": True,
                    }
                )
        finally:
            await client.close()
        cfg.last_auto_update_at = datetime.now(UTC).isoformat()
        cfg.save()

        def _render(rows: list) -> None:
            for r in rows:
                if r["updated"]:
                    console.print(
                        f"[green]↑[/] {r['slug']} ({r['scope']}): "
                        f"{r['from']} → {r['to']}"
                    )
                else:
                    console.print(
                        f"[dim]= {r['slug']}@{r['from']} ({r['scope']}, актуально)[/]"
                    )

        emit_data(results, text_renderer=_render)

    _run(_do())


def cmd_report(
    slug: str,
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


def cmd_publish(
    slug: str,
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
) -> None:
    """Опубликовать новую версию skill'а (skill.publish required)."""
    cfg = ClientConfig.load()
    access = _get_access_token()
    target = get_target(cfg.agent)
    skill_dir = path or target.slug_dir(slug)
    if not skill_dir.exists():
        console.print(f"[red]Папка skill не найдена:[/] {skill_dir}")
        raise typer.Exit(1)
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
    slug: str,
    channel: str = typer.Option("published"),
) -> None:
    """[hub.admin] Backend сам подтягивает новые GitLab tags."""
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
    slug: str,
    name: str = typer.Option(...),
    owner_email: str = typer.Option(..., "--owner-email"),
    owner_name: str = typer.Option(..., "--owner-name"),
    max_users: int = typer.Option(5, "--max-users"),
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
        try:
            r = await client.create_company(
                {
                    "slug": slug, "name": name,
                    "owner_email": owner_email, "owner_display_name": owner_name,
                    "max_users": max_users,
                }
            )
        finally:
            await client.close()

        def _render(p: dict) -> None:
            console.print(f"[green]✓[/] Компания {slug} (id={p['company_id']})")
            console.print(f"  Owner invite: {p['owner_invite_token']}")
            console.print(f"  URL:          {p['owner_invite_url']}")

        emit_data(r, text_renderer=_render)

    _run(_do())


def cmd_admin_invite(
    company_id: str = typer.Option(..., "--company-id"),
    role_id: str = typer.Option(..., "--role-id"),
    group_ids: Optional[str] = typer.Option(None, "--groups"),
) -> None:
    """[invite.manage] Выдать invite member/manager'у в свою компанию."""
    cfg = ClientConfig.load()
    access = _get_access_token()
    groups_list = (
        [g.strip() for g in group_ids.split(",") if g.strip()] if group_ids else []
    )

    async def _do() -> None:
        client = HubClient(
            base_url=cfg.base_url,
            access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        try:
            r = await client.issue_invite(company_id, role_id, groups_list)
        finally:
            await client.close()

        def _render(p: dict) -> None:
            console.print(f"[green]✓[/] Invite: {p['token']}")
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
def build_app() -> typer.Typer:
    cfg = ClientConfig.load()
    is_logged_in = cfg.is_logged_in()

    description_lines = ["Skills Hub CLI"]
    if is_logged_in:
        roles = [
            r for r in (
                "hub-admin" if cfg.is_hub_admin else None,
                "skill-creator" if cfg.is_skill_creator else None,
                "member" if not (cfg.is_hub_admin or cfg.is_skill_creator) else None,
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
    ) -> None:
        """Корневой callback (профиль + json считаны до построения app)."""
        _ = profile, json_output

    # === Always-on ===
    app.command(name="login")(cmd_login)
    app.command(name="set-tokens", hidden=True)(cmd_set_tokens)
    app.command(name="status")(cmd_status)
    app.command(name="logout")(cmd_logout)
    app.command(name="whoami")(cmd_whoami)
    app.command(name="config")(cmd_config)
    app.command(name="web")(cmd_web)

    if not is_logged_in:
        return app

    # === Requires authenticated session ===
    app.command(name="passwd")(cmd_passwd)

    # === Permission-gated user commands ===
    if cfg.has_permission("skill.read"):
        app.command(name="list")(cmd_list)
        app.command(name="show")(cmd_show)
    if cfg.has_permission("skill.install"):
        app.command(name="install")(cmd_install)
        app.command(name="update")(cmd_update)
    if cfg.has_permission("skill.report_issue"):
        app.command(name="report")(cmd_report)

    # === Creator ===
    if cfg.has_permission("skill.publish"):
        app.command(name="publish")(cmd_publish)

    # === Admin sub-app (если есть хотя бы одно admin-право) ===
    can_sync = cfg.has_permission("hub.admin")
    can_company_create = cfg.has_permission("hub.company_create")
    can_invite = cfg.has_permission("invite.manage") or cfg.is_hub_admin
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
