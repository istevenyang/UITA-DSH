"""
display_scale — 屏幕/截图缩放比探测（Retina/DPI 全屏降级场景）。

从 `core/hooks/builtin/annotator.py` 搬迁（2026-07-31，见计划文档
`docs/plans/archive/03-resolver-rosetta-实施纪要.md` §3.1），
供 `annotator.py`（画框标注）与 `core/resolver/screen_coords.py`（L3.8/L4 视觉
命中后的点击坐标换算）共用，避免维护两份"怎么探测全屏 Retina 缩放比"的代码。

零外部依赖（符合 R3：`core/platform/` 不 import Engine/Registry/Resolver 等
运行时模块，只用标准库 + subprocess/ctypes）。
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def detect_fullscreen_scale(img_w: int, img_h: int) -> float:
    """全屏截图：通过屏幕逻辑分辨率推算缩放比。

    macOS: 通过 system_profiler 获取屏幕分辨率
    Windows: 通过 ctypes GetSystemMetrics 获取逻辑分辨率
    Linux/fallback: 启发式（>2000px 认为 2x）

    任何探测异常均静默捕获，最终落到启发式兜底，不会抛出异常。
    """
    # macOS
    if sys.platform == "darwin":
        try:
            import json
            import subprocess
            result = subprocess.run(
                ["system_profiler", "SPDisplaysDataType", "-json"],
                capture_output=True, timeout=3,
            )
            data = json.loads(result.stdout)
            for disp in data.get("SPDisplaysDataType", []):
                res = disp.get("spdisplays_resolution", "")
                parts = res.replace("Retina", "").split("x")
                if len(parts) == 2:
                    try:
                        phys_w = int(parts[0].strip().split()[0])
                        if phys_w > 0:
                            ratio = img_w / phys_w
                            if 0.8 < ratio < 4.0:
                                logger.debug("[display_scale] macOS 全屏 scale=%.3f", ratio)
                                return ratio
                    except Exception:
                        pass
        except Exception:
            pass

    # Windows
    if sys.platform == "win32":
        try:
            import ctypes
            # 获取逻辑分辨率（用户设置的分辨率，不含 DPI 缩放）
            user32 = ctypes.windll.user32
            # SetProcessDPIAware 后 GetSystemMetrics 返回物理像素
            # 未设置时返回逻辑像素（DPI 缩放后的虚拟值）
            logical_w = user32.GetSystemMetrics(0)  # SM_CXSCREEN
            if logical_w > 0:
                ratio = img_w / logical_w
                if 0.8 < ratio < 4.0:
                    logger.debug("[display_scale] Windows 全屏 scale=%.3f (logical=%d, img=%d)",
                                 ratio, logical_w, img_w)
                    return ratio
        except Exception:
            pass

    # 启发式兜底：>3000px 认为 3x，>2000px 认为 2x，否则 1x
    if img_w > 3000:
        return 3.0
    if img_w > 1500:
        return 2.0
    return 1.0
