"""DSH 权限闸：三档模式 × 三级工具的确定性放行矩阵。

口径（与 ``core/runner/agent_runtime/policy.py`` 的"宿主能力白名单 + 确定性
授权"同源，不共享实现以避免 core→tools 反向依赖）：

============  ==============  ==============  ============
模式          read            action          write
============  ==============  ==============  ============
readonly      放行            拒绝            拒绝
supervised    放行            需人工批准      需人工批准
auto          放行            放行            放行
============  ==============  ==============  ============
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from .registry import TIER_READ, DshTool


class Mode(str, Enum):
    READONLY = "readonly"
    SUPERVISED = "supervised"
    AUTO = "auto"

    @classmethod
    def parse(cls, value: object, default: "Mode" = None) -> "Mode":
        raw = str(value or "").strip().lower()
        for item in cls:
            if item.value == raw:
                return item
        return default if default is not None else cls.SUPERVISED


@dataclass(frozen=True)
class Decision:
    """一次放行判定。"""

    allowed: bool
    reason: str = ""
    needs_approval: bool = False


class PolicyGate:
    """按模式与工具名单判定是否允许执行某个工具。"""

    def __init__(self, mode: Mode = Mode.SUPERVISED,
                 allowlist: Optional[Iterable[str]] = None) -> None:
        self.mode = mode if isinstance(mode, Mode) else Mode.parse(mode)
        self.allowlist = set(allowlist) if allowlist else None

    def check(self, tool: DshTool, *, approved: bool = False) -> Decision:
        if self.allowlist is not None and tool.name not in self.allowlist:
            return Decision(False, f"工具 {tool.name} 不在白名单内")
        if self.mode is Mode.AUTO:
            return Decision(True)
        if tool.tier == TIER_READ:
            return Decision(True)
        if self.mode is Mode.READONLY:
            return Decision(False, f"readonly 模式禁止 {tool.tier} 类工具 {tool.name}")
        if approved:
            return Decision(True, "人工已批准")
        return Decision(False, "需要人工批准", needs_approval=True)
