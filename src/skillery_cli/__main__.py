"""Skillery CLI — модульный typer entrypoint.

Команды видны в --help только если у залогиненного пользователя есть
соответствующий permission в JWT (получен от backend при login).

Базовый набор (без auth): login, set-tokens, status.
После login → добавляются list/show/install/update/report (по permissions).
Для skill-creator → publish.
Для hub-admin / company-admin → admin sub-app с подкомандами.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
import time
import weakref
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import typer
from clikit.command_kit import build_root_app, gated
from rich.console import Console
from rich.json import JSON as RichJSON
from rich.prompt import Prompt
from rich.table import Table

from skillery_cli.config import (
    ClientConfig,
    clear_tokens,
    decode_jwt_claims,
    load_tokens,
    populate_from_jwt,
    save_tokens,
    set_active_profile,
)
from skillery_cli.core import (
    cli_package_install,
    linker,
    project_manifest,
    route_health,
    tooling_install,
)
from skillery_cli.core.agents import (
    AntigravityTarget,
    ClaudeCodeTarget,
    CodexTarget,
    detect_agent,
    get_target,
)
from skillery_cli.core import install_reason
from skillery_cli.core.installer import SkillInstaller, read_meta
from skillery_cli.core.store_backup import iter_store_skill_dirs
from skillery_cli.core.manifest_builder import build_manifest, git_commit_sha
from skillery_cli.core.secret_scan import scan_dir as secret_scan_dir
from skillery_cli.daemon.instrumentation import track_skill_event
from skillery_cli.core.transport import ApiError, HubClient
from skillery_cli.core import token_lock as _token_lock
from skillery_cli import _branding
from skillery_cli.commands._common import (
    hydrate_session_permissions,
    register_device_best_effort,
)
from skillery_cli.output import (
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
    from skillkit.errors import ScopeConflict

    try:
        asyncio.run(coro)
    except ApiError as e:
        # 401 — это не «ошибка API», а протухшая сессия: показываем ЧТО делать.
        # Раньше наружу летело сырое «[401/…] Signature has expired» — человек не
        # понимал, что достаточно повторить login (а авто-refresh не сработал,
        # потому что refresh-токена не было / сервер его отверг).
        if e.status_code == 401:
            reason = _REFRESH_FAILURE.get("reason")
            hint = SESSION_EXPIRED_HINT + (f" Причина: {reason}." if reason else "")
            if is_json():
                emit_error(
                    e.code or "SESSION_EXPIRED",
                    f"{hint} Ответ сервера: {e.message or str(e)}",
                    status_code=401,
                )
            else:
                console.print(f"[red]{hint}[/]\n[dim]Ответ сервера: {e}[/]")
            sys.exit(1)
        if is_json():
            emit_error(e.code or "API", e.message or str(e), status_code=e.status_code)
        else:
            console.print(f"[red]Ошибка API:[/] {e}")
        sys.exit(1)
    except (typer.Exit, typer.Abort):
        # click.exceptions.Exit/Abort наследуют RuntimeError — это штатное
        # завершение команды (emit_error уже сделан), пропускаем насквозь.
        raise
    except ScopeConflict as e:
        # A3 (голос 07-24): ownership-гейт (ForeignPathError ⊂ ScopeConflict ⊂
        # RuntimeError) — ловим ПЕРЕД generic RuntimeError и даём человекочитаемую
        # подсказку с ГОТОВОЙ командой --force, а не сырой «RUNTIME: гейт…».
        cmd = "skillery " + " ".join(sys.argv[1:])
        if "--force" not in sys.argv:
            cmd += " --force"
        first = str(e).split("\n")[0].strip()
        hint = (
            f"{first}\n"
            f"Если это тот же навык — перезаписать управляемой версией:\n"
            f"  {cmd}   (УДАЛИТ содержимое каталога)\n"
            f"Если это ваш другой навык — переименуйте/уберите каталог вручную."
        )
        if is_json():
            emit_error("SCOPE_CONFLICT", hint)
        else:
            console.print(
                f"[yellow]Каталог занят и не управляется Skillery.[/]\n{hint}"
            )
        sys.exit(1)
    except RuntimeError as e:
        # Например installer._clone_version: RuntimeError('git clone failed: ...')
        # — короткое сообщение вместо многоэкранного Rich-traceback.
        friendly = _private_repo_hint(str(e))
        if friendly is not None:
            if is_json():
                emit_error("REPO_ACCESS", friendly)
            else:
                console.print(f"[red]Нет доступа к приватному репозиторию.[/]\n{friendly}")
            sys.exit(1)
        emit_error("RUNTIME", str(e))
        sys.exit(1)


# Сигнатуры git-ошибки аутентификации при clone приватного репо (GitHub/GitLab).
_CLONE_AUTH_MARKERS = (
    "authentication failed",
    "invalid username or token",
    "password authentication is not supported",
    "could not read username",
    "terminal prompts disabled",
    "http basic: access denied",
    "permission denied (publickey)",
    "fatal: could not read",
)


def _private_repo_hint(message: str) -> str | None:
    """Если RuntimeError — это провал clone приватного репо по правам, вернуть
    человекочитаемую подсказку; иначе None (оставить сырой текст).

    Живой кейс: навык опубликован из приватного GitHub-репо, но бэкенд не отдал
    контент (Skillery App не установлен на репо → нет bundle) → CLI фолбэком
    клонирует репозиторий напрямую и упирается в 401 без credentials.
    """
    low = message.lower()
    if "clone" not in low and "git" not in low:
        return None
    # GitHub прячет приватный репо за 404 «repository '<url>' not found» —
    # ловим составной сигнатурой (не голым «not found», чтобы не хватать
    # generic-ошибки вроде «repo not found»).
    repo_missing = "repositor" in low and "not found" in low
    if not repo_missing and not any(m in low for m in _CLONE_AUTH_MARKERS):
        return None
    return (
        "У хаба нет доступа к исходному репозиторию навыка, поэтому его файлы не\n"
        "удалось получить. Если репозиторий приватный и принадлежит вам:\n"
        "  • для GitHub — установите Skillery GitHub App на этот репозиторий;\n"
        "  • для GitLab — добавьте токен доступа к навыку в настройках навыка;\n"
        "затем опубликуйте навык заново (после синхронизации хаб сам отдаст файлы).\n"
        "Публичные репозитории скачиваются без дополнительной настройки."
    )


def _strip_invite_url(value: str) -> str:
    m = re.match(r".*/(?:auth/)?invite/([A-Za-z0-9_\-]+)/?$", value)
    if m:
        return m.group(1)
    return value


#: Минимальная форма почты: есть «@», непустые локальная часть и домен с точкой.
#: Полную RFC-валидацию не делаем — задача отсечь МУСОР (в боевом профиле лежало
#: ``user_email = "1"``: JWT-``sub`` это ЧИСЛОВОЙ user_id, а не почта).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")

#: Почему последний refresh не удался — чтобы 401 объяснялся человеку («сессия
#: истекла, выполните login»), а не голым «Signature has expired» из бэкенда.
_REFRESH_FAILURE: dict[str, str | None] = {"reason": None}

SESSION_EXPIRED_HINT = (
    f"Сессия истекла — выполните `{_branding.APP_NAME} login`."
)


def _is_valid_email(value: object) -> bool:
    """Похоже ли значение на почту (есть «@» и домен). Мусор → False."""
    return bool(isinstance(value, str) and _EMAIL_RE.match(value.strip()))


def _resolve_login_email(cfg: ClientConfig, claims: dict | None) -> str:
    """Валидная почта учётки для ключа токенов и конфига (или ``""``).

    Порядок: то, что уже проставил ``hydrate_session_permissions`` из ``/me``
    (авторитетный источник) → ``email`` из JWT → ``sub`` из JWT. ``sub`` в
    JWT-slim — числовой ``user_id``, поэтому подходит только если это реально
    почта; иначе в конфиг НЕ пишем ничего (раньше писали «1», и токены ложились
    в keyring под ключом «1» — сессия «терялась» при следующем запуске).
    """
    claims = claims or {}
    for candidate in (cfg.user_email, claims.get("email"), claims.get("sub")):
        if _is_valid_email(candidate):
            return str(candidate).strip()
    return ""


def _fail_invalid_login_email(raw: object) -> None:
    """Единая ошибка «почта не определилась» — конфиг/keyring не трогаем."""
    emit_error(
        "VALIDATION",
        "Не удалось определить e-mail учётной записи "
        f"(получено: {raw!r}). Токены и конфиг НЕ сохранены — "
        f"повторите вход: `{_branding.APP_NAME} login --email <ваша почта>`.",
    )
    raise typer.Exit(1)


def _get_access_token() -> str:
    cfg = ClientConfig.load()
    if not cfg.user_email:
        console.print("[red]Не авторизован.[/] Сначала: skillery login <invite>")
        raise typer.Exit(1)
    access, refresh = load_tokens(cfg.user_email)
    if not access:
        console.print("[red]Локальный access-токен не найден.[/] Сделайте login заново.")
        raise typer.Exit(1)
    if not refresh:
        # Access есть, refresh НЕТ: протухший access обновить нечем, и человек
        # упирался в голое «401 Signature has expired» из бэкенда. Помечаем
        # причину заранее — ``_run`` покажет её понятным текстом при 401.
        _REFRESH_FAILURE["reason"] = (
            f"refresh-токен для {cfg.user_email} не найден в хранилище"
        )
    return access


#: #2264: внутрипроцессная половина сериализации обновления токена — по одному
#: замку на event loop. Нужна по двум причинам: (1) байтовая блокировка файла
#: видна и СВОЕМУ процессу (второй дескриптор того же файла честно ждёт), так
#: что без неё корутины одного процесса упирались бы друг в друга через ядро;
#: (2) ожидание межпроцессного лока занимает поток из пула ``to_thread`` — если
#: в него уйдут все конкурирующие корутины, пула не хватит даже на освобождение
#: лока владельцем. С этим замком в пул уходит РОВНО ОДНА корутина процесса.
#: Ключ — сам loop (CLI за жизнь процесса может поднять их несколько:
#: ``asyncio.run`` на команду), слабые ссылки не держат мёртвые loop'ы.
_LOOP_REFRESH_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _inproc_refresh_lock() -> asyncio.Lock:
    """Замок обновления токена текущего event loop (создаётся лениво)."""
    loop = asyncio.get_running_loop()
    lock = _LOOP_REFRESH_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _LOOP_REFRESH_LOCKS[loop] = lock
    return lock


class _RefreshGuard:
    """Межпроцессная половина замка обновления токена.

    ``acquire``/``release`` вызываются из рабочих потоков
    (``asyncio.to_thread``) — захват блокирующий, а на event loop демона висит
    long-poll очереди заданий. Поэтому механизм выбран НЕ потоко-аффинный:
    байтовая блокировка файла живёт на дескрипторе, а не на потоке (подробнее —
    ``core/token_lock.py``).

    Неудача захвата НЕ отменяет обновление: лучше рискнуть гонкой, чем
    оставить пользователя без сессии, если сосед завис.
    """

    def __init__(self, timeout: float = _token_lock.DEFAULT_TIMEOUT) -> None:
        self._timeout = timeout
        self._cm: object | None = None

    def acquire(self) -> bool:
        cm = _token_lock.token_refresh_lock(self._timeout)
        self._cm = cm
        try:
            return bool(cm.__enter__())  # type: ignore[attr-defined]
        except Exception:  # сбой лока не должен рвать сессию
            self._cm = None
            return False

    def release(self) -> None:
        cm, self._cm = self._cm, None
        if cm is not None:
            with contextlib.suppress(Exception):
                cm.__exit__(None, None, None)  # type: ignore[attr-defined]


def _make_refresh_callback(cfg: ClientConfig) -> object:
    """Возвращает callback который CLI передаёт в HubClient.

    При 401 HubClient вызывает callback → callback дёргает /auth/refresh
    с сохранённым refresh-токеном, получает новый pair, сохраняет в keyring,
    возвращает (access, refresh). Если refresh не сработал — None, а ПРИЧИНА
    запоминается в ``_REFRESH_FAILURE``: тогда пользователь видит «сессия
    истекла, выполните login», а не сырое «Signature has expired».
    """

    async def _refresh() -> tuple[str, str] | None:
        if not cfg.user_email:
            _REFRESH_FAILURE["reason"] = "в конфиге нет учётной записи"
            return None
        _, refresh = load_tokens(cfg.user_email)
        if not refresh:
            _REFRESH_FAILURE["reason"] = (
                f"refresh-токен для {cfg.user_email} не найден в хранилище"
            )
            return None
        # #2264: обновление токена сериализуем МЕЖДУ ПРОЦЕССАМИ. Демон,
        # watchdog и команда пользователя — разные процессы с общим токеном;
        # без лока они проворачивали один RT одновременно, и сервер (законно)
        # считал повтор кражей — ``reuse_detected``, 401 на всё.
        # Ждём в рабочем потоке: захват блокирующий, а на event loop демона
        # висит long-poll очереди заданий.
        async with _inproc_refresh_lock():
            return await _refresh_locked(cfg, refresh)

    async def _refresh_locked(
        cfg: ClientConfig, refresh: str
    ) -> tuple[str, str] | None:
        guard = _RefreshGuard()
        await asyncio.to_thread(guard.acquire)
        try:
            # ПОД ЛОКОМ перечитываем хранилище: пока мы ждали, сосед мог уже
            # обновить пару. Слать свой (теперь устаревший) токен нельзя —
            # это ровно тот повтор, из-за которого сервер рвёт сессию. Лок
            # без перечитывания лишь упорядочил бы гонку, а не убрал её.
            fresh_access, fresh_refresh = load_tokens(cfg.user_email)
            if fresh_refresh and fresh_refresh != refresh and fresh_access:
                _REFRESH_FAILURE["reason"] = None
                return (fresh_access, fresh_refresh)
            sub = HubClient(base_url=cfg.base_url, access_token=None)
            try:
                data = await sub.refresh(fresh_refresh or refresh)
            except ApiError as exc:
                _REFRESH_FAILURE["reason"] = (
                    f"обновление сессии отклонено сервером ({exc.code})"
                )
                return None
            finally:
                await sub.close()
            new_access = data["access_token"]
            new_refresh = data["refresh_token"]
            save_tokens(cfg.user_email, new_access, new_refresh)
            populate_from_jwt(cfg, new_access)
            cfg.save()
            _REFRESH_FAILURE["reason"] = None
            return (new_access, new_refresh)
        finally:
            await asyncio.to_thread(guard.release)

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


def _stale_chain_deps(
    bundle: dict, *, target, project: Optional[Path]
) -> list[tuple[str, str]]:
    """Зависимости из ``dependencies_chain``, которых нет локально либо старее.

    #2282: ``skill update`` сравнивал ТОЛЬКО версию самого навыка. Пока версия
    потребителя не менялась, он получал «актуально» — а новая версия
    объявленной им зависимости (и тем более зависимость, ПОЯВИВШАЯСЯ в новой
    версии манифеста) не приезжала вообще. Сам навык в цепочке пропускается:
    его версию гейтит обычная проверка ``_is_newer``.
    """
    self_slug = bundle.get("skill_slug")
    stale: list[tuple[str, str]] = []
    for entry in bundle.get("dependencies_chain") or []:
        if not entry or len(entry) < 2:
            continue
        dep_slug, dep_version = entry[0], entry[1]
        if not dep_slug or dep_slug == self_slug:
            continue
        meta = read_meta(target.slug_dir(dep_slug, project=project))
        current = (meta or {}).get("version")
        if current is None or _is_newer(dep_version, current):
            stale.append((dep_slug, dep_version))
    return stale


def _maybe_auto_update(cfg: ClientConfig, *, project: Path | None = None) -> None:
    """Тихо обновляет установленные skills до latest published если cooldown прошёл.

    Обнаружение по scope: global (``~/.claude/skills``) + текущий проект, если
    задан ``project`` (project-only навыки иначе не видны в global scope и не
    автообновлялись). Стор общий: контент материализуется один раз, линкуется в
    каждый scope, где навык установлен.
    """
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
    # ref → {current, slug, skill_id, scopes:set}. scope=None → global.
    scopes: list[Path | None] = [None] if project is None else [None, project]
    found: dict[str, dict] = {}
    for scope in scopes:
        base = target.base_dir(project=scope)
        if not base.exists():
            continue
        for slug_dir in base.iterdir():
            meta = read_meta(slug_dir)
            if not meta:
                continue
            ref = meta.get("slug") or (
                str(meta["skill_id"]) if meta.get("skill_id") is not None else None
            )
            if not ref:
                continue
            entry = found.setdefault(
                ref,
                {
                    "current": meta.get("version", "0.0.0"),
                    "slug": meta.get("slug"),
                    "skill_id": meta.get("skill_id"),
                    "scopes": set(),
                },
            )
            entry["scopes"].add(scope)
    if not found:
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
            installer = SkillInstaller(target, cfg.effective_store_dir())
            for ref, e in found.items():
                try:
                    bundle = await client.install_bundle(ref)
                except Exception:
                    continue
                # P0: bundle без repo_url = stub-источник — обновлять нечем
                # (живой инцидент: такой «апдейт» затирал реальный контент
                # 112-байтовым stub'ом). Пропускаем кандидата целиком.
                if not bundle.get("repo_url"):
                    continue
                # B8: апдейтим ТОЛЬКО если опубликованная версия строго новее
                # установленной — downgrade/равные пропускаем.
                if not _is_newer(bundle["version"], e["current"]):
                    continue
                updated = False
                for scope in e["scopes"]:
                    res = installer.install(
                        slug=e["slug"],
                        skill_id=e["skill_id"],
                        version=bundle["version"],
                        commit_sha=bundle["commit_sha"],
                        repo_url=bundle.get("repo_url"),
                        skill_path=bundle.get("skill_path"),
                        manifest=bundle["manifest"],
                        project=scope,
                    )
                    if getattr(res, "skipped", False):
                        continue  # guard отказал (stub-would-clobber и т.п.)
                    # gap A: контент обновили — обязаны переустановить tooling
                    # (runtime_deps/CLI/MCP) под манифест НОВОЙ версии в ЭТОТ scope.
                    _apply_tooling(
                        res, bundle["manifest"], agent_target=target, project=scope
                    )
                    updated = True
                if updated:
                    err_console.print(
                        f"[dim cyan]↑ auto-update[/] {ref}: {e['current']} → {bundle['version']}"
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
    except Exception as e:
        # auto-update не должен ломать команду, но и не молчит в никуда:
        # тихая диагностическая строка в stderr (stdout — машинный канал).
        Console(stderr=True).print(f"[dim]auto-update пропущен: {e}[/]")


# ======================================================
#        CLI SELF-UPDATE (проверка / уведомление / upgrade)
# ======================================================
_PYPI_JSON_URL = "https://pypi.org/pypi/{package}/json"
_CLI_UPDATE_COOLDOWN = timedelta(hours=24)


def _fetch_latest_pypi_version(package: str, *, timeout: float = 3.0) -> str | None:
    """Latest-версия пакета с PyPI (JSON API). None при любой ошибке/таймауте."""
    import json
    import urllib.request

    url = _PYPI_JSON_URL.format(package=package)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            data = json.load(resp)
        return (data.get("info") or {}).get("version") or None
    except Exception:
        return None


def _check_cli_update_detailed(
    cfg: ClientConfig, *, now: datetime | None = None
) -> tuple[str | None, bool]:
    """(новая_версия|None, свежая_ли_проверка_PyPI_в_этом_вызове). Fail-silent.

    Кэширует таймстамп + виденную версию: PyPI опрашивается не чаще раза/сутки, в
    пределах cooldown ответ берётся из кэша (``fresh=False``). ``fresh=True`` —
    только когда реально сходили в PyPI (по нему гейтится авто-апгрейд: спавним
    обновление максимум раз в сутки, а не на каждой команде).
    """
    from skillery_cli import __version__ as current

    if not cfg.cli_update_check:
        return None, False
    now = now or datetime.now(UTC)
    if cfg.cli_update_check_at:
        try:
            last = datetime.fromisoformat(cfg.cli_update_check_at)
            if now - last < _CLI_UPDATE_COOLDOWN:
                cached = cfg.cli_latest_version
                newer = cached if (cached and _is_newer(cached, current)) else None
                return newer, False
        except ValueError:
            pass
    latest = _fetch_latest_pypi_version(_branding.DIST_NAME)
    cfg.cli_update_check_at = now.isoformat()
    if latest:
        cfg.cli_latest_version = latest
    try:
        cfg.save()
    except Exception:
        pass
    newer = latest if (latest and _is_newer(latest, current)) else None
    return newer, True


def _check_cli_update(cfg: ClientConfig, *, now: datetime | None = None) -> str | None:
    """Новая версия CLI, если доступна на PyPI (иначе None). Fail-silent."""
    return _check_cli_update_detailed(cfg, now=now)[0]


def _stop_daemon_for_upgrade() -> bool:
    """Остановить демона перед обновлением CLI. Идемпотентно, тихо.

    Демон живёт внутри того же окружения, что и CLI, и держит его файлы
    открытыми: `uv tool install --force` спотыкается о `Scripts/` с «Отказано в
    доступе». После апгрейда демон вернётся сам — любая команда CLI поднимает
    его (см. `_heal_daemon_if_dead`).

    ⚠️ САМОУБИЙСТВО ЗАПРЕЩЕНО (инцидент 2026-07-24). Апгрейд запускает и САМ
    демон (push-задача ``cli_upgrade`` и старт-fallback ``_daemon_cli_self_upgrade``),
    а PID-файл в этот момент указывает на НЕГО САМОГО. Раньше он тут же слал себе
    SIGTERM/TerminateProcess — и умирал ДО ``subprocess.Popen`` worker'а: ни
    апгрейда, ни демона (`daemon status` → stopped, `Cycles: 1`), очередь больше
    никто не опрашивал. Гасить живой демон — работа worker'а
    (``_upgrade_worker.stop_daemons``, он исключает собственный PID и делает это
    НЕПОСРЕДСТВЕННО перед заменой файлов).
    """
    try:
        import os as _os
        import signal as _signal

        from skillery_cli.daemon.daemon_runner import (
            is_process_alive,
            read_running_pid,
        )

        pid = read_running_pid()
        if pid is None or not is_process_alive(pid):
            return False
        if pid == _os.getpid():
            return False  # это МЫ; себя не убиваем — см. docstring
        _os.kill(pid, _signal.SIGTERM)
        for _ in range(20):  # ждём до ~2с, пока отпустит файлы
            if not is_process_alive(pid):
                return True
            time.sleep(0.1)
        return True
    except Exception:  # noqa: BLE001 — апгрейд важнее аккуратной остановки
        return False


def _upgrade_already_running() -> bool:
    """Идёт ли апгрейд прямо сейчас (лок держит worker).

    Два одновременных `upgrade` рвут установку: worker'ы перезаписывают одни и
    те же файлы, и trampoline остаётся битым — `skillery` падает с «uv trampoline
    failed to canonicalize script path», лечится только переустановкой.

    Лок берём тем же ядерным механизмом, что и у демона (mutex/flock), просто под
    своим именем — второй реализации единственности не заводим.
    """
    try:
        from skillery_cli._upgrade_worker import lock_path, mutex_name
        from skillery_cli.daemon.single_instance import acquire_daemon_lock

        # И имя мьютекса, и файл — те же, что берёт worker: иначе на POSIX
        # проверка смотрела бы в другой файл и не увидела бы идущий апгрейд.
        lock = acquire_daemon_lock(mutex_name=mutex_name(), lock_path=lock_path())
        if not lock.acquired:
            return True
        lock.release()
        return False
    except Exception:  # noqa: BLE001 — недоступность лока не повод не обновляться
        return False


def _upgrade_launcher() -> str:
    """Интерпретатор для worker'а — ОБЯЗАТЕЛЬНО вне каталога самого инструмента.

    ``sys.executable`` лежит внутри ``…/tools/skillery-cli/Scripts/`` и, пока
    worker жив, ДЕРЖИТ этот каталог: uv не может его удалить, и апгрейд падает
    с «failed to remove directory Scripts: Отказано в доступе (os error 5)».
    Снаружи это выглядело как «обновление запущено» → и тишина: версия не
    менялась, ошибка уходила в DEVNULL. Именно этим worker и убивал сам себя.

    ``sys.base_prefix`` указывает на БАЗОВЫЙ интерпретатор (вне tool-каталога) —
    он ничего не блокирует. Worker'у хватает stdlib (``time``/``subprocess``),
    пакет он не импортирует.
    """
    base = Path(sys.base_prefix)
    # На Windows pythonw ПЕРВЫМ: у него нет консольной подсистемы, поэтому окно
    # не может всплыть даже если флаги создания процесса кто-то потеряет.
    names = (
        ("pythonw.exe", "python.exe")
        if sys.platform == "win32"
        else ("python3", "python")
    )
    for name in names:
        for candidate in (base / name, base / "bin" / name):
            if candidate.exists():
                return str(candidate)
    return sys.executable


def _spawn_background_upgrade(
    delay: float = 4.0, version: str | None = None
) -> bool:
    """Обновление CLI в ОТДЕЛЬНОМ процессе С ЗАДЕРЖКОЙ, НЕ дожидаясь его.

    Задержка критична на Windows: launcher ``skillery.exe`` залочен, пока
    запущен, и uv/pipx не могут его перезаписать (``os error 32``). Ждём, чтобы
    текущий процесс/launcher успел выйти, затем апгрейд. Запускаем
    ИНТЕРПРЕТАТОРОМ (``sys.executable``), НЕ через ``skillery.exe`` — иначе второй
    launcher снова залочит .exe. venv обновляется, launcher пересоздаётся; новая
    версия применяется со СЛЕДУЮЩЕГО запуска CLI. True если процесс стартовал.
    """
    import json
    import shutil
    import subprocess

    from skillery_cli import __version__ as _current_version
    from skillery_cli import _upgrade_worker as worker_mod

    # Два одновременных апгрейда перезаписывают одни и те же файлы и оставляют
    # trampoline битым («uv trampoline failed to canonicalize script path»).
    if _upgrade_already_running():
        return False

    # Демон держит открытыми файлы окружения (Scripts/, Lib/) — без остановки
    # апгрейд падает с «Отказано в доступе» (os error 5). Здесь гасим заранее,
    # но ГЛАВНОЕ гашение делает worker непосредственно перед заменой файлов:
    # между нашим выходом и его стартом демон мог бы вернуться самолечением.
    _stop_daemon_for_upgrade()

    cmds = _upgrade_commands(version)

    # Worker кладём ВНЕ tool-каталога: этот каталог целиком перезаписывается,
    # а скрипт должен пережить замену и не держать его открытым.
    home = Path.home() / _branding.HOME_DIR_NAME
    home.mkdir(parents=True, exist_ok=True)
    worker_py = home / "_upgrade_worker.py"
    config_json = home / "_upgrade_worker.json"
    try:
        shutil.copyfile(worker_mod.__file__, worker_py)
        config_json.write_text(
            json.dumps(
                {
                    "delay": delay,
                    "commands": cmds,
                    # Демон поднимаем УЖЕ НОВЫМ бинарём сразу после апгрейда,
                    # не дожидаясь следующей команды пользователя.
                    "daemon_binary": shutil.which(_branding.APP_NAME) or "",
                    # Для итога-sidecar (A6): CLI покажет «from → to» при след. запуске.
                    "from_version": _current_version,
                    "to_version": version or "",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:
        return False
    from librarykit.proc import popen as proc_popen

    popen_kw: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform != "win32":
        popen_kw["start_new_session"] = True
    try:
        # detached=True → DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP (+ кит
        # доставляет CREATE_NO_WINDOW; при DETACHED система его игнорирует, но
        # политика флагов должна быть ОДНА — она живёт в librarykit.proc, #1144).
        proc_popen(
            [_upgrade_launcher(), str(worker_py), str(config_json)],
            detached=True,
            **popen_kw,
        )
        return True
    except Exception:
        return False


def _detect_upgrade_command(version: str | None = None) -> list[str]:
    """Команда обновления CLI под менеджер установки (uv tool / pipx / pip).

    Две защиты от «обновление молча не сработало»:

    1. **Обход кэша индекса.** uv кэширует индекс PyPI, и `uv tool upgrade`
       отвечал «уже последняя», хотя на PyPI лежала новее — обновление просто
       не наступало. Поэтому всегда просим `--refresh`.
    2. **Пин точной версии.** Версию мы уже узнали у PyPI (`_fetch_latest_pypi_version`),
       так что не оставляем резолверу простора для решения «обновлять нечего»:
       ставим ровно её через `install --force`. Это же чинит и «откат» —
       перестановку на конкретную версию, а не только вперёд.

    Без ``version`` (когда latest неизвестен) — прежнее поведение + ``--refresh``.
    """
    import shutil

    dist = _branding.DIST_NAME
    spec = f"{dist}=={version}" if version else dist
    exe = (sys.executable or "").replace("\\", "/").lower()

    # ВСЕГДА `tool install --force --refresh`, а НЕ `tool upgrade`: у `uv tool
    # upgrade` нет флага `--refresh` (он падает «unexpected argument»), а без
    # обхода кэша индекса обновление и не наступало. `install --force` ставит
    # latest ровно так же, но умеет обойти кэш.
    uv_cmd = ["uv", "tool", "install", "--force", "--refresh", spec]
    pipx_cmd = (
        ["pipx", "install", "--force", spec] if version else ["pipx", "upgrade", dist]
    )

    if "/uv/tools/" in exe and shutil.which("uv"):
        return uv_cmd
    if "/pipx/" in exe and shutil.which("pipx"):
        return pipx_cmd
    if shutil.which("uv"):
        return uv_cmd
    if shutil.which("pipx"):
        return pipx_cmd
    return [sys.executable, "-m", "pip", "install", "--upgrade", spec]


def _upgrade_commands(version: str | None = None) -> list[list[str]]:
    """Цепочка команд обновления: точная версия → общий upgrade.

    Пин даёт детерминизм, но сразу после релиза индекс PyPI ещё не разъехался
    по CDN, и `pkg==X.Y.Z` может транзиентно ответить «no version». Тогда
    добираем обычным `upgrade --refresh` — он поставит latest, который узел уже
    отдаёт. Так обновление не срывается ни от кэша, ни от лага индекса.
    """
    primary = _detect_upgrade_command(version)
    if not version:
        return [primary]
    fallback = _detect_upgrade_command(None)
    return [primary] if fallback == primary else [primary, fallback]


def _format_upgrade_result(data: dict) -> str | None:
    """Собрать человекочитаемый итог фонового апгрейда из sidecar-данных.

    Возвращает ``None``, если данные пустые/битые (нечего показывать).
    """
    if not isinstance(data, dict):
        return None
    frm = data.get("from") or "?"
    to = data.get("to") or "?"
    if data.get("ok"):
        return f"✓ {_branding.DIST_NAME} обновлён {frm} → {to}"
    detail = (data.get("error") or "").strip()
    tail = f" ({detail})" if detail else ""
    return (
        f"✗ Обновление {_branding.DIST_NAME} {frm} → {to} не удалось{tail}. "
        f"Обновите вручную: {_branding.APP_NAME} upgrade"
    )


def _consume_upgrade_result(base: Path | None = None) -> str | None:
    """Показать и удалить (one-shot) итог фонового апгрейда CLI.

    ``upgrade`` запускает обновление отдельным процессом и сразу выходит
    («запущено») — раньше пользователь не узнавал, чем оно кончилось. Worker
    пишет итог в ~/.skillery/_upgrade_result.json; здесь показываем его один раз
    (в stderr, не в JSON-режиме) и удаляем, чтобы не повторять. ``base`` — для
    тестов. Возвращает показанное сообщение (или ``None``).
    """
    import json

    from skillery_cli import output as _output

    if getattr(_output, "_mode", "text") == "json":
        return None
    root = base if base is not None else Path.home() / _branding.HOME_DIR_NAME
    path = root / "_upgrade_result.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    # Удаляем сразу: показать ровно один раз, даже если рендер ниже упадёт.
    try:
        path.unlink()
    except Exception:
        pass
    msg = _format_upgrade_result(data)
    if msg:
        err = Console(stderr=True)
        color = "green" if data.get("ok") else "yellow"
        err.print(f"[{color}]{msg}[/]")
    return msg


def _maybe_notify_cli_update(cfg: ClientConfig) -> None:
    """Самообновление CLI (если включено) ИЛИ уведомление о новой версии.

    Не в JSON-режиме. При ``cli_auto_upgrade`` и СВЕЖЕЙ PyPI-проверке (≤1/сутки)
    — тихо запускаем обновление в фоне (применится со следующего запуска). Иначе
    — ненавязчивая подсказка про `upgrade` в stderr.
    """
    from skillery_cli import __version__ as current
    from skillery_cli import output as _output

    if getattr(_output, "_mode", "text") == "json":
        return
    # Итог прошлого фонового апгрейда (A6) — показать до проверки новых версий.
    _consume_upgrade_result()
    try:
        latest, fresh = _check_cli_update_detailed(cfg)
    except Exception:
        return
    if not latest:
        return
    err = Console(stderr=True)
    # Авто-апгрейд запускаем ТОЛЬКО на свежей проверке (fresh) — иначе спавнили бы
    # процесс обновления на каждой команде в пределах суточного cooldown.
    if fresh and cfg.cli_auto_upgrade and _spawn_background_upgrade(version=latest):
        err.print(
            f"[cyan]↑ Обновляю {_branding.DIST_NAME} {current} → {latest} в фоне[/] "
            "(применится при следующем запуске)."
        )
        return
    err.print(
        f"[yellow]↑ Доступна новая версия {_branding.DIST_NAME} {latest}[/] "
        f"(у вас {current}). Обновить: [bold]{_branding.APP_NAME} upgrade[/] "
        f"или [dim]{' '.join(_detect_upgrade_command(latest))}[/]"
    )


def cmd_upgrade(
    check: bool = typer.Option(
        False, "--check", help="Только проверить наличие новой версии, не обновлять"
    ),
) -> None:
    """Обновить сам CLI (skillery-cli) до последней версии с PyPI.

    Определяет менеджер установки (uv tool / pipx / pip) и запускает обновление.
    ``--check`` — только сверить версию, без установки.
    """
    from skillery_cli import __version__ as current

    cfg = ClientConfig.load()
    latest = _fetch_latest_pypi_version(_branding.DIST_NAME)
    cfg.cli_update_check_at = datetime.now(UTC).isoformat()
    if latest:
        cfg.cli_latest_version = latest
    try:
        cfg.save()
    except Exception:
        pass

    available = bool(latest and _is_newer(latest, current))
    if check or not available:
        payload = {
            "current": current,
            "latest": latest,
            "update_available": available,
        }

        def _render(_: dict) -> None:
            if available:
                console.print(
                    f"Доступно обновление: [bold]{current}[/] → [bold green]{latest}[/]\n"
                    f"Обновить: [bold]{_branding.APP_NAME} upgrade[/]"
                )
            elif latest:
                console.print(f"[green]У вас последняя версия ({current}).[/]")
            else:
                console.print(f"Не удалось проверить PyPI. Текущая версия: {current}.")

        emit_data(payload, text_renderer=_render)
        return

    # Пинуем ровно ту версию, которую только что увидели на PyPI: иначе менеджер
    # решал по своему кэшу индекса, что обновлять нечего, и upgrade был пустышкой.
    cmd = _detect_upgrade_command(latest)
    console.print(
        f"Обновляю {_branding.DIST_NAME}: [bold]{current}[/] → [bold green]{latest}[/]"
    )

    # Windows: launcher (skillery.exe) залочен, пока команда запущена — uv/pipx не
    # могут его перезаписать (os error 32). Запускаем апгрейд в ОТДЕЛЬНОМ процессе
    # С ЗАДЕРЖКОЙ (после выхода этой команды), не синхронно.
    if sys.platform == "win32":
        if _spawn_background_upgrade(version=latest):
            console.print(
                "[cyan]↑ Обновление запущено[/] — применится через несколько секунд. "
                f"Откройте новый терминал и проверьте: [bold]{_branding.APP_NAME} --version[/]."
            )
            return
        emit_error(
            "UPGRADE_FAILED",
            f"Не удалось запустить обновление. Выполните вручную: {' '.join(cmd)}",
        )
        raise typer.Exit(1)

    # Unix: перезапись запущенного бинаря разрешена → синхронно; на ошибке —
    # фолбэк на фоновый апгрейд.
    console.print(f"[dim]$ {' '.join(cmd)}[/]")
    from librarykit.proc import run as proc_run

    # То же, что и на Windows: демон держит файлы окружения.
    _stop_daemon_for_upgrade()
    try:
        # allow_console=True — ЕДИНСТВЕННОЕ осознанное исключение из политики
        # «без окна» (#1144): это интерактивный foreground-`upgrade`, вывод
        # uv/pip адресован пользователю. ⚠️ Контракт `proc.run` захватывает
        # потоки ВСЕГДА, поэтому вывод не стримится, а печатается по завершении
        # (стрима стоила бы отдельная реализация Popen+чтение — заводить вторую
        # политику запуска ради прогресс-бара не стали). Таймаут 900s — как у
        # установки CLI-пакета навыка (сборка колёс на узком канале).
        result = proc_run(cmd, timeout=900, allow_console=True, check=True)
        tail = (result.stdout or "").strip()
        if tail:
            console.print(f"[dim]{tail}[/]")
    except Exception as e:
        if _spawn_background_upgrade(version=latest):
            console.print(
                "[cyan]↑ Синхронно не вышло — доупгрейжу в фоне.[/] "
                "Откройте новый терминал."
            )
            return
        emit_error(
            "UPGRADE_FAILED",
            f"Автообновление не удалось ({e}). Выполните вручную: {' '.join(cmd)}",
        )
        raise typer.Exit(1)
    console.print(
        f"[green]✓ Готово.[/] Если «{_branding.APP_NAME}» показывает старую версию — "
        "откройте новый терминал."
    )


# ======================================================
#                  COMMAND IMPLEMENTATIONS
# ======================================================
def _ensure_daemon_after_login() -> dict:
    """Поднять фонового демона сразу после входа (идемпотентно, best-effort).

    #954: раньше `login` только регистрировал устройство. Демон никто не
    запускал, поэтому задания из веба висели в очереди («устанавливается»
    бесконечно), а устройство выглядело офлайн — признак «на связи» даёт
    именно опрос очереди демоном. Ошибка запуска НЕ ломает вход.
    """
    state: dict = {"event": "failed", "pid": None}
    try:
        from skillery_cli.commands.daemon import ensure_daemon_running

        state = dict(ensure_daemon_running())
    except Exception as exc:  # noqa: BLE001
        state = {"event": "failed", "pid": None, "error": str(exc)}

    # Автозапуск: демон, поднятый только сейчас, умрёт с перезагрузкой — тогда
    # устройство «пропадёт» из веба, а задания снова начнут копиться в очереди.
    # Команды user-scope (без sudo). Отключается SKILLERY_NO_AUTOSTART=1.
    if os.environ.get("SKILLERY_NO_AUTOSTART", "").strip() not in ("", "0"):
        state["autostart"] = {"activated": False, "error": "отключено переменной окружения"}
        return state
    try:
        from skillery_cli.daemon.autostart import ensure_autostart

        state["autostart"] = ensure_autostart()
    except Exception as exc:  # noqa: BLE001
        state["autostart"] = {"activated": False, "error": str(exc)}
    return state


def _print_daemon_hint(state: dict) -> None:
    """Человеку — что с демоном и как включить автозапуск после перезагрузки."""
    event = str(state.get("event") or "")
    if event == "started":
        console.print(
            f"  Демон:       запущен (pid={state.get('pid')}) — задания из веба "
            "применяются автоматически"
        )
    elif event == "already_running":
        console.print(f"  Демон:       уже работает (pid={state.get('pid')})")
    else:
        console.print(
            "[yellow]  Демон не запустился — задания из веба применяться не "
            "будут. Запустите вручную: skillery daemon start[/]"
        )
        return
    auto = state.get("autostart") or {}
    if auto.get("activated"):
        console.print(
            "  Автозапуск:  включён — демон поднимется после перезагрузки"
        )
    else:
        reason = str(auto.get("error") or "").strip()
        console.print(
            "[dim]  Автозапуск не включён"
            + (f" ({reason[:80]})" if reason else "")
            + " — команда: skillery daemon install[/]"
        )


def _log_daemon_issue(msg: str) -> None:
    """Тихо (без вывода в команду) записать проблему демона в logs/daemon.log."""
    try:
        from skillery_cli.core.logging_setup import configure_logging, get_logger

        configure_logging(filename="daemon.log")  # идемпотентно
        get_logger("heal").error(msg)
    except Exception:  # noqa: BLE001 — лог не должен мешать команде
        pass


def _heal_daemon_if_dead() -> None:
    """Самолечение: любая команда CLI поднимает демона, если тот умер.

    Автозапуск покрывает перезагрузку, но демон может упасть и посреди сессии —
    тогда устройство молча «уходит в офлайн», а задания из веба перестают
    применяться. Проверка дешёвая (чтение PID-файла + проверка процесса) и
    полностью тихая: ни вывода, ни исключений, ни задержки для самих команд
    демона (иначе `daemon stop` тут же поднимал бы его обратно).
    """
    if os.environ.get("SKILLERY_NO_DAEMON_HEAL", "").strip() not in ("", "0"):
        return
    argv = " ".join(sys.argv[1:])
    if (
        "daemon" in argv
        or "upgrade" in argv
        or "--help" in argv
        or "--version" in argv
    ):
        return
    # Пока идёт апгрейд — демон поднимать НЕЛЬЗЯ: он держит файлы окружения
    # (Scripts/), и `uv tool install` виснет/падает на их замене. Демон вернёт
    # сам worker апгрейда новым бинарём в самом конце. Раньше любая команда
    # (даже параллельный `upgrade`) через самолечение возвращала демона в
    # середину апгрейда и подвешивала его.
    if _upgrade_already_running():
        return
    try:
        from skillery_cli.config import ClientConfig

        if not ClientConfig.load().is_logged_in():
            return  # незалогиненному демон не нужен
        from skillery_cli.commands.daemon import ensure_daemon_running

        state = ensure_daemon_running()
        event = str((state or {}).get("event") or "")
        if event not in ("started", "already_running"):
            # Тихо для пользователя (никакого вывода в команду), но НЕ молча:
            # провал самолечения = устройство может уйти в офлайн — это стоит
            # увидеть в логах при разборе «почему не на связи».
            _log_daemon_issue(f"self-heal не поднял демон: {state}")
    except Exception as exc:  # noqa: BLE001 — самолечение не мешает команде
        _log_daemon_issue(f"self-heal исключение: {exc}")
        return


def cmd_login(
    invite: Optional[str] = typer.Argument(
        None,
        help=(
            "Invite-токен или URL (для invite-flow). Опускайте если хотите "
            "залогиниться через --email/--password или browser-flow."
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
    code: Optional[str] = typer.Option(
        None,
        "--code",
        help=(
            "Exchange-code для прямого редима (минует browser-flow). "
            "Получить code можно на странице /cli-login."
        ),
    ),
    base_url: Optional[str] = typer.Option(None),
) -> None:
    """Логин: invite-token, email+password, exchange-code или browser-flow.

    Приоритет:
    1. Если передан positional `invite` → invite-flow (как раньше).
    2. Если --email/--password → email+password flow (POST /auth/login).
    3. Если --code → direct redeem (POST /auth/exchanges/{code}/redeem).
    4. Иначе (без аргументов) → browser-flow (открыть браузер на /cli-login).
    """
    cfg = ClientConfig.load()
    if base_url:
        cfg.base_url = base_url

    if invite is not None:
        # --- invite-flow (как раньше) ---
        if email is None:
            email = "" if is_json() else Prompt.ask("Email")
        if name is None:
            name = "" if is_json() else Prompt.ask("Имя для отображения")
        # Мусор в почте не должен доехать до keyring/конфига (ключ токенов = почта).
        if not _is_valid_email(email):
            _fail_invalid_login_email(email)
        token = _strip_invite_url(invite)

        async def _do_invite() -> None:
            client = HubClient(base_url=cfg.base_url)
            try:
                data = await client.login_invite(
                    invite_token=token, email=email, display_name=name
                )
                # JWT-slim: права — из /me/permissions (токен их не несёт).
                await hydrate_session_permissions(client, cfg, data["access_token"])
                # E-D: регистрируем эту машину как устройство (best-effort).
                await register_device_best_effort(client)
            finally:
                await client.close()
            save_tokens(email, data["access_token"], data["refresh_token"])
            daemon_state = _ensure_daemon_after_login()
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
                "daemon": daemon_state,
            }

            def _render(_: dict) -> None:
                console.print(
                    f"[green]✓[/] Авторизован как {cfg.user_display_name or email}"
                )
                _print_daemon_hint(daemon_state)
                if data.get("is_new_user"):
                    console.print("  (новый пользователь, аккаунт создан)")
                roles_descr = []
                if cfg.is_hub_admin():
                    roles_descr.append("hub-admin")
                if cfg.permissions and not roles_descr:
                    roles_descr.append("member")
                console.print(f"  Роли:        {', '.join(roles_descr) or '—'}")
                console.print(f"  Permissions: {len(cfg.permissions)} прав")
                console.print(
                    "[dim]Доступные команды зависят от прав — `skillery --help`[/]"
                )

            emit_data(result, text_renderer=_render)

        _run(_do_invite())
        return

    if email is not None or password is not None:
        # --- password-flow ---
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

    if code is not None:
        # --- code-flow (прямой redeem) ---
        _do_code_login(cfg, code=code)
        return

    # --- browser-flow (по умолчанию) ---
    _do_browser_login(cfg)


def _do_code_login(cfg: ClientConfig, *, code: str) -> None:
    """Логин через exchange-code (прямой redeem без браузера).

    Web UI генерирует code на странице /cli-login, CLI редеемит его здесь.
    """

    async def _do() -> None:
        client = HubClient(base_url=cfg.base_url)
        try:
            data = await client.exchange_redeem(code=code)
            # JWT-slim: права — из /me/permissions (токен их не несёт).
            await hydrate_session_permissions(client, cfg, data["access_token"])
            # E-D: регистрируем эту машину как устройство (best-effort).
            await register_device_best_effort(client)
        finally:
            await client.close()

        # Почта учётки: сперва то, что уже пришло из /me (hydrate), иначе JWT.
        # ⚠️ ``sub`` в JWT-slim — ЧИСЛОВОЙ user_id: писать его как почту нельзя
        # (в боевом профиле так появилось `user_email = "1"`, и токены легли в
        # keyring под ключом «1»).
        claims = decode_jwt_claims(data["access_token"])
        user_email = _resolve_login_email(cfg, claims)
        if not user_email:
            _fail_invalid_login_email((claims or {}).get("sub"))

        save_tokens(user_email, data["access_token"], data["refresh_token"])
        cfg.user_email = user_email
        populate_from_jwt(cfg, data["access_token"])
        cfg.save()
        # #954: любой login-путь (не только invite/browser) поднимает демон +
        # автозапуск + watchdog — иначе устройство «офлайн», задания копятся.
        daemon_state = _ensure_daemon_after_login()

        result = {
            "event": "logged_in",
            "method": "code",
            "user_email": user_email,
            "is_hub_admin": cfg.is_hub_admin(),
            "is_skill_creator": cfg.is_skill_creator(),
            "permissions": cfg.permissions,
            "company_id": cfg.company_id,
            "role_id": cfg.role_id,
            "access_expires_at": cfg.access_expires_at,
        }

        def _render(_: dict) -> None:
            # Имя из /me (проставлено в hydrate); почта/номер — фолбэк.
            who = cfg.user_display_name or cfg.user_email or user_email
            console.print(f"[green]✓[/] Авторизован как {who}")
            # «Роли» = реальные платформенные роли. «skill-creator» — НЕ роль, а
            # способность (право skill.publish), она видна в Permissions. Раньше
            # тут печатался устаревший ярлык роли, которой нет.
            roles_descr = []
            if cfg.is_hub_admin():
                roles_descr.append("hub-admin")
            if cfg.permissions and not roles_descr:
                roles_descr.append("member")
            console.print(f"  Роли:        {', '.join(roles_descr) or '—'}")
            console.print(f"  Permissions: {len(cfg.permissions)} прав")
            _print_daemon_hint(daemon_state)

        emit_data(result, text_renderer=_render)

    _run(_do())


def _do_browser_login(cfg: ClientConfig) -> None:
    """Логин через browser-flow: открыть браузер на /cli-login, ждать callback.

    Запускает локальный HTTP-сервер на 127.0.0.1 со свободным портом,
    открывает браузер на {web_ui_url}/cli-login?port=…&state=…,
    ждёт callback на GET /callback?code=…&state=….
    """
    import webbrowser
    from skillery_cli.core.login_helpers import start_callback_server

    async def _do() -> None:
        # Запускаем callback-сервер
        server, port, state = start_callback_server()
        effective_web_ui_url = cfg.effective_web_ui_url()

        try:
            # Формируем URL для браузера
            browser_url = f"{effective_web_ui_url}/cli-login?port={port}&state={state}"

            # Открываем браузер
            opened = webbrowser.open(browser_url)

            if not opened:
                # Браузер не открылся — выдаём код и подсказку
                console.print(
                    "[yellow]Браузер не открылся автоматически.[/]\n"
                    f"Откройте эту ссылку вручную:\n  {browser_url}"
                )

            # Ждём callback (таймаут 180 сек)
            start_time = datetime.now(UTC)
            timeout_sec = 180
            while True:
                elapsed = (datetime.now(UTC) - start_time).total_seconds()
                if elapsed > timeout_sec:
                    raise RuntimeError(
                        f"Таймаут при ожидании callback'а ({timeout_sec}с). "
                        "Проверьте, что браузер открыл правильную ссылку."
                    )

                # Проверяем, получили ли результат
                if server.RequestHandlerClass.result:
                    break

                # Небольшая пауза перед следующей проверкой
                await asyncio.sleep(0.1)

            result_data = server.RequestHandlerClass.result
            if "error" in result_data:
                raise RuntimeError(
                    f"Ошибка при authenticate: {result_data['error']}"
                )

            code = result_data.get("code")
            if not code:
                raise RuntimeError("Code не получен из callback'а")

            # Редеемим код
            client = HubClient(base_url=cfg.base_url)
            try:
                data = await client.exchange_redeem(code=code)
                # JWT-slim: права — из /me/permissions (токен их не несёт).
                await hydrate_session_permissions(client, cfg, data["access_token"])
                # E-D: регистрируем эту машину как устройство (best-effort).
                await register_device_best_effort(client)
            finally:
                await client.close()

            # Почта учётки: /me (hydrate) → JWT ``email`` → JWT ``sub``, и только
            # если это РЕАЛЬНО почта (``sub`` в JWT-slim — числовой user_id).
            claims = decode_jwt_claims(data["access_token"])
            user_email = _resolve_login_email(cfg, claims)
            if not user_email:
                _fail_invalid_login_email((claims or {}).get("sub"))

            save_tokens(user_email, data["access_token"], data["refresh_token"])
            cfg.user_email = user_email
            populate_from_jwt(cfg, data["access_token"])
            cfg.save()

            daemon_state = _ensure_daemon_after_login()
            result = {
                "event": "logged_in",
                "method": "browser-flow",
                "daemon": daemon_state,
                "user_email": user_email,
                "is_hub_admin": cfg.is_hub_admin(),
                "is_skill_creator": cfg.is_skill_creator(),
                "permissions": cfg.permissions,
                "company_id": cfg.company_id,
                "role_id": cfg.role_id,
                "access_expires_at": cfg.access_expires_at,
            }

            def _render(_: dict) -> None:
                who = cfg.user_display_name or cfg.user_email or user_email
                console.print(f"[green]✓[/] Авторизован как {who}")
                _print_daemon_hint(daemon_state)
                roles_descr = []
                if cfg.is_hub_admin():
                    roles_descr.append("hub-admin")
                if cfg.permissions and not roles_descr:
                    roles_descr.append("member")
                console.print(f"  Роли:        {', '.join(roles_descr) or '—'}")
                console.print(f"  Permissions: {len(cfg.permissions)} прав")

            emit_data(result, text_renderer=_render)

        finally:
            # Выключаем сервер
            server.shutdown()

    _run(_do())


def _do_password_login(cfg: ClientConfig, *, email: str, password: str) -> None:
    # Ключ токенов в keyring — это почта; мусор туда писать нельзя (см.
    # ``_fail_invalid_login_email``). Проверяем ДО сетевого вызова.
    if not _is_valid_email(email):
        _fail_invalid_login_email(email)

    async def _do() -> None:
        client = HubClient(base_url=cfg.base_url)
        try:
            data = await client.login_password(email=email, password=password)
            # JWT-slim: токен не несёт прав — забираем эффективные из
            # /me/permissions тем же (теперь авторизованным) клиентом.
            await hydrate_session_permissions(client, cfg, data["access_token"])
            # E-D: регистрируем эту машину как устройство (best-effort).
            await register_device_best_effort(client)
        finally:
            await client.close()
        save_tokens(email, data["access_token"], data["refresh_token"])
        cfg.user_email = email
        populate_from_jwt(cfg, data["access_token"])
        cfg.save()
        # #954: password-login тоже поднимает демон + автозапуск + watchdog.
        daemon_state = _ensure_daemon_after_login()
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
            console.print(
                f"[green]✓[/] Авторизован как {cfg.user_display_name or email}"
            )
            roles_descr = []
            if cfg.is_hub_admin():
                roles_descr.append("hub-admin")
            if cfg.permissions and not roles_descr:
                roles_descr.append("member")
            console.print(f"  Роли:        {', '.join(roles_descr) or '—'}")
            console.print(f"  Permissions: {len(cfg.permissions)} прав")
            _print_daemon_hint(daemon_state)

        emit_data(result, text_renderer=_render)

    _run(_do())


def cmd_passwd() -> None:
    """Сменить (или установить) пароль текущего user'а.

    Требует залогиненную сессию. Запрашивает новый пароль интерактивно,
    с подтверждением. В JSON-режиме — необходима env-переменная
    SKILLERY_NEW_PASSWORD (для скриптов).
    """
    cfg = ClientConfig.load()
    if not cfg.is_logged_in():
        emit_error("NOT_LOGGED_IN", "Сначала залогиньтесь: skillery login")
        raise typer.Exit(1)
    access, _ = load_tokens(cfg.user_email or "")
    if not access:
        emit_error("NO_TOKEN", "Токен не найден. Сделайте login заново.")
        raise typer.Exit(1)

    if is_json():
        new_pw = os.environ.get(_branding.env("NEW_PASSWORD"))
        if not new_pw:
            emit_error(
                "VALIDATION",
                "В json-режиме новый пароль через env SKILLERY_NEW_PASSWORD",
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
                f"`skillery login --email {cfg.user_email} --password ***`"
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


def _backfill_identity(cfg: ClientConfig) -> None:
    """Тихо добрать display_name/email из /me и закэшировать в конфиг.

    Best-effort: сеть/токен недоступны → молча выходим (whoami покажет что есть).
    """
    async def _do() -> None:
        access, _ = load_tokens(cfg.user_email or "")
        if not access:
            return
        client = HubClient(base_url=cfg.base_url, access_token=access)
        try:
            me = await client.get_me()
        finally:
            await client.close()
        # /me отдаёт {user, claims, memberships} — профиль вложен в ``user``.
        u = me.get("user") if isinstance(me.get("user"), dict) else me
        name = (u.get("display_name") or "").strip()
        email = (u.get("email") or "").strip()
        changed = False
        if name and name != cfg.user_display_name:
            cfg.user_display_name = name
            changed = True
        if email and email != cfg.user_email:
            cfg.user_email = email
            changed = True
        if changed:
            with suppress(Exception):
                cfg.save()

    with suppress(Exception):
        _run(_do())


def cmd_whoami() -> None:
    """Кто я и что доступно."""
    import socket
    from skillery_cli.core.identity import device_uid

    cfg = ClientConfig.load()
    _maybe_notify_cli_update(cfg)
    if not cfg.user_email:
        emit_error("NOT_AUTHENTICATED", "Не авторизован")
        raise typer.Exit(1)
    # Ленивая подгрузка имени: у ранее залогиненных cfg.user_display_name пуст
    # (имя завезли позже), а в user_email мог лежать числовой id. Тихо доберём
    # профиль из /me и закэшируем — чтобы имя показалось без перелогина.
    if not cfg.user_display_name:
        _backfill_identity(cfg)
    payload = {
        "user_email": cfg.user_email,
        "user_display_name": cfg.user_display_name,
        "backend": cfg.base_url,
        "agent": cfg.agent or detect_agent(),
        "is_hub_admin": cfg.is_hub_admin(),
        "is_skill_creator": cfg.is_skill_creator(),
        "company_id": cfg.company_id,
        "role_id": cfg.role_id,
        "permissions": cfg.permissions,
        "auto_update": cfg.auto_update,
        "output_format": cfg.output_format,
        "device_id": device_uid(),
        "hostname": (socket.gethostname() or "").strip()[:120] or "cli",
    }

    def _render(p: dict) -> None:
        # Имя приоритетнее почты; почту показываем строкой ниже. Раньше в шапке
        # был числовой user_id (JWT sub) — «1» вместо имени.
        name = p.get("user_display_name") or p["user_email"]
        console.print(f"[bold]{name}[/]")
        if p.get("user_display_name") and p.get("user_email"):
            console.print(f"  Email:      {p['user_email']}")
        console.print(f"  Backend:    {p['backend']}")
        console.print(f"  Agent:      {p['agent']}")
        console.print(f"  Device:     {p['hostname']} (id:{p['device_id'][:8]}...)")
        # «Роли» = реальные платформенные роли. «skill-creator» — НЕ роль, а
        # способность (право skill.publish/skill.create): она видна в блоке
        # Permissions ниже, поэтому в роли её больше не пишем (иначе устаревший
        # ярлык роли, которой нет).
        roles = []
        if p["is_hub_admin"]:
            roles.append("hub-admin")
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


def cmd_devices() -> None:
    """Список зарегистрированных устройств пользователя."""
    from skillery_cli.core.identity import device_uid

    cfg = ClientConfig.load()
    _maybe_notify_cli_update(cfg)
    if not cfg.user_email:
        emit_error("NOT_AUTHENTICATED", "Не авторизован")
        raise typer.Exit(1)

    access, _refresh = load_tokens(cfg.user_email or "")
    if not access:
        emit_error("NO_TOKEN", "Токен не найден. Сделайте login заново.")
        raise typer.Exit(1)

    async def _do() -> list[dict[str, Any]]:
        client = HubClient(
            base_url=cfg.base_url,
            access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        try:
            return await client.list_devices()
        finally:
            await client.close()

    devices = asyncio.run(_do())
    current_device_id = device_uid()

    # Отсортировать: текущее устройство первым, затем по last_seen_at (новые сверху)
    devices_sorted = sorted(
        devices,
        key=lambda d: (
            not (d.get("client_device_id") == current_device_id or d.get("is_current")),
            -(
                int(d.get("last_seen_at", 0))
                if isinstance(d.get("last_seen_at"), int)
                else 0
            ),
        ),
    )

    payload = {"devices": devices_sorted, "current_device_id": current_device_id}

    def _render(p: dict) -> None:
        current_id = p["current_device_id"]
        for dev in p["devices"]:
            is_current = (
                dev.get("client_device_id") == current_id or dev.get("is_current")
            )
            marker = "* " if is_current else "  "
            # Бэкенд отдаёт `name` (device_name — легаси-имя поля): без этого
            # fallback'а весь список назывался «Unknown».
            name = dev.get("name") or dev.get("device_name") or "Unknown"
            platform = dev.get("platform", "unknown")
            last_seen = dev.get("last_seen_at", "—")
            session_active = dev.get("session_active", False)
            status_mark = "(this PC)" if is_current else ""
            session_status = " · сессия активна" if session_active else ""
            # #906: онлайн = устройство недавно опрашивало очередь заданий.
            online_status = " · на связи" if dev.get("online") else " · офлайн"
            console.print(
                f"{marker}{name} [{platform}]{online_status}{session_status}"
                f" · {last_seen} {status_mark}"
            )

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
            "Сначала залогиньтесь: skillery login <invite-token>",
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
    _maybe_notify_cli_update(cfg)
    target = get_target(cfg.agent)
    actual_project = (
        project
        or (Path(cfg.default_project_dir) if cfg.default_project_dir else None)
        or Path.cwd()
    )
    global_items = _scan_installed(target, project=None)
    project_items = _scan_installed(target, project=actual_project)
    # Что демон поставил в фоне (его onboarding-печать ушла бы в DEVNULL) —
    # показываем агенту/пользователю здесь: что установилось и что доделать.
    pending = _read_pending_onboarding()
    # #1249: команда навыка, перехваченная одноимённым бинарём из чужого
    # каталога, работает «как будто нормально», но идёт мимо учёта. Молчаливой
    # эта потеря быть не должна — показываем в обычном статусе.
    from skillery_cli.core import shim_collisions

    collisions = [c.as_dict() for c in shim_collisions.detect()]
    # #1441: расхождение контракта CLI↔backend (404/405 на фоновом пути демона).
    # Тихий отказ здесь неотличим от «нет заданий», поэтому флаг обязан быть
    # виден в обычном статусе — это единственное место, куда смотрят.
    stale_routes = route_health.unknown_routes()
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
        "pending_onboarding": pending,
        "shim_collisions": collisions,
        "stale_routes": stale_routes,
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
        stale = p.get("stale_routes") or []
        if stale:
            console.print("")
            console.print(
                f"[bold red]⚠ Backend не знает {len(stale)} маршрут(ов) — "
                "устройство МОЖЕТ НЕ ПОЛУЧАТЬ задания:[/]"
            )
            for r in stale:
                console.print(
                    f"  • [bold]{r.get('route')}[/] → {r.get('status')} "
                    f"(раз: {r.get('count')}, последний: {r.get('last_seen')})"
                )
            console.print(
                "  Починить: [bold]skillery self upgrade[/] "
                "— CLI отстал от хаба по контракту API"
            )
        coll = p.get("shim_collisions") or []
        if coll:
            console.print("")
            console.print(
                f"[bold yellow]⚠ Команды навыков перехвачены ({len(coll)}) — "
                "эти вызовы НЕ учитываются:[/]"
            )
            for c in coll:
                console.print(
                    f"  • [bold]{c['command']}[/] → {c['winner']} "
                    f"(вместо навыка «{c['skill_slug']}»)"
                )
            console.print(
                "  Починить: [bold]skillery doctor --fix-path-order[/] "
                "· или звать навык явно: skillery run <slug>"
            )
        pend = p.get("pending_onboarding") or []
        if pend:
            console.print("")
            console.print(
                f"[bold cyan]🆕 Установлено в фоне ({len(pend)}) — что доделать:[/]"
            )
            for it in pend:
                head = it.get("slug") or "—"
                if it.get("summary"):
                    head += f" — {it['summary']}"
                console.print(f"  • [bold]{head}[/]")
                for i, step in enumerate(it.get("next_steps") or [], 1):
                    console.print(f"      {i}. {step}")
                if it.get("docs"):
                    console.print(f"      Документация: {it['docs']}")

    emit_data(payload, text_renderer=_render)
    # Показали (в тексте И в json-payload) — вычищаем: агент/пользователь узнали,
    # висеть вечно журналу незачем. Понадобится снова — в SKILL.md навыка.
    if pending:
        _clear_pending_onboarding()


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
    if not _is_valid_email(email):
        _fail_invalid_login_email(email)
    save_tokens(email, access, refresh)
    cfg.user_email = email
    populate_from_jwt(cfg, access)

    # JWT-slim: права не в токене — забираем из /me/permissions. [ADVANCED]-
    # команда offline-толерантна: недостижимый backend → пустые права (не валим).
    async def _hydrate() -> None:
        client = HubClient(base_url=cfg.base_url)
        try:
            await hydrate_session_permissions(client, cfg, access)
        finally:
            await client.close()

    try:
        asyncio.run(_hydrate())
    except Exception:  # noqa: BLE001 — offline-tolerant advanced command
        cfg.permissions = []

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


def _dedupe_installed_by_realpath(items: list[dict]) -> list[dict]:
    """Убрать дубли навыков по РЕАЛЬНОМУ пути на диске.

    Задвоение: `--installed` сканирует global (`~/.claude/skills/`) и project
    (`<project>/.claude/skills/`) РАЗДЕЛЬНО. Но когда project scope резолвится в
    ту же физическую папку, что и global (команда запущена из home; или
    `.claude/skills` проекта — junction/symlink на глобальную), один и тот же
    навык попадал в вывод дважды — как «global» и как «project» с ОДНИМ путём.

    Дедупим по ``realpath`` (резолвит junction/symlink) + ``normcase`` (Windows
    регистронезависим). Порядок сохраняем — global сканируется первым, поэтому
    у одинаковой физической папки остаётся канонический scope=global. Реальные
    раздельные установки (разные физические папки) не схлопываются.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        raw = it.get("path") or ""
        try:
            key = os.path.normcase(os.path.realpath(raw))
        except Exception:  # noqa: BLE001 — на кривом пути не роняем листинг
            key = os.path.normcase(raw)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _unwrap_list_payload(payload: object) -> list:
    """Развернуть ответ листинга в список строк, устойчиво к форме.

    Backend может отдавать envelope ``{"items": [...]}`` (W5-пагинация) или,
    исторически, голый ``list``. Любая другая/грязная форма (``items: null``,
    не-список) → ``[]``, чтобы текстовый рендер не падал ``TypeError``.
    """
    rows = payload.get("items") if isinstance(payload, dict) else payload
    return rows if isinstance(rows, list) else []


