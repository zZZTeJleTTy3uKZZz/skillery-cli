"""#1489: команды ``skillery capability`` и объявление способностей в манифесте.

Три группы свойств, и все три — про то, что способность стала ПЕРВОКЛАССНОЙ
сущностью CLI, а не деталью навыка:

* **имя одно на всю систему.** ``grok_transcriber`` — ключ entry-point
  ``skillery.plugins``, тот же, что в ``cap`` лиза и в резолве gateway.
  Второго нейминга нет (инвариант §10.9 контракта лиза), поэтому команды
  адресуют способность именно им;
* **remove не отзывает право, revoke не удаляет файлы.** Снятие носителя
  проходит только когда его не держит другая способность с действующим правом
  (§6.3) — та же ``removal_blockers``, что и у задания очереди;
* **манифест ругается до сети.** Кривой ``[[capabilities]]`` обязан стоить
  один разбор TOML, а занятое ДРУГИМ навыком имя (409
  ``CAPABILITY_NAME_CONFLICT``) — объясняться человеку, а не показываться
  кодом.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
import typer

from skillery_cli import output as output_module
from skillery_cli.commands import _common
from skillery_cli.commands import capability as cap_mod
from skillery_cli.core import leases as leases_mod
from skillery_cli.core.capability_manifest import (
    CapabilityManifestError,
    capabilities_of,
    parse_capabilities,
)
from skillery_cli.core.leases import CapabilityRequirement, RequirementsIndex
from skillery_cli.core.transport import ApiError

SKILL = "grok-chat"
TRANSCRIBER = "grok_transcriber"
ASK = "grok_ask"


@pytest.fixture(autouse=True)
def text_mode() -> None:
    output_module._mode = "text"


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "state"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(leases_mod, "state_dir", lambda: root)
    return root


# ==================== манифест: [[capabilities]] ==========================
class TestManifestBlock:
    def test_declaration_becomes_publish_dto(self) -> None:
        caps = parse_capabilities(
            {
                "capabilities": [
                    {
                        "name": TRANSCRIBER,
                        "entry_point": "grok_chat.capabilities.transcribe:T",
                        "title": "Транскрибация",
                        "requires_lease": True,
                    }
                ]
            }
        )
        assert caps == [
            {
                "name": TRANSCRIBER,
                "entry_point": "grok_chat.capabilities.transcribe:T",
                "title": "Транскрибация",
                "requires_lease": True,
            }
        ]

    def test_absent_block_is_not_an_error(self) -> None:
        """Навык-инструкция способностей не объявляет — и не обязан."""
        assert parse_capabilities({"description": "x"}) == []

    @pytest.mark.parametrize(
        ("block", "expected"),
        [
            ([{}], "нет обязательного 'name'"),
            ([{"name": "a"}, {"name": "a"}], "дубль имени"),
            ([{"name": "a", "access_level": "secret"}], "access_level"),
            ([{"name": "a", "require_lease": True}], "неизвестные ключи"),
            ("не массив", "массивом таблиц"),
        ],
    )
    def test_broken_block_names_the_problem(self, block: Any, expected: str) -> None:
        """Строго, а не best-effort: способность без адреса нельзя ни выдать,
        ни отозвать, ни зарезолвить в лизе — молчаливый пропуск опубликовал бы
        версию, в которой обещанной способности просто нет."""
        with pytest.raises(CapabilityManifestError) as exc:
            parse_capabilities({"capabilities": block})
        assert expected in str(exc.value)

    def test_typo_in_flag_is_caught(self) -> None:
        """``require_lease`` вместо ``requires_lease`` уехало бы в никуда.

        Автор был бы уверен, что закрыл способность гейтом, а она осталась бы
        открытой — ровно тот класс ошибок, который никто не замечает.
        """
        with pytest.raises(CapabilityManifestError):
            parse_capabilities({"capabilities": [{"name": "a", "require_lease": True}]})

    def test_reads_from_skill_meta_toml(self, tmp_path: Path) -> None:
        (tmp_path / "_skill_meta.toml").write_text(
            '\n'.join([
                'description = "x"',
                'version = "1.0.0"',
                '[[capabilities]]',
                f'name = "{TRANSCRIBER}"',
                'requires_lease = true',
            ]),
            encoding="utf-8",
        )
        assert capabilities_of(tmp_path) == [
            {"name": TRANSCRIBER, "requires_lease": True}
        ]

    def test_no_meta_file_means_no_capabilities(self, tmp_path: Path) -> None:
        assert capabilities_of(tmp_path) == []

    def test_scaffold_validation_reports_broken_block(self, tmp_path: Path) -> None:
        from skillery_cli.commands.scaffold import validate_scaffolded_skill

        (tmp_path / "SKILL.md").write_text(
            "---\nname: x\nversion: 1.0.0\ndescription: d\n---\n", encoding="utf-8"
        )
        (tmp_path / "_skill_meta.toml").write_text(
            'description = "x"\nversion = "1.0.0"\n[[capabilities]]\ntitle = "нет имени"\n',
            encoding="utf-8",
        )
        errors = validate_scaffolded_skill(tmp_path)
        assert any("capabilities" in e for e in errors)


# ==================== 409: имя глобально уникально ========================
class TestNameConflictIsExplained:
    def test_conflict_gets_human_text_with_next_step(self) -> None:
        """Голый код 409 — это тикет в поддержку.

        Автор навыка локально прав: у него имя уникально. Что оно уникально на
        ВЕСЬ хаб (gateway резолвит только по имени и про навык не знает), знает
        один хаб — значит объяснить обязан клиент.
        """
        text = cap_mod.explain_api_error(
            ApiError(409, "CAPABILITY_NAME_CONFLICT", "occupied", {"name": TRANSCRIBER})
        )
        assert text is not None
        assert TRANSCRIBER in text
        assert "[[capabilities]]" in text, "нет следующего действия — что править"
        assert "entry-point" in text, "нет причины — почему имя глобально"

    def test_other_errors_keep_hub_wording(self) -> None:
        """Свой словарь причин на ВСЕ коды отстал бы от бэкенда — не заводим."""
        assert cap_mod.explain_api_error(ApiError(404, "NOT_FOUND", "нет")) is None


# ==================== capability remove: §6.3 =============================
class _Installer:
    calls: ClassVar[list[dict]] = []

    def __init__(self, target, store):  # type: ignore[no-untyped-def]
        pass

    def remove(self, **kw):  # type: ignore[no-untyped-def]
        _Installer.calls.append(kw)

        class _R:
            removed = True

        return _R()


@pytest.fixture
def installer(monkeypatch: pytest.MonkeyPatch) -> type[_Installer]:
    _Installer.calls = []
    monkeypatch.setattr("skillery_cli.core.installer.SkillInstaller", _Installer)
    monkeypatch.setattr("skillery_cli.__main__._revert_tooling", lambda *a, **k: None)
    return _Installer


def _seed(state: Path, *rows: CapabilityRequirement) -> None:
    index = RequirementsIndex(state)
    index.merge(list(rows))
    index.save()


def _row(name: str, *, granted: bool = True) -> CapabilityRequirement:
    return CapabilityRequirement(
        name=name, requires_lease=True, capability_id="318",
        skill_id="42", skill=SKILL, granted=granted,
    )


class TestCapabilityRemove:
    def test_keeps_skill_held_by_another_capability(
        self, state: Path, installer: type[_Installer]
    ) -> None:
        """ЦКП #1489: не сносит навык, который держит другая способность."""
        _seed(state, _row(TRANSCRIBER), _row(ASK))
        cap_mod.cmd_capability_remove(capability=TRANSCRIBER, agent=None)

        assert installer.calls == [], "навык снесён вместе с соседней способностью"
        index = RequirementsIndex.load(state)
        assert TRANSCRIBER not in index.items, "снятая способность осталась в реестре"
        assert ASK in index.items

    def test_removes_skill_when_last_capability_goes(
        self, state: Path, installer: type[_Installer]
    ) -> None:
        _seed(state, _row(TRANSCRIBER), _row(ASK, granted=False))
        cap_mod.cmd_capability_remove(capability=TRANSCRIBER, agent=None)

        assert installer.calls == [
            {"slug": SKILL, "project": None, "keep_local": False, "purge": True}
        ]

    def test_unknown_capability_exits_with_hint(self, state: Path) -> None:
        with pytest.raises(typer.Exit) as exc:
            cap_mod.cmd_capability_remove(capability="нет-такой", agent=None)
        assert exc.value.exit_code == 1


