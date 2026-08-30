"""#2455: ``skillery propose`` — предложить владельцу навыка свои правки.

До этой команды улучшение чужого навыка не имело ни одного маршрута: человек
правил установленную копию, копия уезжала при первом ``skillery update``, а
владелец о правке не узнавал никогда.

Тесты держат то, ради чего команда и написана, — четыре свойства, каждое из
которых при поломке даёт свой класс беды:

а) **бандл детерминирован.** Иначе повторная сборка неизменившейся папки даёт
   другой ``bundle_digest``, и сверка «в хранилище лежит то, что приняли»
   начинает врать;
б) **база берётся из локального состояния, а изменения считаются по
   СОДЕРЖИМОМУ.** Спрашивать версию у человека нельзя (ответит неточно, и не
   со зла), а сравнение по времени файла пометило бы изменённым весь навык;
в) **сеть — последней.** Всё, что можно отклонить локально (нет правок, не
   установлен, нет версии, секрет в файлах), отклоняется до первого байта
   наружу;
г) **порядок вызовов ровно такой**: метаданные → бандл → подача. Он не
   перестановочен: подача без бандла подала бы пустое предложение.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer

from skillery_cli import output as output_module
from skillery_cli.commands import _common
from skillery_cli.commands import propose as propose_mod
from skillery_cli.config import ClientConfig
from skillery_cli.core.proposal_bundle import (
    ProposalBundleError,
    check_limits,
    collect_files,
    diff_files,
    make_bundle,
    tree_digest,
    unpack_snapshot,
)

SLUG = "grok-chat"


@pytest.fixture(autouse=True)
def text_mode() -> None:
    output_module._mode = "text"


# ============================================================
# (а) бандл: детерминизм и лимиты
# ============================================================
class TestBundle:
    def test_same_content_gives_same_bytes(self) -> None:
        """Времена файлов и порядок обхода не должны попадать в архив."""
        files = {"SKILL.md": b"a", "src/x.py": b"b"}
        reversed_order = dict(reversed(list(files.items())))
        assert make_bundle(files) == make_bundle(reversed_order)

    def test_roundtrip_through_snapshot_unpacking(self) -> None:
        files = {"SKILL.md": b"a", "src/x.py": b"b"}
        assert unpack_snapshot(make_bundle(files)) == files

    def test_snapshot_root_directory_is_stripped(self) -> None:
        """Хаб пакует версию папкой с именем навыка.

        Не срезав общий корень, мы бы сочли ПЕРЕИМЕНОВАННЫМ каждый файл — и
        показали автору «изменено всё».
        """
        packed = make_bundle({f"{SLUG}/SKILL.md": b"a", f"{SLUG}/src/x.py": b"b"})
        assert unpack_snapshot(packed) == {"SKILL.md": b"a", "src/x.py": b"b"}

    def test_local_state_never_leaves_the_machine(self, tmp_path: Path) -> None:
        """``_local``/``.git``/мета установки в бандл не едут.

        В ``_skill_meta.json`` установщик пишет абсолютный путь проекта — он
        уехал бы постороннему вместе с предложением.
        """
        (tmp_path / "_skill_meta.json").write_text("{}", encoding="utf-8")
        (tmp_path / "SKILL.md").write_text("x", encoding="utf-8")
        (tmp_path / "_local").mkdir()
        (tmp_path / "_local" / "token.txt").write_text("s3cret", encoding="utf-8")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "config").write_text("[core]", encoding="utf-8")
        assert set(collect_files(tmp_path)) == {"SKILL.md"}

    def test_empty_folder_is_refused(self) -> None:
        with pytest.raises(ProposalBundleError):
            check_limits({})

    def test_too_many_files_is_refused_before_network(self) -> None:
        """Потолок зеркалит бэкенд: отказ обязан стоить чтение папки."""
        with pytest.raises(ProposalBundleError) as exc:
            check_limits({f"f{i}": b"x" for i in range(2001)})
        assert "2000" in str(exc.value)


# ============================================================
# (б) диффы и пин базы
# ============================================================
class TestDiff:
    def test_added_modified_removed_are_all_visible(self) -> None:
        base = {"a": b"1", "b": b"2", "gone": b"3"}
        current = {"a": b"1", "b": b"CHANGED", "new": b"4"}
        changes = diff_files(base, current)
        assert changes.added == ("new",)
        assert changes.modified == ("b",)
        # Удаление — такое же изменение, как новый файл: применяется целиком.
        assert changes.removed == ("gone",)
        assert not changes.empty

    def test_identical_trees_have_nothing_to_propose(self) -> None:
        files = {"a": b"1"}
        assert diff_files(files, dict(files)).empty

    def test_base_digest_depends_on_content_not_packing(self) -> None:
        """Пересжатие той же версии не должно менять пин базы."""
        files = {"a": b"1", "b": b"2"}
        assert tree_digest(files) == tree_digest(dict(reversed(list(files.items()))))
        assert tree_digest(files) != tree_digest({"a": b"1", "b": b"3"})


# ============================================================
# (в)+(г) команда: локальные отказы и порядок вызовов
# ============================================================
class _Client:
    """Фейк хаба: помнит порядок вызовов — он и есть предмет проверки."""

    def __init__(self, snapshot: bytes | None) -> None:
        self.snapshot = snapshot
        self.calls: list[str] = []
        self.created: dict[str, Any] = {}
        self.bundle: bytes = b""

    async def download_snapshot(self, ref: str, semver: str):  # type: ignore[no-untyped-def]
        self.calls.append("snapshot")
        return self.snapshot

    async def list_devices(self):  # type: ignore[no-untyped-def]
        return [{"id": 5, "client_device_id": "dev-local"}]

    async def create_proposal(self, skill_ref: str, payload: dict[str, Any]):  # type: ignore[no-untyped-def]
        self.calls.append("create")
        self.created = payload
        return {"id": "17", "status": "draft"}

    async def upload_proposal_bundle(self, skill_ref, proposal_id, data):  # type: ignore[no-untyped-def]
        self.calls.append("bundle")
        self.bundle = data

    async def submit_proposal(self, skill_ref, proposal_id):  # type: ignore[no-untyped-def]
        self.calls.append("submit")
        return {"id": proposal_id, "status": "scanning"}

    async def close(self) -> None:
        return None


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Установленная копия навыка в сторе + мета с версией."""
    store_root = tmp_path / "store"
    skill_dir = store_root / SLUG
    skill_dir.mkdir(parents=True)
    # ``write_bytes``, а не ``write_text``: на Windows перевод строки стал бы
    # CRLF, и тест сравнивал бы не то, что писал.
    (skill_dir / "SKILL.md").write_bytes(b"# grok\n")
    (skill_dir / "_skill_meta.json").write_text(
        json.dumps({"slug": SLUG, "skill_id": "42", "version": "1.0.0"}),
        encoding="utf-8",
    )
    cfg = ClientConfig(base_url="http://localhost:8000", store_dir=str(store_root))
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(_common, "get_access_token", lambda: "tok")
    monkeypatch.setattr("skillery_cli.core.identity.device_uid", lambda: "dev-local")
    return skill_dir


