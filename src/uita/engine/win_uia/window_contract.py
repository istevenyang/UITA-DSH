"""WinUIA 窗口契约：窗口枚举 + 控件搜索的后端抽象。

`WinUIAEngine` 的「在哪个窗口找」与「怎么在窗口内找」经由 `WindowBackend`：

| 后端 | 实现 | 角色 |
|---|---|---|
| `uiautomation` | `UiaLibBackend`（本模块） | 默认，原样包装现有 `uiautomation` 查找实现 |
| `pywinauto` | `pywinauto_backend.PywinautoBackend` | 灰度，可选依赖；不可用时回退 `uiautomation` |

两个后端共用 `core.platform.window_hint.match_hint`（hint 语义只写一份）、
`ControlLocator`（QPath 解析由引擎完成，后端只负责搜索）并**始终返回
`uiautomation` 控件对象**，因此 click / type / 取文本等下游逻辑与后端无关。

后端选择：app 级 `AppPlatformConfig.extra["win_backend"]` 优先，其次环境变量
`UIA_WIN_BACKEND`，默认 `uiautomation`（见 `resolve_backend_name`）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable

from uita.core.platform.window_hint import WindowRef

logger = logging.getLogger(__name__)

BACKEND_UIAUTOMATION = "uiautomation"
BACKEND_PYWINAUTO = "pywinauto"
_VALID_BACKENDS = (BACKEND_UIAUTOMATION, BACKEND_PYWINAUTO)
BACKEND_ENV = "UIA_WIN_BACKEND"
BACKEND_EXTRA_KEY = "win_backend"


@dataclass(frozen=True)
class ControlLocator:
    """已解析的 QPath 属性集合（所有指定属性 AND；`name` / `class_name` 支持 `|` 候选）。"""

    name: str = ""
    name_regex: str = ""
    automation_id: str = ""
    control_type: str = ""
    class_name: str = ""
    child_text: str = ""
    sibling_text: str = ""
    max_depth: int = 20


@runtime_checkable
class WindowBackend(Protocol):
    """窗口枚举 + 窗口内控件搜索。返回的控件一律是 `uiautomation` 控件。"""

    name: str

    def enumerate_windows(self) -> list[WindowRef]:
        """可见顶层窗口快照（含 pid / 进程名 / 面积）。"""
        ...

    def find_control(self, hwnd: int, locator: ControlLocator) -> Optional[object]:
        """以 hwnd 为根查找首个匹配控件，未命中返回 None。"""
        ...


def normalize_backend_name(raw: object) -> str:
    """规范化后端名；空值 / 未知值 → 默认 `uiautomation`（未知值记 warning）。"""
    value = str(raw or "").strip().lower()
    if not value:
        return BACKEND_UIAUTOMATION
    if value in _VALID_BACKENDS:
        return value
    logger.warning("[WinUIA] 未知窗口后端 %r，已回退 %s（可选: %s）",
                   raw, BACKEND_UIAUTOMATION, "/".join(_VALID_BACKENDS))
    return BACKEND_UIAUTOMATION


def resolve_backend_name(app_name: str) -> str:
    """app 优先、全局兜底：`extra["win_backend"]` → 环境变量 → `uiautomation`。"""
    env_value = os.environ.get(BACKEND_ENV, "")
    try:
        from uita.core.platform.app_registry import get_extra_config
        value = get_extra_config(app_name, "windows", BACKEND_EXTRA_KEY, default=env_value)
    except Exception as exc:  # 注册表不可用不应影响引擎
        logger.debug("[WinUIA] 读取 app 级 win_backend 失败（回退环境变量）: %s", exc)
        value = env_value
    return normalize_backend_name(value)


def enumerate_top_level_windows() -> list[WindowRef]:
    """Win32 `EnumWindows` 枚举可见、有面积的顶层窗口（`uiautomation` 后端使用）。"""
    try:
        import ctypes
        from ctypes import wintypes

        try:
            import psutil
        except ImportError:  # pragma: no cover - psutil 为主依赖
            psutil = None

        user32 = ctypes.windll.user32
        proc_names: dict[int, str] = {}
        found: list[WindowRef] = []

        def _process_name(pid: int) -> str:
            if pid not in proc_names:
                name = ""
                if psutil is not None:
                    try:
                        name = psutil.Process(pid).name()
                    except Exception:
                        name = ""
                proc_names[pid] = name
            return proc_names[pid]

        def _enum(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            if area <= 0:
                return True
            pid = ctypes.c_ulong(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            title_buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title_buf, 256)
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            found.append(WindowRef(
                hwnd=int(hwnd), pid=int(pid.value),
                title=(title_buf.value or "").strip(),
                class_name=(class_buf.value or "").strip(),
                process_name=_process_name(int(pid.value)),
                area=area,
            ))
            return True

        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        user32.EnumWindows(enum_proc(_enum), 0)
        return found
    except Exception as exc:
        logger.debug("[WinUIA] enumerate_top_level_windows 失败: %s", exc)
        return []


class UiaLibBackend:
    """`uiautomation` 后端：原样包装引擎现有的 DFS 查找，不改变任何匹配语义。

    Args:
        uia_module: `uiautomation` 模块（取 `ControlFromHandle`）。
        legacy_find: 引擎既有的 `_find_uia_control(root, name, automation_id,
            control_type, class_name=, child_text=, sibling_text=, name_regex=,
            max_depth=)`。注入而非 import，避免引擎 ↔ 契约循环依赖。
    """

    name = BACKEND_UIAUTOMATION

    def __init__(self, uia_module: object, legacy_find: Callable[..., Optional[object]]):
        self._uia = uia_module
        self._legacy_find = legacy_find

    def enumerate_windows(self) -> list[WindowRef]:
        return enumerate_top_level_windows()

    def find_control(self, hwnd: int, locator: ControlLocator) -> Optional[object]:
        try:
            root = self._uia.ControlFromHandle(hwnd)
        except Exception as exc:
            logger.debug("[WinUIA] ControlFromHandle(%s) 失败: %s", hwnd, exc)
            return None
        if root is None:
            return None
        return self._legacy_find(
            root, locator.name, locator.automation_id, locator.control_type,
            class_name=locator.class_name, child_text=locator.child_text,
            sibling_text=locator.sibling_text, name_regex=locator.name_regex,
            max_depth=locator.max_depth,
        )