def _fmt_tag_labels(raw: object) -> str:
    """«label, label» из tag-ref'ов. Робастно к ``None`` и не-dict-элементам."""
    items = raw if isinstance(raw, list) else []
    return ", ".join(
        t.get("label", "") if isinstance(t, dict) else str(t) for t in items
    )


def _fmt_version_semvers(raw: object) -> str:
    """«semver, semver» из версий. Робастно к ``None`` и не-dict-элементам."""
    items = raw if isinstance(raw, list) else []
    return ", ".join(
        str(v.get("semver", "")) if isinstance(v, dict) else str(v) for v in items
    )


def cmd_list(
    channel: str = typer.Option("published"),
    installed: bool = typer.Option(
        False, "--installed",
        help="[устаревшее] Псевдоним «skillery skill installed» — та же выдача "
             "(центральный стор + project scope), тот же --scope.",
    ),
    project: Optional[Path] = typer.Option(
        None, "--project", help="Project root для скана project-scope установок"
    ),
    scope: Optional[str] = typer.Option(
        None, "--scope", help="global | project | all (default: all для --installed)"
    ),
) -> None:
    """Список доступных skills (RBAC), либо --installed (псевдоним `installed`).

    #1405: ``--installed`` больше НЕ отдельный сканер. Раньше он ходил по
    каталогам агента, а ``skillery installed`` — по центральному стору, и один
    вопрос получал два разных ответа (16 записей против 8) — как будто навыки
    потерялись. Теперь оба ответа собирает ``commands.installed``, а scope
    подписан в заголовке.
    """
    cfg = ClientConfig.load()
    if installed:
        from skillery_cli.commands.installed import collect_installed, render_installed

        payload = collect_installed(cfg, scope=scope or "all", project=project)
        emit_message(
            "«list --installed» — устаревший псевдоним; используйте "
            "«skillery skill installed»",
            level="warn",
        )
        emit_data(payload, text_renderer=lambda p: render_installed(console, p))
        return

    _maybe_auto_update(cfg, project=project)
    _maybe_notify_cli_update(cfg)
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

        def _render(payload) -> None:
            # backend отдаёт envelope {"items": [...]} (+ pagination); разворачиваем
            rows = _unwrap_list_payload(payload)
            table = Table(title=f"Доступные skills (channel={channel})")
            table.add_column("slug")
            table.add_column("title")
            table.add_column("tags")
            table.add_column("versions")
            for s in rows:
                if not isinstance(s, dict):
                    continue
                table.add_row(
                    str(s.get("slug") or "—"),
                    str(s.get("title") or "—"),
                    # tags — объекты {id,label,...}, не строки
                    _fmt_tag_labels(s.get("tags")),
                    _fmt_version_semvers(s.get("versions")),
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
    _maybe_notify_cli_update(cfg)
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
            console.print(f"  tags:        {_fmt_tag_labels(d.get('tags')) or '—'}")
            console.print(f"  repo:        {d.get('repo_url') or '—'}")
            console.print(f"  is_super:    {d.get('is_super')}")
            console.print("  versions:")
            for v in d.get("versions") or []:
                if not isinstance(v, dict):
                    console.print(f"    • {v}")
                    continue
                semver = str(v.get("semver") or "—")
                channel_v = str(v.get("channel") or "—")
                commit = str(v.get("commit_sha") or "")[:8] or "—"
                console.print(f"    • {semver:10} channel={channel_v:10} commit={commit}")

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
    from skillery_cli.core.manifest_builder import (
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


_TOOLING_KEYS = ("kind", "cli", "mcp", "runtime_dependencies", "onboarding")


def _merge_tooling_from_store(manifest: dict | None, store_dir) -> dict:
    """Дополнить манифест tooling-полями из ``_skill_meta.toml`` в сторе.

    #889: ЕДИНЫЙ источник истины для tooling — декларация в САМОМ навыке
    (``_skill_meta.toml`` рядом с SKILL.md), а не только то, что доехало в
    манифесте бандла хаба. У навыка с подпапкой в репо (``skill_path``, напр.
    ``skills/atlas``) бандл мог прийти БЕЗ cli/runtime_dependencies — тогда CLI
    навыка не ставился. Так хаб-путь и локальный (``--path``/``--from-git``)
    сходятся на одном источнике.

    Значения из декларации навыка имеют приоритет (они точнее). Ошибка чтения —
    не фатальна: возвращаем исходный манифест.
    """
    out = dict(manifest or {})
    if store_dir is None:
        return out
    try:
        from skillery_cli.core.manifest_builder import _read_meta_toml

        meta = _read_meta_toml(store_dir) or {}
    except Exception:  # noqa: BLE001 — декларация опциональна
        return out
    for key in _TOOLING_KEYS:
        if meta.get(key):
            out[key] = meta[key]
    return out


def _pending_onboarding_path() -> Path:
    """Журнал onboarding'а навыков, поставленных ФОНОВО (демоном).

    Демон headless (stdout → DEVNULL), поэтому напечатанный `_emit_onboarding`
    блок «что дальше» терялся — агент не узнавал, что поставилось в фоне и что
    доделать. Демон пишет его сюда, `skillery status` показывает и вычищает.
    """
    from skillery_cli.daemon.daemon_runner import _default_data_dir

    return _default_data_dir() / "pending-onboarding.json"


def _record_pending_onboarding(entry: dict) -> None:
    """Добавить onboarding фоновой установки (dedupe по slug, cap 50)."""
    import json

    p = _pending_onboarding_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa: BLE001
        data = {}
    items = [it for it in (data.get("items") or []) if isinstance(it, dict)]
    # Свежая запись вытесняет прежнюю по тому же slug (не копим дубли апдейтов).
    items = [it for it in items if it.get("slug") != entry.get("slug")]
    items.append(entry)
    items = items[-50:]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"items": items}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001 — журнал не критичен
        pass


def _read_pending_onboarding() -> list[dict]:
    """Прочитать журнал фонового onboarding'а (устойчиво к битому файлу)."""
    import json

    p = _pending_onboarding_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:  # noqa: BLE001
        return []
    return [it for it in (data.get("items") or []) if isinstance(it, dict)]


def _clear_pending_onboarding() -> None:
    """Вычистить журнал (агент/пользователь увидели в `status`)."""
    p = _pending_onboarding_path()
    try:
        if p.exists():
            p.unlink()
    except Exception:  # noqa: BLE001
        pass


def _emit_onboarding(
    manifest: dict | None, *, slug: str, headless: bool = False
) -> None:
    """Онбординг «что делать дальше» ПОСЛЕ установки навыка (#889).

    Источник — декларация навыка в ``_skill_meta.toml``::

        [onboarding]
        summary = "Atlas - локальный PM портфеля проектов и задач."
        next_steps = [
          "atlas setup         # правила агента + SessionStart-хук",
          "atlas task triage   # что в работе",
        ]
        docs = "https://github.com/<owner>/<repo>#readme"

    Цель — чтобы ИИ-агент, поставивший навык, САМ довёл настройку до конца.

    Контракт вывода: ``emit_message`` — в text-режиме человекочитаемый список,
    в json-режиме структурная строка в **stderr** (``next_steps`` массивом), так
    что stdout с основным payload'ом не засоряется и парсинг агентом не ломается.
    """
    ob = (manifest or {}).get("onboarding")
    if not isinstance(ob, dict):
        return
    steps = [str(s) for s in (ob.get("next_steps") or []) if str(s).strip()]
    summary = str(ob.get("summary") or "").strip()
    docs = str(ob.get("docs") or "").strip()
    if not (steps or summary or docs):
        return
    if headless:
        # Демон: stdout → DEVNULL, печать потерялась бы. Пишем в журнал, чтобы
        # `skillery status` показал агенту/пользователю, что поставилось в фоне.
        _record_pending_onboarding(
            {
                "slug": slug,
                "summary": summary,
                "next_steps": steps,
                "docs": docs or None,
                "ts": datetime.now(UTC).isoformat(),
            }
        )
        return
    lines = [f"Навык «{slug}» установлен. Что дальше:"]
    if summary:
        lines.append(f"  {summary}")
    for idx, step in enumerate(steps, 1):
        lines.append(f"  {idx}. {step}")
    if docs:
        lines.append(f"  Документация: {docs}")
    emit_message(
        "\n".join(lines),
        level="info",
        skill=slug,
        next_steps=steps,
        docs=docs or None,
    )


_CLI_PKG_SRC_DIR = ".pkgsrc"  # store/<slug>/.pkgsrc — корень репо с pyproject пакета


def _persist_cli_package_source(repo_root: Path, result) -> None:
    """Сохранить корень репо (pyproject+src) в ``store/<slug>/.pkgsrc``.

    Из него ``_apply_tooling`` ставит CLI-пакет через ``uv tool install`` (как
    install-скрипт навыка). Best-effort: сбой копирования не валит установку самого
    навыка — просто не будет рабочей команды (и это честно отразит self-check).
    """
    try:
        import shutil

        store_dir = getattr(result, "store_dir", None)
        if store_dir is None or not (Path(repo_root) / "pyproject.toml").is_file():
            return  # корень не пакет — нечего сохранять (доки уже материализованы)
        dest = Path(store_dir) / _CLI_PKG_SRC_DIR
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        # Копируем корень БЕЗ тяжёлого/ненужного для сборки (venv, .git, кэши).
        shutil.copytree(
            repo_root,
            dest,
            ignore=shutil.ignore_patterns(
                ".git", ".venv", "venv", "__pycache__", "*.pyc", "node_modules",
                ".pytest_cache", ".ruff_cache", "*.egg-info",
            ),
        )
    except Exception:  # noqa: BLE001 — источник пакета best-effort
        pass


def _cli_package_root(store_dir) -> Path | None:
    """Корень CLI-пакета навыка: ``store/<slug>/.pkgsrc`` (монорепо) или сам стор
    (навык-в-корне). ``None`` — если pyproject нет (навык не несёт своего пакета)."""
    if store_dir is None:
        return None
    for cand in (Path(store_dir) / _CLI_PKG_SRC_DIR, Path(store_dir)):
        if (cand / "pyproject.toml").is_file():
            return cand
    return None


def _manifest_cli_command_names(manifest: dict | None) -> list[str]:
    """Имена команд из ``manifest['cli']`` (command_name)."""
    names: list[str] = []
    for tool in (manifest or {}).get("cli") or []:
        name = tool.get("command_name") if isinstance(tool, dict) else None
        if name:
            names.append(str(name))
    return names


def _manifest_without_cli(manifest: dict | None, drop_names: set[str]) -> dict | None:
    """Копия манифеста без уже поставленных (через uv tool) CLI — чтобы skillkit не
    писал рукодельный shim поверх рабочей команды uv. MCP/runtime_deps сохраняются."""
    if not manifest or not drop_names:
        return manifest
    clis = manifest.get("cli") or []
    kept = [
        t for t in clis
        if not (isinstance(t, dict) and str(t.get("command_name") or "") in drop_names)
    ]
    out = dict(manifest)
    out["cli"] = kept
    return out


def _remove_stale_shim(command_name: str) -> None:
    """Убрать устаревший skillery-shim ``~/.skillery/bin/<cmd>.cmd``+sidecar.

    После установки команды через ``uv tool`` рукодельный shim прошлого (битого)
    install больше не нужен и, оставшись, мог бы затенять рабочую uv-команду в PATH
    (uv-tool-bin у пользователя на PATH — там же лежит сам ``skillery``). Best-effort.
    """
    try:
        from skillery_cli.core._kit_config import _cli_bin_dir

        bin_dir = _cli_bin_dir()
        for fn in (f"{command_name}.cmd", command_name, f"{command_name}.json"):
            p = bin_dir / fn
            if p.is_file():
                p.unlink()
    except Exception:  # noqa: BLE001 — чистка shim best-effort
        pass


def _report_cli_package(rep: dict, expected: list[str]) -> None:
    """Человекочитаемая сводка установки CLI-пакета через uv tool + PATH-подсказка."""
    status = rep.get("status")
    if status == "installed":
        cmds = ", ".join(c["name"] for c in rep.get("commands") or []) or "команда"
        emit_message(f"CLI «{cmds}» установлен (uv tool) и доступен из любой директории.",
                     level="info")
        not_on_path = [c["name"] for c in rep.get("commands") or [] if not c.get("on_path")]
        if not_on_path:
            emit_message(
                f"Команда {', '.join(not_on_path)} появится после обновления PATH "
                "(uv tool bin). Откройте новый терминал или выполните `uv tool update-shell`.",
                level="warn",
            )
    elif status == "error":
        emit_message(
            f"CLI навыка ({', '.join(expected) or '?'}) не установлен: {rep.get('reason', '')}",
            level="warn",
        )


def _apply_tooling(
    result, manifest: dict | None, *, agent_target, project,
    log_file: str = "cli.log", initiator: str = "cli",
) -> None:
    """поставить CLI/MCP/runtime-deps навыка (если он tooling). Graceful.

    Свой CLI-пакет навыка (pyproject в корне репо, сохранён в ``.pkgsrc``) ставим
    ``uv tool install --force <корень>`` — ровно как install-скрипт навыка: чистый
    изолированный tool-venv + команда на PATH. Остальное (MCP, runtime-deps, и CLI
    без своего пакета) — через skillkit; из манифеста для него убираем уже
    поставленные команды, чтобы не писать битый shim поверх рабочей uv-tool-команды.

    Ошибка установки артефактов НЕ ломает установку самого навыка (warn, degradation).

    ``log_file`` — куда писать аудит установки (SK-5): ``cli.log`` (foreground) или
    ``daemon.log`` (фоновый демон). Шаги/итог пишутся ВСЕГДА (install_logger, INFO),
    чтобы тихий пропуск установки пакета был виден в логе.
    """
    from skillery_cli.core.logging_setup import install_logger

    ilog = install_logger(log_file)
    installed_cli: set[str] = set()
    store_dir = getattr(result, "store_dir", None)
    pkg_root = _cli_package_root(store_dir)
    cmd_names = _manifest_cli_command_names(manifest)
    if pkg_root is not None and cmd_names:
        try:
            rep = cli_package_install.install_cli_package(
                pkg_root, command_names=cmd_names
            )
        except Exception as exc:  # noqa: BLE001 — пакет не валит install навыка
            rep = {"status": "error", "reason": str(exc), "package": None, "commands": []}
        _report_cli_package(rep, cmd_names)
        # Аудит: результат uv tool install (rc/package/self-check). WARNING на провал
        # — ровно то, чего не хватало для диагностики тихого пропуска (SK-2).
        _ctx = {
            "step": "cli_package", "commands": cmd_names, "initiator": initiator,
            "status": rep.get("status"), "package": rep.get("package"),
            "self_check": rep.get("commands"), "reason": rep.get("reason") or "",
        }
        if rep.get("status") == "installed":
            ilog.info("установлен CLI-пакет навыка (uv tool)", extra={"context": _ctx})
            installed_cli = set(cmd_names)
            # Убрать устаревший skillery-shim прошлого (битого) install — он бы
            # затенял рабочую uv-tool-команду в PATH.
            for name in cmd_names:
                _remove_stale_shim(name)
        else:
            ilog.warning("CLI-пакет навыка НЕ установлен", extra={"context": _ctx})

    kit_manifest = _manifest_without_cli(manifest, installed_cli)
    try:
        report = tooling_install.apply_tooling_artifacts(
            result, agent_target=agent_target, project=project, manifest=kit_manifest
        )
    except Exception as exc:  # noqa: BLE001 — артефакты не валят install навыка
        ilog.warning("сбой доустановки CLI/MCP/зависимостей", extra={
            "context": {"step": "tooling_artifacts", "initiator": initiator,
                        "error": str(exc)}})
        emit_message(
            f"Не удалось доустановить CLI/MCP/зависимости навыка: {exc}",
            level="warn",
        )
        return
    # Аудит MCP/runtime-deps (кол-во зарегистрированных/поставленных/провалов).
    _deps = report.get("deps") or {}
    ilog.info("tooling-артефакты применены", extra={"context": {
        "step": "tooling_artifacts", "initiator": initiator,
        "cli": [c.get("command_name") for c in report.get("cli") or []],
        "mcp": [mm.get("server_name") for mm in report.get("mcp") or []],
        "deps_failed": [d.get("spec") for d in _deps.get("failed") or []],
    }})
    _report_tooling(report)


def _warn_shim_collision(command_name: str, skill_slug: str = "") -> None:
    """Предупредить, если команду навыка перехватывает чужой одноимённый бинарь.

    Best-effort: диагностика не имеет права сорвать установку — детект и так
    не бросает, но импорт держим локальным и глушим на всякий случай.
    """
    try:
        from skillery_cli.core import shim_collisions

        for c in shim_collisions.detect_for_command(command_name, skill_slug):
            emit_message(shim_collisions.describe(c), level="warn")
    except Exception:  # noqa: BLE001 — предупреждение, а не исключение
        pass


def _report_tooling(report: dict) -> None:
    """Человекочитаемая сводка отчёта apply_tooling_artifacts (text + warn)."""
    for cli in report.get("cli") or []:
        name = cli.get("command_name", "?")
        status = cli.get("status")
        if status == "installed":
            emit_message(f"CLI «{name}» доступен из любой директории.", level="info")
            path_info = cli.get("path") or {}
            if path_info.get("status") == "manual-needed":
                emit_message(
                    "Каталог CLI не в PATH. Добавьте его: "
                    f"{path_info.get('instruction', '')} (или `skillery doctor --fix-path`).",
                    level="warn",
                )
            # #1249: shim положили — но выиграет он только если наш каталог в
            # PATH раньше чужого. Сказать об этом надо ЗДЕСЬ, а не когда
            # пользователь удивится нулевым запускам.
            _warn_shim_collision(name)
        elif status == "conflict":
            # #2272: команду занял ДРУГОЙ навык (или она заведена вручную). Кит
            # чужое не перезаписывает — но пользователь обязан узнать, почему
            # команда навыка не появилась, иначе «не работает» без объяснения.
            emit_message(
                f"CLI «{name}» не поставлен: {cli.get('reason', '')}",
                level="warn",
            )
        elif status in ("error", "skipped"):
            emit_message(
                f"CLI «{name}» не поставлен ({status}): {cli.get('reason', '')}",
                level="warn",
            )
    for mcp in report.get("mcp") or []:
        name = mcp.get("server_name", "?")
        status = mcp.get("status")
        if status == "registered":
            emit_message(f"MCP-сервер «{name}» зарегистрирован в агенте.", level="info")
        elif status == "manual":
            emit_message(mcp.get("instruction", f"MCP «{name}»: см. инструкцию."), level="warn")
        elif status == "conflict":
            # #2272: сервер с таким именем уже в конфиге агента и принадлежит не
            # нам (другой навык или ручная настройка пользователя).
            emit_message(
                f"MCP «{name}» не зарегистрирован: {mcp.get('reason', '')}", level="warn"
            )
        elif status == "error":
            emit_message(
                f"MCP «{name}» не зарегистрирован: {mcp.get('reason', '')}", level="warn"
            )
    deps = report.get("deps") or {}
    for d in deps.get("skipped") or []:
        emit_message(
            f"Зависимость {d.get('spec')} ({d.get('kind')}) не поставлена: "
            f"{d.get('instruction', d.get('reason', ''))}",
            level="warn",
        )
    for d in deps.get("failed") or []:
        emit_message(
            f"Зависимость {d.get('spec')} ({d.get('kind')}) — ошибка установки: "
            f"{d.get('reason', '')}",
            level="warn",
        )


def _revert_tooling(slug: str, *, agent_target, project, store_dir: Path) -> None:
    """снять CLI/MCP навыка при disable/remove (по манифесту из стора).

    Манифест читается из ``_skill_meta.json`` стора (он ещё на месте на момент
    снятия ссылки / до purge). Никогда не бросает — снятие навыка важнее.
    """
    try:
        meta = read_meta(store_dir) or {}
        manifest = meta.get("manifest")
        try:
            # #2272: кит снимает CLI/MCP только при совпадении владельца —
            # для этого ему нужен slug снимаемого навыка.
            report = tooling_install.revert_tooling_artifacts(
                manifest,
                agent_target=agent_target,
                project=project,
                skill_slug=slug,
            )
        except TypeError:
            # Кит старее гейта принадлежности (s-skillkit без skill_slug) —
            # деградируем к прежнему поведению, а не роняем снятие навыка.
            report = tooling_install.revert_tooling_artifacts(
                manifest, agent_target=agent_target, project=project
            )
        for conflict in (report or {}).get("conflicts") or []:
            kind = "CLI" if conflict.get("artifact") == "cli" else "MCP"
            owner = conflict.get("owner")
            whose = f"навыку «{owner}»" if owner else "не нам (заведено вручную)"
            emit_message(
                f"{kind} «{conflict.get('name')}» оставлен: принадлежит {whose}.",
                level="warn",
            )
    except Exception as exc:  # noqa: BLE001 — откат артефактов не валит снятие
        emit_message(f"Не удалось снять CLI/MCP навыка «{slug}»: {exc}", level="warn")
    # Свой CLI-пакет навыка ставился через `uv tool install` (не shim) — снимаем
    # его симметрично `uv tool uninstall <project.name>`, иначе команда осталась бы.
    try:
        pkg_root = _cli_package_root(store_dir)
        info = cli_package_install.read_pyproject_cli(pkg_root) if pkg_root else None
        if info:
            cli_package_install.uninstall_cli_package(info["name"])
    except Exception as exc:  # noqa: BLE001 — снятие пакета best-effort
        emit_message(f"Не удалось снять CLI-пакет навыка «{slug}»: {exc}", level="warn")


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
        # tags/description из frontmatter → в manifest меты: по ним onboard
        # матчит локально установленные навыки (live-smoke bug Phase E).
        from skillery_cli.core.manifest_builder import (
            _read_frontmatter,
            _read_meta_toml,
        )

        fm = _read_frontmatter(skill_dir / "SKILL.md")
        manifest: dict[str, object] = {"version": version, "files": []}
        # локальный tooling-навык несёт cli/mcp/runtime_deps + kind в
        # _skill_meta.toml — прокидываем их в manifest, чтобы _apply_tooling
        # поставил CLI/MCP/зависимости (frontmatter их не содержит).
        meta_toml = _read_meta_toml(skill_dir)
        for key in ("kind", "cli", "mcp", "runtime_dependencies"):
            if meta_toml.get(key):
                manifest[key] = meta_toml[key]
        if fm.get("tags"):
            tags = fm["tags"]
            if isinstance(tags, list):
                manifest["tags"] = [str(t).strip() for t in tags]
            else:
                # Самописный frontmatter-парсер отдаёт inline-список строкой
                # "[a, b]" — срезаем скобки и сплитим по запятой.
                raw = str(tags).strip().strip("[]")
                manifest["tags"] = [
                    s.strip().strip("\"'") for s in raw.split(",") if s.strip()
                ]
        if fm.get("description"):
            manifest["description"] = str(fm["description"]).strip()
        result = installer.install(
            slug=slug, version=version, commit_sha="",
            repo_url=None, local_src=skill_dir, manifest=manifest,
            project=project_path, force=force,
        )
    else:  # git
        version = "0.0.0-local"
        # "" → дефолтная ветка репо (без --ref); иначе явный ref.
        ref = source.get("ref") or ""
        manifest = {"version": version, "files": []}
        result = installer.install(
            slug=slug, version=version, commit_sha="",
            repo_url=source["url"], git_ref=ref,
            manifest=manifest,
            project=project_path, force=force,
        )
    # tooling-навык (локальный источник) → CLI/MCP/runtime-deps. Декларация
    # навыка (_skill_meta.toml в сторе) — источник истины (#889, единый хелпер).
    tooling_manifest = _merge_tooling_from_store(manifest, result.store_dir)
    _apply_tooling(
        result, tooling_manifest, agent_target=agent_target, project=project_path
    )
    _emit_onboarding(tooling_manifest, slug=slug)

    # Источник установки для аналитики (ось «source»): path → local-path,
    # git → git-url (зеркалит installer'ский meta.source).
    src_source = "local-path" if source["kind"] == "path" else "git-url"
    agent_name = agent_target.name
    if result.is_update:
        track_skill_event(
            "skill.update", slug=slug, version=result.version,
            scope=result.scope, source=src_source, agent=agent_name,
        )
    else:
        # Материализация в стор.
        track_skill_event(
            "skill.install", slug=slug, version=result.version,
            scope=result.scope, source=src_source, agent=agent_name,
        )
    # Включение-в-проект (project-линк) — отдельное событие skill.enable.
    if result.scope == "project":
        track_skill_event(
            "skill.enable", slug=slug, version=result.version,
            scope="project", source=src_source, agent=agent_name,
        )
    return [{
        "slug": slug, "skill_id": result.skill_id,
        "version": result.version, "is_update": result.is_update,
        "target_dir": str(result.target_dir), "scope": result.scope,
        "linked": result.linked, "link_kind": result.link_kind,
        "source": source["kind"],
    }]


def _extract_snapshot_subdir(
    archive: Path, into: Path, skill_path: str
) -> Optional[tuple[Path, Path]]:
    """Распаковать снапшот и вернуть ``(подпапка навыка, корень репо)``.

    Снапшот — tar.gz ВСЕГО репозитория (бэкенд снял его под своим токеном). Для
    монорепо-навыка нужный SKILL.md лежит в подпапке. Извлекаем архив, находим
    корень (у git-архива это единственная папка ``repo-<sha>/``), спускаемся в
    ``skill_path`` и проверяем, что там есть SKILL.md. ``None`` ⇒ подпапки/навыка
    в архиве нет → вызывающий откатится на git clone.

    Возвращаем и КОРЕНЬ репо: для tooling-навыка в корне лежит его CLI-пакет
    (``pyproject.toml``+``src/``), из которого ставится команда (``uv tool install
    --force <корень>``, как install-скрипт навыка) — раньше корень выбрасывался.

    Защита от path traversal: имена членов архива с ``..``/абсолютные — отвергаем,
    и итоговый путь обязан оставаться внутри распакованного дерева.
    """
    import tarfile

    dest = into / "unpacked"
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(archive, mode="r:gz") as tar:
            for member in tar.getmembers():
                name = member.name
                if name.startswith("/") or ".." in Path(name).parts:
                    return None  # подозрительный архив — не рискуем
            # filter="data" (py3.12+) — безопасная распаковка; на 3.11 параметра
            # нет, но членов мы уже провалидировали выше.
            try:
                tar.extractall(dest, filter="data")  # noqa: S202
            except TypeError:
                tar.extractall(dest)  # noqa: S202 — py<3.12
    except Exception:  # noqa: BLE001 — битый архив → откат на clone
        return None

    # Корень: если распаковалась ровно одна папка (git-архив) — спускаемся в неё.
    entries = [p for p in dest.iterdir()]
    root = entries[0] if len(entries) == 1 and entries[0].is_dir() else dest

    rel = Path(skill_path.strip("/"))
    if ".." in rel.parts or rel.is_absolute():
        return None
    sub = (root / rel).resolve()
    # Итоговый путь обязан лежать внутри дерева распаковки.
    try:
        sub.relative_to(dest.resolve())
    except ValueError:
        return None
    if not sub.is_dir() or not (sub / "SKILL.md").exists():
        return None
    return sub, root


#: Источники, которые хаб-установка ВЫТЕСНЯЕТ (предварительно сохранив в резерв).
_FOREIGN_SOURCES = ("local-path", "git-url")


def _backup_foreign_before_hub(
    store_root: Path, dir_name: str
) -> Optional[dict]:
    """Убрать в резерв каталог стора, занятый навыком ИЗ ДРУГОГО ИСТОЧНИКА.

    #1405. Установка из Хаба главнее локальной, но «главнее» не значит «затирает
    молча». Раньше хаб-установка поверх ``install --path`` попадала в ветку
    ИНКРЕМЕНТАЛЬНОГО обновления кита (стор уже наш ⇒ ``_do_update``) и
    перезаписывала чужое дерево поверх — вернуться было некуда. Теперь прежний
    каталог целиком уезжает в ``<стор>/.backups/``, а хаб ставится с чистого
    листа.

    ``None`` — вытеснять нечего: каталога нет, он уже хабовый или это не наша
    установка (без ``_skill_meta.json`` кит и сам решает сам, мы не лезем).
    Никогда не бросает: сорванный резерв не имеет права отменить установку —
    но тогда и вытеснения не будет (вызывающий получит ``None``).
    """
    from skillery_cli.core.store_backup import backup_store_skill

    target = Path(store_root) / dir_name
    if not target.is_dir():
        return None
    try:
        meta = read_meta(target)
    except Exception:  # noqa: BLE001 — битая мета: решает кит, не мы
        return None
    if not meta or str(meta.get("source") or "") not in _FOREIGN_SOURCES:
        return None
    try:
        return backup_store_skill(
            store_root, dir_name, reason="hub-takeover", meta=meta
        )
    except Exception:  # noqa: BLE001 — не смогли зарезервировать → не вытесняем
        return None


def _backup_foreign_scope_before_hub(
    store_root: Path, dir_name: str, agent_target, project: Optional[Path]
) -> Optional[dict]:
    """Освободить ИМЯ В ЗОНЕ АГЕНТА, занятое версией вне Skillery (#1405).

    ВТОРАЯ ПОЛОВИНА «хаб становится главным». Резерв каталога СТОРА сам по себе
    ничего не решает: исполняется то, что лежит в ``~/.claude/skills/<навык>``.
    На живой машине владельца ровно это и было — в сторе хабовая версия
    (``vk`` v0.2.6), а в зоне агента посторонний каталог с тем же именем, и
    исполнялся он. Хабовая установка при этом либо падала в ownership-гейт кита
    ПОСЛЕ записи в стор (стор ушёл вперёд, зона агента осталась прежней), либо
    выглядела успешной по стору — «веб сказал ок, а работает локальная».

    Занятое имя освобождается недеструктивно (см. ``backup_scope_entry``):
    каталог уезжает в резерв целиком, ССЫЛКА на рабочую папку автора только
    снимается — её цель не трогаем.

    ``None`` — освобождать нечего: пути нет или он уже наш (ссылка в стор /
    каталог с нашей метой — с этим кит разбирается сам). Никогда не бросает:
    сорванный резерв не имеет права отменить установку.
    """
    from skillkit.installer.ownership import PathGuard

    from skillery_cli.core.store_backup import backup_scope_entry

    try:
        from skillkit import linker

        link = Path(agent_target.slug_dir(dir_name, project=project))
        if not linker.is_link(link) and not link.exists():
            return None
        if PathGuard(Path(store_root)).is_ours(
            link, store_dir=Path(store_root) / dir_name
        ):
            return None
    except Exception:  # noqa: BLE001 — не смогли опознать путь → не вмешиваемся
        return None
    try:
        return backup_scope_entry(
            store_root, dir_name, link, reason="hub-takeover-scope"
        )
    except Exception:  # noqa: BLE001 — не смогли зарезервировать → не вытесняем
        return None


def _announce_takeover(record: dict, *, slug: str, version: str, headless: bool) -> None:
    """Сказать пользователю, что версия подменена и как откатиться."""
    from skillery_cli.core.store_backup import KIND_SCOPE_DIR, KIND_SCOPE_LINK

    kind = str(record.get("kind") or "store")
    if kind == KIND_SCOPE_LINK:
        what = (
            f"каталог агента вёл ссылкой на {record.get('link_target') or '—'} "
            "(ссылка снята, сама папка не тронута)"
        )
    elif kind == KIND_SCOPE_DIR:
        what = "каталог агента был занят версией вне Хаба (сохранена целиком)"
    else:
        what = (
            f"прежняя версия ({record.get('source')} "
            f"v{record.get('version') or '—'}) сохранена"
        )
    text = (
        f"Навык «{slug}»: {what} в резерв {record.get('id')}; "
        f"активна версия из Хаба v{version}. "
        f"Откат: skillery store restore {slug} --backup {record.get('id')}"
    )
    if not headless:
        emit_message(text, level="warn")


def _install_hub_subdir(
    installer,  # SkillInstaller
    *,
    slug: str | None,
    version: str,
    commit_sha: str,
    local_src: Path,
    manifest: dict,
    project: Optional[Path],
    force: bool,
    skill_id,
):
    """Поставить навык-в-подпапке из РАСПАКОВАННОГО снапшота хаба.

    #1405, КОРЕНЬ ПРОБЛЕМЫ «установка из Хаба не становится главной». Снапшот
    монорепо-навыка мы распаковываем сами (кит не принимает ``skill_path``) и
    раньше ставили его как ЛОКАЛЬНУЮ ПАПКУ — ``install_from_path``. Кит честно
    выводил происхождение по типу источника и писал в мету
    ``source="local-path"``, хотя навык приехал из хаба. Последствия ровно те,
    что видел владелец: ``installed`` показывает local-path, автообновление
    (оно отбирает строго ``source == "hub"``) навык не трогает, а разовая
    миграция меты (``store_migrations``) его не спасает — она отрабатывает один
    раз на профиль и требует ``skill_id``, а метка регрессировала при КАЖДОЙ
    следующей установке.

    Лечится явной меткой происхождения: ``InstallRequest.source_label="hub"``
    сильнее вывода по типу источника (ровно так кит помечает свой
    ``SnapshotSource``). Старый кит без ``InstallRequest`` — откат на прежний
    вызов: хуже метка, но установка не падает.
    """
    try:
        from skillkit.installer import InstallRequest, LocalPathSource
    except ImportError:  # старый кит — прежнее поведение
        return installer.install_from_path(
            slug=slug, version=version, commit_sha=commit_sha,
            local_src=local_src, manifest=manifest, project=project,
            force=force, skill_id=skill_id,
        )
    return installer.install(
        InstallRequest(
            slug=slug, version=version, commit_sha=commit_sha, manifest=manifest,
            source=LocalPathSource(Path(local_src)), source_label="hub",
            project=project, force=force, skill_id=skill_id,
        )
    )


async def _materialize_from_bundle(
    installer,  # SkillInstaller
    client: HubClient,
    *,
    dep_slug: str | None,
    dep_version: str,
    dep_bundle: dict,
    dep_repo: str | None,
    dep_id,
    project_path: Optional[Path],
    force: bool,
):
    """Материализовать навык: СНАЧАЛА backend-снапшот, иначе git clone.

    Content-serving: бэкенд заранее (при sync, под токеном хаба) снял tar.gz
    версии в object_storage и отдаёт его на ``GET /skills/{ref}/versions/{semver}
    /snapshot``. Ставя из снапшота, клиент НЕ клонирует репо → не нужны его
    локальные git-креды и прямой доступ к приватному репозиторию (токен остаётся
    на бэкенде).

    Снапшот применим только когда навык лежит в КОРНЕ репо (SKILL.md в корне
    архива): ``install_from_snapshot`` не принимает ``skill_path``. Для навыка в
    подпапке и когда снапшота нет (404) — откат на git clone (он умеет
    ``skill_path`` и работает через клиентские креды)."""
    import tempfile

    from skillkit.errors import ScopeConflict

    skill_path = dep_bundle.get("skill_path")
    commit_sha = dep_bundle["commit_sha"]
    manifest = dep_bundle["manifest"]
    ref = dep_slug or (str(dep_id) if dep_id is not None else "")

    if ref:
        # Снапшот пробуем ВСЕГДА, даже для навыка в подпапке (skill_path). Раньше
        # навык-в-подпапке шёл сразу на git clone — а для ПРИВАТНОГО репо это
        # «Authentication failed»: у устройства нет git-кред, и раздавать их
        # пользователю нельзя. Снапшот бэкенд снял под СВОИМ токеном (GitHub App /
        # hub-gateway) → устройство ставит без git и без секретов.
        #
        # kit-контракт (s-skillkit 0.2.2 на устройствах): install_from_snapshot
        # НЕ принимает skill_path. Поэтому для навыка в подпапке САМИ извлекаем
        # архив, берём подпапку и ставим install_from_path (локальный источник).
        snap: bytes | None = None
        # ЛЮБАЯ ошибка получения (404, 403, 5xx, обрыв) → тихий откат на git clone:
        # снапшота может не быть, а репо — публичным / с локальными кредами.
        try:
            snap = await client.download_snapshot(ref, dep_version)
        except Exception:  # noqa: BLE001 — best-effort: не удалось → clone
            snap = None
        if snap:
            import shutil

            tmp_dir = Path(tempfile.mkdtemp(prefix="skillery-snap-"))
            archive = tmp_dir / f"{ref}-{dep_version}.tar.gz"
            try:
                archive.write_bytes(snap)
                if skill_path:
                    extracted = _extract_snapshot_subdir(archive, tmp_dir, skill_path)
                    if extracted is not None:
                        sub, repo_root = extracted
                        result = _install_hub_subdir(
                            installer,
                            slug=dep_slug or None,
                            version=dep_version,
                            commit_sha=commit_sha,
                            local_src=sub,
                            manifest=manifest,
                            project=project_path,
                            force=force,
                            skill_id=dep_id,
                        )
                        # Монорепо-навык: его CLI-пакет (pyproject+src) лежит в
                        # КОРНЕ репо, а не в подпапке навыка. Сохраняем корень в
                        # store/<slug>/.pkgsrc, чтобы _apply_tooling поставил из
                        # него команду (`uv tool install --force <корень>`) — иначе
                        # `tg`/`vk`/… падали ModuleNotFoundError (пакет не ставился).
                        _persist_cli_package_source(repo_root, result)
                        return result
                else:
                    return installer.install_from_snapshot(
                        slug=dep_slug or None,
                        version=dep_version,
                        commit_sha=commit_sha,
                        archive_path=archive,
                        manifest=manifest,
                        project=project_path,
                        force=force,
                        skill_id=dep_id,
                    )
            except ScopeConflict:
                # #1405: КОНФЛИКТ ИМЕНИ В ЗОНЕ АГЕНТА — не «битый снапшот».
                # Снапшот доехал и лёг в стор, а споткнулась ЛИНКОВКА. Прежний
                # широкий except уводил этот случай на git clone: пользователь
                # получал «нет доступа к приватному репозиторию» (клон падал на
                # кредах) вместо настоящей причины, а стор тем временем уже
                # содержал хабовую версию — та самая картина «веб сказал ок, а
                # исполняется локальная». Пробрасываем: у ScopeConflict есть свой
                # человекочитаемый разбор с готовой командой (см. ``_run``).
                raise
            except Exception:  # noqa: BLE001 — битый снапшот → откат на clone
                pass
            finally:
                # rmtree сносит и архив, и папку одним вызовом — не оставляем
                # temp при сбое unlink (ignore_errors: очистка не важнее install).
                shutil.rmtree(tmp_dir, ignore_errors=True)

    # Fallback: git clone. Достигается, только если снапшота нет; для ПУБЛИЧНОГО
    # репо clone сработает и без кред, для приватного — по локальным git-кредам.
    #
    # #943: skill_path ОБЯЗАН доехать сюда. Выше он уже прочитан — им решается
    # «снапшот или клон» (снапшот применим только для навыка в корне репо). Но в
    # сам install его не передавали: навык-в-подпапке уходил на клон и получал
    # КОРЕНЬ монорепозитория — src/, migrations/, pyproject.toml, а SKILL.md
    # оказывался этажом ниже. В таком виде Claude Code навык не видит вовсе.
    return installer.install(
        slug=dep_slug or None,
        version=dep_version,
        commit_sha=commit_sha,
        repo_url=dep_repo or dep_bundle.get("repo_url"),
        skill_path=skill_path,
        manifest=manifest,
        project=project_path,
        force=force,
        skill_id=dep_id,
    )


def _sweep_orphan_dependencies(  # noqa: ANN001
    installer,
    store_root: Path,
    *,
    consumer: str,
    project_path: Optional[Path],
    agent_target=None,
) -> list[str]:
    """Убрать зависимости, которые остались без потребителя (#2282).

    Обратные ссылки, а не forward-граф: у каждой зависимости хранится
    ``required_by`` — кто её требует. Снятие потребителя вычёркивает его из этих
    списков, и навык уезжает с диска ровно тогда, когда список опустел И навык
    приехал как зависимость. Так же считает ``apt autoremove``: forward-граф
    успевает протухнуть (манифест потребителя уже удалён), обратные ссылки —
    нет.

    Рекурсивно: снятая зависимость сама могла что-то требовать.

    Возвращает имена реально убранных навыков (в порядке уборки).
    """
    swept: list[str] = []
    queue = [consumer]
    seen = {consumer}
    while queue:
        gone = queue.pop(0)
        try:
            orphans = install_reason.forget_consumer(store_root, gone)
        except Exception:  # noqa: BLE001 — уборка не важнее самого удаления
            break
        for orphan in orphans:
            if orphan in seen:
                continue
            seen.add(orphan)
            with suppress(Exception):
                _revert_tooling(
                    orphan, agent_target=agent_target, project=project_path,
                    store_dir=Path(store_root) / orphan,
                )
            try:
                res = installer.remove(
                    slug=orphan, project=project_path, purge=True
                )
            except Exception:  # noqa: BLE001 — см. выше
                continue
            if res.removed:
                swept.append(orphan)
                queue.append(orphan)
    return swept


def _unlink_from_agent_zone(installer, dir_name: str, project_path: Optional[Path]) -> bool:  # noqa: ANN001
    """Убрать навык из ЗОНЫ АГЕНТА, оставив его в сторе (#2282).

    Зона агента (``~/.claude/skills/<навык>`` либо каталог агента в проекте) —
    это витрина: что там лежит, то агент видит списком и может позвать. Навык,
    приехавший ТОЛЬКО как зависимость, там не место — он расширяет потребителя,
    а не является отдельным навыком. Стор при этом не трогаем: потребитель
    берёт контент оттуда.

    Ставим-и-снимаем, а не «не ставим»: линковка выполняется внутри
    ``installer.install`` (единственный путь материализации, общий со снапшотом
    и монорепо-подпапкой), и обходить её пришлось бы тремя разными способами.
    Кит снимает ссылку своим же гейтом принадлежности — чужой каталог на этом
    имени не пострадает.

    Возвращает True, если ссылка действительно была снята.
    """
    try:
        result = installer.remove(slug=dir_name, project=project_path)
    except Exception:  # noqa: BLE001 — витрина не важнее самой установки
        return False
    return bool(result.removed)


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
    headless: bool = False,
    initiator: str = "cli",
) -> list[dict]:
    """Качает bundle (+deps), материализует в стор, линкует в scope.

    ``headless=True`` (демон) → onboarding не печатается (stdout в DEVNULL), а
    пишется в журнал pending-onboarding для последующего `skillery status`.

    ``initiator`` — КТО инициировал установку (для аудита в логах): ``cli``
    (ручной ``skillery install`` foreground) | ``web-queue`` (демон подобрал
    задание из веб-очереди устройства) | ``daemon-auto`` (фоновое авто-обновление
    до latest). Пишется в контекст install-аудита (материализация/провал), чтобы
    в логе было видно, чьё это действие.

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
    from skillery_cli.core.logging_setup import install_logger

    _log_file = "daemon.log" if headless else "cli.log"
    ilog = install_logger(_log_file)
    try:
        bundle = await client.install_bundle(slug, channel=channel)
        installer = SkillInstaller(agent_target, cfg.effective_store_dir())
        chain = bundle.get(
            "dependencies_chain",
            [[bundle["skill_slug"], bundle["version"], bundle.get("repo_url")]],
        )
        installed_chain: list[dict] = []
        # Имя каталога КОРНЯ цепочки — идентичность потребителя в ``required_by``
        # (для slug-less навыка это его числовой id, как и имя папки стора).
        from skillkit.installer import skill_dir_name as _root_dir_name

        _root_dn = _root_dir_name(
            bundle.get("skill_slug") or None, bundle.get("skill_id")
        )
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
            # #1405: имя стора занято навыком из ДРУГОГО источника (локальная
            # папка / произвольный git) → прячем его в резерв ДО материализации.
            # Так хаб ставится начисто (а не патчем поверх чужого дерева), и
            # прежнюю версию можно вернуть `store restore`.
            from skillkit.installer import skill_dir_name as _dir_name

            _store_root = cfg.effective_store_dir()
            _dn = _dir_name(dep_slug or None, dep_id)
            # #2282: причину и накопленный required_by снимаем ДО материализации
            # — кит перезаписывает ``_skill_meta.json`` целиком, наши поля в
            # свежем файле отсутствуют. Без снимка переустановка теряла бы и
            # повышение до explicit, и список потребителей.
            _prior_reason = install_reason.snapshot(_store_root / _dn)
            replaced = _backup_foreign_before_hub(_store_root, _dn)
            # #1405, вторая половина: то же имя в ЗОНЕ АГЕНТА. Стор — хранилище,
            # исполняется ссылка/каталог в `~/.claude/skills/<навык>`; пока он
            # занят посторонней версией, «хаб стал главным» неправда, чем бы ни
            # закончилась запись в стор.
            replaced_scope = _backup_foreign_scope_before_hub(
                _store_root, _dn, agent_target, project_path
            )
            # Content-serving: снапшот с бэкенда (без клиентских git-кред) →
            # fallback на git clone. См. _materialize_from_bundle.
            try:
                result = await _materialize_from_bundle(
                    installer, client,
                    dep_slug=dep_slug, dep_version=dep_version, dep_bundle=dep_bundle,
                    dep_repo=dep_repo, dep_id=dep_id, project_path=project_path,
                    force=force,
                )
            except BaseException:
                # Вытеснение НЕ ИМЕЕТ ПРАВА оставить пользователя без навыка:
                # если хаб-версия не встала (нет снапшота, отвалился clone), тут
                # же возвращаем прежнюю из резерва — иначе рабочий локальный
                # навык исчезал бы из-за неудачной чужой установки.
                for _rec in (replaced_scope, replaced):
                    if _rec is None:
                        continue
                    with suppress(Exception):
                        from skillery_cli.core.store_backup import restore_backup

                        restore_backup(_store_root, _dn, _rec.get("id"))
                raise
            for _rec in (replaced, replaced_scope):
                if _rec is None:
                    continue
                # Состояние пользователя (.env / _local/ / профили браузера +
                # preserved_paths) переезжает в свежую установку: иначе «главной
                # стала хабовая» означало бы «навык перестал работать». У
                # резерва-ССЫЛКИ переносить нечего (мы её только сняли) — и
                # лезть в чужую рабочую папку за состоянием мы не вправе.
                with suppress(Exception):
                    from skillery_cli.core.store_backup import carry_over_user_state

                    _rec["carried_over"] = carry_over_user_state(
                        _rec,
                        Path(result.store_dir or (_store_root / _dn)),
                        extra_preserved=tuple(
                            (dep_bundle.get("manifest") or {}).get("preserved_paths")
                            or ()
                        ),
                    )
                _announce_takeover(
                    _rec, slug=str(dep_slug or dep_id), version=dep_version,
                    headless=headless,
                )
                with suppress(Exception):
                    ilog.info("прежняя версия навыка убрана в резерв", extra={
                        "context": {
                            "step": "takeover", "slug": str(dep_slug or dep_id),
                            "backup_id": _rec.get("id"),
                            "backup_kind": _rec.get("kind") or "store",
                            "replaced_source": _rec.get("source"),
                            "replaced_version": _rec.get("version"),
                            "carried_over": _rec.get("carried_over") or [],
                            "initiator": initiator,
                        }})
            # #2282: ПРИЧИНА установки. Корень цепочки — то, что попросил
            # пользователь (explicit); остальные приехали ради него и являются
            # РАСШИРЕНИЕМ потребителя, а не самостоятельным навыком: они лежат в
            # сторе (потребителю доступны), но в зону агента не линкуются —
            # иначе агент видит плагин в списке навыков и зовёт его напрямую.
            # Явная установка того же навыка «повышает» его до полноценного
            # (apt: manual побеждает auto) — тогда ссылка остаётся.
            _store_dir_final = Path(result.store_dir or (_store_root / _dn))
            _is_root = dep_slug == bundle["skill_slug"]
            _reason = install_reason.EXPLICIT if _is_root else install_reason.DEPENDENCY
            _effective = install_reason.stamp(
                _store_dir_final,
                reason=_reason,
                required_by=None if _is_root else _root_dn,
                prior=_prior_reason,
            )
            _agent_visible = _effective == install_reason.EXPLICIT
            if not _agent_visible and not result.skipped:
                _unlink_from_agent_zone(installer, _dn, project_path)
            entry = {
                "slug": dep_slug, "skill_id": result.skill_id,
                "version": dep_version, "is_update": result.is_update,
                "target_dir": str(result.target_dir), "scope": result.scope,
                "linked": result.linked if _agent_visible else False,
                "link_kind": result.link_kind if _agent_visible else "none",
                "install_reason": _effective,
                "agent_visible": _agent_visible,
                "store_dir": str(_store_dir_final),
            }
            # Фикс 5в: stub-установка помечается в JSON-ответе явно.
            if result.content == "stub":
                entry["content"] = "stub"
            if result.skipped:
                entry["skipped"] = True
                entry["skip_reason"] = result.skip_reason
            if replaced is not None:
                # В JSON-ответе факт вытеснения виден явно (веб/скрипты).
                entry["replaced"] = replaced
            if replaced_scope is not None:
                entry["replaced_scope"] = replaced_scope
            installed_chain.append(entry)
            # Аудит материализации (SK-5): что и как легло в стор. ``initiator``
            # (C2) — чьё это действие: cli / web-queue / daemon-auto.
            ilog.info("навык материализован", extra={"context": {
                "step": "materialize", "slug": dep_slug, "version": dep_version,
                "scope": result.scope, "content": result.content,
                "initiator": initiator,
                "skipped": result.skipped,
                "skip_reason": result.skip_reason if result.skipped else None,
            }})
            if not result.skipped:
                # tooling-навык → CLI в PATH-стор + MCP в конфиг агента +
                # runtime-deps. #889: манифест бандла ДОПОЛНЯЕМ декларацией из
                # самого навыка (_skill_meta.toml в сторе) — у навыка с подпапкой
                # (skill_path) бандл мог прийти без cli/runtime_dependencies.
                _tooling_manifest = _merge_tooling_from_store(
                    dep_bundle.get("manifest"), result.store_dir
                )
                _apply_tooling(
                    result, _tooling_manifest,
                    agent_target=agent_target, project=project_path,
                    log_file=_log_file, initiator=initiator,
                )
                _emit_onboarding(
                    _tooling_manifest,
                    slug=dep_slug or (str(dep_id) if dep_id is not None else ""),
                    headless=headless,
                )
                ref = dep_slug or (str(dep_id) if dep_id is not None else "")
                # Источник = hub (бэкенд-bundle + git clone).
                track_skill_event(
                    "skill.update" if result.is_update else "skill.install",
                    slug=ref, version=dep_version, scope=result.scope,
                    source="hub", agent=agent_target.name,
                )
                # Включение-в-проект (project-линк) → отдельное skill.enable.
                if result.scope == "project":
                    track_skill_event(
                        "skill.enable", slug=ref, version=dep_version,
                        scope="project", source="hub", agent=agent_target.name,
                    )
        return installed_chain
    except Exception as exc:  # noqa: BLE001 — провал ОБЯЗАН оставить причину в логе
        # C2: причина, почему навык НЕ установился, — в ЛОКАЛЬНЫЙ лог (cli.log при
        # foreground, daemon.log при headless), а не только в исключение/stderr.
        with suppress(Exception):
            ilog.error("установка навыка не удалась", extra={"context": {
                "step": "install", "slug": slug, "channel": channel,
                "scope": scope, "initiator": initiator, "error": str(exc),
            }})
        raise
    finally:
        # C3 (#1099) + #1174: аудит установки (шаги + причина провала) уезжает на
        # бэк, чтобы разбирать устройство из веба. Best-effort с жёстким
        # таймаутом — доставка НЕ имеет права держать установку; при офлайне
        # конверты остаются в общем outbox'е и уедут следующим циклом демона.
        # Без ``force``: троттл воркера гасит шквал при цепочке зависимостей.
        with suppress(Exception):
            from skillery_cli.core.outbox_worker import flush_outbox_safe

            await flush_outbox_safe(client)
        await client.close()


# Порядок = приоритет detection в ките (claude_code → codex → antigravity).
# Держим локально (не тянем приватный _CHAIN кита), классы уже экспортируются.
_ALL_AGENT_NAMES: tuple[str, ...] = (
    ClaudeCodeTarget.name,
    CodexTarget.name,
    AntigravityTarget.name,
)


def cmd_install(
    slug: str = typer.Argument(
        ..., metavar="ID_ИЛИ_SLUG", help="id-или-slug скилла (backend принимает оба)"
    ),
    channel: str = typer.Option("published"),
    agent: Optional[str] = typer.Option(None),
    all_agents: bool = typer.Option(
        False, "--all-agents",
        help="Поставить навык под ВСЕ поддерживаемые агенты "
             "(claude_code+codex+antigravity) за один вызов. "
             "Взаимоисключающе с --agent.",
    ),
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

    # При прямом вызове функции (юнит-тесты) незаданный typer.Option приходит
    # sentinel'ом ``OptionInfo`` (он truthy!) — нормализуем в чистый bool,
    # чтобы многотаргет включался ТОЛЬКО на явном True.
    all_agents = all_agents is True

    # --- разбор источника + взаимоисключение флагов ---
    if path is not None and from_git is not None:
        emit_error("VALIDATION", "--path и --from-git взаимоисключающие")
        raise typer.Exit(1)
    if ref is not None and from_git is None:
        emit_error("VALIDATION", "--ref имеет смысл только вместе с --from-git")
        raise typer.Exit(1)
    if all_agents and agent is not None:
        emit_error("VALIDATION", "--all-agents и --agent взаимоисключающие")
        raise typer.Exit(1)

    source: dict | None = None
    if path is not None:
        source = {"kind": "path", "slug": slug, "path": path}
    elif from_git is not None:
        source = {"kind": "git", "slug": slug, "url": from_git, "ref": ref}

    # Один навык → один или несколько агент-таргетов (--all-agents).
    if all_agents:
        targets = [get_target(name) for name in _ALL_AGENT_NAMES]
    else:
        targets = [get_target(agent or cfg.agent)]
    _ = actual_scope  # передаётся через project_path

    # Hub-режим (источник не задан) требует login+токен; локальные — нет.
    access = ""
    if source is None:
        _maybe_auto_update(cfg, project=project_path)
        _maybe_notify_cli_update(cfg)
        access = _get_access_token()

    async def _do() -> None:
        installed_chain: list[dict] = []
        multi = len(targets) > 1
        for tgt in targets:
            chain = await _install_chain(
                cfg, access, slug=slug, channel=channel, scope=actual_scope,
                project_path=project_path, force=force, agent_target=tgt,
                source=source,
            )
            if multi:
                for item in chain:
                    item["agent"] = tgt.name
            installed_chain.extend(chain)
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

        # A2 (голос 07-24): ручная установка из ХАБА должна РАПОРТОВАТЬ факт
        # per-device — иначе навык, поставленный `skillery install`, не виден как
        # «установлен на ЭТОМ устройстве» в вебе/профиле (раньше факт слал только
        # демон-очередь). Backend при отсутствии задания создаёт факт-строку сам
        # (устройство резолвится по id:<cdid> в User-Agent). Только hub-режим
        # (source is None), только реально применённые (не skipped/не stub),
        # best-effort — сбой рапорта не ломает установку. В _install_chain НЕ
        # кладём: демон-путь (_reconcile_device_queue) рапортует сам → дубль.
        if source is None and access:
            rep_client = HubClient(
                base_url=cfg.base_url, access_token=access,
                on_token_refresh=_make_refresh_callback(cfg),
            )
            try:
                reported: set[str] = set()
                for item in installed_chain:
                    if item.get("skipped") or item.get("content") == "stub":
                        continue
                    ref = item.get("slug") or (
                        str(item["skill_id"])
                        if item.get("skill_id") is not None
                        else None
                    )
                    ver = item.get("version")
                    if not ref or not ver or ref in reported:
                        continue
                    reported.add(str(ref))
                    sid = item.get("skill_id")
                    with suppress(Exception):
                        await rep_client.report_device_apply(
                            slug=str(ref), ok=True, version=str(ver),
                            skill_id=str(sid) if sid is not None else None,
                        )
            finally:
                await rep_client.close()

        def _render(items: list) -> None:
            for item in items:
                agent_tag = f"[{item['agent']}] " if item.get("agent") else ""
                if item.get("skipped"):
                    console.print(
                        f"[yellow]→ Пропущен[/] {agent_tag}({item['scope']}) "
                        f"{item['slug']}@{item['version']}: {item.get('skip_reason')}"
                    )
                    continue
                action = "Обновлён" if item["is_update"] else "Установлен"
                # #2282: зависимость приезжает в СТОР и расширяет потребителя, но
                # отдельным навыком в зоне агента не становится — говорим это
                # прямо, иначе «установлен» читается как «агент его увидит».
                if item.get("agent_visible") is False:
                    console.print(
                        f"[green]✓[/] {action} {agent_tag}(зависимость) 🧩 "
                        f"{item['slug']}@{item['version']} → "
                        f"{item.get('store_dir') or item['target_dir']} "
                        "[dim](в сторе; отдельным навыком агенту не показывается)[/]"
                    )
                    continue
                mount = "📎" if item["linked"] else "📄"
                console.print(
                    f"[green]✓[/] {action} {agent_tag}({item['scope']}) {mount} "
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
        store_dir = cfg.effective_store_dir() / slug
        # #2282: включение в проект — ЯВНОЕ действие пользователя. Навык,
        # приехавший когда-то как зависимость, этим повышается до полноценного
        # (и перестаёт быть кандидатом на авто-уборку вместе с потребителем).
        install_reason.stamp(store_dir, reason=install_reason.EXPLICIT)
        store_meta = read_meta(store_dir) or {}
        project_manifest.add(project_path, slug)
        # повторное включение tooling-навыка в проект → CLI/MCP/deps
        # (idempotent). Манифест из меты стора; result-подобие через legacy-link.
        _apply_tooling(
            type("_R", (), {"slug": slug, "skill_id": store_meta.get("skill_id"),
                            "store_dir": store_dir})(),
            store_meta.get("manifest"),
            agent_target=target, project=project_path,
        )
        # Включение-в-проект (навык уже в сторе) → skill.enable; source берём
        # из meta стора (навык пришёл из hub/local-path/git-url).
        track_skill_event(
            "skill.enable", slug=slug,
            version=store_meta.get("version") or "", scope="project",
            source=store_meta.get("source"), agent=target.name,
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
            "залогиньтесь: skillery login",
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
    # снять CLI/MCP навыка из конфига агента (стор цел → манифест есть).
    _revert_tooling(
        slug, agent_target=target, project=project_path,
        store_dir=cfg.effective_store_dir() / slug,
    )
    # Снятие PROJECT-ссылки (стор цел) → skill.disable, НЕ uninstall.
    track_skill_event(
        "skill.disable", slug=slug, scope="project", agent=target.name
    )

    def _render(_: dict) -> None:
        if result.removed:
            console.print(f"[green]✓[/] Выключен из проекта: {slug} [dim](стор сохранён)[/]")
        else:
            console.print(f"[yellow]Не был включён[/]: {slug}")
        if in_manifest:
            console.print("[dim]Убран из .skillery/skills.toml[/]")

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
    """Привести project scope в соответствие .skillery/skills.toml.

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
                # Массовый re-link включает навык в проект → skill.enable на
                # каждый (раньше sync вообще не трекался — дыра аналитики).
                # source читаем из meta стора (откуда навык изначально пришёл).
                store_meta = read_meta(store_root / slug) or {}
                track_skill_event(
                    "skill.enable", slug=slug,
                    version=store_meta.get("version") or "",
                    scope="project", source=store_meta.get("source"),
                    agent=target.name,
                )
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
                "сторе — слинковано. Для докачки: skillery login"
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


