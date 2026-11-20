"""scroll[element_name, direction, amount] action / scroll[direction, amount]（无
元素名，滑动当前窗口）。

amount 缺省或非数字时，动态计算为当前窗口高度的 2/3（留 1/3 重叠区域，避免
惯性/动画导致边界内容遗漏），见 docs/plans/archive/02-dsl-actions-实施纪要.md。

无元素名调用（scroll[up]/scroll[down, 300] 等，第一个参数是方向词）见
docs/plans/archive/02-dsl-actions-实施纪要.md。
"""
from __future__ import annotations
import time
from typing import Optional
from uita.core.actions.base import ActionBase, ActionContext, ActionResult
from uita.core.actions.registry import register_action
from uita.core.logging_manager import log_action_execution

_DEFAULT_AMOUNT_PX = 300  # get_window_rect() 不支持/失败时的历史默认值兜底
_DIRECTIONS = {"up", "down", "left", "right"}


@register_action("scroll")
class ScrollAction(ActionBase):
    @property
    def name(self) -> str:
        return "scroll"

    async def execute(self, ctx: ActionContext) -> ActionResult:
        if not ctx.args:
            return ActionResult.fail(
                "scroll[] 需要至少一个参数: scroll[element, direction, amount] 或 scroll[direction, amount]"
            )

        first = ctx.args[0].strip()
        if first.lower() in _DIRECTIONS:
            return await self._execute_no_element(ctx, first.lower())
        return await self._execute_with_element(ctx, first)

    async def _execute_with_element(self, ctx: ActionContext, element_name: str) -> ActionResult:
        direction = ctx.args[1].strip() if len(ctx.args) > 1 else "down"
        raw_amount = ctx.args[2].strip() if len(ctx.args) > 2 else ""
        amount = self._try_parse_amount(raw_amount)

        t0 = time.monotonic()
        # 先 resolve 再计算动态 amount：resolve 内部会触发引擎的窗口刷新/
        # 窗口栈 reconcile（见 docs/plans/archive/01-engine-lifecycle-实施纪要.md），
        # 确保弹窗场景下 get_window_rect() 读到的是弹窗自身尺寸而非主窗口
        # （真机验证发现：顺序颠倒会导致读到过期的主窗口高度）。
        result = await ctx.resolver.resolve(element_name)
        if not result.found:
            dur = (time.monotonic() - t0) * 1000
            msg = f"scroll: 元素未找到: '{element_name}'"
            log_action_execution("scroll", element_name, passed=False, duration_ms=dur, level="NOT_FOUND", message=msg)
            return ActionResult.fail(msg, duration_ms=dur)
        if amount is None:
            amount = await self._compute_dynamic_amount(ctx)
        await ctx.engine.scroll(result.element, direction, amount)
        dur = (time.monotonic() - t0) * 1000
        msg = f"scroll '{element_name}' {direction} {amount}px"
        log_action_execution(
            "scroll", element_name, passed=True, duration_ms=dur,
            level=getattr(result.level, "name", str(result.level)),
            selector=getattr(result.element, "element_id", ""), message=msg,
        )
        return ActionResult.ok(msg, duration_ms=dur)

    async def _execute_no_element(self, ctx: ActionContext, direction: str) -> ActionResult:
        """scroll[direction, amount] 无元素名调用，滑动当前窗口（element=None）。"""
        raw_amount = ctx.args[1].strip() if len(ctx.args) > 1 else ""
        amount = self._try_parse_amount(raw_amount)

        t0 = time.monotonic()
        # 无元素名场景没有 resolve() 可用，改用 refresh_active_window() 作为
        # 等价的窗口栈 reconcile 触发点（见 EngineBase.refresh_active_window）。
        refresh_fn = getattr(ctx.engine, "refresh_active_window", None)
        if callable(refresh_fn):
            try:
                await refresh_fn()
            except Exception:
                pass
        if amount is None:
            amount = await self._compute_dynamic_amount(ctx)
        await ctx.engine.scroll(None, direction, amount)
        return ActionResult.ok(
            f"scroll (当前窗口) {direction} {amount}px",
            duration_ms=(time.monotonic() - t0) * 1000,
        )

    @staticmethod
    def _try_parse_amount(raw_amount: str) -> Optional[int]:
        if not raw_amount:
            return None
        try:
            return int(raw_amount)
        except ValueError:
            return None  # 非数字（如 "auto"）时走动态计算，不视为参数错误

    @staticmethod
    async def _compute_dynamic_amount(ctx: ActionContext) -> int:
        """滑动距离缺省值：当前窗口高度的 2/3。

        get_window_rect() 不支持（返回 None）或异常时退化为历史默认值
        _DEFAULT_AMOUNT_PX，零回归。
        """
        try:
            rect = await ctx.engine.get_window_rect()
        except Exception:
            return _DEFAULT_AMOUNT_PX
        if rect and len(rect) >= 4 and rect[3] > 0:
            return max(1, int(rect[3] * 2 / 3))
        return _DEFAULT_AMOUNT_PX
