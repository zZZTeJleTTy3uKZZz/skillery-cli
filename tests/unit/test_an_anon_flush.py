"""АНОНИМНАЯ отправка аналитики — семантика, которую переезд не имеет права съесть.

``POST /events`` принимает анонимно (backend ``_optional_claims``), и это
единственная причина, по которой аналитика НЕ едет в ``/telemetry/batch``
(там Bearer обязателен). Событие установки рождается и на машине, где никто не
логинился (``install --path`` / ``--from-git``), и при протухшей сессии — именно
на них стоит метрика активаций. Отправь мы их в ``/telemetry/batch``, они бы
висели в очереди вечно: 401 — статус ПОВТОРЯЕМЫЙ, значит конверты не ушли бы
никогда, а метрика молча исчезла бы.

Закрепляем:
- ``event flush`` без токена → отправка БЕЗ Bearer, не exit(1)
  SESSION_EXPIRED/NOT_LOGGED_IN;
- воркер: токен есть, но 401 (протух) → ретрай ТЕМ ЖЕ батчем без Bearer,
  события уезжают, очередь пустеет;
- деградировать некуда (токена и не было) → ретрая нет, конверты остаются
  (без зацикливания);
- залипший ``/telemetry/batch`` (нет Bearer) НЕ душит анонимную аналитику.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from telemetrykit import outbox

from skillery_cli import output as out_mod
from skillery_cli.commands import _common
from skillery_cli.commands import event as event_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core import analytics_sync
from skillery_cli.core import outbox_worker as ow
from skillery_cli.core.transport import ApiError
from skillery_cli.daemon.event_sender import OutboxSender


def _pending() -> int:
    return analytics_sync.pending_count()


# ============================================================
# cmd_event_flush — без токена шлёт anonymous, не падает
# ============================================================
def test_flush_without_token_sends_anonymous(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Нет user_email/токена → flush строит client без access_token и
    отправляет (accepted), без exit(1)."""
    monkeypatch.setattr(out_mod, "_mode", "json")
    analytics_sync.track("skill.enable", resource_type="skill", resource_id="1")

    cfg = ClientConfig(base_url="http://x")  # НЕ залогинен (user_email=None)
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    seen_tokens: list = []
    fake_client = MagicMock()

    async def _ingest(events, *, idempotency_key):
        return {"accepted": len(events)}

    async def _close() -> None:
        return None

    fake_client.ingest_events = _ingest
    fake_client.close = _close
    fake_client.send_telemetry_batch = None

    def _make(cfg_, access):
        seen_tokens.append(access)
        return fake_client

    monkeypatch.setattr(_common, "make_client", _make)

    # НЕ должно бросить typer.Exit
    event_mod.cmd_event_flush()

    # Очередь опустела (события ушли как anonymous)
    assert _pending() == 0
    # client построен с пустым/None токеном (anonymous)
    assert seen_tokens and not seen_tokens[0]


