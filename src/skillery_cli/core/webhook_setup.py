"""Создание git-webhook'а навыка У ПРОВАЙДЕРА локальными ``gh``/``glab``.

Зачем этот модуль вообще нужен. Хаб умеет регистрировать webhook сам
(``POST /skills/{slug}/webhook``), но только если у НЕГО есть токен с правами
на hooks этого репо. Обычно такого токена нет — хаб отвечает ``mode="manual"``,
отдаёт callback-url + секрет, и создание hook'а остаётся ручной работой в
настройках репозитория. Тогда «автообновления» не работают, пока человек не
сходит в UI.

Здесь эта дыра закрывается тем же приёмом, что и в :mod:`repo_connect`
(project-токен GitLab через локальный ``glab``): у владельца репо локально уже
авторизованы провайдерские CLI — их правами и создаём hook.

Контракт hook'а — ЗЕРКАЛО бэкенда (``infrastructure/git/webhook_registrar.py``),
иначе приёмник ``POST /webhooks/git/{provider}`` не сойдётся по подписи:

* **GitHub** — ``events=["push"]``, ``config={url, content_type:"json", secret}``;
  приёмник проверяет ``X-Hub-Signature-256`` (HMAC-SHA256 от RAW тела).
* **GitLab** — ``{url, token, push_events, tag_push_events}``; приёмник
  проверяет ``X-Gitlab-Token``.

Идемпотентность: ПЕРЕД созданием ищем свой hook среди существующих и
ОБНОВЛЯЕМ его, а не добавляем второй.

Что значит «свой» — здесь принципиально. Приёмник у хаба ОДИН на провайдера
(``<base>/webhooks/git/github``), а секрет хаб выдаёт СВОЙ НА КАЖДЫЙ навык.
Репозиторий-монорепо с десятком навыков — норма (хаб ищет навыки по
``repo_url_key``, т.е. по одному репо их может быть много). Поэтому «один hook
на репо» ЛОМАЕТ монорепо: hook хранит ровно один секрет, и после регистрации
второго навыка приёмник перестаёт узнавать подпись первого — доставки молча
отбиваются 401, а автосинк остальных навыков тихо умирает.

Чтобы hook'и разных навыков одного репо не затирали друг друга, в URL hook'а
добавляем маркер навыка: ``<base>/webhooks/git/github?skillery_skill=<id>``.
Приёмник query-параметры не читает (роут — только ``{provider}`` в пути), а
подпись считается от ТЕЛА, поэтому маркер полностью прозрачен для бэкенда и
при этом делает hook адресуемым: свой = тот, у кого тот же маркер. Ищем не
только по точному URL, но и по маркеру с любым хостом — так узнаём свой hook,
оставшийся от прежнего публичного адреса хаба, и обновляем его вместо дубля.

Секрет НИКОГДА не уходит в argv (в списке процессов его видно всей машине):
тело запроса отдаём провайдерскому CLI через stdin (``--input -``).

Все внешние эффекты (подпроцесс) инъектируются — модуль юнит-тестируем без
реальных ``gh``/``glab`` и без сети.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Literal

from skillery_cli.core.repo_connect import CommandRunner, RepoSlug

# Провайдер → локальный CLI, которым ходим в его API.
_TOOLS: dict[str, str] = {"github": "gh", "gitlab": "glab"}

# События, на которые подписываем hook. Зеркало бэкенда: GitHub — только push;
# GitLab — push + tag push (бэкенд просит оба).
GITHUB_HOOK_EVENTS = ("push",)

# Таймаут одного вызова провайдерского CLI (сеть внутри него).
_CALL_TIMEOUT_S = 30

# Маркер навыка в query приёмника: делает hook адресуемым НА НАВЫК, хотя
# приёмник у хаба один на провайдера. Бэкенд query не читает и подпись считает
# от тела — маркер для него невидим (см. модульный docstring).
SKILL_QUERY_KEY = "skillery_skill"


@dataclass(frozen=True)
class HookOutcome:
    """Итог попытки создать/обновить/удалить hook у провайдера.

    ``action``:
    - ``created`` — hook у провайдера создан;
    - ``updated`` — уже был, перезаписан (тот же id, новый секрет) — дублей нет;
    - ``deleted`` — снят;
    - ``skipped`` — сделать не удалось; ``reason`` говорит ПОЧЕМУ, чтобы
      вызывающий показал человеку ручные шаги, а не молча «получилось».
    """

    action: Literal["created", "updated", "deleted", "skipped"]
    hook_id: str | None = None
    reason: str | None = None
    detail: str | None = None
    # Итоговый адрес hook'а (приёмник + маркер навыка). Секрета тут нет —
    # строку можно печатать и класть в машинный вывод.
    url: str | None = None

    @property
    def ok(self) -> bool:
        return self.action != "skipped"


@dataclass(frozen=True)
class HookState:
    """Наблюдаемое состояние hook'а у провайдера (для ``webhook status --probe``).

    ``last_delivery_at`` даёт GitHub (API доставок); GitLab в CE такой истории
    не отдаёт — там останется ``None``, и это честно видно в выводе.
    """

    found: bool = False
    hook_id: str | None = None
    url: str | None = None
    active: bool | None = None
    last_delivery_at: str | None = None
    last_response_code: int | None = None
    reason: str | None = None
    events: tuple[str, ...] = field(default_factory=tuple)
    #: Нашли hook, ведущий на приёмник хаба, но БЕЗ маркера навыка — значит его
    #: завёл сам хаб (auto-режим), а не мы. Отдельное поле, потому что «нашего
    #: hook'а нет» и «автообновлений нет» — РАЗНЫЕ вещи: во втором случае синк
    #: как раз работает, и говорить «не найден» значит пугать зря.
    hub_hook_id: str | None = None


def secret_fingerprint(secret: str | None) -> str | None:
    """Короткий отпечаток секрета для логов/вывода — САМ секрет не раскрывает.

    Нужен, чтобы человек мог сверить «тот ли секрет стоит в репо», не печатая
    секрет в stdout и не давая его агентам.
    """
    if not secret:
        return None
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


def provider_tool(provider: str) -> str | None:
    """``github`` → ``gh``, ``gitlab`` → ``glab``; иначе ``None``."""
    return _TOOLS.get(provider)


# --- низкий уровень: вызов провайдерского CLI --------------------------------


def _classify_failure(stderr: str | None) -> str:
    """Отличить «не залогинен/нет прав» и «нет такого репо» от прочих сбоев.

    Разбираем ТОЛЬКО по признакам, наружу отдаём свой код: вывод провайдерского
    CLI мы не пересказываем и не логируем — в нём может оказаться эхо запроса, а
    в запросе секрет.
    """
    text = (stderr or "").lower()
    if not text:
        return "tool_failed"
    if "404" in text or "not found" in text:
        return "repo_not_found"
    if (
        "401" in text
        or "403" in text
        or "auth login" in text
        or "unauthorized" in text
        or "forbidden" in text
        or "authentication" in text
    ):
        return "tool_unauthorized"
    return "tool_failed"


def _api(
    provider: str,
    repo: RepoSlug,
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    runner: CommandRunner,
) -> tuple[bool, Any, str]:
    """Один вызов ``gh api`` / ``glab api`` → ``(ok, данные, код_ошибки)``.

    Тело (если есть) уходит через stdin (``--input -``): в argv секрету не место.
    Коды ошибок: ``tool_missing`` (CLI не установлен), ``tool_unauthorized``
    (не залогинен/нет прав), ``repo_not_found`` (репо не виден этому логину),
    ``tool_failed`` (прочее — сеть/API), ``bad_response`` (не-JSON).
    """
    tool = _TOOLS[provider]
    args = [tool, "api", "--hostname", repo.host, "--method", method, path]
    stdin_data: str | None = None
    if body is not None:
        args += ["--input", "-"]
        stdin_data = json.dumps(body)
    try:
        proc = runner(
            args,
            capture_output=True,
            text=True,
            timeout=_CALL_TIMEOUT_S,
            check=False,
            input=stdin_data,
        )
    except FileNotFoundError:
        return False, None, "tool_missing"
    except (OSError, subprocess.SubprocessError):
        return False, None, "tool_failed"
    if proc.returncode != 0:
        return False, None, _classify_failure(proc.stderr)
    raw = (proc.stdout or "").strip() or "null"
    try:
        return True, json.loads(raw), ""
    except (json.JSONDecodeError, TypeError):
        return False, None, "bad_response"


def _hooks_path(provider: str, repo: RepoSlug) -> str:
    if provider == "github":
        return f"repos/{repo.owner}/{repo.name}/hooks"
    # GitLab: id проекта — url-энкоднутый путь (вложенные группы тоже через %2F).
    project_id = urllib.parse.quote(repo.path, safe="")
    return f"projects/{project_id}/hooks"


def _hook_url(provider: str, hook: dict[str, Any]) -> str:
    """URL приёмника из объекта hook'а (у GitHub он внутри ``config``)."""
    if provider == "github":
        cfg = hook.get("config") or {}
        return str(cfg.get("url") or "") if isinstance(cfg, dict) else ""
    return str(hook.get("url") or "")


