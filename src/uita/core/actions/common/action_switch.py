"""
switch[<app>] / switch_back[] — 跨应用切换 Action。

switch[sample_desktop]              切换焦点到目标应用
switch[sample_desktop, timeout=30s]  带超时
switch[sample_desktop, reuse=false] 强制重建引擎实例
switch_back[]               切回 app_stack 栈顶的前一个 app
switch_back[cleanup=true]  切回前清理当前 app

内部调用 AppLifecycleStack.switch_to() / switch_back()（Python 接口已就绪，
见 docs/plans/archive/01-engine-lifecycle-实施纪要.md）。

跨引擎类型切换天然支持：sample_console(Playwright) → sample_web(MacAppKit) → switch_back → sample_console(Playwright)
原实例复用（_cache 按 (app, platform) 缓存，同一 app 复用同一 engine 实例）。

计划文档：docs/plans/archive/02-dsl-actions-实施纪要.md

switch[api]（2026-08-25 新增，"api" 为保留关键字，非真实 app 名）：临时切
换到当前 case 的 app 已注册的 API 引擎（core/lifecycle/api_engine_slot.py），
与 AppLifecycleStack 完全独立并行，不影响既有 switch[<真实app>] 逻辑。见
docs/plans/done/archive_202608.md §2.1。
"""
from __future__ import annotations

import asyncio
import logging
import time

from uita.core.actions.base import ActionBase, ActionContext, ActionResult
from uita.core.actions.registry import register_action
from uita.core.lifecycle.api_engine_slot import ApiEngineSlotError, get_default_api_slot
from uita.core.lifecycle.app_lifecycle_stack import get_default_stack
from uita.core.logging_manager import log_action_execution

logger = logging.getLogger("actions.switch")

_API_MODE_KEYWORD = "api"


def _parse_bool(val: str, default: bool = False) -> bool:
    v = val.strip().lower()
    if not v:
        return default
    return v in ("true", "yes", "1", "on")


def _parse_timeout(val: str, default_s: float = 30.0) -> float:
    v = val.strip()
    if not v:
        return default_s
    try:
        if v.endswith("ms"):
            return float(v[:-2]) / 1000.0
        if v.endswith("s"):
            return float(v[:-1])
        return float(v)
    except ValueError:
        return default_s