def _wire(monkeypatch: pytest.MonkeyPatch, client: _Client) -> None:
    monkeypatch.setattr(_common, "make_client", lambda *a, **kw: client)
    # Гейты секретов проверяются своими тестами (#204/E50); здесь важно, что
    # они ЗОВУТСЯ до сети — это проверяет отдельный тест ниже.
    monkeypatch.setattr(
        "skillery_cli.__main__._run_publish_secret_scan", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "skillery_cli.__main__._run_publish_denylist_gate", lambda *a, **k: None
    )


def _base_snapshot(**files: bytes) -> bytes:
    return make_bundle({f"{SLUG}/{name}": data for name, data in files.items()})


class TestProposeCommand:
    def test_not_installed_skill_is_refused_locally(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Предлагать правки можно только к установленной копии."""
        _wire(monkeypatch, _Client(None))
        with pytest.raises(typer.Exit) as exc:
            propose_mod.cmd_propose(
                skill="нет-такого", message="t", body_file=None, path=None, yes=True
            )
        assert exc.value.exit_code == 1

    def test_no_changes_means_nothing_to_propose(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Копия совпала с базой — отправлять нечего, и сеть на этом кончается."""
        client = _Client(_base_snapshot(**{"SKILL.md": b"# grok\n"}))
        _wire(monkeypatch, client)
        propose_mod.cmd_propose(
            skill=SLUG, message="t", body_file=None, path=None, yes=True
        )
        assert client.calls == ["snapshot"], "предложение ушло без правок"

    def test_missing_base_snapshot_stops_before_sending(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Без слепка базы пин делать не из чего — молча слать нельзя."""
        client = _Client(None)
        _wire(monkeypatch, client)
        with pytest.raises(SystemExit):
            propose_mod.cmd_propose(
                skill=SLUG, message="t", body_file=None, path=None, yes=True
            )
        assert client.calls == ["snapshot"]

    def test_happy_path_order_is_metadata_bundle_submit(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Порядок не перестановочен: подача без бандла подала бы пустое."""
        client = _Client(_base_snapshot(**{"SKILL.md": b"# old\n"}))
        _wire(monkeypatch, client)

        propose_mod.cmd_propose(
            skill=SLUG, message="Починил пример", body_file=None, path=None, yes=True
        )

        assert client.calls == ["snapshot", "create", "bundle", "submit"]
        assert client.created["base_version"] == "1.0.0"
        assert client.created["title"] == "Починил пример"
        # Пин базы — слепок СОДЕРЖИМОГО скачанной версии.
        assert client.created["base_digest"] == tree_digest({"SKILL.md": b"# old\n"})
        # Провенанс: id устройства резолвится, но отправку не держит.
        assert client.created["device_id"] == 5
        assert unpack_snapshot(client.bundle)["SKILL.md"] == b"# grok\n"

    def test_secret_scan_runs_before_any_upload(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Найденный секрет — стоп НА ЭТОЙ машине: до хаба он не доезжает."""
        client = _Client(_base_snapshot(**{"SKILL.md": b"# old\n"}))
        monkeypatch.setattr(_common, "make_client", lambda *a, **kw: client)

        def _boom(*_a: Any, **_k: Any) -> None:
            raise typer.Exit(1)

        monkeypatch.setattr("skillery_cli.__main__._run_publish_secret_scan", _boom)
        monkeypatch.setattr(
            "skillery_cli.__main__._run_publish_denylist_gate", lambda *a, **k: None
        )

        with pytest.raises(typer.Exit):
            propose_mod.cmd_propose(
                skill=SLUG, message="t", body_file=None, path=None, yes=True
            )
        assert "create" not in client.calls and "bundle" not in client.calls

    def test_empty_title_is_refused(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Пустой заголовок — отказ, а не «Update skill»."""
        client = _Client(_base_snapshot(**{"SKILL.md": b"# old\n"}))
        _wire(monkeypatch, client)
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with pytest.raises(typer.Exit) as exc:
            propose_mod.cmd_propose(
                skill=SLUG, message=None, body_file=None, path=None, yes=True
            )
        assert exc.value.exit_code == 2
        assert client.calls == []


# ============================================================
# регистрация: группа + плоское имя из спеки
# ============================================================
def test_group_and_flat_name_are_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = ClientConfig(
        base_url="http://localhost:8000",
        user_email="u@example.com",
        permissions=["skill.read"],
    )
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    from skillery_cli.__main__ import build_app

    app = build_app()
    group = next(g for g in app.registered_groups if g.name == "proposal")
    commands = {c.name: c for c in group.typer_instance.registered_commands}
    assert {"create", "list", "show", "withdraw"} <= set(commands)
    # Отправка в группе спрятана: одно действие с двумя ВИДИМЫМИ именами
    # лишает человека канона (инвариант #2267), а видимое имя здесь одно —
    # плоское `propose` из спеки.
    assert commands["create"].hidden is True
    # Плоское `propose` — имя из спеки приёма предложений, оно уезжает в чужие
    # инструкции; прятать его за `proposal create` нельзя.
    flat = {c.name for c in app.registered_commands if not c.hidden}
    assert "propose" in flat


# ============================================================
# list / show --diff / withdraw: реальные пути, а не фейк
# ============================================================
class _ReadClient:
    """Фейк чтения: помнит, куда ходили и с какими параметрами."""

    def __init__(self) -> None:
        self.status: Any = "не-звали"
        self.diff_called = False

    async def list_proposals(self, skill_ref: str, *, status: str | None = None):  # type: ignore[no-untyped-def]
        self.status = status
        return {
            "items": [
                {
                    "id": "17",
                    "status": "stale",
                    "title": "Починил пример",
                    "provenance": {"changed_paths": ["SKILL.md"]},
                }
            ],
            "total": 1,
        }

    async def get_proposal(self, skill_ref: str, proposal_id: str):  # type: ignore[no-untyped-def]
        return {
            "id": proposal_id,
            "status": "submitted",
            "title": "Починил пример",
            "body": "зачем",
            "base_version_id": "3",
            "bundle_size": 120,
        }

    async def get_proposal_diff(self, skill_ref: str, proposal_id: str):  # type: ignore[no-untyped-def]
        self.diff_called = True
        return {"proposal_id": proposal_id, "diff": "--- a\n+++ b\n"}

    async def withdraw_proposal(self, skill_ref: str, proposal_id: str) -> None:
        self.status = ("withdrawn", skill_ref, proposal_id)

    async def close(self) -> None:
        return None


class TestReadingCommands:
    def _wire(self, monkeypatch: pytest.MonkeyPatch, client: _ReadClient) -> None:
        cfg = ClientConfig(base_url="http://localhost:8000")
        monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
        monkeypatch.setattr(_common, "get_access_token", lambda: "tok")
        monkeypatch.setattr(_common, "make_client", lambda *a, **kw: client)

    def test_list_passes_status_facet(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _ReadClient()
        self._wire(monkeypatch, client)
        propose_mod.cmd_proposal_list(skill=SLUG, status="stale")
        assert client.status == "stale"

    def test_list_rejects_unknown_status_before_network(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _ReadClient()
        self._wire(monkeypatch, client)
        with pytest.raises(typer.Exit) as exc:
            propose_mod.cmd_proposal_list(skill=SLUG, status="нет-такого")
        assert exc.value.exit_code == 2
        assert client.status == "не-звали"

    def test_show_without_diff_does_not_ask_for_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Diff считает хаб и считает НЕ бесплатно — просим только по флагу."""
        client = _ReadClient()
        self._wire(monkeypatch, client)
        propose_mod.cmd_proposal_show(skill=SLUG, proposal_id="17", diff=False)
        assert client.diff_called is False

    def test_show_with_diff_prints_hub_diff(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = _ReadClient()
        self._wire(monkeypatch, client)
        output_module._mode = "json"
        try:
            propose_mod.cmd_proposal_show(skill=SLUG, proposal_id="17", diff=True)
            payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        finally:
            output_module._mode = "text"
        assert client.diff_called is True
        assert payload["diff"]["diff"].startswith("--- a")

    def test_withdraw_addresses_skill_and_proposal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _ReadClient()
        self._wire(monkeypatch, client)
        propose_mod.cmd_proposal_withdraw(skill=SLUG, proposal_id="17")
        assert client.status == ("withdrawn", SLUG, "17")
