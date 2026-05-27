"""Тесты CLI utility функций."""
from __future__ import annotations

import pytest

from skills_hub_cli.__main__ import _strip_invite_url


@pytest.mark.parametrize(
    ("input_", "expected"),
    [
        ("ABC123_xyz-token", "ABC123_xyz-token"),
        ("https://hub.local/auth/invite/ABC123", "ABC123"),
        ("http://localhost:8000/invite/ABC123_xyz", "ABC123_xyz"),
        ("https://hub.local/auth/invite/ABC123/", "ABC123"),
    ],
)
def test_strip_invite_url(input_: str, expected: str) -> None:
    assert _strip_invite_url(input_) == expected
