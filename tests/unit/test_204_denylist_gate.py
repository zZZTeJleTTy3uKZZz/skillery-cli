"""#204: денилист-гейт внутренней инфы в publish (поверх секрет-скана).

_run_publish_denylist_gate через skillgate.scan_repo ловит абсолютные
windows-пути / публичные IP, блокирует publish (кроме --force). Манифест НЕ
гейтится (version из --tag, не из frontmatter).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import typer

from skillery_cli.__main__ import _run_publish_denylist_gate


def _skill(tmp_path: Path, code: str) -> Path:
    d = tmp_path / "skill"
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\n# s\n", encoding="utf-8"
    )
    (d / "code.py").write_text(code, encoding="utf-8")
    return d


def test_denylist_gate_blocks_windows_user_path(tmp_path: Path) -> None:
    d = _skill(tmp_path, 'LOG_DIR = "C:/Users/10001/Documents/secret"')  # leak-gate-allow
    with pytest.raises(typer.Exit):
        _run_publish_denylist_gate(d, force=False)


def test_denylist_gate_force_overrides(tmp_path: Path) -> None:
    d = _skill(tmp_path, 'LOG_DIR = "C:/Users/10001/Documents/secret"')  # leak-gate-allow
    # --force → предупреждение, но НЕ abort.
    _run_publish_denylist_gate(d, force=True)


def test_denylist_gate_clean_skill_passes(tmp_path: Path) -> None:
    d = _skill(tmp_path, "VALUE = 1  # чистый код без внутренней инфы")
    _run_publish_denylist_gate(d, force=False)  # no raise


def test_denylist_gate_ignores_secret_only(tmp_path: Path) -> None:
    # Секрет (AWS) — это забота секрет-скана, НЕ денилист-гейта (internal-info).
    # Денилист-гейт не должен абортить на чистом от внутр.инфы навыке с секретом.
    d = _skill(tmp_path, 'AWS = "AKIAIOSFODNN7EXAMPLE"')
    _run_publish_denylist_gate(d, force=False)  # no raise (не internal-info)
