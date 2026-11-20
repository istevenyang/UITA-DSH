"""Host-owned capabilities and deterministic authorization."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .session import Candidate, Failure, Mode, Observation


READ_TOOLS = frozenset({"read_text", "inspect_window", "inspect_visibility", "wait"})
RECOVERY_TOOLS = frozenset({"focus", "switch_window", "close_safe_overlay"})
BLOCKED_CATEGORIES = frozenset({"environment", "guard", "lock", "recovery", "dangerous", "product"})


@dataclass(frozen=True)
class Tool:
    name: str
    read_only: bool
    targets: tuple[str, ...]
    parameter_types: dict[str, type]
    invoke: Callable[[str, dict[str, Any]], Awaitable[None]]
    authorize: Callable[[Candidate, Observation], bool]

    def __post_init__(self) -> None:
        allowed = READ_TOOLS if self.read_only is True else RECOVERY_TOOLS
        if type(self.read_only) is not bool or self.name not in allowed:
            raise ValueError("tool is not in the fixed capability whitelist")
        if any(kind not in (str, int, float, bool) for kind in self.parameter_types.values()):
            raise ValueError("tool parameters must have scalar types")

    def description(self) -> dict[str, Any]:
        return {"name": self.name, "read_only": self.read_only,
                "targets": list(self.targets),
                "parameters": {key: kind.__name__ for key, kind in self.parameter_types.items()}}


class PolicyGate:
    def __init__(self, mode: Mode, tools: tuple[Tool, ...]):
        self.mode = mode
        self.tools = {tool.name: tool for tool in tools}
        if len(self.tools) != len(tools):
            raise ValueError("duplicate tool name")

    def descriptions(self, observation: Observation) -> list[dict[str, Any]]:
        result = []
        for tool in self.tools.values():
            if not tool.read_only and self.mode is not Mode.RECOVERY:
                continue
            item = tool.description()
            item["targets"] = [target for target in tool.targets if target in observation.targets]
            if item["targets"]:
                result.append(item)
        return result

    def permit(self, failure: Failure, observation: Observation,
               candidate: Candidate) -> Tool:
        if self.mode is Mode.OFF or failure.phase != "main":
            raise ValueError("review disabled or unsafe phase")
        if (failure.category in BLOCKED_CATEGORIES or observation.product_mismatch
                or not observation.safety.valid):
            raise ValueError("original preconditions or safety gate failed")
        tool = self.tools.get(candidate.tool)
        if tool is None or (not tool.read_only and self.mode is not Mode.RECOVERY):
            raise ValueError("tool unavailable in current mode")
        if candidate.target not in observation.targets or candidate.target not in tool.targets:
            raise ValueError("target outside observed tool scope")
        if set(candidate.parameters) != set(tool.parameter_types):
            raise ValueError("tool parameter keys mismatch")
        if any(type(candidate.parameters[key]) is not kind
               for key, kind in tool.parameter_types.items()):
            raise ValueError("tool parameter type mismatch")
        if not candidate.evidence_refs or any(ref not in observation.evidence_refs
                                               for ref in candidate.evidence_refs):
            raise ValueError("missing current evidence")
        if tool.authorize(candidate, observation) is not True:
            raise ValueError("host authorization rejected action")
        return tool
