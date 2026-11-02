"""Состояние установленного: ``~/.skillery/products.json``.

Файл — единственный ответ на вопросы ``product list`` и ``product remove``,
поэтому запись детерминирована: ключи сортируются, отступ фиксирован, а
повторная запись того же состояния НЕ трогает файл (иначе повторная установка
меняла бы mtime и «идемпотентность» была бы только на словах).
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .paths import home_paths

STATE_VERSION = 1


class StateError(RuntimeError):
    """Файл состояния не читается или имеет неожидаемый вид."""


def state_path() -> Path:
    """Путь состояния от текущего HOME/конфига (вычисляется при вызове)."""
    return home_paths().products_file


class InstalledState:
    """Реестр установленных продуктов и их компонентов."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self.products: dict[str, dict[str, Any]] = {}

    @property
    def path(self) -> Path:
        return self._path or state_path()

    @classmethod
    def load(cls, path: Path | None = None) -> InstalledState:
        state = cls(path)
        file = state.path
        if not file.exists():
            return state
        try:
            raw = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateError(f"состояние продуктов не разобрано как JSON ({file}): {exc}") from exc
        except OSError as exc:
            raise StateError(f"состояние продуктов не читается ({file}): {exc}") from exc

        products = raw.get("products") if isinstance(raw, dict) else None
        if not isinstance(products, dict):
            raise StateError(f"состояние продуктов повреждено ({file}): нет products")
        state.products = products
        return state

    def save(self) -> None:
        """Записать состояние, если оно изменилось (атомарно, UTF-8, ``\\n``)."""
        payload = {"version": STATE_VERSION, "products": self.products}
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        file = self.path
        if file.exists() and file.read_text(encoding="utf-8") == text:
            return
        file.parent.mkdir(parents=True, exist_ok=True)
        tmp = file.with_name(file.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, file)

    def get_product(self, slug: str) -> dict[str, Any] | None:
        return self.products.get(slug)

    def record_component(
        self,
        product_slug: str,
        product_version: str,
        *,
        key: str,
        component_type: str,
        status: str,
        detail: str = "",
        path: str = "",
        artifact: Mapping[str, Any] | None = None,
    ) -> None:
        product = self.products.setdefault(
            product_slug, {"version": product_version, "components": {}}
        )
        product["version"] = product_version
        components = product.setdefault("components", {})
        components[key] = {
            "type": component_type,
            "status": status,
            "detail": detail,
            "path": path,
            "artifact": dict(artifact) if artifact else {},
        }

    def drop_component(self, product_slug: str, key: str) -> None:
        product = self.products.get(product_slug)
        if product is None:
            return
        components = product.get("components", {})
        components.pop(key, None)

    def drop_product(self, slug: str) -> None:
        self.products.pop(slug, None)
