"""
JudgerBase / JudgerSpec / JudgeResult — 判定器体系基础类型。

见 docs/plans/archive/02-dsl-actions-实施纪要.md §3.1。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # 避免循环 import：judgers 只在类型标注层引用 ActionContext/ResolveResult
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult


@dataclass
class JudgerSpec:
    """一次 assert/verify 调用的判定规格。"""
    description: str                              # 元素描述（url/title/js 类判定可为空字符串）
    kwargs: dict[str, str] = field(default_factory=dict)  # 除 timeout 外的全部判定 kwarg
    strict: bool = False


@dataclass
class JudgeResult:
    ok: bool
    reason: str = ""
    last_value: str = ""


class JudgerBase(ABC):
    """所有判定器的抽象基类。"""

    name: str = ""

    @abstractmethod
    async def check(
        self,
        ctx: "ActionContext",
        resolve_result: "Optional[ResolveResult]",
        spec: JudgerSpec,
    ) -> JudgeResult:
        """执行一次判定，返回 JudgeResult。

        resolve_result 可能为 None（无元素类判定，如 url/title/js）。
        """
        ...
