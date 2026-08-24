"""#2264 — параллельное обновление refresh-токена не убивает сессию.

Прод (устройство ``DESKTOP-C1CK65V``, 13.08): три ротации ОДНОГО refresh-токена
за три секунды, следом ``revoked_reason='reuse_detected'`` — и 401 на всё
(``/auth/refresh``, ``/telemetry/events``, ``/devices/*/tasks``), телеметрия
молчала 11 суток. Обновляли одновременно демон и команда пользователя: разные
процессы, общий токен, никакой сериализации.

Здесь фиксируются обе половины лечения:

1. **межпроцессный лок** (``core.token_lock``) — проверяется НАСТОЯЩИМИ
   процессами, а не мокой: два ``python``-процесса берут лок на одном файле, их
   интервалы владения не пересекаются;
2. **перечитывание токена ПОД ЛОКОМ** — без него лок лишь упорядочил бы гонку:
   второй процесс дождался бы своей очереди и всё равно отправил уже
   провёрнутый токен. Сервер такой повтор законно считает кражей.

Сервер в тестах ведёт себя как настоящий: принимает только ТЕКУЩИЙ токен,
предъявление устаревшего = ``reuse_detected`` и смерть сессии.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap

import pytest

from skillery_cli import __main__ as m
from skillery_cli.core import token_lock

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _reset_reason():
    m._REFRESH_FAILURE["reason"] = None
    yield
    m._REFRESH_FAILURE["reason"] = None


def _cfg():
    from skillery_cli.config import ClientConfig

    cfg = ClientConfig(base_url="http://localhost:8000")
    cfg.user_email = "u@e.io"
    return cfg


class _FakeHub:
    """Сервер с настоящей семантикой ротации refresh-токена.

    Принимает только текущий токен; предъявление уже провёрнутого — признак
    кражи: сессия отзывается целиком и дальше 401 навсегда.
    """

    def __init__(self) -> None:
        self.current = "R0"
        self.seq = 0
        self.calls: list[str] = []
        self.session_revoked = False

    async def rotate(self, token: str) -> dict[str, str]:
        self.calls.append(token)
        # Небольшая пауза — окно, в котором сосед успевает влезть, если его
        # никто не сериализует (без неё гонка «не успевает» случиться).
        await asyncio.sleep(0.05)
        if self.session_revoked:
            raise _api_error("UNAUTHORIZED")
        if token != self.current:
            self.session_revoked = True  # reuse_detected → вся сессия мертва
            raise _api_error("UNAUTHORIZED")
        self.seq += 1
        self.current = f"R{self.seq}"
        return {"access_token": f"A{self.seq}", "refresh_token": self.current}


def _api_error(code: str):
    from skillery_cli.core.transport import ApiError

    return ApiError(status_code=401, code=code, message="reuse detected")


@pytest.fixture()
def _wired(monkeypatch, tmp_path):
    """Проводка: общее «хранилище» токенов + фейковый хаб + изолированный лок."""
    hub = _FakeHub()
    store = {"access": "A0", "refresh": "R0"}

    monkeypatch.setenv(
        "SKILLERY_TOKEN_REFRESH_LOCK", str(tmp_path / "token-refresh.lock")
    )
    monkeypatch.setattr(
        m, "load_tokens", lambda _e: (store["access"], store["refresh"])
    )

    def _save(_e: str, access: str, refresh: str) -> None:
        store["access"], store["refresh"] = access, refresh

    monkeypatch.setattr(m, "save_tokens", _save)
    monkeypatch.setattr(m, "populate_from_jwt", lambda cfg, tok: None)

    class _Client:
        def __init__(self, **_kw) -> None:
            pass

        async def refresh(self, token: str) -> dict[str, str]:
            return await hub.rotate(token)

        async def close(self) -> None:
            return None

    monkeypatch.setattr(m, "HubClient", _Client)
    return hub, store


async def test_parallel_refresh_keeps_session_alive(_wired, monkeypatch) -> None:
    """Два конкурирующих обновления одного токена — сессия ВЫЖИВАЕТ.

    Это и есть прод-сценарий: демон и команда пользователя упёрлись в 401
    одновременно. Без сериализации второй уходит на сервер со СТАРЫМ токеном,
    сервер видит повтор и рубит сессию — устройство мертво.
    """
    hub, store = _wired
    cfg = _cfg()
    monkeypatch.setattr(type(cfg), "save", lambda self: None)
    cb = m._make_refresh_callback(cfg)

    first, second = await asyncio.gather(cb(), cb())  # type: ignore[operator]

    assert not hub.session_revoked, (
        "сервер счёл параллельное обновление кражей — сессия устройства убита"
    )
    assert first is not None and second is not None, "кто-то остался без пары"
    # Отправлен РОВНО ОДИН запрос на ротацию: второй, дождавшись лока,
    # перечитал хранилище и увидел уже обновлённую пару.
    assert hub.calls == ["R0"], hub.calls
    # Оба получили одну и ту же — АКТУАЛЬНУЮ — пару.
    assert first == second == (store["access"], store["refresh"])


async def test_refresh_rereads_token_under_lock(_wired, monkeypatch) -> None:
    """Под локом токен ПЕРЕЧИТЫВАЕТСЯ, свой устаревший не отправляется.

    Отдельно от гонки: имитируем соседа, который обновил пару, пока мы ждали
    лок. Уйти на сервер со своим (прочитанным до лока) токеном нельзя.
    """
    hub, store = _wired
    cfg = _cfg()
    monkeypatch.setattr(type(cfg), "save", lambda self: None)

    real_acquire = m._RefreshGuard.acquire

    def _acquire_then_neighbour(self) -> bool:
        got = real_acquire(self)
        # «Сосед» уже провернул токен, пока мы стояли в очереди.
        store["access"], store["refresh"] = "A9", "R9"
        hub.current = "R9"
        return got

    monkeypatch.setattr(m._RefreshGuard, "acquire", _acquire_then_neighbour)

    result = await m._make_refresh_callback(cfg)()  # type: ignore[operator]

    assert result == ("A9", "R9")
    assert hub.calls == [], "устаревший токен всё-таки ушёл на сервер"
    assert not hub.session_revoked


def _child_env(lock_file) -> dict[str, str]:
    """Окружение дочернего процесса: изолированный файл лока + импортируемый пакет."""
    import os
    import pathlib

    pkg_root = pathlib.Path(m.__file__).parents[1]  # …/src
    return {
        **dict(os.environ),
        "SKILLERY_TOKEN_REFRESH_LOCK": str(lock_file),
        "PYTHONPATH": os.pathsep.join(
            [str(pkg_root), os.environ.get("PYTHONPATH", "")]
        ).strip(os.pathsep),
    }


def test_lock_is_exclusive_across_real_processes(tmp_path) -> None:
    """Лок межпроцессный: интервалы владения двух ПРОЦЕССОВ не пересекаются.

    Внутрипроцессный ``asyncio.Lock``/``threading.Lock`` этот тест не прошёл бы
    — а гонка на проде была именно между процессами (демон и команда).
    """
    lock_file = tmp_path / "token-refresh.lock"
    src = tmp_path / "hold.py"
    src.write_text(
        textwrap.dedent(
            """
            import json, os, sys, time
            from skillery_cli.core import token_lock

            with token_lock.token_refresh_lock(timeout=30.0) as ok:
                start = time.monotonic()
                time.sleep(0.4)   # держим лок — сосед обязан ждать
                end = time.monotonic()
            print(json.dumps({"ok": ok, "start": start, "end": end}))
            """
        ),
        encoding="utf-8",
    )
    env = _child_env(lock_file)
    procs = [
        subprocess.Popen(  # запускаем свой же интерпретатор
            [sys.executable, str(src)],
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        for _ in range(2)
    ]
    spans = []
    for proc in procs:
        out, _ = proc.communicate(timeout=90)
        assert proc.returncode == 0, out
        spans.append(json.loads(out.strip().splitlines()[-1]))

    assert all(s["ok"] for s in spans), "лок не взят одним из процессов"
    a, b = sorted(spans, key=lambda s: s["start"])
    # Часы monotonic у процессов независимы, поэтому сравниваем не отметки, а
    # ДЛИТЕЛЬНОСТИ: пересечение означало бы, что оба владели локом разом.
    total = max(s["end"] for s in spans) - min(s["start"] for s in spans)
    assert total >= 0.8 - 0.05, (
        "владения наложились: суммарное время меньше двух удержаний"
    )
    assert a["end"] - a["start"] >= 0.35 and b["end"] - b["start"] >= 0.35


def test_lock_survives_dead_owner(tmp_path) -> None:
    """Убитый владелец не вешает лок навсегда — ядро закрывает дескриптор."""
    lock_file = tmp_path / "token-refresh.lock"
    src = tmp_path / "hang.py"
    src.write_text(
        textwrap.dedent(
            """
            import sys, time
            from skillery_cli.core import token_lock
            with token_lock.token_refresh_lock(timeout=30.0):
                print("held", flush=True)
                time.sleep(60)
            """
        ),
        encoding="utf-8",
    )
    env = _child_env(lock_file)
    victim = subprocess.Popen(  # запускаем свой же интерпретатор
        [sys.executable, str(src)], stdout=subprocess.PIPE, text=True, env=env
    )
    try:
        assert victim.stdout is not None
        assert victim.stdout.readline().strip() == "held"
        victim.kill()  # жёсткое убийство, лок не отпущен по-хорошему
        victim.wait(timeout=30)
    finally:
        if victim.poll() is None:  # pragma: no cover — страховка
            victim.kill()

    import os

    os.environ["SKILLERY_TOKEN_REFRESH_LOCK"] = str(lock_file)
    try:
        with token_lock.token_refresh_lock(timeout=10.0) as acquired:
            assert acquired, "лок остался занят мёртвым владельцем"
    finally:
        os.environ.pop("SKILLERY_TOKEN_REFRESH_LOCK", None)
