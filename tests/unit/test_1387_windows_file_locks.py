"""#1387: два Windows-дефекта класса «файл занят другим процессом».

Оба вылезали стеной трейсбека на успешной команде ``skillery login``:

1. ``RotatingFileHandler`` пытался переименовать ``logs/daemon.log``, который
   держит открытым живой демон → ``PermissionError: [WinError 32]``, а
   ``logging.handleError`` печатал полный traceback в stderr;
2. ``install_watchdog`` переписывал ``skillery-watchdog.vbs``, который прямо
   сейчас исполняет ``wscript`` → ``PermissionError: [Errno 13]`` и
   «watchdog не установлен».

Реальную межпроцессную блокировку на CI (и на POSIX) не воспроизвести, поэтому
блокировка мокается на уровне вызова, который её и получает: ``os.replace``
для ротации и ``Path.write_bytes`` для лаунчера.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from skillery_cli.core import logging_setup
from skillery_cli.daemon import autostart


# ── 1. ротация лога под чужой блокировкой ───────────────────────────────────
def _handler(tmp_path: Path, **kw) -> logging_setup.SafeRotatingFileHandler:
    h = logging_setup.SafeRotatingFileHandler(
        tmp_path / "daemon.log", maxBytes=200, backupCount=2,
        encoding="utf-8", **kw,
    )
    h.setFormatter(logging.Formatter("%(message)s"))
    return h


def _record(msg: str) -> logging.LogRecord:
    return logging.LogRecord("t", logging.ERROR, __file__, 1, msg, None, None)


def test_rotation_under_foreign_lock_does_not_raise_and_keeps_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Занятый файл: записи продолжают уходить, stderr чист (нет стены трейсбека)."""
    handler = _handler(tmp_path)
    log = tmp_path / "daemon.log"

    def _locked(*_a, **_kw):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(os, "replace", _locked)
    monkeypatch.setattr(os, "rename", _locked)

    for i in range(30):  # заведомо перевалит за maxBytes=200
        handler.emit(_record(f"x{i} " + "y" * 40))
    handler.close()

    body = log.read_text(encoding="utf-8")
    assert "x0 " in body and "x29 " in body, "записи не должны теряться"
    assert not (tmp_path / "daemon.log.1").exists(), "ротация не прошла — и не должна"
    err = capsys.readouterr().err
    assert "Logging error" not in err and "Traceback" not in err, err


def test_rotation_retry_is_throttled_after_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """После неудачи ротация не пробуется снова, пока не истечёт пауза."""
    handler = _handler(tmp_path)
    calls = {"n": 0}

    def _locked(*_a, **_kw):
        calls["n"] += 1
        raise PermissionError(32, "locked")

    monkeypatch.setattr(os, "replace", _locked)
    monkeypatch.setattr(os, "rename", _locked)
    for i in range(30):
        handler.emit(_record(f"z{i} " + "y" * 40))
    assert calls["n"] == 1, "повторные попытки в замок — это и был симптом"

    # Пауза истекла — пробуем ещё раз (и снова не роняем).
    handler._rotate_blocked_until = 0.0
    handler.emit(_record("after " + "y" * 200))
    assert calls["n"] == 2
    handler.close()


def test_rotation_still_works_when_file_is_free(tmp_path: Path) -> None:
    """Без блокировки поведение прежнее: файл ротируется."""
    handler = _handler(tmp_path)
    for i in range(30):
        handler.emit(_record(f"w{i} " + "y" * 40))
    handler.close()
    assert (tmp_path / "daemon.log.1").exists()


def test_handler_is_lazy_until_first_record(tmp_path: Path) -> None:
    """``delay=True``: команда, которая ничего не записала, файл не открывает."""
    handler = _handler(tmp_path)
    assert handler.stream is None
    assert not (tmp_path / "daemon.log").exists()
    handler.emit(_record("first"))
    assert (tmp_path / "daemon.log").exists()
    handler.close()


