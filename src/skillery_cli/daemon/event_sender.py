"""Batch sender: снимает chunk events из очереди и POST'ит на /events.

Sender — async-ready: вызывается из event-loop через
``DaemonRunner``, но также работает как обычная корутина для тестов
(monkeypatch ``HubClient``).

Поведение при ошибке:
- network / 5xx — events возвращаются в очередь (`collector.requeue`),
  следующий цикл повторит.
- 4xx (кроме 401) — events ТАКЖЕ возвращаются (это баг payload'а или
  permission denied; даём шанс пользователю исправить, но не
  накапливаем infinitely — лимит ретраев в metadata).
- 401 — token expired/invalid; не дропаем events, ждём пока юзер
  перелогинится.

Idempotency-Key — формируется как ``sha256(json.dumps(batch))``: один
и тот же payload даёт стабильный key (backend кеширует ответ).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from skillery_cli.core.transport import ApiError, HubClient
from skillery_cli.daemon.event_collector import EventCollector, QueuedEvent


@dataclass
class SendResult:
    sent: int
    accepted: int
    skipped: int
    requeued: int
    last_error: str | None = None


class EventSender:
    """Берёт events из ``EventCollector`` и отправляет в backend."""

    MAX_BATCH = 100  # совпадает с backend SystemConfig events.batch_max default

    def __init__(
        self,
        collector: EventCollector,
        client_factory,  # type: ignore[no-untyped-def]
        *,
        batch_size: int = 50,
    ) -> None:
        """``client_factory`` — callable() -> HubClient.

        Sender не владеет client'ом напрямую (т.к. tokens могут обновляться);
        каждый ``send_once`` зовёт factory и закрывает client после.
        """
        self._collector = collector
        self._make_client = client_factory
        self._batch_size = min(max(batch_size, 1), self.MAX_BATCH)

    @staticmethod
    def _idempotency_key(batch: list[dict[str, Any]]) -> str:
        """Стабильный key от содержимого batch: одинаковый batch → одинаковый key.

        Backend кеширует ответ при наличии Idempotency-Key, так что повторная
        отправка identical-batch'а не создаёт дубликатов.
        """
        canon = json.dumps(batch, sort_keys=True, ensure_ascii=False)
        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        return f"sh-cli-{digest[:24]}"

    def _build_client(self, *, anonymous: bool = False):  # type: ignore[no-untyped-def]
        """Зовёт factory. Контракт:

        ``factory(anonymous=False) -> HubClient`` — обычный client (с токеном,
        если есть). ``factory(anonymous=True)`` строит client БЕЗ Bearer и
        возвращает ``None``, если деградировать некуда (токена и так не было).

        Старый контракт (factory без параметра ``anonymous``) поддержан: при
        ``anonymous=False`` зовём ``factory()`` без аргумента; при
        ``anonymous=True`` такой factory не умеет деградировать → ``None``.
        """
        if not anonymous:
            try:
                return self._make_client(anonymous=False)
            except TypeError:
                return self._make_client()
        # anonymous=True — только если factory это поддерживает.
        try:
            return self._make_client(anonymous=True)
        except TypeError:
            return None

    async def send_once(self) -> SendResult:
        """Один цикл: drain batch → POST → at-failure requeue.

        POST /events анонимен (backend ``_optional_claims``): если токен протух
        (401/SESSION_EXPIRED), один раз ретраим тем же batch БЕЗ Bearer
        (anonymous) — протухшая сессия не глушит телеметрию автономного CLI.
        """
        batch_events: list[QueuedEvent] = self._collector.drain(
            limit=self._batch_size
        )
        if not batch_events:
            return SendResult(sent=0, accepted=0, skipped=0, requeued=0)

        dtos = [e.to_ingest_dto() for e in batch_events]
        idem = self._idempotency_key(dtos)
        client: HubClient = self._build_client()
        try:
            try:
                resp = await client.ingest_events(dtos, idempotency_key=idem)
            except ApiError as e:
                # 401 (токен протух) → ретрай anonymous (если есть куда
                # деградировать). Иначе — requeue (как раньше).
                if e.status_code == 401:
                    retry = await self._send_anonymous(dtos, idem)
                    if retry is not None:
                        return retry
                self._collector.requeue(batch_events)
                return SendResult(
                    sent=len(batch_events),
                    accepted=0,
                    skipped=0,
                    requeued=len(batch_events),
                    last_error=f"{e.code}: {e.message}",
                )
            except Exception as e:  # noqa: BLE001 — network/timeout/etc.
                self._collector.requeue(batch_events)
                return SendResult(
                    sent=len(batch_events),
                    accepted=0,
                    skipped=0,
                    requeued=len(batch_events),
                    last_error=str(e),
                )
            accepted = int(resp.get("accepted", 0))
            return SendResult(
                sent=len(batch_events),
                accepted=accepted,
                skipped=max(0, len(batch_events) - accepted),
                requeued=0,
            )
        finally:
            await client.close()

    async def _send_anonymous(
        self, dtos: list[dict[str, Any]], idem: str
    ) -> SendResult | None:
        """Повтор batch анонимным client'ом (без Bearer).

        Возвращает ``SendResult`` если ретрай выполнен; ``None`` если
        деградировать некуда (factory не дала anonymous client — токена и не
        было). Любая ошибка на ретрае → ``None`` (вызывающий сделает requeue).
        """
        anon = self._build_client(anonymous=True)
        if anon is None:
            return None
        try:
            resp = await anon.ingest_events(dtos, idempotency_key=idem)
        except Exception:  # noqa: BLE001 — ретрай не удался → пусть requeue
            return None
        finally:
            await anon.close()
        accepted = int(resp.get("accepted", 0))
        return SendResult(
            sent=len(dtos), accepted=accepted,
            skipped=max(0, len(dtos) - accepted), requeued=0,
        )

    @staticmethod
    def make_run_id() -> str:
        """Стабильный run-id для skill.run events (uuid4 hex, 12 chars)."""
        return uuid.uuid4().hex[:12]