def hook_callback_url(receiver_url: str, skill_id: str | int) -> str:
    """Адрес НАШЕГО hook'а = приёмник хаба + маркер навыка в query.

    Маркер нужен, чтобы hook'и разных навыков ОДНОГО репо (монорепо — обычное
    дело) были разными объектами у провайдера: у каждого свой секрет, иначе
    регистрация следующего навыка молча ломает подпись предыдущего.
    """
    if not receiver_url:
        return ""
    sep = "&" if "?" in receiver_url else "?"
    marker = urllib.parse.quote(str(skill_id), safe="")
    return f"{receiver_url}{sep}{SKILL_QUERY_KEY}={marker}"


def _skill_marker(url: str) -> str | None:
    """Значение ``skillery_skill`` из URL hook'а (или ``None``, если маркера нет)."""
    try:
        query = urllib.parse.urlsplit(url).query
    except ValueError:
        return None
    values = urllib.parse.parse_qs(query).get(SKILL_QUERY_KEY) or []
    return values[0] if values else None


def _is_receiver_url(provider: str, url: str) -> bool:
    """Похож ли URL на приёмник хаба (``.../webhooks/git/<provider>``)."""
    try:
        path = urllib.parse.urlsplit(url).path
    except ValueError:
        return False
    return path.rstrip("/").endswith(f"/webhooks/git/{provider}")


