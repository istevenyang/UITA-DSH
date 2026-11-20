"""
CountJudger — count_min/count_max/count_eq/count_gt/count_lt 计数判定。

依赖 EngineBase.count_by_id()。该方法在 core/engine/base.py 中已经是"可选覆盖"
方法（非 18 个抽象方法之一，默认实现委托给 find_by_id 返回 0/1），Mac/Win 原生
引擎当前未 override，计数精度退化为"最多判定单个元素存在"——已知局限，
架构已预留（backlog #7），后续引擎层扩展即可，本判定器无需改动。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Optional

from .base import JudgeResult, JudgerBase, JudgerSpec

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult

_CHECKS: list[tuple[str, Callable[[int, int], bool]]] = [
    ("count_min", lambda target, n: n >= target),
    ("count_max", lambda target, n: n <= target),
    ("count_eq", lambda target, n: n == target),
    ("count_gt", lambda target, n: n > target),
    ("count_lt", lambda target, n: n < target),
]


class CountJudger(JudgerBase):
    name = "count"

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        selector = None
        if resolve_result is not None and resolve_result.element is not None:
            selector = resolve_result.element.element_id
        if not selector:
            return JudgeResult(
                ok=False,
                reason=f"count 判定需要可复用的 selector，但 '{spec.description}' 当前无 element_id",
            )

        count = await ctx.engine.count_by_id(selector)
        for key, cmp in _CHECKS:
            raw = spec.kwargs.get(key)
            if raw is None:
                continue
            try:
                target = int(raw)
            except ValueError:
                return JudgeResult(ok=False, reason=f"{key}={raw!r} 不是合法整数", last_value=str(count))
            if not cmp(target, count):
                return JudgeResult(
                    ok=False, reason=f"count={count} 不满足 {key}={raw}", last_value=str(count),
                )
        return JudgeResult(ok=True, last_value=str(count))
