"""_connect_private_repo — подключение приватного репо к автосинку при publish.

Патчим repo_connect (без сети/glab): GitLab → авто-токен; GitHub-приват →
подсказка (repo_token не меняется); ручной токен уважается; чужой хост — no-op.
"""
from __future__ import annotations

import pytest

from skillery_cli.core import repo_connect


@pytest.fixture
def conn():
    from skillery_cli.__main__ import _connect_private_repo

    return _connect_private_repo


def test_gitlab_autocreates_token(monkeypatch, conn) -> None:
    monkeypatch.setattr(
        repo_connect, "create_gitlab_project_token", lambda slug: "glpat-new"
    )
    tok = conn("https://gitlab.com/acme/skills.git", None)
    assert tok == "glpat-new"


def test_gitlab_failure_keeps_none(monkeypatch, conn) -> None:
    monkeypatch.setattr(
        repo_connect, "create_gitlab_project_token", lambda slug: None
    )
    tok = conn("https://gitlab.com/acme/skills.git", None)
    assert tok is None


def test_manual_token_respected_no_glab_call(monkeypatch, conn) -> None:
    called = {"n": 0}

    def _boom(slug):
        called["n"] += 1
        return "should-not-be-used"

    monkeypatch.setattr(repo_connect, "create_gitlab_project_token", _boom)
    tok = conn("https://gitlab.com/acme/skills.git", "my-manual-pat")
    assert tok == "my-manual-pat"
    assert called["n"] == 0


def test_github_private_shows_hint_keeps_token(monkeypatch, conn) -> None:
    monkeypatch.setattr(
        repo_connect, "github_repo_is_public", lambda slug: False
    )
    tok = conn("https://github.com/acme/private.git", None)
    assert tok is None  # App-подсказка показана, токен не выдуман


def test_github_public_no_hint(monkeypatch, conn) -> None:
    monkeypatch.setattr(
        repo_connect, "github_repo_is_public", lambda slug: True
    )
    tok = conn("https://github.com/acme/public.git", None)
    assert tok is None


def test_unknown_host_noop(conn) -> None:
    tok = conn("https://bitbucket.org/acme/skills.git", "keep-me")
    assert tok == "keep-me"
