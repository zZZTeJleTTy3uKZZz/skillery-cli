"""#1221 END-TO-END: реальный shim → реальный ``skillery run`` → РОВНО один конверт.

Почему одних моков мало
-----------------------
Юнит-тесты (``test_cmd_run.py``) держат логику обёртки, а тесты кита — текст
shim'а. Но вся фича живёт ровно в СТЫКЕ этих двух половин, и ломается именно
он: shim зовёт ``skillery run``, а ``skillery run`` обязан звать entrypoint
НАПРЯМУЮ. Ошибись здесь — и получишь либо бесконечную рекурсию (run зовёт
shim), либо ДВА события на один вызов. Двойной учёт хуже отсутствующего: он
молча врёт в метрике, ради которой всё и делалось.

Поэтому здесь ничего не подменяется: на диск кладётся настоящий shim (его
пишет сам кит), на PATH — настоящий CLI, и shim реально исполняется оболочкой
ОС. Проверяется то, что нельзя проверить грепом: навык отработал, и в outbox'е
ровно ОДИН конверт.

Тест пропускается на ките ``s-skillkit`` < 0.3.5: там shim ещё зовёт entrypoint
напрямую, и стыка, который проверяем, физически не существует.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from telemetrykit import STATUS_ERROR, STATUS_OK

SLUG = "e2e-skill"
COMMAND = "e2etool"
VERSION = "2.0.0"

pytestmark = pytest.mark.skipif(
    not hasattr(__import__("skillkit.path_store", fromlist=["x"]), "set_runner"),
    reason="s-skillkit < 0.3.5: shim ещё не ведёт через раннер учёта",
)


def _kit():
    from skillkit import path_store

    return path_store


def _write_skill(store: Path, entrypoint_spec: str) -> None:
    """Навык в сторе — ровно в той раскладке, что делает установщик."""
    skill_dir = store / SLUG
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {SLUG}\n---\n", encoding="utf-8")
    (skill_dir / "_skill_meta.json").write_text(
        json.dumps(
            {
                "slug": SLUG,
                "version": VERSION,
                "source": "hub",
                "manifest": {
                    "version": VERSION,
                    "kind": "tooling",
                    "cli": [
                        {"command_name": COMMAND, "entrypoint": entrypoint_spec}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )


def _write_payload(path: Path, log: Path) -> str:
    """Сам «навык»: отмечается в логе. Возвращает строку entrypoint для shim'а."""
    path.write_text(
        "\n".join(
            [
                "import sys",
                "log = " + repr(str(log)),
                "with open(log, 'a', encoding='utf-8') as fh:",
                "    fh.write('SKILL ' + ' '.join(sys.argv[1:]))",
                "    fh.write(chr(10))",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{path}"'


def _install_runner_stub(dirpath: Path) -> None:
    """НАСТОЯЩИЙ CLI под именем ``skillery`` на PATH (не заглушка-регистратор).

    ``PYTHONPATH`` тащим из ``sys.path`` текущего интерпретатора: тест может
    идти в оверлейном окружении (``uv run --with-editable``), где сам
    ``sys.executable`` про ``skillery_cli`` ничего не знает. Нам нужен именно
    ТОТ код, что проверяем, а не какой-то установленный рядом.
    """
    dirpath.mkdir(parents=True, exist_ok=True)
    pythonpath = os.pathsep.join(p for p in sys.path if p)
    if sys.platform == "win32":
        (dirpath / "skillery.cmd").write_text(
            "@echo off\r\n"
            f'set "PYTHONPATH={pythonpath}"\r\n'
            f'"{sys.executable}" -m skillery_cli %*\r\n',
            encoding="utf-8",
            newline="",
        )
    else:
        stub = dirpath / "skillery"
        stub.write_text(
            "#!/bin/sh\n"
            f'PYTHONPATH="{pythonpath}"\n'
            "export PYTHONPATH\n"
            f'exec "{sys.executable}" -m skillery_cli "$@"\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)


@pytest.fixture()
def scene(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Стор + bin + outbox + настоящий shim + настоящий CLI на PATH."""
    store = tmp_path / "store"
    bin_dir = tmp_path / "bin"
    outbox = tmp_path / "outbox.jsonl"
    log = tmp_path / "calls.log"
    runner_dir = tmp_path / "runner-bin"

    store.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)

    entrypoint = _write_payload(tmp_path / "payload.py", log)
    _write_skill(store, entrypoint)
    _install_runner_stub(runner_dir)

    # Каталоги отдаём через env — их увидит и родитель (генерация shim'а), и
    # ДОЧЕРНИЙ настоящий CLI. Подмена атрибутов сюда не годится: у ребёнка
    # свой процесс, и никакие monkeypatch до него не доедут.
    monkeypatch.setenv("SKILLERY_STORE_DIR", str(store))
    monkeypatch.setenv("SKILLERY_BIN_DIR", str(bin_dir))
    monkeypatch.setenv("SKILLERY_OUTBOX_PATH", str(outbox))

    shim = _kit().add_cli(COMMAND, entrypoint, skill_slug=SLUG)
    assert shim.exists(), "кит обязан положить shim на диск"

    return {
        "shim": shim,
        "log": log,
        "outbox": outbox,
        "runner_dir": runner_dir,
        "bin_dir": bin_dir,
    }


def _run_shim(scene: dict[str, Path], args: list[str]) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "PATH": str(scene["runner_dir"]) + os.pathsep + os.environ.get("PATH", ""),
    }
    return subprocess.run(  # noqa: S603 — исполняем собственный сгенерированный shim
        [str(scene["shim"]), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _envelopes(outbox: Path) -> list[dict]:
    """Конверты запусков навыка из настоящего outbox-файла."""
    if not outbox.exists():
        return []
    out = []
    for line in outbox.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            env = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(env, dict) and env.get("kind") == "skill_run":
            out.append(env)
    return out


def test_shim_call_runs_the_skill_and_writes_exactly_one_envelope(
    scene: dict[str, Path],
) -> None:
    """ЦКП задачи целиком: вызвали команду навыка — навык отработал, учёт ОДИН.

    «Ровно один» — не придирка. Два конверта на вызов завысили бы метрику
    ровно вдвое и были бы неотличимы от роста использования.
    """
    result = _run_shim(scene, ["post", "create"])

    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"

    # (а) навык реально отработал и получил свои аргументы без изменений…
    assert scene["log"].read_text(encoding="utf-8").splitlines() == [
        "SKILL post create"
    ]

    # (б) …и учёт сработал РОВНО один раз, с идентичностью навыка, а не CLI.
    envelopes = _envelopes(scene["outbox"])
    assert len(envelopes) == 1, f"ожидали один конверт, получили {len(envelopes)}"
    payload = envelopes[0]["payload"]
    assert payload["component_id"] == SLUG
    assert payload["component_version"] == VERSION
    assert payload["status"] == STATUS_OK


def test_nonzero_exit_of_skill_survives_the_whole_chain(
    tmp_path: Path, scene: dict[str, Path]
) -> None:
    """Код возврата навыка проходит shim → run → вызывающего НЕИЗМЕННЫМ."""
    failing = tmp_path / "failing.py"
    failing.write_text("import sys\nsys.exit(9)\n", encoding="utf-8")
    entrypoint = f'"{sys.executable}" "{failing}"'
    _write_skill(tmp_path / "store", entrypoint)
    _kit().add_cli(COMMAND, entrypoint, skill_slug=SLUG)

    result = _run_shim(scene, [])

    assert result.returncode == 9, f"stderr={result.stderr!r}"
    envelopes = _envelopes(scene["outbox"])
    assert len(envelopes) == 1
    assert envelopes[0]["payload"]["status"] == STATUS_ERROR


def test_chain_terminates_and_does_not_recurse(scene: dict[str, Path]) -> None:
    """Цепочка КОНЕЧНА: ни зависания, ни лавины конвертов от витков цикла.

    Именно это ломается, если ``run`` начнёт резолвить исполняемое в shim.
    Таймаут здесь — часть проверки: рекурсия не завершилась бы никогда.
    """
    result = _run_shim(scene, ["ping"])

    assert result.returncode == 0
    assert len(_envelopes(scene["outbox"])) == 1
    assert scene["log"].read_text(encoding="utf-8").count("SKILL") == 1
