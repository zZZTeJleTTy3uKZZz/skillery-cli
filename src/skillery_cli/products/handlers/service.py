"""Служба ОС: установщик скачивается, проверяется и запускается тихо.

Порядок жёсткий: скачать → проверить sha256 → только потом запустить. Проверка
``detect`` после запуска — часть установки: установщик мог «отработать» и ничего
не поставить, и молчаливое «готово» здесь дороже ошибки.
"""
from __future__ import annotations

import shlex
import tempfile
from collections.abc import Mapping
from pathlib import Path

from ..download import fetch_verified
from ..plan import Artifact, Component
from ..runner import CommandError
from .base import (
    ComponentResult,
    InstallContext,
    ProductError,
    artifact_install,
    expand_path,
    install_list,
    require_artifact,
)


class ServiceHandler:
    component_type = "service"

    def plan(self, component: Component, ctx: InstallContext) -> list[str]:
        artifact = require_artifact(component)
        detect = _detect(artifact)
        return [
            f"скачать установщик {artifact.url}",
            "проверить sha256",
            "запустить тихо: " + " ".join(install_list(artifact, "args")),
            f"проверить {detect}",
        ]

    def verify(self, component: Component, ctx: InstallContext) -> bool:
        artifact = require_artifact(component)
        detect_file = _detect_file(artifact)
        if detect_file and expand_path(detect_file).exists():
            return True
        detect_command = _detect_command(artifact)
        if detect_command:
            argv = shlex.split(detect_command)
            return bool(argv) and ctx.runner.run(argv).ok
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
                detail=_detect(artifact),
            )
        if not _detect_file(artifact) and not _detect_command(artifact):
            raise ProductError(
                f"service/{component.key}: в install-метаданных нет detect — "
                "установку нельзя проверить"
            )

        with tempfile.TemporaryDirectory(prefix="skillery-installer-") as tmp:
            installer = fetch_verified(
                artifact.url,
                artifact.sha256,
                Path(tmp),
                downloader=ctx.downloader,
            )
            argv = [str(installer), *install_list(artifact, "args")]
            result = ctx.runner.run(argv, timeout=1800)
            if not result.ok:
                raise CommandError(result)

        if not self.verify(component, ctx):
            raise ProductError(
                f"service/{component.key}: установщик завершился, но detect не найден "
                f"({_detect(artifact)})"
            )
        return ComponentResult(
            key=component.key,
            type=self.component_type,
            status="installed",
            detail=_detect(artifact),
        )

    async def remove(self, component: Component, ctx: InstallContext) -> ComponentResult:
        artifact = require_artifact(component)
        uninstall_args = install_list(artifact, "uninstall_args")
        if not uninstall_args:
            return ComponentResult(
                key=component.key,
                type=self.component_type,
                status="skipped",
                detail="установщик не объявляет uninstall_args — снимайте через ОС",
            )
        if not artifact.url:
            return ComponentResult(
                key=component.key,
                type=self.component_type,
                status="skipped",
                detail="нет url установщика для удаления",
            )
        with tempfile.TemporaryDirectory(prefix="skillery-installer-") as tmp:
            installer = fetch_verified(
                artifact.url,
                artifact.sha256,
                Path(tmp),
                downloader=ctx.downloader,
            )
            result = ctx.runner.run([str(installer), *uninstall_args], timeout=1800)
            if not result.ok:
                raise CommandError(result)
        return ComponentResult(
            key=component.key, type=self.component_type, status="removed"
        )


def _detect(artifact: Artifact) -> str:
    detect_file = _detect_file(artifact)
    if detect_file:
        return detect_file
    return _detect_command(artifact)


def _detect_file(artifact: Artifact) -> str:
    detect = _detect_spec(artifact)
    value = detect.get("file")
    return value if isinstance(value, str) else ""


def _detect_command(artifact: Artifact) -> str:
    detect = _detect_spec(artifact)
    value = detect.get("command")
    return value if isinstance(value, str) else ""


def _detect_spec(artifact: Artifact) -> Mapping[str, object]:
    install = artifact_install(artifact)
    detect = install.get("detect")
    return detect if isinstance(detect, Mapping) else {}