async def _reconcile_capability_leases(cfg: ClientConfig, access: str) -> dict:
    """#1490: обновить набор лизов способностей в такте тяжёлой сверки.

    Тонкая обёртка над :func:`skillery_cli.core.lease_sync.sync_leases` — вся
    политика (порог обновления как доля TTL, различение «хаб ответил нет» и
    «хаб недоступен», монотонный пол времени) живёт там, здесь только
    построение клиента и гарантия, что демон от лизов не умрёт.

    Почему best-effort: на этом же такте едут очередь заданий устройства и
    доставка outbox'а. Отсутствие свежего лиза — это отказ ОДНОЙ способности
    через сутки, а упавший такт — молчащее устройство прямо сейчас.
    """
    from skillery_cli.core.identity import device_uid
    from skillery_cli.core.lease_sync import sync_leases

    client = HubClient(
        base_url=cfg.base_url, access_token=access,
        on_token_refresh=_make_refresh_callback(cfg),
    )
    try:
        return await sync_leases(client, device_id=device_uid())
    except Exception as exc:  # noqa: BLE001 — демон не умирает из-за лизов
        from skillery_cli.core.logging_setup import get_logger

        get_logger("lease").warning(
            "обновление лизов не прошло",
            extra={"context": {"error": str(exc) or type(exc).__name__}},
        )
        return {"error": str(exc) or type(exc).__name__}
    finally:
        await client.close()


