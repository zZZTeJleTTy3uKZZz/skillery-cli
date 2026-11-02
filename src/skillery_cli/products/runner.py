"""Инъецируемый запуск внешних команд (``uv``, ``claude``, установщик ОС).

Обработчики не зовут ``subprocess`` напрямую: политика окон/таймаутов живёт в
``librarykit.proc``, а тесты подменяют раннер целиком и не запускают ничего
настоящего. Это же делает установку проверяемой: «что именно мы попросили у
uv/claude» — данные, а не побочный эффект.
"""
from __future__ import annotations

import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from librarykit import proc

DEFAULT_TIMEOUT = 600.0


@dataclass(frozen=True)
class CommandResult:
    """Итог внешней команды: код, потоки, факт таймаута."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class CommandRunner(Protocol):
    """Минимальный контракт запуска: реализует и настоящий, и фейковый раннер."""

    def run(self, argv: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT) -> CommandResult:
        ...

    def which(self, command: str) -> str | None:
        ...


class CommandError(RuntimeError):
    """Внешняя команда завершилась неуспешно."""

    def __init__(self, result: CommandResult) -> None:
        self.result = result
        reason = "таймаут" if result.timed_out else f"код {result.returncode}"
        detail = (result.stderr or result.stdout).strip().splitlines()
        tail = f": {detail[-1]}" if detail else ""
        super().__init__(f"команда {' '.join(result.argv)} — {reason}{tail}")


@dataclass
class SubprocessRunner:
    """Настоящий запуск через ``librarykit.proc`` (единая политика окон/таймаутов)."""

    default_timeout: float = DEFAULT_TIMEOUT
    _runs: list[CommandResult] = field(default_factory=list, repr=False)

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        result = proc.run(
            list(argv),
            timeout=self.default_timeout if timeout is None else timeout,
        )
        mapped = CommandResult(
            argv=tuple(str(item) for item in argv),
            returncode=int(result.returncode),
            stdout=str(result.stdout or ""),
            stderr=str(result.stderr or ""),
            timed_out=bool(getattr(result, "timed_out", False)),
        )
        self._runs.append(mapped)
        return mapped

    def which(self, command: str) -> str | None:
        return shutil.which(command)


class FakeRunner:
    """Раннер для тестов: записывает команды и отдаёт заранее заданные ответы."""

    def __init__(
        self,
        *,
        results: dict[tuple[str, ...], CommandResult] | None = None,
        default: CommandResult | None = None,
        paths: dict[str, str] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self._results = results or {}
        self._default = default or CommandResult(argv=(), returncode=0)
        self._paths = paths or {}

    def run(self, argv: Sequence[str], *, timeout: float | None = None) -> CommandResult:
        key = tuple(str(item) for item in argv)
        self.calls.append(key)
        return self._results.get(key, self._default)

    def which(self, command: str) -> str | None:
        return self._paths.get(command)
