"""Модель install-plan продукта и локальное состояние установленного (#5985).

Контракт плана — ADR-0001: backend отдаёт ``{"products": [...]}``, компоненты
уже отфильтрованы по ОС/архитектуре и упорядочены топологически; то, что под
эту ОС не поставляется, лежит в ``skipped``. CLI не додумывает артефакты и не
переставляет компоненты — он исполняет план.

Состояние — ``~/.skillery/products.json``: единственный ответ на вопросы
``product list`` и ``product remove``. Оно обязано быть детерминированным:
повторная установка тех же компонентов не меняет файл ни на байт.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from skillery_cli.products.paths import home_paths
from skillery_cli.products.plan import (
    Component,
    InstallPlan,
    PlanError,
    load_install_plan,
    parse_install_plan,
)
from skillery_cli.products.state import InstalledState, StateError, state_path


@pytest.fixture(autouse=True)
def _clean_product_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Боевые раскладки хоста не должны протекать в ожидания теста.

    ``DSH_HOME``/``DSH_PROFILE``/``CLAUDE_CONFIG_DIR``/``LOCALAPPDATA`` заданы на
    живой машине, а conftest изолирует только HOME. Тест, проверяющий дефолты,
    обязан видеть ровно дефолты; тесты оверрайдов ставят переменные сами.
    """
    for var in ("DSH_HOME", "DSH_PROFILE", "CLAUDE_CONFIG_DIR", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)


PLAN: dict = {
    "products": [
        {
            "slug": "dictation",
            "version": "1.10.0",
            "components": [
                {
                    "key": "dictation-service",
                    "type": "service",
                    "version": "1.10.0",
                    "artifact": {
                        "kind": "installer",
                        "os": "windows",
                        "arch": "amd64",
                        "url": "https://example.test/Dictation-Setup.exe",
                        "sha256": "a" * 64,
                        "size": 123,
                        "install": {
                            "args": ["/VERYSILENT"],
                            "detect": {"file": "%LOCALAPPDATA%\\Dictation\\dictation.exe"},
                        },
                    },
                    "depends_on": [],
                },
                {
                    "key": "voice-input",
                    "type": "plugin-dsh",
                    "version": "1.4.1",
                    "artifact": {
                        "kind": "archive",
                        "os": "windows",
                        "arch": "any",
                        "url": "https://example.test/voice-input.zip",
                        "sha256": "b" * 64,
                    },
                    "depends_on": ["dictation-service"],
                },
            ],
            "skipped": [{"key": "atlas", "reason": "no artifact for windows/arm64"}],
        }
    ]
}


def test_plan_parses_contract_example() -> None:
    plan = parse_install_plan(PLAN)

    assert isinstance(plan, InstallPlan)
    assert len(plan.products) == 1
    product = plan.products[0]
    assert product.slug == "dictation"
    assert product.version == "1.10.0"
    assert [component.key for component in product.components] == [
        "dictation-service",
        "voice-input",
    ]
    assert product.skipped[0]["reason"] == "no artifact for windows/arm64"

    service = product.components[0]
    assert isinstance(service, Component)
    assert service.type == "service"
    assert service.artifact is not None
    assert service.artifact.kind == "installer"
    assert service.artifact.install["args"] == ["/VERYSILENT"]

    plugin = product.components[1]
    assert plugin.depends_on == ("dictation-service",)


def test_plan_rejects_unknown_component_type() -> None:
    data = json.loads(json.dumps(PLAN))
    data["products"][0]["components"][0]["type"] = "daemon"
    with pytest.raises(PlanError, match="type"):
        parse_install_plan(data)


def test_plan_rejects_component_without_key() -> None:
    data = json.loads(json.dumps(PLAN))
    data["products"][0]["components"][0].pop("key")
    with pytest.raises(PlanError, match="key"):
        parse_install_plan(data)


def test_plan_rejects_artifact_without_kind() -> None:
    data = json.loads(json.dumps(PLAN))
    data["products"][0]["components"][0]["artifact"].pop("kind")
    with pytest.raises(PlanError, match="kind"):
        parse_install_plan(data)


def test_plan_rejects_unknown_artifact_kind() -> None:
    data = json.loads(json.dumps(PLAN))
    data["products"][0]["components"][0]["artifact"]["kind"] = "torrent"
    with pytest.raises(PlanError, match="kind"):
        parse_install_plan(data)


def test_plan_rejects_unknown_os() -> None:
    data = json.loads(json.dumps(PLAN))
    data["products"][0]["components"][0]["artifact"]["os"] = "beos"
    with pytest.raises(PlanError, match="os"):
        parse_install_plan(data)


