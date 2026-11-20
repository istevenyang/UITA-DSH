"""
SelectorJudger — selector= 声明式选择器直查判定（2026-07-25 新增，见
docs/plans/archive/02-dsl-actions-实施纪要.md §3.7 风险 5）。

**Web only**：与 JsJudger 一致，依赖 PlaywrightEngine.find_by_id()（CSS 选择器
直查）。原生引擎（Mac/Win）无 DOM 引用概念，直接判定失败并给出明确原因（不
静默通过）。非空即真，不做可见性二次判定。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .base import JudgeResult, JudgerBase, JudgerSpec

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult


class SelectorJudger(JudgerBase):
    name = "selector"

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        from uita.engine.playwright.engine import PlaywrightEngine

        if not isinstance(ctx.engine, PlaywrightEngine):
            return JudgeResult(ok=False, reason="当前引擎不支持 selector= 直查（仅 Web 引擎支持）")

        selector = spec.kwargs.get("selector", "")
        if not selector:
            return JudgeResult(ok=False, reason="selector= 值为空")

        try:
            element = await ctx.engine.find_by_id(selector)
        except Exception as exc:  # noqa: BLE001 — 选择器查询异常视为判定失败，不向上抛出
            return JudgeResult(ok=False, reason=f"selector= 直查异常: {exc}")

        if element is None:
            return JudgeResult(ok=False, reason=f"selector={selector!r} 未命中任何元素")
        return JudgeResult(ok=True, last_value=selector)
