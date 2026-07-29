"""#1145 — миграция меты стора: source ``local-path`` → ``hub``.

ПОЧЕМУ ЭТО БАГ, А НЕ КОСМЕТИКА. Автообновление отбирает кандидатов строго по
``source == "hub"``. До s-skillkit 0.3.3 снапшот-установка из хаба помечалась
``local-path`` — и навык МОЛЧА выпадал из фильтра: ни ошибки, ни лога, просто
версия больше никогда не менялась. На машине владельца так застыли 7 навыков.

Ключевой инвариант, который здесь и охраняется: мигрируем ТОЛЬКО записи с
непустым ``skill_id`` (он проставляется исключительно для хаб-установок).
Настоящие ``--local-path`` установки авторов трогать нельзя — пометив их
``hub``, мы отдали бы их рабочий каталог фоновому авто-апдейту на перезапись.
"""
from __future__ import annotations

import json
from pathlib import Path

from skillery_cli.core.store_migrations import (
    ensure_store_meta_migrated,
    migrate_local_path_to_hub,
)

_META = "_skill_meta.json"


def _seed(store: Path, name: str, **meta) -> Path:
    d = store / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("x", encoding="utf-8")
    (d / _META).write_text(json.dumps({"slug": name, **meta}), encoding="utf-8")
    return d


def _source(d: Path) -> str | None:
    return json.loads((d / _META).read_text(encoding="utf-8")).get("source")


def test_hub_install_mislabeled_local_path_is_migrated(tmp_path: Path) -> None:
    store = tmp_path / "store"
    d = _seed(store, "vk", source="local-path", skill_id="42", repo_url=None)

    assert migrate_local_path_to_hub(store) == ["vk"]
    assert _source(d) == "hub"


def test_real_local_path_install_is_untouched(tmp_path: Path) -> None:
    """Без skill_id — это авторская установка из папки; переписать её нельзя."""
    store = tmp_path / "store"
    d = _seed(store, "my-draft", source="local-path")

    assert migrate_local_path_to_hub(store) == []
    assert _source(d) == "local-path"


def test_empty_skill_id_counts_as_no_skill_id(tmp_path: Path) -> None:
    """Пустая строка — не признак хаба (иначе гарантия невредимости дырявая)."""
    store = tmp_path / "store"
    d = _seed(store, "draft2", source="local-path", skill_id="  ")

    assert migrate_local_path_to_hub(store) == []
    assert _source(d) == "local-path"


def test_other_sources_are_untouched(tmp_path: Path) -> None:
    store = tmp_path / "store"
    hub = _seed(store, "already", source="hub", skill_id="7")
    git = _seed(store, "from-git", source="git-url", skill_id="8")

    assert migrate_local_path_to_hub(store) == []
    assert _source(hub) == "hub"
    assert _source(git) == "git-url"


def test_migration_is_idempotent(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _seed(store, "vk", source="local-path", skill_id="42")

    assert migrate_local_path_to_hub(store) == ["vk"]
    assert migrate_local_path_to_hub(store) == []


def test_missing_store_and_broken_meta_do_not_raise(tmp_path: Path) -> None:
    """Стор пользователя важнее починки меты: битый сосед не роняет проход."""
    assert migrate_local_path_to_hub(tmp_path / "нет-такого") == []

    store = tmp_path / "store"
    broken = store / "broken"
    broken.mkdir(parents=True)
    (broken / _META).write_text("{не json", encoding="utf-8")
    _seed(store, "vk", source="local-path", skill_id="42")

    assert migrate_local_path_to_hub(store) == ["vk"]


class _Cfg:
    """Минимальный двойник ClientConfig: только то, что читает миграция."""

    def __init__(self, store: Path) -> None:
        self._store = store
        self.store_meta_migrated = False
        self.saves = 0

    def effective_store_dir(self) -> Path:
        return self._store

    def save(self) -> None:
        self.saves += 1


def test_ensure_runs_once_and_sets_marker(tmp_path: Path) -> None:
    store = tmp_path / "store"
    _seed(store, "vk", source="local-path", skill_id="42")
    cfg = _Cfg(store)

    assert ensure_store_meta_migrated(cfg) == ["vk"]
    assert cfg.store_meta_migrated is True
    assert cfg.saves == 1

    # Второй прогон не платит за скан стора вообще.
    _seed(store, "tg", source="local-path", skill_id="43")
    assert ensure_store_meta_migrated(cfg) == []
    assert cfg.saves == 1


def test_ensure_never_raises_on_broken_config(tmp_path: Path) -> None:
    """Миграция — удобство, а не условие работы CLI/демона."""

    class _Boom:
        store_meta_migrated = False

        def effective_store_dir(self):  # noqa: ANN201
            raise OSError("нет доступа к стору")

    assert ensure_store_meta_migrated(_Boom()) == []


def test_auto_update_picks_up_migrated_skill(tmp_path: Path, monkeypatch) -> None:
    """Сквозной смысл миграции: навык ВОЗВРАЩАЕТСЯ в отбор автообновления.

    Отбор в ``_auto_update_hub_installs`` — это ровно
    ``[s for s in _collect_store_skills(root) if s.get("source") == "hub"]``.
    До миграции застывший навык в него не попадал.
    """
    import skillery_cli.__main__ as main_mod

    store = tmp_path / "store"
    _seed(store, "vk", source="local-path", skill_id="42", version="1.0.0")

    before = [s for s in main_mod._collect_store_skills(store) if s["source"] == "hub"]
    assert before == []

    migrate_local_path_to_hub(store)

    after = [s for s in main_mod._collect_store_skills(store) if s["source"] == "hub"]
    assert [s["ref"] for s in after] == ["vk"]


# ══════════ #1221 — миграция shim'ов на учёт вызова через «skillery run» ══════════
class TestShimMigrationToRunner:
    """Разница между «учёт для новых установок» и «учёт для всех».

    У пользователей уже лежат shim'ы старого формата — с прямым вызовом
    entrypoint, мимо учёта. Без перегенерации метрика «запуски» осталась бы
    вырожденной для всего, что установлено до кита 0.3.5, то есть для всего
    реально используемого.
    """

    def test_delegates_to_kit_and_reports_regenerated(self, monkeypatch) -> None:
        from skillery_cli.core import path_store, store_migrations

        monkeypatch.setattr(
            path_store, "regenerate_shims", lambda: ["vk", "atlas"], raising=False
        )
        assert store_migrations.ensure_shims_route_through_runner() == ["vk", "atlas"]

    def test_old_kit_without_regenerate_is_survived(self, monkeypatch) -> None:
        """Кит <0.3.5 метода не несёт — это отсутствие учёта, а не падение CLI."""
        from skillery_cli.core import path_store, store_migrations

        monkeypatch.delattr(path_store, "regenerate_shims", raising=False)
        assert store_migrations.ensure_shims_route_through_runner() == []

    def test_kit_failure_never_breaks_the_caller(self, monkeypatch) -> None:
        """Нет прав на bin-каталог → пустой список, а не упавший демон."""
        from skillery_cli.core import path_store, store_migrations

        def _boom() -> list[str]:
            raise OSError("bin-каталог только на чтение")

        monkeypatch.setattr(path_store, "regenerate_shims", _boom, raising=False)
        assert store_migrations.ensure_shims_route_through_runner() == []
