"""
UrlJudger — url=/url_contains=/url_regex= 当前页面 URL 判定。

**Web only**：依赖本阶段新增的 PlaywrightEngine.get_url()（非 EngineBase 契约，
与 P1 的 evaluate() 同批次风格：Playwright 专属逃生舱）。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from .base import JudgeResult, JudgerBase, JudgerSpec

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult


class UrlJudger(JudgerBase):
    name = "url"

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        from uita.engine.playwright.engine import PlaywrightEngine

        if not isinstance(ctx.engine, PlaywrightEngine):
            return JudgeResult(ok=False, reason="仅 Web 引擎支持该判定（url/url_contains/url_regex）")

        current_url = await ctx.engine.get_url()
        url_eq = spec.kwargs.get("url")
        url_contains = spec.kwargs.get("url_contains")
        url_regex = spec.kwargs.get("url_regex")

        if url_eq is not None and current_url != url_eq:
            return JudgeResult(ok=False, reason=f"url={current_url!r} != {url_eq!r}", last_value=current_url)
        if url_contains is not None and url_contains not in current_url:
            return JudgeResult(
                ok=False, reason=f"url={current_url!r} 不包含 {url_contains!r}", last_value=current_url,
            )
        if url_regex is not None and not re.search(url_regex, current_url):
            return JudgeResult(
                ok=False, reason=f"url={current_url!r} 不匹配正则 {url_regex!r}", last_value=current_url,
            )
        return JudgeResult(ok=True, last_value=current_url)
