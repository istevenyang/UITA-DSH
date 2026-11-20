"""跨平台字体解析 — 截图标注取字的唯一入口。

标注文案含中文（`预期：…` / `判定：…` / 元素名）。Windows 上若回落到纯拉丁字体
（arial / segoeui / tahoma / calibri），Pillow 找不到对应字形，中文会被画成一串
缺字方框「口口口」，证据图直接不可读。

统一口径：

1. 取字一律经本模块（`load_font`），禁止各标注模块自备候选表；
2. 优先 CJK 字体（`resolve_cjk_font_path`），按平台候选表顺序取第一个可加载者；
3. 取不到 CJK 字体时**不静默继续**：返回 `cjk_ok=False`，调用方须把文案降级为
   ASCII（`to_ascii`）后再绘制，宁可信息变粗也不出现「口口口」；
4. `resolve_cjk_font_path` 的 `exists` / 平台参数可注入，便于单测在无字体环境下
   覆盖全部分支（宪法 R6）。
"""
from __future__ import annotations

import os
import sys
from typing import Any, Callable, Optional

__all__ = [
    "CJK_FONT_CANDIDATES",
    "candidates_for",
    "to_ascii",
    "resolve_cjk_font_path",
    "has_cjk_font",
    "load_font",
    "norm_platform",
]

_WINDOWS_FONTS: tuple[str, ...] = (
    "msyh.ttc",       # 微软雅黑
    "msyhbd.ttc",     # 微软雅黑 Bold
    "msyhl.ttc",      # 微软雅黑 Light
    "simhei.ttf",     # 黑体
    "simsun.ttc",     # 宋体
    "simkai.ttf",     # 楷体
    "Deng.ttf",       # 等线
)

_POSIX_CANDIDATES: dict[str, tuple[str, ...]] = {
    "darwin": (
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ),
    "linux": (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
    ),
}

# 兼容旧引用：静态快照（Windows 段取默认安装目录，运行时请用 candidates_for）
CJK_FONT_CANDIDATES: dict[str, tuple[str, ...]] = {
    **_POSIX_CANDIDATES,
    "win32": tuple(os.path.join(r"C:\Windows", "Fonts", name) for name in _WINDOWS_FONTS),
}


def norm_platform(platform: Optional[str] = None) -> str:
    """把 `win32` / `Windows` / `macOS` / `MacOS` 等写法归一成候选表键。"""
    value = (platform or sys.platform or "").strip().lower()
    if value in ("win32", "windows", "win", "cygwin", "msys"):
        return "win32"
    if value in ("darwin", "macos", "mac"):
        return "darwin"
    if value.startswith("linux"):
        return "linux"
    return value


def _windows_fonts_dir() -> str:
    return os.path.join(os.environ.get("WINDIR") or r"C:\Windows", "Fonts")


def candidates_for(platform: Optional[str] = None) -> tuple[str, ...]:
    """按平台返回中文字体候选绝对路径（Windows 跟随 `WINDIR`）。"""
    key = norm_platform(platform)
    if key == "win32":
        windir = _windows_fonts_dir()
        return tuple(os.path.join(windir, name) for name in _WINDOWS_FONTS)
    return _POSIX_CANDIDATES.get(key, ())


def resolve_cjk_font_path(
    platform: Optional[str] = None,
    *,
    exists: Optional[Callable[[str], bool]] = None,
) -> Optional[str]:
    """返回第一个存在的 CJK 字体路径；一个都没有则 `None`。"""
    check = exists or os.path.isfile
    for path in candidates_for(platform):
        try:
            if check(path):
                return path
        except Exception:  # noqa: BLE001 - 探测失败视为该候选不可用
            continue
    return None


def has_cjk_font(platform: Optional[str] = None, *, exists: Optional[Callable[[str], bool]] = None) -> bool:
    """是否能在该平台上定位到中文字体文件（不看能否真正加载）。"""
    return resolve_cjk_font_path(platform, exists=exists) is not None


def _open_font(path: str, size: int) -> Any:
    from PIL import ImageFont

    if path.lower().endswith(".ttc"):
        try:
            return ImageFont.truetype(path, size, index=0)
        except TypeError:  # Pillow<10 无 index 参数
            return ImageFont.truetype(path, size)
    return ImageFont.truetype(path, size)


def load_font(
    size: int,
    platform: Optional[str] = None,
    *,
    exists: Optional[Callable[[str], bool]] = None,
) -> tuple[Any, bool]:
    """返回 `(font, cjk_ok)`；`cjk_ok=False` 表示字体无中文字形。

    Pillow 缺失时返回 `(None, False)`。找不到 CJK 字体时退回内置位图字体，
    保证拉丁文本仍可绘制，由调用方负责把中文降级为 ASCII。
    """
    try:
        from PIL import ImageFont
    except Exception:  # noqa: BLE001 - Pillow 未安装不属致命错误
        return None, False

    path = resolve_cjk_font_path(platform, exists=exists)
    if path:
        try:
            return _open_font(path, size), True
        except Exception:  # noqa: BLE001 - 文件存在但不可加载，继续走降级
            pass

    try:
        try:
            return ImageFont.load_default(size=size), False  # Pillow>=10.1
        except TypeError:
            return ImageFont.load_default(), False
    except Exception:  # noqa: BLE001
        return None, False


def to_ascii(text: str) -> str:
    """把非 ASCII 字符逐个替换为 `?`，长度对齐，避免缺字形方框。

    用于 `cjk_ok=False` 时的文案降级：`预期：面板可见` → `??????`（全角冒号也非 ASCII）。
    """
    return "".join(ch if ord(ch) < 128 else "?" for ch in str(text or ""))
