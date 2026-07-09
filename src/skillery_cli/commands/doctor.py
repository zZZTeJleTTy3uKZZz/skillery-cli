"""``skillery doctor`` — self-check окружения CLI (образец reverse-factory).

Always-on (как ``status``): запускается без логина, печатает pass/warn/fail по
ключевым предпосылкам и завершается нонзеро при критических провалах. ``--strict``
ужесточает: WARN тоже становится провалом (для CI). JSON-режим (``--json`` /
config) даёт машинную структуру для AI-агентов и скриптов.

Проверки:
1. **Python >= 3.11** — fail если ниже (CLI не запустится);
2. **Менеджер пакетов** — uv (предпочтительно) / pip; warn если только pip,
   fail если ни одного (нечем доустанавливать runtime-deps навыков);
3. **Агент** — какой ИИ-агент детектится (claude_code/codex/...); warn если ни
   один не установлен (install сработает, но скилл некому подхватить);
4. **Hub login** — залогинен ли (pass) или аноним (warn: оффлайн-режим);
5. **PATH-стор** — bin-каталог CLI-шимов (``~/.skillery/bin``) в PATH? warn
   если нет (CLI установленных навыков не позовутся → ``doctor`` чинит);
6. **clikit** — доступен ли пакет (опц.; warn если нет — нужен tooling-навыкам
   с встроенным CLI).

Движок (``run_checks`` + ``_probe_*``) — чистые функции, тестируются точечно;
``cmd_doctor`` только печатает и выставляет exit-код.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass

import typer
from rich.console import Console
from rich.table import Table

from skillery_cli.config import ClientConfig
from skillery_cli.core import path_store
from skillery_cli.core.agents import detect_agent, get_target
from skillery_cli.output import emit_data, is_json

console = Console()

MIN_PYTHON: tuple[int, int] = (3, 11)


# --------------------------------------------------------------------------
#  Result
# --------------------------------------------------------------------------
@dataclass
class Result:
    """Один пункт проверки. level: 'pass' | 'warn' | 'fail'."""

    name: str
    level: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "level": self.level, "detail": self.detail}


def ok(name: str, detail: str = "") -> Result:
    return Result(name, "pass", detail)


def warn(name: str, detail: str = "") -> Result:
    return Result(name, "warn", detail)


def fail(name: str, detail: str = "") -> Result:
    return Result(name, "fail", detail)


# --------------------------------------------------------------------------
#  probes (каждая мокается в тестах для pass/warn/fail)
# --------------------------------------------------------------------------
def _probe_python() -> Result:
    want = ".".join(str(x) for x in MIN_PYTHON)
    cur = sys.version.split()[0]
    if sys.version_info >= MIN_PYTHON:
        return ok(f"Python >= {want}", f"найден {cur}")
    return fail(f"Python >= {want}", f"текущий {cur} — обнови интерпретатор")


def _pip_available() -> bool:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            capture_output=True,
            check=False,
        )
        return proc.returncode == 0
    except OSError:
        return False


def _probe_package_manager() -> Result:
    if shutil.which("uv"):
        return ok("Менеджер пакетов", "uv найден (быстрый, рекомендуется)")
    if _pip_available():
        return warn(
            "Менеджер пакетов",
            "uv не найден — будет использован pip (медленнее). "
            "Рекомендуется uv: https://docs.astral.sh/uv/",
        )
    return fail(
        "Менеджер пакетов",
        "нет ни uv, ни pip — runtime-зависимости навыков не доустановить. "
        "Поставь uv или pip.",
    )


def _probe_agent(cfg: ClientConfig) -> Result:
    """Какой ИИ-агент будет таргетом установки навыков."""
    name = cfg.agent or detect_agent()
    try:
        installed = get_target(name).exists()
    except Exception:  # noqa: BLE001 — неизвестный агент в конфиге не валит doctor
        return warn("Агент", f"агент '{name}' из конфига не распознан")
    if installed:
        return ok("Агент", f"{name} (установлен)")
    return warn(
        "Агент",
        f"{name} (каталог агента не найден — install сработает, но скиллы "
        "подхватит только после установки самого агента)",
    )


def _probe_login(cfg: ClientConfig) -> Result:
    if cfg.is_logged_in():
        return ok("Hub login", f"вошли как {cfg.user_email}")
    return warn(
        "Hub login",
        "не авторизован — доступны автономная установка (--path/--from-git) и "
        "локальный стор; для hub-каталога: skillery login",
    )


def _probe_path_store() -> Result:
    """bin-каталог CLI-шимов навыков в PATH?"""
    target = path_store.bin_dir()
    if path_store._already_on_path(target):
        return ok("PATH-стор", f"{target} в PATH")
    return warn(
        "PATH-стор",
        f"{target} НЕ в PATH — CLI установленных навыков не позовутся из "
        "произвольной директории. Почини: skillery doctor --fix-path "
        "(или добавь каталог в PATH вручную)",
    )


def _probe_clikit() -> Result:
    """clikit — опциональная зависимость (нужен tooling-навыкам с CLI)."""
    try:
        spec = importlib.util.find_spec("clikit")
    except (ImportError, ValueError):
        spec = None
    if spec is not None:
        return ok("clikit", "доступен")
    return warn(
        "clikit",
        "пакет clikit не установлен (опционально — нужен навыкам с встроенным "
        "CLI на clikit; рантайм skillery работает без него)",
    )


# --------------------------------------------------------------------------
#  движок
# --------------------------------------------------------------------------
def run_checks(cfg: ClientConfig) -> list[Result]:
    """Прогнать все проверки. Возвращает список Result (без печати)."""
    return [
        _probe_python(),
        _probe_package_manager(),
        _probe_agent(cfg),
        _probe_login(cfg),
        _probe_path_store(),
        _probe_clikit(),
    ]


def _summary(results: list[Result]) -> dict[str, int]:
    summary = {"pass": 0, "warn": 0, "fail": 0}
    for r in results:
        summary[r.level] = summary.get(r.level, 0) + 1
    return summary


# --------------------------------------------------------------------------
#  команда
# --------------------------------------------------------------------------
def cmd_doctor(
    strict: bool = typer.Option(
        False, "--strict", help="Считать WARN провалом (для CI)."
    ),
    fix_path: bool = typer.Option(
        False,
        "--fix-path",
        help="Починить PATH: добавить bin-каталог стора в PATH (без admin).",
    ),
) -> None:
    """Проверка окружения: Python/uv/agent/login/PATH/clikit (pass/warn/fail).

    Завершается нонзеро при критических провалах; ``--strict`` — также при
    предупреждениях. ``--fix-path`` чинит PATH-стор перед проверкой. ``--json``
    даёт машинную структуру.
    """
    strict = _unwrap_bool(strict, False)
    fix_path = _unwrap_bool(fix_path, False)
    cfg = ClientConfig.load()

    path_fix: dict[str, str] | None = None
    if fix_path:
        path_fix = path_store.ensure_on_path()

    results = run_checks(cfg)
    summary = _summary(results)

    has_fail = summary["fail"] > 0
    has_warn = summary["warn"] > 0
    ok_env = not has_fail and not (strict and has_warn)

    payload = {
        "checks": [r.as_dict() for r in results],
        "summary": summary,
        "ok": ok_env,
        "strict": strict,
    }
    if path_fix is not None:
        payload["path_fix"] = path_fix

    def _render(p: dict) -> None:
        if path_fix is not None:
            st = path_fix.get("status")
            if st == "added":
                console.print(
                    f"[green]✓[/] PATH: добавлен {path_fix['bin_dir']} "
                    "(перезапусти терминал, чтобы изменения вступили в силу)"
                )
            elif st == "already":
                console.print(f"[green]✓[/] PATH: {path_fix['bin_dir']} уже в PATH")
            else:  # manual-needed
                console.print(
                    f"[yellow]![/] PATH не удалось обновить автоматически. "
                    f"Выполни вручную: {path_fix.get('instruction', '')}"
                )
        table = Table(title="skillery · doctor")
        table.add_column("проверка", style="bold")
        table.add_column("статус")
        table.add_column("детали", overflow="fold")
        marks = {
            "pass": "[green][ OK ][/]",
            "warn": "[yellow][WARN][/]",
            "fail": "[red][FAIL][/]",
        }
        for r in results:
            table.add_row(r.name, marks.get(r.level, r.level), r.detail)
        console.print(table)
        s = p["summary"]
        console.print(
            f"Итог: [green]{s['pass']} OK[/] · [yellow]{s['warn']} WARN[/] · "
            f"[red]{s['fail']} FAIL[/]"
        )
        if has_fail:
            console.print("[red]Есть критические провалы — почини их перед работой.[/]")
        elif has_warn and strict:
            console.print("[yellow]STRICT: предупреждения считаются провалом.[/]")
        else:
            console.print("[green]Окружение пригодно к работе.[/]")

    emit_data(payload, text_renderer=_render)

    if not ok_env:
        raise typer.Exit(1)


def _unwrap_bool(value: object, fallback: bool) -> bool:
    """Снять typer-sentinel при прямом вызове из кода/тестов (паттерн scaffold)."""
    if isinstance(value, typer.models.OptionInfo):
        default = getattr(value, "default", fallback)
        return bool(default) if isinstance(default, bool) else fallback
    return bool(value)


def register(app: typer.Typer) -> None:
    """Регистрирует always-on команду ``doctor`` (self-check окружения)."""
    app.command(name="doctor")(cmd_doctor)
