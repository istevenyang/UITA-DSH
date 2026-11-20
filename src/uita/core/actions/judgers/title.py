"""
TitleJudger — title=/title_contains= 窗口/页面标题判定。

依赖 EngineBase.get_window_title()（全平台通用，Mac/Win/Web 均已实现）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .base import JudgeResult, JudgerBase, JudgerSpec

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult


class TitleJudger(JudgerBase):
    name = "title"

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        title = await ctx.engine.get_window_title()
        title_eq = spec.kwargs.get("title")
        title_contains = spec.kwargs.get("title_contains")

        if title_eq is not None and title != title_eq:
            return JudgeResult(ok=False, reason=f"title={title!r} != {title_eq!r}", last_value=title)
        if title_contains is not None and title_contains not in title:
            return JudgeResult(
                ok=False, reason=f"title={title!r} 不包含 {title_contains!r}", last_value=title,
            )
        return JudgeResult(ok=True, last_value=title)
