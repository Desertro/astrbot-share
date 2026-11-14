from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from astrbot.core.platform.astr_message_event import AstrMessageEvent


@dataclass
class CapabilityRequest:
    event: AstrMessageEvent
    args: dict[str, Any]
    is_admin: bool
    agent: Any


ActionHandler = Callable[[CapabilityRequest], Awaitable[str]]


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ActionHandler


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    description: str
    actions: dict[str, ActionSpec]


class Provider(Protocol):
    def build(self) -> CapabilitySpec: ...


class CapabilityRegistry:
    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilitySpec] = {}

    def register_provider(self, provider: Provider) -> None:
        capability = provider.build()
        if capability.name in self._capabilities:
            raise ValueError(f"duplicate capability: {capability.name}")
        self._capabilities[capability.name] = capability

    def capability_names(self) -> list[str]:
        return list(self._capabilities)

    def catalog(
        self,
        *,
        query: str = "",
        domain: str = "auto",
        enabled: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        query_norm = (query or "").strip().casefold()
        domain_norm = (domain or "auto").strip().lower()
        result: list[dict[str, Any]] = []

        for capability in self._capabilities.values():
            if enabled and capability.name not in enabled:
                continue
            if (
                domain_norm not in {"", "auto", "all"}
                and capability.name != domain_norm
            ):
                continue

            capability_text = (f"{capability.name} {capability.description}").casefold()
            include_all = not query_norm or query_norm in capability_text
            actions: list[dict[str, Any]] = []
            for action in capability.actions.values():
                action_text = f"{action.name} {action.description}".casefold()
                if include_all or query_norm in action_text:
                    actions.append(
                        {
                            "action": action.name,
                            "description": action.description,
                            "arguments_schema": action.parameters,
                        }
                    )
            if actions:
                result.append(
                    {
                        "capability": capability.name,
                        "description": capability.description,
                        "actions": actions,
                    }
                )
        if not result and query_norm:
            return self.catalog(query="", domain=domain, enabled=enabled)
        return result

    def get(self, capability: str, action: str) -> ActionSpec | None:
        spec = self._capabilities.get(str(capability or "").strip().lower())
        if not spec:
            return None
        return spec.actions.get(str(action or "").strip().lower())

    async def execute(
        self,
        capability: str,
        action: str,
        request: CapabilityRequest,
    ) -> str:
        spec = self.get(capability, action)
        if not spec:
            return (
                f"未知能力或动作：{capability}.{action}。"
                "请先调用 capability_catalog 查看白名单。"
            )
        return await spec.handler(request)
