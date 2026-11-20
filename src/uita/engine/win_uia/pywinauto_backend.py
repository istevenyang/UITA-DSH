"""pywinauto 窗口后端（灰度，可选依赖）。

仅在 `UIA_WIN_BACKEND=pywinauto` 或 app 级 `extra["win_backend"]="pywinauto"`
时被引擎装配；`pywinauto` 未安装 / import 失败时 `create_pywinauto_backend`
返回 None，由引擎回退 `uiautomation` 并记 warning，**不静默**。

实现要点：

- 窗口枚举：与 `uiautomation` 后端共用 Win32 `EnumWindows`（`Desktop.windows()`
  经 UIA 遍历桌面根，遇无响应窗口会长时间阻塞）；
- 控件搜索：以 hwnd 的 `UIAElementInfo` 为根 `descendants()`（按文档序，等价
  `uiautomation` 的 DFS 前序），属性（Name / AutomationId / ClassName /
  ControlType）逐项 AND，`@ChildText` / `@SiblingText` 复用引擎注入的谓词；
- 命中后经 `Control.CreateControlFromElement` 转成 `uiautomation` 控件，
  下游 click / type / 取文本与后端无关；
- 不支持 `MaxDepth`（原生 `FindAll` 为全子树）；
- 本机缺 MFC 运行库（`mfc140u.dll`）时 `win32ui` 无法 import，而 pywinauto
  import 期强依赖它；该模块仅 `capture_as_image` 用到，引擎自有截图，因此
  import 失败时注册空桩后重试。
"""
from __future__ import annotations

import logging
import re
import sys
import types
from typing import Callable, Optional

from uita.core.platform.window_hint import WindowRef, split_candidates
from uita.engine.win_uia.window_contract import (
    BACKEND_PYWINAUTO,
    ControlLocator,
    enumerate_top_level_windows,
)

logger = logging.getLogger(__name__)

_TextPredicate = Callable[[object, str], bool]


def load_pywinauto() -> Optional[types.ModuleType]:
    """惰性 import pywinauto；不可用返回 None（记录原因）。"""
    if sys.platform != "win32":
        return None
    try:
        import win32ui  # noqa: F401
    except ImportError:
        sys.modules.setdefault("win32ui", types.ModuleType("win32ui"))
        logger.info("[WinUIA] win32ui 不可用（缺 MFC 运行库），已注册空桩供 pywinauto import")
    try:
        import pywinauto
        return pywinauto
    except Exception as exc:
        logger.warning("[WinUIA] pywinauto 不可用（%s: %s），将回退 uiautomation",
                       type(exc).__name__, exc)
        return None


class PywinautoBackend:
    """pywinauto UIA 后端（见模块文档）。"""

    name = BACKEND_PYWINAUTO

    def __init__(self, pywinauto_module: types.ModuleType, uia_module: object,
                 child_text_fn: _TextPredicate, sibling_text_fn: _TextPredicate):
        self._pw = pywinauto_module
        self._uia = uia_module
        self._child_text = child_text_fn
        self._sibling_text = sibling_text_fn

    def enumerate_windows(self) -> list[WindowRef]:
        # 与 uiautomation 后端共用 Win32 EnumWindows：窗口枚举不需要 UIA，
        # 且 `Desktop.windows()` 要经 UIA 遍历桌面根的全部子节点，遇到无响应
        # 窗口会长时间阻塞（真机实测卡死）。pywinauto 只负责窗口内控件搜索。
        return enumerate_top_level_windows()

    def find_control(self, hwnd: int, locator: ControlLocator) -> Optional[object]:
        root = self._pw.uia_element_info.UIAElementInfo(hwnd)
        control_type = locator.control_type
        if control_type.endswith("Control"):
            control_type = control_type[: -len("Control")]
        try:
            candidates = root.descendants(control_type=control_type) if control_type else root.descendants()
        except Exception as exc:
            # 未知 ControlType 名等：退化为全量再由下方属性过滤
            logger.debug("[WinUIA] pywinauto descendants(control_type=%r) 失败，退化全量: %s",
                         control_type, exc)
            candidates = root.descendants()

        names = split_candidates(locator.name)
        classes = split_candidates(locator.class_name)
        for info in candidates:
            try:
                if names and (info.name or "") not in names:
                    continue
                if locator.name_regex and not re.search(locator.name_regex, info.name or ""):
                    continue
                if locator.automation_id and (info.automation_id or "") != locator.automation_id:
                    continue
                if classes and (info.class_name or "") not in classes:
                    continue
                control = self._uia.Control.CreateControlFromElement(info.element)
            except Exception as exc:
                logger.debug("[WinUIA] pywinauto 候选属性读取失败，跳过: %s", exc)
                continue
            if locator.child_text and not self._child_text(control, locator.child_text):
                continue
            if locator.sibling_text and not self._sibling_text(control, locator.sibling_text):
                continue
            return control
        return None


def create_pywinauto_backend(uia_module: object, child_text_fn: _TextPredicate,
                             sibling_text_fn: _TextPredicate) -> Optional[PywinautoBackend]:
    """装配 pywinauto 后端；依赖不可用返回 None。"""
    pw = load_pywinauto()
    if pw is None:
        return None
    return PywinautoBackend(pw, uia_module, child_text_fn, sibling_text_fn)
