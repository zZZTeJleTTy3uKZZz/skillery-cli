"""Сетевая сторона лиза (#1490) — такт обновления в демоне.

Пара к :mod:`skillery_cli.core.leases`: там локальная проверка без сети, здесь
единственное место, где CLI за лизами ХОДИТ. Разделение не косметическое —
``run`` импортирует только локальный модуль, и «случайно сходить в сеть на
запуске» неоткуда.

────────────────────────────────────────────────────────────────────────────
ТАКТ (зовётся из ``commands/daemon.py`` в тяжёлой сверке, ``_HEAVY_RECONCILE_SEC
= 180``)

1. ``GET /me/capabilities?supports_lease=true`` — что мне сейчас разрешено.
   Это и есть **второй рубеж отзыва** (контракт лиза §6.1): способность,
   которую отозвали, из ответа пропадает, и локальный лиз на неё удаляется в
   пределах такта — секунды-минуты вместо суток.
2. Обновление реестра требований (``capabilities.json``) и часов: смещение от
   заголовка ``Date`` + монотонный пол ``hub_time_floor`` (§5).
3. Решение «надо ли перевыпускать» — по остатку срока (см. :data:`порог`).
4. Если надо — ОДИН ``PUT /me/leases`` на весь набор, а не N выдач.
5. ``denied[]`` из ответа — явный отказ хаба: такие лизы удаляются немедленно,
   grace не применяется никогда (§7).

ПОЧЕМУ ПОРОГ — ДОЛЯ TTL, А НЕ «СУТКИ». Постановка задачи требует обновлять
лизы, «истекающие в ближайшие сутки», а TTL лиза — ровно сутки (решение
владельца §12.А). Под такой критерий попадают ВСЕ лизы ВСЕГДА: каждое
устройство перевыпускало бы весь набор каждые 180 секунд — 480 раз в сутки на
машину. Правило контракта (§3) и исправленная владельцем арифметика: обновляем,
когда осталось меньше ``LEASE_REFRESH_FRACTION`` (⅓) срока, то есть ≈3 похода в
сутки. Остаток в момент обрыва связи при этом всегда 8–24 ч — именно он и есть
офлайн-запас, ради которого ``grace`` не понадобился.

ОТКАЗ ХАБА ≠ НЕДОСТУПНОСТЬ ХАБА (§7) — главное различение модуля. Сетевая
ошибка, таймаут, 5xx, протухший логин означают «о правах ничего не известно»:
не трогаем ни одного лиза, устройство продолжает работать по имеющимся до их
``exp``. Явный ответ хаба (``denied[]``, пропажа из ``/me/capabilities``)
означает «права нет»: лиз удаляется сразу. Спутать эти два случая — значит либо
класть всю локальную работу при каждом обрыве связи, либо не отзывать доступ
вовсе.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from leasekit import JwkSet, LeaseVerdict, inspect_lease
from leasekit.jwks import JwksError

from skillery_cli.core.leases import (
    LEASE_REFRESH_FRACTION,
    CapabilityRequirement,
    RequirementsIndex,
    current_subject,
    lease_store,
    state_dir,
)

_LOG = logging.getLogger(__name__)

#: Как часто перекачивать JWKS, если ключи и так на месте (сутки).
#:
#: Ротация ключа не убивает выданные лизы — хаб продолжает раздавать снятые с
#: подписи публичные части (контракт §2.1), — поэтому гнать этот запрос каждые
#: 180 секунд незачем. Внеочередное обновление и так случается там, где оно
#: действительно нужно: перед перевыпуском набора и при неизвестном ``kid``.
_JWKS_MAX_AGE_SEC = 24 * 3600


def _requirements_from_capabilities(items: list[dict[str, Any]]) -> list[CapabilityRequirement]:
    """DTO витрины ``/me/capabilities`` → строки реестра требований."""
    rows: list[CapabilityRequirement] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        rows.append(
            CapabilityRequirement(
                name=name,
                requires_lease=bool(item.get("requires_lease")),
                capability_id=str(item.get("id") or ""),
                skill_id=str(item.get("skill_id") or ""),
            )
        )
    return rows


def _needs_refresh(token: str | None, *, subject: str, capability: str, clock, keys) -> bool:  # noqa: ANN001
    """Пора ли перевыпускать лиз: нет / не проходит проверку / осталось < ⅓ TTL.

    Используется ``inspect_lease`` — диагностический путь кита, который вердикт
    ВОЗВРАЩАЕТ, а не поднимает. Здесь это ровно то, что нужно: решение
    «сходить за новым» — не гейт, и превращать его в исключение значило бы
    ронять такт демона на каждом истёкшем лизе.

    TTL не захардкожен, а вычисляется из самого лиза (``exp − iat``): цифру
    выбирает хаб, и второй её источник на устройстве неминуемо разъедется с
    первым.
    """
    if not token:
        return True
    check = inspect_lease(
        token, subject=subject, capability=capability, clock=clock, keys=keys
    )
    if check.verdict is not LeaseVerdict.VALID or check.lease is None:
        return True
    claims = check.lease.claims
    ttl = max(1, int(claims.exp) - int(claims.iat))
    remaining = int(claims.exp) - int(clock.effective_now)
    return remaining < ttl * LEASE_REFRESH_FRACTION


def _load_keys() -> JwkSet | None:
    try:
        return JwkSet.from_file(state_dir() / "hub_keys.json")
    except JwksError:
        return None


def _keys_are_stale() -> bool:
    path = state_dir() / "hub_keys.json"
    try:
        return (time.time() - path.stat().st_mtime) > _JWKS_MAX_AGE_SEC
    except OSError:
        return True


async def _refresh_jwks(client: Any) -> JwkSet | None:
    """Скачать и сохранить JWKS. Сетевая ошибка — не беда: работаем на старых ключах."""
    try:
        payload = await client.fetch_lease_jwks()
        keys = JwkSet.from_mapping(payload)
    except Exception as exc:  # noqa: BLE001 — хаб недоступен ≠ отказ (§7)
        _LOG.debug("JWKS не обновлён: %s", exc)
        return None
    path = state_dir() / "hub_keys.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        import json

        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        _LOG.warning("JWKS не сохранён в %s: %s", path, exc)
    return keys


async def sync_leases(client: Any, *, device_id: str) -> dict[str, Any]:
    """Один такт обновления лизов. Никогда не бросает — возвращает сводку.

    Демон не имеет права умереть из-за лизов: у него на такте ещё очередь
    заданий и доставка outbox'а. Поэтому любая неожиданность гасится здесь и
    уезжает в лог, а не наверх.

    Возвращает ``{"granted": N, "issued": [...], "denied": [...],
    "dropped": [...], "refreshed": bool, "error": str|None}`` — сводка для
    журнала и тестов.
    """
    summary: dict[str, Any] = {
        "granted": 0, "issued": [], "denied": [], "dropped": [], "revoked": [],
        "refreshed": False, "error": None,
    }
    subject = current_subject()
    if subject is None:
        # Не залогинены — проверять нечего и обновлять нечего. Существующие
        # лизы НЕ трогаем: `needs_login` — это «мы не знаем», а не «права нет».
        summary["error"] = "no-subject"
        return summary

    index = RequirementsIndex.load()
    store = lease_store()

    # 1. Что мне разрешено. Ошибка = хаб недоступен → выходим НИЧЕГО не удалив.
    try:
        items = await client.list_my_capabilities(supports_lease=True)
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("/me/capabilities недоступен: %s", exc)
        summary["error"] = str(exc) or type(exc).__name__
        return summary

    hub_now = getattr(client, "last_hub_time", None)
    if isinstance(hub_now, int) and hub_now > 0:
        index.hub_offset = hub_now - int(time.time())
        store.raise_floor(hub_now)

    rows = _requirements_from_capabilities(items)
    index.merge(rows)
    granted = {row.name for row in rows}
    summary["granted"] = len(granted)

    # #1486: витрина — ЯВНЫЙ ответ хаба, поэтому пропажа из неё значит «права
    # нет», а не «мы не знаем» (сеть отвалилась бы выше, ничего не тронув).
    # Помечаем право снятым, но СТРОКУ НЕ УДАЛЯЕМ: требование лиза липкое, и
    # именно этот факт потом решает, держит ли способность навык на диске.
    summary["revoked"] = list(index.mark_revoked(frozenset(granted)))

    # 2. Второй рубеж отзыва: способность пропала из ответа хаба ⇒ права нет.
    #    Это ЯВНЫЙ ответ, а не молчание сети (сеть отвалилась бы выше), поэтому
    #    лиз удаляется немедленно — grace не применяется никогда (§7).
    for capability in store.capabilities():
        if capability not in granted:
            store.drop(capability, hub_now=hub_now if isinstance(hub_now, int) else None)
            summary["dropped"].append(capability)

    # 3. Ключи. Нет вовсе — без них не проверить ни один лиз, значит и решить
    #    «пора ли обновлять» нельзя: считаем, что пора.
    keys = _load_keys()
    if keys is None or _keys_are_stale():
        fresh = await _refresh_jwks(client)
        keys = fresh or keys

    # 4. Кому пора. Порог — доля TTL (см. модульный docstring).
    #
    #    Набор — ТОЛЬКО способности с ``requires_lease``: остальные исполняются
    #    без доказательства (гейт точечный, §12.В), и лиз им не нужен. Просить
    #    его «на всякий случай» значило бы раздувать и запрос, и журнал выдач
    #    хаба на весь каталог пользователя.
    wanted = sorted(row.name for row in rows if row.requires_lease)
    if keys is None:
        due = list(wanted)
    else:
        clock = store.clock(offset=index.hub_offset)
        due = [
            capability
            for capability in wanted
            if _needs_refresh(
                store.token(capability),
                subject=subject,
                capability=capability,
                clock=clock,
                keys=keys,
            )
        ]

    if not due:
        index.save()
        return summary

    # 5. ОДИН запрос на весь набор. PUT — полная замена (REST-14/16): просим то,
    #    что нужно устройству целиком, а не только просроченное, иначе журнал
    #    хаба перестал бы отвечать на вопрос «что у меня действует».
    try:
        result = await client.replace_my_leases(
            capabilities=wanted, device_id=device_id, supports_lease=True
        )
    except Exception as exc:  # noqa: BLE001 — снова: недоступность ≠ отказ
        _LOG.debug("PUT /me/leases не прошёл: %s", exc)
        summary["error"] = str(exc) or type(exc).__name__
        index.save()
        return summary

    summary["refreshed"] = True
    hub_now = getattr(client, "last_hub_time", None)
    stamp = hub_now if isinstance(hub_now, int) and hub_now > 0 else None

    issued_rows: list[CapabilityRequirement] = []
    for issued in result.get("issued") or []:
        if not isinstance(issued, dict):
            continue
        name = str(issued.get("capability") or "").strip()
        token = str(issued.get("token") or "").strip()
        if not name or not token:
            continue
        store.put(name, token, hub_now=stamp)
        summary["issued"].append(name)
        # Слаг носителя приезжает ТОЛЬКО здесь (витрина отдаёт лишь skill_id) —
        # а гейт запуска резолвит навык именно по слагу.
        issued_rows.append(
            CapabilityRequirement(
                name=name,
                requires_lease=index.items[name].requires_lease
                if name in index.items
                else True,
                capability_id=str(issued.get("capability_id") or ""),
                skill_id=index.items[name].skill_id if name in index.items else "",
                skill=str(issued.get("skill") or ""),
            )
        )
    index.merge(issued_rows)

    for denial in result.get("denied") or []:
        if not isinstance(denial, dict):
            continue
        name = str(denial.get("capability") or "").strip()
        if not name:
            continue
        # Хаб ответил «нет» поимённо — удаляем сразу, как и при пропаже из
        # витрины. Причину (`code`) наружу не различаем: решение владельца
        # §12.Д — NO_GRANT и GRANT_REVOKED для пользователя одно и то же.
        store.drop(name, hub_now=stamp)
        # Поимённый отказ — такой же явный ответ, как пропажа из витрины:
        # право снято, и навык-носитель этой способностью больше не держится.
        if index.revoke(name):
            summary["revoked"].append(name)
        summary["denied"].append(name)
        _LOG.info(
            "лиз «%s» не выдан: %s", name, denial.get("code"),
            extra={"context": {"capability": name, "code": denial.get("code")}},
        )

    index.save()
    return summary


__all__ = ["sync_leases"]