def test_configure_logging_installs_safe_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Штатная настройка логов ставит именно устойчивый хендлер (оба входа)."""
    monkeypatch.setattr(logging_setup, "log_dir", lambda: tmp_path)
    logger = logging.getLogger("skillery")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logging_setup.configure_logging("ERROR", filename="daemon.log")
    assert logger.handlers
    assert all(
        isinstance(h, logging_setup.SafeRotatingFileHandler) for h in logger.handlers
    )
    ilog = logging_setup.install_logger("daemon.log")
    assert all(
        isinstance(h, logging_setup.SafeRotatingFileHandler) for h in ilog.handlers
    )
    for h in list(logger.handlers) + list(ilog.handlers):
        h.close()
        (logger if h in logger.handlers else ilog).removeHandler(h)


# ── 2. .vbs-лаунчер под чужой блокировкой ───────────────────────────────────
def test_launcher_write_is_noop_when_content_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Совпало содержимое → файл не переписывается (нечего ломать блокировкой)."""
    target = tmp_path / "skillery-watchdog.vbs"
    target.write_bytes(b"body")

    def _boom(*_a, **_kw):
        raise AssertionError("не должны писать, когда содержимое совпадает")

    monkeypatch.setattr(Path, "write_bytes", _boom)
    assert autostart.write_launcher_script(target, "body") == target


def test_launcher_write_is_byte_exact_and_idempotent_with_crlf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Настоящее содержимое .vbs (CRLF) — второй проход обязан быть no-op.

    Ловушка, из-за которой «пропуск записи» не работал бы вовсе: ``write_text``
    на Windows транслирует ``\\n`` в ``\\r\\n``, и CRLF-текст ложился на диск
    как ``\\r\\r\\n``. Сравнение прочитанного с исходником не совпадало никогда
    — а значит, при КАЖДОМ login мы бы лезли писать в занятый wscript'ом файл.
    """
    target = tmp_path / "skillery-watchdog.vbs"
    text = "line1" + autostart._WIN_EOL + "line2" + autostart._WIN_EOL
    autostart.write_launcher_script(target, text)
    assert target.read_bytes() == text.encode("utf-8"), "лишний \\r от текстового режима"

    def _boom(*_a, **_kw):
        raise AssertionError("повторная запись того же содержимого не нужна")

    monkeypatch.setattr(Path, "write_bytes", _boom)
    autostart.write_launcher_script(target, text)


def test_launcher_write_survives_lock_when_file_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Занят и содержимое иное → прежний файл остаётся, исключения нет."""
    target = tmp_path / "skillery-watchdog.vbs"
    target.write_bytes(b"old")
    monkeypatch.setattr(
        Path, "write_bytes",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            PermissionError(13, "Permission denied")
        ),
    )
    logged: list[str] = []
    monkeypatch.setattr(
        autostart, "_log_autostart", lambda msg, **kw: logged.append(msg)
    )
    assert autostart.write_launcher_script(target, "new") == target
    assert target.read_bytes() == b"old"
    assert logged and "занят" in logged[0]


def test_launcher_write_raises_when_nothing_to_fall_back_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Файла нет и записать нельзя — это настоящая проблема, её не глушим."""
    monkeypatch.setattr(
        Path, "write_bytes",
        lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError(13, "denied")),
    )
    with pytest.raises(PermissionError):
        autostart.write_launcher_script(tmp_path / "absent.vbs", "text")


@pytest.mark.skipif(os.name != "nt", reason="watchdog ставится только на Windows")
def test_install_watchdog_ok_when_vbs_is_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Полный путь ``install_watchdog``: блокировка .vbs больше не срывает установку."""
    home = tmp_path
    vbs = home / autostart._HOME / "skillery-watchdog.vbs"
    vbs.parent.mkdir(parents=True, exist_ok=True)
    vbs.write_bytes(b"stale")
    monkeypatch.setattr(
        Path, "write_bytes",
        lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError(13, "denied")),
    )
    monkeypatch.setattr(autostart, "_log_autostart", lambda *_a, **_kw: None)

    class _Proc:
        returncode = 0
        stderr = ""

    monkeypatch.setattr(autostart, "proc_run", lambda *_a, **_kw: _Proc())
    res = autostart.install_watchdog(home_dir=home)
    assert res["installed"] is True, res
