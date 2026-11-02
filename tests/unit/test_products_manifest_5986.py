"""Схема и рендер манифеста продукта ``product.toml`` (ADR-0001, задача #5986).

Проверяем ровно тот контракт, на который опирается импортёр реестров:

- обязательные поля и допустимые значения берутся из ADR (типы компонентов,
  ОС/архитектуры, виды артефактов);
- отсутствующий ``url``/``sha256`` у двоичного артефакта — это НЕ ошибка схемы
  (манифест фиксирует пробел, его видно в GAPS.md), но сами ключи обязаны быть:
  иначе пробел нельзя отличить от забытого поля;
- рендер детерминирован и читается обратно ``tomllib`` без потерь.
"""
from __future__ import annotations

import tomllib
from typing import Any

from skillery_cli.products.manifest import render_product, validate_product


def _product(**overrides: Any) -> dict[str, Any]:
    """Минимальный корректный продукт: один cli-компонент, один binary-артефакт."""
    product: dict[str, Any] = {
        "slug": "demo",
        "title": "Демо",
        "version": "1.2.3",
        "components": [
            {
                "key": "demo-cli",
                "type": "cli",
                "artifacts": [
                    {
                        "os": "linux",
                        "arch": "amd64",
                        "kind": "binary",
                        "url": "https://example.test/demo",
                        "sha256": "a" * 64,
                        "install": {"bin": "demo"},
                    }
                ],
            }
        ],
    }
    product.update(overrides)
    return product


def test_valid_product_has_no_errors() -> None:
    assert validate_product(_product()) == []


def test_missing_top_level_fields_are_reported() -> None:
    errors = validate_product({"components": []})
    assert any("slug" in e for e in errors)
    assert any("title" in e for e in errors)
    assert any("version" in e for e in errors)


def test_empty_version_is_rejected() -> None:
    errors = validate_product(_product(version=""))
    assert any("version" in e for e in errors)


def test_unknown_component_type_is_rejected() -> None:
    product = _product()
    product["components"][0]["type"] = "daemon"
    errors = validate_product(product)
    assert any("type" in e and "daemon" in e for e in errors)


def test_component_requires_key_and_artifacts() -> None:
    product = _product()
    product["components"][0].pop("key")
    product["components"][0]["artifacts"] = []
    errors = validate_product(product)
    assert any("key" in e for e in errors)
    assert any("artifacts" in e for e in errors)


def test_unknown_artifact_kind_os_and_arch_are_rejected() -> None:
    product = _product()
    artifact = product["components"][0]["artifacts"][0]
    artifact["kind"] = "torrent"
    artifact["os"] = "beos"
    artifact["arch"] = "mips"
    errors = validate_product(product)
    assert any("kind" in e for e in errors)
    assert any("os" in e for e in errors)
    assert any("arch" in e for e in errors)


def test_binary_artifact_requires_url_and_sha256_keys() -> None:
    product = _product()
    artifact = product["components"][0]["artifacts"][0]
    artifact.pop("url")
    artifact.pop("sha256")
    errors = validate_product(product)
    assert any("url" in e for e in errors)
    assert any("sha256" in e for e in errors)


def test_binary_artifact_with_empty_url_and_sha256_is_allowed() -> None:
    """Пробел фиксируется пустой строкой и попадает в GAPS.md — схема не падает."""
    product = _product()
    artifact = product["components"][0]["artifacts"][0]
    artifact["url"] = ""
    artifact["sha256"] = ""
    assert validate_product(product) == []


def test_installer_requires_install_args_and_detect() -> None:
    product = _product()
    artifact = product["components"][0]["artifacts"][0]
    artifact["kind"] = "installer"
    artifact["install"] = {}
    errors = validate_product(product)
    assert any("args" in e for e in errors)
    assert any("detect" in e for e in errors)


def test_uv_tool_requires_package_name() -> None:
    product = _product()
    artifact = product["components"][0]["artifacts"][0]
    artifact["kind"] = "uv-tool"
    artifact["install"] = {"index_url": ""}
    errors = validate_product(product)
    assert any("package" in e for e in errors)


def test_skill_ref_requires_skill_ref_and_does_not_require_url() -> None:
    product = _product()
    artifact = product["components"][0]["artifacts"][0]
    artifact["kind"] = "skill-ref"
    artifact.pop("url")
    artifact.pop("sha256")
    errors = validate_product(product)
    assert any("skill_ref" in e for e in errors)

    artifact["skill_ref"] = "demo"
    assert validate_product(product) == []


def test_duplicate_component_keys_are_rejected() -> None:
    product = _product()
    product["components"].append(dict(product["components"][0]))
    errors = validate_product(product)
    assert any("повтор" in e.lower() or "duplicate" in e.lower() for e in errors)


def test_duplicate_artifact_of_same_os_arch_kind_is_rejected() -> None:
    product = _product()
    artifacts = product["components"][0]["artifacts"]
    artifacts.append(dict(artifacts[0]))
    errors = validate_product(product)
    assert any("linux" in e for e in errors)


def test_render_round_trips_through_tomllib() -> None:
    product = _product()
    text = render_product(product)
    parsed = tomllib.loads(text)
    assert parsed == product


def test_render_is_byte_stable() -> None:
    product = _product()
    assert render_product(product) == render_product(product)