def test_flush_with_expired_token_does_not_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Залогинен, но load_tokens вернул None (протух) → anonymous, не exit."""
    monkeypatch.setattr(out_mod, "_mode", "json")
    analytics_sync.track("skill.enable", resource_type="skill", resource_id="1")

    cfg = ClientConfig(base_url="http://x", user_email="u@e.io")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    # keyring/файл пусты → токена нет. ВАЖНО: cmd_event_flush делает локальный
    # импорт `from skillery_cli.config import load_tokens`, поэтому патчить
    # надо ИСТОЧНИК (config), а не _common — иначе мок мимо, и читается реальный
    # keyring пользователя (тест «флейчил» в зависимости от сохранённой сессии).
    monkeypatch.setattr(
        "skillery_cli.config.load_tokens", lambda email: (None, None)
    )

    seen_tokens: list = []
    fake_client = MagicMock()

    async def _ingest(events, *, idempotency_key):
        return {"accepted": len(events)}

    async def _close() -> None:
        return None

    fake_client.ingest_events = _ingest
    fake_client.close = _close
    fake_client.send_telemetry_batch = None
    monkeypatch.setattr(
        _common, "make_client",
        lambda cfg_, access: (seen_tokens.append(access), fake_client)[1],
    )

    event_mod.cmd_event_flush()
    assert _pending() == 0
    assert seen_tokens and not seen_tokens[0]


# ============================================================
# Воркер — 401 при наличии токена → ретрай anonymous
# ============================================================
# Контракт фабрики: ``factory(anonymous=False) -> client | None``.
# ``anonymous=True`` строит client БЕЗ Bearer; возвращает ``None``, если
# деградировать некуда (токена и так не было) — тогда ретрая нет.
class _Client:
    def __init__(self, token: str | None, calls: list) -> None:
        self.token = token
        self._calls = calls
        self.send_telemetry_batch = None

    async def ingest_events(self, events, *, idempotency_key):
        self._calls.append(self.token)
        if self.token:  # токен есть → сервер отверг (протух)
            raise ApiError(
                status_code=401, code="SESSION_EXPIRED",
                message="expired", details={},
            )
        return {"accepted": len(events)}

    async def close(self) -> None:
        return None


async def test_worker_retries_anonymous_on_401(tmp_path: Path) -> None:
    """Первый client (с токеном) → 401; воркер берёт anonymous client и
    повторяет ТОТ ЖЕ батч → accepted, очередь пустеет."""
    analytics_sync.track("skill.enable", resource_type="skill", resource_id="1")
    calls: list[str | None] = []

    def _factory(anonymous: bool = False):
        return _Client(None if anonymous else "tok", calls)

    sender = OutboxSender(_factory)
    result = await sender.send_once(force=True)

    # Два вызова: с токеном (401) → anonymous (accepted)
    assert calls == ["tok", None]
    assert result.accepted == 1
    assert result.requeued == 0
    assert _pending() == 0


async def test_worker_no_anonymous_retry_when_already_tokenless(
    tmp_path: Path,
) -> None:
    """Изначально anonymous (factory(anonymous=True) → None = деградировать
    некуда): 401 НЕ ретраится, конверты остаются — без зацикливания."""
    analytics_sync.track("skill.enable", resource_type="skill", resource_id="1")
    calls: list = []

    def _factory(anonymous: bool = False):
        # Токена не было → anonymous-вариант идентичен → деградации нет.
        return None if anonymous else _Client("tok", calls)

    sender = OutboxSender(_factory)
    result = await sender.send_once(force=True)

    assert len(calls) == 1, "анонимного ретрая нет — деградировать некуда"
    assert result.requeued == 1
    assert _pending() == 1, "401 — повторяемый статус, конверт остаётся"


async def test_worker_legacy_factory_without_anonymous_arg_still_works(
    tmp_path: Path,
) -> None:
    """Обратная совместимость: фабрика без параметра ``anonymous`` работает."""
    analytics_sync.track("skill.enable", resource_type="skill", resource_id="1")
    calls: list = []

    sender = OutboxSender(lambda: _Client(None, calls))
    result = await sender.send_once(force=True)

    assert result.accepted == 1
    assert _pending() == 0


async def test_stuck_telemetry_branch_does_not_starve_anonymous_analytics(
    tmp_path: Path,
) -> None:
    """Незалогиненная машина: логи вечно 401, аналитика обязана уезжать.

    Это главный риск объединения очередей — залипшая ветка не должна ни
    блокировать голову очереди, ни включать backoff на весь транспорт.
    """
    for i in range(3):
        outbox.append("log", {"level": "error", "message": f"провал {i}"})
    analytics_sync.track("skill.install", resource_type="skill", resource_id="1")

    class _Mixed:
        def __init__(self) -> None:
            self.events: list[list[dict]] = []

        async def send_telemetry_batch(self, envelopes):
            raise ApiError(status_code=401, code="UNAUTHORIZED",
                           message="no bearer", details={})

        async def ingest_events(self, events, *, idempotency_key):
            self.events.append(list(events))
            return {"accepted": len(events)}

        async def close(self) -> None:
            return None

    client = _Mixed()
    res = await ow.flush_outbox(client, force=True)

    assert client.events and client.events[0][0]["event_type"] == "skill.install"
    assert _pending() == 0, "аналитика уехала анонимной веткой"
    assert len(outbox.read_batch(100)) == 3, "логи остались ждать логина"
    assert ow.backoff_delay() == 0.0, (
        "проход продвинулся — душить всю очередь backoff'ом нельзя"
    )
