"""
截图标注工具 — 用 Pillow 在截图上叠加红框 + 操作类型标签。

坐标换算流程（窗口截图模式）：
  1. screencapture -l <wid> 截图含窗口阴影（透明像素）
  2. 通过 numpy 批量扫描 RGBA 找内容区起点，结果缓存避免重复计算
  3. 缩放比 = 内容区像素 / 窗口逻辑尺寸（精确，与 Retina 比例无关）
  4. px = (elem_abs_x - win_abs_x) * scale_x + shadow_x

坐标换算流程（全屏降级模式）：
  - win_rect=None 或 is_window_screenshot=False
  - 元素坐标直接乘全屏 Retina 缩放比（通过屏幕逻辑分辨率推断）

健壮性保证：
  - numpy 不可用时退化为 Pillow getpixel 慢速扫描
  - 阴影检测结果缓存（同一图片只扫描一次）
  - font=None 时跳过文字，不报错
  - 所有路径都有异常 catch，标注失败只 log warning
  - 边界严格裁剪，不会越界
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from uita.core.platform.display_scale import detect_fullscreen_scale as _detect_fullscreen_scale

logger = logging.getLogger("hooks.screenshot.annotator")

# 操作类型 → 颜色映射
_ACTION_COLORS: dict[str, str] = {
    "click":     "#FF3B30",
    "type":      "#007AFF",
    "assert":    "#34C759",
    "verify":    "#34C759",
    "wait":      "#FF9500",
    "hover":     "#AF52DE",
    "scroll":    "#5AC8FA",
    "press_key": "#FF2D55",
    "set":       "#8E8E93",
}
_DEFAULT_COLOR = "#FF3B30"
_FAIL_COLOR    = "#FF6600"

# 阴影检测结果缓存：image_path → (sx, sy, rx, ry)
_shadow_cache: dict[str, tuple] = {}


def annotate(
    image_path: str,
    elem_rect: tuple,                       # (x, y, w, h) 元素屏幕绝对逻辑坐标
    label: str,
    win_rect: Optional[tuple] = None,       # (x, y, w, h) 窗口屏幕绝对逻辑坐标
    failed: bool = False,
    is_window_screenshot: bool = True,      # False 时按全屏截图处理
) -> None:
    """在 image_path 的截图上原地叠加红框 + 标签。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("[annotator] Pillow 未安装，跳过截图标注")
        return

    # 验证文件可读
    if not os.path.isfile(image_path) or os.path.getsize(image_path) == 0:
        logger.warning("[annotator] 截图文件不存在或为空: %s", image_path)
        return

    try:
        img = Image.open(image_path)
        # 只有 RGBA 格式才有阴影透明通道，否则转 RGB
        has_alpha = img.mode in ("RGBA", "LA", "PA")
        img_rgba = img.convert("RGBA") if has_alpha else None
        img_w, img_h = img.size

        ex, ey, ew, eh = elem_rect

        # ── 计算像素坐标 ─────────────────────────────────────
        if win_rect is not None and is_window_screenshot and has_alpha:
            wx, wy, ww, wh = win_rect
            px, py, pw, ph = _calc_window_coords(
                img_rgba, img_w, img_h, image_path,
                ex, ey, ew, eh, wx, wy, ww, wh,
            )
        elif win_rect is not None and is_window_screenshot and not has_alpha:
            # 窗口截图但保存为 RGB（无 alpha）：直接按比例换算，无阴影偏移
            wx, wy, ww, wh = win_rect
            scale_x = img_w / ww if ww > 0 else 2.0
            scale_y = img_h / wh if wh > 0 else 2.0
            px = int((ex - wx) * scale_x)
            py = int((ey - wy) * scale_y)
            pw = int(ew * scale_x)
            ph = int(eh * scale_y)
        else:
            # 全屏截图降级：元素绝对坐标，需检测全屏 Retina 缩放比
            scale = _detect_fullscreen_scale(img_w, img_h)
            px = int(ex * scale)
            py = int(ey * scale)
            pw = int(ew * scale)
            ph = int(eh * scale)

        # 边界裁剪
        px = max(0, min(px, img_w - 2))
        py = max(0, min(py, img_h - 2))
        pw = max(4, min(pw, img_w - px - 1))
        ph = max(4, min(ph, img_h - py - 1))

        # ── 绘制（统一在 RGB 上操作，PNG 保存无损）──────────
        img_rgb = img.convert("RGB")
        draw = ImageDraw.Draw(img_rgb)

        color  = _FAIL_COLOR if failed else _ACTION_COLORS.get(label.lower(), _DEFAULT_COLOR)
        # 线宽按截图分辨率自适应（4K≈6px, HD≈3px, 窗口截图≈3px）
        border = max(2, round(img_w / 700))

        # 白色外描边（1px，增强任意背景下的可见性）
        draw.rectangle(
            [px - 1, py - 1, px + pw + 1, py + ph + 1],
            outline="#FFFFFF", width=1,
        )
        # 彩色主框
        draw.rectangle(
            [px, py, px + pw, py + ph],
            outline=color, width=border,
        )

        # 标签（跟随框的左上角）
        _draw_label(draw, label, color, px, py, img_w)

        img_rgb.save(image_path)
        logger.debug(
            "[annotator] %s → px=(%d,%d,%dx%d) file=%s",
            label, px, py, pw, ph, os.path.basename(image_path),
        )

    except Exception as exc:
        logger.warning("[annotator] 标注失败，保留原始截图: %s", exc)


