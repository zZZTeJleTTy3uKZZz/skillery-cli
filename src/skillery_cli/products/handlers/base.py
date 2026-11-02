"""Общий контракт обработчиков: ``plan → install → verify → remove``.

Обработчик ставит ОДИН компонент и не знает про продукт и порядок: порядок,
состояние и отчёт — дело движка. Так добавление нового ``Component.type`` не
трогает ни команды, ни состояние.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..download import download, fetch_verified, safe_extract
from ..paths import HomePaths
from ..plan import Artifact, Component
from ..runner import CommandResult, CommandRunner, SubprocessRunner
from ..state import InstalledState

#: Установщик навыка: сигнатура существующей цепочки, завёрнутая адаптером.
SkillInstallerFn = Callable[[str, "InstallContext"], Awaitable[None]]
SkillRemoverFn = Callable[[str], None]


class ProductError(RuntimeError):
    """Компонент нельзя поставить/снять по понятной человеку причине."""


@dataclass
class ComponentResult:
    """Итог одного компонента: что сделали и где это лежит."""

    key: str
    type: str
    status: str
    detail: str = ""
    path: str = ""
    actions: tuple[str, ...] = ()


class ComponentHandler(Protocol):
    """Обработчик по ``Component.type``."""

    component_type: str

    def plan(self, component: Component, ctx: InstallContext) -> list[str]:
        """Что будет сделано (для ``--dry-run``), без побочных эффектов."""

    def verify(self, component: Component, ctx: InstallContext) -> bool:
        """Компонент уже стоит и годен? Проверка без установки."""

    async def install(
        self, component: Component, ctx: InstallContext
    ) -> ComponentResult:
        """Поставить компонент идемпотентно."""

    async def remove(
        self, component: Component, ctx: InstallContext
    ) -> ComponentResult:
        """Снять компонент, не трогая чужое."""


@dataclass
class InstallContext:
    """Всё, что обработчику нужно от машины и от тестов."""

    paths: HomePaths
    runner: CommandRunner = field(default_factory=SubprocessRunner)
    state: InstalledState = field(default_factory=InstalledState.load)
    dry_run: bool = False
    downloader: Callable[..., Path] = download
    skill_installer: SkillInstallerFn | None = None
    skill_remover: SkillRemoverFn | None = None
    message: Callable[[str], None] = lambda _text: None


def require_artifact(component: Component) -> Artifact:
    """Артефакт обязателен: компонент без него — это ``skipped`` плана."""
    if component.artifact is None:
        raise ProductError(
            f"у компонента {component.key!r} нет артефакта под эту ОС/архитектуру"
        )
    return component.artifact


def artifact_install(artifact: Artifact) -> Mapping[str, Any]:
    install = artifact.install
    return install if isinstance(install, Mapping) else {}


def install_str(artifact: Artifact, key: str) -> str:
    value = artifact_install(artifact).get(key)
    return value if isinstance(value, str) else ""


def install_list(artifact: Artifact, key: str) -> list[str]:
    value = artifact_install(artifact).get(key)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [str(item) for item in value]
    return []


def run_checked(
    ctx: InstallContext,
    argv: Sequence[str],
    *,
    timeout: float | None = None,
) -> CommandResult:
    result = ctx.runner.run(argv, timeout=timeout)
    if not result.ok:
        from ..runner import CommandError

        raise CommandError(result)
    return result


def expand_path(value: str) -> Path:
    """Развернуть переменные окружения и ``~`` в пути из install-метаданных."""
    return Path(os.path.expandvars(value)).expanduser()


def install_archive(
    component: Component,
    ctx: InstallContext,
    target: Path,
    *,
    dir_name: str = "",
) -> Path:
    """Скачать архив, проверить sha256, распаковать и заменить каталог целиком.

    Распаковка идёт во временный каталог: пока байты не проверены и не разобраны,
    трогать существующую установку нельзя. Затем каталог заменяется атомарно
    настолько, насколько это позволяет файловая система.
    """
    artifact = require_artifact(component)
    with tempfile.TemporaryDirectory(prefix="skillery-artifact-") as tmp:
        workdir = Path(tmp)
        downloaded = fetch_verified(
            artifact.url, artifact.sha256, workdir, downloader=ctx.downloader
        )
        unpacked = safe_extract(downloaded, workdir / "unpacked")
        source = unpacked
        if dir_name and (unpacked / dir_name).is_dir():
            source = unpacked / dir_name
        else:
            children = [item for item in unpacked.iterdir() if item.is_dir()]
            if len(children) == 1 and not any(
                item.is_file() for item in unpacked.iterdir()
            ):
                source = children[0]
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    return target
