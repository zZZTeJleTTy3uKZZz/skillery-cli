"""Аналитика-эпик W1a: anonymous-fallback отправки событий.

POST /events принимает анонимно (backend ``_optional_claims``). Протухшая или
отсутствующая сессия НЕ должна глушить телеметрию автономного CLI:
- ``event flush`` без токена → отправка БЕЗ Bearer (anonymous), не exit(1)
  SESSION_EXPIRED/NOT_LOGGED_IN;
- ``EventSender``: токен есть, но 401 (протух) → ретрай тем же batch без Bearer
  (anonymous), события уходят, очередь пустеет.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from skills_hub_cli import output as out_mod
from skills_hub_cli.commands import _common
from skills_hub_cli.commands import event as event_mod
from skills_hub_cli.config import ClientConfig
from skills_hub_cli.core.transport import ApiError
from skills_hub_cli.daemon.event_collector import EventCollector
from skills_hub_cli.daemon.event_sender import EventSender


def _override_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    queue_path = tmp_path / "events.queue.json"
    monkeypatch.setattr(event_mod, "default_queue_path", lambda: queue_path)
    return queue_path


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
    queue_path = _override_queue(monkeypatch, tmp_path)
    coll = EventCollector(queue_path)
    coll.append("skill.enable", resource_type="skill", resource_id="1")

    cfg = ClientConfig(base_url="http://x")  # НЕ залогинен (user_email=None)
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    seen_tokens: list = []
    fake_client = MagicMock()

    async def _ingest(events, *, idempotency_key):
        return {"accepted": len(events), "event_ids": ["e0"]}

    async def _close() -> None:
        return None

    fake_client.ingest_events = _ingest
    fake_client.close = _close

    def _make(cfg_, access):
        seen_tokens.append(access)
        return fake_client

    monkeypatch.setattr(_common, "make_client", _make)

    # НЕ должно бросить typer.Exit
    event_mod.cmd_event_flush()

    # Очередь опустела (события ушли как anonymous)
    assert coll.size() == 0
    # client построен с пустым/None токеном (anonymous)
    assert seen_tokens and not seen_tokens[0]


def test_flush_with_expired_token_does_not_exit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Залогинен, но load_tokens вернул None (протух) → anonymous, не exit."""
    monkeypatch.setattr(out_mod, "_mode", "json")
    queue_path = _override_queue(monkeypatch, tmp_path)
    coll = EventCollector(queue_path)
    coll.append("skill.enable", resource_type="skill", resource_id="1")

    cfg = ClientConfig(base_url="http://x", user_email="u@e.io")
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    # keyring/файл пусты → токена нет
    monkeypatch.setattr(_common, "load_tokens", lambda email: (None, None))

    seen_tokens: list = []
    fake_client = MagicMock()

    async def _ingest(events, *, idempotency_key):
        return {"accepted": len(events)}

    async def _close() -> None:
        return None

    fake_client.ingest_events = _ingest
    fake_client.close = _close
    monkeypatch.setattr(
        _common, "make_client",
        lambda cfg_, access: (seen_tokens.append(access), fake_client)[1],
    )

    event_mod.cmd_event_flush()
    assert coll.size() == 0
    assert seen_tokens and not seen_tokens[0]


# ============================================================
# EventSender — 401 при наличии токена → ретрай anonymous
# ============================================================
# Контракт factory: ``factory(anonymous=False) -> HubClient | None``.
# ``anonymous=True`` строит client БЕЗ Bearer; возвращает ``None``, если
# деградировать некуда (токена и так не было) — sender тогда не ретраит.
@pytest.mark.asyncio
async def test_sender_retries_anonymous_on_401(tmp_path: Path) -> None:
    """Первый client (с токеном) → 401; sender запрашивает anonymous client и
    повторяет тот же batch → accepted, очередь пустеет."""
    coll = EventCollector(tmp_path / "q.json")
    coll.append("skill.enable", resource_type="skill", resource_id="1")

    calls: list[str | None] = []

    class _Client:
        def __init__(self, token: str | None) -> None:
            self.token = token

        async def ingest_events(self, events, *, idempotency_key):
            calls.append(self.token)
            if self.token:  # токен есть → сервер отверг (протух)
                raise ApiError(
                    status_code=401, code="SESSION_EXPIRED",
                    message="expired", details={},
                )
            return {"accepted": len(events)}

        async def close(self) -> None:
            return None

    def _factory(anonymous: bool = False):
        return _Client(None if anonymous else "tok")

    sender = EventSender(coll, _factory)

    result = await sender.send_once()
    # Два вызова: с токеном (401) → anonymous (accepted)
    assert calls == ["tok", None]
    assert result.accepted == 1
    assert coll.size() == 0


@pytest.mark.asyncio
async def test_sender_no_anonymous_retry_when_already_tokenless(
    tmp_path: Path,
) -> None:
    """Изначально anonymous (factory(anonymous=True) → None = деградировать
    некуда): 401 НЕ ретраится, batch requeue как раньше — без зацикливания."""
    coll = EventCollector(tmp_path / "q.json")
    coll.append("skill.enable", resource_type="skill", resource_id="1")

    calls: list = []

    class _Client:
        async def ingest_events(self, events, *, idempotency_key):
            calls.append(1)
            raise ApiError(status_code=401, code="SESSION_EXPIRED",
                           message="x", details={})

        async def close(self) -> None:
            return None

    def _factory(anonymous: bool = False):
        # Токена не было → anonymous-вариант идентичен → деградации нет.
        return None if anonymous else _Client()

    sender = EventSender(coll, _factory)
    result = await sender.send_once()
    # Один вызов, без анонимного ретрая (некуда деградировать).
    assert len(calls) == 1
    assert result.requeued == 1
    assert coll.size() == 1


@pytest.mark.asyncio
async def test_sender_legacy_factory_no_anonymous_arg_still_works(
    tmp_path: Path,
) -> None:
    """Обратная совместимость: factory без параметра anonymous (старый
    контракт) работает — 200 на первом же вызове, без ретрая."""
    coll = EventCollector(tmp_path / "q.json")
    coll.append("skill.enable", resource_type="skill", resource_id="1")

    class _Client:
        async def ingest_events(self, events, *, idempotency_key):
            return {"accepted": len(events)}

        async def close(self) -> None:
            return None

    # factory без kwargs — как было до фикса.
    sender = EventSender(coll, lambda: _Client())
    result = await sender.send_once()
    assert result.accepted == 1
    assert coll.size() == 0
