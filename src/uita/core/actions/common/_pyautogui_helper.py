"""
pyautogui 坐标点击/输入辅助（P4 新增）。

计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md §3.4

架构定位：pyautogui 不是通过 EngineRegistry 注册的第二个 EngineBase 实现，而是
Resolver/action 层内部的坐标点击工具函数（视觉指纹/LLM 视觉命中后直接屏幕坐标
点击，绕开 EngineBase.click()）。这样不违反 R9/R11——一个 case 仍只绑定一个
EngineBase 实例，生命周期不受影响。

坐标假设（⚠️ 未经真机验证，见最终问题汇总的已知阻塞项）：`ElementRef.coords`
当前假定为"屏幕绝对逻辑坐标"（与 `EngineBase.get_window_rect()` 文档注明的
坐标系一致）。真机环境下 Retina 缩放比、多显示器场景下的坐标换算是否需要
额外处理，本次会话无法在真机验证，需要用户在真机环境自行确认。

⚠️ 安全护栏（settings.ENABLE_PYAUTOGUI）——本模块开发过程中曾在本地测试环境
真实触发过一次系统级按键注入（详见最终问题汇总"安全事件"记录）：pyautogui
控制的是整台主机的鼠标/键盘，一旦误触发（mock 配置疏漏、代码路径意外分支等）
会对宿主机产生不可预期的副作用（乱点/乱敲当前聚焦的任意窗口）。因此本模块
在真正执行任何 pyautogui 调用前，强制要求 ``settings.ENABLE_PYAUTOGUI`` 为
``True``（2026-07-17 起改为 settings.py 明文配置项，不再支持环境变量动态
开启，见 settings.py 注释：真机启用需改代码走 review，避免误配置导致安全
事故），未开启时直接抛出 RuntimeError 拒绝执行——即使 pyautogui 包本身已
安装、调用参数完全正确，缺省状态下也绝不会产生真实副作用。这是纯防御性
设计，不影响该能力在真机环境下的正常使用（用户需要时手动改 settings.py）。

惰性 import pyautogui：模块导入本身不需要它已安装，只有在真正调用
click_at_coords()/type_at_coords() 时才会 import，未安装或无 GUI/无障碍权限
（macOS 需要"辅助功能"授权，与现有 MacAppKitEngine 的 TCC 权限模型一致）时
抛出清晰的 RuntimeError。单测通过 monkeypatch `import_pyautogui` 注入 mock 并
设置 settings.ENABLE_PYAUTOGUI=True，不依赖真实安装/权限，也不会产生真实
副作用。
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import sys


logger = logging.getLogger(__name__)


def _enabled() -> bool:
    import uita.config as settings
    return bool(getattr(settings, "ENABLE_PYAUTOGUI", False))


def _assert_enabled() -> None:
    if not _enabled():
        raise RuntimeError(
            "pyautogui 坐标点击/输入能力默认关闭（安全护栏）。如确需在真机环境"
            "启用视觉指纹/LLM 视觉坐标点击或 upload_file 的 pyautogui 备路，"
            "请显式将 settings.py 中的 ENABLE_PYAUTOGUI 改为 True 后重试。"
        )


def import_pyautogui():
    """惰性导入 pyautogui，供本模块及各 app 专属 actions.py 的 pyautogui 备路复用。

    调用前必须先通过 _assert_enabled() 校验（各公开函数已内置该校验，直接调用
    import_pyautogui() 本身不做校验，仅负责导入）。
    """
    try:
        import pyautogui  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "pyautogui 未安装，无法执行视觉指纹/LLM 视觉命中后的坐标点击/输入。"
            "请先 pip install pyautogui（见 requirements.txt）；macOS 还需要在"
            "\"系统设置 → 隐私与安全性 → 辅助功能\"中授权当前终端/进程。"
        ) from exc
    return pyautogui


async def move_to_coords(x: int, y: int, hold_ms: int = 0) -> None:
    """将鼠标移动到屏幕绝对坐标 (x, y)（不点击），可选停留时长。

    2026-08-21 新增（边缘唤醒/悬浮球场景：hover_at action、hover[] 的
    hold_ms= kwarg 均复用本函数）。真实 OS 级 `pyautogui.moveTo`，与
    click_at_coords 走同一安全护栏（settings.ENABLE_PYAUTOGUI）。

    Args:
        x, y: 屏幕绝对逻辑坐标。
        hold_ms: 移动到位后停留的毫秒数（默认 0，不停留）。部分系统级
            "鼠标靠近唤醒"交互（如边缘唤醒/悬浮球 hover 反馈）存在防误触
            延迟，需要真实停留一段时间才会触发，此参数用于满足该时序需求。
    """
    _assert_enabled()
    pyautogui = import_pyautogui()
    await asyncio.to_thread(pyautogui.moveTo, x, y)
    if hold_ms > 0:
        await asyncio.sleep(hold_ms / 1000)


async def click_at_coords(x: int, y: int, button: str = "left") -> None:
    """在屏幕绝对坐标 (x, y) 处点击（供 click action 处理坐标句柄使用）。

    需先设置 UITA_ENABLE_PYAUTOGUI=1（安全护栏，见模块 docstring）。
    """
    _assert_enabled()
    normalized = str(button).strip().lower()
    if normalized not in {"left", "right", "middle"}:
        raise ValueError(f"unsupported mouse button: {button!r}")
    pyautogui = import_pyautogui()
    if normalized == "left":
        await asyncio.to_thread(pyautogui.click, x, y)
    else:
        await asyncio.to_thread(pyautogui.click, x, y, button=normalized)


async def type_at_coords(x: int, y: int, text: str, clear: bool = True) -> None:
    """先点击坐标聚焦，再输入文字（供 type action 处理坐标句柄使用）。

    需先设置 UITA_ENABLE_PYAUTOGUI=1（安全护栏，见模块 docstring）。
    clear=True 时先 Cmd+A/Ctrl+A 全选再输入（跨平台用 pyautogui.hotkey 分别
    处理，避免硬编码单一平台快捷键）。
    """
    _assert_enabled()
    pyautogui = import_pyautogui()
    await asyncio.to_thread(pyautogui.click, x, y)
    if clear:
        modifier = "command" if sys.platform == "darwin" else "ctrl"
        await asyncio.to_thread(pyautogui.hotkey, modifier, "a")
    await asyncio.to_thread(pyautogui.typewrite, text)


async def drag_between_coords(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    mode: str = "jump",
    duration_ms: int = 0,
    steps: int = 1,
) -> None:
    """从源坐标拖拽到目标坐标。

    mode="jump" 时直接跳到目标点，避免平滑拖动过程中误触其他控件。
    """
    _assert_enabled()
    pyautogui = import_pyautogui()

    def _drag() -> None:
        pyautogui.moveTo(x1, y1)
        pyautogui.mouseDown()
        count = max(int(steps), 1)
        total_duration = 0 if mode == "jump" else max(duration_ms, 0) / 1000
        segment_duration = total_duration / count
        for index in range(1, count + 1):
            ratio = index / count
            x = round(x1 + (x2 - x1) * ratio)
            y = round(y1 + (y2 - y1) * ratio)
            pyautogui.moveTo(x, y, duration=segment_duration)
        pyautogui.mouseUp()

    await asyncio.to_thread(_drag)


async def press_key(key: str) -> None:
    """通过 pyautogui 按键。"""
    _assert_enabled()
    pyautogui = import_pyautogui()
    await asyncio.to_thread(pyautogui.press, key)


async def paste_text(text: str) -> None:
    """将文本放入剪贴板并 Ctrl/Cmd+V。"""
    _assert_enabled()
    pyautogui = import_pyautogui()
    await asyncio.to_thread(_set_clipboard_text, text)
    modifier = "command" if sys.platform == "darwin" else "ctrl"
    await asyncio.to_thread(pyautogui.hotkey, modifier, "v")


def _set_clipboard_text(text: str) -> None:
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(text)
        return
    except Exception:
        pass
    if sys.platform == "win32":
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $args[0]", text],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return
    raise RuntimeError("无法设置剪贴板：请安装 pyperclip 或提供平台剪贴板支持")

