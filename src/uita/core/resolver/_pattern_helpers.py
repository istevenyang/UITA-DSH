"""Resolver 视觉指纹编码辅助（纯函数，core/ 内自持实现，不依赖 tools/）。

R2 修复：此前 `core/resolver/resolver.py` 直接 import
`tools.agent_rosetta.gen.fingerprint_writer` 违反宪法 R2（`core/` 禁止 import
`tools/`）。本模块把 `fingerprint_writer.py` 中两个纯函数（`_crop_to_bytes`/
`_encode_pattern`）下沉到 `core/` 内部独立实现，参考 `core/actions/common/
_page_hash_helper.py` 的既有模式（不 import tools，core 内独立实现）。

见 docs/plans/archive/08-ci-delivery-实施纪要.md §5.3。
"""
from __future__ import annotations

import io
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def crop_to_bytes(
    image_path: str, rect: tuple, *, dpr: float = 1.0
) -> Optional[bytes]:
    """从截图裁切元素区域，返回 PNG 字节。

    dpr: devicePixelRatio（Retina 2x = 2.0），DOM 元素坐标为 CSS 像素，
         截图是物理像素，裁切时需 x dpr 缩放到对的物理位置。
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(image_path)
        w, h = img.size
        x1, y1, x2, y2 = rect
        if dpr != 1.0:
            x1, y1, x2, y2 = int(x1 * dpr), int(y1 * dpr), int(x2 * dpr), int(y2 * dpr)
        x1 = max(0, min(x1, w - 1))
        y1 = max(0, min(y1, h - 1))
        x2 = max(x1 + 1, min(x2, w))
        y2 = max(y1 + 1, min(y2, h))
        crop = img.crop((x1, y1, x2, y2))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as exc:
        logger.debug("[_pattern_helpers] 裁切失败: %s", exc)
        return None


def encode_pattern(png_bytes: bytes, bbox: Optional[tuple] = None) -> str:
    """将 PNG 字节编码为 rosetta_map.pattern 格式（z85 字符串）。"""
    from uita.core.rosetta.pattern_codec import encode
    return encode(png_bytes, bbox=bbox)
