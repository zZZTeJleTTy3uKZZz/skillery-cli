"""#1490: проверка права В МОМЕНТ ИСПОЛЬЗОВАНИЯ — гейт лиза в ``skillery run``.

ЦКП задачи: «Запуск способности с отозванным правом отказывает с внятным
текстом не позже истечения лиза — даже если машина всё это время была офлайн;
при живой сети и действующем праве запуск не ходит в сеть и не замедляется».

Отсюда четыре группы, и ни одну нельзя выкинуть:

1. **Офлайн-сценарий** (доказательство ЦКП). Сети нет вовсе — сеть в этих
   тестах не подменена двойником, а физически недоступна: транспорт
   импортируется через модуль, которого гейт не касается. Действующий лиз →
   запуск идёт; истёкший → отказ с текстом.
2. **Гейт точечный.** Навык без способностей с ``requires_lease`` запускается
   ровно как раньше — это самое опасное место задачи: ошибка здесь останавливает
   людям всю работу, а не «платную часть».
3. **Липкость реестра требований.** Отзыв убирает способность из
   ``/me/capabilities`` и лиз из хранилища — но НЕ требование. Иначе гейт
   снимался бы ровно в момент отзыва, то есть работал бы наоборот.
4. **Тексты отказа** — те, что даёт кит (второго набора формулировок в проекте
   нет), и в каждом есть следующее действие.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from leasekit import LeaseVerdict
from leasekit.testing import LeaseSigner
from librarykit.errors import CliError

from skillery_cli import output as output_module
from skillery_cli.commands import run as run_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core import leases as leases_mod

SLUG = "grok-chat"
CAP = "grok_transcriber"
SUBJECT = "user:1042"
VERSION = "2.0.0"


# --------------------------------------------------------------------------
#  Фикстуры
# --------------------------------------------------------------------------
def _jwt(sub: str) -> str:
    """Access-токен ТОЛЬКО с нужным claim: гейт читает ``sub`` без верификации."""
    import base64

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    head = b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({"sub": sub, "exp": 0}).encode())
    return f"{head}.{body}.signature-not-checked-here"


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Стор с навыком + каталог состояния лизов + подписант хаба."""
    output_module._mode = "text"
    store_dir = tmp_path / "store"
    skill_dir = store_dir / SLUG
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text("---\nname: grok\n---\n", encoding="utf-8")
    (skill_dir / "_skill_meta.json").write_text(
        json.dumps({
            "slug": SLUG, "version": VERSION, "skill_id": "318",
            "manifest": {"version": VERSION, "kind": "tooling", "cli": [
                {"command_name": SLUG, "entrypoint": "grok.cli:main"},
            ]},
        }),
        encoding="utf-8",
    )
    cfg = ClientConfig(
        base_url="http://localhost:8000", store_dir=str(store_dir),
        user_email="owner@example.com",
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(leases_mod, "state_dir", lambda: state)

    token = _jwt("1042")
    monkeypatch.setattr(
        "skillery_cli.config.load_tokens", lambda email: (token, "refresh")
    )

    signer = LeaseSigner()
    (state / "hub_keys.json").write_text(
        json.dumps(signer.jwks_mapping), encoding="utf-8"
    )

    class _Env:
        def __init__(self) -> None:
            self.signer = signer
            self.state = state
            self.store_dir = store_dir
            self.spawned: list[list[str]] = []

        def require(self, capability: str = CAP, *, skill: str = SLUG,
                    skill_id: str | None = None) -> None:
            """Отметить способность как требующую лиз (то, что пишет демон)."""
            index = leases_mod.RequirementsIndex.load(state)
            index.merge([leases_mod.CapabilityRequirement(
                name=capability, requires_lease=True, capability_id="318",
                skill_id=skill_id if skill_id is not None
                else ("318" if skill == SLUG else "777"),
                skill=skill,
            )])
            index.save()

        def put_lease(self, *, ttl: int = 24 * 3600, subject: str = SUBJECT,
                      capability: str = CAP, signer: LeaseSigner | None = None,
                      issued_at: int | None = None) -> None:
            token = (signer or self.signer).issue(
                subject=subject, capability=capability, skill=SLUG,
                ttl=ttl, issued_at=issued_at,
            )
            leases_mod.LeaseStore(state).put(capability, token,
                                             hub_now=int(time.time()))

    return _Env()


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch: pytest.MonkeyPatch):
    """Запуск процесса подменён — тесты про гейт, а не про запуск навыка."""
    calls: list[list[str]] = []

    def _fake(argv: list[str], *, cwd: Any) -> int:
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(run_mod, "_spawn", _fake)
    monkeypatch.setattr(run_mod, "_resolve_executable", lambda entry, slug: ["python"])
    return calls


