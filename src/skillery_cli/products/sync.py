"""Синхронизация назначенных продуктов: план с сервера → установка на машину.

Ровно то, ради чего продукт существует: человек (или его компания) назначил
продукт в вебе, а ``skillery sync`` / демон приводят устройство к этому набору.
Модуль — политика ПОВЕРХ исполнения: скачивание, обработчики по типу и состояние
уже умеет ``engine``, а здесь решается, что ставить, что обновлять и что не
трогать.

Три правила, которые нельзя нарушить (ADR-0001):

1. **Версия решает, нужна ли сеть за артефактами.** Совпала с products.json —
   продукт ``already``: ни одного скачивания, состояние не переписывается.
2. **Обновление снимает только то, что реально меняется.** Компонент, которого
   нет в новой версии, снимается; компонент с другим артефактом — снимается и
   ставится заново; остальные не трогаются. Иначе ``plugin-claude``/``service``
   по своему ``verify`` ответили бы «уже стоит» и обновление осталось бы на бумаге.
3. **Снятое с назначения не удаляется само.** ``kept`` — сообщение «не назначен,
   оставлен»; удаление только по явному ``--prune``. Навыки ведут себя так же
   (#1486): пропажа из ответа хаба — не приказ стирать.

Недоступный маршрут (404/405 у старого backend) — ``InstallPlanUnavailableError``:
вызывающий решает, предупредить (sync/демон) или упасть (``product sync``), но
синхронизация навыков от этого не страдает.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any, Protocol

from skillery_cli.core.identity import device_uid
from skillery_cli.core.route_health import (
    CONTRACT_STATUSES,
    record_ok,
    record_unknown_route,
)
from skillery_cli.core.transport import ApiError
from skillery_cli.core.versioning import is_newer

from .engine import (
    STATUS_ABSENT,
    STATUS_ALREADY,
    STATUS_FAILED,
    STATUS_INSTALLED,
    STATUS_PLANNED,
    STATUS_REMOVED,
    STATUS_SKIPPED,
    install_product,
    remove_component,
    remove_product,
)
from .handlers.base import InstallContext
from .paths import current_arch, current_os, home_paths
from .plan import Artifact, Component, InstallPlan, PlanError, Product, parse_install_plan
from .runner import SubprocessRunner
from .state import InstalledState

#: Маршрут плана — один на запрос и на диагностику route_health.
PLAN_PATH = "/me/products/install-plan"


class ProductSyncError(RuntimeError):
    """План не соответствует контракту: ставить нечего, причина известна."""


class InstallPlanUnavailableError(ProductSyncError):
    """Backend не знает маршрут плана (старый сервер) — это предупреждение."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(message)


class InstallPlanFetcher(Protocol):
    """Минимальный контракт клиента: настоящий ``HubClient`` и фейк в тестах."""

    async def get_my_products_install_plan(
        self,
        *,
        os: str,
        arch: str,
        device_id: str | None = None,
        channel: str = "",
    ) -> dict[str, Any]:
        ...


@dataclass
class ProductSyncReport:
    """Итог синхронизации: что поставили, обновили, оставили и что сломали."""

    installed: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "installed": list(self.installed),
            "updated": list(self.updated),
            "already": list(self.already),
            "skipped": list(self.skipped),
            "kept": list(self.kept),
            "pruned": list(self.pruned),
            "failed": list(self.failed),
            "warnings": list(self.warnings),
            "dry_run": self.dry_run,
        }


def build_install_context(*, dry_run: bool = False) -> InstallContext:
    """Контекст установки от текущего HOME — точка подмены для тестов."""
    return InstallContext(
        paths=home_paths(),
        runner=SubprocessRunner(),
        state=InstalledState.load(),
        dry_run=dry_run,
    )


