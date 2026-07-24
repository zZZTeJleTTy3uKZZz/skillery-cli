"""Установка CLI-пакета навыка через ``uv tool install`` — как install-скрипты навыков.

Приватные CLI-навыки (tg/vk/google-flow/notebooklm/…) — монорепо: доки навыка в
``skills/<name>/``, а сам пакет CLI (``pyproject.toml`` + ``src/``) — в КОРНЕ репо.
Снапшот бэкенда несёт ВЕСЬ репо (хаб собрал его под своим App-токеном), поэтому
устройству git-креды не нужны, а внешние зависимости пакета публичны и резолвятся
анонимно.

Ставим ровно так же, как ``install/install.(ps1|py)`` самого навыка:
``uv tool install --force <корень>`` → изолированный tool-venv + консольная команда
на PATH. Рукодельный shim при этом не нужен — uv создаёт настоящий entry-point из
``[project.scripts]`` пакета. Снятие — ``uv tool uninstall <project.name>``.

Модуль без побочных зависимостей: subprocess-раннер инъектируется (тесты), а вся
логика (парс pyproject, сборка команды, классификация результата) — чистая.
"""
from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

try:  # py>=3.11 (requires-python >=3.11) — есть всегда; guard на всякий случай
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

Runner = Callable[[list[str]], int]


def _debug(message: str, **ctx: object) -> None:
    """DEBUG-трейс шага установки (uv tool install rc). Никогда не валит install.

    Level-gated: на стандартном ERROR ничего не пишется, на debug/verbose виден
    каждый rc. Лениво импортируем логгер, чтобы модуль оставался автономным
    (subprocess-раннер инъектируется) на пути без логирования.
    """
    try:
        from skillery_cli.core.logging_setup import get_logger

        get_logger("install").debug(message, extra={"context": ctx})
    except Exception:  # noqa: BLE001 — логи не должны валить установку пакета
        pass


def read_pyproject_cli(pkg_root: Path) -> dict | None:
    """Прочитать ``{name, scripts}`` из ``pyproject.toml`` корня пакета.

    ``None`` — если pyproject нет или в нём нет ``[project].name`` (значит корень
    не является устанавливаемым пакетом, ставить нечего).
    """
    pp = Path(pkg_root) / "pyproject.toml"
    if not pp.is_file() or tomllib is None:
        return None
    try:
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — битый pyproject → нечего ставить
        return None
    proj = data.get("project") or {}
    name = proj.get("name")
    if not name:
        return None
    scripts = proj.get("scripts") or {}
    return {
        "name": str(name),
        "scripts": {str(k): str(v) for k, v in scripts.items()},
    }


def _default_runner(cmd: list[str]) -> int:
    try:
        return subprocess.run(  # noqa: S603
            cmd, capture_output=True, timeout=900
        ).returncode
    except Exception:  # noqa: BLE001
        return 1


def install_cli_package(
    pkg_root: Path,
    *,
    command_names: list[str] | None = None,
    uv_path: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Runner | None = None,
) -> dict:
    """``uv tool install --force <pkg_root>`` + мягкий self-check команд.

    Ставит пакет из корня (как install-скрипт навыка). ``command_names`` — какие
    команды ожидаем на PATH (если не заданы — берём из ``[project.scripts]``).

    Возвращает report:
      - ``status``: ``"installed"`` (uv tool install rc==0) | ``"error"`` | ``"skipped"``
        (в корне нет устанавливаемого пакета — не наш случай, просто пропускаем);
      - ``package``: имя дистрибутива (для ``uv tool uninstall``) или None;
      - ``commands``: ``[{"name":.., "on_path":bool, "ok":bool}]`` — для PATH-подсказки;
      - ``reason``: причина при error/skipped.

    Инвариант: успех установки определяется КОДОМ ``uv tool install`` (сборка+install
    пакета). Если команда пока не на PATH — это НЕ провал установки, а повод показать
    подсказку про PATH (как для рукодельного shim), поэтому self-check мягкий.
    """
    report: dict = {"status": "skipped", "package": None, "commands": [], "reason": ""}
    info = read_pyproject_cli(pkg_root)
    if info is None:
        report["reason"] = "в корне нет pyproject.toml с [project].name — ставить нечего"
        return report
    report["package"] = info["name"]

    uv = uv_path or which("uv")
    if not uv:
        report["status"] = "error"
        report["reason"] = "не найден uv — нужен для установки CLI-пакета навыка"
        return report

    run = runner or _default_runner
    # Ровно как install/install.py навыка: из корня распакованного снапшота.
    rc = run([uv, "tool", "install", "--force", str(pkg_root)])
    _debug("uv tool install", step="cli_package_uv", package=info["name"], rc=rc)
    if rc != 0:
        report["status"] = "error"
        report["reason"] = f"uv tool install вернул код {rc}"
        return report

    report["status"] = "installed"
    names = command_names or list(info["scripts"].keys())
    for name in names:
        exe = which(name)
        on_path = exe is not None
        # Проверяем запуск ТОЛЬКО если бинарь уже на PATH — иначе PATH просто не
        # обновился в текущем процессе (не провал установки).
        ok = bool(on_path) and (run([exe, "--version"]) == 0 or run([exe, "--help"]) == 0)
        _debug("cli command self-check", step="cli_package_check",
               command=name, on_path=on_path, ok=ok)
        report["commands"].append({"name": name, "on_path": on_path, "ok": ok})
    return report


def uninstall_cli_package(
    package_name: str,
    *,
    uv_path: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Runner | None = None,
) -> bool:
    """``uv tool uninstall <package_name>``. True при успехе (или нечего снимать)."""
    if not package_name:
        return False
    uv = uv_path or which("uv")
    if not uv:
        return False
    run = runner or _default_runner
    return run([uv, "tool", "uninstall", package_name]) == 0
