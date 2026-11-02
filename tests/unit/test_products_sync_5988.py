"""``skillery sync`` ставит назначенные продукты и подтягивает версии (#5988).

Контракт backend — ``GET /me/products/install-plan`` (ADR-0001, реализация
skillery-backend). Тесты идут через НАСТОЯЩИЙ ``HubClient`` под respx: фейковый
клиент не поймал бы ни опечатку в пути, ни потерю query-параметров, а именно
ими ломается доставка плана на устройство. Артефакты при этом локальные
(``file://``): сеть нужна только там, где её действительно нет в контракте.

Гарантии:
- чистый HOME → всё назначенное ставится;
- повтор идемпотентен и НЕ качает артефакты (products.json не меняется);
- новая версия обновляет только изменившиеся/новые компоненты, снимая те,
  которых в новой версии нет;
- откат на более старую версию не делается;
- снятое с назначения остаётся, пока не передан ``--prune``;
- старый backend (404 на маршрут) не роняет синхронизацию навыков.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import Response

from skillery_cli import output as output_module
from skillery_cli.config import ClientConfig
from skillery_cli.core.identity import device_uid
from skillery_cli.core.transport import HubClient
from skillery_cli.products.handlers.base import InstallContext
from skillery_cli.products.paths import current_arch, current_os, home_paths
from skillery_cli.products.runner import FakeRunner
from skillery_cli.products.state import InstalledState
from skillery_cli.products.sync import (
    InstallPlanUnavailableError,
    sync_products,
)

BASE = "http://localhost:8000"
PLAN_PATH = "/me/products/install-plan"


@pytest.fixture(autouse=True)
def _clean_product_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DSH_HOME", "DSH_PROFILE", "CLAUDE_CONFIG_DIR", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def ctx() -> InstallContext:
    return InstallContext(
        paths=home_paths(),
        runner=FakeRunner(),
        state=InstalledState.load(),
    )


def _artifact(tmp_path: Path, name: str, payload: bytes) -> tuple[str, str]:
    path = tmp_path / f"{name}.bin"
    path.write_bytes(payload)
    return path.as_uri(), hashlib.sha256(payload).hexdigest()


def _cli_component(key: str, url: str, sha256: str, bin_name: str) -> dict[str, Any]:
    return {
        "key": key,
        "type": "cli",
        "version": "1.0.0",
        "artifact": {
            "kind": "binary",
            "os": "linux",
            "arch": "amd64",
            "url": url,
            "sha256": sha256,
            "install": {"bin": bin_name},
        },
    }


def _product(slug: str, version: str, *components: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": slug,
        "version": version,
        "components": list(components),
        "skipped": [],
    }


def _plan(*products: dict[str, Any]) -> dict[str, Any]:
    return {"products": list(products)}


async def _sync(
    plan: dict[str, Any],
    ctx: InstallContext,
    *,
    dry_run: bool = False,
    prune: bool = False,
) -> tuple[Any, Any]:
    """Прогнать sync против respx-плана; вернуть (отчёт, respx-маршрут)."""
    with respx.mock(base_url=BASE) as router:
        route = router.get(PLAN_PATH).mock(return_value=Response(200, json=plan))
        client = HubClient(base_url=BASE)
        try:
            report = await sync_products(
                client, ctx, dry_run=dry_run, prune=prune
            )
        finally:
            await client.close()
    return report, route


async def test_clean_home_installs_all_assigned_products(
    ctx: InstallContext, tmp_path: Path
) -> None:
    alpha_url, alpha_sha = _artifact(tmp_path, "alpha", b"alpha-v1")
    beta_url, beta_sha = _artifact(tmp_path, "beta", b"beta-v1")
    plan = _plan(
        _product("alpha", "1.0.0", _cli_component("alpha-cli", alpha_url, alpha_sha, "alpha")),
        _product("beta", "1.0.0", _cli_component("beta-cli", beta_url, beta_sha, "beta")),
    )

    report, _ = await _sync(plan, ctx)

    assert report.installed == ["alpha", "beta"]
    assert report.updated == [] and report.already == []
    assert (ctx.paths.bin_dir / "alpha").read_bytes() == b"alpha-v1"
    assert (ctx.paths.bin_dir / "beta").read_bytes() == b"beta-v1"
    state = InstalledState.load()
    assert state.get_product("alpha")["version"] == "1.0.0"
    assert state.get_product("beta")["components"]["beta-cli"]["type"] == "cli"


async def test_plan_query_carries_platform_and_device(
    ctx: InstallContext, tmp_path: Path
) -> None:
    url, sha = _artifact(tmp_path, "alpha", b"payload")
    plan = _plan(_product("alpha", "1.0.0", _cli_component("alpha-cli", url, sha, "alpha")))

    _, route = await _sync(plan, ctx)

    query = route.calls[0].request.url.params
    assert query["os"] == current_os()
    assert query["arch"] == current_arch()
    assert query["device_id"] == device_uid()


async def test_repeat_sync_is_idempotent_and_does_not_fetch_artifacts(
    ctx: InstallContext, tmp_path: Path
) -> None:
    url, sha = _artifact(tmp_path, "alpha", b"alpha-v1")
    plan = _plan(_product("alpha", "1.0.0", _cli_component("alpha-cli", url, sha, "alpha")))
    await _sync(plan, ctx)
    state_file = ctx.paths.products_file
    before = state_file.read_bytes()

    def _forbidden(*args: Any, **kwargs: Any) -> Path:
        raise AssertionError("повторный sync не должен качать артефакты")

    again = InstallContext(
        paths=home_paths(), runner=FakeRunner(), state=InstalledState.load()
    )
    again.downloader = _forbidden
    report, _ = await _sync(plan, again)

    assert report.already == ["alpha"]
    assert report.installed == [] and report.updated == []
    assert state_file.read_bytes() == before, "совпадающая версия не трогает products.json"


async def test_new_version_updates_only_changed_and_new_components(
    ctx: InstallContext, tmp_path: Path
) -> None:
    stable_url, stable_sha = _artifact(tmp_path, "stable", b"stable")
    old_url, old_sha = _artifact(tmp_path, "tool", b"tool-v1")
    new_url, new_sha = _artifact(tmp_path, "tool2", b"tool-v2")
    extra_url, extra_sha = _artifact(tmp_path, "extra", b"extra-v1")

    v1 = _plan(
        _product(
            "alpha",
            "1.0.0",
            _cli_component("stable-cli", stable_url, stable_sha, "stable"),
            _cli_component("tool-cli", old_url, old_sha, "tool"),
            _cli_component("legacy-cli", old_url, old_sha, "legacy"),
        )
    )
    await _sync(v1, ctx)
    assert (ctx.paths.bin_dir / "legacy").exists()

    downloaded: list[str] = []

    def _counting(url: str, dest: Path, *, timeout: float = 0) -> Path:
        downloaded.append(url)
        from skillery_cli.products.download import download

        return download(url, dest, timeout=timeout)

    ctx.downloader = _counting
    v2 = _plan(
        _product(
            "alpha",
            "2.0.0",
            _cli_component("stable-cli", stable_url, stable_sha, "stable"),
            _cli_component("tool-cli", new_url, new_sha, "tool"),
            _cli_component("extra-cli", extra_url, extra_sha, "extra"),
        )
    )

    report, _ = await _sync(v2, ctx)

    assert report.updated == ["alpha"]
    assert (ctx.paths.bin_dir / "tool").read_bytes() == b"tool-v2"
    assert (ctx.paths.bin_dir / "extra").read_bytes() == b"extra-v1"
    assert not (ctx.paths.bin_dir / "legacy").exists(), "компонент вне новой версии снят"
    assert old_url not in downloaded, "устаревший артефакт не качается"
    assert stable_url not in downloaded, "неизменившийся компонент не переустанавливается"
    state = InstalledState.load()
    assert state.get_product("alpha")["version"] == "2.0.0"
    assert "legacy-cli" not in state.get_product("alpha")["components"]


async def test_downgrade_is_not_applied(ctx: InstallContext, tmp_path: Path) -> None:
    old_url, old_sha = _artifact(tmp_path, "v1", b"v1")
    new_url, new_sha = _artifact(tmp_path, "v2", b"v2")
    await _sync(
        _plan(_product("alpha", "2.0.0", _cli_component("alpha-cli", new_url, new_sha, "alpha"))),
        ctx,
    )

    report, _ = await _sync(
        _plan(_product("alpha", "1.0.0", _cli_component("alpha-cli", old_url, old_sha, "alpha"))),
        ctx,
    )

    assert report.skipped == ["alpha"]
    assert (ctx.paths.bin_dir / "alpha").read_bytes() == b"v2"
    assert InstalledState.load().get_product("alpha")["version"] == "2.0.0"


async def test_unassigned_products_are_kept_by_default(
    ctx: InstallContext, tmp_path: Path
) -> None:
    url, sha = _artifact(tmp_path, "alpha", b"alpha")
    await _sync(
        _plan(_product("alpha", "1.0.0", _cli_component("alpha-cli", url, sha, "alpha"))),
        ctx,
    )
    beta_url, beta_sha = _artifact(tmp_path, "beta", b"beta")

    report, _ = await _sync(
        _plan(_product("beta", "1.0.0", _cli_component("beta-cli", beta_url, beta_sha, "beta"))),
        ctx,
    )

    assert report.kept == ["alpha"]
    assert report.pruned == []
    assert (ctx.paths.bin_dir / "alpha").exists(), "снятое с назначения не удаляется само"
    assert (ctx.paths.bin_dir / "beta").exists()


async def test_prune_removes_only_unassigned(
    ctx: InstallContext, tmp_path: Path
) -> None:
    url, sha = _artifact(tmp_path, "alpha", b"alpha")
    await _sync(
        _plan(_product("alpha", "1.0.0", _cli_component("alpha-cli", url, sha, "alpha"))),
        ctx,
    )
    beta_url, beta_sha = _artifact(tmp_path, "beta", b"beta")

    report, _ = await _sync(
        _plan(_product("beta", "1.0.0", _cli_component("beta-cli", beta_url, beta_sha, "beta"))),
        ctx,
        prune=True,
    )

    assert report.pruned == ["alpha"]
    assert report.kept == []
    assert not (ctx.paths.bin_dir / "alpha").exists()
    assert (ctx.paths.bin_dir / "beta").exists()
    assert InstalledState.load().get_product("alpha") is None


async def test_dry_run_touches_nothing(ctx: InstallContext, tmp_path: Path) -> None:
    url, sha = _artifact(tmp_path, "alpha", b"alpha")
    plan = _plan(_product("alpha", "1.0.0", _cli_component("alpha-cli", url, sha, "alpha")))

    report, _ = await _sync(plan, ctx, dry_run=True)

    assert report.installed == ["alpha"]
    assert not (ctx.paths.bin_dir / "alpha").exists()
    assert not ctx.paths.products_file.exists()
    assert report.to_dict()["dry_run"] is True


async def test_404_raises_unavailable_with_human_text(
    ctx: InstallContext,
) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get(PLAN_PATH).mock(
            return_value=Response(404, json={"code": "NOT_FOUND", "message": "нет"})
        )
        client = HubClient(base_url=BASE)
        try:
            with pytest.raises(InstallPlanUnavailableError, match="404"):
                await sync_products(client, ctx)
        finally:
            await client.close()


def test_current_platform_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    import platform as platform_mod
    import sys as sys_mod

    import skillery_cli.products.paths as paths_mod

    monkeypatch.setattr(sys_mod, "platform", "linux")
    monkeypatch.setattr(platform_mod, "machine", lambda: "x86_64")
    assert (current_os(), current_arch()) == ("linux", "amd64")

    monkeypatch.setattr(sys_mod, "platform", "win32")
    monkeypatch.setattr(platform_mod, "machine", lambda: "AMD64")
    assert (current_os(), current_arch()) == ("windows", "amd64")

    monkeypatch.setattr(sys_mod, "platform", "darwin")
    monkeypatch.setattr(platform_mod, "machine", lambda: "arm64")
    assert (current_os(), current_arch()) == ("macos", "arm64")

    monkeypatch.setattr(paths_mod, "platform", platform_mod)
    monkeypatch.setattr(platform_mod, "machine", lambda: "aarch64")
    assert (current_os(), current_arch()) == ("macos", "arm64")


def test_unknown_platform_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    import platform as platform_mod
    import sys as sys_mod

    import skillery_cli.products.paths as paths_mod

    monkeypatch.setattr(paths_mod, "platform", platform_mod)
    monkeypatch.setattr(sys_mod, "platform", "beos")
    with pytest.raises(ValueError, match="beos"):
        current_os()


# ── встроено в `skillery sync` и демон ─────────────────────────────────────


def _seed_skill(store: Path, slug: str) -> None:
    skill = store / slug
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("hi", encoding="utf-8")
    (skill / "_skill_meta.json").write_text("{}", encoding="utf-8")


def test_legacy_backend_keeps_skills_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """404 на install-plan: навыки синхронизированы, шаг продуктов — предупреждение."""
    import skillery_cli.__main__ as main_mod
    from skillery_cli.core import project_manifest as pm
    from skillery_cli.core.agents import ClaudeCodeTarget
    from skillery_cli.core.installer import SkillInstaller

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    store = tmp_path / "store"
    project = tmp_path / "proj"
    project.mkdir()
    _seed_skill(store, "demo")
    pm.add(project, "demo")
    SkillInstaller(target, store_dir=store).link_existing(
        "demo", project=project, force=True
    )

    cfg = ClientConfig(base_url=BASE, store_dir=str(store))
    cfg.user_email = "x@y.io"
    cfg.permissions = ["skill.install"]
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "load_tokens", lambda email: ("tok", "rt"))
    monkeypatch.setattr(output_module, "_mode", "json")

    with respx.mock(base_url=BASE) as router:
        router.get(PLAN_PATH).mock(
            return_value=Response(404, json={"code": "NOT_FOUND", "message": "нет"})
        )
        main_mod.cmd_sync(project=project, prune=False, agent=None, channel="published")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert "demo" in payload["linked"]
    assert "warning" in payload["products"]
    assert "404" in payload["products"]["warning"]


def test_sync_installs_products_after_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """`skillery sync` ставит назначенные продукты — на чистом HOME, после навыков."""
    import skillery_cli.__main__ as main_mod
    from skillery_cli.core.agents import ClaudeCodeTarget

    target = ClaudeCodeTarget(root=tmp_path / ".claude")
    project = tmp_path / "proj"
    project.mkdir()
    cfg = ClientConfig(base_url=BASE, store_dir=str(tmp_path / "store"))
    cfg.user_email = "x@y.io"
    cfg.permissions = ["skill.install"]
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(main_mod, "get_target", lambda name: target)
    monkeypatch.setattr(main_mod, "load_tokens", lambda email: ("tok", "rt"))
    monkeypatch.setattr(output_module, "_mode", "json")

    url, sha = _artifact(tmp_path, "alpha", b"alpha")
    plan = _plan(_product("alpha", "1.0.0", _cli_component("alpha-cli", url, sha, "alpha")))
    with respx.mock(base_url=BASE) as router:
        router.get(PLAN_PATH).mock(return_value=Response(200, json=plan))
        main_mod.cmd_sync(project=project, prune=False, agent=None, channel="published")

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["products"]["installed"] == ["alpha"]
    assert (home_paths().bin_dir / "alpha").read_bytes() == b"alpha"


async def test_daemon_step_is_best_effort_on_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import skillery_cli.__main__ as main_mod

    cfg = ClientConfig(base_url=BASE)
    cfg.user_email = "x@y.io"
    cfg.permissions = ["skill.install"]
    with respx.mock(base_url=BASE) as router:
        router.get(PLAN_PATH).mock(
            return_value=Response(404, json={"code": "NOT_FOUND", "message": "нет"})
        )
        report = await main_mod._reconcile_assigned_products(cfg, "tok")

    assert "404" in report["warning"]
    assert report["installed"] == [] and report["updated"] == []


async def test_products_sync_flag_disables_the_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import skillery_cli.__main__ as main_mod

    cfg = ClientConfig(base_url=BASE)
    cfg.products_sync = False
    report = await main_mod._reconcile_assigned_products(cfg, "tok")
    assert report == {"disabled": True}


def test_sync_no_products_flag_skips_the_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Явный ``--no-products`` сильнее конфига: шаг не трогает ни сеть, ни HOME."""
    import skillery_cli.__main__ as main_mod
    from skillery_cli.core.agents import ClaudeCodeTarget

    project = tmp_path / "proj"
    project.mkdir()
    cfg = ClientConfig(base_url=BASE, store_dir=str(tmp_path / "store"))
    cfg.user_email = "x@y.io"
    cfg.permissions = ["skill.install"]
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(
        main_mod, "get_target",
        lambda name: ClaudeCodeTarget(root=tmp_path / ".claude"),
    )
    monkeypatch.setattr(main_mod, "load_tokens", lambda email: ("tok", "rt"))
    monkeypatch.setattr(output_module, "_mode", "json")

    called: list[str] = []

    async def _fake_step(cfg, access, **kwargs):  # noqa: ANN001
        called.append("products")
        return {}

    monkeypatch.setattr(main_mod, "_reconcile_assigned_products", _fake_step)

    main_mod.cmd_sync(
        project=project, prune=False, agent=None, channel="published", products=False
    )

    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert called == []
    assert "products" not in payload


