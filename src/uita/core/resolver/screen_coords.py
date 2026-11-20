"""
ScreenCoordConverter — 把某次 take_screenshot() 产出的图片像素坐标，换算为
pyautogui / page.mouse.click 使用的屏幕绝对逻辑坐标（points）。

计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md §3.2
参考设计：docs/plans/archive/10-platform-scale-实施纪要.md §2.1

坐标系背景：
  - get_window_rect() 返回屏幕绝对逻辑坐标（points），与 AXPosition/HWND 一致。
  - take_screenshot() 在 macOS Retina 屏上返回物理像素图（通常是逻辑尺寸的
    2x/3x），坐标原点随截图方式不同而不同：
      * 原生引擎窗口截图（Quartz L1 无阴影 / screencapture -l 含阴影 /
        WinUIA PrintWindow）：原点=窗口内容左上角
      * 原生引擎全屏降级：原点=屏幕左上角
      * Playwright：get_window_rect() 恒返回 None（未 override，见
        EngineBase 默认实现），截图是浏览器 viewport 局部图，只需除以
        window.devicePixelRatio（无窗口偏移概念），通过 dpr_hint 显式传入，
        优先于全屏启发式探测。
  - L3.8（matchTemplate/特征匹配）/ L4（LLM 视觉定位）返回的坐标都是"截图
    图片像素坐标"，必须换算成屏幕绝对逻辑坐标才能喂给 pyautogui.click()/
    page.mouse.click()。

零外部依赖（符合 R3/R14：core/resolver/ 内部横向工具，不 import
Engine/Registry/Action/Resolver 等运行时模块）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ScreenCoordConverter:
    is_window_screenshot: bool
    window_rect: Optional[tuple]      # (x, y, w, h) 逻辑坐标，来自 get_window_rect()
    image_width: int                  # 截图图片像素宽（window_rect 分支才会用到）
    image_height: int                 # 截图图片像素高（window_rect 分支才会用到）
    has_alpha: bool = False           # 图片是否含 alpha 通道（判断是否可能有阴影）
    dpr_hint: Optional[float] = None  # 已知的设备像素比（如 Playwright
                                       # window.devicePixelRatio），仅在
                                       # window_rect 分支不适用时使用，优先于
                                       # 全屏启发式探测（display_scale.py）

    def scale_factor(self) -> Optional[tuple[float, float]]:
        """返回本次换算实际使用的 (scale_x, scale_y)；无法安全确定时返回 None。"""
        if self.window_rect is not None and self.is_window_screenshot:
            if self.has_alpha:
                # 含阴影降级路径（screencapture -l 等），阴影像素偏移未知，
                # 宁可 NOT_FOUND 不误点（见计划文档 §3.2 第 3 条）。
                logger.debug(
                    "[ScreenCoordConverter] 窗口截图含 alpha（阴影降级路径），"
                    "无法安全换算坐标"
                )
                return None
            wx, wy, ww, wh = self.window_rect
            if ww <= 0 or wh <= 0:
                return None
            scale_x = self.image_width / ww
            scale_y = self.image_height / wh
            if scale_x <= 0 or scale_y <= 0:
                return None
            return (scale_x, scale_y)

        # 无窗口偏移场景：Playwright viewport 截图（dpr_hint 优先）/
        # 原生引擎全屏降级（window_rect is None 或 not is_window_screenshot）
        if self.dpr_hint and self.dpr_hint > 0:
            return (self.dpr_hint, self.dpr_hint)

        from uita.core.platform.display_scale import detect_fullscreen_scale
        s = detect_fullscreen_scale(self.image_width, self.image_height)
        return (s, s) if s > 0 else None

    def to_screen(self, px: float, py: float) -> Optional[tuple[float, float]]:
        """返回 (screen_x, screen_y)；无法安全换算时返回 None（宁可 NOT_FOUND
        不误点击）。"""
        scale = self.scale_factor()
        if scale is None:
            return None
        scale_x, scale_y = scale
        if self.window_rect is not None and self.is_window_screenshot:
            wx, wy, _, _ = self.window_rect
            return (wx + px / scale_x, wy + py / scale_y)
        return (px / scale_x, py / scale_y)

    def to_screen_bbox(
        self, x: float, y: float, w: float, h: float,
    ) -> Optional[tuple[float, float, float, float]]:
        """把截图像素坐标系下的 bbox 换算为屏幕绝对逻辑坐标 bbox（供 region=
        九宫格区域点击复用同一坐标系，见 core/resolver/click_region.py）。"""
        top_left = self.to_screen(x, y)
        if top_left is None:
            return None
        scale_x, scale_y = self.scale_factor()
        return (top_left[0], top_left[1], w / scale_x, h / scale_y)
