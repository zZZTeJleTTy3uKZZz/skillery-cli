"""``subprocess.run``-совместимый раннер поверх :mod:`librarykit.proc` (#1144).

ЗАЧЕМ ОТДЕЛЬНЫЙ АДАПТЕР, А НЕ ПРЯМОЙ ``proc.run`` В КАЖДОМ МОДУЛЕ. В CLI есть
модули с ИНЪЕКТИРУЕМЫМ раннером провайдерских CLI (``gh``/``glab``):
``core.repo_connect``, ``core.webhook_setup``, ``commands.webhook``. Их контракт —
``CommandRunner = Callable[..., CompletedProcess]``, и тесты подставляют вместо
раннера свои двойники с этой же сигнатурой (``capture_output=``, ``text=``,
``input=``, ``check=``). Прямой ``proc.run`` сигнатуру не повторяет (``input``
там нет вовсе — ``stdin`` всегда ``DEVNULL``), поэтому переход на него означал бы
переписывание контракта и всех двойников. Здесь — ОДИН адаптер, который держит
привычную форму снаружи, а внутри отдаёт запуск киту.

Что даёт кит и чего не было раньше:

* на win32 ВСЕГДА ``CREATE_NO_WINDOW`` — ``gh``/``glab``, вызванные из
  DETACHED-демона, больше не мигают консольным окном;
* ``timeout`` обязателен — без него зависший провайдерский CLI вешал команду
  навсегда (нажать Ctrl-C фоновому процессу некому).

Единственная причина импорта ``subprocess`` здесь — КОНСТАНТА ``PIPE`` для
передачи тела запроса на stdin (``gh api --input -``). Вызовов ``subprocess.*``
в модуле нет: их запрещает гейт ``tests/test_no_raw_subprocess.py``.
"""
from __future__ import annotations

import subprocess  # noqa: F401 — только константа PIPE; запуск идёт через proc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from librarykit.proc import popen as _popen
from librarykit.proc import run as _run

#: Дефолт на случай, если вызывающий таймаут не передал. Провайдерские CLI
#: (``gh``/``glab``) — это один HTTP-вызов: минута с огромным запасом.
DEFAULT_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class CommandResult:
    """Минимальная форма ``subprocess.CompletedProcess`` — ровно то, что читают
    вызывающие (``returncode`` / ``stdout`` / ``stderr``).

    ``timed_out`` — доп. поле кита: убитый по таймауту процесс отдаёт
    ``returncode = -1``, и по одному коду его не отличить от «упал сам».
    """

    returncode: int
    stdout: Any
    stderr: Any
    timed_out: bool = False


def run_command(
    args: Sequence[Any],
    *,
    capture_output: bool = True,
    text: bool = True,
    timeout: float | None = None,
    check: bool = False,
    input: str | bytes | None = None,  # noqa: A002 — имя из контракта subprocess
    cwd: Any | None = None,
    env: Mapping[str, str] | None = None,
    log: Any | None = None,
) -> CommandResult:
    """Запустить команду, вернув ``CompletedProcess``-подобный результат.

    ``capture_output`` принимается для совместимости сигнатуры и игнорируется:
    :func:`librarykit.proc.run` захватывает потоки ВСЕГДА (мусор дочернего
    процесса не должен лететь в терминал пользователя).

    ``input`` не покрывается контрактом ``proc.run`` (там ``stdin=DEVNULL``
    всегда), поэтому тело подаётся через :func:`librarykit.proc.popen` с
    ``stdin=PIPE`` — политика окон/флагов при этом та же самая, вторая
    реализация «как правильно на Windows» не заводится.
    """
    del capture_output
    effective_timeout = DEFAULT_TIMEOUT_S if timeout is None else float(timeout)

    if input is None:
        result = _run(
            args,
            timeout=effective_timeout,
            text=text,
            cwd=cwd,
            env=env,
            log=log,
            check=check,
        )
        return CommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.timed_out,
        )

    empty: Any = "" if text else b""
    proc = _popen(
        args,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
    )
    try:
        out, err = proc.communicate(input, timeout=effective_timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        # Иначе процесс остаётся жить и держать пайпы: демону его потом не найти.
        proc.kill()
        out, err = proc.communicate()
        timed_out = True
    result = CommandResult(
        returncode=-1 if timed_out else proc.returncode,
        stdout=out if out is not None else empty,
        stderr=err if err is not None else empty,
        timed_out=timed_out,
    )
    if check and (result.returncode != 0 or timed_out):
        from librarykit.errors import ProcessError

        raise ProcessError(
            f"команда не выполнена (код {result.returncode}): {args[0]}",
            cmd=tuple(str(a) for a in args),
            returncode=result.returncode,
            stderr="",
            timed_out=timed_out,
        )
    return result


__all__ = ["DEFAULT_TIMEOUT_S", "CommandResult", "run_command"]
