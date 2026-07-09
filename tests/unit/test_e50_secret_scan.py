"""E50 — secret-scan: regex fallback, gitleaks (mock subprocess), masking.

Реальных секретов в тестах НЕТ — используется публичный AWS example key
`AKIAIOSFODNN7EXAMPLE` и синтетические паттерны.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillery_cli.core import secret_scan
from skillery_cli.core.secret_scan import (
    Finding,
    mask_secret,
    regex_scan_dir,
    scan_dir,
    scan_text,
)

# Публичный AWS example access key (НЕ настоящий секрет — из доков AWS).
AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"


# --------------------------------------------------------------------------
# Маскировка
# --------------------------------------------------------------------------
def test_mask_secret_keeps_head_tail() -> None:
    masked = mask_secret(AWS_EXAMPLE_KEY)
    assert masked == "AKIA…MPLE"
    # Полного секрета в маске нет.
    assert AWS_EXAMPLE_KEY not in masked


def test_mask_secret_short_fully_hidden() -> None:
    assert mask_secret("abcd") == "****"
    assert mask_secret("") == ""


# --------------------------------------------------------------------------
# Regex-скан: прямые паттерны
# --------------------------------------------------------------------------
def test_scan_text_detects_aws_key() -> None:
    findings = scan_text("config.py", f'AWS_KEY = "{AWS_EXAMPLE_KEY}"')
    rules = {f.rule for f in findings}
    assert "aws-akia" in rules  # G5: rule-ID из skillgate
    # snippet замаскирован — полного ключа нет.
    for f in findings:
        assert AWS_EXAMPLE_KEY not in f.snippet


def test_scan_text_detects_private_key_header() -> None:
    findings = scan_text("id_rsa", "-----BEGIN RSA PRIVATE KEY-----")
    assert any(f.rule == "private-key-block" for f in findings)  # G5: skillgate-ID


def test_scan_text_detects_password_assignment() -> None:
    findings = scan_text("settings.ini", "password = hunter2supersecret")
    assert any(f.rule == "generic-password" for f in findings)


def test_scan_text_detects_token_assignment() -> None:
    findings = scan_text(
        "app.py", "token = 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345'"
    )
    # G5: skillgate ловит и github-token (формат), и generic-token-assignment.
    assert any(
        f.rule in ("generic-token-assignment", "github-token") for f in findings
    )


def test_scan_text_high_entropy_in_secret_context() -> None:
    # 40-символьная случайная base64 в контексте "api_secret".
    findings = scan_text(
        "creds.env",
        "api_secret=Zx9KpL2mNqRsTuVwXyAbCdEfGhIjKlMnOpQrStUv",
    )
    assert any(f.rule in ("high-entropy-string", "generic-token") for f in findings)


def test_scan_text_clean_no_findings() -> None:
    findings = scan_text(
        "readme.md", "# Title\n\nThis is documentation with no secrets.\n"
    )
    assert findings == []


def test_scan_text_low_entropy_not_flagged() -> None:
    # Длинная, но НЕ случайная строка (повторы) — без контекста secret.
    findings = scan_text("data.txt", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    assert findings == []


# --------------------------------------------------------------------------
# Regex-скан папки
# --------------------------------------------------------------------------
def test_regex_scan_dir_finds_secret_in_nested_file(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text("---\nname: x\n---\nclean\n", encoding="utf-8")
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "creds.py").write_text(
        f'AWS_ACCESS_KEY_ID = "{AWS_EXAMPLE_KEY}"\n', encoding="utf-8"
    )
    findings = regex_scan_dir(tmp_path)
    assert len(findings) >= 1
    assert findings[0].file == "src/creds.py"
    assert findings[0].line == 1


def test_regex_scan_dir_skips_ignored_dirs(tmp_path: Path) -> None:
    local = tmp_path / "_local"
    local.mkdir()
    (local / "secret.txt").write_text(
        f"password = {AWS_EXAMPLE_KEY}", encoding="utf-8"
    )
    git = tmp_path / ".git"
    git.mkdir()
    (git / "config").write_text(f"token = '{AWS_EXAMPLE_KEY}xxxxxx'", encoding="utf-8")
    findings = regex_scan_dir(tmp_path)
    assert findings == []


def test_regex_scan_dir_skips_binary_suffix(tmp_path: Path) -> None:
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n" + AWS_EXAMPLE_KEY.encode())
    findings = regex_scan_dir(tmp_path)
    assert findings == []


def test_regex_scan_dir_clean(tmp_path: Path) -> None:
    (tmp_path / "SKILL.md").write_text(
        "---\nname: clean\ndescription: ok\n---\nbody\n", encoding="utf-8"
    )
    assert regex_scan_dir(tmp_path) == []


# --------------------------------------------------------------------------
# scan_dir: выбор движка (gitleaks vs regex)
# --------------------------------------------------------------------------
def test_scan_dir_uses_regex_when_gitleaks_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secret_scan, "gitleaks_available", lambda: False)
    (tmp_path / "f.py").write_text(f'key="{AWS_EXAMPLE_KEY}"', encoding="utf-8")
    result = scan_dir(tmp_path)
    assert result.backend == "regex"
    assert result.gitleaks_available is False
    assert len(result.findings) >= 1


def test_scan_dir_clean_regex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secret_scan, "gitleaks_available", lambda: False)
    (tmp_path / "f.py").write_text("x = 1\n", encoding="utf-8")
    result = scan_dir(tmp_path)
    assert result.findings == []
    assert result.backend == "regex"


def test_scan_dir_uses_gitleaks_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gitleaks доступен → subprocess замокан, отчёт распарсен."""
    monkeypatch.setattr(secret_scan, "gitleaks_available", lambda: True)

    gitleaks_report = [
        {
            "File": str(tmp_path / "leak.py"),
            "StartLine": 7,
            "RuleID": "aws-access-token",
            "Secret": AWS_EXAMPLE_KEY,
            "Match": f'key = "{AWS_EXAMPLE_KEY}"',
        }
    ]

    class _FakeProc:
        returncode = 1  # gitleaks: leaks found
        stdout = ""
        stderr = ""

    def _fake_run(cmd, capture_output, text):
        # Эмулируем gitleaks: пишем JSON-отчёт в --report-path.
        idx = cmd.index("--report-path")
        Path(cmd[idx + 1]).write_text(json.dumps(gitleaks_report), encoding="utf-8")
        return _FakeProc()

    monkeypatch.setattr(secret_scan.subprocess, "run", _fake_run)

    result = scan_dir(tmp_path)
    assert result.backend == "gitleaks"
    assert result.gitleaks_available is True
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.rule == "aws-access-token"
    assert f.line == 7
    assert f.file == "leak.py"
    # Маскировка: полного секрета нет.
    assert AWS_EXAMPLE_KEY not in f.snippet
    assert f.snippet == "AKIA…MPLE"


