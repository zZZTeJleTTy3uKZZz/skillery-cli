"""#1221: ``skillery run <slug>`` — обёртка, фиксирующая ФАКТ вызова навыка.

Что закреплено
--------------
Метрика «запуски» была вырождена (равна установкам), потому что событие
использования навыка никто не писал: просить LLM отчитаться о вызове —
ненадёжно. Обёртка переносит учёт в КОД, и тесты держат её контракт:

* успешный вызов → РОВНО один конверт ``skill_run`` со статусом успеха и
  идентичностью навыка (slug + версия из его меты), а не CLI;
* ненулевой код возврата навыка проходит наружу НЕИЗМЕННЫМ (иначе обёртка
  сломала бы вызывающие скрипты) и попадает в событие как ошибка;
* прерывание (Ctrl-C) — отдельный статус, а не «ошибка навыка»;
* сломанная телеметрия НЕ ломает запуск: команда важнее метрики;
* значения аргументов в конверт не попадают — только имена флагов
  (приватность санитайзера кита не ослабляется обёрткой).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from librarykit.errors import CliError
from telemetrykit import STATUS_ERROR, STATUS_INTERRUPTED, STATUS_OK, outbox

from skillery_cli import output as output_module
from skillery_cli.commands import run as run_mod
from skillery_cli.config import ClientConfig

SLUG = "demo-skill"
VERSION = "1.4.2"


# --------------------------------------------------------------------------
#  Фикстуры: стор с навыком + подменённый запуск процесса
# --------------------------------------------------------------------------
def _make_skill(store: Path, *, with_cli: bool, slug: str = SLUG) -> Path:
    """Материализовать навык в сторе так же, как это делает установщик."""
    skill_dir = store / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {slug}\n---\n", encoding="utf-8")
    manifest: dict[str, Any] = {"version": VERSION, "kind": "prompt", "cli": []}
    if with_cli:
        manifest["kind"] = "tooling"
        manifest["cli"] = [{"command_name": slug, "entrypoint": "demo_skill.cli:main"}]
    (skill_dir / "_skill_meta.json").write_text(
        json.dumps({"slug": slug, "version": VERSION, "manifest": manifest}),
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Стор навыков + конфиг CLI, указывающий на него. Вывод — text-режим."""
    output_module._mode = "text"
    store_dir = tmp_path / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    cfg = ClientConfig(base_url="http://localhost:8000", store_dir=str(store_dir))
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    return store_dir


class _Spawn:
    """Двойник запуска процесса: помнит argv и отдаёт заданный код возврата."""

    def __init__(self, code: int = 0, raises: BaseException | None = None) -> None:
        self.code = code
        self.raises = raises
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *, cwd: Path | None) -> int:
        self.calls.append(list(argv))
        if self.raises is not None:
            raise self.raises
        return self.code


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, spawn: _Spawn) -> _Spawn:
    monkeypatch.setattr(run_mod, "_spawn", spawn)
    return spawn


def _patch_shim(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str = SLUG) -> Path:
    """Подменить резолв shim'а: на диске его нет, но путь должен доехать до argv."""
    shim = tmp_path / "bin" / name
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(run_mod, "_shim_path", lambda command_name: shim)
    return shim


def _runs() -> list[dict[str, Any]]:
    """Конверты запусков навыков из общего outbox'а."""
    return [e for e in outbox.read_batch(10_000) if e.get("kind") == run_mod.KIND_SKILL_RUN]


