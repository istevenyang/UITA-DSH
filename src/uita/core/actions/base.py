"""
ActionBase, ActionResult, ActionContext — foundation for all actions.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from uita.core.engine.base import EngineBase
from uita.core.resolver.resolver import Resolver


class ActionStatus(Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ActionResult:
    status: ActionStatus = ActionStatus.PASSED
    message: str = ""
    duration_ms: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == ActionStatus.PASSED

    @classmethod
    def ok(cls, message: str = "", duration_ms: float = 0.0) -> "ActionResult":
        return cls(status=ActionStatus.PASSED, message=message, duration_ms=duration_ms)

    @classmethod
    def fail(cls, message: str, duration_ms: float = 0.0) -> "ActionResult":
        return cls(status=ActionStatus.FAILED, message=message, duration_ms=duration_ms)


@dataclass
class ActionContext:
    """Runtime context passed to every action execution."""
    engine: EngineBase
    resolver: Resolver
    args: list[str]             # parsed positional arguments from the DSL statement
    kwargs: dict[str, str] = field(default_factory=dict)  # key=value 关键字参数（P2 新增，DSL 解析结果）
    extra: dict[str, Any] = field(default_factory=dict)


class ActionBase(ABC):
    """Abstract base for all TCS actions."""

    @property
    @abstractmethod
    def name(self) -> str:
        """The action keyword (e.g. 'click', 'type')."""
        ...

    @abstractmethod
    async def execute(self, ctx: ActionContext) -> ActionResult:
        """Execute the action with the given context."""
        ...