def find_hook(
    provider: str, hooks: Any, callback_url: str, skill_id: str | int
) -> dict[str, Any] | None:
    """Найти hook ИМЕННО ЭТОГО навыка — основа идемпотентности.

    Сначала точное совпадение URL; если нет — по маркеру навыка при любом хосте:
    так узнаём свой hook, оставшийся от прежнего публичного адреса хаба, и
    обновим его вместо создания второго.

    Чужие hook'и (другого навыка того же репо, hook, созданный самим хабом в
    auto-режиме, посторонний CI) НЕ трогаем принципиально: перезапись их секрета
    молча отключила бы автосинк чужого навыка, и никто бы этого не заметил.
    """
    if not isinstance(hooks, list):
        return None
    marker = str(skill_id)
    fallback: dict[str, Any] | None = None
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        url = _hook_url(provider, hook)
        if callback_url and url == callback_url:
            return hook
        if (
            fallback is None
            and _skill_marker(url) == marker
            and _is_receiver_url(provider, url)
        ):
            fallback = hook
    return fallback


# --- тела запросов (зеркало бэкенда) -----------------------------------------


def _create_body(provider: str, callback_url: str, secret: str) -> dict[str, Any]:
    if provider == "github":
        return {
            "name": "web",
            "active": True,
            "events": list(GITHUB_HOOK_EVENTS),
            "config": {
                "url": callback_url,
                "content_type": "json",
                "secret": secret,
            },
        }
    return {
        "url": callback_url,
        "token": secret,
        "push_events": True,
        "tag_push_events": True,
    }


def _update_body(provider: str, callback_url: str, secret: str) -> dict[str, Any]:
    """Тело обновления. У GitHub PATCH не принимает ``name`` — убираем."""
    body = _create_body(provider, callback_url, secret)
    body.pop("name", None)
    return body


def _update_method(provider: str) -> str:
    return "PATCH" if provider == "github" else "PUT"


def tool_hint(provider: str, reason: str) -> str:
    """Человеческое объяснение, почему авто-настройка не вышла."""
    tool = _TOOLS.get(provider, "?")
    if reason == "tool_missing":
        return (
            f"локального {tool} нет в PATH — установите его или настройте "
            "webhook в репозитории вручную (шаги ниже)"
        )
    if reason == "tool_unauthorized":
        return (
            f"{tool} не авторизован или у этого логина нет прав "
            f"admin/maintainer на репо — проверьте `{tool} auth status` "
            "либо настройте webhook вручную (шаги ниже)"
        )
    if reason == "repo_not_found":
        return (
            f"{tool} не видит такого репозитория (репо не существует, "
            "переименован или недоступен этому логину) — сверьте repo_url "
            "навыка либо настройте webhook вручную (шаги ниже)"
        )
    if reason == "tool_failed":
        return (
            f"{tool} не смог обратиться к API (нет сети, прокси или сбой "
            f"провайдера) — проверьте `{tool} auth status` либо настройте "
            "webhook вручную (шаги ниже)"
        )
    if reason == "bad_response":
        return f"{tool} вернул неожиданный ответ — настройте webhook вручную"
    if reason == "provider_unsupported":
        return (
            "хост репозитория не github/gitlab — авто-настройка невозможна, "
            "заведите webhook вручную"
        )
    if reason == "callback_url_missing":
        return (
            "хаб не сообщил публичный URL приёмника (не задан public base URL "
            "на сервере) — без него hook создавать некуда; это чинит админ хаба"
        )
    return reason


