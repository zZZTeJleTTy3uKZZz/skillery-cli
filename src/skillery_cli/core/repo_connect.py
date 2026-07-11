"""Подключение приватных репо навыков к автосинку хаба (per-project scoped).

Две стратегии авторизации приватного репо БЕЗ account-wide PAT:

- **GitHub** — App ``skillery-sync``: пользователь ставит App на свой репо, хаб
  минтит per-installation read-only токен. CLI при публикации приватного
  github-репо без токена подсказывает/открывает install-URL App'а.
- **GitLab** — project access token (scope ``read_api``): CLI через локальный
  ``glab`` создаёт токен ПРОЕКТА (не аккаунта) и отдаёт его хабу как
  ``repo_token``. Хаб авторизует REST-вызовы синка этим scoped-токеном.

Все внешние эффекты (сеть/подпроцесс/часы) инъектируются — модуль юнит-тестируем
без реальных github/glab.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable, Optional
from urllib import request as _urlrequest
from urllib.error import HTTPError, URLError

# Имя создаваемого GitLab project-токена (видно в настройках проекта).
_GITLAB_TOKEN_NAME = "skillery-sync"
# Срок жизни project-токена: GitLab требует expires_at ≤ 1 года. Берём 364 дня.
_GITLAB_TOKEN_TTL_DAYS = 364
# access_level=20 (Reporter) — минимум для read_api на теги/содержимое.
_GITLAB_REPORTER_LEVEL = 20


@dataclass(frozen=True)
class RepoSlug:
    """Разобранный git-URL: хост + owner/name (без ``.git``, без кредов)."""

    host: str
    owner: str
    name: str

    @property
    def path(self) -> str:
        return f"{self.owner}/{self.name}"


def parse_repo_slug(repo_url: str) -> Optional[RepoSlug]:
    """``https://github.com/acme/skills.git`` → ``RepoSlug(github.com, acme, skills)``.

    Поддерживает http(s) и scp-подобный ssh (``git@host:owner/name.git``).
    Возвращает ``None``, если из URL не выделить host+owner+name.
    """
    if not repo_url or not repo_url.strip():
        return None
    url = repo_url.strip()

    # scp-форма: git@github.com:owner/name(.git)
    if url.startswith("git@") or ("@" in url and "://" not in url):
        try:
            _, rest = url.split("@", 1)
            host, path = rest.split(":", 1)
        except ValueError:
            return None
    else:
        # http(s):// или ssh://... — срезаем схему и креды.
        without_scheme = url.split("://", 1)[-1]
        if "@" in without_scheme:
            without_scheme = without_scheme.split("@", 1)[-1]
        if "/" not in without_scheme:
            return None
        host, path = without_scheme.split("/", 1)

    host = host.strip().rstrip("/").split(":")[0].lower()
    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if not host or len(parts) < 2:
        return None
    # owner = первый сегмент, name = ПОСЛЕДНИЙ (поддержка вложенных групп GitLab).
    return RepoSlug(host=host, owner=parts[0], name=parts[-1])


def infer_provider(repo_url: str) -> Optional[str]:
    """``"github"`` | ``"gitlab"`` | ``None`` по хосту репо."""
    slug = parse_repo_slug(repo_url)
    if slug is None:
        return None
    host = slug.host
    if "github" in host:
        return "github"
    if "gitlab" in host:
        return "gitlab"
    return None


# Проба публичности: возвращает код HTTP-статуса анонимного GET (или None при
# сетевом сбое). Инъектируется в тестах.
StatusProbe = Callable[[str], Optional[int]]


def _default_status_probe(url: str) -> Optional[int]:
    req = _urlrequest.Request(url, headers={"User-Agent": "skillery-cli"})
    try:
        with _urlrequest.urlopen(req, timeout=8) as resp:  # noqa: S310
            return int(resp.status)
    except HTTPError as e:
        return int(e.code)
    except (URLError, OSError, ValueError):
        return None


def github_repo_is_public(
    slug: RepoSlug, *, probe: StatusProbe = _default_status_probe
) -> Optional[bool]:
    """Публичен ли github-репо (анонимный GET REST API).

    ``True`` — публичный (200); ``False`` — приватный/нет доступа (404/403);
    ``None`` — определить не удалось (сетевой сбой) ⇒ вызывающий пусть покажет
    install-подсказку на всякий случай.
    """
    if "github.com" not in slug.host:
        # Enterprise/иной хост не пробим — не знаем.
        return None
    code = probe(f"https://api.github.com/repos/{slug.owner}/{slug.name}")
    if code is None:
        return None
    if code == 200:
        return True
    if code in (401, 403, 404):
        return False
    return None


# Раннер подпроцесса (subprocess.run-совместимый) — инъекция для тестов.
CommandRunner = Callable[..., subprocess.CompletedProcess]


def create_gitlab_project_token(
    slug: RepoSlug,
    *,
    runner: CommandRunner = subprocess.run,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Создать GitLab project access token (scope ``read_api``) через ``glab``.

    Требует локально установленный и авторизованный ``glab`` с правами
    maintainer на проекте. Возвращает токен или ``None`` при любом сбое
    (нет glab / нет прав / неожиданный ответ) — вызывающий откатится на
    ручной ``--repo-token``.
    """
    now = now or datetime.now(UTC)
    expires_at = (now + timedelta(days=_GITLAB_TOKEN_TTL_DAYS)).strftime(
        "%Y-%m-%d"
    )
    # GitLab API: путь проекта URL-энкодится (owner%2Fname; вложенные группы
    # тоже через %2F). Эндпоинт после /api/v4/.
    project_path = slug.path.replace("/", "%2F")
    args = [
        "glab",
        "api",
        "--hostname",
        slug.host,
        "--method",
        "POST",
        f"projects/{project_path}/access_tokens",
        "-f",
        f"name={_GITLAB_TOKEN_NAME}",
        "-f",
        "scopes[]=read_api",
        "-f",
        f"access_level={_GITLAB_REPORTER_LEVEL}",
        "-f",
        f"expires_at={expires_at}",
    ]
    try:
        proc = runner(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    token = data.get("token") if isinstance(data, dict) else None
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None
