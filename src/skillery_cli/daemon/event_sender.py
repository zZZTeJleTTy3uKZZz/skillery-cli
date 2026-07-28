"""#1180: отправщик такта демона — тонкая обёртка над воркером ОБЩЕГО outbox'а.

Что здесь было раньше
---------------------
``EventSender`` снимал батч со СВОЕЙ очереди (``~/.skillery/events.queue.json``,
``EventCollector``) и слал его на ``POST /events``. Это была третья очередь на
машине; владелец требует ОДНУ. Хранилище переехало в общий
``telemetrykit.outbox`` (``kind="analytics_event"``, см.
:mod:`skillery_cli.core.analytics_sync`), а сеть делает единый воркер
:mod:`skillery_cli.core.outbox_worker` — он же сохраняет анонимную ветку
``/events`` для незалогиненной машины.

Зачем тогда этот класс
----------------------
``DaemonRunner`` строит backoff по результату ``send_once`` (``sent``/
``requeued``), а ``daemon status`` показывает накопленный ``state``. Эти
наблюдаемые вещи ломать нельзя, поэтому контракт «такт демона →
:class:`SendResult`» остаётся, меняется только его реализация.

Отправка живёт ИМЕННО в такте, а не в reconcile-хуке: reconcile подключается
только у залогиненного пользователя, а анонимная аналитика обязана уезжать и
без логина.

Отображение исхода воркера в :class:`SendResult`::

    sent     = сколько конвертов прочитано и отдано в отправку
    accepted = сколько принял бэкенд
    skipped  = прочитано, но ни принято, ни отклонено (старый клиент/бэкенд)
    requeued = осталось в очереди после прохода (ничего не потеряно)

Классификация ``DaemonRunner``'а («провал = слали, но всё вернулось») при таком
отображении работает как прежде: офлайн ⇒ ``removed=0`` ⇒ ``requeued == sent``.
Троттл воркера (``is_due``) отдаёт пустой результат — это НЕ провал, backoff от
него не растёт.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from skillery_cli.core import outbox_worker


@dataclass
class SendResult:
    """Исход такта отправки (то, что видят ``DaemonRunner`` и ``daemon status``)."""

    sent: int
    accepted: int
    skipped: int
    requeued: int
    last_error: str | None = None


class OutboxSender:
    """Такт демона: один проход доставки ОБЩЕГО outbox'а.

    ``client_factory`` — ``callable`` с контрактом
    ``factory(anonymous: bool = False) -> client | None``:

    - ``anonymous=False`` — обычный клиент (с токеном, если он есть);
    - ``anonymous=True`` — клиент БЕЗ Bearer либо ``None``, если деградировать
      некуда (токена и так не было).

    Старый контракт (фабрика без параметра) поддержан: тогда анонимного ретрая
    просто нет. Клиент не хранится: токен может обновиться между тактами, поэтому
    каждый проход строит свой и закрывает его в ``finally``.
    """

    def __init__(
        self,
        client_factory: Any,
        *,
        min_interval: float = outbox_worker.MIN_FLUSH_INTERVAL_SEC,
        limit: int = outbox_worker.BATCH_LIMIT,
        timeout: float = outbox_worker.FLUSH_TIMEOUT_SEC,
    ) -> None:
        self._make_client = client_factory
        self._min_interval = min_interval
        self._limit = limit
        self._timeout = timeout

    # ── построение клиентов ──
    def _build_client(self, *, anonymous: bool = False) -> Any:
        """Зовёт фабрику, терпя обе версии контракта. Никогда не бросает."""
        try:
            if not anonymous:
                try:
                    return self._make_client(anonymous=False)
                except TypeError:
                    return self._make_client()
            try:
                return self._make_client(anonymous=True)
            except TypeError:
                # Старая фабрика деградировать не умеет — анонимного пути нет.
                return None
        except Exception:  # noqa: BLE001 — нет клиента → просто не в этот раз
            return None

    async def send_once(self, *, force: bool = False) -> SendResult:
        """Один такт доставки. Никогда не бросает.

        ``force`` пробивает троттл воркера (``skillery event flush`` — ручная
        досылка «прямо сейчас»), но НЕ backoff: принуждать имеет смысл окно
        «не чаще раза в N секунд», а не отказ сети.
        """
        if not force and not outbox_worker.is_due(min_interval=self._min_interval):
            # Гейт ДО построения клиента: незачем поднимать HTTP-клиент на
            # каждый двухсекундный такт long-poll'а, чтобы тут же его закрыть.
            return SendResult(sent=0, accepted=0, skipped=0, requeued=0)

        client = self._build_client()
        if client is None:
            return SendResult(sent=0, accepted=0, skipped=0, requeued=0)
        try:
            result = await outbox_worker.flush_outbox_safe(
                client,
                limit=self._limit,
                timeout=self._timeout,
                force=force,
                min_interval=self._min_interval,
                anonymous_factory=lambda: self._build_client(anonymous=True),
            )
        finally:
            close = getattr(client, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 — закрытие не важнее доставки
                    pass

        if result.skipped:
            return SendResult(sent=0, accepted=0, skipped=0, requeued=0)
        return SendResult(
            sent=result.read,
            accepted=result.accepted,
            skipped=max(0, result.read - result.accepted - result.rejected),
            requeued=max(0, result.read - result.removed),
            last_error=result.error,
        )


__all__ = ["OutboxSender", "SendResult"]
