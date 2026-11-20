"""L7：assert_fingerprint（简称 ASSERT_FP）—— 视觉指纹存在性断言层。

当 resolver 的结构化链（L1~L6）返回 NOT_FOUND 时，
若被断言元素在 rosetta_map 中保有视觉指纹（pattern），则对当前窗口截图执行
与 L7（`l7_visual_fingerprint.py::VisualFingerprintResolver`）完全同一套模板匹配
（ORB→SIFT→matchTemplate 三级降级）——命中 → assert 通过。

**仅 assert[]/verify[] 触发**：`if[]`/`elif[]` 条件求值不应触发本层（见
`_assert_fp_check_fn` 首行的 `ctx.extra["assert_call"]` 判断）。

架构：与 L8 OCR / L8 LLM 视觉并列注入 `ExistenceJudger.fallback_chain`，均由
`core/runner/resolver_factory.py::build_resolver_for_app()` 统一装配。装配顺序要求
**先注入 L8 OCR，再注入本层**——两者注入逻辑都是"插入链首"，后调用者会把先调用者
挤到更前面，因此最终链首是本层（L7 视觉指纹断言），其次 L8 OCR。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from uita.core.resolver.l7_visual_fingerprint import VisualFingerprintResolver

logger = logging.getLogger(__name__)

_ASSERT_FP_TAG = "_uita_assert_fp_layer"


def _import_cv_np():
    """cv2/numpy 惰性成对导入。"""
    try:
        import cv2  # noqa: F401
        import numpy as np  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "L7 assert_fingerprint 需要 opencv-python，"
            "请先 pip install opencv-python"
        ) from exc
    return cv2, np


def inject_assert_fingerprint_fallback(fingerprint_resolver: "VisualFingerprintResolver") -> None:
    """向 ExistenceJudger.fallback_chain 注入 L7 assert_fingerprint 检查层。"""
    if fingerprint_resolver is None:
        logger.debug("[L7][ASSERT_FP] fingerprint_resolver 为空，跳过注入")
        return

    from uita.core.actions.judgers.existence import ExistenceJudger

    if any(getattr(fn, _ASSERT_FP_TAG, False) for fn in ExistenceJudger.fallback_chain):
        logger.debug("[L7][ASSERT_FP] 已注入，跳过")
        return

    async def _assert_fp_check_fn(ctx, description: str) -> Optional[bool]:
        """L7 check 函数 → ExistenceCheckFn 协议。"""
        if not ctx.extra.get("assert_call"):
            return None

        if not fingerprint_resolver.has_any_fingerprint(description):
            return None

        try:
            screenshot = await ctx.engine.take_screenshot()
        except Exception as exc:
            logger.debug("[L7][ASSERT_FP] take_screenshot 异常（忽略）: %s", exc)
            return None

        if not screenshot:
            logger.debug("[L7][ASSERT_FP] 截图为空: %r", description)
            return None

        try:
            cv2, np = _import_cv_np()
            arr = np.frombuffer(screenshot, dtype=np.uint8)
            screenshot_bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except Exception as exc:
            logger.warning("[L7][ASSERT_FP] 截图解码失败: %r — %s", description, exc)
            return None

        if screenshot_bgr is None:
            logger.debug("[L7][ASSERT_FP] 截图解码结果为空: %r", description)
            return None

        try:
            match = fingerprint_resolver.match(description, screenshot_bgr)
        except Exception as exc:
            logger.warning(
                "[L7][ASSERT_FP] match() 异常（忽略，交下一层）: %r — %s",
                description, exc, exc_info=True,
            )
            return None

        if match is not None:
            logger.debug(
                "[L7][ASSERT_FP] 视觉指纹命中: %r conf=%.2f algo=%s",
                description, match.confidence, match.match_algo,
            )
            return True

        logger.debug("[L7][ASSERT_FP] 视觉指纹未命中，交下一层: %r", description)
        return None

    setattr(_assert_fp_check_fn, _ASSERT_FP_TAG, True)
    ExistenceJudger.fallback_chain = [_assert_fp_check_fn, *ExistenceJudger.fallback_chain]
    logger.info("[L7][ASSERT_FP] 已注入 ExistenceJudger.fallback_chain（链首）")