# --- публичные операции -------------------------------------------------------


def ensure_provider_hook(
    *,
    provider: str,
    repo: RepoSlug,
    callback_url: str,
    skill_id: str | int,
    secret: str,
    runner: CommandRunner = subprocess.run,
) -> HookOutcome:
    """Создать ИЛИ обновить hook ЭТОГО навыка у провайдера (идемпотентно).

    ``callback_url`` — приёмник хаба как есть; адрес самого hook'а получаем из
    него добавлением маркера навыка (см. :func:`hook_callback_url`), поэтому
    соседние навыки того же репо живут отдельными hook'ами и не затирают друг
    другу секрет.

    Повторный запуск не плодит дубли: существующий hook этого навыка
    перезаписывается новым секретом (id сохраняется). Любой сбой — не
    исключение, а ``action="skipped"`` c причиной: вызывающий покажет ручные
    шаги.
    """
    if provider not in _TOOLS:
        return HookOutcome(
            "skipped",
            reason="provider_unsupported",
            detail=tool_hint(provider, "provider_unsupported"),
        )
    if not callback_url:
        return HookOutcome(
            "skipped",
            reason="callback_url_missing",
            detail=tool_hint(provider, "callback_url_missing"),
        )

    target_url = hook_callback_url(callback_url, skill_id)
    path = _hooks_path(provider, repo)
    ok, hooks, err = _api(provider, repo, "GET", path, runner=runner)
    if not ok:
        return HookOutcome("skipped", reason=err, detail=tool_hint(provider, err))

    existing = find_hook(provider, hooks, target_url, skill_id)
    if existing is not None:
        hook_id = str(existing.get("id"))
        ok, _data, err = _api(
            provider,
            repo,
            _update_method(provider),
            f"{path}/{hook_id}",
            body=_update_body(provider, target_url, secret),
            runner=runner,
        )
        if not ok:
            return HookOutcome(
                "skipped",
                hook_id=hook_id,
                reason=err,
                detail=tool_hint(provider, err),
            )
        return HookOutcome("updated", hook_id=hook_id, url=target_url)

    ok, data, err = _api(
        provider,
        repo,
        "POST",
        path,
        body=_create_body(provider, target_url, secret),
        runner=runner,
    )
    if not ok:
        return HookOutcome("skipped", reason=err, detail=tool_hint(provider, err))
    hook_id = str(data.get("id")) if isinstance(data, dict) and data.get("id") else None
    return HookOutcome("created", hook_id=hook_id, url=target_url)


def _find_hub_hook_id(provider: str, hooks: Any) -> str | None:
    """Id hook'а, заведённого САМИМ хабом: адрес приёмника без маркера навыка."""
    if not isinstance(hooks, list):
        return None
    for hook in hooks:
        if not isinstance(hook, dict):
            continue
        url = _hook_url(provider, hook)
        if _is_receiver_url(provider, url) and not _skill_marker(url):
            return str(hook.get("id"))
    return None