async def _reconcile_hub_installs(
    cfg: ClientConfig,
    access: str,
    *,
    channel: str,
    agent_target,  # IAgentTarget
    force: bool = False,
    initiator: str = "cli",
) -> dict[str, list]:
    """«Нажал Установить в вебе → CLI скачал»: подтянуть /me/installs в стор.

    Идемпотентно: для каждого навыка из ``/me/installs`` сравниваем версию с
    локальным стором (``read_meta``). Отсутствующий или устаревший → качаем
    через :func:`_install_chain` (GLOBAL scope, как ``skillery install`` без
    ``--project``). Уже актуальный — пропускаем.

    Возвращает report ``{downloaded, updated, skipped, failed}`` (списки имён).
    Используется и ``cmd_pull``, и best-effort reconcile в демоне.

    ⚠️ **Односторонняя сверка — сознательно (#1486).** Пропажа навыка из
    ``/me/installs`` здесь НЕ приводит к удалению, и это не недоделка. Три
    причины, каждой достаточно:

    1. ``/me/installs`` — не набор, а ОКНО: бэкенд считает его из последних
       500 install-событий актора (``routes/me.py``). Навык, поставленный
       давно, из ответа выпадает сам собой — «удалять отсутствующее» значило
       бы стирать рабочие навыки по расписанию активности пользователя.
    2. Сюда попадают и ЧУЖИЕ установки: ``install --path``, ``--from-git``,
       project-scope. Их в хабе нет и быть не должно.
    3. Канал снятия уже есть и он адресный — задание ``action=remove`` в
       очереди устройства (§6.1 контракта лиза), которое ещё и проверяет, не
       держит ли навык другая востребованная способность.
    """
    store_root = cfg.effective_store_dir()
    client = HubClient(
        base_url=cfg.base_url, access_token=access,
        on_token_refresh=_make_refresh_callback(cfg),
    )
    report: dict[str, list] = {
        "downloaded": [], "updated": [], "skipped": [], "failed": [],
    }
    try:
        installs = await client.list_my_installs()
    finally:
        await client.close()

    for entry in installs:
        slug = entry.get("slug")
        skill_id = entry.get("skill_id")
        remote_version = entry.get("installed_version") or ""
        # Адресуем навык по slug, иначе по id (slug-less). Это же — имя папки
        # стора (installer.install кладёт slug-less под str(id)).
        ref = slug or (str(skill_id) if skill_id is not None else None)
        if not ref:
            continue
        local_meta = read_meta(store_root / ref) or {}
        local_version = local_meta.get("version")
        local_source = str(local_meta.get("source") or "")
        # #1405: сравнение версий имеет смысл только между ОДНОРОДНЫМИ
        # установками. Раньше локальный `install --path` со своей версией
        # (напр. atlas v0.3.9) глушил хаб-установку той же/меньшей версии —
        # веб рапортовал успех, а на устройстве оставалась локальная версия и
        # исполнялась именно она. Чужой источник ⇒ хаб забирает имя себе
        # (прежняя версия уедет в резерв внутри _install_chain).
        foreign = bool(local_meta) and local_source in _FOREIGN_SOURCES
        if local_meta and local_version and not force and not foreign:
            # Уже в сторе хаб-версия: качаем только если remote СТРОГО новее.
            if not remote_version or not _is_newer(remote_version, local_version):
                report["skipped"].append(ref)
                continue
            bucket = "updated"
        elif local_meta:
            bucket = "updated"
        else:
            bucket = "downloaded"
        try:
            await _install_chain(
                cfg, access, slug=str(ref), channel=channel, scope="global",
                project_path=None, force=force, agent_target=agent_target,
                headless=True, initiator=initiator,
            )
            report[bucket].append(ref)
        except Exception as exc:  # noqa: BLE001 — причина провала ОБЯЗАНА быть видна
            # Раньше провал молча уходил в report["failed"] без текста — в
            # daemon.log не оставалось следа, почему навык из веб-набора не встал.
            with suppress(Exception):
                from skillery_cli.core.logging_setup import install_logger

                install_logger("daemon.log").error(
                    "докачка навыка из /me/installs не удалась", extra={
                        "context": {"step": "install", "skill": str(ref),
                                    "bucket": bucket, "initiator": initiator,
                                    "error": str(exc)}})
            report["failed"].append(ref)
    return report


