"""Гейт: в CLI НЕТ прямых вызовов ``subprocess`` (#1144).

Первопричина с прода. Демон Skillery стартует DETACHED (``DETACHED_PROCESS |
CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW``) — собственной консоли у него НЕТ.
На Windows КАЖДЫЙ консольный ребёнок (``uv``, ``git``, ``powershell``,
``schtasks``, ``gh``/``glab``), запущенный без ``CREATE_NO_WINDOW``, получает от
системы СВОЮ консоль: владелец видел чёрные окна ``uv.EXE`` поверх браузера,
пока демон в фоне ставил навык.

Главное заблуждение, из-за которого дефект жил: ``capture_output=True`` окно НЕ
подавляет. Перенаправление stdio (``STARTUPINFO``) и аллокация консоли
(``creationflags``) — независимые механизмы Win32. Второй класс дефектов —
запуск БЕЗ таймаута: фоновому демону некому нажать Ctrl-C, зависший ``uv``/
``gitleaks`` висит вечно.

Единая политика живёт в ``librarykit.proc`` (win32 → ``CREATE_NO_WINDOW``
всегда, ``timeout`` обязателен, ``stdin=DEVNULL``, маскировка секретов в логе).
Дублировать её в каждом вызове нельзя — так флаг и оказался ровно в ОДНОМ месте
из десяти. Поэтому здесь не «рекомендация в ревью», а исполняемый гейт: любой
новый ``subprocess.run|Popen|call|check_output|check_call`` в
``src/skillery_cli/**`` роняет тесты.

⚠️ Гейт смотрит на ВЫЗОВЫ в AST, а не на текст: ``subprocess.PIPE`` /
``subprocess.DEVNULL`` (константы) и ``subprocess.SubprocessError`` (тип
исключения) разрешены, как и вызовы ``subprocess.run`` ВНУТРИ строковых
шаблонов (``templates/__init__.py`` генерирует stdlib-only установщики навыков —
у сгенерированного скрипта нашего кита в окружении нет).
"""
from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent.parent / "src" / "skillery_cli"

#: Вызовы, которые обязаны идти через ``librarykit.proc``.
_FORBIDDEN = frozenset({"run", "Popen", "call", "check_output", "check_call"})

#: ЯВНЫЙ allowlist модулей, которым прямой ``subprocess`` разрешён. Держится
#: минимальным, и КАЖДАЯ строка обоснована — молча сюда ничего не попадает.
_ALLOWLIST: dict[str, str] = {
    # Worker апгрейда запускается БАЗОВЫМ интерпретатором (`sys.base_prefix`,
    # см. `_upgrade_launcher`), а НЕ python'ом tool-venv: иначе он держал бы
    # каталог `…/tools/skillery-cli/Scripts/`, и `uv` не смог бы его перезаписать
    # («os error 5»). Из базового интерпретатора `librarykit` НЕ импортируется —
    # его там нет. Плюс worker переживает физическую ЗАМЕНУ пакета и потому не
    # имеет права импортировать вообще ничего, кроме stdlib. Свою политику окон
    # он несёт сам (`_no_window_kwargs`: CREATE_NO_WINDOW + DEVNULL) и таймауты
    # проставляет во всех вызовах.
    "_upgrade_worker.py": "автономный worker вне tool-venv: доступен только stdlib",
}


def _module_paths() -> list[Path]:
    return sorted(p for p in _PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def _raw_subprocess_calls(tree: ast.AST) -> list[str]:
    """Имена вызовов вида ``subprocess.<forbidden>(...)`` в модуле.

    Учитываются и алиасы модуля (``import subprocess as _sp`` → ``_sp.run``):
    переименование не должно быть лазейкой мимо гейта.
    """
    aliases = {"subprocess"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess" and alias.asname:
                    aliases.add(alias.asname)
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr in _FORBIDDEN
            and isinstance(func.value, ast.Name)
            and func.value.id in aliases
        ):
            found.append(f"{func.value.id}.{func.attr} (строка {node.lineno})")
    return found


def test_no_raw_subprocess_calls_in_cli() -> None:
    offenders: dict[str, list[str]] = {}
    for path in _module_paths():
        rel = path.relative_to(_PACKAGE).as_posix()
        if rel in _ALLOWLIST:
            continue
        calls = _raw_subprocess_calls(ast.parse(path.read_text(encoding="utf-8")))
        if calls:
            offenders[rel] = calls

    assert not offenders, (
        "прямой запуск подпроцесса в CLI — используй librarykit.proc.run "
        "(на Windows он ВСЕГДА ставит CREATE_NO_WINDOW и требует timeout; "
        "capture_output=True консольное окно НЕ подавляет): "
        + "; ".join(f"{mod}: {', '.join(calls)}" for mod, calls in sorted(offenders.items()))
    )


def test_gate_detects_planted_violation() -> None:
    """Сам гейт не «зелёный по недосмотру»: подложенный вызов он видит."""
    assert _raw_subprocess_calls(ast.parse("import subprocess\nsubprocess.run(['git'])\n"))
    # …в том числе под алиасом.
    assert _raw_subprocess_calls(
        ast.parse("import subprocess as sp\nsp.Popen(['git'])\n")
    )


def test_gate_allows_constants_and_exception_types() -> None:
    """Константы и типы исключений — не запуск процесса, ложных срабатываний нет."""
    src = (
        "import subprocess\n"
        "kw = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.PIPE}\n"
        "try:\n    pass\nexcept subprocess.SubprocessError:\n    pass\n"
    )
    assert not _raw_subprocess_calls(ast.parse(src))


def test_allowlist_entries_exist_and_are_justified() -> None:
    """Allowlist не «протухает»: каждый путь существует и несёт причину."""
    for rel, reason in _ALLOWLIST.items():
        assert (_PACKAGE / rel).is_file(), f"allowlist указывает на несуществующий {rel}"
        assert reason.strip(), f"запись allowlist {rel} без обоснования"