async def fetch_install_plan(
    client: InstallPlanFetcher,
    *,
    os_name: str | None = None,
    arch: str | None = None,
    device_id: str | None = None,
    channel: str = "",
) -> InstallPlan:
    """Запросить и разобрать install-plan устройства.

    ``os``/``arch`` — текущей машины; ``device_id`` — тот же стабильный id, что
    уходит в очередь устройства и телеметрию (второй не заводим). Разбор — тот
    же ``parse_install_plan``, что у ``skillery product install``: контракт у
    планов один, второго валидатора нет.
    """
    os_name = os_name or current_os()
    arch = arch or current_arch()
    device_id = device_id if device_id is not None else device_uid()
    try:
        payload = await client.get_my_products_install_plan(
            os=os_name,
            arch=arch,
            device_id=device_id or None,
            channel=channel,
        )
    except ApiError as exc:
        if exc.status_code in CONTRACT_STATUSES:
            # 404/405 — расхождение контракта, а не сбой сети: клиент и backend
            # разной свежести. Флаг виден в `skillery status` (route_health).
            with suppress(Exception):
                record_unknown_route(
                    "GET", PLAN_PATH, exc.status_code, source="sync.products"
                )
            raise InstallPlanUnavailableError(
                exc.status_code,
                f"backend не знает {PLAN_PATH} (HTTP {exc.status_code}) — "
                "продукты не синхронизированы, навыки это не затрагивает; "
                "обновите CLI (skillery self upgrade)",
            ) from exc
        raise
    with suppress(Exception):
        record_ok("GET", PLAN_PATH)
    try:
        return parse_install_plan(payload)
    except PlanError as exc:
        raise ProductSyncError(f"install-plan нарушает контракт: {exc}") from exc


async def sync_products(
    client: InstallPlanFetcher,
    ctx: InstallContext,
    *,
    dry_run: bool | None = None,
    prune: bool = False,
    os_name: str | None = None,
    arch: str | None = None,
    device_id: str | None = None,
    channel: str = "",
) -> ProductSyncReport:
    """Привести устройство к назначенному набору продуктов (идемпотентно)."""
    if dry_run is None:
        dry_run = ctx.dry_run
    ctx.dry_run = dry_run
    report = ProductSyncReport(dry_run=dry_run)

    plan = await fetch_install_plan(
        client,
        os_name=os_name,
        arch=arch,
        device_id=device_id,
        channel=channel,
    )

    assigned: set[str] = set()
    for product in plan.products:
        if product.slug in assigned:
            report.warnings.append(
                f"{product.slug}: продукт повторяется в плане — использован первый"
            )
            continue
        assigned.add(product.slug)
        await _sync_product(product, ctx, report)

    await _settle_unassigned(ctx, report, assigned, prune=prune, dry_run=dry_run)

    if not dry_run:
        ctx.state.save()
    return report


async def _sync_product(
    product: Product, ctx: InstallContext, report: ProductSyncReport
) -> None:
    local = ctx.state.get_product(product.slug)
    recorded = (local or {}).get("components")
    if not local or not isinstance(recorded, Mapping) or not recorded:
        # Чистая машина (или состояние без компонентов — установка недоделана):
        # ставим целиком, идемпотентность внутри обработчиков.
        run = await install_product(product, ctx)
        _report_new(product, run, report)
        return

    local_version = str(local.get("version") or "")
    if local_version and product.version == local_version:
        # Правило 1: совпавшая версия — already, без сети за артефактами.
        report.already.append(product.slug)
        return
    if local_version and not is_newer(product.version, local_version):
        report.skipped.append(product.slug)
        report.warnings.append(
            f"{product.slug}: плановая версия {product.version} не новее локальной "
            f"{local_version} — откат не делаем"
        )
        return
    await _update_product(product, local, ctx, report)