# ── 核心换算 ─────────────────────────────────────────────────

def _calc_window_coords(
    img_rgba, img_w: int, img_h: int, cache_key: str,
    ex: int, ey: int, ew: int, eh: int,
    wx: int, wy: int, ww: int, wh: int,
) -> tuple:
    """窗口截图模式：检测阴影偏移，精确换算坐标。"""
    # 缓存阴影检测结果，同一图片只扫描一次
    if cache_key not in _shadow_cache:
        sx, sy = _detect_shadow_left_top(img_rgba, img_w, img_h)
        rx, ry = _detect_shadow_right_bottom(img_rgba, img_w, img_h)
        _shadow_cache[cache_key] = (sx, sy, rx, ry)
    sx, sy, rx, ry = _shadow_cache[cache_key]

    content_w = img_w - sx - rx
    content_h = img_h - sy - ry

    # 防止 content 尺寸异常（全透明图、检测错误等）
    if content_w <= 0 or content_h <= 0:
        logger.debug("[annotator] 阴影检测异常，content=%dx%d，退化无阴影", content_w, content_h)
        content_w, content_h = img_w, img_h
        sx = sy = 0

    scale_x = content_w / ww if ww > 0 else 2.0
    scale_y = content_h / wh if wh > 0 else 2.0

    px = int((ex - wx) * scale_x) + sx
    py = int((ey - wy) * scale_y) + sy
    pw = int(ew * scale_x)
    ph = int(eh * scale_y)

    logger.debug(
        "[annotator] shadow=(%d,%d,%d,%d) content=%dx%d scale=(%.4f,%.4f) "
        "rel=(%d,%d) → px=(%d,%d,%dx%d)",
        sx, sy, rx, ry, content_w, content_h, scale_x, scale_y,
        ex - wx, ey - wy, px, py, pw, ph,
    )
    return px, py, pw, ph


# ── 阴影检测 ─────────────────────────────────────────────────

def _detect_shadow_left_top(img, img_w: int, img_h: int) -> tuple:
    """快速检测左/上阴影偏移（numpy 优先，降级 getpixel）。"""
    try:
        import numpy as np
        arr = np.array(img)[:, :, 3]  # alpha 通道

        # 左边界：找第一个列均值 > 64 的列（阴影是渐变，用低阈值）
        shadow_x = 0
        for x in range(min(150, img_w)):
            if arr[:, x].mean() > 64:
                shadow_x = x
                break

        # 上边界
        shadow_y = 0
        for y in range(min(150, img_h)):
            if arr[y, :].mean() > 64:
                shadow_y = y
                break

        return shadow_x, shadow_y

    except ImportError:
        return _detect_shadow_left_top_slow(img, img_w, img_h)


