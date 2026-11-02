"""Навык как компонент продукта: делегирует существующей установке навыка.

Модель навыков не меняется (ADR): здесь нет второй установки, копирования
файлов или своего стора — вызывается та же цепочка ``_install_chain``, что у
``skillery install``, и тот же ``SkillInstaller.remove``. Поэтому поведение
навыков остаётся прежним, а продукт лишь называет, какой навык ему нужен.
"""
from __future__ import annotations

from ..plan import Component
from .base import (
    ComponentResult,
    InstallContext,
    ProductError,
    require_artifact,
)


class SkillHandler:
    component_type = "skill"

    def plan(self, component: Component, ctx: InstallContext) -> list[str]:
        artifact = require_artifact(component)
        return [f"skillery install {artifact.skill_ref} (существующая цепочка)"]

    def verify(self, component: Component, ctx: InstallContext) -> bool:
        slug = self._skill_ref(component)
        return (ctx.paths.skill_store / slug).is_dir()

    async def install(
        self, component: Component, ctx: InstallContext
    ) -> ComponentResult:
        slug = self._skill_ref(component)
        target = ctx.paths.skill_store / slug
        if target.is_dir():
            return ComponentResult(
                key=component.key,
                type=self.component_type,
                status="already-installed",
                path=str(target),
            )
        if ctx.skill_installer is not None:
            await ctx.skill_installer(slug, ctx)
        else:
            await _default_install(slug, ctx)
        return ComponentResult(
            key=component.key,
            type=self.component_type,
            status="installed",
            path=str(target),
        )

    async def remove(self, component: Component, ctx: InstallContext) -> ComponentResult:
        slug = self._skill_ref(component)
        target = ctx.paths.skill_store / slug
        if not target.is_dir():
            return ComponentResult(
                key=component.key,
                type=self.component_type,
                status="absent",
                path=str(target),
            )
        if ctx.skill_remover is not None:
            ctx.skill_remover(slug)
        else:
            _default_remove(slug)
        return ComponentResult(
            key=component.key,
            type=self.component_type,
            status="removed",
            path=str(target),
        )

    def _skill_ref(self, component: Component) -> str:
        artifact = require_artifact(component)
        if not artifact.skill_ref:
            raise ProductError(
                f"skill/{component.key}: артефакт skill-ref без slug навыка"
            )
        return artifact.skill_ref


async def _default_install(slug: str, ctx: InstallContext) -> None:
    """Существующая цепочка установки навыка — без второго пути."""
    from skillery_cli.__main__ import _install_chain
    from skillery_cli.commands import _common
    from skillery_cli.config import ClientConfig
    from skillery_cli.core.agents import get_target

    cfg = ClientConfig.load()
    access = _common.get_access_token()
    await _install_chain(
        cfg,
        access,
        slug=slug,
        channel="published",
        scope="global",
        project_path=None,
        force=False,
        agent_target=get_target(cfg.agent),
        initiator="cli",
    )


def _default_remove(slug: str) -> None:
    """Существующее снятие навыка: SkillInstaller, а не своё удаление папки."""
    from skillery_cli.config import ClientConfig
    from skillery_cli.core.agents import get_target
    from skillery_cli.core.installer import SkillInstaller

    cfg = ClientConfig.load()
    installer = SkillInstaller(get_target(cfg.agent), cfg.effective_store_dir())
    installer.remove(slug=slug, purge=True)