def _run(slug: str = SLUG) -> None:
    run_mod.cmd_run(slug, None, None, None)


# --------------------------------------------------------------------------
#  1. ОФЛАЙН — доказательство ЦКП
# --------------------------------------------------------------------------
class TestOffline:
    """Машина без сети. Гейт ОБЯЗАН решить всё локально, в обе стороны."""

    def test_valid_lease_runs_offline(self, env, _no_spawn) -> None:
        """Лиз действителен → навык запускается, сети не требуется."""
        env.require()
        env.put_lease()
        _run()
        assert _no_spawn, "навык с действующим лизом обязан запуститься"

    def test_expired_lease_denies_offline(self, env) -> None:
        """Лиз истёк → отказ. Ровно ЦКП: офлайн, без единого запроса.

        ``grace = 0`` (решение владельца §12.Б), поэтому ``exp`` — жёсткая
        граница: лиз выписан сутки назад с TTL сутки и уже мёртв.
        """
        env.require()
        env.put_lease(ttl=3600, issued_at=int(time.time()) - 7200)
        with pytest.raises(CliError) as excinfo:
            _run()
        assert excinfo.value.code == run_mod.LEASE_DENIED_CODE
        assert "Срок доступа" in str(excinfo.value)
        assert "Подключитесь к сети" in str(excinfo.value)

    def test_gate_makes_no_network_call(self, env, monkeypatch, _no_spawn) -> None:
        """Гейт не поднимает транспорт даже теоретически.

        Проверка не «мок не вызван», а жёстче: любое построение ``HubClient``
        на пути запуска роняет тест. Сетевой вызов на КАЖДЫЙ запуск — то, что
        ЦКП запрещает прямым текстом («не ходит в сеть и не замедляется»).
        """
        import skillery_cli.core.transport as transport_mod

        def _boom(*a: Any, **kw: Any):
            raise AssertionError("запуск навыка не имеет права ходить в сеть")

        monkeypatch.setattr(transport_mod, "HubClient", _boom)
        env.require()
        env.put_lease()
        _run()
        assert _no_spawn


# --------------------------------------------------------------------------
#  2. ГЕЙТ ТОЧЕЧНЫЙ — самое опасное место задачи
# --------------------------------------------------------------------------
class TestGateIsNarrow:
    def test_skill_without_gated_capabilities_runs(self, env, _no_spawn) -> None:
        """Реестр требований пуст → запуск как раньше, лиз не спрашивается."""
        _run()
        assert _no_spawn

    def test_capability_without_requires_lease_is_not_gated(
        self, env, _no_spawn
    ) -> None:
        """Способность есть, но ``requires_lease=false`` → гейта нет."""
        index = leases_mod.RequirementsIndex.load(env.state)
        index.merge([leases_mod.CapabilityRequirement(
            name="grok_ask", requires_lease=False, skill=SLUG,
        )])
        index.save()
        _run()
        assert _no_spawn

    def test_other_skill_gated_does_not_block_this_one(self, env, _no_spawn) -> None:
        """Требование чужого навыка не закрывает наш."""
        env.require(capability="vk_content", skill="vk")
        _run()
        assert _no_spawn

    def test_live_capability_keeps_skill_working(self, env, _no_spawn) -> None:
        """§6.3: отзыв одной способности не ломает навык, пока жива соседняя.

        У ``grok-chat`` две гейтуемых способности; лиз есть только на одну.
        Требовать лиз на КАЖДУЮ значило бы ломать работающую способность из-за
        соседней, которую пользователю никогда не выдавали.
        """
        env.require(capability="grok_ask")
        env.require(capability=CAP)
        env.put_lease(capability="grok_ask")
        _run()
        assert _no_spawn

    def test_broken_state_does_not_stop_work(self, env, monkeypatch, _no_spawn) -> None:
        """Сбой самого гейта — не повод не запускать: гарантию даёт срок лиза."""
        def _boom(*a: Any, **kw: Any):
            raise RuntimeError("реестр внезапно сломался")

        monkeypatch.setattr(leases_mod, "check_skill_run", _boom)
        monkeypatch.setattr(
            "skillery_cli.core.leases.check_skill_run", _boom, raising=False
        )
        _run()
        assert _no_spawn


