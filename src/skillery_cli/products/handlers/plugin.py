"""Плагин — ОДНА сущность, харнес выбирается полем ``hosts`` (ADR-0001).

Отдельных типов для DSH и Claude Code больше нет: компонент с ``type=plugin``
несёт список ``hosts``, и установщик раскладывает его в каждый харнес, который
ЕСТЬ на устройстве. Отсутствующий харнес — это ``skipped`` с причиной, а не
ошибка: план продукта общий для всех машин, и «нет Claude Code» — норма.

Харнес, для которого установка ещё не реализована (alice и прочие), тоже
``skipped``: плагин не потерян, он просто ждёт своей реализации. Так новый
харнес не превращает установку продукта в ``failed``.

Реализации по харнесам — в ``plugin_hosts/``; здесь только диспетчеризация,
чтобы добавление харнеса не трогало ни движок, ни команды.
"""
from __future__ import annotations

from ..plan import Component
from .base import ComponentResult, InstallContext
from .plugin_hosts import HOST_HANDLERS

#: Причина, по которой плагин не ставится в харнес (подставляется host).
UNSUPPORTED_REASON = "установка плагинов для {host} пока не поддерживается"
HARNESS_ABSENT_REASON = "харнес {host} не найден на устройстве"
NO_ARTIFACT_REASON = "нет артефакта для харнеса {host}"
NO_ARTIFACT_AT_ALL = "нет артефакта под эту ОС/архитектуру"

_INSTALLED = ("installed", "already-installed")


class PluginHandler:
    """Установщик ``type=plugin``: один компонент — несколько харнесов."""

    component_type = "plugin"

    def plan(self, component: Component, ctx: InstallContext) -> list[str]:
        lines: list[str] = []
        for host in target_hosts(component):
            obstacle = self._obstacle(component, host, ctx)
            if obstacle:
                lines.append(f"пропустить {host}: {obstacle}")
                continue
            lines.extend(
                f"{host}: {line}" for line in HOST_HANDLERS[host].plan(component, ctx)
            )
        return lines

    def verify(self, component: Component, ctx: InstallContext) -> bool:
        return all(
            self._obstacle(component, host, ctx) or HOST_HANDLERS[host].verify(component, ctx)
            for host in target_hosts(component)
        )

    async def install(
        self, component: Component, ctx: InstallContext
    ) -> ComponentResult:
        hosts_done: list[str] = []
        skipped: list[str] = []
        path = ""
        any_installed = False

        for host in target_hosts(component):
            obstacle = self._obstacle(component, host, ctx)
            if obstacle:
                skipped.append(f"{host}: {obstacle}")
                continue
            result = await HOST_HANDLERS[host].install(component, ctx)
            if result.status in _INSTALLED:
                hosts_done.append(host)
                path = path or result.path
                any_installed = any_installed or result.status == "installed"
            else:
                skipped.append(f"{host}: {result.detail or result.status}")

        if any_installed:
            status = "installed"
        elif hosts_done:
            status = "already-installed"
        else:
            status = "skipped"
        return ComponentResult(
            key=component.key,
            type=self.component_type,
            status=status,
            detail="; ".join(skipped),
            path=path,
            hosts=tuple(hosts_done),
        )

    async def remove(self, component: Component, ctx: InstallContext) -> ComponentResult:
        hosts_done: list[str] = []
        removed = False
        notes: list[str] = []
        path = ""

        for host in target_hosts(component):
            if host not in HOST_HANDLERS:
                notes.append(f"{host}: {UNSUPPORTED_REASON.format(host=host)}")
                continue
            if _artifact_host_mismatch(component, host):
                notes.append(f"{host}: {NO_ARTIFACT_REASON.format(host=host)}")
                continue
            result = await HOST_HANDLERS[host].remove(component, ctx)
            hosts_done.append(host)
            removed = removed or result.status == "removed"
            path = path or result.path
            if result.status not in ("removed", "absent"):
                notes.append(f"{host}: {result.detail or result.status}")

        return ComponentResult(
            key=component.key,
            type=self.component_type,
            status="removed" if removed else "absent",
            detail="; ".join(notes),
            path=path,
            hosts=tuple(hosts_done),
        )

    def _obstacle(
        self, component: Component, host: str, ctx: InstallContext
    ) -> str:
        """Причина не ставить плагин в харнес; пустая строка — ставим."""
        if host not in HOST_HANDLERS:
            return UNSUPPORTED_REASON.format(host=host)
        if component.artifact is None:
            return NO_ARTIFACT_AT_ALL
        if _artifact_host_mismatch(component, host):
            return NO_ARTIFACT_REASON.format(host=host)
        if not HOST_HANDLERS[host].available(ctx):
            return HARNESS_ABSENT_REASON.format(host=host)
        return ""


def target_hosts(component: Component) -> tuple[str, ...]:
    """Харнесы компонента: ``hosts``, иначе харнес артефакта, иначе все.

    Пустой ``hosts`` у плагина схема не пропустит; запасной путь нужен
    состоянию, записанному до #6006, и ручным вызовам обработчика.
    """
    if component.hosts:
        return tuple(component.hosts)
    artifact = component.artifact
    if artifact is not None and artifact.host:
        return (artifact.host,)
    return tuple(HOST_HANDLERS)


def _artifact_host_mismatch(component: Component, host: str) -> bool:
    """Артефакт под другой харнес: ставить его в этот нельзя."""
    artifact = component.artifact
    return bool(artifact is not None and artifact.host and artifact.host != host)


__all__ = ["UNSUPPORTED_REASON", "PluginHandler", "target_hosts"]