def test_render_keeps_russian_titles_and_inline_install() -> None:
    product = _product()
    product["title"] = "Диктовка"
    product["components"][0]["artifacts"][0]["install"] = {
        "args": ["/VERYSILENT"],
        "detect": {"file": "%LOCALAPPDATA%\\Programs\\Dictation\\dictation.exe"},
    }
    text = render_product(product)
    assert 'title = "Диктовка"' in text
    # install — однострочная inline-таблица, как в ADR: удобно читать глазами.
    install_line = next(line for line in text.splitlines() if line.startswith("install = "))
    assert install_line.count("\n") == 0
    assert tomllib.loads(text)["components"][0]["artifacts"][0]["install"] == {
        "args": ["/VERYSILENT"],
        "detect": {"file": "%LOCALAPPDATA%\\Programs\\Dictation\\dictation.exe"},
    }


def test_render_omits_empty_depends_on() -> None:
    product = _product()
    product["components"][0]["depends_on"] = []
    assert "depends_on" not in render_product(product)

    product["components"][0]["depends_on"] = ["other"]
    assert 'depends_on = ["other"]' in render_product(product)


def test_render_empty_install_table_stays_valid_toml() -> None:
    product = _product()
    product["components"][0]["artifacts"][0]["install"] = {}
    text = render_product(product)
    assert "install = {}" in text
    assert tomllib.loads(text) == product


# ── #6006: плагин — одна сущность, харнес — поле hosts ───────────────────────

def _plugin(**overrides: Any) -> dict[str, Any]:
    """Продукт с одним plugin-компонентом: харнес задаётся полем hosts."""
    component: dict[str, Any] = {
        "key": "demo-plugin",
        "type": "plugin",
        "hosts": ["dsh"],
        "artifacts": [
            {
                "os": "windows",
                "arch": "any",
                "kind": "archive",
                "host": "dsh",
                "url": "",
                "sha256": "",
            }
        ],
    }
    component.update(overrides.pop("component", {}))
    return {
        "slug": "demo",
        "title": "Демо",
        "version": "1.0.0",
        "components": [component],
        **overrides,
    }


def test_plugin_requires_at_least_one_host() -> None:
    product = _plugin()
    product["components"][0].pop("hosts")
    errors = validate_product(product)
    assert any("hosts" in error and "plugin" in error for error in errors)


def test_plugin_rejects_empty_hosts() -> None:
    errors = validate_product(_plugin(component={"hosts": []}))
    assert any("hosts" in error for error in errors)


def test_plugin_with_known_hosts_is_valid() -> None:
    product = _plugin(component={"hosts": ["dsh", "claude-code"]})
    assert validate_product(product) == []


def test_unknown_host_is_rejected() -> None:
    errors = validate_product(_plugin(component={"hosts": ["vim"]}))
    assert any("vim" in error for error in errors)


def test_duplicate_hosts_are_rejected() -> None:
    errors = validate_product(_plugin(component={"hosts": ["dsh", "dsh"]}))
    assert any("повтор" in error.lower() for error in errors)


def test_hosts_must_be_list_of_strings() -> None:
    errors = validate_product(_plugin(component={"hosts": "dsh"}))
    assert any("hosts" in error for error in errors)


def test_skill_may_limit_hosts() -> None:
    product = _product()
    product["components"][0]["hosts"] = ["claude-code"]
    assert validate_product(product) == []


def test_old_plugin_types_are_rejected() -> None:
    for legacy in ("plugin" + "-dsh", "plugin" + "-claude"):
        errors = validate_product(
            _plugin(component={"type": legacy})
        )
        assert any("type" in error and legacy in error for error in errors), legacy


def test_new_component_types_are_valid() -> None:
    for component_type in ("rules", "hook", "agent-profile"):
        product = _product()
        component = product["components"][0]
        component["type"] = component_type
        component["key"] = component_type
        assert validate_product(product) == [], component_type


def test_artifacts_may_target_different_hosts() -> None:
    product = _plugin(component={"hosts": ["dsh", "claude-code"]})
    component = product["components"][0]
    component["artifacts"].append(
        {
            "os": "windows",
            "arch": "any",
            "kind": "archive",
            "host": "claude-code",
            "url": "",
            "sha256": "",
        }
    )
    assert validate_product(product) == []


def test_unknown_artifact_host_is_rejected() -> None:
    product = _plugin()
    product["components"][0]["artifacts"][0]["host"] = "vim"
    errors = validate_product(product)
    assert any("host" in error and "vim" in error for error in errors)


def test_artifact_host_must_belong_to_component_hosts() -> None:
    product = _plugin(component={"hosts": ["dsh"]})
    product["components"][0]["artifacts"][0]["host"] = "claude-code"
    errors = validate_product(product)
    assert any("hosts" in error for error in errors)


def test_render_keeps_hosts_and_artifact_host() -> None:
    product = _plugin(component={"hosts": ["dsh", "claude-code"]})
    product["components"][0]["artifacts"][0]["host"] = "claude-code"
    text = render_product(product)
    assert 'hosts = ["dsh", "claude-code"]' in text
    assert 'host = "claude-code"' in text
    assert tomllib.loads(text) == product


def test_render_omits_empty_hosts_for_non_plugin() -> None:
    text = render_product(_product())
    assert "hosts =" not in text
    assert "host =" not in text
