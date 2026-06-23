"""cli-kits W5: output.py — тонкая обёртка над clikit.output.

Гарантии (контракт, который НЕ должны сломать потребители commands/*/__main__):
- публичный API сохранён: emit_data/emit_message/emit_error/is_json/
  init_output_mode/console;
- дефолтный режим — text (как сейчас; --json переключает);
- emit_data/emit_message делегируют в clikit.output;
- mutate module._mode (как делают 47 тестовых файлов) ВЛИЯЕТ на поведение;
- json-режим: данные в stdout (JSON Lines), сообщения/ошибки в stderr.
"""
from __future__ import annotations

import json

import pytest

import clikit.output as clikit_output
from skills_hub_cli import output as out


@pytest.fixture(autouse=True)
def _reset_mode() -> None:
    """Каждый тест стартует с дефолта (text) и восстанавливает после."""
    saved = out._mode
    out._mode = "text"
    yield
    out._mode = saved


# ---------- публичный API на месте ----------
def test_public_api_present() -> None:
    for name in ("emit_data", "emit_message", "emit_error", "is_json",
                 "init_output_mode", "console"):
        assert hasattr(out, name), name


def test_backed_by_clikit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """output.py использует clikit.output как бэкенд (а не свой json.dumps).

    Проверка реальной делегации: подменяем clikit.output.emit_data —
    обёртка обязана его вызвать.
    """
    calls: list = []
    monkeypatch.setattr(clikit_output, "emit_data", lambda *a, **k: calls.append((a, k)))
    out._mode = "json"
    out.emit_data({"z": 9})
    assert calls, "output.emit_data должен делегировать в clikit.output.emit_data"


# ---------- дефолт = text ----------
def test_default_mode_is_text() -> None:
    out.init_output_mode(json_flag=False, config_format="")
    assert out.is_json() is False


def test_json_flag_forces_json() -> None:
    out.init_output_mode(json_flag=True, config_format="text")
    assert out.is_json() is True


def test_config_format_json_when_no_flag() -> None:
    out.init_output_mode(json_flag=False, config_format="json")
    assert out.is_json() is True


def test_env_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILLS_HUB_OUTPUT", "json")
    out.init_output_mode(json_flag=False, config_format="text")
    assert out.is_json() is True


# ---------- mutate _mode напрямую влияет (контракт 47 тестов) ----------
def test_mutating_mode_attr_affects_emit_data_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out._mode = "json"
    out.emit_data({"a": 1})
    captured = capsys.readouterr()
    assert json.loads(captured.out.strip()) == {"a": 1}


def test_mutating_mode_attr_affects_emit_data_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list = []
    out._mode = "text"
    out.emit_data({"a": 1}, text_renderer=lambda d: seen.append(d))
    assert seen == [{"a": 1}]
    captured = capsys.readouterr()
    assert captured.out.strip() == ""  # рендерер ничего не печатал в stdout


# ---------- emit_message ----------
def test_emit_message_json_goes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    out._mode = "json"
    out.emit_message("привет", level="warn", extra_field="x")
    captured = capsys.readouterr()
    assert captured.out.strip() == ""  # stdout — чистый машинный канал
    rec = json.loads(captured.err.strip().splitlines()[-1])
    assert rec["event"] == "warn"
    assert rec["message"] == "привет"
    assert rec["extra_field"] == "x"


# ---------- emit_error ----------
def test_emit_error_json_contract(capsys: pytest.CaptureFixture[str]) -> None:
    out._mode = "json"
    out.emit_error("API", "что-то пошло не так", status_code=404)
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    rec = json.loads(captured.err.strip().splitlines()[-1])
    assert rec["event"] == "error"
    assert rec["code"] == "API"
    assert rec["message"] == "что-то пошло не так"


def test_emit_error_legacy_signature_no_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Старая сигнатура emit_error(code, message, **extra) без status_code."""
    out._mode = "json"
    out.emit_error("RUNTIME", "boom", hint="retry")
    captured = capsys.readouterr()
    rec = json.loads(captured.err.strip().splitlines()[-1])
    assert rec["code"] == "RUNTIME"
    assert rec["message"] == "boom"
    assert rec["hint"] == "retry"
