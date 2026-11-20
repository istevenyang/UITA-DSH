"""click[element_name] action。

覆盖层感知（P1 新增）：click 前后调用 ctx.resolver.on_click_start()/on_click_end()
以支持 DOM 覆盖层（dropdown/modal/drawer）检测——仅 Web 引擎生效，其余引擎
on_click_start() 恒返回空集合、on_click_end() 恒无副作用（见 Resolver 内 hasattr
判断），对现有用例零行为影响。

声明式首策略 kwarg（P3 新增，见 docs/plans/20260703103100-...）：
click[selector=".btn"] / click[role=button] / click[text=发送] 跳过路由决策
直达指定策略，失败后仍按 ActionRouter 剩余策略降级。不声明 kwarg 时走
ctx.resolver.resolve() 现状路径，零回归。

坐标句柄（P4 新增，见 docs/plans/20260703103200-...）：命中元素若
element.raw is None 且 element.coords 非 None（视觉指纹/LLM 视觉命中），
走 pyautogui 坐标点击分支，不调用 ctx.engine.click()（坐标定位与具体元素
引用无关）。P3/P4 之前的所有命中路径 element.coords 恒为 None，此分支不会
触发，零回归。

区域点击 kwarg（2026-07-14 新增，见 docs/plans/20260714160000-...）：
click[图标, region=top_right] 在坐标句柄命中且 element.bbox 非 None（仅
L3.8 视觉指纹命中时填充）时，按声明的九宫格区域计算点击坐标，默认仍为
center（即 element.coords）。未声明 region= 或命中结果无 bbox（L4/普通
元素）时行为与改造前完全一致，零回归；非法 region 取值显式报错。

弹窗检测 kwarg（2026-07-25 新增，见 docs/plans/20260725110000-sample_web-device-
status-card-popup-window.md）：click[元素, expect_new_window=true] 在点击
前调用 ctx.engine.snapshot_visible_windows() 记录基线窗口快照，点击后调用
ctx.engine.register_any_new_window() 轮询检测新出现的窗口并注入引擎窗口栈
（用于点击后不确定目标 PID 的弹窗场景，如卡片内箭头按钮）。不声明该 kwarg
（默认）或引擎不支持这两个方法（hasattr 兜底）时行为与改造前完全一致，
零回归、零性能影响。

prompt= 直达 L4 视觉定位（2026-07-30 新增，见 docs/plans/20260730180000-
click-assert-prompt-l4-direct.md）：click[prompt="自然语言描述目标"] 检测到
该 kwarg 时跳过声明式策略（selector=/role=/text=）与标准 resolve()，直接
调用 ctx.resolver.resolve_via_l4_prompt() 走 L4（vision_client.locate()）
拿坐标，复用下方已有的坐标句柄点击分支（Playwright viewport / pyautogui
兜底，region= 联动同样生效）。prompt= 与其它声明式 kwarg/位置参数互斥，
同时声明时 prompt= 优先级最高，其余被忽略（打 warning，不报错）。未声明
prompt=（默认）时行为与改造前完全一致，零回归。
"""
from __future__ import annotations

import asyncio
import logging
import time

from uita.core.actions.base import ActionBase, ActionContext, ActionResult
from uita.core.actions.common._pyautogui_helper import click_at_coords
from uita.core.actions.registry import register_action
from uita.core.logging_manager import log_action_execution
from uita.core.resolver.click_region import resolve_click_point
from uita.core.resolver.router import declared_strategy_label, extract_declared_strategy

logger = logging.getLogger(__name__)


