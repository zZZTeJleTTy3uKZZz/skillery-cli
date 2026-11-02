"""Исполнение install-plan: порядок, идемпотентность, dry-run, снятие (#5985).

Движок — то место, где компоненты становятся продуктом: он держит порядок
зависимостей, пишет состояние и не валит весь план из-за одного сломанного
компонента. Тесты работают на реальных файлах в подменённом HOME и фейковом
runner'е: никаких uv/claude/установщиков.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from skillery_cli.products.engine import (
    install_plan,
    install_product,
    remove_product,
)
from skillery_cli.products.handlers.base import InstallContext
from skillery_cli.products.paths import home_paths
from skillery_cli.products.plan import Artifact, Component, InstallPlan, Product
from skillery_cli.products.runner import CommandResult, FakeRunner
from skillery_cli.products.state import InstalledState


@pytest.fixture(autouse=True)
def _clean_product_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DSH_HOME", "DSH_PROFILE", "CLAUDE_CONFIG_DIR", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def ctx() -> InstallContext:
    return InstallContext(
        paths=home_paths(),
        runner=FakeRunner(
            results={
                ("uv", "tool", "list"): CommandResult(
                    argv=("uv", "tool", "list"), returncode=0
                ),
            }
        ),
        state=InstalledState.load(),
    )


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _archive(tmp_path: Path, entries: dict[str, bytes]) -> tuple[str, str]:
    data = _zip_bytes(entries)
    path = tmp_path / "asset.zip"
    path.write_bytes(data)
    return path.as_uri(), hashlib.sha256(data).hexdigest()


def _run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


def _binary_cli(tmp_path: Path, *, key: str = "mytool-cli", command: str = "mytool") -> Component:
    payload = f"#!/bin/sh\necho {command}\n".encode()
    source = tmp_path / f"{command}.bin"
    source.write_bytes(payload)
    return Component(
        key=key,
        type="cli",
        artifact=Artifact(
            kind="binary",
            os="linux",
            arch="amd64",
            url=source.as_uri(),
            sha256=hashlib.sha256(payload).hexdigest(),
            install={"bin": command},
        ),
    )


def test_components_install_in_dependency_order(ctx: InstallContext, tmp_path: Path) -> None:
    first = _binary_cli(tmp_path, key="first-cli", command="first")
    second = _binary_cli(tmp_path, key="second-cli", command="second")
    # План нарочно в обратном порядке: движок обязан поставить зависимость первой.
    product = Product(
        slug="demo",
        version="1.0.0",
        components=(
            Component(
                key="second-cli",
                type=second.type,
                artifact=second.artifact,
                depends_on=("first-cli",),
            ),
            first,
        ),
    )
    result = _run(install_product(product, ctx))

    order = [component.key for component in result.components]
    assert order == ["first-cli", "second-cli"]
    assert (ctx.paths.bin_dir / "first").exists()
    assert (ctx.paths.bin_dir / "second").exists()


def test_install_is_idempotent_and_state_is_stable(ctx: InstallContext, tmp_path: Path) -> None:
    component = _binary_cli(tmp_path)
    plan = InstallPlan(products=(Product(slug="demo", version="1.0.0", components=(component,)),))

    first = _run(install_plan(plan, ctx))
    state_after_first = ctx.paths.products_file.read_bytes()
    second = _run(install_plan(plan, ctx))
    state_after_second = ctx.paths.products_file.read_bytes()

    assert first.products[0].components[0].status == "installed"
    assert second.products[0].components[0].status == "already-installed"
    assert state_after_first == state_after_second
    assert ctx.state.get_product("demo")["components"]["mytool-cli"]["type"] == "cli"


def test_dry_run_changes_nothing(ctx: InstallContext, tmp_path: Path) -> None:
    component = _binary_cli(tmp_path)
    plan = InstallPlan(products=(Product(slug="demo", version="1.0.0", components=(component,)),))
    ctx.dry_run = True

    result = _run(install_plan(plan, ctx))

    assert result.products[0].components[0].status == "planned"
    assert not (ctx.paths.bin_dir / "mytool").exists()
    assert not ctx.paths.products_file.exists()
    assert result.products[0].components[0].actions


def test_failed_component_does_not_stop_the_rest(ctx: InstallContext, tmp_path: Path) -> None:
    broken = Component(
        key="broken-cli",
        type="cli",
        artifact=Artifact(
            kind="binary",
            os="linux",
            arch="amd64",
            url="file:///nonexistent/mytool",
            sha256="0" * 64,
            install={"bin": "broken"},
        ),
    )
    good = _binary_cli(tmp_path)
    plan = InstallPlan(
        products=(
            Product(slug="demo", version="1.0.0", components=(broken, good)),
        )
    )

    result = _run(install_plan(plan, ctx))

    statuses = {item.key: item.status for item in result.products[0].components}
    assert statuses["broken-cli"] == "failed"
    assert statuses["mytool-cli"] == "installed"
    assert result.summary["failed"] == 1


def test_component_without_artifact_is_skipped(ctx: InstallContext) -> None:
    plan = InstallPlan(
        products=(
            Product(
                slug="demo",
                version="1.0.0",
                components=(Component(key="nothing", type="cli"),),
            ),
        )
    )
    result = _run(install_plan(plan, ctx))
    assert result.products[0].components[0].status == "skipped"
    assert "артефакт" in result.products[0].components[0].detail


def test_remove_product_uses_state_and_drops_it(ctx: InstallContext, tmp_path: Path) -> None:
    component = _binary_cli(tmp_path)
    plan = InstallPlan(products=(Product(slug="demo", version="1.0.0", components=(component,)),))
    _run(install_plan(plan, ctx))

    result = _run(remove_product("demo", ctx))

    assert result.status == "removed"
    assert not (ctx.paths.bin_dir / "mytool").exists()
    assert ctx.state.get_product("demo") is None
    assert InstalledState.load().products == {}


def test_remove_unknown_product_is_reported(ctx: InstallContext) -> None:
    result = _run(remove_product("ghost", ctx))
    assert result.status == "absent"


def test_plugin_dsh_state_records_artifact_for_removal(ctx: InstallContext, tmp_path: Path) -> None:
    plugin_json = b'{"id": "dsh-demo", "entry": "index.js"}'
    url, digest = _archive(
        tmp_path, {"demo/plugin.json": plugin_json, "demo/index.js": b"code"}
    )
    component = Component(
        key="demo",
        type="plugin-dsh",
        artifact=Artifact(kind="archive", os="linux", arch="any", url=url, sha256=digest),
    )
    plan = InstallPlan(products=(Product(slug="demo", version="1.0.0", components=(component,)),))
    _run(install_plan(plan, ctx))

    recorded = ctx.state.get_product("demo")["components"]["demo"]["artifact"]
    assert recorded["kind"] == "archive"
    assert recorded["url"] == url

    _run(remove_product("demo", ctx))
    assert not (ctx.paths.dsh_plugins / "demo").exists()
    assert ctx.paths.products_file.exists()
    assert InstalledState.load().products == {}


def test_plan_summary_counts(ctx: InstallContext, tmp_path: Path) -> None:
    plan = InstallPlan(
        products=(
            Product(slug="one", version="1", components=(_binary_cli(tmp_path),)),
            Product(slug="two", version="1", components=(Component(key="x", type="cli"),)),
        )
    )
    result = _run(install_plan(plan, ctx))
    assert result.summary == {
        "products": 2,
        "components": 2,
        "installed": 1,
        "already_installed": 0,
        "skipped": 1,
        "failed": 0,
    }
    assert result.to_dict()["products"][0]["slug"] == "one"