_MAX_INSTALL_ATTEMPTS = 3


def _install_attempts_path() -> Path:
    # Локальный импорт: `_default_config_dir` — приватная деталь config-модуля,
    # в шапке __main__ её нет. Раньше имя звалось «из воздуха» — любой вызов
    # счётчика попыток падал бы NameError (ruff F821 это и показывал).
    from skillery_cli.config import _default_config_dir

    return _default_config_dir() / "install_attempts.json"


def _load_install_attempts() -> dict:
    import json

    try:
        return json.loads(_install_attempts_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — нет файла/битый → пусто
        return {}


def _save_install_attempts(data: dict) -> None:
    import json

    try:
        p = _install_attempts_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
    except Exception:  # noqa: BLE001 — лимит попыток не критичнее самой команды
        pass


async def _reconcile_device_queue(
    cfg: ClientConfig,
    access: str,
    *,
    channel: str,
    agent_target,  # IAgentTarget
    wait: int = 0,
    payload: dict | None = None,
    client=None,  # HubClient | None
) -> dict[str, list]:
    """#905: забрать очередь ЭТОГО устройства, применить и ОТРАПОРТОВАТЬ факт.

    #1191 (SSE): применение очереди переиспользуется push-каналом, поэтому
    источник данных отделён от их обработки:

    - ``payload`` — УЖЕ полученный ответ очереди (``{items, device_tasks}``);
      задан ⇒ сетевого запроса нет, применяем ровно его. Так SSE-клиент
      (``daemon/queue_stream.py``) прогоняет тело события ``queue`` через ТОТ ЖЕ
      код, что и long-poll — обработка заданий не продублирована.
    - ``client`` — ЧУЖОЙ HubClient (владелец закроет сам). Свой создаём и
      закрываем только когда его не передали.

    ``wait>0`` — LONG-POLL: запрос очереди висит на сервере до <wait> сек, пока
    не появится задание (мгновенная доставка + heartbeat). Сам факт висящего
    коннекта держит устройство «на связи».

    Отличие от :func:`_reconcile_hub_installs`: сервер адресует задания
    конкретному устройству (``desired_version`` на ``(user, device, skill)``), а
    мы обязаны сообщить РЕЗУЛЬТАТ. Раньше рапорта не было вовсе — упавшая
    установка выглядела успешной, и веб не знал, что реально стоит на машине.

    Успех рапортуем версией, которая РЕАЛЬНО легла в стор (перечитываем мету),
    а не той, что просили — иначе снова получим намерение вместо факта.
    """
    # ⚠️ ИМЕНОВАНИЕ: помощник уровня ACCESS импортируем ПОД ПСЕВДОНИМОМ. Прямой
    # `from ... import access` перекрывал ОДНОИМЁННЫЙ ПАРАМЕТР этой функции —
    # access-токен подменялся функцией логирования, и в `HubClient(access_token=…)`
    # и в `_install_chain(cfg, access, …)` уходил объект функции вместо токена.
    # Наружу это выглядело как «очередь устройства всегда пуста» (401 → ApiError →
    # `except Exception: return report`): device_tasks/cli_upgrade не применялись
    # никогда, а задача вечно висела в `delivered`.
    from skillery_cli.core.logging_setup import access as _access_log
    from skillery_cli.core.logging_setup import get_logger, install_logger

    # Трейс опроса очереди — level-gated (ACCESS/DEBUG видны на debug/verbose);
    # провал установки — install_logger("daemon.log") пишет ВСЕГДА (как SK-5),
    # чтобы причина невыполненной веб-задачи была видна и на стандартном ERROR.
    _rlog = get_logger("reconcile")
    _ilog = install_logger("daemon.log")
    from skillery_cli.core.identity import device_uid

    store_root = cfg.effective_store_dir()
    report: dict[str, list] = {"applied": [], "failed": [], "skipped": []}
    # Long-poll держит коннект до <wait>с — HTTP-таймаут клиента ОБЯЗАН быть
    # больше wait, иначе клиент отвалится РАНЬШЕ ответа сервера (таймаут задаётся
    # на КОНСТРУКЦИИ HubClient — транспорт кита не принимает per-request timeout).
    own_client = client is None
    if own_client:
        client = HubClient(
            base_url=cfg.base_url, access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
            timeout=(float(wait) + 10.0) if wait > 0 else 30.0,
        )
    try:
        if payload is not None:
            # #1191: тело события SSE — сеть уже отработала, применяем как есть.
            resp = {
                "items": list(payload.get("items") or []),
                "device_tasks": list(payload.get("device_tasks") or []),
            }
        else:
            try:
                # #1102: берём ПОЛНЫЙ ответ очереди (items + device_tasks) одним
                # запросом. getattr-фолбэк — для старого транспорта/фейков без
                # `fetch_device_queue_full` (skill-очередь тогда работает как прежде,
                # device_tasks просто пусты).
                if hasattr(client, "fetch_device_queue_full"):
                    resp = await client.fetch_device_queue_full(
                        auto_update=cfg.auto_update, wait=wait,
                        supports_removal=True,  # #13: этот CLI умеет снимать навыки
                    )
                else:
                    items = await client.fetch_device_queue(
                        auto_update=cfg.auto_update, wait=wait, supports_removal=True,
                    )
                    resp = {"items": items, "device_tasks": []}
            except ApiError as exc:
                # #1441: 404/405 — это НЕ «заданий нет» и не сетевой сбой, а
                # расхождение контракта (путь очереди переименован на backend).
                # Раньше оно уходило в общий `except Exception: return report`, и
                # устройство просто замолкало: ни падения, ни строчки в логе, ни
                # признака в статусе. Теперь — WARNING в daemon.log + видимый флаг
                # в `skillery status`.
                if exc.status_code in route_health.CONTRACT_STATUSES:
                    with suppress(Exception):
                        route_health.record_unknown_route(
                            "GET", f"/devices/{device_uid()}/tasks", exc.status_code,
                            source="daemon.reconcile",
                        )
                    with suppress(Exception):
                        _ilog.warning(
                            "очередь устройства недоступна: backend не знает "
                            "маршрут — обнови CLI (skillery self upgrade)",
                            extra={"context": {
                                "status": exc.status_code, "code": exc.code,
                            }},
                        )
                return report
            except Exception:
                # Старый backend / нет устройства в UA — молча уступаем legacy-пути.
                return report
            else:
                with suppress(Exception):
                    route_health.record_ok("GET", f"/devices/{device_uid()}/tasks")
        queue = list(resp.get("items") or [])
        device_tasks = list(resp.get("device_tasks") or [])

        # ACCESS: факт опроса очереди + её размер (heartbeat устройства). На
        # стандартном ERROR не пишется — только на debug/verbose (или ACCESS).
        with suppress(Exception):
            _access_log(_rlog, "опрошена очередь устройства", extra={"context": {
                "queue": len(queue), "wait": wait, "initiator": "web-queue",
            }})
        if not queue:
            with suppress(Exception):
                _rlog.debug("очередь устройства пуста")

        attempts = _load_install_attempts()
        live_keys: set[str] = set()
        for item in queue:
            slug = item.get("slug")
            skill_id = item.get("skill_id")
            ref = slug or (str(skill_id) if skill_id is not None else None)
            # #1452: рапорт адресует навык числовым id в ТЕЛЕ (приоритетнее
            # slug'а в пути) — задание очереди его уже несёт, лишнего запроса нет.
            sid = str(skill_id) if skill_id is not None else None
            desired = str(item.get("desired_version") or "")
            if not ref:
                continue
            # Ключ учёта — по (навык + желаемая версия): смена версии = свежий
            # старт (прошлые провалы к новой версии не относятся).
            key = f"{ref}@{desired}"
            live_keys.add(key)
            tried = int(attempts.get(key, 0))
            # ЛИМИТ ПОПЫТОК: после 3 провалов не трогаем задание — иначе демон
            # каждый цикл повторял заведомо провальный git clone приватного репо,
            # поднимая видимое окно git-bash. Отказ уже отрапортован серверу.
            if tried >= _MAX_INSTALL_ATTEMPTS:
                with suppress(Exception):
                    _rlog.debug("задание пропущено (лимит попыток)", extra={
                        "context": {"skill": str(ref), "desired": desired,
                                    "attempts": tried}})
                report["skipped"].append(ref)
                continue
            # #13: removal-задание — СНЯТЬ навык с устройства, а не ставить.
            # Отдаётся только демонам, заявившим supports_removal (backend-гейт),
            # поэтому старый CLI сюда не попадёт и навык не переустановит.
            if str(item.get("action") or "install") == "remove":
                # #1486, §6.3 контракта лиза: ОТЗЫВ ≠ СНЯТИЕ. Лиз отвечает,
                # можно ли ИСПОЛНЯТЬ, а задание remove — должны ли ЛЕЖАТЬ
                # файлы. Пока навык-носитель держит хотя бы одна способность с
                # действующим правом, снимать его нельзя: иначе отзыв
                # `grok_transcriber` убил бы `grok_ask` из того же пакета,
                # право на который никто не отзывал.
                blockers = ()
                with suppress(Exception):  # реестра нет/битый ⇒ прежнее поведение
                    from skillery_cli.core.leases import removal_blockers

                    blockers = removal_blockers(str(ref), skill_id=sid)
                if blockers:
                    held = ", ".join(blockers)
                    reason = (
                        f"навык {ref} не снят: его держат способности с "
                        f"действующим доступом ({held}). Отзыв одной "
                        "способности не снимает навык, нужный другой"
                    )
                    with suppress(Exception):
                        _ilog.warning("снятие навыка отклонено (носитель занят)", extra={
                            "context": {"step": "remove", "skill": str(ref),
                                        "initiator": "web-queue", "held_by": list(blockers)}})
                    # Рапортуем ОТКАЗ, а не успех: иначе хаб записал бы навык
                    # снятым, а файлы остались бы на машине — состояние
                    # устройства в вебе стало бы неправдой. Счётчик попыток
                    # (лимит 3) не даёт крутить это заданию вечно.
                    with suppress(Exception):
                        await client.report_device_apply(
                            slug=str(ref), ok=False, error=reason, skill_id=sid
                        )
                    attempts[key] = tried + 1
                    report["skipped"].append(ref)
                    continue
                try:
                    # revert CLI/MCP навыка ДО remove — при purge стор (и его
                    # манифест) удаляется, revert читает манифест пока он на месте.
                    _revert_tooling(
                        str(ref), agent_target=agent_target, project=None,
                        store_dir=store_root / str(ref),
                    )
                    SkillInstaller(agent_target, store_root).remove(
                        slug=str(ref), project=None, keep_local=False,
                        purge=True,
                    )
                    with suppress(Exception):
                        track_skill_event(
                            "skill.uninstall", slug=str(ref),
                            scope="global", agent=agent_target.name,
                        )
                    # #1490: файлов навыка на машине больше нет — забываем и его
                    # требования лиза. Это ЕДИНСТВЕННАЯ законная причина убрать
                    # строку из реестра: отзыв права такой причиной не является
                    # (контракт лиза §6.3 — лиз про «можно исполнять», снятие
                    # про «должны ли лежать файлы»).
                    with suppress(Exception):
                        from skillery_cli.core.leases import RequirementsIndex

                        _idx = RequirementsIndex.load()
                        if _idx.forget_skill(str(ref), str(sid) if sid else None):
                            _idx.save()
                    # Рапорт об успешном снятии — без версии (backend по
                    # action=remove удалит строку очереди).
                    await client.report_device_apply(
                        slug=str(ref), ok=True, skill_id=sid
                    )
                    with suppress(Exception):
                        await client.report_cli_log(
                            level="info",
                            message=f"CLI: навык {ref} снят с устройства",
                            logger="cli.uninstall",
                            context={"skill": str(ref)},
                        )
                    attempts.pop(key, None)
                    report["applied"].append(ref)
                except Exception as exc:  # noqa: BLE001 — провал ОБЯЗАН быть виден
                    attempts[key] = tried + 1
                    with suppress(Exception):
                        _ilog.error("снятие навыка (веб-очередь) не удалось", extra={
                            "context": {"step": "remove", "skill": str(ref),
                                        "initiator": "web-queue", "error": str(exc)}})
                    with suppress(Exception):
                        await client.report_device_apply(
                            slug=str(ref), ok=False, error=str(exc), skill_id=sid
                        )
                    report["failed"].append(ref)
                continue
            try:
                with suppress(Exception):
                    _rlog.debug("подобрано задание из веб-очереди", extra={
                        "context": {"skill": str(ref), "desired": desired,
                                    "initiator": "web-queue"}})
                await _install_chain(
                    cfg, access, slug=str(ref), channel=channel,
                    scope="global", project_path=None, force=False,
                    agent_target=agent_target, headless=True,
                    initiator="web-queue",
                )
                applied = (read_meta(store_root / ref) or {}).get("version")
                await client.report_device_apply(
                    slug=str(ref), ok=True, version=str(applied or desired),
                    skill_id=sid,
                )
                # #1024: установка видна в вебе /logs с привязкой к пользователю.
                with suppress(Exception):
                    await client.report_cli_log(
                        level="info",
                        message=f"CLI: навык {ref} установлен (v{applied or desired})",
                        logger="cli.install",
                        context={"skill": str(ref), "version": str(applied or desired)},
                    )
                attempts.pop(key, None)  # успех — счётчик сбрасываем
                report["applied"].append(ref)
            except Exception as exc:  # noqa: BLE001 — провал ОБЯЗАН быть виден
                attempts[key] = tried + 1
                gave_up = attempts[key] >= _MAX_INSTALL_ATTEMPTS
                msg = str(exc)
                if gave_up:
                    msg = (
                        f"установка не удалась после {_MAX_INSTALL_ATTEMPTS} "
                        f"попыток — прекращаю повторы. Последняя ошибка: {msg}"
                    )
                # C1/C2: причина провала — в daemon.log (ERROR), не ТОЛЬКО на бэк.
                # Раньше except слал лишь report_device_apply — в локальном логе
                # демона не оставалось следа, почему веб-задача не выполнилась.
                with suppress(Exception):
                    _ilog.error("установка из веб-очереди не удалась", extra={
                        "context": {"step": "install", "skill": str(ref),
                                    "desired": desired, "initiator": "web-queue",
                                    "attempts": attempts[key], "gave_up": gave_up,
                                    "error": str(exc)}})
                try:
                    await client.report_device_apply(
                        slug=str(ref), ok=False, error=msg, skill_id=sid
                    )
                except Exception:
                    pass  # сеть упала — сервер оставит задание в очереди
                report["failed"].append(ref)
        # Забываем счётчики для заданий, которых уже нет в очереди (сняты/сменили
        # версию) — файл не растёт бесконечно.
        _save_install_attempts({k: v for k, v in attempts.items() if k in live_keys})
        # #1102: обобщённые device-задачи (cli_upgrade / skill_update / generic)
        # из ТОГО ЖЕ ответа очереди — применяем и рапортуем факт.
        if device_tasks:
            with suppress(Exception):
                await _apply_device_tasks(
                    client, device_tasks, rlog=_rlog, ilog=_ilog
                )
    finally:
        # #1174: досылки ЗДЕСЬ БОЛЬШЕ НЕТ. Доставку общего outbox'а (логи CLI +
        # запуски навыков) делает ОДИН воркер в цикле демона
        # (``commands/daemon.py::_deliver_outbox``) — со своим троттлом и
        # backoff'ом. Раньше force-flush висел тут и на long-poll-ритме (такт ~2с)
        # означал бы запрос к бэку каждые пару секунд.
        #
        # #1191: ЧУЖОЙ клиент (SSE-сессия) не закрываем — иначе оборвали бы
        # живой стрим, из которого сами же и получили это тело.
        if own_client:
            await client.close()
    return report


async def _report_device_task_safe(
    client, cdid: str, task_id: int, status: str, error: str | None, *, ilog
) -> None:
    """Рапорт о device-task; сбой доставки не валит цикл (backend переотдаст)."""
    try:
        await client.report_device_task(
            client_device_id=cdid, task_id=task_id, status=status, error=error
        )
    except Exception as exc:  # noqa: BLE001 — сеть упала → задача останется в очереди
        with suppress(Exception):
            ilog.error("device-task: рапорт не доставлен", extra={"context": {
                "task_id": task_id, "status": status, "error": str(exc)}})


async def _apply_cli_upgrade_task(
    client, cdid: str, task: dict, *, rlog, ilog
) -> None:
    """#1102: применить device-task ``cli_upgrade`` — запустить self-upgrade до target.

    Рапорт ``applied`` — при успешном СТАРТЕ фонового апгрейда до ``target``
    (фактический итог покажет upgrade-result sidecar со следующей версии).
    ``failed`` с причиной — если спавн не удался. Дедуп: если апгрейд уже идёт
    (`_upgrade_already_running`), НЕ спавним второй и НЕ шлём терминальный статус —
    backend переотдаст задачу, а мы отрапортуем, когда лок освободится.

    ЗАПРЕТ произвольных пакетов: ставим ровно ``skillery-cli==<target>``
    (`_spawn_background_upgrade` пинует dist из `_upgrade_commands`). Даунгрейда
    не делаем: если текущая версия уже >= target — идемпотентно рапортуем applied.

    ВИДИМОСТЬ: исход КАЖДОЙ device-задачи (запущен / уже на версии / отложен /
    провал) пишется через ``ilog`` — ``install_logger`` держит СВОЙ INFO-хендлер и
    пишет всегда. Раньше успех и пропуск шли уровнем ACCESS(15), а стандартный
    уровень демона — ERROR, поэтому в ``daemon.log`` были видны ТОЛЬКО провалы:
    задача «делалась», а следов не оставляла. ACCESS остаётся для шума long-poll.
    """
    from skillery_cli import __version__ as current

    task_id = task.get("id")
    payload = task.get("payload") or {}
    target = str(payload.get("target_version") or "").strip()

    if not target:
        # Некорректный payload — рапортуем failed, иначе задача переотдаётся вечно.
        with suppress(Exception):
            ilog.error("cli_upgrade: пустой target_version", extra={"context": {
                "task_id": task_id, "task_type": "cli_upgrade",
                "initiator": "web-queue"}})
        await _report_device_task_safe(
            client, cdid, task_id, "failed", "target_version отсутствует", ilog=ilog
        )
        return

    # Уже на target (или новее) — апгрейда нет, но целевое состояние достигнуто:
    # идемпотентно закрываем задачу applied (и не даунгрейдим по ошибочному target).
    if not _is_newer(target, current):
        with suppress(Exception):
            ilog.info("cli_upgrade: уже на целевой версии", extra={"context": {
                "task_id": task_id, "task_type": "cli_upgrade", "target": target,
                "current": current, "status": "applied", "initiator": "web-queue"}})
        await _report_device_task_safe(
            client, cdid, task_id, "applied", None, ilog=ilog
        )
        return

    # Дедуп: апгрейд уже идёт — два параллельных рвут trampoline. Не спавним и не
    # рапортуем терминальный статус (backend переотдаст задачу на следующем такте).
    # Запись ОБЯЗАНА быть заметной: раньше пропуск был тихим (ACCESS), и задача
    # висела в `delivered` без единого следа о том, почему ничего не происходит.
    if _upgrade_already_running():
        with suppress(Exception):
            ilog.info(
                "cli_upgrade отложен: апгрейд уже идёт, повторим на следующем такте",
                extra={"context": {
                    "task_id": task_id, "task_type": "cli_upgrade", "target": target,
                    "status": "deferred", "initiator": "web-queue"}},
            )
        return

    if _spawn_background_upgrade(version=target):
        with suppress(Exception):
            ilog.info("cli_upgrade запущен", extra={"context": {
                "task_id": task_id, "task_type": "cli_upgrade", "target": target,
                "from": current, "status": "applied", "initiator": "web-queue"}})
        await _report_device_task_safe(
            client, cdid, task_id, "applied", None, ilog=ilog
        )
    else:
        with suppress(Exception):
            ilog.error("cli_upgrade: не удалось запустить обновление", extra={"context": {
                "task_id": task_id, "task_type": "cli_upgrade", "target": target,
                "initiator": "web-queue"}})
        await _report_device_task_safe(
            client, cdid, task_id, "failed",
            "не удалось запустить фоновое обновление", ilog=ilog,
        )


async def _apply_device_tasks(client, tasks: list[dict], *, rlog, ilog) -> None:
    """#1102: применить обобщённые device-задачи из очереди (initiator=web-queue).

    - ``cli_upgrade`` — запускаем self-upgrade до target и рапортуем факт
      (см. :func:`_apply_cli_upgrade_task`).
    - ``skill_update`` / ``generic`` — ЗАДЕЛ: пока лог (INFO) + skip, терминальный
      статус НЕ шлём (backend переотдаст, когда научимся их применять).

    Каждая задача изолирована: сбой одной не валит остальные и не роняет демон.
    Каждая задача ВИДНА: и успех, и пропуск идут в ``ilog`` (INFO пишется всегда),
    а не уровнем ACCESS, который на стандартном ERROR-уровне демона молчал.
    """
    from skillery_cli.core.identity import device_uid

    cdid = device_uid()
    for task in tasks:
        task_id = task.get("id")
        ttype = str(task.get("task_type") or "generic")
        if task_id is None:
            continue
        try:
            if ttype == "cli_upgrade":
                await _apply_cli_upgrade_task(client, cdid, task, rlog=rlog, ilog=ilog)
            else:
                # Задел под skill_update/generic: пропуск ВИДЕН (INFO), без
                # терминального рапорта — backend переотдаст задачу позже.
                with suppress(Exception):
                    ilog.info("device-task пока не поддержана — пропуск", extra={
                        "context": {"task_id": task_id, "task_type": ttype,
                                    "status": "skipped", "initiator": "web-queue"}})
        except Exception as exc:  # noqa: BLE001 — одна задача не валит остальные
            with suppress(Exception):
                ilog.error("device-task: обработка не удалась", extra={"context": {
                    "task_id": task_id, "task_type": ttype,
                    "initiator": "web-queue", "error": str(exc)}})


async def _daemon_cli_self_upgrade(cfg: ClientConfig, *, force: bool) -> bool:
    """#1102: каденс-fallback авто-апгрейда CLI в демоне (не push, а страховка).

    Слои надёжности, если push-задача ``cli_upgrade`` не прилетела, но демон жив:

    - **при СТАРТЕ демона** (``force=True``) — конвергируем на уже известный
      latest (из кэша), даже если PyPI в этом вызове не опрашивался: свежезагру-
      женная машина сразу подтягивается к последней версии;
    - **периодически** (``force=False``, раз в час из цикла демона) — спавним
      ТОЛЬКО на СВЕЖЕЙ PyPI-проверке (``fresh``), то есть максимум раз в сутки
      (суточный cooldown живёт в :func:`_check_cli_update_detailed`).

    Инварианты: PyPI не спамим (кэш ``cli_latest_version`` + 24ч cooldown);
    параллельных апгрейдов не плодим (`_upgrade_already_running` / лок worker'а).
    Fail-silent: страховка не должна валить демон-цикл. initiator=daemon-auto.
    """
    from skillery_cli import __version__ as current
    from skillery_cli.core.logging_setup import install_logger

    if not cfg.cli_auto_upgrade:
        return False
    _dlog = install_logger("daemon.log")
    try:
        latest, fresh = _check_cli_update_detailed(cfg)
    except Exception:  # noqa: BLE001 — проверка версии не повод валить демон
        return False
    if not latest:
        return False
    # Периодик — только на свежей проверке (иначе спавнили бы на каждом часе в
    # пределах суточного cooldown); старт (force) — можно и из кэша.
    if not (fresh or force):
        return False
    # Дедуп: апгрейд уже идёт — второй рвёт trampoline.
    if _upgrade_already_running():
        with suppress(Exception):
            _dlog.info(
                "cli self-upgrade отложен: апгрейд уже идёт, "
                "повторим на следующем такте",
                extra={"context": {"target": latest, "status": "deferred",
                                   "initiator": "daemon-auto"}},
            )
        return False
    if _spawn_background_upgrade(version=latest):
        with suppress(Exception):
            _dlog.info("cli self-upgrade запущен", extra={"context": {
                "from": current, "target": latest, "force": force,
                "status": "applied", "initiator": "daemon-auto"}})
        return True
    with suppress(Exception):
        _dlog.error(
            "cli self-upgrade: не удалось запустить обновление",
            extra={"context": {"target": latest, "initiator": "daemon-auto"}},
        )
    return False


async def _auto_update_hub_installs(
    cfg: ClientConfig,
    access: str,
    *,
    agent_target,  # IAgentTarget
    channel: str = "published",
) -> dict[str, list]:
    """Фоново поднять установленные ХАБ-навыки до latest published версии хаба.

    Отличие от :func:`_reconcile_hub_installs` (device-sync НАБОРА между
    устройствами — целевая версия там = ``installed_version`` из ``/me/installs``,
    т.е. то, что записано в вебе, а НЕ latest хаба): здесь целевая версия —
    ФАКТИЧЕСКИЙ latest published хаба (``install_bundle(ref,
    channel="published")``). Демон вызывает ОБА прохода: сначала device-sync,
    потом это авто-поднятие — поэтому новее опубликованная версия поднимается
    сама, а не «зависает» на записанной в вебе.

    Гейты:
    - ``cfg.auto_update`` (дефолт True) — off ⇒ no-op (device-sync прежний, до
      latest не поднимаем);
    - cooldown ``auto_update_cooldown_min`` — общий таймстамп
      ``last_auto_update_at`` с :func:`_maybe_auto_update` (foreground-путь),
      чтобы фон и команды не дёргали bump чаще раза в N минут.

    Только ``source == "hub"`` навыки локального стора; git-url/local-path
    пропускаются (их latest в хабе нет). Best-effort per-skill: сбой одного
    (``install_bundle`` 404 = снят/не-хаб, сетевой сбой, падение установки) не
    валит остальные и не роняет демон. Не даунгрейдит (строго :func:`_is_newer`).
    Устанавливает scope=global (как ``skillery install`` без ``--project``).

    Возвращает report ``{updated, skipped, failed}`` (списки ref).
    """
    report: dict[str, list] = {"updated": [], "skipped": [], "failed": []}
    # #1145: чиним меты, испорченные китом <0.3.3 (снапшот из хаба помечался
    # source="local-path"), ДО любых гейтов ниже. Отбор кандидатов идёт строго
    # по source == "hub", поэтому такой навык молча выпадал именно отсюда и
    # застывал на своей версии навсегда. Одноразово и идемпотентно: маркер в
    # конфиге, повторный проход стоит одну проверку флага. Выше гейтов — чтобы
    # выключённое автообновление или ещё не истёкший cooldown не откладывали
    # починку меты на неопределённый срок.
    from skillery_cli.core.store_migrations import (
        ensure_shims_route_through_runner,
        ensure_store_meta_migrated,
    )

    ensure_store_meta_migrated(cfg)
    # #1221: у уже установленных навыков shim'ы старого формата зовут entrypoint
    # напрямую — мимо учёта. Перегенерация по sidecar'ам и есть разница между
    # «учёт для новых установок» и «учёт для всех». Тоже ВЫШЕ гейтов ниже:
    # выключённое автообновление не должно означать выключённый учёт.
    ensure_shims_route_through_runner()
    if not cfg.auto_update:
        # Автообновление выключено пользователем — оставляем device-sync как есть,
        # до latest ничего не поднимаем.
        return report
    # Cooldown (общий с _maybe_auto_update): не чаще раза в N минут.
    cooldown = timedelta(minutes=cfg.auto_update_cooldown_min)
    if cfg.last_auto_update_at:
        try:
            last = datetime.fromisoformat(cfg.last_auto_update_at)
            if datetime.now(UTC) - last < cooldown:
                return report
        except ValueError:
            pass

    store_root = cfg.effective_store_dir()
    hub_skills = [
        s for s in _collect_store_skills(store_root) if s.get("source") == "hub"
    ]
    if not hub_skills:
        # Двигаем cooldown даже впустую — иначе фон бил бы стор каждый reconcile.
        _touch_auto_update_cooldown(cfg)
        return report

    # Сначала собираем latest-бандлы (сеть) под одним клиентом, потом ставим —
    # так HttpClient закрывается до потенциально долгих git-операций install.
    client = HubClient(
        base_url=cfg.base_url, access_token=access,
        on_token_refresh=_make_refresh_callback(cfg),
    )
    candidates: list[tuple[str, str, dict]] = []  # (ref, local_version, bundle)
    try:
        for s in hub_skills:
            ref = s["ref"]
            try:
                bundle = await client.install_bundle(ref, channel=channel)
            except Exception:
                # 404 (снят/не-хаб) / сетевой сбой — мягкий пропуск, не валим фон.
                report["skipped"].append(ref)
                continue
            candidates.append((ref, s.get("version") or "0.0.0", bundle))
    finally:
        await client.close()

    from skillery_cli.core.logging_setup import get_logger, install_logger

    _rlog = get_logger("reconcile")
    for ref, local_version, bundle in candidates:
        remote_version = bundle.get("version") or ""
        # bundle без repo_url = stub-источник — обновлять нечем (тот же инвариант,
        # что в _maybe_auto_update: такой «апдейт» затирал бы контент stub'ом).
        if not bundle.get("repo_url"):
            with suppress(Exception):
                _rlog.debug("auto-update: пропуск (stub без repo_url)", extra={
                    "context": {"skill": str(ref), "initiator": "daemon-auto"}})
            report["skipped"].append(ref)
            continue
        # Не даунгрейд: ставим ТОЛЬКО если latest строго новее локального.
        if not _is_newer(remote_version, local_version):
            with suppress(Exception):
                _rlog.debug("auto-update: пропуск (не новее локального)", extra={
                    "context": {"skill": str(ref), "local": local_version,
                                "remote": remote_version, "initiator": "daemon-auto"}})
            report["skipped"].append(ref)
            continue
        try:
            with suppress(Exception):
                _rlog.debug("auto-update: поднимаю до latest", extra={
                    "context": {"skill": str(ref), "local": local_version,
                                "remote": remote_version, "initiator": "daemon-auto"}})
            await _install_chain(
                cfg, access, slug=str(ref), channel=channel, scope="global",
                project_path=None, force=False, agent_target=agent_target,
                initiator="daemon-auto",
            )
            report["updated"].append(ref)
        except Exception as exc:  # noqa: BLE001 — один навык не валит фон
            # Падение установки одного навыка (git/ФС) не трогает остальные, но
            # причина ОБЯЗАНА быть видна в daemon.log (не молчаливый append).
            with suppress(Exception):
                install_logger("daemon.log").error(
                    "auto-update навыка не удался", extra={
                        "context": {"step": "install", "skill": str(ref),
                                    "remote": remote_version,
                                    "initiator": "daemon-auto", "error": str(exc)}})
            report["failed"].append(ref)

    # #912: автообновление обязано РАПОРТОВАТЬ факт. Иначе веб продолжал бы
    # показывать старую версию (состояние устройства обновляется только
    # рапортом), и «навык обновился сам» выглядело бы как «ничего не менялось».
    if report["updated"]:
        await _report_auto_updates(cfg, access, report["updated"])

    _touch_auto_update_cooldown(cfg)
    return report


async def _report_auto_updates(
    cfg: ClientConfig, access: str, refs: list
) -> None:
    """Сообщить хабу версии, которые автообновление реально положило в стор.

    Версию берём ИЗ СТОРА (а не из бандла) — рапортуем то, что лежит на диске.
    Best-effort: старый бэкенд без эндпоинта или сетевой сбой не должны валить
    фоновой проход.
    """
    store_root = cfg.effective_store_dir()
    client = HubClient(
        base_url=cfg.base_url, access_token=access,
        on_token_refresh=_make_refresh_callback(cfg),
    )
    try:
        for ref in refs:
            try:
                meta = read_meta(store_root / str(ref)) or {}
                version = meta.get("version")
                if not version:
                    continue
                await client.report_device_apply(
                    slug=str(ref), ok=True, version=str(version)
                )
            except Exception:
                continue
    finally:
        await client.close()


def _touch_auto_update_cooldown(cfg: ClientConfig) -> None:
    """Подвинуть общий cooldown-таймстамп авто-обновления (best-effort save)."""
    cfg.last_auto_update_at = datetime.now(UTC).isoformat()
    try:
        cfg.save()
    except Exception:
        pass


def cmd_pull(
    agent: Optional[str] = typer.Option(None),
    channel: str = typer.Option("published"),
    force: bool = typer.Option(
        False, "--force", help="Перекачать даже если локальная версия актуальна",
    ),
) -> None:
    """Скачать навыки, помеченные установленными в вебе («нажал Установить»).

    Тянет ``/me/installs`` и докачивает отсутствующие/устаревшие в глобальный
    стор (как ``skillery install`` без ``--project``). Уже актуальные —
    пропускает. Это вторая половина потока «установка из веба»: веб помечает
    навык установленным, CLI ``pull`` приносит файлы. Нужен login.
    """
    cfg = ClientConfig.load()
    if not cfg.is_logged_in():
        emit_error("NOT_LOGGED_IN", "Сначала: skillery login <invite>")
        raise typer.Exit(1)
    access = _get_access_token()
    target = get_target(agent or cfg.agent)

    async def _do() -> None:
        report = await _reconcile_hub_installs(
            cfg, access, channel=channel, agent_target=target, force=force,
        )

        def _render(r: dict) -> None:
            console.print(
                f"[green]pull[/]: ↓downloaded {len(r['downloaded'])}  "
                f"↑updated {len(r['updated'])}  ={len(r['skipped'])} skipped  "
                f"✗{len(r['failed'])} failed"
            )
            for s in r["failed"]:
                console.print(f"  [yellow]✗ не удалось получить:[/] {s}")

        emit_data(report, text_renderer=_render)

    _run(_do())


def _collect_store_skills(store_dir: Path) -> list[dict]:
    """Перечислить навыки локального стора → ``[{ref, skill_id, source, version}]``.

    ``ref`` = идентичность папки стора (slug, для slug-less — числовой id, как в
    ``_reconcile_hub_installs``). Только папки с ``_skill_meta.json`` (наши
    материализованные навыки) — чужие копии без меты игнорируются.
    """
    out: list[dict] = []
    if not store_dir.is_dir():
        return out
    for d in sorted(store_dir.iterdir(), key=lambda p: p.name):
        if not d.is_dir():
            continue
        meta = read_meta(d)
        if meta is None:
            continue
        ref = str(meta.get("slug") or d.name)
        out.append({
            "ref": ref,
            "skill_id": meta.get("skill_id"),
            "source": meta.get("source"),
            "version": meta.get("version"),
        })
    return out


def cmd_push(
    channel: str = typer.Option("published"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Показать что было бы отмечено, ничего не слать",
    ),
) -> None:
    """Отметить локальный набор навыков установленным в хабе (вторая половина sync).

    Зеркало ``pull``: ``pull`` тянет ``/me/installs`` → локальный стор; ``push``
    берёт локальный стор → помечает каждый навык установленным в хабе
    (``POST /skills/{slug}/install``), чтобы он попал в ``/me/installs`` и
    подтянулся ``pull``'ом на другом устройстве. Так личный набор навыков
    синхронизируется между устройствами через хаб-хранилище (A → хаб → B).

    Навыки, которых нет в хабе (авторские local-path/git, ещё не
    опубликованные) → 404 → в ``skipped`` (сначала ``skillery publish``). Нужен
    login.
    """
    cfg = ClientConfig.load()
    if not cfg.is_logged_in():
        emit_error("NOT_LOGGED_IN", "Сначала: skillery login")
        raise typer.Exit(1)
    access = _get_access_token()
    skills = _collect_store_skills(cfg.effective_store_dir())
    report: dict[str, list] = {"pushed": [], "skipped": [], "failed": []}

    if dry_run:
        report["pushed"] = [s["ref"] for s in skills]
        emit_data(
            {**report, "dry_run": True},
            text_renderer=lambda r: console.print(
                f"[green]push --dry-run[/]: отметил бы {len(r['pushed'])} навык(ов): "
                + ", ".join(r["pushed"])
            ),
        )
        return

    async def _do() -> None:
        client = HubClient(
            base_url=cfg.base_url, access_token=access,
            on_token_refresh=_make_refresh_callback(cfg),
        )
        try:
            for s in skills:
                ref = s["ref"]
                try:
                    await client.install_skill(ref, channel=channel)
                    report["pushed"].append(ref)
                except ApiError as e:
                    # 404 = навыка нет в хабе (не опубликован) → skip, не fail.
                    if e.status_code == 404:
                        report["skipped"].append(ref)
                    else:
                        report["failed"].append(ref)
        finally:
            await client.close()

        def _render(r: dict) -> None:
            console.print(
                f"[green]push[/]: ↑pushed {len(r['pushed'])}  "
                f"={len(r['skipped'])} skipped (нет в хабе)  "
                f"✗{len(r['failed'])} failed"
            )
            for s in r["skipped"]:
                console.print(
                    f"  [yellow]∅ нет в хабе:[/] {s} — сначала skillery publish"
                )
            for s in r["failed"]:
                console.print(f"  [red]✗ не удалось отметить:[/] {s}")

        emit_data(report, text_renderer=_render)

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
    # .skillery/skills.toml — иначе следующий `sync --prune` снимет его
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
    # Служебная зона стора (.backups) — не навык: перечисляем через единую точку.
    for d in iter_store_skill_dirs(store_root):
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


def cmd_store_backups(
    slug: Optional[str] = typer.Argument(
        None, metavar="[НАВЫК]", help="Показать резервы только этого навыка"
    ),
) -> None:
    """Резервные копии навыков в сторе (#1405).

    Резерв создаётся, когда установка из Хаба забирает имя, занятое навыком из
    другого источника (локальная папка / произвольный git): прежний каталог не
    затирается, а целиком уезжает в служебную зону стора. Вернуть —
    ``skillery store restore <навык> [--backup <id>]``.
    """
    from skillery_cli.core.store_backup import describe_backup, list_backups

    cfg = ClientConfig.load()
    if not isinstance(slug, str):
        slug = None
    items = list_backups(cfg.effective_store_dir(), slug)

    def _render(rows: list) -> None:
        if not rows:
            console.print("[dim]Резервных копий нет.[/]")
            return
        table = Table(title="Резервы навыков (свежие сверху)")
        table.add_column("навык")
        table.add_column("id")
        table.add_column("что сохранено")
        table.add_column("причина")
        for r in rows:
            table.add_row(
                str(r.get("dir_name") or "—"), str(r.get("id") or "—"),
                describe_backup(r), str(r.get("reason") or "—"),
            )
        console.print(table)
        console.print(
            "[dim]Откат: skillery store restore <навык> --backup <id>[/]"
        )

    emit_data(items, text_renderer=_render)


def cmd_store_restore(
    slug: str = typer.Argument(..., metavar="НАВЫК", help="Имя навыка в сторе"),
    backup: Optional[str] = typer.Option(
        None, "--backup", help="id резерва (по умолчанию — самый свежий)"
    ),
) -> None:
    """Вернуть навык из резерва (откат вытеснения хаб-установкой, #1405).

    Откат сам обратим: то, что стоит сейчас, не удаляется, а уезжает в новый
    резерв — вернуться обратно можно этой же командой.
    """
    from skillery_cli.core.store_backup import (
        BackupError,
        describe_backup,
        restore_backup,
    )

    cfg = ClientConfig.load()
    if not isinstance(backup, str):
        backup = None
    try:
        result = restore_backup(cfg.effective_store_dir(), slug, backup)
    except BackupError as exc:
        emit_error("NOT_FOUND", str(exc))
        raise typer.Exit(1) from exc

    def _render(p: dict) -> None:
        rec = p["restored"]
        console.print(
            f"[green]✓[/] Навык [bold]{slug}[/] возвращён из резерва "
            f"{rec.get('id')} ({describe_backup(rec)})"
        )
        if p.get("replaced"):
            console.print(
                f"[dim]Прежняя установка сохранена в резерв "
                f"{p['replaced'].get('id')}[/]"
            )

    emit_data(result, text_renderer=_render)


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
    from skillery_cli.core.installer import _force_rmtree

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
    # #1405: резервы (.backups) ссылками не адресуются НИКОГДА — попади они в
    # обход, gc снёс бы ровно то, ради чего резерв и делается. Перечисление —
    # через единую точку, которая служебную зону не отдаёт.
    for d in iter_store_skill_dirs(store_root):
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
                scope_label = "project" if proj else "global"
                # Устойчивость: навык может быть НЕ из хаба (локальный/ручной —
                # bitrix24, mangoproxy, *-local). Тогда install_bundle бросает
                # ApiError (404 «не найден») — ловим ПОШТУЧНО и пропускаем этот
                # навык, не роняя весь `update` (раньше первый не-хабовый навык
                # валил обновление всех остальных).
                try:
                    bundle = await client.install_bundle(ref, channel=channel)
                except ApiError as e:
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
                            "skipped": True,
                            "skip_reason": "not_in_hub"
                            if e.status_code == 404
                            else "hub_error",
                        }
                    )
                    continue
                # #2282: обновления требует не только сам навык, но и его
                # цепочка — зависимость могла выйти новее либо появиться в
                # новой версии манифеста.
                stale_deps = _stale_chain_deps(
                    bundle, target=target, project=proj
                )
                # Гейт как в _maybe_auto_update: обновляем ТОЛЬКО если бандл
                # строго новее (баг B8 — раньше `== ` пропускал лишь равенство,
                # т.е. downgrade на младшую версию проходил в install).
                if not _is_newer(bundle["version"], current_version) and not stale_deps:
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
                chain = bundle.get("dependencies_chain") or []
                if len(chain) > 1:
                    # Навык С ЗАВИСИМОСТЯМИ обновляем тем же путём, что и
                    # ставим, — ``_install_chain``: он обходит цепочку целиком
                    # и умеет снапшот-канал (без git-кред устройства). Прямой
                    # ``installer.install`` этого не умеет: он обновил бы один
                    # навык и молча оставил зависимость на старой версии.
                    # Версии ДО установки: после неё на диске уже новые, и
                    # «from» в отчёте равнялся бы «to» (то есть апдейт выглядел
                    # бы как пустой).
                    before = {
                        entry[0]: (
                            read_meta(target.slug_dir(entry[0], project=proj)) or {}
                        ).get("version", "0.0.0")
                        for entry in chain
                        if entry and entry[0]
                    }
                    installed = await _install_chain(
                        cfg,
                        access,
                        slug=ref,
                        channel=channel,
                        scope=scope_label,
                        project_path=proj,
                        force=True,
                        agent_target=target,
                        initiator="cli",
                    )
                    for row in installed:
                        row_slug = row.get("slug")
                        results.append(
                            {
                                "slug": row_slug,
                                "skill_id": row.get("skill_id"),
                                "ref": row_slug or ref,
                                "scope": row.get("scope") or scope_label,
                                "project": str(proj) if proj else None,
                                "from": before.get(row_slug, current_version),
                                "to": row.get("version"),
                                "updated": not row.get("skipped"),
                                "via_chain": True,
                            }
                        )
                        if not row.get("skipped"):
                            track_skill_event(
                                "skill.update",
                                slug=row_slug or ref,
                                version=row.get("version"),
                                scope=row.get("scope") or scope_label,
                                agent=target.name,
                            )
                    continue
                up = installer.install(
                    slug=meta_slug,
                    skill_id=meta_skill_id,
                    version=bundle["version"],
                    commit_sha=bundle["commit_sha"],
                    repo_url=bundle.get("repo_url"),
                    skill_path=bundle.get("skill_path"),
                    manifest=bundle["manifest"],
                    project=proj,
                )
                if up.skipped:
                    # Stub-guard: installer отказался затирать живой контент —
                    # честно рапортуем пропуск, событие skill.update не шлём.
                    results.append(
                        {
                            "slug": meta_slug,
                            "skill_id": up.skill_id or meta_skill_id,
                            "ref": ref,
                            "scope": scope_label,
                            "project": str(proj) if proj else None,
                            "from": current_version,
                            "to": current_version,
                            "updated": False,
                            "skipped": True,
                            "skip_reason": up.skip_reason,
                        }
                    )
                    continue
                # gap A: контент навыка обновлён — переустановить tooling
                # (runtime_deps/CLI/MCP) под манифест НОВОЙ версии. manifest и
                # project берём из контекста апдейта (как в install).
                _apply_tooling(
                    up, bundle["manifest"], agent_target=target, project=proj
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
                # track skill.update event.
                track_skill_event(
                    "skill.update",
                    slug=ref,
                    version=bundle["version"],
                    scope=scope_label,
                    agent=target.name,
                )
        finally:
            await client.close()
        cfg.last_auto_update_at = datetime.now(UTC).isoformat()
        cfg.save()

        def _render(rows: list) -> None:
            for r in rows:
                # slug может быть None у slug-less skill — показываем ref (id).
                label = r.get("slug") or r.get("ref")
                if r.get("skipped"):
                    console.print(
                        f"[yellow]⚠ {label} ({r['scope']}): обновление пропущено "
                        f"({r.get('skip_reason')})[/]"
                    )
                elif r["updated"]:
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
    # снять CLI/MCP навыка ДО remove — при --purge стор (и его манифест)
    # удаляется, поэтому revert читает манифест из стора, пока он на месте.
    _revert_tooling(
        slug, agent_target=target, project=project_path,
        store_dir=cfg.effective_store_dir() / slug,
    )
    result = installer.remove(
        slug=slug, project=project_path, keep_local=keep_local, purge=purge
    )

    # Фикс 5а: симметрия с disable — снятый из project scope навык убираем и
    # из .skillery/skills.toml, иначе следующий sync вернёт его обратно.
    manifest_removed = False
    if project_path is not None:
        manifest_removed = project_manifest.remove(project_path, slug)

    if not result.removed:
        # #1146: «ничего не сняли» ≠ «навыка нет». Чаще всего он ЕСТЬ, просто
        # лежит в другом scope: демон ставит в global, а remove без --scope
        # идёт в project (`cfg.default_install_scope`). Раньше в этом случае
        # печаталось голое «Не установлен» — и пользователь оставался с навыком
        # на диске и без единой подсказки, что делать.
        in_store = (cfg.effective_store_dir() / slug).is_dir()
        hint = ""
        if in_store:
            hint = (
                " — но он есть в сторе: снять глобально `--scope global`, "
                "удалить совсем `--purge`"
            )

        def _render_missing(_: dict) -> None:
            console.print(
                f"[yellow]Не установлен[/] ({result.scope}): {slug} "
                f"(нет папки {result.target_dir}){hint}"
            )

        emit_data(
            {
                "slug": slug,
                "scope": result.scope,
                "removed": False,
                "kept_local": False,
                "purged": result.purged,
                "manifest_removed": manifest_removed,
                "in_store": in_store,
                "path": str(result.target_dir),
            },
            text_renderer=_render_missing,
        )
        return

    # + аналитика-эпик: разводим disable vs uninstall.
    # project-scope без --purge = только снята ссылка (стор цел) → skill.disable.
    # --purge ИЛИ global-scope = навык удалён из стора → skill.uninstall.
    if result.scope == "project" and not result.purged:
        track_skill_event(
            "skill.disable", slug=slug, scope="project", agent=target.name
        )
    else:
        track_skill_event(
            "skill.uninstall",
            slug=slug,
            scope=result.scope,
            agent=target.name,
            extra={"kept_local": result.kept_local},
        )
        # #1490: навык ушёл из стора — забываем его требования лиза (см. тот же
        # комментарий в removal-ветке очереди устройства).
        with suppress(Exception):
            from skillery_cli.core.leases import RequirementsIndex

            _idx = RequirementsIndex.load()
            if _idx.forget_skill(slug):
                _idx.save()

    # #1146: терминология — ПО ФАКТУ содеянного, а не по названию команды.
    # Кит без --purge снимает из project-scope ТОЛЬКО ссылку; стор цел, навык
    # никуда не делся. Функция это уже знала (аналитика выше шлёт skill.disable),
    # но печатала «Удалён» — противоречие внутри одного вызова, из-за которого
    # следующий `installed` «необъяснимо» показывал якобы удалённый навык.
    disabled_only = result.scope == "project" and not result.purged

    # #2282: снятие потребителя уносит за собой ЕГО зависимости — но только те,
    # что (а) приехали как зависимость и (б) больше никем не требуются. Явно
    # поставленный навык остаётся, даже осиротев: пользователь просил его сам
    # (инвариант `apt autoremove`, см. skillery_cli.core.install_reason).
    # Отключение из проекта (ссылка снята, стор цел) зависимости не трогает —
    # навык никуда не делся, следующий `enable` вернёт его без сети.
    removed_dependencies: list[str] = []
    if not disabled_only:
        removed_dependencies = _sweep_orphan_dependencies(
            installer, cfg.effective_store_dir(), consumer=slug,
            project_path=project_path, agent_target=target,
        )

    def _render(_: dict) -> None:
        if disabled_only:
            console.print(
                f"[green]✓[/] Отключён (project): {slug} — остаётся в сторе, "
                "полное удаление: --purge"
            )
        elif result.kept_local:
            console.print(
                f"[green]✓[/] Удалён ({result.scope}): {slug} "
                f"[dim](preserved_paths сохранены в {result.target_dir})[/]"
            )
        else:
            console.print(f"[green]✓[/] Удалён ({result.scope}): {slug}")
        if manifest_removed:
            console.print("[dim]Убран из .skillery/skills.toml[/]")
        for dep in removed_dependencies:
            console.print(
                f"[green]✓[/] Убрана зависимость: {dep} "
                "[dim](приехала ради снятого навыка, больше никем не требуется)[/]"
            )

    emit_data(
        {
            "slug": slug,
            "scope": result.scope,
            "removed": True,
            "removed_dependencies": removed_dependencies,
            # Машинному потребителю тоже нужна разница «отключён» vs «удалён»:
            # по одному removed=True он её не восстановит.
            "disabled_only": disabled_only,
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


def _run_publish_denylist_gate(skill_dir: Path, *, force: bool) -> None:
    """#204: гейт денилиста ВНУТРЕННЕЙ ИНФЫ поверх секрет-скана — «не только
    секреты». Через ``skillgate.scan_repo`` (= ``run_gate(manifest=False)``):
    ловит абсолютные windows-пути (``C:/Users/<num>``) и публичные IP, которые
    не должны утечь в публикуемый навык. Секреты покрывает
    ``_run_publish_secret_scan`` (gitleaks/regex).

    Манифест-часть ``run_gate`` НЕ применяем осознанно: ``check_manifest``
    требует ``version`` в SKILL.md-frontmatter, а CLI берёт версию из ``--tag``
    (стандартные навыки версию в frontmatter не кладут) → был бы false-fail.
    Манифест валидируется своим путём (``build_manifest`` ниже).

    Kit недоступен ⇒ best-effort no-op (секрет-скан уже отработал).
    """
    try:
        import skillgate
        from skillgate import run_gate
        from skillgate.rules import load_rules
    except ImportError:
        return
    # run_gate(manifest=False) = scan_repo: только секреты+денилист, без манифеста.
    report = run_gate(skill_dir, manifest=False)
    data_dir = Path(skillgate.__file__).parent / "data"
    internal_ids = {
        r.id
        for r in load_rules(data_dir / "denylist.toml")
        if r.category == "internal-info"
    }
    internal = [f for f in report.findings if f.rule in internal_ids]
    if not internal:
        return
    fails = [f for f in internal if f.severity.value == "fail"]
    payload = [
        {"file": f.file, "line": f.line, "rule": f.rule, "message": f.message}
        for f in internal
    ]
    if is_json():
        emit_error(
            "publish_denylist_failed"
            if (fails and not force)
            else "publish_denylist_warn",
            f"Денилист внутренней инфы: {len(internal)} находок "
            f"({len(fails)} блокирующих)",
            findings=payload,
            forced=force,
        )
    else:
        tone = "red" if fails else "yellow"
        console.print(
            f"[{tone}]denylist: {len(internal)} находок внутренней инфы "
            f"({len(fails)} блокирующих)[/]"
        )
        for f in internal:
            console.print(f"  [dim]{f.file}:{f.line}[/] ({f.rule}) {f.message}")
    if fails and not force:
        if not is_json():
            console.print(
                "[red]Publish прерван.[/] Уберите внутреннюю инфу или "
                "[bold]--force[/]."
            )
        raise typer.Exit(1)


def _connect_private_repo(
    repo_url: str, repo_token: Optional[str]
) -> Optional[str]:
    """Подключить приватный репо к автосинку хаба (per-project scoped).

    Возвращает ``repo_token`` для payload'а: для GitLab может создать
    project-токен через ``glab`` (заменяя account-wide PAT); для GitHub без
    токена — подсказывает установить App ``skillery-sync`` на репо (хаб синкнёт
    installation-токеном, PAT не нужен). Всё best-effort: сбой ⇒ поведение как
    раньше (ручной ``--repo-token`` / publish без токена).
    """
    from skillery_cli import _branding
    from skillery_cli.core import repo_connect

    provider = repo_connect.infer_provider(repo_url)
    slug = repo_connect.parse_repo_slug(repo_url)
    if provider is None or slug is None:
        return repo_token

    # Токен уже дали вручную — уважаем, ничего не трогаем.
    if repo_token:
        return repo_token

    if provider == "gitlab":
        token = repo_connect.create_gitlab_project_token(slug)
        if token:
            console.print(
                f"[green]✓[/] Создан GitLab project-токен (read_api) для "
                f"[bold]{slug.path}[/] — приватный репо синкнётся без "
                "account-wide PAT."
            )
            return token
        console.print(
            "[yellow]Не удалось авто-создать GitLab project-токен[/] "
            "(нужен установленный и авторизованный [bold]glab[/] с правами "
            "maintainer). Если репо приватный — передайте "
            "[bold]--repo-token <PAT со scope read_api>[/]."
        )
        return repo_token

    # provider == "github": проба публичности → подсказка про App.
    is_public = repo_connect.github_repo_is_public(slug)
    if is_public is True:
        return repo_token  # публичный — App/токен не нужен.
    install_url = _branding.GITHUB_APP_INSTALL_URL
    console.print(
        f"[yellow]Приватный GitHub-репо?[/] Установите App "
        f"[bold]{_branding.GITHUB_APP_SLUG}[/] на [bold]{slug.path}[/] — "
        "хаб будет синкать его read-only installation-токеном (PAT не нужен):"
    )
    console.print(f"  [cyan]{install_url}[/]")
    # Открываем браузер только в интерактиве (не в CI/скриптах).
    if sys.stdout.isatty():
        import webbrowser

        try:
            webbrowser.open(install_url)
        except Exception:  # noqa: BLE001 — открытие браузера best-effort
            pass
    return repo_token


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
    repo_token: Optional[str] = typer.Option(
        None, "--repo-token",
        help="PAT для приватного репо (github/gitlab). Сохраняется на хабе "
        "зашифрованным (repo-credential) — БЕЗ него автосинк приватного репо "
        "не сможет клонировать. Провайдер выводится из repo_url.",
    ),
    skill_path: Optional[str] = typer.Option(
        None, "--skill-path",
        help="Подпапка навыка в репо (skills/<name>/), #268. По умолчанию — корень.",
    ),
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
        # #204: полный гейт «не только секреты» — денилист внутренней инфы.
        _run_publish_denylist_gate(skill_dir, force=force)

    version = tag.lstrip("v")
    # #1489: способности объявляет манифест навыка. Разбираем ДО сборки и до
    # сети — опечатка в [[capabilities]] обязана стоить один разбор TOML, а не
    # весь проход публикации с 422 в конце.
    from skillery_cli.core.capability_manifest import (
        CapabilityManifestError,
        capabilities_of,
    )

    try:
        declared_capabilities = capabilities_of(skill_dir)
    except CapabilityManifestError as exc:
        emit_error("VALIDATION", str(exc))
        raise typer.Exit(2) from exc
    manifest = build_manifest(skill_dir, version=version)
    actual_commit = commit_sha or git_commit_sha(skill_dir)
    if not actual_commit:
        # Папка вне git (или git недоступен) — коммита нет. Плейсхолдер
        # остаётся ради контракта хаба (commit_sha обязателен), но пользователь
        # должен знать: такой «sha» не резолвится ни в одном провайдере, и
        # превью репозитория будет опираться на теги, а не на версию.
        actual_commit = "0" * 7
        emit_message(
            "Коммит не определён (папка вне git) — версия будет опубликована "
            "без привязки к коммиту; вкладка «Исходники» покажет последний тег.",
            level="warning",
        )

    # Приватный репо → per-project scoped авторизация автосинка (GitLab:
    # авто-project-токен через glab; GitHub: подсказка установить App). В
    # dry-run сеть/glab не трогаем.
    if repo_url and not dry_run:
        repo_token = _connect_private_repo(repo_url, repo_token)

    # Парсинг тегов: очищаем скобки [ ] { }, дробим по запятой.
    def _parse_tags(tags_str: str) -> list[str]:
        # Убираем квадратные и фигурные скобки со скобок
        clean = tags_str.strip()
        clean = clean.lstrip("[{").rstrip("]}")
        # Дробим по запятой, стриппим каждый
        return [t.strip() for t in clean.split(",") if t.strip()]

    payload = {
        "slug": slug,
        "title": title or manifest.description.split("\n", 1)[0][:255] or slug,
        "description": description or manifest.description or slug,
        "semver": version,
        "channel": channel,
        "commit_sha": actual_commit,
        "tags": (
            _parse_tags(tags)
            if tags else manifest.tags
        ),
        "repo_url": repo_url,
        "repo_token": repo_token,
        "skill_path": skill_path,
        "is_super": is_super,
        "manifest": {
            "version": manifest.version,
            "description": manifest.description,
            "triggers": manifest.triggers,
            "tags": manifest.tags,
            "files": manifest.files,
            "dependencies": manifest.dependencies,
            "preserved_paths": manifest.preserved_paths,
            # Tooling-поля (kind=tooling): без них backend сохранил бы пустой
            # cli/runtime_dependencies → install не поставил бы сам CLI-инструмент.
            "kind": manifest.kind,
            "cli": manifest.cli,
            "mcp": manifest.mcp,
            "runtime_dependencies": manifest.runtime_dependencies,
            # #1489: [[capabilities]] — из ЭТОГО объявления хаб выводит реестр
            # способностей версии (sync_capabilities_from_manifest). Без поля
            # опубликованная через CLI версия не завела бы ни одной строки, и
            # выдать способность отдельно от навыка стало бы невозможно.
            "capabilities": declared_capabilities,
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
        except ApiError as exc:
            # #1489: 409 CAPABILITY_NAME_CONFLICT — единственный отказ, который
            # автор навыка не в состоянии понять по коду: локально всё
            # валидно, а имя занято ЧУЖИМ навыком, о чём знает только хаб.
            # Голый код здесь = тикет в поддержку, поэтому объясняем причину и
            # называем следующее действие.
            from skillery_cli.commands.capability import explain_api_error

            hint = explain_api_error(exc)
            if hint is None:
                raise
            emit_error(exc.code, hint, status_code=exc.status_code)
            raise typer.Exit(1) from exc
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


def cmd_skill_sync_versions(
    slug: str = typer.Argument(
        ..., metavar="ID_ИЛИ_SLUG", help="id-или-slug скилла (backend принимает оба)"
    ),
    channel: str = typer.Option("published"),
) -> None:
    """[hub.admin] Подтянуть новые git-теги навыка как версии (по id-или-slug).

    #2267: действие над НАВЫКОМ живёт в группе ``skill``, а не в группе по
    имени роли. Прежнее ``admin sync-skill`` осталось скрытым алиасом.
    Парная команда чтения — ``skill sync-status``.
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
            r = await client.sync_skill(slug, channel=channel)
        finally:
            await client.close()

        def _render(p: dict) -> None:
            console.print(f"[green]✓[/] Sync {p['skill_slug']}:")
            console.print(f"  Новые:        {p['new_versions'] or '—'}")
            console.print(f"  Существовали: {p['existing_versions'] or '—'}")

        emit_data(r, text_renderer=_render)

    _run(_do())


def cmd_skill_yank(
    slug: str = typer.Argument(
        ..., metavar="ID_ИЛИ_SLUG", help="id-или-slug навыка"
    ),
    version: str = typer.Argument(..., metavar="SEMVER", help="версия, напр. 1.0.0"),
    unyank: bool = typer.Option(
        False, "--unyank", help="Вернуть ранее снятую версию"
    ),
) -> None:
    """[skill.manage] Снять (yank) версию навыка — исключить из latest/install.

    Снятая версия остаётся в истории; `--unyank` возвращает её обратно (#340).
    #2267: живёт в группе ``skill`` (действие над навыком); прежнее
    ``admin yank`` осталось скрытым алиасом.
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
            await client.yank_skill_version(
                slug=slug, semver=version, yank=not unyank
            )
        finally:
            await client.close()
        verb = "Возвращена" if unyank else "Снята"
        emit_data(
            {"slug": slug, "version": version, "yanked": not unyank},
            text_renderer=lambda _p: console.print(
                f"[green]✓[/] {verb} версия {slug}@{version}"
            ),
        )

    _run(_do())


# #2267: `cmd_admin_company_create` и `cmd_admin_invite` УДАЛЕНЫ — это были
# вторые реализации уже существующих действий:
#   * создание компании — `commands.company.cmd_company_create` (`company create`);
#   * выдача инвайта    — `commands.member.cmd_member_invite` (`member invite`).
# Обе шлют ровно те же flat-запросы (POST /companies, POST /invites), поэтому
# копии не нужны: одно действие = одна реализация. Прежние имена `admin
# company-create` / `admin invite` остались СКРЫТЫМИ deprecated-алиасами,
# делегирующими в канонические функции (см. `_register_admin_compat`).


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

    ``__version__`` живёт в ``skillery_cli/__init__.py`` — пробрасываем его
    в typer (раньше флаг отсутствовал). Печать уважает json-режим.
    """
    if not value:
        return
    from skillery_cli import __version__

    emit_data(
        {"version": __version__},
        text_renderer=lambda p: console.print(p["version"]),
    )
    raise typer.Exit()


def build_app() -> typer.Typer:
    cfg = ClientConfig.load()
    is_logged_in = cfg.is_logged_in()

    description_lines = [f"{_branding.APP_NAME.capitalize()} CLI"]
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
        description_lines.append(
            "[dim]Не авторизован. Доступно без логина: login / register / join, "
            "автономная установка (install --path | --from-git), управление "
            "стором (enable/disable/remove/sync/migrate/store), локальные "
            "коллекции (collection *-local), onboard.[/]"
        )

    # cli-kits W6: каркас root-приложения строится через clikit.command_kit
    # (build_root_app), а НЕ голым typer.Typer. Бренд берётся из _branding.
    # Помощь и no_args_is_help сохранены прежними.
    #
    # ВАЖНО — почему callback и `version`-команда ниже переопределяются/снимаются:
    # build_root_app задаёт СВОЙ глобальный callback (json-дефолт + --text/--plain
    # через clikit.output) и регистрирует подкоманду `version`. У этого CLI
    # ИСТОРИЧЕСКИЙ контракт вывода ДРУГОЙ: дефолт — text, режим инициализируется
    # пре-проходом по argv ДО построения app (см. init_output_mode выше через
    # skillery_cli.output), а callback — no-op. Чтобы не сломать ни поведение
    # вывода (is_json()), ни набор команд (`skillery --help`), мы:
    #   1) переопределяем callback историческим (--profile/--json/--version, без
    #      --text и без clikit-реинициализации вывода);
    #   2) снимаем авто-зарегистрированную команду `version` (флага --version и
    #      его callback'а достаточно — набор команд остаётся прежним).
    app = build_root_app(
        brand=_branding.APP_NAME,
        help="\n".join(description_lines),
        no_args_is_help=True,
    )
    # Снять авто-`version`-подкоманду из build_root_app: исторически её нет,
    # версия печатается ТОЛЬКО глобальным флагом --version.
    app.registered_commands = [
        c for c in app.registered_commands if c.name != "version"
    ]

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
            help="Показать версию skillery CLI и выйти.",
            callback=_version_callback,
            is_eager=True,
        ),
        debug: bool = typer.Option(
            False, "--debug",
            help="Полный DEBUG-трейс в логи на время процесса (стандарт — только "
                 "ошибки). Эквивалент env SKILLERY_LOG_LEVEL=debug.",
        ),
        verbose: bool = typer.Option(
            False, "--verbose", "-v",
            help="Максимальный TRACE-трейс в логи на время процесса "
                 "(подробнее --debug). Эквивалент env SKILLERY_LOG_LEVEL=trace.",
        ),
    ) -> None:
        """Корневой callback (профиль + json считаны до построения app).

        Переопределяет callback из build_root_app, чтобы сохранить исторический
        контракт вывода (дефолт text, режим уже инициализирован пре-проходом).
        """
        _ = profile, json_output, version
        # Рантайм-переключатель уровня логов: --debug/--verbose кладут env на
        # ВРЕМЯ процесса (configure_logging читает env поверх cfg). Стандартный
        # уровень (error) в конфиге не меняется — это разовый форсаж трейса.
        if verbose:
            os.environ["SKILLERY_LOG_LEVEL"] = "trace"
        elif debug:
            os.environ["SKILLERY_LOG_LEVEL"] = "debug"
        # Логи CLI в ~/.skillery/logs/ (уровень из конфига/env, по умолчанию error).
        with suppress(Exception):
            from skillery_cli.core.logging_setup import configure_logging

            configure_logging(ClientConfig.load().log_level)
        # C3 (#1099) + #1174: автосинк логов на бэк — подключаем ОДИН handler
        # поверх дерева ``skillery`` (WARNING+ отовсюду, INFO+ из аудита
        # установки). Он лишь кладёт конверт ``kind="log"`` в ОБЩИЙ outbox;
        # сеть — дело воркера доставки, поэтому команда не платит за синк ни
        # временем, ни падением при офлайне. Тот же вызов идемпотентно
        # перекладывает наследство старой очереди C3 (миграция #1174).
        with suppress(Exception):
            from skillery_cli.core.log_sync import attach_log_sync

            attach_log_sync()
        # #1180: та же логика для ТРЕТЬЕЙ очереди — аналитики. Продюсер теперь
        # пишет в общий outbox (``kind="analytics_event"``), а неотправленное из
        # ``~/.skillery/events.queue.json`` переливаем при первом же старте:
        # это установки/включения, на которых стоит метрика адопции.
        with suppress(Exception):
            from skillery_cli.core.analytics_sync import migrate_legacy_queue

            migrate_legacy_queue()
        _heal_daemon_if_dead()

    # === Always-on ===
    app.command(name="login")(cmd_login)
    # --- P1 account ---
    # register/join — always-on онбординг: самостоятельная регистрация и
    # вступление в компанию по ссылке работают ДО login (join у залогиненного
    # сам переключается на /invite-links/accept).
    from skillery_cli.commands import account as _account_mod

    _account_mod.register(app)
    app.command(name="set-tokens", hidden=True)(cmd_set_tokens)
    app.command(name="status")(cmd_status)
    # doctor — ALWAYS-ON (рядом со status): self-check окружения (Python/uv/
    # agent/login/PATH/clikit), pass/warn/fail, --strict для CI. Сети не нужно.
    from skillery_cli.commands import doctor as _doctor_mod

    _doctor_mod.register(app)
    app.command(name="logout")(cmd_logout)
    app.command(name="whoami")(cmd_whoami)
    app.command(name="devices")(cmd_devices)
    from skillery_cli.commands import run as _run_mod
    from skillery_cli.commands.installed import cmd_installed
    from skillery_cli.commands.logs import cmd_logs

    app.command(name="installed")(cmd_installed)
    app.command(name="logs")(cmd_logs)
    # run — ОБЁРТКА вызова навыка (#1221): фиксирует факт запуска кодом, а не
    # обещанием модели отчитаться. Регистрируется в базовом наборе (без
    # permissions): запускают навыки и незалогиненные — событие копится в
    # общем outbox и уезжает первым же проходом воркера демона.
    _run_mod.register(app)
    app.command(name="config")(cmd_config)
    app.command(name="web")(cmd_web)
    # upgrade — обновить сам CLI (skillery-cli) с PyPI (uv tool / pipx / pip).
    app.command(name="upgrade")(cmd_upgrade)
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
    # pull — докачка навыков, помеченных установленными в вебе (/me/installs).
    # Hub-режим: внутри сам требует login.
    app.command(name="pull")(cmd_pull)
    # push — зеркало pull: отметить локальный набор установленным в хабе, чтобы
    # он подтянулся pull'ом на другом устройстве (кросс-девайс sync через хаб).
    app.command(name="push")(cmd_push)
    app.command(name="migrate")(cmd_migrate)
    store_app = typer.Typer(no_args_is_help=True, help="Центральный стор навыков")
    app.add_typer(store_app, name="store")
    store_app.command("list")(cmd_store_list)
    store_app.command("path")(cmd_store_path)
    store_app.command("gc")(cmd_store_gc)
    # #1405: резерв вытесненной установки и откат к ней.
    store_app.command("backups")(cmd_store_backups)
    store_app.command("restore")(cmd_store_restore)
    # --- P1 local-collections ---
    # Единый collection sub-app — ALWAYS-ON: все глаголы (list/show/install/
    # create/add/remove/delete/tags) регистрируются всегда; create/add/remove/
    # delete имеют локальный режим через флаг --local (оффлайн, без логина).
    # Серверный режим (без --local) гейтится ВНУТРИ модуля флагами
    # server_enabled (skill.read) / can_install (skill.install) / can_manage
    # (catalog.manage, M-2) — проверяются в рантайме команд.
    from skillery_cli.commands import collection as _coll_mod

    _coll_mod.register(
        app,
        server_enabled=cfg.has_permission("skill.read"),
        can_install=cfg.has_permission("skill.install"),
        # серверный CRUD коллекций (create/add/remove/tags) —
        # право catalog.manage (hub.admin bypass внутри has_permission).
        can_manage=cfg.has_permission("catalog.manage"),
    )

    # --- P1 onboard ---
    # Онбординг проекта — ALWAYS-ON: без логина работает по локальному
    # стору; hub-поиск и докачка отсутствующих включаются только при сессии.
    from skillery_cli.commands import onboard as _onboard_mod

    _onboard_mod.register(app)

    # --- discovery suggest ---
    # `skillery suggest "<query>"` — подбор навыков по свободному запросу.
    # ALWAYS-ON, read-only: локальный стор всегда, hub-поиск при сессии.
    from skillery_cli.commands import suggest as _suggest_mod

    _suggest_mod.register(app)

    # --- AI advisor ask ---
    # `skillery ask "<query>"` — реальный LLM-адвайзер (тот же, что в вебе):
    # SSE-стрим ответа + карточки навыков. Требует логина.
    from skillery_cli.commands import ask as _ask_mod

    _ask_mod.register(app)

    # --- scaffold ---
    # `skillery new <slug> --kind ...` — генерация скелета навыка. ALWAYS-ON:
    # локальная генерация на диск, сети/логина не требует.
    from skillery_cli.commands import scaffold as _scaffold_mod

    _scaffold_mod.register(app)

    # --- analytics local ---
    # `skillery analytics local` — read-only локальная картина (стор + проект
    # + очередь событий). ALWAYS-ON: только локальные файлы, сети/логина не надо.
    from skillery_cli.commands import analytics as _analytics_mod

    _analytics_mod.register(app)

    if not is_logged_in:
        return app

    # === Requires authenticated session ===
    app.command(name="passwd")(cmd_passwd)

    # === Permission-gated user commands ===
    if cfg.has_permission("skill.read"):
        app.command(name="list")(cmd_list)
        app.command(name="show")(cmd_show)
        # --- P1 local-collections --- (перенесено): collection.register
        # теперь вызывается в always-on зоне выше — серверные команды
        # гейтятся внутри модуля (server_enabled=skill.read,
        # can_install=skill.install), локальные живут без логина.
        # contributors (public-аналог skill.read).
        from skillery_cli.commands import contrib as _contrib_mod

        _contrib_mod.register(app)
        # comments list (public via skill.read).
        from skillery_cli.commands import comment as _comment_mod

        # post-команду регистрируем отдельно ниже (нужен comment.post).
        app.command(name="comments")(_comment_mod.cmd_comments_list)
    # install зарегистрирован в always-on блоке (см. выше): автономные
    # источники --path/--from-git не требуют login.
    # enable/disable/remove/sync/migrate/store(list/path/gc) — тоже в
    # always-on блоке (фикс 3): lifecycle локального стора живёт без login.
    # cli-kits W6: одиночные permission-гейты → command_kit.gated.
    gated(app, permission="skill.install", has_permission=cfg.has_permission,
          name="update")(cmd_update)
    gated(app, permission="skill.report_issue", has_permission=cfg.has_permission,
          name="report")(cmd_report)

    # === Skill review: ratings + comments post ===
    if cfg.has_permission("skill.rate"):
        from skillery_cli.commands import rate as _rate_mod

        _rate_mod.register(app)
    # comment / comment-edit / comment-delete — независимые per-permission гейты
    # (PATCH/DELETE /comments/{id} адресуют comment по числовому id).
    # cli-kits W6: → command_kit.gated (1:1 одиночные команды).
    from skillery_cli.commands import comment as _comment_mod

    # Ресурс-группа `comment` (canon #890): глаголы — ПОДКОМАНДЫ (`comment edit`),
    # а не приклеенные дефисом (`comment-edit`). Старые дефисные формы оставлены
    # СКРЫТЫМИ алиасами — скрипты не ломаются.
    comment_app = typer.Typer(
        no_args_is_help=True, help="Комментарии к навыкам: add/edit/delete/list."
    )
    gated(comment_app, permission="comment.post", has_permission=cfg.has_permission,
          name="add")(_comment_mod.cmd_comment_post)
    gated(comment_app, permission="comment.edit_own", has_permission=cfg.has_permission,
          name="edit")(_comment_mod.cmd_comment_edit)
    gated(comment_app, permission="comment.delete_own", has_permission=cfg.has_permission,
          name="delete")(_comment_mod.cmd_comment_delete)
    gated(comment_app, permission="comment.post", has_permission=cfg.has_permission,
          name="list")(_comment_mod.cmd_comments_list)
    app.add_typer(comment_app, name="comment")
    # Back-compat СКРЫТЫЕ алиасы (прежние плоские дефисные формы).
    # #1223: алиас теперь ПРЕДУПРЕЖДАЕТ о новом имени в stderr — иначе он
    # консервирует скрипты на старой форме вместо перевода на канон.
    from skillery_cli._grouping import deprecated_alias as _dep_alias

    gated(app, permission="comment.edit_own", has_permission=cfg.has_permission,
          name="comment-edit", hidden=True, deprecated=True)(
        _dep_alias(_comment_mod.cmd_comment_edit,
                   old="comment-edit", new="comment edit")
    )
    gated(app, permission="comment.delete_own", has_permission=cfg.has_permission,
          name="comment-delete", hidden=True, deprecated=True)(
        _dep_alias(_comment_mod.cmd_comment_delete,
                   old="comment-delete", new="comment delete")
    )

    # === Support tickets ===
    if cfg.has_permission("ticket.create") or cfg.has_permission("ticket.read"):
        from skillery_cli.commands import ticket as _ticket_mod

        # `status` (PATCH) — под ticket.update_status; reply — под ticket.create.
        _ticket_mod.register_ticket(
            app, can_update=cfg.has_permission("ticket.update_status")
        )

    # === Event tracking + daemon (always-on для залогиненного user'а) ===
    from skillery_cli.commands import daemon as _daemon_mod
    from skillery_cli.commands import event as _event_mod

    _event_mod.register(app, can_read_hub=cfg.is_hub_admin())
    _daemon_mod.register(app)

    # === Creator ===
    # cli-kits W6: одиночный гейт publish → command_kit.gated.
    gated(app, permission="skill.publish", has_permission=cfg.has_permission,
          name="publish")(cmd_publish)

    # --- auto-sync: webhook навыка ---
    # `skillery webhook register|status|revoke` — включение автообновлений из
    # git. Видимость — у того, кто может публиковать навык или им управлять
    # (RBAC ручек на бэке: владелец навыка / skill.manage / hub.admin;
    # финально права режет backend).
    from skillery_cli.commands import webhook as _webhook_mod

    _webhook_mod.register(
        app,
        can_manage=(
            cfg.has_permission("skill.publish")
            or cfg.has_permission("skill.manage")
        ),
    )

    # === #2267: группа `admin` РАСФОРМИРОВАНА по сущностям ===
    # Решение владельца по канону REST (#1452) — «префикс /admin убран из
    # путей полностью: админ и пользователь ходят в одни ручки, видимость
    # решают права и RLS». В путях API это сделано; здесь то же самое делаем
    # в командах: действие живёт в группе своей СУЩНОСТИ, а доступ к нему
    # решают права, а не имя группы.
    #
    #   admin sync-skill    → skill sync-versions   (право hub.admin)
    #   admin yank          → skill yank            (skill.manage | hub.admin)
    #   admin company-create→ company create        (hub.company_create)
    #   admin invite        → member invite         (user.invite | invite.manage)
    #
    # Права здесь ровно те же, что были у прежних admin-команд — переезд
    # ничего не ослабляет и не ужесточает. Прежние имена продолжают работать
    # скрытыми deprecated-алиасами (`_register_admin_compat` в конце сборки).
    can_admin_sync_versions = cfg.has_permission("hub.admin")
    can_company_create = cfg.has_permission("hub.company_create")
    # invite: ОБЪЕДИНЕНИЕ гейтов двух прежних копий (admin invite —
    # invite.manage, member invite — user.invite). Выбрать одно молча значило
    # бы отнять команду у половины прежних вызывающих, поэтому ANY-of.
    can_invite = (
        cfg.has_permission("user.invite")
        or cfg.has_permission("invite.manage")
    )
    can_yank = cfg.has_permission("skill.manage") or cfg.is_hub_admin()

    # --- P1 member ---
    # Участники + каталог ролей (C3): members/roles — любой залогиненный
    # (backend сам сужает выдачу: member без admin-прав видит только себя),
    # мутации — по правам (hub.admin bypass внутри has_permission).
    from skillery_cli.commands import member as _member_mod

    _member_mod.register(
        app,
        # #2267: ANY-of user.invite | invite.manage (см. блок выше).
        can_invite=can_invite,
        can_remove=cfg.has_permission("user.remove"),
        can_change_role=cfg.has_permission("role.manage"),
        # S3 D2.2: lock/unlock гейтится тем же предикатом, что backend
        # `_can_admin_users` (а не узким user.lock) — иначе owner/manager не
        # видят команду, хотя бэк/UI их допускают. suspend/activate/
        # revoke-sessions — тот же предикат (зеркало bulk-эндпоинтов).
        can_lock=cfg.can_admin_users(),
        can_reset_password=cfg.has_permission("company.manage"),
        # CRUD пользователей. create/edit/delete — company-admin или
        # hub.admin (зеркало backend `_can_admin_users` per-action); transfer/
        # export — строго hub.admin (backend hub-admin-only).
        can_create=cfg.has_permission("user.create")
        or cfg.has_permission("company.manage"),
        can_update=cfg.has_permission("user.update")
        or cfg.has_permission("company.manage"),
        can_delete=cfg.has_permission("user.delete")
        or cfg.has_permission("company.manage"),
        can_transfer=cfg.is_hub_admin(),
        can_export=cfg.is_hub_admin(),
    )

    # --- permissions + role set-permissions ---
    # Управление каталогом прав и набором прав роли — гейт hub.admin
    # (backend допускает set-permissions ещё и role.manage в своей компании,
    # но CLI-видимость держим на hub.admin; бэк финально режет 403).
    from skillery_cli.commands import permission as _permission_mod

    _permission_mod.register(app, can_manage=cfg.is_hub_admin())

    # --- P1 company ---
    # sub-app `company` (C2): show/switch — always-on для залогиненного
    # (бэк сам режет tenant-изоляцией); остальные подкоманды гейтятся
    # permissions-зеркалом backend-роутов. has_permission() уже включает
    # hub.admin bypass.
    from skillery_cli.commands import company as _company_mod

    _company_mod.register(
        app,
        can_list=cfg.is_hub_admin(),
        can_create=can_company_create,  # #2267: тот же гейт, что был у admin company-create

        can_edit=cfg.has_permission("company.manage"),
        can_invite_links=(
            cfg.has_permission("company.manage")
            or cfg.has_permission("role.manage")
        ),
        can_catalog_view=(
            cfg.has_permission("catalog.manage")
            or cfg.has_permission("catalog.view_all")
        ),
        can_catalog_manage=cfg.has_permission("catalog.manage"),
    )

    # === #1224: закрытие гэпов матрицы функционала ===
    # Матрица docs/ops/functional-canon.md числила эти ручки за CLI, но
    # обращений к ним в коде не было ни одного. Регистрируем ПОСЛЕ основных
    # модулей и ДО _finalize_groups: модули, которые дописывают глаголы в
    # существующие группы (skill/auth), должны видеть их уже созданными, а
    # _grouping потом дольёт в те же группы исторические плоские команды.
    from skillery_cli.commands import access as _access_mod
    from skillery_cli.commands import session as _session_mod
    from skillery_cli.commands import skill_extra as _skill_extra_mod
    from skillery_cli.commands import system as _system_mod
    from skillery_cli.commands import tag as _tag_mod

    # Теги: чтение — любому вошедшему, мутации — tag.create/tag.manage.
    _tag_mod.register(
        app,
        can_manage=(
            cfg.has_permission("tag.create") or cfg.has_permission("tag.manage")
        ),
    )
    # Гранты доступа: у backend гейтится даже GET, поэтому гейтим целиком.
    _access_mod.register(app, can_manage=cfg.has_permission("skill.manage"))
    # #1489: способности. Чтение и своя машина — всем (это ответ на вопрос
    # «что мне разрешено»); выдача/отзыв прав — под гейтом навыка-носителя.
    from skillery_cli.commands import capability as _capability_mod

    _capability_mod.register(app, can_manage=cfg.has_permission("skill.manage"))
    # Конфигурация хаба — только hub.admin.
    _system_mod.register(app, can_manage=cfg.is_hub_admin())
    # Сессии и профиль — всегда: это про СВОЙ аккаунт, прав не требует.
    _session_mod.register(app)
    # Доп-глаголы навыка: чтение всем, мутации — публикующим/управляющим.
    _skill_extra_mod.register(
        app,
        can_publish=(
            cfg.has_permission("skill.publish")
            or cfg.has_permission("skill.manage")
        ),
    )

    # #2267: два бывших admin-глагола — действия над НАВЫКОМ, поэтому их место
    # в группе `skill` (рядом с sync-status/version add), а не в группе по
    # имени роли. Регистрируем ПОСЛЕ skill_extra: группа `skill` к этому
    # моменту уже создана.
    skill_app = _skill_group(app)
    gated(skill_app, permission="yank", has_permission=lambda _p: can_yank,
          name="yank")(cmd_skill_yank)
    gated(skill_app, permission="sync-versions",
          has_permission=lambda _p: can_admin_sync_versions,
          name="sync-versions")(cmd_skill_sync_versions)

    # #2267: прежние имена группы `admin` — СКРЫТЫЕ deprecated-алиасы.
    _register_admin_compat(
        app,
        can_sync=can_admin_sync_versions,
        can_yank=can_yank,
        can_company_create=can_company_create,
        can_invite=can_invite,
    )

    _finalize_groups(app)
    return app


def _skill_group(app: typer.Typer) -> typer.Typer:
    """Sub-app ``skill`` (создаётся, если его ещё нет).

    Группа собирается несколькими модулями (``skill_extra``, ``_grouping``),
    поэтому берём уже существующий инстанс — иначе глаголы разъедутся по двум
    одноимённым группам.
    """
    for group in app.registered_groups:
        if group.name == "skill" and group.typer_instance is not None:
            return group.typer_instance
    sub = typer.Typer(
        no_args_is_help=True,
        help="Навыки: поиск, установка, включение, обновление, публикация.",
    )
    app.add_typer(sub, name="skill")
    return sub


def _register_admin_compat(
    app: typer.Typer,
    *,
    can_sync: bool,
    can_yank: bool,
    can_company_create: bool,
    can_invite: bool,
) -> None:
    """Back-compat группы ``admin`` (#2267): скрытые deprecated-алиасы.

    Группа расформирована (каждое действие переехало в группу своей
    сущности), но CLI стоит у пользователей и зовётся из скриптов и SKILL.md
    навыков — снести имена значило бы молча сломать чужую автоматизацию.
    Поэтому ровно тот же приём, что для ``accept-invite`` (#1223):

    * группа и все её команды ``hidden=True`` + ``deprecated=True`` — из
      справки уходят, вызываться продолжают;
    * алиас ведёт в ТУ ЖЕ функцию, что новая форма (не копию — иначе формы
      разойдутся при первой правке);
    * при вызове в stderr уходит предупреждение с новым именем.

    Гейты — те же булевы, что у канонических форм: алиас не даёт доступа,
    которого нет у новой формы.
    """
    from skillery_cli.commands.company import cmd_company_create
    from skillery_cli.commands.member import cmd_member_invite

    if not (can_sync or can_yank or can_company_create or can_invite):
        return
    admin_app = typer.Typer(
        no_args_is_help=True,
        help="УСТАРЕЛО: команды переехали в группы skill/company/member.",
        hidden=True,
        deprecated=True,
    )
    app.add_typer(admin_app, name="admin", hidden=True, deprecated=True)
    _admin_alias(
        admin_app, can_sync, "sync-skill", "skill sync-versions",
        cmd_skill_sync_versions,
    )
    _admin_alias(admin_app, can_yank, "yank", "skill yank", cmd_skill_yank)
    _admin_alias(
        admin_app, can_company_create, "company-create", "company create",
        cmd_company_create,
    )
    _admin_alias(
        admin_app, can_invite, "invite", "member invite", cmd_member_invite
    )


def _admin_alias(
    admin_app: typer.Typer,
    allowed: bool,
    old_verb: str,
    new_path: str,
    func: Any,
) -> None:
    """Один скрытый deprecated-алиас ``admin <old_verb>`` → ``<new_path>``."""
    from skillery_cli._grouping import deprecated_alias

    gated(
        admin_app,
        permission=old_verb,
        has_permission=lambda _p: allowed,
        name=old_verb,
        hidden=True,
        deprecated=True,
    )(deprecated_alias(func, old=f"admin {old_verb}", new=new_path))


def _finalize_groups(app: typer.Typer) -> None:
    """#1223: плоские команды → ресурсные группы ``skillery <ресурс> <глагол>``.

    Делается ОДНИМ проходом в самом конце сборки, а не в каждом ``register()``:
    к этому моменту известно, какие команды реально зарегистрированы (часть
    режут permission-гейты), и группа создаётся ровно под доступные глаголы.
    Плоские имена остаются рабочими скрытыми алиасами с предупреждением в
    stderr — см. ``skillery_cli._grouping``.
    """
    from skillery_cli._grouping import apply_resource_groups

    apply_resource_groups(app)


app = build_app()


def _write_crash_log(exc: BaseException) -> Path:
    """Полный traceback в ``~/.skillery/last-error.log`` (для разбора), best-effort."""
    import traceback

    from skillery_cli.config import _default_config_dir

    logpath = _default_config_dir() / "last-error.log"
    try:
        logpath.parent.mkdir(parents=True, exist_ok=True)
        logpath.write_text(
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass
    return logpath


def _self_heal_repairs() -> list[str]:
    """Диагностика + авто-починка известных проблем (сейчас — битый конфиг)."""
    try:
        from skillery_cli.commands.doctor import repair_config

        return repair_config()
    except Exception:  # noqa: BLE001
        return []


def _session_hint() -> str | None:
    """Подсказка про истёкшую сессию, если именно она — вероятная причина (#1387)."""
    try:
        from skillery_cli.commands.doctor import _probe_session

        res = _probe_session(ClientConfig.load())
    except Exception:  # noqa: BLE001 — подсказка не имеет права валить обработчик
        return None
    if res.level in ("fail", "warn"):
        return f"{res.name}: {res.detail}"
    return None


def _invoke_app(*, retry: bool) -> None:
    err = Console(stderr=True)
    try:
        app()
    except SystemExit:
        raise  # штатные exit-коды typer/click (в т.ч. наши emit_error → Exit(1))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:  # noqa: BLE001 — глобальный self-heal, не роняем raw-traceback
        err.print(f"\n[red]✗ Ошибка:[/] {type(exc).__name__}: {exc}")
        err.print(f"[cyan]↻ Запускаю self-check ({_branding.APP_NAME} doctor)…[/]")
        repairs = _self_heal_repairs()
        if repairs:
            for r in repairs:
                err.print(f"  [green]✓ нашёл и починил:[/] {r}")
            if retry:
                err.print("[cyan]↻ Повторяю команду…[/]")
                _invoke_app(retry=False)  # ровно один ретрай после починки
                return
        else:
            # #1387: «починок не нашлось» было ответом даже на истёкшую сессию.
            # Авто-чинить нечего (вход делает человек), но назвать причину и
            # точную команду мы обязаны.
            hint = _session_hint()
            if hint:
                err.print(f"  [yellow]{hint}[/]")
            else:
                err.print("  [dim]известных авто-починок не нашлось[/]")
        logpath = _write_crash_log(exc)
        err.print(
            f"[yellow]Не удалось авто-починить.[/] Детали в логе: [dim]{logpath}[/]\n"
            f"Диагностика: [bold]{_branding.APP_NAME} doctor[/] "
            f"(или [bold]{_branding.APP_NAME} doctor --fix[/])."
        )
        raise SystemExit(1) from None


def main() -> None:
    """Entry-point CLI с self-heal: при НЕОЖИДАННОМ исключении не роняем
    raw-traceback, а запускаем доктор, авто-чиним известное (битый конфиг) и
    повторяем команду; если не вышло — пишем ``last-error.log`` и подсказываем
    ``doctor``. Штатные ошибки (API/валидация) идут прежним чистым путём."""
    _invoke_app(retry=True)


if __name__ == "__main__":
    main()
