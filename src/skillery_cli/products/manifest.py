"""Схема и рендер манифеста продукта ``product.toml`` (ADR-0001).

Манифест — единственный источник правды для ``skillery product publish``,
поэтому проверка здесь строгая по составу полей и мягкая по значениям:

- обязательные поля обязаны присутствовать, но ``url``/``sha256`` у двоичных
  артефактов могут быть пустыми строками. Так манифест фиксирует известный
  пробел («релиза ещё нет»), а не молчит о нём; перечень таких артефактов
  печатается в ``catalog/products/GAPS.md`` и должен закрываться до публикации.
- ``skill-ref`` ссылается на навык Skillery по slug и не несёт байтов;
- ``uv-tool`` описывается пакетом (``install.package``), а не ссылкой;
- ``installer`` обязан знать тихую установку (``args``) и способ проверки
  (``detect.file``) — иначе установка не идемпотентна.

Значения перечислений взяты из ADR дословно: ``Component.type``,
``os``, ``arch``, ``kind``.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

COMPONENT_TYPES: tuple[str, ...] = (
    "skill",
    "cli",
    "mcp",
    "plugin-dsh",
    "plugin-claude",
    "service",
)
OS_VALUES: tuple[str, ...] = ("windows", "linux", "macos", "any")
ARCH_VALUES: tuple[str, ...] = ("amd64", "arm64", "any")
ARTIFACT_KINDS: tuple[str, ...] = (
    "skill-ref",
    "uv-tool",
    "binary",
    "archive",
    "installer",
    "mcp-spec",
)

_TOP_LEVEL_KEYS = frozenset({"slug", "title", "version", "components"})
_ARTIFACT_KEYS = frozenset(
    {"os", "arch", "kind", "url", "sha256", "skill_ref", "install"}
)
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")
_BYTES_KINDS = frozenset({"binary", "archive", "installer"})


def validate_product(product: Mapping[str, Any]) -> list[str]:
    """Проверить манифест; вернуть список ошибок (пустой — манифест годен)."""
    errors: list[str] = []
    if not isinstance(product, Mapping):
        return ["манифест продукта должен быть таблицей (mapping)"]

    unknown = sorted(set(product) - _TOP_LEVEL_KEYS)
    if unknown:
        errors.append(f"неизвестные поля продукта: {', '.join(unknown)}")

    for field in ("slug", "title", "version"):
        value = product.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"обязательное поле продукта {field!r} отсутствует или пустое")

    slug = product.get("slug")
    if isinstance(slug, str) and slug and not _SLUG_RE.match(slug):
        errors.append(f"slug {slug!r} не в формате kebab-case (a-z, 0-9, дефис)")

    version = product.get("version")
    if isinstance(version, str) and version and not _VERSION_RE.match(version):
        errors.append(f"version {version!r} не в формате semver (X.Y.Z)")

    components = product.get("components")
    if not isinstance(components, Sequence) or isinstance(components, (str, bytes)):
        errors.append("обязательное поле product.components отсутствует или не список")
        return errors
    if not components:
        errors.append("у продукта нет ни одного компонента")

    seen_keys: set[str] = set()
    for index, component in enumerate(components):
        errors.extend(_validate_component(component, index, seen_keys))
    return errors


def _validate_component(
    component: Any, index: int, seen_keys: set[str]
) -> list[str]:
    where = f"components[{index}]"
    if not isinstance(component, Mapping):
        return [f"{where}: компонент должен быть таблицей"]

    errors: list[str] = []
    unknown = sorted(set(component) - {"key", "type", "depends_on", "artifacts"})
    if unknown:
        errors.append(f"{where}: неизвестные поля компонента: {', '.join(unknown)}")

    key = component.get("key")
    if not isinstance(key, str) or not key.strip():
        errors.append(f"{where}: обязательное поле key отсутствует или пустое")
    elif not _SLUG_RE.match(key):
        errors.append(f"{where}: key {key!r} не в формате kebab-case")
    elif key in seen_keys:
        errors.append(f"{where}: повтор ключа компонента {key!r}")
    else:
        seen_keys.add(key)

    component_type = component.get("type")
    if component_type not in COMPONENT_TYPES:
        errors.append(
            f"{where}: недопустимый type {component_type!r}; "
            f"разрешены {', '.join(COMPONENT_TYPES)}"
        )

    depends_on = component.get("depends_on", [])
    if not isinstance(depends_on, Sequence) or isinstance(depends_on, (str, bytes)):
        errors.append(f"{where}: depends_on должен быть списком строк")
    else:
        for dependency in depends_on:
            if not isinstance(dependency, str) or not dependency.strip():
                errors.append(f"{where}: пустая или нестроковая зависимость в depends_on")

    artifacts = component.get("artifacts")
    if not isinstance(artifacts, Sequence) or isinstance(artifacts, (str, bytes)):
        errors.append(f"{where}: обязательное поле artifacts отсутствует или не список")
        return errors
    if not artifacts:
        errors.append(f"{where}: artifacts пуст — у компонента нет ни одного артефакта")

    seen_artifacts: set[tuple[str, str, str]] = set()
    for artifact_index, artifact in enumerate(artifacts):
        errors.extend(
            _validate_artifact(
                artifact,
                f"{where}.artifacts[{artifact_index}]",
                seen_artifacts,
            )
        )
    return errors


def _validate_artifact(
    artifact: Any, where: str, seen: set[tuple[str, str, str]]
) -> list[str]:
    if not isinstance(artifact, Mapping):
        return [f"{where}: артефакт должен быть таблицей"]

    errors: list[str] = []
    unknown = sorted(set(artifact) - _ARTIFACT_KEYS)
    if unknown:
        errors.append(f"{where}: неизвестные поля артефакта: {', '.join(unknown)}")

    os_name = artifact.get("os")
    arch = artifact.get("arch")
    kind = artifact.get("kind")
    if os_name not in OS_VALUES:
        errors.append(
            f"{where}: недопустимый os {os_name!r}; разрешены {', '.join(OS_VALUES)}"
        )
    if arch not in ARCH_VALUES:
        errors.append(
            f"{where}: недопустимый arch {arch!r}; разрешены {', '.join(ARCH_VALUES)}"
        )
    if kind not in ARTIFACT_KINDS:
        errors.append(
            f"{where}: недопустимый kind {kind!r}; разрешены {', '.join(ARTIFACT_KINDS)}"
        )
        return errors

    identity = (str(os_name), str(arch), str(kind))
    if identity in seen:
        errors.append(
            f"{where}: повтор артефакта os={identity[0]} arch={identity[1]} kind={identity[2]}"
        )
    else:
        seen.add(identity)

    install = artifact.get("install")
    if install is not None and not isinstance(install, Mapping):
        errors.append(f"{where}: install должен быть таблицей")
        install = None

    if kind in _BYTES_KINDS:
        for field in ("url", "sha256"):
            if field not in artifact or not isinstance(artifact.get(field), str):
                errors.append(
                    f"{where}: для kind={kind} обязательное поле {field} "
                    "(пустая строка допустима, отсутствие — нет)"
                )

    if kind == "skill-ref":
        skill_ref = artifact.get("skill_ref")
        if not isinstance(skill_ref, str) or not skill_ref.strip():
            errors.append(f"{where}: для kind=skill-ref обязательное поле skill_ref")

    if kind == "uv-tool":
        package = (install or {}).get("package")
        if not isinstance(package, str) or not package.strip():
            errors.append(f"{where}: для kind=uv-tool обязательное поле install.package")

    if kind == "installer":
        args = (install or {}).get("args")
        if not isinstance(args, Sequence) or isinstance(args, (str, bytes)):
            errors.append(f"{where}: для kind=installer обязателен install.args (список)")
        detect = (install or {}).get("detect")
        if not isinstance(detect, Mapping) or not isinstance(detect.get("file"), str):
            errors.append(
                f"{where}: для kind=installer обязателен install.detect.file"
            )

    if kind == "mcp-spec":
        transport = (install or {}).get("transport")
        if not isinstance(transport, str) or not transport.strip():
            errors.append(f"{where}: для kind=mcp-spec обязателен install.transport")
    return errors


def render_product(product: Mapping[str, Any]) -> str:
    """Отрендерить манифест в TOML: стабильный порядок, UTF-8, inline install."""
    errors = validate_product(product)
    if errors:
        raise ValueError("манифест не проходит схему: " + "; ".join(errors))

    lines = [
        f"slug = {_toml_value(product['slug'])}",
        f"title = {_toml_value(product['title'])}",
        f"version = {_toml_value(product['version'])}",
    ]
    for component in product["components"]:
        lines.extend(["", "[[components]]"])
        lines.append(f"key = {_toml_value(component['key'])}")
        lines.append(f"type = {_toml_value(component['type'])}")
        depends_on = component.get("depends_on") or []
        if depends_on:
            lines.append(f"depends_on = {_toml_value(list(depends_on))}")
        for artifact in component["artifacts"]:
            lines.extend(["", "[[components.artifacts]]"])
            lines.append(f"os = {_toml_value(artifact['os'])}")
            lines.append(f"arch = {_toml_value(artifact['arch'])}")
            lines.append(f"kind = {_toml_value(artifact['kind'])}")
            if "url" in artifact:
                lines.append(f"url = {_toml_value(artifact['url'])}")
            if "sha256" in artifact:
                lines.append(f"sha256 = {_toml_value(artifact['sha256'])}")
            if "skill_ref" in artifact:
                lines.append(f"skill_ref = {_toml_value(artifact['skill_ref'])}")
            if "install" in artifact:
                lines.append(f"install = {_toml_value(artifact['install'])}")
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    """Значение TOML. JSON-экранирование совместимо с basic string."""
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        items = ", ".join(
            f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in sorted(value.items())
        )
        return f"{{ {items} }}"
    if isinstance(value, Sequence):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"не умею рендерить значение типа {type(value)!r}")


def _toml_key(key: str) -> str:
    if re.match(r"^[A-Za-z0-9_-]+$", key):
        return key
    return json.dumps(key, ensure_ascii=False)
