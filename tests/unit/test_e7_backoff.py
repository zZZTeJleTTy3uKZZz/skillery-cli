"""E7 офлайн-batch — экспоненциальный backoff между неудачными flush.

Когда backend недоступен (network/5xx → events requeue'нулись), daemon не должен
долбить сервер каждые ``interval`` секунд. ``BackoffPolicy`` растит паузу
экспоненциально на каждый подряд неуспех (capped), и сбрасывает её на первом
успехе. ``DaemonRunner`` спит ``backoff.next_delay(base_interval, failed)``.
"""
from __future__ import annotations

import pytest

from skills_hub_cli.daemon.backoff import BackoffPolicy


def test_backoff_base_when_no_failures() -> None:
    b = BackoffPolicy(factor=2.0, max_delay=600.0)
    # 0 подряд-неуспехов → базовый интервал без множителя.
    assert b.delay(base=60.0, failures=0) == 60.0


def test_backoff_grows_exponentially() -> None:
    b = BackoffPolicy(factor=2.0, max_delay=10_000.0)
    assert b.delay(base=60.0, failures=1) == 120.0   # 60 * 2^1
    assert b.delay(base=60.0, failures=2) == 240.0   # 60 * 2^2
    assert b.delay(base=60.0, failures=3) == 480.0   # 60 * 2^3


def test_backoff_capped_at_max() -> None:
    b = BackoffPolicy(factor=2.0, max_delay=300.0)
    # 60 * 2^10 = 61440, но cap 300.
    assert b.delay(base=60.0, failures=10) == 300.0


def test_backoff_stateful_record_and_reset() -> None:
    """Stateful API: record_failure растит счётчик, record_success сбрасывает."""
    b = BackoffPolicy(factor=2.0, max_delay=10_000.0)
    assert b.failures == 0
    b.record_failure()
    b.record_failure()
    assert b.failures == 2
    assert b.current_delay(base=60.0) == 240.0
    b.record_success()
    assert b.failures == 0
    assert b.current_delay(base=60.0) == 60.0


def test_backoff_factor_one_is_constant() -> None:
    """factor=1 → backoff отключён (всегда базовый интервал)."""
    b = BackoffPolicy(factor=1.0, max_delay=600.0)
    assert b.delay(base=60.0, failures=5) == 60.0
