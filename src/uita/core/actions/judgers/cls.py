"""
ClassJudger — has_class=/not_has_class= class 属性判定。

**Web only**：class 概念仅 DOM 有意义，非 PlaywrightEngine 直接判定失败并说明原因
（与 P1 wait_reply 相同的 isinstance 判断模式，不静默通过、不抛异常）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .base import JudgeResult, JudgerBase, JudgerSpec

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult


class ClassJudger(JudgerBase):
    name = "class"

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        from uita.engine.playwright.engine import PlaywrightEngine

        if not isinstance(ctx.engine, PlaywrightEngine):
            return JudgeResult(ok=False, reason="仅 Web 引擎支持该判定（has_class/not_has_class）")
        if resolve_result is None or not resolve_result.found:
            return JudgeResult(ok=False, reason=f"元素不存在，无法判定 class: '{spec.description}'")

        class_attr = await ctx.engine.get_attribute(resolve_result.element, "class") or ""
        classes = class_attr.split()
        has_class = spec.kwargs.get("has_class")
        not_has_class = spec.kwargs.get("not_has_class")

        if has_class is not None and has_class not in classes:
            return JudgeResult(
                ok=False, reason=f"class={class_attr!r} 不含 {has_class!r}", last_value=class_attr,
            )
        if not_has_class is not None and not_has_class in classes:
            return JudgeResult(
                ok=False, reason=f"class={class_attr!r} 意外含 {not_has_class!r}", last_value=class_attr,
            )
        return JudgeResult(ok=True, last_value=class_attr)