def test_skill_ref_requires_slug() -> None:
    data = {
        "products": [
            {
                "slug": "atlas",
                "version": "0.7.8",
                "components": [
                    {
                        "key": "atlas-skill",
                        "type": "skill",
                        "artifact": {
                            "kind": "skill-ref",
                            "os": "any",
                            "arch": "any",
                        },
                    }
                ],
            }
        ]
    }
    with pytest.raises(PlanError, match="skill_ref"):
        parse_install_plan(data)

    data["products"][0]["components"][0]["artifact"]["skill_ref"] = "atlas"
    plan = parse_install_plan(data)
    assert plan.products[0].components[0].artifact is not None
    assert plan.products[0].components[0].artifact.skill_ref == "atlas"


def test_uv_tool_does_not_require_url_or_sha256() -> None:
    plan = parse_install_plan(
        {
            "products": [
                {
                    "slug": "skillery",
                    "components": [
                        {
                            "key": "skillery-cli",
                            "type": "cli",
                            "artifact": {
                                "kind": "uv-tool",
                                "os": "any",
                                "arch": "any",
                                "install": {"package": "skillery-cli"},
                            },
                        }
                    ],
                }
            ]
        }
    )
    artifact = plan.products[0].components[0].artifact
    assert artifact is not None
    assert artifact.install["package"] == "skillery-cli"


def test_plan_requires_products_list() -> None:
    with pytest.raises(PlanError, match="products"):
        parse_install_plan({})


def test_load_plan_reports_missing_and_broken_file(tmp_path: Path) -> None:
    with pytest.raises(PlanError, match="не найден"):
        load_install_plan(tmp_path / "missing.json")

    broken = tmp_path / "plan.json"
    broken.write_text("{не json", encoding="utf-8")
    with pytest.raises(PlanError, match="JSON"):
        load_install_plan(broken)


def test_state_lives_in_skillery_home() -> None:
    path = state_path()
    assert path == Path.home() / ".skillery" / "products.json"


def test_state_roundtrip_and_record_component() -> None:
    state = InstalledState.load()
    state.record_component(
        "dictation",
        "1.10.0",
        key="dictation-service",
        component_type="service",
        status="installed",
        detail="C:/Dictation/dictation.exe",
    )
    state.save()

    reloaded = InstalledState.load()
    product = reloaded.get_product("dictation")
    assert product is not None
    assert product["version"] == "1.10.0"
    component = product["components"]["dictation-service"]
    assert component["type"] == "service"
    assert component["status"] == "installed"
    assert component["detail"] == "C:/Dictation/dictation.exe"


def test_state_save_is_byte_stable_on_repeat() -> None:
    state = InstalledState.load()
    state.record_component(
        "x", "1.0.0", key="x-cli", component_type="cli", status="installed", detail=""
    )
    state.save()
    path = state_path()
    first = path.read_bytes()

    again = InstalledState.load()
    again.record_component(
        "x", "1.0.0", key="x-cli", component_type="cli", status="installed", detail=""
    )
    again.save()
    assert path.read_bytes() == first


def test_state_drop_product() -> None:
    state = InstalledState.load()
    state.record_component(
        "x", "1.0.0", key="x-cli", component_type="cli", status="installed", detail=""
    )
    state.save()
    state.drop_product("x")
    state.save()
    assert InstalledState.load().products == {}


def test_state_broken_json_is_readable_error() -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(StateError, match="JSON"):
        InstalledState.load()


def test_home_paths_are_under_home() -> None:
    paths = home_paths()
    home = Path.home()
    assert paths.bin_dir == home / ".local" / "bin"
    assert paths.claude_dir == home / ".claude"
    assert paths.claude_settings == home / ".claude" / "settings.json"
    assert paths.claude_plugins == home / ".claude" / "dsh-plugins"
    assert paths.dsh_home == home / ".dsh"
    assert paths.dsh_plugins == home / ".dsh" / "plugins"
    assert paths.dsh_profile == home / ".dsh" / "profiles" / "desktop"
    assert paths.skill_store == home / ".skillery" / "store"
    assert paths.products_file == home / ".skillery" / "products.json"


def test_home_paths_honour_dsh_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    custom = Path.home() / "dsh-custom"
    monkeypatch.setenv("DSH_HOME", str(custom))
    monkeypatch.setenv("DSH_PROFILE", "work")
    paths = home_paths()
    assert paths.dsh_home == custom
    assert paths.dsh_plugins == custom / "plugins"
    assert paths.dsh_profile == custom / "profiles" / "work"


def test_home_paths_windows_bin_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    local = Path.home() / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr("skillery_cli.products.paths._is_windows", lambda: True)
    paths = home_paths()
    assert paths.bin_dir == local / "skillery" / "bin"