# ==================== список: имя + состояние лиза ========================
class _ListClient:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = items
        self.kwargs: dict[str, Any] = {}

    async def list_my_capabilities(self, **kw):  # type: ignore[no-untyped-def]
        self.kwargs = kw
        return list(self._items)

    async def close(self) -> None:
        return None


class TestCapabilityList:
    def test_declares_lease_support_and_shows_canonical_names(
        self, state: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Без ``supports_lease`` хаб fail-closed не отдаёт платную способность.

        То есть список молча врал бы: пользователь видел бы каталог без ровно
        тех строк, ради которых линия и делалась (§8 контракта).
        """
        client = _ListClient([{"id": "318", "name": TRANSCRIBER, "skill_id": "42"}])
        monkeypatch.setattr(_common, "make_client", lambda *a, **kw: client)
        monkeypatch.setattr(_common, "get_access_token", lambda: "tok")

        output_module._mode = "json"
        try:
            cap_mod.cmd_capability_list()
            payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        finally:
            output_module._mode = "text"

        assert client.kwargs == {"supports_lease": True}
        assert payload["items"][0]["name"] == TRANSCRIBER


# ==================== регистрация группы ==================================
class TestRegistration:
    def _app(self, monkeypatch: pytest.MonkeyPatch, permissions: list[str]):  # type: ignore[no-untyped-def]
        from skillery_cli.config import ClientConfig

        cfg = ClientConfig(
            base_url="http://localhost:8000",
            user_email="u@example.com",
            permissions=permissions,
        )
        monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
        from skillery_cli.__main__ import build_app

        app = build_app()
        group = next(t for t in app.registered_groups if t.name == "capability")
        return [c.name for c in group.typer_instance.registered_commands]

    def test_reading_and_own_machine_are_always_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """«Что мне разрешено» — не привилегия: прятать не от кого."""
        names = self._app(monkeypatch, ["skill.read"])
        assert {"list", "get", "install", "remove"} <= set(names)
        assert "grant" not in names and "revoke" not in names

    def test_grants_require_carrier_permission(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """У способности своего владельца нет — гейт тот же, что у навыка."""
        names = self._app(monkeypatch, ["skill.manage"])
        assert {"grant", "revoke"} <= set(names)
