"""E50 — cmd_publish integration: abort on findings, --force, --strict.

Тестируем helper `_run_publish_secret_scan` напрямую (он поднимает
typer.Exit) — мокаем `secret_scan_dir`, чтобы не зависеть от gitleaks/FS.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import typer

import skillery_cli.__main__ as cli
from skillery_cli.core.secret_scan import Finding, ScanResult

AWS_EXAMPLE_KEY = "AKIAIOSFODNN7EXAMPLE"


def _result(
    findings: list[Finding], *, backend: str = "regex", available: bool = False
) -> ScanResult:
    return ScanResult(
        findings=findings, backend=backend, gitleaks_available=available
    )


def _leak() -> Finding:
    return Finding(
        file="src/creds.py", line=3, rule="aws-access-key", snippet="AKIA…MPLE"
    )


def test_clean_scan_does_not_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "secret_scan_dir", lambda d: _result([], available=True, backend="gitleaks")
    )
    # Не должно бросить.
    cli._run_publish_secret_scan(tmp_path, force=False, strict=False)


def test_findings_abort_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "secret_scan_dir", lambda d: _result([_leak()]))
    with pytest.raises(typer.Exit) as exc:
        cli._run_publish_secret_scan(tmp_path, force=False, strict=False)
    assert exc.value.exit_code == 1


def test_findings_force_override_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "secret_scan_dir", lambda d: _result([_leak()]))
    # --force → продолжаем (не бросаем).
    cli._run_publish_secret_scan(tmp_path, force=True, strict=False)


def test_gitleaks_absent_warns_but_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "secret_scan_dir", lambda d: _result([], available=False)
    )
    # gitleaks нет, но clean → warning + продолжаем (не бросаем).
    cli._run_publish_secret_scan(tmp_path, force=False, strict=False)


def test_gitleaks_absent_strict_aborts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli, "secret_scan_dir", lambda d: _result([], available=False)
    )
    with pytest.raises(typer.Exit) as exc:
        cli._run_publish_secret_scan(tmp_path, force=False, strict=True)
    assert exc.value.exit_code == 1


def test_findings_printed_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Вывод находки содержит file:line + rule, но НЕ полный секрет."""
    monkeypatch.setattr(cli, "secret_scan_dir", lambda d: _result([_leak()]))
    with pytest.raises(typer.Exit):
        cli._run_publish_secret_scan(tmp_path, force=False, strict=False)
    out = capsys.readouterr().out
    assert "src/creds.py:3" in out
    assert "aws-access-key" in out
    assert AWS_EXAMPLE_KEY not in out


def test_publish_has_force_strict_options() -> None:
    """cmd_publish экспонирует --force / --strict / --skip-secret-scan."""
    import inspect

    params = inspect.signature(cli.cmd_publish).parameters
    assert "force" in params
    assert "strict" in params
    assert "skip_secret_scan" in params