def _detect_shadow_right_bottom(img, img_w: int, img_h: int) -> tuple:
    """快速检测右/下阴影偏移（numpy 优先，降级 getpixel）。"""
    try:
        import numpy as np
        arr = np.array(img)[:, :, 3]

        shadow_right = 0
        for x in range(img_w - 1, max(img_w - 150, 0), -1):
            if arr[:, x].mean() > 64:
                shadow_right = img_w - 1 - x
                break

        shadow_bottom = 0
        for y in range(img_h - 1, max(img_h - 150, 0), -1):
            if arr[y, :].mean() > 64:
                shadow_bottom = img_h - 1 - y
                break

        return shadow_right, shadow_bottom

    except ImportError:
        return _detect_shadow_right_bottom_slow(img, img_w, img_h)


def _detect_shadow_left_top_slow(img, img_w: int, img_h: int) -> tuple:
    """getpixel 慢速降级（无 numpy 时使用）。"""
    step_y = max(1, img_h // 30)
    step_x = max(1, img_w // 30)

    shadow_x = 0
    for x in range(min(150, img_w)):
        samples = [img.getpixel((x, y))[3] for y in range(0, img_h, step_y)]
        if sum(samples) / len(samples) > 64:
            shadow_x = x
            break

    shadow_y = 0
    for y in range(min(150, img_h)):
        samples = [img.getpixel((x, y))[3] for x in range(0, img_w, step_x)]
        if sum(samples) / len(samples) > 64:
            shadow_y = y
            break

    return shadow_x, shadow_y


def _detect_shadow_right_bottom_slow(img, img_w: int, img_h: int) -> tuple:
    step_y = max(1, img_h // 30)
    step_x = max(1, img_w // 30)

    shadow_right = 0
    for x in range(img_w - 1, max(img_w - 150, 0), -1):
        samples = [img.getpixel((x, y))[3] for y in range(0, img_h, step_y)]
        if sum(samples) / len(samples) > 64:
            shadow_right = img_w - 1 - x
            break

    shadow_bottom = 0
    for y in range(img_h - 1, max(img_h - 150, 0), -1):
        samples = [img.getpixel((x, y))[3] for x in range(0, img_w, step_x)]
        if sum(samples) / len(samples) > 64:
            shadow_bottom = img_h - 1 - y
            break

    return shadow_right, shadow_bottom


# ── 标签绘制 ─────────────────────────────────────────────────

def _draw_label(draw, label: str, color: str, px: int, py: int, img_w: int) -> None:
    """在框左上角绘制操作标签。font=None 时跳过文字只绘制色块。"""
    font_size = max(14, round(14 * img_w / 1000))
    pad = max(3, round(3 * img_w / 1000))
    font, label = _text_font(font_size, label)

    if font is not None:
        try:
            bbox = font.getbbox(label)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        except Exception:
            tw, th = font_size * len(label), font_size
    else:
        tw, th = font_size * len(label), font_size

    tag_x = px
    tag_y = max(0, py - th - pad * 2)

    draw.rectangle(
        [tag_x, tag_y, tag_x + tw + pad * 2, tag_y + th + pad * 2],
        fill=color,
    )
    if font is not None:
        try:
            draw.text((tag_x + pad, tag_y + pad), label, fill="#FFFFFF", font=font)
        except Exception as exc:
            logger.debug("[annotator] 文字绘制失败: %s", exc)


def _load_font(size: int):
    """加载字体（走 `core/platform/fonts` 统一候选表），Pillow 缺失或异常时返回 None。"""
    from uita.core.platform.fonts import load_font

    font, _cjk_ok = load_font(size)
    return font


def _text_font(font_size: int, text: str) -> tuple[object, str]:
    """返回 `(font, 可安全绘制文案)`。

    字体无中文字形时把文案降级为 ASCII：Windows 上纯拉丁字体画中文会得到一串
    缺字方框「口口口」，证据图不可读，降级比画方框更可判。
    """
    from uita.core.platform.fonts import load_font, to_ascii

    font, cjk_ok = load_font(font_size)
    raw = str(text)
    return font, (raw if cjk_ok else to_ascii(raw))


def annotate_not_found(image_path: str, element_name: str) -> None:
    """在截图左上角绘制 "[NOT FOUND] <name>" 红色文字标签（无需元素坐标）。

    供 take_action_screenshot_before() 在元素解析失败时调用，标注当前页面状态。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("[annotator] Pillow 未安装，跳过 not_found 标注")
        return

    if not os.path.isfile(image_path) or os.path.getsize(image_path) == 0:
        logger.warning("[annotator] 截图文件不存在或为空: %s", image_path)
        return

    try:
        img = Image.open(image_path).convert("RGB")
        img_w, img_h = img.size
        draw = ImageDraw.Draw(img)

        label = f"[NOT FOUND] {element_name}"
        color = "#FF0000"
        font_size = max(14, round(14 * img_w / 1000))
        pad = max(3, round(3 * img_w / 1000))
        font, label = _text_font(font_size, label)

        if font is not None:
            try:
                bbox = font.getbbox(label)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            except Exception:
                tw, th = font_size * len(label), font_size
        else:
            tw, th = font_size * len(label), font_size

        # 红色背景块（左上角）
        draw.rectangle(
            [0, 0, min(tw + pad * 2, img_w - 1), min(th + pad * 2, img_h - 1)],
            fill=color,
        )
        # 白色文字
        if font is not None:
            try:
                draw.text((pad, pad), label, fill="#FFFFFF", font=font)
            except Exception:
                pass

        img.save(image_path)
        logger.debug("[annotator] not_found 标注: name=%r file=%s",
                     element_name, os.path.basename(image_path))

    except Exception as exc:
        logger.warning("[annotator] annotate_not_found 失败，保留原始截图: %s", exc)


def annotate_action_label(image_path: str, label: str) -> None:
    """在截图左上角绘制动作文字标签（蓝色背景，白色文字）。

    用于 press_key / soft_reset 等无需元素定位的动作，在截图中标注当前执行的动作，
    例如 "press_key[Escape]"，替代原来的 "[NOT FOUND]" 红色标签。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("[annotator] Pillow 未安装，跳过 action_label 标注")
        return

    if not os.path.isfile(image_path) or os.path.getsize(image_path) == 0:
        logger.warning("[annotator] 截图文件不存在或为空: %s", image_path)
        return

    try:
        img = Image.open(image_path).convert("RGB")
        img_w, _ = img.size
        draw = ImageDraw.Draw(img)

        color_bg = "#1565C0"   # 深蓝背景
        color_fg = "#FFFFFF"   # 白色文字
        font_size = max(14, round(14 * img_w / 1000))
        pad = max(3, round(3 * img_w / 1000))
        font, label = _text_font(font_size, label)

        if font is not None:
            try:
                bbox = font.getbbox(label)
                tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            except Exception:
                tw, th = font_size * len(label), font_size
        else:
            tw, th = font_size * len(label), font_size

        draw.rectangle(
            [0, 0, min(tw + pad * 2, img_w - 1), th + pad * 2],
            fill=color_bg,
        )
        if font is not None:
            try:
                draw.text((pad, pad), label, fill=color_fg, font=font)
            except Exception:
                pass

        img.save(image_path)
        logger.debug("[annotator] action_label 标注: label=%r file=%s",
                     label, os.path.basename(image_path))

    except Exception as exc:
        logger.warning("[annotator] annotate_action_label 失败，保留原始截图: %s", exc)


def clear_shadow_cache() -> None:
    """清除阴影检测缓存（测试用）。"""
    _shadow_cache.clear()
