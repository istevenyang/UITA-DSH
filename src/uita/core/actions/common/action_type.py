"""type[element_name, text] action。

声明式首策略 kwarg：type[文字, selector=".input"] / [role=textbox] / [label=邮箱]
跳过路由决策直达指定策略。注意 "text=" 不在 type 的允许 kwarg 集合内——它与
type 自身第二位置参数（待输入文字）语义冲突。

坐标句柄：命中元素若 element.raw is None 且 element.coords 非 None（L7 视觉指纹 /
L8 LLM 视觉命中），走 viewport 坐标点击+输入或 pyautogui 坐标点击+输入分支。

区域点击 kwarg：type[文字, selector=".input", region=...] 声明九宫格点击区域
（仅在 element.bbox 非 None 即 L7 视觉指纹命中时生效，其余场景零回归）。
"""
from __future__ import annotations

import asyncio
import logging
import time

from uita.core.actions.base import ActionBase, ActionContext, ActionResult
from uita.core.actions.common._pyautogui_helper import type_at_coords
from uita.core.actions.registry import register_action
from uita.core.logging_manager import log_action_execution
from uita.core.resolver.click_region import resolve_click_point
from uita.core.resolver.router import declared_strategy_label, extract_declared_strategy

logger = logging.getLogger(__name__)

_TYPE_ALLOWED_DECLARED_KEYS = ("selector", "role", "label")


def _strip_quotes(text: str) -> str:
    """如果 text 被成对引号包裹（"..." 或 '...'），剥掉外层引号。"""
    s = text.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return text


@register_action("type")
class TypeAction(ActionBase):
    @property
    def name(self) -> str:
        return "type"

    async def execute(self, ctx: ActionContext) -> ActionResult:
        declared = (
            extract_declared_strategy(ctx.kwargs, allowed=_TYPE_ALLOWED_DECLARED_KEYS)
            if ctx.kwargs else None
        )

        if declared:
            if not ctx.args:
                return ActionResult.fail(
                    "type[] 声明式 kwarg 场景仍需一个位置参数（待输入文字）："
                    "type[文字, selector=...]"
                )
            text = _strip_quotes(ctx.args[-1])
            element_name = ctx.args[0].strip() if len(ctx.args) > 1 else declared_strategy_label(declared)
        else:
            if len(ctx.args) < 2:
                return ActionResult.fail("type[] requires two arguments: type[element_name, text]")
            element_name = ctx.args[0].strip()
            text = _strip_quotes(ctx.args[1])

        t0 = time.monotonic()
        try:
            retries = max(int(ctx.kwargs.get("retry", 0)), 0)
            interval_ms = max(float(ctx.kwargs.get("retry_interval_ms", 1000)), 0.0)
        except (TypeError, ValueError):
            return ActionResult.fail("type retry/retry_interval_ms must be numeric")

        resolve_result = None
        for attempt in range(retries + 1):
            if declared:
                resolve_result = await ctx.resolver.resolve_with_strategy(element_name, declared)
            else:
                resolve_result = await ctx.resolver.resolve(element_name)
            if resolve_result.found:
                break
            if attempt < retries:
                await asyncio.sleep(interval_ms / 1000)
        if resolve_result is None or not resolve_result.found:
            dur = (time.monotonic() - t0) * 1000
            msg = f"type: element not found after {retries + 1} attempts: '{element_name}'"
            log_action_execution("type", element_name, passed=False, duration_ms=dur, level="NOT_FOUND", message=msg)
            return ActionResult.fail(msg, duration_ms=dur)

        element = resolve_result.element
        level_name = getattr(resolve_result.level, "name", str(resolve_result.level))
        if element.raw is None and element.coords is not None:
            region_str = ctx.kwargs.get("region") if ctx.kwargs else None
            try:
                x, y = resolve_click_point(element.coords, element.bbox, region_str)
            except ValueError as exc:
                return ActionResult.fail(
                    f"type: {exc}", duration_ms=(time.monotonic() - t0) * 1000,
                )
            if region_str and element.bbox is None:
                logger.debug(
                    "[type] region=%r 声明但当前命中无 bbox（非 L7），忽略，使用中心点",
                    region_str,
                )
            if hasattr(ctx.engine, "type_at_viewport_coords"):
                await ctx.engine.type_at_viewport_coords(x, y, text, clear=True)
                dur = (time.monotonic() - t0) * 1000
                msg = f"typed '{text}' into '{element_name}' via viewport coords=({x:.0f}, {y:.0f})"
                log_action_execution(
                    "type", element_name, passed=True, duration_ms=dur,
                    level=level_name, coords=(round(x, 1), round(y, 1)),
                    bbox=getattr(element, "bbox", None), region=region_str or "", message=msg,
                )
                return ActionResult.ok(msg, duration_ms=dur)
            await type_at_coords(x, y, text, clear=True)
            dur = (time.monotonic() - t0) * 1000
            msg = f"typed '{text}' into '{element_name}' via pyautogui coords=({x:.0f}, {y:.0f})"
            log_action_execution(
                "type", element_name, passed=True, duration_ms=dur,
                level=level_name, coords=(round(x, 1), round(y, 1)),
                bbox=getattr(element, "bbox", None), region=region_str or "", message=msg,
            )
            return ActionResult.ok(msg, duration_ms=dur)

        await ctx.engine.type_text(element, text, clear=True)
        dur = (time.monotonic() - t0) * 1000
        msg = f"typed '{text}' into '{element_name}'"
        log_action_execution(
            "type", element_name, passed=True, duration_ms=dur,
            level=level_name, selector=getattr(element, "element_id", ""), message=msg,
        )
        return ActionResult.ok(msg, duration_ms=dur)
