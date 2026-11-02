"""CLI-компонент: ``uv tool install`` или бинарь в пользовательский bin.

Идемпотентность проверяется ДО установки: ``uv tool list``/наличие файла
отвечают, стоит ли уже. Поэтому повторный ``product install`` не переставляет
пакет и не меняет бинарь.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from ..download import fetch_verified, is_archive, safe_extract
from ..plan import Artifact, Component
from .base import (
    ComponentResult,
    InstallContext,
    ProductError,
    install_list,
    install_str,
    require_artifact,
    run_checked,
)


class CliHandler:
    component_type = "cli"

    def plan(self, component: Component, ctx: InstallContext) -> list[str]:
        artifact = require_artifact(component)
        if artifact.kind == "uv-tool":
            return [f"uv tool install {self._uv_spec(artifact)}"]
        return [
            f"скачать {artifact.url}",
            "проверить sha256",
            f"положить бинарь в {self._target(component, artifact, ctx)}",
        ]

    def verify(self, component: Component, ctx: InstallContext) -> bool:
        artifact = require_artifact(component)
        if artifact.kind == "uv-tool":
            return self._uv_installed(ctx, artifact)
        if artifact.kind == "binary":
            return self._target(component, artifact, ctx).is_file()
        return False

    async def install(
        self, component: Component, ctx: InstallContext
    ) -> ComponentResult:
        artifact = require_artifact(component)
        if self.verify(component, ctx):
            return ComponentResult(
                key=component.key,
                type=self.component_type,
                status="already-installed",
                path=self._target(component, artifact, ctx)
                if artifact.kind == "binary"
                else "",
            )

        if artifact.kind == "uv-tool":
            argv = ["uv", "tool", "install", "--force"]
            index_url = install_str(artifact, "index_url")
            if index_url:
                argv.extend(["--index-url", index_url])
            argv.append(self._uv_spec(artifact))
            run_checked(ctx, argv)
            return ComponentResult(
                key=component.key, type=self.component_type, status="installed"
            )

        if artifact.kind == "binary":
            target = self._install_binary(component, ctx)
            return ComponentResult(
                key=component.key,
                type=self.component_type,
                status="installed",
                path=str(target),
            )

        raise ProductError(
            f"cli/{component.key}: kind {artifact.kind!r} не поддерживается "
            "(ожидали uv-tool или binary)"
        )

    async def remove(self, component: Component, ctx: InstallContext) -> ComponentResult:
        artifact = require_artifact(component)
        if artifact.kind == "uv-tool":
            package = install_str(artifact, "package")
            if not package:
                return ComponentResult(
                    key=component.key,
                    type=self.component_type,
                    status="skipped",
                    detail="в артефакте нет имени пакета",
                )
            if not self._uv_installed(ctx, artifact):
                return ComponentResult(
                    key=component.key, type=self.component_type, status="absent"
                )
            run_checked(ctx, ["uv", "tool", "uninstall", package])
            return ComponentResult(
                key=component.key, type=self.component_type, status="removed"
            )

        if artifact.kind == "binary":
            target = self._target(component, artifact, ctx)
            if not target.exists():
                return ComponentResult(
                    key=component.key, type=self.component_type, status="absent"
                )
            target.unlink()
            return ComponentResult(
                key=component.key,
                type=self.component_type,
                status="removed",
                path=str(target),
            )

        return ComponentResult(
            key=component.key, type=self.component_type, status="skipped"
        )

    # ── внутренности ────────────────────────────────────────────────────────

    def _uv_spec(self, artifact: Artifact) -> str:
        package = install_str(artifact, "package")
        if not package:
            raise ProductError("uv-tool: в install-метаданных нет package")
        extras = install_list(artifact, "extras")
        return f"{package}[{','.join(extras)}]" if extras else package

    def _uv_installed(self, ctx: InstallContext, artifact: Artifact) -> bool:
        package = install_str(artifact, "package").split("[", 1)[0]
        result = ctx.runner.run(["uv", "tool", "list"])
        if not result.ok:
            return False
        for line in result.stdout.splitlines():
            name = line.strip().split(maxsplit=1)[0] if line.strip() else ""
            if name == package:
                return True
        return False

    def _target(
        self, component: Component, artifact: Artifact, ctx: InstallContext
    ) -> Path:
        bin_name = install_str(artifact, "bin") or component.key
        return ctx.paths.bin_dir / bin_name

    def _install_binary(self, component: Component, ctx: InstallContext) -> Path:
        artifact = require_artifact(component)
        target = self._target(component, artifact, ctx)
        with tempfile.TemporaryDirectory(prefix="skillery-artifact-") as tmp:
            workdir = Path(tmp)
            downloaded = fetch_verified(
                artifact.url,
                artifact.sha256,
                workdir,
                downloader=ctx.downloader,
            )
            source = downloaded
            if is_archive(downloaded):
                unpacked = safe_extract(downloaded, workdir / "unpacked")
                source = _find_member(unpacked, target.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            os.chmod(target, 0o755)
        return target


def _find_member(root: Path, name: str) -> Path:
    matches = sorted(path for path in root.rglob(name) if path.is_file())
    if not matches:
        raise ProductError(f"в архиве нет файла {name!r}")
    return matches[0]