# --------------------------------------------------------------------------
#  3. ОТЗЫВ И ЛИПКОСТЬ РЕЕСТРА
# --------------------------------------------------------------------------
class TestRevocation:
    def test_dropped_lease_denies_but_requirement_survives(self, env) -> None:
        """Демон удалил лиз по отказу хаба → отказ. Требование осталось.

        Именно это отличает контур от fail-open: удали мы требование вслед за
        правом, следующий запуск прошёл бы без всякой проверки.
        """
        env.require()
        env.put_lease()
        leases_mod.LeaseStore(env.state).drop(CAP)
        with pytest.raises(CliError) as excinfo:
            _run()
        assert "Нет доступа" in str(excinfo.value)
        assert leases_mod.RequirementsIndex.load(env.state).gated_for_skill(SLUG) == (CAP,)

    def test_deleting_leases_file_does_not_open_the_gate(self, env) -> None:
        """Снести ``leases.json`` — не способ обойти проверку."""
        env.require()
        env.put_lease()
        (env.state / "leases.json").unlink()
        with pytest.raises(CliError):
            _run()

    def test_forget_skill_clears_requirement(self, env) -> None:
        """Снятие навыка — ЕДИНСТВЕННАЯ законная причина забыть требование."""
        env.require()
        index = leases_mod.RequirementsIndex.load(env.state)
        assert index.forget_skill(SLUG) == 1
        index.save()
        assert leases_mod.RequirementsIndex.load(env.state).gated_for_skill(SLUG) == ()

    def test_lease_of_another_user_is_refused(self, env) -> None:
        """``sub`` — единственная привязка, по которой отказывают (§2.4)."""
        env.require()
        env.put_lease(subject="user:9999")
        with pytest.raises(CliError) as excinfo:
            _run()
        assert "другому пользователю" in str(excinfo.value)

    def test_foreign_key_is_refused(self, env) -> None:
        """Самовыписанный лиз чужим ключом не проходит: подпись асимметричная."""
        env.require()
        env.put_lease(signer=LeaseSigner(kid="hub-test"))
        with pytest.raises(CliError) as excinfo:
            _run()
        assert "не подтверждён" in str(excinfo.value)

    def test_clock_rolled_back_is_refused(self, env) -> None:
        """Перевод часов назад ниже монотонного пола → CLOCK_SUSPECT, а не «пускаем»."""
        env.require()
        env.put_lease()
        store = leases_mod.LeaseStore(env.state)
        store.raise_floor(int(time.time()) + 10 * 24 * 3600)
        with pytest.raises(CliError) as excinfo:
            _run()
        assert "часы" in str(excinfo.value).lower()


# --------------------------------------------------------------------------
#  4. ТЕКСТЫ И ВЕРДИКТЫ
# --------------------------------------------------------------------------
class TestMessages:
    def test_no_account_points_to_login(self, env, monkeypatch) -> None:
        """Лиз нужен, а учётки нет → подсказка вести должна ко входу."""
        env.require()
        monkeypatch.setattr(leases_mod, "current_subject", lambda *a, **kw: None)
        with pytest.raises(CliError) as excinfo:
            _run()
        assert "skillery login" in str(excinfo.value)

    def test_expired_beats_missing_in_the_message(self, env) -> None:
        """Из нескольких отказов показываем самый содержательный.

        «Срок истёк» объясняет ситуацию, «нет доступа» — нет; без порядка текст
        зависел бы от порядка ключей в json.
        """
        env.require(capability="grok_ask")
        env.require(capability=CAP)
        env.put_lease(capability=CAP, ttl=60, issued_at=int(time.time()) - 600)
        denied = leases_mod.check_skill_run(SLUG)
        assert denied is not None
        assert denied.verdict is LeaseVerdict.EXPIRED

    def test_every_denial_text_says_what_to_do(self, env) -> None:
        """Инвариант контракта: отказ не заканчивается констатацией."""
        env.require()
        denied = leases_mod.check_skill_run(SLUG)
        assert denied is not None
        assert "skillery" in denied.message
