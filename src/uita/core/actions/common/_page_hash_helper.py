"""
_page_hash_helper — 操作前后页面 hash 对比验证的辅助函数（默认关闭功能）。

计划文档：docs/plans/archive/02-dsl-actions-实施纪要.md

算法：8x8 average hash（aHash），与 tools/agent_rosetta/views/orchestrate/
state_graph.py::_ahash() 同一实现思路（灰度缩放 8x8 → 逐像素与均值比较 → 16位 hex），
本次在 core/ 内独立实现（不 import tools/，core/ 与 tools/ 是不同层级的目录，不应
产生跨目录依赖）。仅依赖 Pillow（core/ 既有依赖，见 pattern_codec.py/annotator.py
已用）。

适用范围（用户已确认）：click/type/hover 三个 action，不含 wait/assert/press_key/
scroll 等其余动作。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# 适用 action 集合（用户已确认：click/type/hover，不含 wait/assert/press_key/scroll 等）
HASH_VERIFY_ACTIONS: frozenset = frozenset({"click", "type", "hover"})


def compute_ahash(image_bytes: bytes) -> str:
    """8x8 average hash（16 位 hex）。计算失败（非图片字节/Pillow 未安装）返回空串。"""
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(image_bytes)).convert("L").resize((8, 8))
        pixels = list(img.getdata())
        avg = sum(pixels) / len(pixels) if pixels else 0
        bits = 0
        for p in pixels:
            bits = (bits << 1) | (1 if p >= avg else 0)
        return f"{bits:016x}"
    except Exception as exc:
        logger.debug("[page-hash] aHash 计算失败（忽略）: %s", exc)
        return ""


async def capture_page_hash(engine) -> Optional[str]:
    """截图并计算 hash；截图失败/hash 计算失败均返回 None（调用方跳过对比）。"""
    take_screenshot = getattr(engine, "take_screenshot", None)
    if take_screenshot is None:
        return None
    try:
        screenshot_bytes = await take_screenshot()
    except Exception as exc:
        logger.debug("[page-hash] 截图失败（忽略）: %s", exc)
        return None
    if not screenshot_bytes:
        return None
    h = compute_ahash(screenshot_bytes)
    return h or None
