"""repo_connect — парсинг git-URL, github-проба публичности, glab project-token.

Все внешние эффекты инъектируются (probe/runner/now) — без реальной сети/glab.
"""
from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest

from skillery_cli.core.repo_connect import (
    RepoSlug,
    create_gitlab_project_token,
    github_repo_is_public,
    infer_provider,
    parse_repo_slug,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/acme/skills.git", RepoSlug("github.com", "acme", "skills")),
        ("https://github.com/acme/skills", RepoSlug("github.com", "acme", "skills")),
        ("git@github.com:acme/skills.git", RepoSlug("github.com", "acme", "skills")),
        ("https://gitlab.com/grp/sub/proj.git", RepoSlug("gitlab.com", "grp", "proj")),
        ("https://user:pass@gitlab.com/acme/skills.git", RepoSlug("gitlab.com", "acme", "skills")),
        ("ssh://git@gitlab.example.com:22/acme/skills.git", RepoSlug("gitlab.example.com", "acme", "skills")),
    ],
)
def test_parse_repo_slug(url: str, expected: RepoSlug) -> None:
    assert parse_repo_slug(url) == expected


@pytest.mark.parametrize("bad", ["", "   ", "not-a-url", "https://github.com/onlyowner"])
def test_parse_repo_slug_invalid(bad: str) -> None:
    assert parse_repo_slug(bad) is None


@pytest.mark.parametrize(
    "url,provider",
    [
        ("https://github.com/a/b", "github"),
        ("git@github.com:a/b.git", "github"),
        ("https://gitlab.com/a/b", "gitlab"),
        ("https://gitlab.self.hosted/a/b", "gitlab"),
        ("https://bitbucket.org/a/b", None),
    ],
)
def test_infer_provider(url: str, provider) -> None:
    assert infer_provider(url) == provider


def test_github_public_true_on_200() -> None:
    slug = RepoSlug("github.com", "acme", "skills")
    assert github_repo_is_public(slug, probe=lambda url: 200) is True


def test_github_public_false_on_404() -> None:
    slug = RepoSlug("github.com", "acme", "skills")
    assert github_repo_is_public(slug, probe=lambda url: 404) is False


def test_github_public_none_on_network_error() -> None:
    slug = RepoSlug("github.com", "acme", "skills")
    assert github_repo_is_public(slug, probe=lambda url: None) is None


def test_github_public_none_on_enterprise_host() -> None:
    slug = RepoSlug("github.enterprise.local", "acme", "skills")
    # Не пробим кастомный хост — None (не знаем).
    assert github_repo_is_public(slug, probe=lambda url: 200) is None


def _fake_runner(returncode: int, stdout: str):
    def _run(args, **kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")

    return _run


def test_gitlab_token_created() -> None:
    slug = RepoSlug("gitlab.com", "acme", "skills")
    runner = _fake_runner(0, json.dumps({"token": "glpat-scoped-xyz", "id": 1}))
    tok = create_gitlab_project_token(
        slug, runner=runner, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert tok == "glpat-scoped-xyz"


def test_gitlab_token_args_scope_and_expiry() -> None:
    slug = RepoSlug("gitlab.com", "acme", "skills")
    captured: dict = {}

    def _run(args, **kwargs):
        captured["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout='{"token":"t"}', stderr="")

    create_gitlab_project_token(
        slug, runner=_run, now=datetime(2026, 1, 1, tzinfo=UTC)
    )
    args = captured["args"]
    joined = " ".join(args)
    assert "scopes[]=read_api" in joined
    assert "projects/acme%2Fskills/access_tokens" in joined
    assert "--hostname" in args and "gitlab.com" in args
    # expires_at = now + 364 дней = 2026-12-31
    assert "expires_at=2026-12-31" in joined


def test_gitlab_token_none_on_glab_failure() -> None:
    slug = RepoSlug("gitlab.com", "acme", "skills")
    runner = _fake_runner(1, "")  # glab вернул ошибку
    assert create_gitlab_project_token(slug, runner=runner) is None


def test_gitlab_token_none_when_glab_missing() -> None:
    slug = RepoSlug("gitlab.com", "acme", "skills")

    def _run(args, **kwargs):
        raise FileNotFoundError("glab not found")

    assert create_gitlab_project_token(slug, runner=_run) is None


def test_gitlab_token_none_on_bad_json() -> None:
    slug = RepoSlug("gitlab.com", "acme", "skills")
    runner = _fake_runner(0, "not-json")
    assert create_gitlab_project_token(slug, runner=runner) is None