def test_products_sync_enabled_by_default() -> None:
    assert ClientConfig().products_sync is True


# ── команда `skillery product sync` ────────────────────────────────────────


def _app():  # noqa: ANN202
    import typer

    from skillery_cli.commands import product as product_mod

    root = typer.Typer()
    product_mod.register(root)
    return root


def _logged_in_cfg(tmp_path: Path) -> ClientConfig:
    cfg = ClientConfig(base_url=BASE, store_dir=str(tmp_path / "store"))
    cfg.user_email = "x@y.io"
    cfg.permissions = ["skill.install"]
    return cfg


def test_product_sync_command_prints_json_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from typer.testing import CliRunner

    from skillery_cli.commands import product as product_mod

    url, sha = _artifact(tmp_path, "alpha", b"alpha")
    plan = _plan(_product("alpha", "1.0.0", _cli_component("alpha-cli", url, sha, "alpha")))
    cfg = _logged_in_cfg(tmp_path)
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(product_mod, "load_tokens", lambda email: ("tok", "rt"))
    monkeypatch.setattr(output_module, "_mode", "text")

    with respx.mock(base_url=BASE) as router:
        router.get(PLAN_PATH).mock(return_value=Response(200, json=plan))
        result = CliRunner().invoke(_app(), ["product", "sync"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["installed"] == ["alpha"]
    assert (home_paths().bin_dir / "alpha").exists()


def test_product_sync_dry_run_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from skillery_cli.commands import product as product_mod

    url, sha = _artifact(tmp_path, "alpha", b"alpha")
    plan = _plan(_product("alpha", "1.0.0", _cli_component("alpha-cli", url, sha, "alpha")))
    cfg = _logged_in_cfg(tmp_path)
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(product_mod, "load_tokens", lambda email: ("tok", "rt"))

    with respx.mock(base_url=BASE) as router:
        router.get(PLAN_PATH).mock(return_value=Response(200, json=plan))
        result = CliRunner().invoke(_app(), ["product", "sync", "--dry-run"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["dry_run"] is True
    assert not (home_paths().bin_dir / "alpha").exists()
    assert not home_paths().products_file.exists()


def test_product_sync_requires_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    cfg = ClientConfig(base_url=BASE, store_dir=str(tmp_path / "store"))
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))

    result = CliRunner().invoke(_app(), ["product", "sync"])

    assert result.exit_code == 1
    assert "login" in result.output.lower()


def test_product_sync_reports_legacy_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standalone-команда не глотает 404: человек должен знать, что сервер старый."""
    from typer.testing import CliRunner

    from skillery_cli.commands import product as product_mod

    cfg = _logged_in_cfg(tmp_path)
    monkeypatch.setattr(ClientConfig, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(product_mod, "load_tokens", lambda email: ("tok", "rt"))

    with respx.mock(base_url=BASE) as router:
        router.get(PLAN_PATH).mock(
            return_value=Response(404, json={"code": "NOT_FOUND", "message": "нет"})
        )
        result = CliRunner().invoke(_app(), ["product", "sync"])

    assert result.exit_code == 1
    assert "404" in (result.output + (result.stderr or ""))


def test_products_sync_flag_survives_config_roundtrip(tmp_path: Path) -> None:
    """Выключенный шаг обязан пережить save/load, иначе флаг — украшение."""
    path = tmp_path / "config.toml"
    cfg = ClientConfig(base_url="http://localhost:8000", products_sync=False)
    cfg.save(path)

    assert ClientConfig.load(path).products_sync is False
