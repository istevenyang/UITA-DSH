"""
ContentJudger — contains=/not_contains= 文本内容判定。

优先读 DOM 文本比对；元素定位全部失败（L1→L3 未命中）时，
改用 vision_client 截图 + AI 视觉兜底判定。
"""
from __future__ import annotations

import inspect
import logging
import math
import struct
from typing import TYPE_CHECKING, Optional

from .base import JudgeResult, JudgerBase, JudgerSpec
from .existence import _record_observation, _strict_observation, _valid_rect

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult

logger = logging.getLogger(__name__)


class ContentJudger(JudgerBase):
    name = "content"

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        # ── 元素找到 + 有 DOM 引用 → 读文本比对 ──────────────────────────
        if resolve_result is not None and resolve_result.found:
            element = resolve_result.element
            if element.raw is None:
                if _strict_observation(ctx):
                    return await self._strict_visual_text(ctx, resolve_result, spec)
                # 坐标型结果（L7 VISUAL_FP / L8 LLM_VISUAL），无 DOM 引用
                return JudgeResult(
                    ok=False,
                    reason=(
                        f"无法判定文本内容 '{spec.description}'："
                        f"元素由 {resolve_result.level.value} 坐标定位，无 DOM 引用"
                    ),
                )

            text = await ctx.engine.get_text(element)
            contains = spec.kwargs.get("contains")
            not_contains = spec.kwargs.get("not_contains")

            if contains is not None and contains not in text:
                return JudgeResult(
                    ok=False, reason=f"文本 {text!r} 不包含 {contains!r}", last_value=text,
                )
            if not_contains is not None and not_contains in text:
                return JudgeResult(
                    ok=False, reason=f"文本 {text!r} 意外包含 {not_contains!r}", last_value=text,
                )
            return JudgeResult(ok=True, last_value=text)

        if _strict_observation(ctx):
            return await self._strict_visual_text(ctx, resolve_result, spec)

        # ── 元素未找到 → 截图 + AI 视觉兜底判定 ──────────────────────────
        contains = spec.kwargs.get("contains")
        not_contains = spec.kwargs.get("not_contains")

        # 没有视觉判定条件时退出（不应发生，contains/not_contains 是 ContentJudger 的唯一触发条件）
        if contains is None and not_contains is None:
            return JudgeResult(
                ok=False, reason=f"元素不存在，无法判定文本内容: '{spec.description}'",
            )

        vision_client = self._get_vision_client(ctx)
        if vision_client is None:
            return JudgeResult(
                ok=False, reason=f"元素不存在且视觉客户端未注入，无法判定: '{spec.description}'",
            )

        try:
            screenshot = await ctx.engine.take_screenshot()
        except Exception as exc:
            logger.debug("[ContentJudger] take_screenshot 异常: %s", exc)
            return JudgeResult(
                ok=False, reason=f"截图失败，无法进行视觉判定: '{spec.description}'",
            )
        if not screenshot:
            return JudgeResult(
                ok=False, reason=f"截图为空，无法进行视觉判定: '{spec.description}'",
            )

        # 根据 contains / not_contains 构造问题
        if contains is not None:
            question = (
                f"请判断当前界面中是否出现了'{contains}'描述的内容"
            )
        else:
            question = (
                f"请判断当前界面中是否没有出现'{not_contains}'描述的内容"
            )

        try:
            verdict = await vision_client.judge(question, screenshot)
        except Exception as exc:
            logger.debug("[ContentJudger] vision_client.judge 异常: %s", exc)
            return JudgeResult(
                ok=False, reason=f"AI 视觉判定异常: '{spec.description}' — {exc}",
            )

        if contains is not None:
            return JudgeResult(
                ok=verdict,
                reason=(
                    f"AI 视觉判定: contains={contains!r} → {'✅ 存在' if verdict else '❌ 不存在'}"
                ),
            )
        else:
            return JudgeResult(
                ok=verdict,
                reason=(
                    f"AI 视觉判定: not_contains={not_contains!r} → "
                    f"{'✅ 未出现' if verdict else '❌ 仍存在'}"
                ),
            )

    async def _strict_visual_text(self, ctx, resolve_result, spec: JudgerSpec) -> JudgeResult:
        """驱动须裁切原始 PNG 并返回区域原文；缺能力时拒绝整屏视觉。"""
        def unknown(reason):
            _record_observation(ctx, None, reason)
            return JudgeResult(ok=False, reason=f"UNKNOWN: {reason}: '{spec.description}'")

        cropper = ctx.extra.get("assert_region_cropper")
        reader = ctx.extra.get("assert_region_text_reader")
        if not callable(cropper) or not callable(reader):
            return unknown("缺少目标区域裁切或文本读取驱动")
        element = resolve_result.element if resolve_result is not None else None
        if element is None:
            return unknown("缺少可靠目标元素矩形")
        try:
            rect = _valid_rect(await ctx.engine.get_element_rect(element))
            if rect is None:
                return unknown("目标元素矩形无效")
            screenshot = await ctx.engine.take_screenshot()
            frame = _valid_rect(await ctx.engine.get_last_screenshot_rect())
            if frame is None:
                return unknown("缺少实际截图矩形，拒绝全屏降级")
            if _valid_rect(await ctx.engine.get_element_rect(element)) != rect:
                return unknown("截图期间目标矩形变化")
            if rect[0] < frame[0] or rect[1] < frame[1] or (
                rect[0] + rect[2] > frame[0] + frame[2] or rect[1] + rect[3] > frame[1] + frame[3]
            ):
                return unknown("目标区域不完整或不属于当前截图窗口")
            dimensions = self._png_dimensions(screenshot)
            if dimensions is None:
                return unknown("截图 PNG 无效")
            sx, sy = dimensions[0] / frame[2], dimensions[1] / frame[3]
            left = math.floor((rect[0] - frame[0]) * sx)
            top = math.floor((rect[1] - frame[1]) * sy)
            right = math.ceil((rect[0] + rect[2] - frame[0]) * sx)
            bottom = math.ceil((rect[1] + rect[3] - frame[1]) * sy)
            cropped = cropper(screenshot, rect, frame)
            if inspect.isawaitable(cropped):
                cropped = await cropped
            if self._png_dimensions(cropped) != (right - left, bottom - top):
                return unknown("裁切尺寸不匹配目标区域")
            if rect != frame and cropped == screenshot:
                return unknown("裁切驱动返回整屏截图")
            text = reader(cropped)
            if inspect.isawaitable(text):
                text = await text
            if not isinstance(text, str) or not text.strip():
                return unknown("区域文本读取无明确原文")
        except Exception:
            return unknown("目标区域截图或裁切读取失败")
        contains = spec.kwargs.get("contains")
        not_contains = spec.kwargs.get("not_contains")
        if contains is None and not_contains is None:
            return unknown("缺少原始文本判据")
        ctx.extra["existence_strategy"] = "ocr"
        if contains is not None and contains not in text:
            return JudgeResult(ok=False, reason=f"区域原文 {text!r} 不包含 {contains!r}", last_value=text)
        if not_contains is not None and not_contains in text:
            return JudgeResult(ok=False, reason=f"区域原文 {text!r} 意外包含 {not_contains!r}", last_value=text)
        return JudgeResult(ok=True, reason="目标裁切区域原文匹配", last_value=text)

    @staticmethod
    def _png_dimensions(data) -> Optional[tuple]:
        if not isinstance(data, bytes) or len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            return None
        width, height = struct.unpack(">II", data[16:24])
        return (width, height) if width > 0 and height > 0 else None

    @staticmethod
    def _get_vision_client(ctx: "ActionContext") -> Optional[object]:
        """获取视觉客户端，按优先级尝试多条路径。

        1. ctx.resolver.has_vision_client → 取 resolver.vision_client（L8 定位用的实例）
        2. ExistenceJudger.fallback_chain → 最后一级 L8 函数闭包中捕获的 vision_client
        3. 自行构造一个新的 DshVisionClient（最后手段）
        """
        # 路径 1：Resolver 的 vision_client
        if hasattr(ctx.resolver, "has_vision_client") and ctx.resolver.has_vision_client:
            return ctx.resolver.vision_client

        # 路径 2：ExistenceJudger fallback_chain 中的 L8 闭包
        try:
            from uita.core.actions.judgers.existence import ExistenceJudger
            for fn in reversed(ExistenceJudger.fallback_chain):
                if getattr(fn, "_uita_l8_layer", False):
                    # 闭包捕获了 vision_client
                    import inspect
                    cells = getattr(fn, "__closure__", None)
                    if cells:
                        for cell in cells:
                            try:
                                val = cell.cell_contents
                                if val is not None and hasattr(val, "judge"):
                                    return val
                            except (ValueError, AttributeError):
                                continue
        except Exception:
            pass

        # 路径 3：自行构造
        try:
            from uita.core.actions.judgers.l8_integration import build_vision_client
            return build_vision_client("dsh")
        except Exception:
            pass

        return None
