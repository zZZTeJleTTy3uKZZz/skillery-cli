"""MCP-компонент: регистрация сервера у агента (Claude — ``claude mcp add``).

Значения секретов в плане не хранятся — только имена переменных. Поэтому в
команду уходят ИМЕНА с пустым значением: человек (или его keyring) заполнит их
сам, а CLI не выдумывает секрет и не печатает его в историю команд.
"""
from __future__ import annotations

from ..plan import Artifact, Component
from .base import (
    ComponentResult,
    InstallContext,
    ProductError,
    artifact_install,
    install_list,
    install_str,
    require_artifact,
    run_checked,
)

TRANSPORTS = ("stdio", "http", "sse")


class McpHandler:
    component_type = "mcp"

    def plan(self, component: Component, ctx: InstallContext) -> list[str]:
        artifact = require_artifact(component)
        return [f"claude mcp add --scope user {self._name(component, artifact)} …"]

    def verify(self, component: Component, ctx: InstallContext) -> bool:
        artifact = require_artifact(component)
        name = self._name(component, artifact)
        return ctx.runner.run(["claude", "mcp", "get", name]).ok

    async def install(
        self, component: Component, ctx: InstallContext
    ) -> ComponentResult:
        artifact = require_artifact(component)
        name = self._name(component, artifact)
        if self.verify(component, ctx):
            return ComponentResult(
                key=component.key,
                type=self.component_type,
                status="already-installed",
                detail=name,
            )

        transport = install_str(artifact, "transport")
        if transport not in TRANSPORTS:
            raise ProductError(
                f"mcp/{component.key}: транспорт {transport!r} не поддерживается "
                f"(разрешены {', '.join(TRANSPORTS)})"
            )
        argv = ["claude", "mcp", "add", "--scope", "user"]
        if transport == "stdio":
            base = install_str(artifact, "command")
            if not base:
                raise ProductError(f"mcp/{component.key}: для stdio нужен command")
            argv.append(name)
            for env_key in install_list(artifact, "env_keys"):
                argv.extend(["-e", f"{env_key}="])
            argv.extend(["--", base, *install_list(artifact, "args")])
        else:
            url = install_str(artifact, "url")
            if not url:
                raise ProductError(f"mcp/{component.key}: для {transport} нужен url")
            argv.extend(["--transport", transport, name, url])

        run_checked(ctx, argv)
        return ComponentResult(
            key=component.key, type=self.component_type, status="installed", detail=name
        )

    async def remove(self, component: Component, ctx: InstallContext) -> ComponentResult:
        artifact = require_artifact(component)
        name = self._name(component, artifact)
        if not self.verify(component, ctx):
            return ComponentResult(
                key=component.key,
                type=self.component_type,
                status="absent",
                detail=name,
            )
        run_checked(ctx, ["claude", "mcp", "remove", "-s", "user", name])
        return ComponentResult(
            key=component.key, type=self.component_type, status="removed", detail=name
        )

    def _name(self, component: Component, artifact: Artifact) -> str:
        install = artifact_install(artifact)
        value = install.get("name")
        return value if isinstance(value, str) and value.strip() else component.key