# ══════════════════════════ (а) успешный вызов ══════════════════════════
class TestSuccessfulRun:
    def test_writes_single_ok_envelope_with_skill_identity(
        self, store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_skill(store, with_cli=True)
        shim = _patch_shim(monkeypatch, tmp_path)
        spawn = _patch_spawn(monkeypatch, _Spawn(code=0))

        run_mod.cmd_run(SLUG, ["post", "create"])

        envelopes = _runs()
        assert len(envelopes) == 1, "ровно один конверт на один запуск"
        payload = envelopes[0]["payload"]
        assert payload["status"] == STATUS_OK
        # Идентичность — НАВЫКА, а не CLI: иначе события всех навыков слились бы.
        assert payload["component_id"] == SLUG
        assert payload["component_version"] == VERSION
        # Аргументы ушли навыку как есть, первым идёт shim стора.
        assert spawn.calls == [[str(shim), "post", "create"]]

    def test_prompt_skill_without_artifacts_is_checkpoint(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Запускать нечего — но факт использования всё равно фиксируется."""
        _make_skill(store, with_cli=False)
        spawn = _patch_spawn(monkeypatch, _Spawn(code=0))

        run_mod.cmd_run(SLUG, [])

        assert spawn.calls == [], "prompt-навык не порождает процесс"
        envelopes = _runs()
        assert len(envelopes) == 1
        assert envelopes[0]["payload"]["status"] == STATUS_OK


# ═════════════════ (б) ненулевой код возврата пробрасывается ═════════════════
class TestExitCodePassthrough:
    def test_nonzero_code_propagates_unchanged_and_marks_error(
        self, store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_skill(store, with_cli=True)
        _patch_shim(monkeypatch, tmp_path)
        _patch_spawn(monkeypatch, _Spawn(code=3))

        with pytest.raises(SystemExit) as exc:
            run_mod.cmd_run(SLUG, [])

        # Ровно 3, а не «1»: обёртка не имеет права подменять код навыка.
        assert exc.value.code == 3
        assert _runs()[0]["payload"]["status"] == STATUS_ERROR


# ══════════════════════════ (в) прерывание ══════════════════════════
class TestInterrupted:
    def test_ctrl_c_gets_its_own_status_and_130(
        self, store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_skill(store, with_cli=True)
        _patch_shim(monkeypatch, tmp_path)
        _patch_spawn(monkeypatch, _Spawn(raises=KeyboardInterrupt()))

        with pytest.raises(SystemExit) as exc:
            run_mod.cmd_run(SLUG, [])

        assert exc.value.code == run_mod.EXIT_INTERRUPTED
        # Прерванные запуски не должны раздувать долю ошибок.
        assert _runs()[0]["payload"]["status"] == STATUS_INTERRUPTED


# ════════════════ (г) сломанная телеметрия не ломает запуск ════════════════
class TestTelemetryNeverBreaksTheRun:
    def test_tracker_creation_failure_is_survived(
        self, store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_skill(store, with_cli=True)
        _patch_shim(monkeypatch, tmp_path)
        spawn = _patch_spawn(monkeypatch, _Spawn(code=0))

        import telemetrykit

        def _boom(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("телеметрия сломана")

        monkeypatch.setattr(telemetrykit, "track_run", _boom)

        run_mod.cmd_run(SLUG, [])

        assert len(spawn.calls) == 1, "команда пользователя важнее метрики"

    def test_unwritable_outbox_is_survived_and_code_still_passes(
        self, store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Outbox недоступен (путь ведёт ВНУТРЬ файла) — запуск идёт как обычно."""
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("", encoding="utf-8")
        monkeypatch.setenv("SKILLERY_OUTBOX_PATH", str(blocker / "outbox.jsonl"))

        _make_skill(store, with_cli=True)
        _patch_shim(monkeypatch, tmp_path)
        spawn = _patch_spawn(monkeypatch, _Spawn(code=7))

        with pytest.raises(SystemExit) as exc:
            run_mod.cmd_run(SLUG, [])

        assert exc.value.code == 7
        assert len(spawn.calls) == 1


# ═══════════════ (д) приватность: значений аргументов в событии нет ═══════════════
class TestArgumentPrivacy:
    def test_only_flag_names_reach_the_envelope(
        self, store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_skill(store, with_cli=True)
        _patch_shim(monkeypatch, tmp_path)
        spawn = _patch_spawn(monkeypatch, _Spawn(code=0))

        secret = "79001234567"
        run_mod.cmd_run(SLUG, ["message", "send", "--phone", secret, f"--token={secret}"])

        payload = _runs()[0]["payload"]
        assert payload["arg_names"] == ["--phone", "--token"]
        assert secret not in json.dumps(payload, ensure_ascii=False)
        # …при этом САМ навык получил значения полностью.
        assert secret in spawn.calls[0]


# ══════════════════════════ ошибки — по иерархии CLI ══════════════════════════
class TestErrors:
    def test_unknown_skill_raises_cli_error_in_russian(self, store: Path) -> None:
        with pytest.raises(CliError) as exc:
            run_mod.cmd_run("нет-такого", [])
        assert exc.value.code == "SKILL_NOT_FOUND"
        assert "не установлен" in str(exc.value)

    def test_unknown_command_of_skill_raises_cli_error(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_skill(store, with_cli=True)
        with pytest.raises(CliError) as exc:
            run_mod.cmd_run(SLUG, [], command_name="нетакой")
        assert exc.value.code == "COMMAND_NOT_FOUND"

    def test_missing_shim_and_no_path_command_raises_cli_error(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ни shim'а, ни команды в PATH → рецепт починки, а не FileNotFoundError."""
        _make_skill(store, with_cli=True)
        monkeypatch.setattr(run_mod, "_shim_path", lambda command_name: None)
        monkeypatch.setattr("shutil.which", lambda name: None)

        with pytest.raises(CliError) as exc:
            run_mod.cmd_run(SLUG, [])
        assert exc.value.code == "CLI_NOT_REGISTERED"


# ══════════════════ реальный процесс (без двойника _spawn) ══════════════════
class TestRealChildProcess:
    """``_spawn`` не замокан: проверяем сам контракт запуска через ``proc.popen``.

    Двойник ``_spawn`` покрывает логику обёртки, но не то, что запуск вообще
    состоится: ошибка в kwargs ``popen`` (stdio, политика окон win32) видна
    только на живом процессе.
    """

    def test_child_exit_code_reaches_caller(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        _make_skill(store, with_cli=True)
        monkeypatch.setattr(
            run_mod,
            "_resolve_executable",
            lambda entry, slug: [sys.executable, "-c", "import sys; sys.exit(5)"],
        )

        with pytest.raises(SystemExit) as exc:
            run_mod.cmd_run(SLUG, [])

        assert exc.value.code == 5
        assert _runs()[0]["payload"]["status"] == STATUS_ERROR


# ═════════════ инструкции навыков ведут вызов через обёртку ═════════════
@pytest.mark.parametrize("kind", ["prompt", "comprehensive", "tooling"])
def test_generated_skill_md_routes_calls_through_wrapper(kind: str) -> None:
    """Половина ЦКП — инструкции. Скаффолд обязан диктовать вызов обёрткой.

    Иначе учёт снова упирается в готовность модели отчитаться о вызове — ровно
    то, что #1221 и убирает.
    """
    from skillery_cli.templates import render_skill_md

    text = render_skill_md(slug="my-skill", kind=kind, description="демо")
    assert "skillery run my-skill" in text


# ══════════════════════════ регистрация в CLI ══════════════════════════
def test_run_is_registered_in_base_command_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` доступен без permissions: запускают навыки в т.ч. незалогиненные."""
    from skillery_cli.__main__ import build_app

    monkeypatch.setattr(
        ClientConfig, "load", classmethod(lambda cls: ClientConfig(base_url="http://x"))
    )
    names = {c.name for c in build_app().registered_commands}
    assert "run" in names
