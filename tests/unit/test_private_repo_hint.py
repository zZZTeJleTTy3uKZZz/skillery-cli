"""RE1: понятная подсказка вместо сырой git-ошибки при install приватного репо."""
from __future__ import annotations

import pytest

from skillery_cli.__main__ import _private_repo_hint


@pytest.mark.parametrize(
    "msg",
    [
        "git clone failed: remote: Invalid username or token. Password "
        "authentication is not supported for Git operations. fatal: "
        "Authentication failed for 'https://github.com/x/y.git/'",
        "git clone failed: fatal: could not read Username for "
        "'https://github.com'",
        "git clone failed: remote: HTTP Basic: Access denied",
        "git clone failed: fatal: repository 'https://...' not found",
        "git clone failed: Permission denied (publickey).",
    ],
)
def test_returns_friendly_for_auth_failures(msg: str) -> None:
    hint = _private_repo_hint(msg)
    assert hint is not None
    assert "GitHub App" in hint and "приватн" in hint.lower()


@pytest.mark.parametrize(
    "msg",
    [
        "some unrelated runtime error",
        "git clone failed: disk full",  # git, но не про доступ
        "installer: manifest parse error",
    ],
)
def test_returns_none_for_non_auth(msg: str) -> None:
    assert _private_repo_hint(msg) is None
