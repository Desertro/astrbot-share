from __future__ import annotations

from typing import Any

from astrbot.core.star.star import star_registry

from ..registry import ActionSpec, CapabilitySpec


class BaseCapabilityProvider:
    name = ""
    description = ""
    plugin_names: tuple[str, ...] = ()

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def build(self) -> CapabilitySpec:
        raise NotImplementedError

    def resolve_plugin_by_names(self, names: tuple[str, ...]) -> Any | None:
        wanted = set(names)
        for meta in reversed(star_registry):
            if not getattr(meta, "activated", False):
                continue
            meta_names = {
                str(getattr(meta, "name", "") or ""),
                str(getattr(meta, "root_dir_name", "") or ""),
            }
            if meta_names.intersection(wanted):
                return getattr(meta, "star_cls", None)
        return None

    def resolve_plugin(self) -> Any | None:
        return self.resolve_plugin_by_names(self.plugin_names)

    def require_plugin(self) -> Any | None:
        return self.resolve_plugin()

    def missing_plugin(self) -> str:
        return f"插件未加载或未启用：{self.plugin_names[0]}"

    @staticmethod
    def action(
        name: str,
        description: str,
        parameters: dict[str, Any] | None,
        handler: Any,
    ) -> ActionSpec:
        return ActionSpec(
            name=name,
            description=description,
            parameters=parameters
            or {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            handler=handler,
        )
