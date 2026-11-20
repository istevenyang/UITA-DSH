"""
wait: [element_name, timeout=Ns]                        — poll until element is visible.
wait: [element_name, state=gone, timeout=Ns]            — poll until element disappears
                                                            (2026-08-21 新增，反向轮询)。
wait: [element_name, poll=200ms]                        — 细粒度轮询间隔（默认 1s）。
wait: [element_name, visual_only=true]                  — 每轮只做 L3.8 视觉指纹匹配，
                                                            不碰 SEMANTIC/ROLE/LLM 策略。
wait: Ns                                                — bare duration sleep.

``visual_only=true`` 时若该元素在 rosetta_map 未配 ``pattern_*``（无指纹），
**立即失败**并说明原因，不空等超时。元素"只有 pattern、没有 win/selector"时
框架本来就会把解析链收缩为视觉指纹（``router.build_effective_chain``），
此时不需要该参数也已等效。
"""
from __future__ import annotations

import asyncio
import time

from uita.core.actions.base import ActionBase, ActionContext, ActionResult
from uita.core.actions.registry import register_action
from uita.core.dsl.nodes import WaitNode
from uita.core.logging_manager import log_action_execution


@register_action("wait")
class WaitAction(ActionBase):
    @property
    def name(self) -> str:
        return "wait"

    async def execute(self, ctx: ActionContext) -> ActionResult:
        # ctx.extra may carry a WaitNode with pre-parsed timeout info
        wait_node: WaitNode | None = ctx.extra.get("wait_node")
        t0 = time.monotonic()

        # Bare duration wait
        if wait_node is not None and wait_node.target is None:
            await asyncio.sleep(wait_node.timeout_s)
            return ActionResult.ok(
                f"waited {wait_node.timeout_s}s",
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        # Element wait (poll until visible, or until gone if state=gone)
        poll_interval = 1.0
        visual_only = False
        if wait_node is not None:
            element_name = wait_node.target
            timeout_s = wait_node.timeout_s
            state = (wait_node.state or "visible").strip().lower()
            poll_interval = max(0.05, float(getattr(wait_node, "poll_interval_s", 1.0) or 1.0))
            visual_only = bool(getattr(wait_node, "visual_only", False))
        elif ctx.args:
            element_name = ctx.args[0].strip()
            timeout_s = 10.0
            state = "visible"
        else:
            return ActionResult.fail("wait: no target specified")

        if state not in ("visible", "gone"):
            return ActionResult.fail(
                f"wait: invalid state={state!r}, expected visible/gone",
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        if visual_only:
            probe = getattr(ctx.resolver, "has_initial_visual_fingerprint", None)
            if callable(probe) and not probe(element_name):
                return ActionResult.fail(
                    f"wait: element '{element_name}' 未配置视觉指纹（rosetta_map 无 pattern），"
                    "visual_only=true 无法使用",
                    duration_ms=(time.monotonic() - t0) * 1000,
                )

        if state == "gone":
            return await self._wait_gone(
                ctx, element_name, timeout_s, t0,
                poll_interval=poll_interval, visual_only=visual_only,
            )

        deadline = time.monotonic() + timeout_s

        while time.monotonic() < deadline:
            # 中间轮询用 local_only=True，只走本地策略（map_exact/semantic/
            # role_match/visual_fp），跳过 LLM_SELECTOR/LLM_VISUAL：元素还没
            # 渲染出来时截图也定位不到，LLM 调用（实测 ~2.7s/次）纯属浪费。
            # 仅最后一次轮询（本次 sleep 后即超时）改用标准全链，给纯视觉/LLM
            # 定位元素最后一次兜底机会，避免因跳过 LLM 而误判超时。
            # 见 docs/plans/archive/02-dsl-actions-实施纪要.md。
            is_last_attempt = (time.monotonic() + poll_interval) >= deadline
            if visual_only:
                # 只做 L3.8 指纹（不叠加任何结构化/LLM 策略），命中即视为出现
                resolve_result = await ctx.resolver.resolve_visual(element_name)
            else:
                resolve_result = await ctx.resolver.resolve(
                    element_name, local_only=not is_last_attempt
                )
            if resolve_result.found:
                element = resolve_result.element
                # L3.8/L4 纯坐标句柄（raw=None, coords 非 None）：视觉指纹/LLM
                # 视觉定位已确认元素可见，跳过 is_visible() 校验——该方法契约只
                # 认原生控件引用，坐标句柄天然 raw=None 会被误判不可见，导致
                # 自愈命中结果被吞掉。与 ExistenceJudger.check() 的同类豁免保持
                # 一致（core/actions/judgers/existence.py），见
                # docs/plans/archive/01-engine-lifecycle-实施纪要.md。
                if element.raw is None and getattr(element, "coords", None) is not None:
                    ctx.extra["_wait_resolve_result"] = resolve_result
                    dur = (time.monotonic() - t0) * 1000
                    msg = f"element '{element_name}' appeared in {dur:.0f}ms"
                    log_action_execution(
                        "wait", element_name, passed=True, duration_ms=dur,
                        level=getattr(resolve_result.level, "name", str(resolve_result.level)),
                        coords=getattr(element, "coords", None), bbox=getattr(element, "bbox", None),
                        message=msg,
                    )
                    return ActionResult.ok(msg, duration_ms=dur)
                visible = await ctx.engine.is_visible(element)
                if visible:
                    ctx.extra["_wait_resolve_result"] = resolve_result
                    dur = (time.monotonic() - t0) * 1000
                    msg = f"element '{element_name}' appeared in {dur:.0f}ms"
                    log_action_execution(
                        "wait", element_name, passed=True, duration_ms=dur,
                        level=getattr(resolve_result.level, "name", str(resolve_result.level)),
                        selector=getattr(element, "element_id", ""),
                        message=msg,
                    )
                    return ActionResult.ok(msg, duration_ms=dur)
            await asyncio.sleep(poll_interval)

        dur = (time.monotonic() - t0) * 1000
        msg = f"wait: element '{element_name}' not visible within {timeout_s}s"
        log_action_execution("wait", element_name, passed=False, duration_ms=dur, level="NOT_FOUND", message=msg)
        return ActionResult.fail(msg, duration_ms=dur)

    async def _wait_gone(
        self, ctx: ActionContext, element_name: str, timeout_s: float, t0: float,
        *, poll_interval: float = 1.0, visual_only: bool = False,
    ) -> ActionResult:
        """反向轮询：等待元素消失（找不到 / 不可见）才算成功。

        2026-08-21 新增（S2/S3 场景：边缘唤醒浮层/悬浮球"鼠标移开后状态回退"
        验证）。始终走 local_only=True（不落 LLM 视觉兜底）——"消失"是否定
        判定，LLM 兜底的价值（帮纯视觉/语义策略找到位置漂移的元素）在此不
        成立，且避免每轮 poll 都触发一次 ~2.7s 的 LLM 调用拖慢超时判定。
        """
        deadline = time.monotonic() + timeout_s

        while time.monotonic() < deadline:
            if visual_only:
                resolve_result = await ctx.resolver.resolve_visual(element_name)
            else:
                resolve_result = await ctx.resolver.resolve(element_name, local_only=True)
            if not resolve_result.found:
                return ActionResult.ok(
                    f"element '{element_name}' gone (not found) in "
                    f"{(time.monotonic()-t0)*1000:.0f}ms",
                    duration_ms=(time.monotonic() - t0) * 1000,
                )
            element = resolve_result.element
            if element.raw is None and getattr(element, "coords", None) is not None:
                # 坐标句柄（视觉指纹/LLM 视觉）：本轮命中即视为"仍可见"，继续轮询。
                pass
            else:
                visible = await ctx.engine.is_visible(element)
                if not visible:
                    return ActionResult.ok(
                        f"element '{element_name}' gone (not visible) in "
                        f"{(time.monotonic()-t0)*1000:.0f}ms",
                        duration_ms=(time.monotonic() - t0) * 1000,
                    )
            await asyncio.sleep(poll_interval)

        return ActionResult.fail(
            f"wait: element '{element_name}' still present/visible after {timeout_s}s "
            f"(state=gone expected)",
            duration_ms=(time.monotonic() - t0) * 1000,
        )

