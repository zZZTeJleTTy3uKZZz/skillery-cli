"""Исполнение install-plan: порядок, идемпотентность, состояние, снятие.

Движок не знает ни одного типа компонента: он спрашивает обработчик, ловит
ПОНЯТНЫЕ ошибки и продолжает план. Один сломанный компонент — это строка
``failed`` в отчёте, а не отменённая установка остальных.

Состояние пишется после успешных компонентов: ``product list`` и
``product remove`` обязаны видеть ровно то, что реально лежит на диске.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .download import ArtifactError
from .handlers import get_handler
from .handlers.base import ComponentResult, InstallContext, ProductError
from .plan import Artifact, Component, InstallPlan, Product
from .runner import CommandError

STATUS_INSTALLED = "installed"
STATUS_ALREADY = "already-installed"
STATUS_PLANNED = "planned"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUS_REMOVED = "removed"
STATUS_ABSENT = "absent"

_ERRORS = (ProductError, CommandError, ArtifactError, OSError, LookupError)


@dataclass(frozen=True)
class ComponentRun:
    """Итог компонента для отчёта и состояния."""

    key: str
    type: str
    status: str
    detail: str = ""
    path: str = ""
    actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProductRun:
    slug: str
    version: str
    status: str
    components: tuple[ComponentRun, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "version": self.version,
            "status": self.status,
            "components": [
                {
                    "key": item.key,
                    "type": item.type,
                    "status": item.status,
                    "detail": item.detail,
                }
                for item in self.components
            ],
        }


@dataclass(frozen=True)
class PlanRun:
    products: tuple[ProductRun, ...] = ()
    summary: Mapping[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "products": [product.to_dict() for product in self.products],
            "summary": dict(self.summary),
        }


@dataclass(frozen=True)
class RemoveRun:
    slug: str
    status: str
    components: tuple[ComponentRun, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "status": self.status,
            "components": [
                {"key": item.key, "type": item.type, "status": item.status}
                for item in self.components
            ],
        }


async def install_plan(plan: InstallPlan, ctx: InstallContext) -> PlanRun:
    """Поставить все продукты плана и записать состояние."""
    products = tuple([await install_product(product, ctx) for product in plan.products])
    if not ctx.dry_run:
        ctx.state.save()
    return PlanRun(products=products, summary=_summary(products))


async def install_product(product: Product, ctx: InstallContext) -> ProductRun:
    """Поставить один продукт: компоненты в порядке зависимостей."""
    runs: list[ComponentRun] = []
    for component in order_components(product.components):
        runs.append(await _install_component(product, component, ctx))
    return ProductRun(
        slug=product.slug,
        version=product.version,
        status=_product_status(runs),
        components=tuple(runs),
    )


async def remove_product(slug: str, ctx: InstallContext) -> RemoveRun:
    """Снять продукт по состоянию: обработчики знают, что именно удалять."""
    recorded = ctx.state.get_product(slug)
    if recorded is None:
        return RemoveRun(slug=slug, status=STATUS_ABSENT)

    runs: list[ComponentRun] = []
    failed = False
    for key in sorted(recorded.get("components", {})):
        result = await remove_component(slug, key, ctx)
        runs.append(result)
        failed = failed or result.status == STATUS_FAILED

    if not failed:
        ctx.state.drop_product(slug)
        ctx.state.save()

    status = STATUS_FAILED if failed else STATUS_REMOVED
    return RemoveRun(slug=slug, status=status, components=tuple(runs))


async def remove_component(
    product_slug: str, key: str, ctx: InstallContext
) -> ComponentRun:
    """Снять ОДИН записанный компонент — шаг обновления продукта (#5988).

    Обновление снимает только те компоненты, которых нет в новой версии или
    чей артефакт изменился: без точечного снятия обработчики увидели бы старое
    на диске, сказали «уже стоит» и оставили прежнюю версию. Запись из
    состояния убирается только после успешного снятия — иначе состояние
    разошлось бы с диском.
    """
    recorded = ctx.state.get_product(product_slug)
    meta = (recorded or {}).get("components", {}).get(key)
    if not isinstance(meta, Mapping):
        return ComponentRun(
            key=key,
            type="",
            status=STATUS_ABSENT,
            detail="компонента нет в состоянии",
        )
    component = Component(
        key=key,
        type=str(meta.get("type") or ""),
        artifact=_artifact_from_state(meta.get("artifact")),
    )
    if component.artifact is None:
        return ComponentRun(
            key=key,
            type=component.type,
            status=STATUS_SKIPPED,
            detail="в состоянии нет артефакта — снимать нечем",
        )
    try:
        handler = get_handler(component.type)
        result = await handler.remove(component, ctx)
    except _ERRORS as exc:
        return ComponentRun(
            key=key,
            type=component.type,
            status=STATUS_FAILED,
            detail=str(exc),
        )
    if result.status in (STATUS_REMOVED, STATUS_ABSENT):
        ctx.state.drop_component(product_slug, key)
    return _run_from_result(result)


def order_components(components: Sequence[Component]) -> tuple[Component, ...]:
    """Топологический порядок по ``depends_on``; неизвестные связи игнорируются.

    План backend'а уже упорядочен, но полагаться на это в исполнителе нельзя:
    порядок — часть контракта установки, и проверять его должен тот, кто ставит.
    Цикл не валит установку: оставшиеся компоненты идут в исходном порядке.
    """
    by_key = {component.key: component for component in components}
    pending = list(components)
    done: set[str] = set()
    ordered: list[Component] = []
    while pending:
        progressed = False
        for component in list(pending):
            if all(
                dependency in done or dependency not in by_key
                for dependency in component.depends_on
            ):
                ordered.append(component)
                done.add(component.key)
                pending.remove(component)
                progressed = True
        if not progressed:  # цикл: не выдумываем порядок, ставим как пришло
            ordered.extend(pending)
            break
    return tuple(ordered)


async def _install_component(
    product: Product, component: Component, ctx: InstallContext
) -> ComponentRun:
    try:
        handler = get_handler(component.type)
    except LookupError as exc:
        return ComponentRun(
            key=component.key,
            type=component.type,
            status=STATUS_FAILED,
            detail=str(exc),
        )

    if ctx.dry_run:
        try:
            actions = tuple(handler.plan(component, ctx))
        except ProductError as exc:
            return ComponentRun(
                key=component.key,
                type=component.type,
                status=STATUS_FAILED,
                detail=str(exc),
            )
        return ComponentRun(
            key=component.key,
            type=component.type,
            status=STATUS_PLANNED,
            actions=actions,
        )

    if component.artifact is None:
        return ComponentRun(
            key=component.key,
            type=component.type,
            status=STATUS_SKIPPED,
            detail="нет артефакта под эту ОС/архитектуру (см. skipped плана)",
        )

    try:
        result = await handler.install(component, ctx)
    except _ERRORS as exc:
        return ComponentRun(
            key=component.key,
            type=component.type,
            status=STATUS_FAILED,
            detail=str(exc),
        )

    if result.status in (STATUS_INSTALLED, STATUS_ALREADY):
        ctx.state.record_component(
            product.slug,
            product.version,
            key=component.key,
            component_type=component.type,
            status=STATUS_INSTALLED,
            detail=result.detail,
            path=str(result.path or ""),
            artifact=_artifact_to_state(component.artifact),
        )
    return _run_from_result(result)


def _run_from_result(result: ComponentResult) -> ComponentRun:
    return ComponentRun(
        key=result.key,
        type=result.type,
        status=result.status,
        detail=result.detail,
        path=result.path,
        actions=result.actions,
    )


def _product_status(runs: Sequence[ComponentRun]) -> str:
    statuses = {run.status for run in runs}
    if STATUS_FAILED in statuses:
        return STATUS_FAILED
    if STATUS_PLANNED in statuses:
        return STATUS_PLANNED
    if statuses and statuses <= {STATUS_SKIPPED}:
        return STATUS_SKIPPED
    if STATUS_INSTALLED in statuses:
        return STATUS_INSTALLED
    return STATUS_ALREADY


def _summary(products: Sequence[ProductRun]) -> dict[str, int]:
    counts = {
        STATUS_INSTALLED: 0,
        "already_installed": 0,
        STATUS_SKIPPED: 0,
        STATUS_FAILED: 0,
    }
    components = 0
    for product in products:
        for run in product.components:
            components += 1
            if run.status == STATUS_ALREADY:
                counts["already_installed"] += 1
            elif run.status in counts:
                counts[run.status] += 1
    return {
        "products": len(products),
        "components": components,
        **counts,
    }


def _artifact_to_state(artifact: Artifact) -> dict[str, Any]:
    return {
        "kind": artifact.kind,
        "os": artifact.os,
        "arch": artifact.arch,
        "url": artifact.url,
        "sha256": artifact.sha256,
        "size": artifact.size,
        "skill_ref": artifact.skill_ref,
        "install": dict(artifact.install),
    }


def _artifact_from_state(raw: Any) -> Artifact | None:
    if not isinstance(raw, Mapping):
        return None
    install = raw.get("install")
    return Artifact(
        kind=str(raw.get("kind") or ""),
        os=str(raw.get("os") or ""),
        arch=str(raw.get("arch") or ""),
        url=str(raw.get("url") or ""),
        sha256=str(raw.get("sha256") or ""),
        size=raw.get("size") if isinstance(raw.get("size"), int) else None,
        skill_ref=str(raw.get("skill_ref") or ""),
        install=install if isinstance(install, Mapping) else {},
    )