async def _update_product(
    product: Product,
    local: Mapping[str, Any],
    ctx: InstallContext,
    report: ProductSyncReport,
) -> None:
    """Обновить продукт: снять исчезнувшее/изменившееся, затем поставить план."""
    old = local.get("components") or {}
    new_by_key = {component.key: component for component in product.components}

    gone = [key for key in old if key not in new_by_key]
    changed = [
        key
        for key, meta in old.items()
        if key in new_by_key and _component_changed(meta, new_by_key[key])
    ]
    for key in sorted(set(gone) | set(changed)):
        if ctx.dry_run:
            continue
        run = await remove_component(product.slug, key, ctx)
        if run.status == STATUS_FAILED:
            report.failed.append(product.slug)
            report.warnings.append(f"{product.slug}/{key}: не снят — {run.detail}")
            return
        if run.status not in (STATUS_REMOVED, STATUS_ABSENT):
            report.warnings.append(
                f"{product.slug}/{key}: старое не снято ({run.status}) — "
                "возможно, останется прежняя версия"
            )

    run = await install_product(product, ctx)
    if run.status == STATUS_FAILED:
        _report_failed(product, run, report)
        return
    if run.status == STATUS_SKIPPED and not ctx.dry_run:
        report.skipped.append(product.slug)
        report.warnings.append(
            f"{product.slug}: под эту платформу нечего ставить "
            "(все компоненты пропущены)"
        )
        return
    if not ctx.dry_run:
        ctx.state.set_version(product.slug, product.version)
    report.updated.append(product.slug)


def _report_new(product: Product, run: Any, report: ProductSyncReport) -> None:
    if run.status == STATUS_FAILED:
        _report_failed(product, run, report)
        return
    if run.status == STATUS_SKIPPED:
        report.skipped.append(product.slug)
        report.warnings.append(
            f"{product.slug}: нет артефактов под эту платформу — установка пропущена"
        )
        return
    if run.status == STATUS_PLANNED:
        report.installed.append(product.slug)  # --dry-run: план без действий
        return
    report.installed.append(product.slug)
    if run.status not in (STATUS_INSTALLED, STATUS_ALREADY):
        report.warnings.append(f"{product.slug}: итог установки — {run.status}")


def _report_failed(product: Product, run: Any, report: ProductSyncReport) -> None:
    if product.slug not in report.failed:
        report.failed.append(product.slug)
    for component in run.components:
        if component.status == STATUS_FAILED:
            report.warnings.append(
                f"{product.slug}/{component.key}: {component.detail or 'ошибка'}"
            )


async def _settle_unassigned(
    ctx: InstallContext,
    report: ProductSyncReport,
    assigned: set[str],
    *,
    prune: bool,
    dry_run: bool,
) -> None:
    """Локальные продукты вне плана: оставить (kept) или снять (--prune)."""
    extra = sorted(set(ctx.state.products) - assigned)
    if not prune:
        report.kept.extend(extra)
        return
    for slug in extra:
        if dry_run:
            report.pruned.append(slug)
            continue
        run = await remove_product(slug, ctx)
        if run.status == STATUS_FAILED:
            report.failed.append(slug)
            report.warnings.append(f"{slug}: не удалось снять — см. component-статусы")
            continue
        report.pruned.append(slug)


def _component_changed(meta: Any, component: Component) -> bool:
    """Компонент из состояния и из плана — один и тот же артефакт?"""
    if not isinstance(meta, Mapping):
        return True
    if str(meta.get("type") or "") != component.type:
        return True
    recorded = _state_artifact_signature(meta.get("artifact"))
    return recorded != _artifact_signature(component.artifact)


def _state_artifact_signature(raw: Any) -> tuple[Any, ...]:
    if not isinstance(raw, Mapping):
        return ()
    return (
        str(raw.get("kind") or ""),
        str(raw.get("url") or ""),
        str(raw.get("sha256") or "").lower(),
        str(raw.get("skill_ref") or ""),
        _freeze(raw.get("install") or {}),
    )


def _artifact_signature(artifact: Artifact | None) -> tuple[Any, ...]:
    if artifact is None:
        return ()
    # os/arch в подписи не участвуют: это фильтр запроса, а не свойство байтов;
    # size тоже необязателен в ответе backend'а. Меняется то, что скачивается.
    return (
        artifact.kind,
        artifact.url,
        artifact.sha256.lower(),
        artifact.skill_ref,
        _freeze(artifact.install),
    )


def _freeze(value: Mapping[str, Any]) -> str:
    return json.dumps(dict(value), sort_keys=True, default=str, ensure_ascii=False)