@register_action("click")
class ClickAction(ActionBase):
    @property
    def name(self) -> str:
        return "click"

    async def execute(self, ctx: ActionContext) -> ActionResult:
        try:
            retries = max(0, int(ctx.kwargs.get("retry", "0")))
            interval_ms = max(0.0, float(ctx.kwargs.get("retry_interval_ms", "1000")))
            timeout_ms = (
                max(0.0, float(ctx.kwargs["timeout_ms"]))
                if "timeout_ms" in ctx.kwargs
                else None
            )
            optional = ctx.kwargs.get("optional", "false").strip().lower() in (
                "true", "yes", "1", "on",
            )
        except ValueError:
            return ActionResult.fail(
                "click retry/retry_interval_ms/timeout_ms must be numeric"
            )

        last_result = None
        last_error = None
        for attempt in range(retries + 1):
            try:
                if timeout_ms is None:
                    last_result = await self._execute_once(ctx)
                else:
                    last_result = await asyncio.wait_for(
                        self._execute_once(ctx), timeout=timeout_ms / 1000,
                    )
                if last_result.passed:
                    return last_result
            except Exception as exc:  # locator failures are retryable like legacy_runner
                if retries == 0 and not optional:
                    raise
                last_error = exc
            if attempt < retries:
                await asyncio.sleep(interval_ms / 1000)
        if last_result is not None:
            if optional and not last_result.passed:
                return ActionResult.ok(
                    f"optional click skipped after {retries + 1} attempts: {last_result.message}"
                )
            return last_result
        if optional:
            return ActionResult.ok(
                f"optional click skipped after {retries + 1} attempts: {last_error}"
            )
        return ActionResult.fail(f"click failed after {retries + 1} attempts: {last_error}")

    async def _execute_once(self, ctx: ActionContext) -> ActionResult:
        prompt = ctx.kwargs.get("prompt") if ctx.kwargs else None
        if prompt:
            return await self._execute_prompt(ctx, prompt)

        declared = extract_declared_strategy(ctx.kwargs) if ctx.kwargs else None
        if not ctx.args and not declared:
            return ActionResult.fail(
                "click[] requires at least one argument (element name) "
                "or a declared strategy kwarg (selector=/role=/text=)"
            )

        element_name = ctx.args[0].strip() if ctx.args else declared_strategy_label(declared)
        t0 = time.monotonic()

        if declared:
            resolve_result = await ctx.resolver.resolve_with_strategy(element_name, declared)
        else:
            # 外层调用方（如 apps/sample_web/actions.py::_click_nav_with_visual_fallback，为
            # 结构化导航探测是否存在）可能已对同名元素执行过一次 resolve，通过
            # ctx.extra["prewarmed_resolve"]（dict[name, ResolveResult]）传入，命中则
            # 复用，省去重复的全链路 resolve；用 pop() 一次性消费，不影响 click 的
            # retry 语义（重试轮次仍强制真实 resolve，见
            # docs/plans/archive/09-apps-business-实施纪要.md）。
            prewarmed_resolve: dict = ctx.extra.get("prewarmed_resolve") or {}
            _pre = prewarmed_resolve.pop(element_name, None)
            resolve_result = (
                _pre if _pre is not None else await ctx.resolver.resolve(element_name)
            )
        if not resolve_result.found:
            dur = (time.monotonic() - t0) * 1000
            log_action_execution(
                "click", element_name, passed=False, duration_ms=dur,
                level="NOT_FOUND", message=f"click: element not found: '{element_name}'",
            )
            return ActionResult.fail(
                f"click: element not found: '{element_name}'",
                duration_ms=dur,
            )

        element = resolve_result.element
        level_name = getattr(resolve_result.level, "name", str(resolve_result.level))
        if element.raw is None and element.coords is not None:
            # L7/L8 坐标句柄：优先用 Playwright viewport 精确点击（无窗口偏移/DPR 问题）
            return await self._click_coords_handle(ctx, element, element_name, t0, level_name=level_name)

        expect_new_window = (
            str(ctx.kwargs.get("expect_new_window", "")).lower() == "true"
            if ctx.kwargs else False
        )
        snapshot_fn = getattr(ctx.engine, "snapshot_visible_windows", None) if expect_new_window else None
        before_snapshot = await snapshot_fn() if callable(snapshot_fn) else None

        before_overlay = await ctx.resolver.on_click_start()
        mode = str(ctx.kwargs.get("mode", "")).strip().lower()
        if mode == "select":
            select_fn = getattr(ctx.engine, "select", None)
            if not callable(select_fn):
                return ActionResult.fail("click[mode=select] 当前引擎不支持 SelectionItem.Select")
            await select_fn(element)
        else:
            await ctx.engine.click(element)
        await ctx.resolver.on_click_end(before_overlay)

        if expect_new_window and before_snapshot is not None:
            register_fn = getattr(ctx.engine, "register_any_new_window", None)
            if callable(register_fn):
                await register_fn(before_snapshot, timeout=5.0)

        dur = (time.monotonic() - t0) * 1000
        log_action_execution(
            "click", element_name, passed=True, duration_ms=dur,
            level=level_name, selector=getattr(element, "element_id", ""),
            coords=getattr(element, "coords", None), bbox=getattr(element, "bbox", None),
            message=f"clicked '{element_name}'",
        )
        return ActionResult.ok(
            f"clicked '{element_name}'",
            duration_ms=dur,
        )

    async def _click_coords_handle(
        self, ctx: ActionContext, element, element_name: str, t0: float, level_name: str = "L7_VISUAL_FP",
    ) -> ActionResult:
        """坐标句柄点击（L7/L8 视觉命中共用）：region= 联动 + Playwright viewport
        精确点击 / pyautogui 兜底。从 execute() 抽出，供 prompt= 直达 L8 分支复用。
        """
        region_str = ctx.kwargs.get("region") if ctx.kwargs else None
        try:
            x, y = resolve_click_point(element.coords, element.bbox, region_str)
        except ValueError as exc:
            return ActionResult.fail(
                f"click: {exc}", duration_ms=(time.monotonic() - t0) * 1000,
            )
        if region_str and element.bbox is None:
            logger.debug(
                "[click] region=%r 声明但当前命中无 bbox（非 L7），忽略，使用中心点",
                region_str,
            )
        if hasattr(ctx.engine, "click_at_viewport_coords"):
            await ctx.engine.click_at_viewport_coords(x, y)
            dur = (time.monotonic() - t0) * 1000
            msg = f"clicked '{element_name}' via viewport coords=({x:.0f}, {y:.0f})"
            log_action_execution(
                "click", element_name, passed=True, duration_ms=dur,
                level=level_name, coords=(round(x, 1), round(y, 1)),
                bbox=getattr(element, "bbox", None), region=region_str or "",
                message=msg,
            )
            return ActionResult.ok(msg, duration_ms=dur)
        # 兜底：pyautogui（非 Playwright 引擎时）
        await click_at_coords(x, y)
        dur = (time.monotonic() - t0) * 1000
        msg = f"clicked '{element_name}' via pyautogui coords=({x:.0f}, {y:.0f})"
        log_action_execution(
            "click", element_name, passed=True, duration_ms=dur,
            level=level_name, coords=(round(x, 1), round(y, 1)),
            bbox=getattr(element, "bbox", None), region=region_str or "",
            message=msg,
        )
        return ActionResult.ok(msg, duration_ms=dur)


    async def _execute_prompt(self, ctx: ActionContext, prompt: str) -> ActionResult:
        """prompt= 直达 L4 视觉定位：跳过声明式策略/标准 resolve()，直接调用
        ctx.resolver.resolve_via_l4_prompt() 拿坐标，复用坐标句柄点击分支。

        prompt= 与位置参数/其它声明式 kwarg（selector=/role=/text=）互斥，
        同时声明时 prompt= 优先级最高，其余被忽略（打 warning，不报错）。
        region= 例外：与 prompt= 不冲突，可联动生效。
        """
        other_kwargs = {
            k: v for k, v in (ctx.kwargs or {}).items() if k not in ("prompt", "region")
        }
        if ctx.args or other_kwargs:
            logger.warning(
                "[click] prompt=%r 已声明，位置参数 %s / 其它 kwarg %s 均被忽略"
                "（prompt= 优先级最高）",
                prompt, ctx.args, other_kwargs,
            )

        t0 = time.monotonic()
        resolve_result = await ctx.resolver.resolve_via_l4_prompt(prompt)
        if not resolve_result.found:
            return ActionResult.fail(
                f"click: prompt= 视觉定位未命中: '{prompt}'"
                "（无视觉客户端/截图失败/AI 未定位到目标）",
                duration_ms=(time.monotonic() - t0) * 1000,
            )
        return await self._click_coords_handle(ctx, resolve_result.element, prompt, t0)
