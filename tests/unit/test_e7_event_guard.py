"""E7 анти-спам — `EventGuard`: дедуп по идемпотентности + throttle по rate.

Каждый ``skillery`` вызов — отдельный процесс, поэтому окно дедупа/throttle
персистится в sidecar-файле рядом с очередью (``events.guard.json``). Guard —
чистый «принять/отклонить» решатель ПЕРЕД append'ом:

- ДЕДУП: идентичный fingerprint (event_type + resource_id + ключевые поля
  payload) в окне ``dedup_window`` секунд → reject (не плодим дубль при ретрае
  или дабл-клике).
- THROTTLE: > ``max_per_window`` событий одного (event_type, resource_id) в окне
  ``throttle_window`` → reject (анти-флуд по одному ресурсу).

Решение монотонно по времени: устаревшие записи (за окном) вычищаются.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skillery_cli.daemon.event_guard import EventGuard


def _guard(tmp_path: Path, **kw) -> EventGuard:  # noqa: ANN003
    return EventGuard(tmp_path / "events.guard.json", **kw)


# ─────────────────────────── ДЕДУП ───────────────────────────
def test_dedup_rejects_identical_within_window(tmp_path: Path) -> None:
    g = _guard(tmp_path, dedup_window=60.0)
    assert g.should_accept("skill.enable", "42", now=1000.0) is True
    # тот же event в окне 60s → отклонён (дубль)
    assert g.should_accept("skill.enable", "42", now=1010.0) is False


def test_dedup_allows_after_window(tmp_path: Path) -> None:
    g = _guard(tmp_path, dedup_window=60.0)
    assert g.should_accept("skill.enable", "42", now=1000.0) is True
    # за пределами окна (61s) → снова принят
    assert g.should_accept("skill.enable", "42", now=1061.0) is True


def test_dedup_distinguishes_resource(tmp_path: Path) -> None:
    g = _guard(tmp_path, dedup_window=60.0)
    assert g.should_accept("skill.enable", "42", now=1000.0) is True
    # другой resource_id — не дубль
    assert g.should_accept("skill.enable", "99", now=1001.0) is True


def test_dedup_distinguishes_event_type(tmp_path: Path) -> None:
    g = _guard(tmp_path, dedup_window=60.0)
    assert g.should_accept("skill.enable", "42", now=1000.0) is True
    # другой тип на тот же ресурс — не дубль (enable≠disable)
    assert g.should_accept("skill.disable", "42", now=1001.0) is True


def test_dedup_payload_keys_differentiate(tmp_path: Path) -> None:
    """Разный version в payload → не дубль (контент события отличается)."""
    g = _guard(tmp_path, dedup_window=60.0)
    assert g.should_accept(
        "skill.update", "42", payload={"version": "1.0.0"}, now=1000.0
    ) is True
    assert g.should_accept(
        "skill.update", "42", payload={"version": "2.0.0"}, now=1001.0
    ) is True
    # тот же version → дубль
    assert g.should_accept(
        "skill.update", "42", payload={"version": "2.0.0"}, now=1002.0
    ) is False


# ─────────────────────────── THROTTLE ───────────────────────────
def test_throttle_caps_rate_per_resource(tmp_path: Path) -> None:
    """> max_per_window РАЗНЫХ событий одного (type,resource) в окне → reject."""
    # dedup_window=0 чтобы дедуп не мешал (различаем по payload-счётчику)
    g = _guard(
        tmp_path, dedup_window=0.0, throttle_window=60.0, max_per_window=3
    )
    # 3 разных события (разный payload) проходят
    for i in range(3):
        assert g.should_accept(
            "skill.run", "42", payload={"n": i}, now=1000.0 + i
        ) is True
    # 4-е в том же окне → throttled
    assert g.should_accept(
        "skill.run", "42", payload={"n": 99}, now=1004.0
    ) is False


def test_throttle_window_slides(tmp_path: Path) -> None:
    g = _guard(
        tmp_path, dedup_window=0.0, throttle_window=60.0, max_per_window=2
    )
    assert g.should_accept("skill.run", "42", payload={"n": 0}, now=1000.0) is True
    assert g.should_accept("skill.run", "42", payload={"n": 1}, now=1001.0) is True
    # 3-е сразу → throttled
    assert g.should_accept("skill.run", "42", payload={"n": 2}, now=1002.0) is False
    # после сдвига окна (старые выпали) → снова можно
    assert g.should_accept("skill.run", "42", payload={"n": 3}, now=1062.0) is True


def test_throttle_independent_per_resource(tmp_path: Path) -> None:
    g = _guard(
        tmp_path, dedup_window=0.0, throttle_window=60.0, max_per_window=1
    )
    assert g.should_accept("skill.run", "A", payload={"n": 0}, now=1000.0) is True
    # лимит на A исчерпан, но B независим
    assert g.should_accept("skill.run", "A", payload={"n": 1}, now=1001.0) is False
    assert g.should_accept("skill.run", "B", payload={"n": 0}, now=1002.0) is True


# ─────────────────────────── ПЕРСИСТ ───────────────────────────
def test_guard_persists_across_instances(tmp_path: Path) -> None:
    """Новый процесс (новый instance того же файла) видит окно предыдущего."""
    path = tmp_path / "events.guard.json"
    g1 = EventGuard(path, dedup_window=60.0)
    assert g1.should_accept("skill.enable", "42", now=1000.0) is True
    g2 = EventGuard(path, dedup_window=60.0)
    # дубль виден в другом instance (персист на диске)
    assert g2.should_accept("skill.enable", "42", now=1010.0) is False


def test_guard_corrupt_file_is_safe(tmp_path: Path) -> None:
    """Битый sidecar → guard не падает, считает окно пустым (fail-open)."""
    path = tmp_path / "events.guard.json"
    path.write_text("{not json", encoding="utf-8")
    g = EventGuard(path, dedup_window=60.0)
    # не бросает; первое событие проходит
    assert g.should_accept("skill.enable", "42", now=1000.0) is True


def test_guard_prunes_stale_records(tmp_path: Path) -> None:
    """Старые записи (за самым широким окном) не копятся вечно."""
    path = tmp_path / "events.guard.json"
    g = EventGuard(path, dedup_window=60.0, throttle_window=60.0, max_per_window=100)
    for i in range(10):
        g.should_accept("skill.run", "42", payload={"n": i}, now=1000.0 + i)
    # далеко в будущем — все прежние записи устарели и вычищаются
    g.should_accept("skill.run", "42", payload={"n": 999}, now=99999.0)
    assert g.record_count() <= 1