def probe_provider_hook(
    *,
    provider: str,
    repo: RepoSlug,
    callback_url: str,
    skill_id: str | int,
    runner: CommandRunner = subprocess.run,
) -> HookState:
    """Посмотреть глазами провайдера: есть ли hook ЭТОГО навыка и была ли доставка.

    Хаб в ``GET /skills/{slug}/webhook`` времени последней доставки НЕ хранит —
    единственный источник этой правды сам провайдер. GitHub отдаёт историю
    доставок (берём последнюю), GitLab CE — нет (останется ``None``).
    """
    if provider not in _TOOLS:
        return HookState(reason="provider_unsupported")
    path = _hooks_path(provider, repo)
    ok, hooks, err = _api(provider, repo, "GET", path, runner=runner)
    if not ok:
        return HookState(reason=err)
    hook = find_hook(
        provider, hooks, hook_callback_url(callback_url, skill_id), skill_id
    )
    if hook is None:
        # Своего hook'а нет — но, возможно, есть hook САМОГО ХАБА (auto-режим): он
        # ведёт на тот же приёмник, только без маркера навыка. Живой случай
        # 2026-07-21: hook хаба на GitLab существовал и работал, а проба
        # отвечала «не найден», то есть сообщала об исправной настройке как о
        # сломанной. Отличаем явно.
        return HookState(found=False, hub_hook_id=_find_hub_hook_id(provider, hooks))

    hook_id = str(hook.get("id"))
    events = tuple(
        str(e) for e in (hook.get("events") or []) if isinstance(e, (str, int))
    )
    last_code: int | None = None
    last_response = hook.get("last_response")
    if isinstance(last_response, dict):
        raw_code = last_response.get("code")
        last_code = int(raw_code) if isinstance(raw_code, int) else None

    delivered_at: str | None = None
    if provider == "github":
        ok, deliveries, _err = _api(
            provider,
            repo,
            "GET",
            f"{path}/{hook_id}/deliveries?per_page=1",
            runner=runner,
        )
        if ok and isinstance(deliveries, list) and deliveries:
            first = deliveries[0]
            if isinstance(first, dict):
                raw = first.get("delivered_at")
                delivered_at = str(raw) if raw else None

    return HookState(
        found=True,
        hook_id=hook_id,
        url=_hook_url(provider, hook) or None,
        active=bool(hook.get("active")) if "active" in hook else None,
        last_delivery_at=delivered_at,
        last_response_code=last_code,
        events=events,
    )


def delete_provider_hook(
    *,
    provider: str,
    repo: RepoSlug,
    callback_url: str,
    skill_id: str | int,
    runner: CommandRunner = subprocess.run,
) -> HookOutcome:
    """Снять hook ЭТОГО навыка у провайдера (best-effort, идемпотентно).

    Нужен при отзыве: hook, созданный ЛОКАЛЬНЫМ ``gh``/``glab``, хаб удалить не
    может — он не знает его external id (в manual-режиме id у хаба нет).

    Снимаем строго свой (по маркеру навыка): в монорепо рядом живут hook'и
    соседних навыков, и удалить чужой — значит выключить чужой автосинк.
    """
    if provider not in _TOOLS:
        return HookOutcome(
            "skipped",
            reason="provider_unsupported",
            detail=tool_hint(provider, "provider_unsupported"),
        )
    path = _hooks_path(provider, repo)
    ok, hooks, err = _api(provider, repo, "GET", path, runner=runner)
    if not ok:
        return HookOutcome("skipped", reason=err, detail=tool_hint(provider, err))
    hook = find_hook(
        provider, hooks, hook_callback_url(callback_url, skill_id), skill_id
    )
    if hook is None:
        # Нечего снимать — это успех отзыва, а не сбой.
        return HookOutcome("deleted", reason="not_found")
    hook_id = str(hook.get("id"))
    ok, _data, err = _api(
        provider, repo, "DELETE", f"{path}/{hook_id}", runner=runner
    )
    if not ok:
        return HookOutcome(
            "skipped", hook_id=hook_id, reason=err, detail=tool_hint(provider, err)
        )
    return HookOutcome("deleted", hook_id=hook_id)


def manual_instructions(
    provider: str, callback_url: str, skill_id: str | int
) -> list[str]:
    """Пошаговая ручная настройка — то, что человек делает вместо нас.

    URL даём С МАРКЕРОМ навыка (как сделали бы сами): в монорепо это то, что
    отличает hook одного навыка от hook'а соседнего, а нам потом позволяет
    узнать этот hook и обновить его, а не завести дубль.

    Секрет СЮДА не подставляем: строки уходят в т.ч. в JSON-вывод для агентов.
    """
    base = callback_url or f"<публичный URL хаба>/webhooks/git/{provider}"
    url = hook_callback_url(base, skill_id)
    secret_step = (
        "вставьте секрет webhook'а (печатается ОДИН раз в text-режиме: "
        "повторите команду без --json)"
    )
    if provider == "github":
        return [
            "Откройте репозиторий → Settings → Webhooks → Add webhook",
            f"Payload URL (вместе с ?{SKILL_QUERY_KEY}=… — он адресует навык): {url}",
            "Content type: application/json",
            f"Secret: {secret_step}",
            "Which events: Just the push event",
            "Active: включено → Add webhook",
        ]
    return [
        "Откройте проект → Settings → Webhooks → Add new webhook",
        f"URL (вместе с ?{SKILL_QUERY_KEY}=… — он адресует навык): {url}",
        f"Secret token: {secret_step}",
        "Триггеры: Push events + Tag push events",
        "→ Add webhook",
    ]