@register_action("switch")
class SwitchAction(ActionBase):
    """switch[<app>] — 切换被测应用焦点；switch[api] — 切换到 API 引擎模式。

    内部调用 AppLifecycleStack.switch_to()，自动处理引擎构造 + 冷启动。
    interpreter 在 switch 成功后会更新 self._engine / self._resolver 为目标 app。

    kwarg:
        timeout: 等待目标 app 启动就绪的超时（默认 30s）
        reuse:   true=复用已存在的 engine 实例（如有）；
                 false=强制重新构造（默认 true）
        cleanup: true=切换前对当前 app 做清理（默认 false，本次预留不实现）

    ``switch[api]``（"api" 保留关键字，不受理真实 app 名冲突——已确认
    `apps/` 目录下无 app 字面命名为 "api"）：根据当前 case 的 app（
    ``ctx.engine.app_name``），切换到该 app 已注册的 API 引擎，供后续
    ``get``/``post``/``put``/``delete[]``/``api_assert[]`` 使用。
    """

    @property
    def name(self) -> str:
        return "switch"

    async def execute(self, ctx: ActionContext) -> ActionResult:
        if not ctx.args:
            return ActionResult.fail("switch: 缺少目标 app 名称（用法：switch[<app>]）")

        app_name = ctx.args[0].strip()
        if not app_name:
            return ActionResult.fail("switch: app 名称为空")

        if app_name.casefold() == _API_MODE_KEYWORD:
            return await self._switch_to_api_mode(ctx)

        kwargs = ctx.kwargs
        timeout_s = _parse_timeout(kwargs.get("timeout", ""), default_s=30.0)
        # reuse / cleanup 本次预留，不阻断（默认行为已是复用）
        # reuse = _parse_bool(kwargs.get("reuse", "true"), default=True)
        # cleanup = _parse_bool(kwargs.get("cleanup", "false"), default=False)

        stack = get_default_stack()

        # 从当前 engine 获取平台（switch 不跨平台，只切 app）
        current_platform = ctx.engine.platform if ctx.engine else "macos"

        t0 = time.monotonic()
        try:
            mgr = await asyncio.wait_for(
                stack.switch_to(app_name, current_platform, tag_prefix="switch"),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError:
            return ActionResult.fail(
                f"switch[{app_name}] 超时 ({timeout_s}s)",
                duration_ms=(time.monotonic() - t0) * 1000,
            )
        except Exception as exc:
            return ActionResult.fail(
                f"switch[{app_name}] 失败: {exc}",
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        dur = (time.monotonic() - t0) * 1000
        msg = f"switched to {app_name}"
        log_action_execution("switch", app_name, passed=True, duration_ms=dur, message=msg)
        return ActionResult.ok(msg, duration_ms=dur)

    @staticmethod
    async def _switch_to_api_mode(ctx: ActionContext) -> ActionResult:
        """switch[api]：根据当前 case 的 app 切换到已注册的 API 引擎（见
        docs/plans/done/archive_202608.md §2.1）。
        与 AppLifecycleStack 完全独立并行，不触碰其缓存/栈内容。"""
        slot = get_default_api_slot()
        if slot.is_active:
            return ActionResult.fail(
                f"switch[api]: 已处于 API 模式（app={slot.app_name!r}），"
                f"请先 switch_back[] 切回 UI 引擎"
            )
        if ctx.engine is None:
            return ActionResult.fail("switch[api]: 当前无有效引擎，无法确定被测 app")

        current_app_name = ctx.engine.app_name
        current_platform = ctx.engine.platform

        t0 = time.monotonic()
        try:
            await slot.activate(current_app_name, current_platform)
        except ApiEngineSlotError as exc:
            return ActionResult.fail(
                f"switch[api]: {exc}",
                duration_ms=(time.monotonic() - t0) * 1000,
            )
        return ActionResult.ok(
            f"switched to api mode (app={current_app_name})",
            duration_ms=(time.monotonic() - t0) * 1000,
        )


@register_action("switch_back")
class SwitchBackAction(ActionBase):
    """switch_back[] — 切回 app_stack 栈顶的前一个 app；若当前处于
    switch[api] 的 API 模式，则优先退出 API 模式（不触碰 AppLifecycleStack，
    栈内容全程未变，原有 UI 引擎天然保持不变，见计划文档 20260825090000 §2.1）。

    内部调用 AppLifecycleStack.switch_back()，自动对上一个 app 做 recover()
    （健康检查 + 自愈：LaunchPolicy.recover）。

    kwarg:
        cleanup: true=切回后对刚被弹出的那个 app 做 evict()（进程终止 + 缓存
                 清理），而非仅 recover() 保留复用。适用于 run_exe[] 打开的
                 一次性临时程序（默认 false，sample_web 等正式常驻 app 场景
                 零回归——不清理、继续复用现有引擎实例）。

    计划文档：docs/plans/archive/01-engine-lifecycle-实施纪要.md
    """

    @property
    def name(self) -> str:
        return "switch_back"

    async def execute(self, ctx: ActionContext) -> ActionResult:
        api_slot = get_default_api_slot()
        if api_slot.is_active:
            t0 = time.monotonic()
            await api_slot.deactivate()
            return ActionResult.ok(
                "switched back from api mode",
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        stack = get_default_stack()
        cleanup = _parse_bool(ctx.kwargs.get("cleanup", "false"), default=False)
        popped_mgr = stack.current  # switch_back() 内部会 pop 掉它，这里先记录

        t0 = time.monotonic()
        try:
            mgr = await stack.switch_back()
        except Exception as exc:
            return ActionResult.fail(
                f"switch_back 失败: {exc}",
                duration_ms=(time.monotonic() - t0) * 1000,
            )
        if mgr is None:
            return ActionResult.fail(
                "switch_back 失败：栈已空，无上一个 app 可切回",
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        if cleanup and popped_mgr is not None and popped_mgr is not mgr:
            try:
                await stack.evict(popped_mgr.app_name, popped_mgr.platform)
                logger.info("[switch_back] cleanup=true，已 evict %s/%s",
                            popped_mgr.app_name, popped_mgr.platform)
            except Exception as exc:
                logger.warning("[switch_back] cleanup evict 异常（忽略）: %s", exc)

        return ActionResult.ok(
            f"switched back to {mgr.app_name}",
            duration_ms=(time.monotonic() - t0) * 1000,
        )