def test_scan_dir_gitleaks_clean_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(secret_scan, "gitleaks_available", lambda: True)

    class _FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, capture_output, text):
        idx = cmd.index("--report-path")
        Path(cmd[idx + 1]).write_text("[]", encoding="utf-8")
        return _FakeProc()

    monkeypatch.setattr(secret_scan.subprocess, "run", _fake_run)
    result = scan_dir(tmp_path)
    assert result.backend == "gitleaks"
    assert result.findings == []


def test_scan_dir_gitleaks_crash_falls_back_to_regex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gitleaks есть, но падает (exit 2) → деградируем на regex, не теряя проверку."""
    monkeypatch.setattr(secret_scan, "gitleaks_available", lambda: True)
    (tmp_path / "f.py").write_text(f'key="{AWS_EXAMPLE_KEY}"', encoding="utf-8")

    class _FakeProc:
        returncode = 2  # реальная ошибка gitleaks
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(
        secret_scan.subprocess, "run", lambda *a, **k: _FakeProc()
    )
    result = scan_dir(tmp_path)
    assert result.backend == "regex"
    assert result.gitleaks_available is True  # был доступен, но упал
    assert len(result.findings) >= 1


def test_finding_is_frozen() -> None:
    f = Finding(file="a", line=1, rule="r", snippet="s")
    with pytest.raises((AttributeError, Exception)):
        f.line = 2  # type: ignore[misc]
