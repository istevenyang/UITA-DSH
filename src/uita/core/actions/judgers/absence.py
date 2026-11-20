"""
AbsenceJudger — absent=true 时使用，断言元素不存在。

核心策略：只信任结构化层级（L1/L2/L3/L4/L5/L6）的 resolve 结果。
L7（视觉指纹）和 L8（LLM 视觉定位）的结果在 absent 场景下不可信——
LLM 视觉定位天然倾向于"找到点什么"，误判率极高，不适合回答"不存在"这个问题。

同理，ExistenceJudger.fallback_chain（视觉/OCR 降级链）也不适用于 absent
场景，跳过以避免误判。

见 docs/plans/archive/02-dsl-actions-实施纪要.md §3.1b。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .base import JudgeResult, JudgerBase, JudgerSpec
from .existence import _observe_existence, _strict_observation

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveLevel, ResolveResult

# 结构化层级：这些层级基于精确的 DOM/AX 查询，结果可信
_STRUCTURAL_LEVELS: set[str] = {
    "L1_MAP_EXACT",
    "L2_SEMANTIC",
    "L3_TEXT_MATCH",
    "L4_LABEL_MATCH",
    "L5_ROLE_MATCH",
    "L6_LLM_SELECTOR",
}


class AbsenceJudger(JudgerBase):
    name = "absence"

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        ctx.extra.setdefault("existence_strategy", "structured")
        if _strict_observation(ctx):
            observed = await _observe_existence(ctx, resolve_result, spec.description, strict=True)
            if observed is True:
                return JudgeResult(ok=False, reason=f"元素仍存在: '{spec.description}'（期望不存在）")
            if observed is False:
                return JudgeResult(ok=True)
            return JudgeResult(ok=False, reason=f"UNKNOWN: 不存在性观察证据不足: '{spec.description}'")

        # 只信任结构化层级的 found 结果；L7 视觉指纹和 L8 LLM 视觉定位
        # 在 absent 场景下不可信（误判率高），视为"未找到"。
        found = (
            resolve_result is not None
            and resolve_result.found
            and resolve_result.level.value in _STRUCTURAL_LEVELS
        )

        # 不走 ExistenceJudger.fallback_chain（视觉/OCR 降级链同样不适用于
        # absent 场景——它们只能回答"看起来像"，不能精确回答"不存在"）。

        if found:
            # DOM 中存在但不可见（如 display:none / opacity:0）也视为 absent。
            # 这对 Ant Design Popover 等隐藏后不移除 DOM 节点的组件至关重要。
            if (
                resolve_result is not None
                and resolve_result.element is not None
                and resolve_result.element.raw is not None
            ):
                visible = await ctx.engine.is_visible(resolve_result.element)
                if not visible:
                    return JudgeResult(ok=True)
            return JudgeResult(ok=False, reason=f"元素仍存在: '{spec.description}'（期望不存在）")
        return JudgeResult(ok=True)
