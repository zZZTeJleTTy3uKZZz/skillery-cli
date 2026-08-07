"""#1490: такт демона — обновление набора лизов способностей.

Что здесь закреплено (и почему именно это):

* **Порог обновления — доля TTL, а не «сутки».** Постановка требовала обновлять
  лизы, истекающие в ближайшие сутки, при TTL ровно сутки: под критерий
  попадали бы ВСЕ лизы ВСЕГДА — 480 перевыпусков в сутки на устройство.
  Владелец исправил арифметику до ⅓ TTL. Тест держит именно это: свежий лиз
  сетевого запроса НЕ вызывает, доживающий последнюю треть — вызывает.
* **Один запрос на набор, а не N.** ``PUT /me/leases`` вместо N точечных выдач.
* **Отказ хаба ≠ недоступность хаба (§7).** Сетевая ошибка не имеет права
  удалить ни одного лиза; явный ``denied`` и пропажа из ``/me/capabilities`` —
  удаляют немедленно (grace не применяется никогда).
* **Монотонный пол времени** поднимается временем ХАБА (заголовок ``Date``), а
  не локальными часами: иначе контур снимается переводом часов назад.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from leasekit import LeaseStore
from leasekit.testing import LeaseSigner

from skillery_cli.core import lease_sync as sync_mod
from skillery_cli.core import leases as leases_mod

CAP = "grok_transcriber"
SUBJECT = "user:1042"
DEVICE = "dev-1"


class FakeHub:
    """Двойник ``HubClient``: считает вызовы и отдаёт заготовленные ответы.

    Считать вызовы обязательно: главное свойство такта — что при живых свежих
    лизах он в сеть за ними НЕ ходит, а это видно только по счётчику.
    """

    def __init__(self, *, capabilities: list[dict[str, Any]], signer: LeaseSigner,
                 denied: list[dict[str, Any]] | None = None,
                 caps_error: Exception | None = None,
                 put_error: Exception | None = None) -> None:
        self._capabilities = capabilities
        self._signer = signer
        self._denied = denied or []
        self._caps_error = caps_error
        self._put_error = put_error
        self.last_hub_time = int(time.time())
        self.calls: list[str] = []

    async def list_my_capabilities(self, *, supports_lease: bool = True,
                                   size: int = 200) -> list[dict[str, Any]]:
        self.calls.append("capabilities")
        assert supports_lease is True, "клиент обязан заявлять supports_lease (§8)"
        if self._caps_error is not None:
            raise self._caps_error
        return self._capabilities

    async def replace_my_leases(self, *, capabilities: list[str], device_id: str,
                                supports_lease: bool = True) -> dict[str, Any]:
        self.calls.append("put-leases")
        if self._put_error is not None:
            raise self._put_error
        denied_names = {d["capability"] for d in self._denied}
        issued = [
            {
                "jti": f"lse_{name}", "capability": name, "capability_id": "318",
                "skill": "grok-chat",
                "token": self._signer.issue(subject=SUBJECT, capability=name),
                "expires_at": "2026-08-08T00:00:00Z",
            }
            for name in capabilities if name not in denied_names
        ]
        return {"issued": issued, "denied": self._denied}

    async def fetch_lease_jwks(self) -> dict[str, Any]:
        self.calls.append("jwks")
        return self._signer.jwks_mapping


def _capability(name: str = CAP, *, requires_lease: bool = True) -> dict[str, Any]:
    return {
        "id": 318, "name": name, "skill_id": 42,
        "requires_lease": requires_lease, "granted": True,
    }


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(leases_mod, "state_dir", lambda: root)
    monkeypatch.setattr(sync_mod, "state_dir", lambda: root)
    monkeypatch.setattr(sync_mod, "current_subject", lambda *a, **kw: SUBJECT)
    return root


@pytest.fixture
def signer(state: Path) -> LeaseSigner:
    s = LeaseSigner()
    (state / "hub_keys.json").write_text(json.dumps(s.jwks_mapping), encoding="utf-8")
    return s


async def _sync(hub: FakeHub) -> dict[str, Any]:
    return await sync_mod.sync_leases(hub, device_id=DEVICE)


# --------------------------------------------------------------------------
#  Порог обновления — исправленная владельцем арифметика
# --------------------------------------------------------------------------
@pytest.mark.asyncio
class TestRefreshThreshold:
    async def test_fresh_lease_is_not_reissued(self, state, signer) -> None:
        """Свежий лиз → ``PUT /me/leases`` не зовётся вовсе.

        Это и есть исправление постановки: при пороге «истекает в ближайшие
        сутки» и TTL сутки сюда попадал бы КАЖДЫЙ такт — 480 перевыпусков в
        день на машину.
        """
        LeaseStore(state).put(
            CAP, signer.issue(subject=SUBJECT, capability=CAP), hub_now=int(time.time())
        )
        hub = FakeHub(capabilities=[_capability()], signer=signer)
        summary = await _sync(hub)
        assert "put-leases" not in hub.calls
        assert summary["refreshed"] is False

    async def test_lease_in_last_third_is_reissued(self, state, signer) -> None:
        """Осталось меньше ⅓ TTL → перевыпуск. Порог считается из самого лиза
        (``exp − iat``), а не из захардкоженных 24 часов."""
        ttl = 24 * 3600
        old = signer.issue(
            subject=SUBJECT, capability=CAP, ttl=ttl,
            issued_at=int(time.time()) - int(ttl * 0.8),
        )
        LeaseStore(state).put(CAP, old, hub_now=int(time.time()))
        hub = FakeHub(capabilities=[_capability()], signer=signer)
        summary = await _sync(hub)
        assert "put-leases" in hub.calls
        assert summary["issued"] == [CAP]
        assert LeaseStore(state).token(CAP) != old

    async def test_missing_lease_is_issued(self, state, signer) -> None:
        hub = FakeHub(capabilities=[_capability()], signer=signer)
        summary = await _sync(hub)
        assert summary["issued"] == [CAP]
        assert LeaseStore(state).token(CAP)

    async def test_one_request_for_the_whole_set(self, state, signer) -> None:
        """Набор обновляется ОДНИМ запросом: N выдач = N раундтрипов на парк."""
        caps = [_capability("grok_ask"), _capability(CAP), _capability("gemini_ask")]
        hub = FakeHub(capabilities=caps, signer=signer)
        await _sync(hub)
        assert hub.calls.count("put-leases") == 1
        assert len(LeaseStore(state).capabilities()) == 3

    async def test_capability_without_requires_lease_is_not_requested(
        self, state, signer
    ) -> None:
        """Гейт точечный: способности без ``requires_lease`` лиз не нужен."""
        hub = FakeHub(
            capabilities=[_capability("grok_ask", requires_lease=False)], signer=signer
        )
        await _sync(hub)
        assert "put-leases" not in hub.calls
        assert LeaseStore(state).capabilities() == ()


# --------------------------------------------------------------------------
#  Отзыв: два рубежа из трёх живут здесь
# --------------------------------------------------------------------------
@pytest.mark.asyncio
class TestRevocation:
    async def test_capability_gone_from_hub_drops_lease(self, state, signer) -> None:
        """Способность пропала из ``/me/capabilities`` — это ЯВНЫЙ ответ «нет»."""
        LeaseStore(state).put(
            CAP, signer.issue(subject=SUBJECT, capability=CAP), hub_now=int(time.time())
        )
        hub = FakeHub(capabilities=[], signer=signer)
        summary = await _sync(hub)
        assert summary["dropped"] == [CAP]
        assert LeaseStore(state).token(CAP) is None

    async def test_denied_in_batch_drops_lease(self, state, signer) -> None:
        """Частичный отказ — норма: одна отозванная не роняет остальные."""
        hub = FakeHub(
            capabilities=[_capability(CAP), _capability("grok_ask")],
            signer=signer,
            denied=[{"capability": CAP, "code": "NO_GRANT", "message": "нет права"}],
        )
        summary = await _sync(hub)
        assert summary["denied"] == [CAP]
        assert summary["issued"] == ["grok_ask"]
        assert LeaseStore(state).token(CAP) is None
        assert LeaseStore(state).token("grok_ask")

    async def test_requirement_survives_revocation(self, state, signer) -> None:
        """Требование лиза ЛИПКОЕ: право отозвали — гейт остался.

        Убери мы строку вслед за правом, следующий ``skillery run`` прошёл бы
        вообще без проверки, то есть отзыв открывал бы доступ.
        """
        hub = FakeHub(capabilities=[_capability()], signer=signer)
        await _sync(hub)
        gone = FakeHub(capabilities=[], signer=signer)
        await _sync(gone)
        index = leases_mod.RequirementsIndex.load(state)
        assert index.items[CAP].requires_lease is True
        assert index.gated_for_skill("grok-chat") == (CAP,)


# --------------------------------------------------------------------------
#  Хаб недоступен — противоположный случай (§7)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
class TestHubUnavailable:
    async def test_network_error_touches_nothing(self, state, signer) -> None:
        """Сеть отвалилась → ни один лиз не удалён. Иначе любой обрыв связи
        клал бы всю локальную работу."""
        token = signer.issue(subject=SUBJECT, capability=CAP)
        LeaseStore(state).put(CAP, token, hub_now=int(time.time()))
        hub = FakeHub(
            capabilities=[], signer=signer, caps_error=OSError("connect failed")
        )
        summary = await _sync(hub)
        assert summary["error"]
        assert LeaseStore(state).token(CAP) == token

    async def test_put_failure_keeps_existing_leases(self, state, signer) -> None:
        old = signer.issue(
            subject=SUBJECT, capability=CAP, ttl=3600,
            issued_at=int(time.time()) - 3000,
        )
        LeaseStore(state).put(CAP, old, hub_now=int(time.time()))
        hub = FakeHub(
            capabilities=[_capability()], signer=signer,
            put_error=OSError("timeout"),
        )
        summary = await _sync(hub)
        assert summary["error"]
        assert LeaseStore(state).token(CAP) == old

    async def test_no_login_is_not_revocation(self, state, signer, monkeypatch) -> None:
        """``needs_login`` = «мы не знаем», а не «права нет»."""
        token = signer.issue(subject=SUBJECT, capability=CAP)
        LeaseStore(state).put(CAP, token, hub_now=int(time.time()))
        monkeypatch.setattr(sync_mod, "current_subject", lambda *a, **kw: None)
        hub = FakeHub(capabilities=[], signer=signer)
        summary = await _sync(hub)
        assert summary["error"] == "no-subject"
        assert hub.calls == []
        assert LeaseStore(state).token(CAP) == token


# --------------------------------------------------------------------------
#  Часы (§5)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
class TestClock:
    async def test_hub_time_raises_the_floor(self, state, signer) -> None:
        """Пол поднимается временем ХАБА и никогда не уменьшается."""
        hub = FakeHub(capabilities=[_capability()], signer=signer)
        hub.last_hub_time = int(time.time()) + 500
        await _sync(hub)
        assert LeaseStore(state).time_floor >= hub.last_hub_time

    async def test_offset_is_persisted_for_the_gate(self, state, signer) -> None:
        """Смещение часов хаба доезжает до локального гейта через реестр."""
        hub = FakeHub(capabilities=[_capability()], signer=signer)
        hub.last_hub_time = int(time.time()) + 3600
        await _sync(hub)
        assert leases_mod.RequirementsIndex.load(state).hub_offset > 3000

    async def test_jwks_is_fetched_when_absent(self, state, signer) -> None:
        """Ключей нет — без них не проверить ни один лиз; тянем их сами."""
        (state / "hub_keys.json").unlink()
        hub = FakeHub(capabilities=[_capability()], signer=signer)
        await _sync(hub)
        assert "jwks" in hub.calls
        assert (state / "hub_keys.json").exists()
