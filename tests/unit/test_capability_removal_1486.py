"""#1486: отзыв доводится до устройства — и НЕ сносит навык, нужный другому.

Главное свойство, ради которого написан весь файл (контракт лиза §6.3):

    отзыв ≠ снятие.

Лиз отвечает на вопрос «можно ли ИСПОЛНЯТЬ», задание ``action=remove`` — на
вопрос «должны ли ЛЕЖАТЬ файлы». Отзыв ``grok_transcriber`` при живом гранте
на ``grok_ask`` обязан убить лиз транскрибации и НЕ тронуть ``grok-chat`` —
иначе одна отозванная способность ломает соседнюю, право на которую никто не
отзывал.

Тесты держат три рубежа доведения (§6.1) в той их части, что живёт в CLI:

* такт демона — способность пропала из ``/me/capabilities`` ⇒ право снято, лиз
  удалён немедленно, но СТРОКА реестра остаётся (иначе гейт снимался бы ровно
  в момент отзыва);
* задание ``action=remove`` из очереди — снимает навык ТОЛЬКО когда его не
  держит ни одна способность с действующим правом;
* недоступность хаба ничего не отзывает (§7) — сеть молчит ≠ права нет.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from leasekit import LeaseStore
from leasekit.testing import LeaseSigner

from skillery_cli import __main__ as m
from skillery_cli.core import lease_sync as sync_mod
from skillery_cli.core import leases as leases_mod
from skillery_cli.core.leases import (
    CapabilityRequirement,
    RequirementsIndex,
    removal_blockers,
)

SUBJECT = "user:1042"
DEVICE = "dev-1"
SKILL = "grok-chat"
TRANSCRIBER = "grok_transcriber"
ASK = "grok_ask"


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(leases_mod, "state_dir", lambda: root)
    monkeypatch.setattr(sync_mod, "state_dir", lambda: root)
    monkeypatch.setattr(sync_mod, "current_subject", lambda *a, **kw: SUBJECT)
    return root


def _row(name: str, *, granted: bool = True, requires_lease: bool = True):
    return CapabilityRequirement(
        name=name,
        requires_lease=requires_lease,
        capability_id="318" if name == TRANSCRIBER else "204",
        skill_id="42",
        skill=SKILL,
        granted=granted,
    )


def _index(state: Path, *rows: CapabilityRequirement) -> RequirementsIndex:
    index = RequirementsIndex(state)
    index.merge(list(rows))
    index.save()
    return index


# ==================== реестр: право и требование — разные факты ============
class TestGrantStateInRegistry:
    def test_revoked_capability_keeps_its_row(self, state: Path) -> None:
        """Право снято, ТРЕБОВАНИЕ осталось — иначе отзыв снимал бы гейт.

        Строка реестра отвечает на вопрос «нужен ли здесь лиз вообще». Удалив
        её вслед за отозванным правом, мы своими руками открыли бы доступ
        ровно в тот момент, когда его закрывают.
        """
        index = _index(state, _row(TRANSCRIBER), _row(ASK))
        assert index.mark_revoked(frozenset({ASK})) == (TRANSCRIBER,)
        index.save()

        reloaded = RequirementsIndex.load(state)
        assert TRANSCRIBER in reloaded.items, "строка требования не имеет права исчезать"
        assert reloaded.items[TRANSCRIBER].granted is False
        # Гейт запуска по-прежнему знает, что способность платная.
        assert TRANSCRIBER in reloaded.gated_for_skill(SKILL)

    def test_legacy_row_without_flag_counts_as_granted(self, state: Path) -> None:
        """Файл старого CLI ключа не несёт ⇒ право считаем живым.

        Ошибка в эту сторону оставляет навык на диске лишний такт; в обратную
        — УДАЛЯЕТ файлы по умолчанию, а это необратимо.
        """
        (state / leases_mod.REQUIREMENTS_FILENAME).write_text(
            json.dumps(
                {
                    "ver": 1,
                    "hub_offset": 0,
                    "capabilities": {ASK: {"requires_lease": True, "skill": SKILL}},
                }
            ),
            encoding="utf-8",
        )
        assert RequirementsIndex.load(state).held_by(SKILL) == (ASK,)


# ==================== §6.3: отзыв одной способности не сносит навык =======
class TestCarrierGuard:
    def test_skill_held_by_another_granted_capability(self, state: Path) -> None:
        _index(state, _row(TRANSCRIBER, granted=False), _row(ASK, granted=True))
        assert removal_blockers(SKILL, skill_id="42") == (ASK,)

    def test_nothing_holds_skill_when_all_revoked(self, state: Path) -> None:
        _index(state, _row(TRANSCRIBER, granted=False), _row(ASK, granted=False))
        assert removal_blockers(SKILL, skill_id="42") == ()

    def test_capability_does_not_hold_skill_against_itself(self, state: Path) -> None:
        """``capability remove X`` не может быть заблокирован самим X."""
        _index(state, _row(TRANSCRIBER))
        assert removal_blockers(SKILL, skill_id="42", exclude=(TRANSCRIBER,)) == ()

    def test_plain_skill_is_not_guarded(self, state: Path) -> None:
        """Навык без способностей снимается как раньше — гейт точечный (§12.В)."""
        _index(state, _row(ASK))
        assert removal_blockers("atlas", skill_id="7") == ()

    def test_broken_registry_does_not_block_removal(self, state: Path) -> None:
        """Битый реестр не имеет права запереть файлы на диске навсегда.

        Гарантию «нельзя исполнять» даёт лиз, а не наличие файлов; направление
        ошибки выбрано в сторону работоспособности сознательно.
        """
        (state / leases_mod.REQUIREMENTS_FILENAME).write_text("{не json", encoding="utf-8")
        assert removal_blockers(SKILL) == ()


# ==================== рубеж 2: такт демона ================================
class _Hub:
    """Двойник ``HubClient`` для такта лизов."""

    def __init__(self, capabilities: list[dict[str, Any]], signer: LeaseSigner,
                 *, caps_error: Exception | None = None,
                 denied: list[dict[str, Any]] | None = None) -> None:
        self._capabilities = capabilities
        self._signer = signer
        self._caps_error = caps_error
        self._denied = denied or []
        self.last_hub_time = int(time.time())

    async def list_my_capabilities(self, *, supports_lease: bool = True,
                                   size: int = 200) -> list[dict[str, Any]]:
        if self._caps_error is not None:
            raise self._caps_error
        return self._capabilities

    async def replace_my_leases(self, *, capabilities: list[str], device_id: str,
                                supports_lease: bool = True) -> dict[str, Any]:
        denied_names = {d["capability"] for d in self._denied}
        return {
            "issued": [
                {
                    "capability": name, "capability_id": "318", "skill": SKILL,
                    "token": self._signer.issue(subject=SUBJECT, capability=name),
                    "expires_at": "2026-08-08T00:00:00Z",
                }
                for name in capabilities if name not in denied_names
            ],
            "denied": self._denied,
        }

    async def fetch_lease_jwks(self) -> dict[str, Any]:
        return self._signer.jwks_mapping


@pytest.fixture
def signer(state: Path) -> LeaseSigner:
    s = LeaseSigner()
    (state / "hub_keys.json").write_text(json.dumps(s.jwks_mapping), encoding="utf-8")
    return s


def _capability(name: str) -> dict[str, Any]:
    return {"id": "318", "name": name, "skill_id": "42", "requires_lease": True}


class TestRevocationReachesDevice:
    async def test_vanished_capability_loses_lease_and_grant(
        self, state: Path, signer: LeaseSigner
    ) -> None:
        """Пропала из витрины ⇒ право снято, лиз удалён, навык НЕ осиротел."""
        _index(state, _row(TRANSCRIBER), _row(ASK))
        store = LeaseStore(state)
        store.put(TRANSCRIBER, signer.issue(subject=SUBJECT, capability=TRANSCRIBER))
        store.put(ASK, signer.issue(subject=SUBJECT, capability=ASK))

        summary = await sync_mod.sync_leases(
            _Hub([_capability(ASK)], signer), device_id=DEVICE
        )

        assert TRANSCRIBER in summary["dropped"], "лиз отозванной способности не удалён"
        assert TRANSCRIBER in summary["revoked"]
        assert LeaseStore(state).token(TRANSCRIBER) is None
        # А вот навык-носитель по-прежнему держит живая соседка.
        assert removal_blockers(SKILL, skill_id="42") == (ASK,)

    async def test_denied_in_batch_revokes_too(
        self, state: Path, signer: LeaseSigner
    ) -> None:
        """Поимённый ``denied`` — такой же явный ответ хаба, как пропажа."""
        _index(state, _row(TRANSCRIBER))
        summary = await sync_mod.sync_leases(
            _Hub(
                [_capability(TRANSCRIBER)],
                signer,
                denied=[{"capability": TRANSCRIBER, "code": "GRANT_EXPIRED"}],
            ),
            device_id=DEVICE,
        )
        assert summary["denied"] == [TRANSCRIBER]
        assert summary["revoked"] == [TRANSCRIBER]
        assert removal_blockers(SKILL, skill_id="42") == ()

    async def test_hub_unreachable_revokes_nothing(
        self, state: Path, signer: LeaseSigner
    ) -> None:
        """§7: сеть молчит ≠ права нет. Ни одного отзыва, ни одного удаления."""
        _index(state, _row(TRANSCRIBER))
        store = LeaseStore(state)
        store.put(TRANSCRIBER, signer.issue(subject=SUBJECT, capability=TRANSCRIBER))

        summary = await sync_mod.sync_leases(
            _Hub([], signer, caps_error=OSError("connection reset")), device_id=DEVICE
        )

        assert summary["revoked"] == [] and summary["dropped"] == []
        assert LeaseStore(state).token(TRANSCRIBER) is not None
        assert removal_blockers(SKILL, skill_id="42") == (TRANSCRIBER,)


# ==================== рубеж 1: задание action=remove ======================
class _QueueClient:
    def __init__(self, queue: list[dict]) -> None:
        self._queue = queue
        self.reports: list[dict] = []

    async def fetch_device_queue_full(self, **kw):  # type: ignore[no-untyped-def]
        return {"items": list(self._queue), "device_tasks": []}

    async def report_device_apply(self, **kw):  # type: ignore[no-untyped-def]
        self.reports.append(kw)
        return {"status": "applied" if kw.get("ok") else "failed"}

    async def report_cli_log(self, **kw):  # type: ignore[no-untyped-def]
        return {}

    async def close(self) -> None:
        return None


class _Agent:
    name = "claude"


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from skillery_cli.config import ClientConfig

    c = ClientConfig.load()
    monkeypatch.setattr(type(c), "effective_store_dir", lambda self: tmp_path / "store")
    (tmp_path / "store").mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def removals(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    class _FakeInstaller:
        def __init__(self, target, store):  # type: ignore[no-untyped-def]
            pass

        def remove(self, **kw):  # type: ignore[no-untyped-def]
            calls.append(kw)

    monkeypatch.setattr(m, "SkillInstaller", _FakeInstaller)
    monkeypatch.setattr(m, "_revert_tooling", lambda *a, **k: None)
    monkeypatch.setattr(m, "track_skill_event", lambda *a, **k: None)
    monkeypatch.setattr(m, "HubClient", lambda **kw: None)
    monkeypatch.setattr(m, "_make_refresh_callback", lambda cfg: None)
    return calls


class TestRemoveTaskRespectsHolders:
    async def test_skill_kept_when_another_capability_holds_it(
        self, state: Path, cfg, removals: list[dict], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ЦКП #1486: навык снимается только если его никто не держит.

        Отозвали транскрибацию, чат остался — файлы ``grok-chat`` обязаны
        уцелеть, иначе отзыв одной способности убил бы другую.
        """
        _index(state, _row(TRANSCRIBER, granted=False), _row(ASK, granted=True))
        client = _QueueClient(
            [{"slug": SKILL, "skill_id": "42", "action": "remove", "desired_version": ""}]
        )

        report = await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=_Agent(), client=client
        )

        assert removals == [], "навык снят, хотя его держит живая способность"
        assert report["skipped"] == [SKILL]
        # Хабу рапортуем ОТКАЗ с причиной: молчаливый «ok» сделал бы состояние
        # устройства в вебе неправдой (записан снятым при живых файлах).
        assert client.reports and client.reports[0]["ok"] is False
        assert ASK in client.reports[0]["error"]

    async def test_skill_removed_when_nothing_holds_it(
        self, state: Path, cfg, removals: list[dict]
    ) -> None:
        _index(state, _row(TRANSCRIBER, granted=False), _row(ASK, granted=False))
        client = _QueueClient(
            [{"slug": SKILL, "skill_id": "42", "action": "remove", "desired_version": ""}]
        )

        report = await m._reconcile_device_queue(
            cfg, "tok", channel="published", agent_target=_Agent(), client=client
        )

        assert report["applied"] == [SKILL]
        assert removals == [
            {"slug": SKILL, "project": None, "keep_local": False, "purge": True}
        ]
        assert client.reports[0]["ok"] is True
        # Файлов нет ⇒ и требований этого навыка больше нет (единственная
        # законная причина забыть строку реестра).
        assert RequirementsIndex.load(state).carried_by(SKILL, "42") == ()
