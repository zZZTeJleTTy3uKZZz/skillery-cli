"""Экспоненциальный backoff между неудачными flush daemon'а.

Когда отправка batch'а проваливается (network/5xx → events requeue'нулись),
``DaemonRunner`` не должен повторять каждые ``interval`` секунд — это долбит
недоступный backend. ``BackoffPolicy`` растит паузу как
``base * factor**failures`` (capped ``max_delay``) и сбрасывает на первом успехе.

Pure-функция ``delay(base, failures)`` + stateful-обёртка
(``record_failure``/``record_success``/``current_delay``) для использования в
loop'е runner'а.
"""
from __future__ import annotations


class BackoffPolicy:
    def __init__(self, *, factor: float = 2.0, max_delay: float = 3600.0) -> None:
        self._factor = max(1.0, float(factor))
        self._max_delay = max(0.0, float(max_delay))
        self._failures = 0

    @property
    def failures(self) -> int:
        return self._failures

    def delay(self, *, base: float, failures: int) -> float:
        """Пауза для данного числа подряд-неуспехов (чистая функция).

        ``failures=0`` → ``base`` (без множителя). ``factor=1`` → backoff
        выключён (всегда ``base``). Результат capped ``max_delay``.
        """
        base = max(0.0, float(base))
        n = max(0, int(failures))
        if n == 0 or self._factor == 1.0:
            return min(base, self._max_delay) if self._max_delay else base
        delay = base * (self._factor**n)
        if self._max_delay:
            return min(delay, self._max_delay)
        return delay

    # ── stateful API для loop'а ──
    def record_failure(self) -> None:
        self._failures += 1

    def record_success(self) -> None:
        self._failures = 0

    def current_delay(self, *, base: float) -> float:
        return self.delay(base=base, failures=self._failures)
