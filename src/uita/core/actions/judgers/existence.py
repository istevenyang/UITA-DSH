"""
ExistenceJudger — 默认判定器（无 kwarg 时使用），存在 + 可见。

visible= 开关（默认 true，零回归）：
- `assert[元素]`               → 存在 + 可见（默认，与改造前完全一致）
- `assert[元素, visible=false]` → 仅断言存在（resolve 命中即可），跳过 is_visible
  检查。用于 disabled/未启用态控件（如未选文件时的"立即清理按钮"、disabled
  下拉框等）——这类元素在 AX/UIA 树中确实存在，但引擎 is_visible 因 enabled=False
  返回 False，默认断言会误判为"存在但不可见"而失败。visible=false 时只校验元素
  在页面中已注册/渲染，不校验其可交互性。

架构预留（面向 L8 OCR、L8 视觉判断）：`fallback_chain` 是一个
有序的降级函数列表。落地 L8 视觉判断时只需 `ExistenceJudger.fallback_chain.append(dsh_visual_judge_fn)`，
接入 L8 OCR 时只需 `insert(0, ocr_judge_fn)`，均不改动 check() 方法体。
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import uita.config as settings
from uita.core.platform.app_registry import get_extra_config

from .base import JudgeResult, JudgerBase, JudgerSpec

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult

# 返回 True/False = 该层给出了明确结论；返回 None = 该层无法判断，交给下一层
ExistenceCheckFn = Callable[["ActionContext", str], Awaitable[Optional[bool]]]

# visible= 取值被判定为"关闭可见性校验"的字符串集合（大小写不敏感）
_VISIBLE_FALSE_VALUES = frozenset({"false", "0", "no", "off", "none"})


def _want_visible(kwargs: dict) -> bool:
    """解析 visible= kwarg：默认（未声明）为 True（存在+可见，零回归）；
    显式传 false/0/no/off/none 时返回 False（仅断言存在，跳过可见性检查）。"""
    raw = kwargs.get("visible")
    if raw is None:
        return True
    return str(raw).strip().lower() not in _VISIBLE_FALSE_VALUES


def _strict_observation(ctx: "ActionContext") -> bool:
    extra = ctx.extra if isinstance(ctx.extra, dict) else {}
    if "strict_assert_observation" in extra:
        value = extra["strict_assert_observation"]
    else:
        default = getattr(settings, "STRICT_ASSERT_OBSERVATION", False)
        app = getattr(ctx.engine, "app_name", "")
        platform = getattr(ctx.engine, "platform", "")
        value = get_extra_config(app, platform, "STRICT_ASSERT_OBSERVATION", default=default) if (
            isinstance(app, str) and isinstance(platform, str)
        ) else default
    return value is True or (isinstance(value, str) and value.strip().lower() in {"true", "1", "yes", "on"})


def _valid_rect(rect) -> Optional[tuple]:
    if not isinstance(rect, (tuple, list)) or len(rect) != 4:
        return None
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in rect):
        return None
    if not all(math.isfinite(value) for value in rect) or rect[2] <= 0 or rect[3] <= 0:
        return None
    return tuple(rect)


def _record_observation(ctx, verdict: Optional[bool], reason: str) -> Optional[bool]:
    ctx.extra["existence_observation"] = verdict
    ctx.extra["existence_observation_reason"] = reason
    return verdict


async def _observe_existence(ctx, resolve_result, description: str, *, strict: bool) -> Optional[bool]:
    """严格 False 仅来自代码标记的完整作用域确定性观察，不来自视觉模型否定。"""
    ctx.extra["existence_strategy"] = "structured"
    if resolve_result is not None and resolve_result.found:
        return _record_observation(ctx, True, "resolve_found")
    if ctx.extra.get("repair_mode"):
        return _record_observation(ctx, None if strict else False, "repair_mode_no_observation")
    for layer_fn in ExistenceJudger.fallback_chain:
        try:
            verdict = await layer_fn(ctx, description)
        except Exception:
            if strict:
                continue
            raise
        if verdict is not True and verdict is not False:
            continue
        if strict and verdict is False and (
            not getattr(layer_fn, "_uita_assert_absence_authoritative", False)
            or getattr(layer_fn, "_uita_l8_layer", False)
        ):
            continue
        if getattr(layer_fn, "_uita_l8_ocr_layer", False):
            strategy = "ocr"
        elif getattr(layer_fn, "_uita_assert_fp_layer", False):
            strategy = "assert_fp"
        elif getattr(layer_fn, "_uita_l8_layer", False):
            strategy = "llm_visual"
        else:
            strategy = "fallback"
        ctx.extra["existence_strategy"] = strategy
        return _record_observation(ctx, verdict, strategy)
    return _record_observation(ctx, None if strict else False, "insufficient_observation")


class ExistenceJudger(JudgerBase):
    name = "existence"

    # P2 阶段为空列表；P4/backlog 落地时直接 append/insert，不改本类其余代码。
    fallback_chain: list[ExistenceCheckFn] = []

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        strict = _strict_observation(ctx)
        found = await _observe_existence(ctx, resolve_result, spec.description, strict=strict)
        if found is None:
            return JudgeResult(ok=False, reason=f"UNKNOWN: 元素观察证据不足: '{spec.description}'")
        if not found:
            return JudgeResult(ok=False, reason=f"元素不存在: '{spec.description}'")
        if strict and _want_visible(spec.kwargs):
            element = resolve_result.element if resolve_result is not None else None
            try:
                rect = _valid_rect(await ctx.engine.get_element_rect(element)) if element is not None else None
                window = _valid_rect(await ctx.engine.get_window_rect())
            except Exception:
                rect = window = None
            if rect is None or window is None:
                _record_observation(ctx, None, "missing_target_or_window_rect")
                return JudgeResult(ok=False, reason=f"UNKNOWN: 缺少可靠目标或窗口矩形: '{spec.description}'")
            if min(rect[0] + rect[2], window[0] + window[2]) <= max(rect[0], window[0]) or (
                min(rect[1] + rect[3], window[1] + window[3]) <= max(rect[1], window[1])
            ):
                return JudgeResult(ok=False, reason=f"元素 '{spec.description}' 存在但不可见（窗口外）")

        # visible=false → 仅断言"存在且未被显式隐藏（允许 disabled）"：
        # 放行 enabled=False 的置灰控件（这类元素仍渲染在屏幕上），但仍拒绝
        # AXHidden=True 的真隐藏元素，避免掩盖"实际不可见"造成假阳性。
        # 引擎若提供 is_hidden() 能力（如 MacAppKitEngine）则用它精准区分；
        # 未提供该能力的引擎优雅回退为"仅存在性"（命中即通过），保持向后兼容。
        if not _want_visible(spec.kwargs):
            if resolve_result is None or resolve_result.element is None:
                return JudgeResult(ok=True)
            # 纯坐标句柄（视觉/OCR 定位）：本身已是视觉判断，视为未隐藏
            if resolve_result.element.raw is None and getattr(
                resolve_result.element, "coords", None
            ) is not None:
                return JudgeResult(ok=True)
            is_hidden_fn = getattr(ctx.engine, "is_hidden", None)
            if is_hidden_fn is not None:
                hidden = await is_hidden_fn(resolve_result.element)
                if hidden:
                    return JudgeResult(
                        ok=False,
                        reason=f"元素 '{spec.description}' 已被隐藏（AXHidden=True）",
                    )
            return JudgeResult(ok=True)

        # 结构化命中时同时要求可见，对齐改造前 action_assert.py"存在+可见"语义。
        # 视觉/OCR 降级链命中的场景（found 由 fallback_chain 判定为 True，无
        # resolve_result.element 可查 is_visible）直接视为可见（该层本身已是视觉判断）。
        if resolve_result is None or resolve_result.element is None:
            return JudgeResult(ok=True)

        # L7/L8 纯坐标句柄（raw=None, coords 非 None）：LLM/视觉定位已确认可见，跳过 is_visible
        if resolve_result.element.raw is None and getattr(resolve_result.element, "coords", None) is not None:
            return JudgeResult(ok=True)

        visible = await ctx.engine.is_visible(resolve_result.element)
        if not visible:
            return JudgeResult(ok=False, reason=f"元素 '{spec.description}' 存在但不可见")
        return JudgeResult(ok=True)
