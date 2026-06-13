"""Установка runtime-зависимостей навыка (E3 фаза 2).

Навык типа ``tooling`` (E6) может декларировать ``runtime_dependencies`` —
список ``{kind, spec}``, где ``kind ∈ {pip, npm, system}``:

- ``pip``    → ставим через **uv** (``uv pip install``) → fallback ``pip``
  (``python -m pip install``). Нет ни uv, ни pip → НЕ падаем, кладём в
  ``skipped`` с готовой инструкцией (graceful degradation, эталон
  ``reverse-factory/install.py``);
- ``npm``    → ставим через ``npm install -g`` только если ``npm`` есть в PATH;
  иначе ``skipped`` + инструкция (node/npm — не наша забота ставить);
- ``system`` → НИКОГДА не ставим сами (apt/brew/choco/права) — только
  инструкция в ``skipped``.

Идемпотентность — на менеджере пакетов (повторный install уже стоящего = no-op).
Функция НИКОГДА не бросает: ошибка любой зависимости → она в ``failed``/
``skipped``, остальные продолжают. Возврат::

    {"installed": [{kind, spec}, ...],
     "skipped":   [{kind, spec, reason, instruction}, ...],
     "failed":    [{kind, spec, reason}, ...]}

Кроссплатформенность: выбор менеджера — через ``shutil.which`` (обёрнут в
``_which`` для мокабельности) + ``python -m pip`` (текущий интерпретатор,
работает на win/mac/linux). subprocess-примитив ``_run`` тоже подменяем в тестах.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Any

# kinds, которые умеем обрабатывать (прочее → skipped с reason unknown-kind).
_KNOWN_KINDS = frozenset({"pip", "npm", "system"})


# --------------------------------------------------------------------------
#  Подменяемые в тестах примитивы окружения
# --------------------------------------------------------------------------
def _which(name: str) -> str | None:
    """Путь к исполняемому ``name`` в PATH или None (обёртка для моков)."""
    return shutil.which(name)


def _pip_available() -> bool:
    """Доступен ли ``pip`` для текущего интерпретатора (``python -m pip``)."""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
            check=False,
        )
        return proc.returncode == 0
    except OSError:
        return False


def _run(cmd: list[str]) -> int:
    """Запустить команду установки, вернуть returncode (не бросает на OSError)."""
    try:
        return subprocess.run(cmd, check=False).returncode
    except OSError:
        return 127


# --------------------------------------------------------------------------
#  Public API
# --------------------------------------------------------------------------
def install_runtime_dependencies(
    deps: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Поставить runtime-зависимости навыка. Никогда не бросает.

    ``deps`` — список ``{"kind": pip|npm|system, "spec": "..."}`` (как в манифесте
    E6 ``runtime_dependencies`` / ``aggregated_runtime_dependencies``). Кривые
    элементы (не-dict, без kind/spec, неизвестный kind) пропускаются/идут в
    skipped. Возвращает отчёт ``{installed, skipped, failed}``.
    """
    report: dict[str, list[dict[str, Any]]] = {
        "installed": [],
        "skipped": [],
        "failed": [],
    }
    if not deps:
        return report

    for raw in deps:
        if not isinstance(raw, dict):
            continue  # мусор — игнор
        kind = raw.get("kind")
        spec = raw.get("spec")
        if not kind or not spec:
            continue  # неполная декларация — игнор
        kind = str(kind)
        spec = str(spec)
        if kind not in _KNOWN_KINDS:
            report["skipped"].append(
                {
                    "kind": kind,
                    "spec": spec,
                    "reason": f"неизвестный kind зависимости: {kind}",
                    "instruction": f"установите {spec} вручную ({kind}).",
                }
            )
            continue
        _dispatch(kind, spec, report)
    return report


def _dispatch(
    kind: str, spec: str, report: dict[str, list[dict[str, Any]]]
) -> None:
    """Маршрутизация одной зависимости по kind в нужную ветку установки."""
    if kind == "pip":
        _install_pip(spec, report)
    elif kind == "npm":
        _install_npm(spec, report)
    else:  # system
        report["skipped"].append(
            {
                "kind": "system",
                "spec": spec,
                "reason": "системная зависимость не ставится автоматически",
                "instruction": (
                    f"установите системный пакет «{spec}» вашим пакетным "
                    "менеджером (apt/brew/choco/...)."
                ),
            }
        )


# --------------------------------------------------------------------------
#  pip: uv pip install → python -m pip install
# --------------------------------------------------------------------------
def _install_pip(spec: str, report: dict[str, list[dict[str, Any]]]) -> None:
    entry = {"kind": "pip", "spec": spec}
    if _which("uv") is not None:
        cmd = ["uv", "pip", "install", spec]
    elif _pip_available():
        cmd = [sys.executable, "-m", "pip", "install", spec]
    else:
        report["skipped"].append(
            {
                **entry,
                "reason": "не найдены ни uv, ни pip",
                "instruction": (
                    f"установите uv (https://docs.astral.sh/uv/) или pip, затем "
                    f"`pip install {spec}`."
                ),
            }
        )
        return
    rc = _run(cmd)
    if rc == 0:
        report["installed"].append(entry)
    else:
        report["failed"].append(
            {**entry, "reason": f"менеджер вернул код {rc}"}
        )


# --------------------------------------------------------------------------
#  npm: npm install -g (только если npm есть)
# --------------------------------------------------------------------------
def _install_npm(spec: str, report: dict[str, list[dict[str, Any]]]) -> None:
    entry = {"kind": "npm", "spec": spec}
    if _which("npm") is None:
        report["skipped"].append(
            {
                **entry,
                "reason": "npm не найден в PATH",
                "instruction": (
                    f"установите Node.js/npm (https://nodejs.org), затем "
                    f"`npm install -g {spec}`."
                ),
            }
        )
        return
    rc = _run(["npm", "install", "-g", spec])
    if rc == 0:
        report["installed"].append(entry)
    else:
        report["failed"].append(
            {**entry, "reason": f"npm вернул код {rc}"}
        )
