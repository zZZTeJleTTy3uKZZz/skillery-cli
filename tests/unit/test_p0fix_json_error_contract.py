"""P0-фикс [MAJOR]: единый --json контракт ошибок в `_run` (__main__.py).

Факты живого e2e:
- (а) RuntimeError('git clone failed: ...') из installer не перехватывался
  `_run` → многоэкранный Rich-traceback; под --json stdout пуст;
- (б) ApiError печатался plain «Ошибка API: [404/...]» в stdout ДАЖЕ под
  --json;
- (в) VALIDATION-ошибки корректно давали JSON в stderr.
Три разных канала/формата → агентский парсинг ломается.

Требуемое поведение `_run`:
- except ApiError → json-режим: emit_error(код из ответа или 'API', message)
  = {"event":"error",...} в stderr; text-режим: прежнее читабельное
  «Ошибка API: ...»; exit 1.
- except RuntimeError → emit_error('RUNTIME', str(e)) — короткое сообщение
  без traceback в обоих режимах; exit 1.
"""
from __future__ import annotations

import json

import pytest

from skills_hub_cli import __main__ as main_mod
from skills_hub_cli import output as output_module
from skills_hub_cli.core.transport import ApiError


def _last_json_line(stream_text: str) -> dict:
    lines = [line for line in stream_text.strip().splitlines() if line.strip()]
    assert lines, f"ожидали JSON-строку, поток пуст: {stream_text!r}"
    return json.loads(lines[-1])


async def _raise_api_error(
    status_code: int = 404,
    code: str = "not_found",
    message: str = "Skill не найден",
) -> None:
    raise ApiError(status_code, code, message, {})


async def _raise_runtime_error(message: str) -> None:
    raise RuntimeError(message)


# ============================================================
# ApiError под --json → {"event":"error",...} в stderr + exit 1
# ============================================================
def test_run_api_error_json_mode_emits_json_error_and_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(output_module, "_mode", "json")

    with pytest.raises(SystemExit) as exc_info:
        main_mod._run(_raise_api_error())

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    # stdout чист — никакого plain «Ошибка API: ...»
    assert "Ошибка API" not in captured.out
    assert captured.out.strip() == ""
    rec = _last_json_line(captured.err)
    assert rec["event"] == "error"
    assert rec["code"] == "not_found"
    assert "Skill не найден" in rec["message"]
    assert "Traceback" not in captured.err


def test_run_api_error_json_mode_empty_code_falls_back_to_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(output_module, "_mode", "json")

    with pytest.raises(SystemExit) as exc_info:
        main_mod._run(_raise_api_error(status_code=500, code="", message="boom"))

    assert exc_info.value.code == 1
    rec = _last_json_line(capsys.readouterr().err)
    assert rec["event"] == "error"
    assert rec["code"] == "API"
    assert rec["message"] == "boom"


def test_run_api_error_text_mode_keeps_readable_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """text-режим НЕ ломаем: прежнее читабельное «Ошибка API: [404/...]»."""
    monkeypatch.setattr(output_module, "_mode", "text")

    with pytest.raises(SystemExit) as exc_info:
        main_mod._run(_raise_api_error(message="не найден"))

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Ошибка API" in captured.out
    assert "[404/not_found]" in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


# ============================================================
# RuntimeError → emit_error('RUNTIME', ...) без traceback + exit 1
# ============================================================
def test_run_runtime_error_json_mode_emits_json_error_and_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(output_module, "_mode", "json")

    with pytest.raises(SystemExit) as exc_info:
        main_mod._run(
            _raise_runtime_error("git clone failed: exit code 128 (fatal: repo not found)")
        )

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out.strip() == ""
    rec = _last_json_line(captured.err)
    assert rec["event"] == "error"
    assert rec["code"] == "RUNTIME"
    assert "git clone failed" in rec["message"]
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_run_runtime_error_text_mode_short_message_no_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(output_module, "_mode", "text")

    with pytest.raises(SystemExit) as exc_info:
        main_mod._run(_raise_runtime_error("git clone failed: exit code 128"))

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "git clone failed" in combined
    assert "RUNTIME" in combined
    assert "Traceback" not in combined


# ============================================================
# typer.Exit / typer.Abort — НЕ RUNTIME-ошибки (хотя наследуют RuntimeError
# через click) — обязаны пролетать насквозь без второго error-события
# ============================================================
def test_run_typer_exit_propagates_without_runtime_error_event(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Команды делают emit_error(...) + raise typer.Exit(1) внутри корутины.

    _run не должен превращать это в дублирующее {"code":"RUNTIME","message":"1"}.
    """
    import typer

    monkeypatch.setattr(output_module, "_mode", "json")

    async def _exit() -> None:
        raise typer.Exit(1)

    with pytest.raises(typer.Exit):
        main_mod._run(_exit())

    captured = capsys.readouterr()
    assert "RUNTIME" not in captured.err
    assert captured.err.strip() == ""


def test_run_typer_abort_propagates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import typer

    monkeypatch.setattr(output_module, "_mode", "json")

    async def _abort() -> None:
        raise typer.Abort()

    with pytest.raises(typer.Abort):
        main_mod._run(_abort())
    assert "RUNTIME" not in capsys.readouterr().err


# ============================================================
# Успешная корутина — поведение не изменилось
# ============================================================
def test_run_success_passes_through(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(output_module, "_mode", "json")
    done: list[bool] = []

    async def _ok() -> None:
        done.append(True)

    main_mod._run(_ok())
    assert done == [True]
    captured = capsys.readouterr()
    assert captured.err.strip() == ""
