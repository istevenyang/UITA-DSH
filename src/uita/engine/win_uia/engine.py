"""
WinUIAEngine — Windows UI Automation engine (uiautomation)。

生产实现的 16 个 EngineBase 抽象方法参考 SampleAgentbot engine_pc_uia.py。
截图方法保留 win32gui 窗口截图 + 全屏兜底两级降级（2026-07-13 移除未接线的
pywinauto 分支，见 docs/plans/archive/01-engine-lifecycle-实施纪要.md）。

核心架构：STA 单线程执行器
  所有 UIA/COM 操作固定在一个 STA 线程执行（ThreadPoolExecutor(max_workers=1)），
  避免 IUIAutomation 进程单例跨线程 marshaling 失败。

坐标系：Windows UIA 坐标单位为物理像素，与屏幕 DPI 相关，
无 macOS Retina 阴影问题，annotator 的 RGB 无阴影分支可直接使用。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from typing import ClassVar, Optional

from uita.core.engine.base import ElementRef, EngineBase
from uita.core.engine.registry import register_engine
from uita.config import NOTICE
from uita.core.platform.window_hint import WindowHint, match_hint
from uita.engine.win_uia.pywinauto_backend import create_pywinauto_backend
from uita.engine.win_uia.window_contract import (
    BACKEND_PYWINAUTO,
    BACKEND_UIAUTOMATION,
    ControlLocator,
    UiaLibBackend,
    WindowBackend,
    resolve_backend_name,
)

logger = logging.getLogger(__name__)

# window_hint 作用域哨兵：严格模式下 hint 未命中任何窗口（元素视为未找到）
_SCOPE_MISS = object()


def _split_candidates(value: str) -> list[str]:
    """按 `|` 拆分多语言候选值，去除两端空白，过滤空字符串。

    单一候选（无 `|`）时返回单元素列表，行为与改动前完全一致。用于应对同一
    控件在不同系统语言态下 Name/ChildText/SiblingText 等属性值不同的场景
    （如 QPath `@Name='开始扫描|Start Scan'`），见宪法 R22c /
    docs/plans/20260731120000-axpath-multilang-candidate-values-and-
    constitution-update.md。与 `engine/mac_appkit/ax_query.py::_split_candidates`
    逻辑一致，两个引擎目录彼此不允许 import（R1 分层隔离），各自独立实现一份。
    """
    if "|" not in value:
        return [value]
    parts = [p.strip() for p in value.split("|")]
    return [p for p in parts if p]


# ── Windows-only 依赖（惰性 import + try/except）─────────────────────

if sys.platform == "win32":
    try:
        import uiautomation as _uia
        HAS_UIA = True
    except ImportError:
        _uia = None  # type: ignore
        HAS_UIA = False
    try:
        import pyautogui
        HAS_PYAUTOGUI = True
    except ImportError:
        HAS_PYAUTOGUI = False
    try:
        import comtypes
        HAS_COMTYPES = True
    except ImportError:
        HAS_COMTYPES = False
else:
    _uia = None  # type: ignore
    HAS_UIA = False
    HAS_PYAUTOGUI = False
    HAS_COMTYPES = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


# ── Win32 VK 码映射表（press_key 使用）─────────────────────────────

_VK_MAP: dict[str, int] = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08, "back": 0x08, "delete": 0x2E, "del": 0x2E,
    "space": 0x20, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "printscreen": 0x2C, "pause": 0x13,
    "control": 0x11, "ctrl": 0x11, "alt": 0x12, "shift": 0x10,
    "lwin": 0x5B, "rwin": 0x5C, "apps": 0x5D,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
    **{chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)},
    **{str(i): ord(str(i)) for i in range(10)},
}

# 组合键中的"修饰键"子集（需在按下期间保持 keydown，而非按下即弹起）
_MODIFIER_VKS: dict[str, int] = {
    "control": 0x11, "ctrl": 0x11, "alt": 0x12, "shift": 0x10,
    "lwin": 0x5B, "rwin": 0x5C, "win": 0x5B,
}

# ── 系统托盘/桌面级跨窗口点击常量（sample_web quit_via_tray 使用，见
# docs/plans/archive/01-engine-lifecycle-实施纪要.md）────────

_TRAY_WND_CLASS = "Shell_TrayWnd"
# 溢出窗口类名/展开箭头 Name 均为候选列表（按顺序遍历，命中任一即用），
# 兼容不同 Windows 版本/语言环境（见 docs/plans/20260721180000-system-tray-
# icon-hidden-overflow-fallback.md）：
#   - "NotifyIconOverflowWindow"/"显示隐藏的图标"：Win10 及传统 Win11（简体中文）
#   - "TopLevelWindowForOverflowXamlIsland"/"Show Hidden Icons"：部分较新
#     Win11 版本采用 XAML Islands 重构后的溢出窗口/英文环境（该类名来自
#     社区调研，非微软官方文档保证的稳定接口，可能随系统版本变化，仅作为
#     候选之一，找不到不影响现有候选继续尝试）
_TRAY_OVERFLOW_CLASS_CANDIDATES = (
    "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland",
)
_TRAY_OVERFLOW_BUTTON_NAME_CANDIDATES = (
    "显示隐藏的图标",
    "Show Hidden Icons",
)
# 真机验证发现（2026-07-21）：Shell_TrayWnd 子树内除通知区托盘图标
# （ClassName 前缀 'SystemTray.'）外，还包含任务栏固定/运行中应用图标
# （ClassName='Taskbar.TaskListButtonAutomationPeer'，如"示例桌面应用 - 1个
# 运行窗口"），二者 Name 都可能包含同一关键字（如"示例桌面应用"），若不
# 限定 ClassName 前缀，DFS 会误命中任务栏应用图标而非真正的系统托盘图标
# （真机复现：误点开了"示例桌面应用-全局搜"的固定/取消固定菜单）。
_SYSTEM_TRAY_CLASS_PREFIX = "SystemTray."
# 菜单/对话框顶层窗口的标准 Win32 类名（#32768=菜单, #32770=对话框）。
# 真机验证发现（2026-07-21）：DemoTray.exe 的托盘右键菜单/确认弹窗均为 Qt
# 自绘窗口，真实类名为 'Qt51514QWindowPopupSaveBits'（菜单）/
# 'Qt51514QWindowToolSaveBits'（确认弹窗），并非标准 Win32 类名。Qt 对这类
# 临时/弹出型窗口有固定命名规律：'Qt5<版本号>QWindow<Popup|Tool>...'（主
# 窗口类名则是 'Qt51514QWindowIcon'，不含 Popup/Tool，可正确排除），故
# click_by_name_anywhere 按"标准类名 或 Qt弹出/工具窗口命名模式"双重匹配，
# 比硬编码精确类名更能容忍 Qt 版本号变化，且不做全窗口无差别扫描。
_MENU_DIALOG_CLASS_NAMES = ("#32768", "#32770")
_QT_POPUP_CLASS_RE = re.compile(r"^Qt5\d+QWindow(Popup|Tool)")

# ── UIA ControlType 名称映射（find_by_role 使用）────────────────────

_CONTROL_TYPE_MAP: dict[str, str] = {
    "button": "ButtonControl",
    "edit": "EditControl",
    "text": "TextControl", "statictext": "TextControl",
    "checkbox": "CheckBoxControl",
    "combobox": "ComboBoxControl",
    "list": "ListControl", "listitem": "ListItemControl",
    "menu": "MenuControl", "menuitem": "MenuItemControl",
    "tab": "TabControl", "tabitem": "TabItemControl",
    "tree": "TreeControl", "treeitem": "TreeItemControl",
    "window": "WindowControl", "pane": "PaneControl",
    "hyperlink": "HyperlinkControl", "image": "ImageControl",
    "slider": "SliderControl", "spinner": "SpinnerControl",
    "progressbar": "ProgressBarControl",
    "radiobutton": "RadioButtonControl",
    "scrollbar": "ScrollBarControl", "tooltip": "ToolTipControl",
    "datagrid": "DataGridControl", "datagridcell": "DataGridCellControl",
    "group": "GroupControl", "header": "HeaderControl", "headeritem": "HeaderItemControl",
    "document": "DocumentControl", "separator": "SeparatorControl",
    "semanticzoom": "SemanticZoomControl", "appbar": "AppBarControl",
}


@register_engine("*", "windows")
class WinUIAEngine(EngineBase):
    """Windows UIA engine via uiautomation（截图走 win32gui + 全屏兜底）。"""

    # ── 类级 STA 执行器 ───────────────────────────────────────────────

    _uia_executor: ClassVar[Optional[concurrent.futures.ThreadPoolExecutor]] = None
    _uia_executor_lock: ClassVar[threading.Lock] = threading.Lock()

    # 鼠标滚轮兜底：像素→刻度经验换算系数（见 _sync_scroll_wheel_fallback）
    _WHEEL_PIXELS_PER_TICK: ClassVar[int] = 100

    @staticmethod
    def _uia_thread_init() -> None:
        if _uia is not None:
            try:
                _uia.uiautomation.SetGlobalTimeout(5000)
            except Exception:
                pass
        if HAS_COMTYPES:
            try:
                import comtypes
                comtypes.CoInitialize()
            except Exception:
                pass

    @classmethod
    def _get_uia_executor(cls) -> concurrent.futures.ThreadPoolExecutor:
        if cls._uia_executor is None:
            with cls._uia_executor_lock:
                if cls._uia_executor is None:
                    cls._uia_executor = concurrent.futures.ThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix="uia-sta",
                        initializer=cls._uia_thread_init,
                    )
        return cls._uia_executor

    @classmethod
    def _shutdown_uia_executor(cls) -> None:
        """释放类级 STA 线程池。

        ThreadPoolExecutor 的 worker 线程默认非 daemon，进程退出时会阻塞等待
        所有非 daemon 线程结束；若不释放，batch 收尾（BATCH_ASYNC_RETURN）后
        python 进程永不退出（执行机观察：引擎关闭后进程仍存活且 CPU 持续活动）。
        线程池 shutdown 后置 None，后续 _get_uia_executor() 按需重建。
        """
        with cls._uia_executor_lock:
            if cls._uia_executor is not None:
                cls._uia_executor.shutdown(wait=False)
                cls._uia_executor = None

    async def _run_on_sta(self, fn, *args, timeout: Optional[float] = None):
        """经 STA 执行器调度，可选外层超时。

        2026-08-21 新增（见计划文档 20260820160000-mac-win-engine-
        screenshot-hang-isolation-and-shared-bounded-executor.md）：STA 单
        线程（max_workers=1）是 COM 线程亲和性的架构硬约束，一旦这唯一的
        worker 被卡死的调用占用，该引擎实例（及共享同一类级执行器的其他
        实例）后续所有 UIA 操作会永久排队、不会恢复——是三引擎中最严重的
        失败模式（"完全假死"而非"部分拖慢"）。

        ``timeout`` 为 ``None``（默认，现有 29 个调用点不传新参数）时行为
        与改动前完全一致：无限等待。传入具体秒数且调用超时时，不再无限期
        占用这唯一的 worker——放弃当前 STA 线程池（``_shutdown_uia_executor()``，
        该卡死线程作为孤儿在后台继续跑，但不再持有引用/不阻塞后续调用），
        下次调用惰性重建全新 STA 线程（重新执行 ``_uia_thread_init`` 完成
        COM 初始化），使引擎实例从"永久假死"恢复为"可继续工作"。
        """
        loop = asyncio.get_event_loop()
        if timeout is None:
            return await loop.run_in_executor(self._get_uia_executor(), fn, *args)
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self._get_uia_executor(), fn, *args),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "[WinUIA] STA 调用超时(%ds)，放弃当前 STA 线程并重建"
                "（该实例后续操作将使用全新 STA 线程恢复工作）", timeout,
            )
            self._shutdown_uia_executor()
            return None

    # ── 实例初始化 ─────────────────────────────────────────────────

    def __init__(self, app_path: str = "", app_name: str = "",
                 process_name: str = "", launch_args=None,
                 preserve_existing_processes: bool = False,
                 window_title: str = "", window_class_name: str = "",
                 force_launch: bool = False, **kwargs):
        self._app_path = app_path
        self._app_name = app_name or "app"
        # 上一次 _sync_kill() 的效果判定（见 docs/plans/
        # docs/plans/archive/01-engine-lifecycle-实施纪要.md）：
        #   True  = 已 kill 的进程全部确认退出
        #   False = 至少一个已 kill 的进程超时仍存活（疑似驱动级自我保护拦截，
        #           触发 core/lifecycle/engine_lifecycle.py::clean_launch()
        #           的升级检测）
        #   None  = 无法判定（未找到匹配进程可杀，或 psutil 不可用）
        # 供 clean_launch() 通过 getattr(engine, "last_kill_effective", None)
        # 探测，非 WinUIAEngine 引擎无此属性，行为不受影响（零回归）。
        self.last_kill_effective: Optional[bool] = None
        # 真实可执行文件名（不含 .exe，用于 _sync_kill 按进程名终止）。未显式
        # 指定时回退为 app_name（向后兼容旧行为，零回归）——但当窗口标题
        # 显示名（app_name，如 "示例桌面应用"）与真实 exe 名（如 "DemoTray"）
        # 不一致时必须显式传入，否则 _sync_kill 永远匹配不到进程，restart()
        # 硬重启形同虚设。见 docs/plans/archive/01-engine-lifecycle-实施纪要.md。
        self._process_name = process_name or self._app_name
        self._launch_args = list(launch_args or [])
        self._preserve_existing_processes = preserve_existing_processes
        self._force_launch = force_launch
        self._hwnd: Optional[int] = None
        self._window_handle_name: str = window_title or app_name
        self._window_class_name: str = window_class_name
        self._hwnd_stack: list[int] = []  # hwnd 栈：栈顶 = 当前操作窗口，见 R30 计划
        # docs/plans/archive/01-engine-lifecycle-实施纪要.md
        # QtProbe Qt 探针通道（可选能力）：attach 后非 None；None 时本引擎全部
        # 方法走原 UIA 逻辑，行为与引入该通道前逐字节一致。见 docs/plans/
        # 20261116203324-win-qt-probe-channel-plan.md。
        self._qt_probe: Optional[object] = None
        # 伴生进程探针（pid → 客户端）：主窗口进程之外的 Qt 进程（如轻页面 HUD
        # `MiniHomePagePro.exe`）各有独立探针，按 pid 分别持有。空字典即"只有主
        # 进程通道"，与引入该字段前行为一致。见 docs/plans/
        # 20261118190000-close-window-target-window.md §多进程探针。
        self._qt_probes: dict[int, object] = {}
        # window_hint 运行时状态：当前窗口作用域（None=无 / hwnd / _SCOPE_MISS）、
        # hint→hwnd 缓存（用前校验 IsWindow + 可见）、窗口后端（惰性装配）。
        self._scope_state: object = None
        self._hint_hwnd_cache: dict = {}
        self._win_backend: Optional[WindowBackend] = None
    # ── 元数据 ────────────────────────────────────────────────────

    @property
    def app_name(self) -> str:
        return self._app_name

    @property
    def platform(self) -> str:
        return "windows"

    @property
    def app_path(self) -> str:
        """已解析/配置的可执行文件完整路径（只读，供 quick_jump 等场景取安装目录）。

        见 docs/plans/archive/01-engine-lifecycle-实施纪要.md §7。
        """
        return self._app_path

    # ══════════════════════════════════════════════════════════════════
    # 生命周期
    # ══════════════════════════════════════════════════════════════════

    async def launch(self) -> None:
        """加载 uiautomation 模块 + 窗口绑定（窗口绑定延迟到首次操作）。"""
        if not HAS_UIA:
            raise RuntimeError("uiautomation 未安装（仅 Windows 可用）")
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._get_uia_executor(), self._sync_launch)

    def _sync_launch(self) -> None:
        """STA 线程内：初始化 COM + 寻找窗口 HWND；未找到且提供 app_path 时拉起应用。

        向后兼容：app_path 为空时保持原行为（仅附着已运行窗口，未找到不报错，
        `_hwnd` 保持 None，由调用方 `is_healthy()` 感知）。仅当 app_path 非空
        才启用"未找到窗口 → 拉起 → 轮询确认"的新逻辑（对应 compat-fix 计划 §3.1）。
        """
        if HAS_COMTYPES:
            try:
                import comtypes
                comtypes.CoInitialize()
            except Exception:
                pass
        hwnd = None if self._force_launch else self._ensure_hwnd()
        if hwnd is not None:
            self._bring_to_foreground(hwnd)
            return
        if not self._app_path:
            logger.debug("[WinUIA] 未找到窗口 '%s'，且未提供 app_path，跳过启动",
                         self._window_handle_name)
            return
        if not os.path.exists(self._app_path):
            raise RuntimeError(
                f"[WinUIA] 无法找到窗口 '{self._window_handle_name}'，"
                f"且 app_path 不存在: {self._app_path!r}"
            )
        logger.info("[WinUIA] 未找到窗口，启动应用: %s", self._app_path)
        # 三级窗口匹配基线（第 3 级 diff 兜底用，见下方循环）：拉起前先拍一次可见
        # 窗口快照。计划文档：
        # docs/plans/archive/01-engine-lifecycle-实施纪要.md §10.2
        baseline = self._sync_snapshot_visible_windows()
        proc = subprocess.Popen(
            [self._app_path, *self._launch_args],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for attempt in range(30):
            time.sleep(1.0)
            match_kind = "标题"
            hwnd = None if self._force_launch else self._ensure_hwnd()  # 1. 标题匹配（原逻辑，不变，优先尝试）
            if hwnd is None:
                # 2. PID 进程树匹配（新增）：应对 launcher 进程自己不建窗口、
                # 转而 spawn 子进程持有真实窗口的场景（如安装包/自解压程序）。
                match_kind = "PID树"
                hwnd = self._find_window_by_pid_tree(proc.pid, set(self._hwnd_stack))
            if hwnd is None:
                # 3. 全局可见窗口 diff 兜底（新增，最后一道防线）：标题与 PID
                # 树匹配都失败时才触发。
                match_kind = "可见窗口diff兜底"
                current = self._sync_snapshot_visible_windows()
                new_hwnds = current - baseline - set(self._hwnd_stack)
                hwnd = next(iter(new_hwnds), None)
            if hwnd is not None:
                self._hwnd = hwnd
                if not self._hwnd_stack:
                    self._hwnd_stack = [hwnd]
                logger.log(
                    NOTICE,
                    "[WinUIA] 启动并附着成功 (%.0fs, 匹配方式=%s): %s",
                    attempt + 1, match_kind, self._app_name,
                )
                self._bring_to_foreground(hwnd)
                return
        raise RuntimeError(f"[WinUIA] 应用启动超时 (30s): {self._app_path}")

    async def bring_to_foreground(self) -> None:
        """公开生命周期钩子：将被测 App 窗口置于前台（供
        core/lifecycle/engine_lifecycle.py::prepare_engine_for_case() 在每条
        用例开始前调用，与 reset_page_state() 同属"引擎可选扩展方法"模式，
        通过 getattr 探测，未实现的引擎/平台不受影响）。

        经 STA 执行器调度，与其余 hwnd 相关操作共用同一线程。见
        docs/plans/archive/01-engine-lifecycle-实施纪要.md。
        """
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._get_uia_executor(), self._sync_bring_to_foreground)

    def _sync_bring_to_foreground(self) -> None:
        # A force-launch ephemeral engine must not bind an existing same-title
        # window before its own process has been launched.
        if self._force_launch and self._hwnd is None:
            return
        hwnd = self._get_hwnd()
        if hwnd:
            self._bring_to_foreground(hwnd)

    def _bring_to_foreground(self, hwnd: int) -> None:
        """尽力将窗口置于前台可见（best-effort，失败不影响后续 Invoke/点击逻辑）。

        Windows 10+ 有"前台锁定超时"机制，非用户交互触发的进程调用
        SetForegroundWindow 可能被系统静默拒绝（不抛异常，仅不生效），本方法
        仅做视觉可见性增强，不作为点击成功的前置条件（点击判定逻辑不变，见
        docs/plans/archive/01-engine-lifecycle-实施纪要.md）。
        """
        attached_threads: list[int] = []
        try:
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            current_thread = kernel32.GetCurrentThreadId()
            foreground_hwnd = user32.GetForegroundWindow()
            thread_ids = [
                user32.GetWindowThreadProcessId(foreground_hwnd, None) if foreground_hwnd else 0,
                user32.GetWindowThreadProcessId(hwnd, None),
            ]
            for thread_id in thread_ids:
                if thread_id and thread_id != current_thread and thread_id not in attached_threads:
                    if user32.AttachThreadInput(current_thread, thread_id, True):
                        attached_threads.append(thread_id)

            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        except Exception as exc:
            logger.debug("[WinUIA] 置前台失败（忽略）: %s", exc)
        finally:
            for thread_id in reversed(attached_threads):
                try:
                    user32.AttachThreadInput(current_thread, thread_id, False)
                except Exception:
                    pass

    async def close(self) -> None:
        """清理 COM 资源 + 释放类级 STA 线程池（避免非 daemon 线程阻塞进程退出）。

        同时断开 qt 探针连接（只断连，不杀进程——探针随被测进程存活）。
        """
        await self.detach_qt_probe()
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._get_uia_executor(), self._sync_close)
        self._shutdown_uia_executor()

    def _sync_close(self) -> None:
        if HAS_COMTYPES:
            try:
                import comtypes
                comtypes.CoUninitialize()
            except Exception:
                pass
        self._hwnd = None
        self._hwnd_stack = []
        self._reset_window_hint_state()

    async def restart(self) -> None:
        """硬重启：按进程名终止 → 清空 hwnd 缓存 → 重新 launch()。

        对应 R11 硬重启语义：core.lifecycle.engine_lifecycle.clean_launch() 在
        engine 提供 restart() 时优先调用它（而非 close()+launch() 降级路径）。
        """
        logger.log(NOTICE, "[WinUIA] 重启应用: %s", self._app_name)
        loop = asyncio.get_event_loop()
        if self._preserve_existing_processes:
            await loop.run_in_executor(self._get_uia_executor(), self._sync_kill)
            await self.close()
        else:
            await self.close()
            await loop.run_in_executor(self._get_uia_executor(), self._sync_kill)
        await self.launch()

    async def terminate(self) -> None:
        """只终止进程，不重新拉起（区别于 restart() 的 kill+launch 组合）。

        可选生命周期钩子（getattr 探测模式，不加入 EngineBase 18 个抽象方法，
        R9：引擎专用能力写在具体引擎类）。复用现成的 _sync_kill()（hwnd 精确
        kill + process_name 兜底 + 轮询确认退出）。供 run_exe[] 场景的
        AppLifecycleStack.evict() 使用。

        **调用顺序：先 kill 后 close**（区别于 `restart()` 现状的
        close→kill→launch 顺序）——`_sync_close()` 会把 `self._hwnd`/
        `self._hwnd_stack` 清空为 `None`/`[]`，若先 close 再 kill，
        `_sync_kill()` 内依赖 hwnd 反查 PID 的精确终止（步骤 0/1，见
        `_sync_kill()` 文档）会因 hwnd 已被清空而完全失效，只能退化到
        `process_name` 扫描兜底（步骤 2，仅精确匹配启动时的 exe 文件名）。
        真机验证发现：cpu-z 安装向导场景下，launcher 进程（`cpu-z_2.18-cn.exe`）
        自身很快退出、真正持有窗口的是不同名的子进程
        （`cpu-z_2.18-cn.tmp.exe`，由 `_find_window_by_pid_tree()` 匹配到的
        hwnd 所属），`process_name` 扫描匹配不到它，导致 evict() 报告"已
        evict"但该子进程实际仍存活（与 `docs/plans/20260725110000-sample_web-
        device-status-card-popup-window.md`"踩坑记录"同类问题：action 执行
        无异常≠真正生效）。改为先 kill（此时 hwnd/栈仍完整，步骤 0/1 精确
        kill 生效）再 close，修复此问题。见
        docs/plans/archive/01-engine-lifecycle-实施纪要.md §11。
        """
        logger.info("[WinUIA] 终止应用（不重启）: %s", self._app_name)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._get_uia_executor(), self._sync_kill)
        await self.close()

    def _sync_kill(self) -> None:
        """终止应用进程：优先按当前 hwnd 反查真正持有窗口的 PID 精确终止，
        `process_name` 扫描作为兜底（覆盖 hwnd 缺失，或托盘/主界面分离等
        多进程架构下的其余同名残留进程），kill 后轮询确认目标 PID 真正
        退出，异常分级记录日志（不再完全静默）。

        见 docs/plans/archive/01-engine-lifecycle-实施纪要.md。

        结尾汇总设置 ``self.last_kill_effective``（见 docs/plans/
        archive/01-engine-lifecycle-实施纪要.md，
        修复"kill 被驱动级自我保护拦截但 restart() 仍判定为完全成功"的
        真机复现问题）：
          - 未找到任何匹配进程可杀（``killed_pids`` 为空）或 psutil 不可用
            → ``None``（无法判定，不属于"检测到拦截"场景，不触发升级）
          - 全部确认退出 → ``True``
          - 至少一个超时仍存活 → ``False``（真正的"kill 未生效"信号，供
            ``clean_launch()`` 检测并升级到 ``LaunchPolicy.
            on_hard_restart_ineffective()`` 兜底）
        """
        if not HAS_PSUTIL:
            logger.warning("[WinUIA] psutil 不可用，无法终止进程")
            self._hwnd = None
            self._hwnd_stack = []
            self._reset_window_hint_state()
            self.last_kill_effective = None
            return
        killed_pids: set[int] = set()
        # 0. hwnd 栈内除主窗口(栈底)外所有栈帧对应进程精确 kill：覆盖
        #    jump[] 拉起的独立子进程窗口（如 sample_web 设置中心 AppSetting.exe，
        #    与主进程 QMUI.exe 不同 PID）残留场景——该场景下若仅按①②步
        #    （self._hwnd/process_name）终止，子窗口进程可能被漏杀，导致
        #    下次 jump[] 因目标为单例应用而被转发给残留旧实例、新进程秒退，
        #    register_child_process_window 永远等不到新窗口而超时。见
        #    docs/plans/archive/01-engine-lifecycle-实施纪要.md。
        #    栈深 ≤1（无子窗口）时循环零次执行，行为与改造前完全一致（零回归）。
        for stacked_hwnd in self._hwnd_stack[1:]:
            stacked_pid = self._get_pid_from_hwnd(stacked_hwnd)
            if stacked_pid and stacked_pid not in killed_pids:
                self._kill_pid(stacked_pid, killed_pids)
        # 1. hwnd 反查 PID 精确杀（最可靠路径，不依赖 process_name 猜测）
        hwnd_pid = self._get_pid_from_hwnd(self._hwnd) if self._hwnd else None
        if hwnd_pid and hwnd_pid not in killed_pids:
            self._kill_pid(hwnd_pid, killed_pids)
        # 2. process_name 扫描兜底（hwnd 为 None，或需补杀 hwnd 命中之外的
        #    其余同名残留进程，如托盘/主界面分离架构）
        if not self._preserve_existing_processes:
            target = f"{self._process_name}.exe".lower()
            for proc in psutil.process_iter(["pid", "name"]):
                try:
                    pname = (proc.info.get("name") or "").lower()
                    if pname == target and proc.pid not in killed_pids:
                        self._kill_pid(proc.pid, killed_pids, proc=proc)
                except Exception as exc:
                    logger.debug("[WinUIA] 扫描进程列表时异常（忽略）: %s", exc)
                    continue
        # 3. 存活确认（轮询最多 3s，超时仅 warning 不阻断），汇总判定结果
        if killed_pids:
            all_exited = True
            for pid in killed_pids:
                if not self._wait_pid_exit(pid):
                    all_exited = False
            self.last_kill_effective = all_exited
            time.sleep(1.5)
        else:
            self.last_kill_effective = None
        self._hwnd = None
        self._hwnd_stack = []
        self._reset_window_hint_state()

    @staticmethod
    def _get_pid_from_hwnd(hwnd: int) -> Optional[int]:
        """通过 Win32 `GetWindowThreadProcessId` 反查窗口所属进程 PID
        （Win32 标准语义：返回窗口所属进程，而非线程宿主进程）。"""
        try:
            import ctypes
            pid = ctypes.c_ulong(0)
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return pid.value or None
        except Exception as exc:
            logger.debug("[WinUIA] GetWindowThreadProcessId 失败: %s", exc)
            return None

    @staticmethod
    def _kill_pid(pid: int, killed_pids: set, proc=None) -> None:
        """终止单个 PID，异常分级记录日志：`AccessDenied` 单独 warning
        （提示可能权限不足/应用自我保护拦截），其余异常 debug 级别。"""
        try:
            target_proc = proc if proc is not None else psutil.Process(pid)
            target_proc.kill()
            killed_pids.add(pid)
            logger.info("[WinUIA] 已终止 PID=%s", pid)
        except psutil.AccessDenied:
            logger.warning(
                "[WinUIA] 终止 PID=%s 被拒绝（AccessDenied，可能权限不足或"
                "应用自我保护拦截）", pid,
            )
        except psutil.NoSuchProcess:
            logger.debug("[WinUIA] PID=%s 已不存在，跳过", pid)
        except Exception as exc:
            logger.debug("[WinUIA] 终止 PID=%s 异常（忽略）: %s", pid, exc)

    @staticmethod
    def _wait_pid_exit(pid: int, timeout: float = 3.0) -> bool:
        """轮询确认 PID 真正退出（`psutil.pid_exists`），最多等待
        `timeout` 秒，超时仅记录 warning 不阻断（避免真机无限期卡住）。

        返回 ``True``=确认已退出（或 `psutil.pid_exists` 异常，视为已退出，
        与原静默行为一致），``False``=超时后仍确认存活（供
        ``_sync_kill()`` 汇总 ``last_kill_effective``）。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if not psutil.pid_exists(pid):
                    return True
            except Exception:
                return True
            time.sleep(0.2)
        try:
            still_alive = psutil.pid_exists(pid)
        except Exception:
            still_alive = False
        if still_alive:
            logger.warning(
                "[WinUIA] PID=%s 在 %.1fs 内未确认退出（可能被自我保护拦截）",
                pid, timeout,
            )
            return False
        return True

    async def is_healthy(self) -> bool:
        """检查执行器 + 窗口状态。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._get_uia_executor(), self._sync_is_healthy)

    def _sync_is_healthy(self) -> bool:
        if not HAS_UIA:
            return False
        try:
            hwnd = self._get_hwnd()
            return hwnd is not None
        except Exception:
            return False

    # ══════════════════════════════════════════════════════════════════
    # 跨窗口/系统级点击（系统托盘图标、菜单项、对话框按钮）
    #
    # 与 find_by_id/click 的区别：后者限定在 self._hwnd（被测 App 自身窗口）
    # 子树内查找；本节两个方法面向"当前被测 App 窗口子树之外"的系统级 UI
    # （任务栏托盘图标宿主窗口 Shell_TrayWnd、右键菜单、系统对话框），供
    # apps/sample_web/sample_launch_policy.py::quit_via_tray() 驱动"合法退出"复用（见
    # docs/plans/archive/01-engine-lifecycle-实施纪要.md）。
    # 不作为 EngineBase 抽象方法新增（R9：引擎专用能力写在具体引擎类）。
    # ══════════════════════════════════════════════════════════════════

    async def find_and_click_taskbar_icon(
        self, tooltip_keyword: str, right_click: bool = True,
    ) -> bool:
        """在系统任务栏托盘区查找 Name/工具提示包含 ``tooltip_keyword`` 的图标
        并点击（默认右键，用于唤出图标右键菜单）。

        枚举顺序：先查 ``Shell_TrayWnd``（任务栏常驻可见图标区），未命中再查
        "显示隐藏的图标"弹出窗口（按 ``_TRAY_OVERFLOW_CLASS_CANDIDATES`` 顺序
        遍历候选窗口类名，兼容不同 Windows 版本/语言，见模块顶部常量注释）——
        若该弹出窗口本身未展开，先点击任务栏上的展开箭头按钮（同样遍历
        ``_TRAY_OVERFLOW_BUTTON_NAME_CANDIDATES`` 候选 Name）再重试一次。

        找不到返回 ``False``（不抛异常，由调用方决定后续降级路径）。
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_find_and_click_taskbar_icon,
            tooltip_keyword, right_click,
        )

    def _sync_find_and_click_taskbar_icon(
        self, tooltip_keyword: str, right_click: bool,
    ) -> bool:
        if not HAS_UIA:
            return False
        # 1. 任务栏常驻可见图标区
        icon = self._find_taskbar_icon_in(_TRAY_WND_CLASS, tooltip_keyword)
        if icon is not None:
            return self._click_uia_control(icon, right_click)
        # 2. "显示隐藏的图标"弹出窗口：溢出窗口类名遍历候选列表；未展开时
        #    先点箭头展开（箭头 Name 同样遍历候选列表），再重试一次
        icon = self._find_taskbar_icon_in_overflow(tooltip_keyword)
        if icon is None and self._expand_hidden_icons_arrow():
            time.sleep(0.5)
            icon = self._find_taskbar_icon_in_overflow(tooltip_keyword)
        if icon is not None:
            return self._click_uia_control(icon, right_click)
        logger.warning(
            "[WinUIA] 任务栏未找到包含 '%s' 的图标（已尝试展开隐藏图标箭头）",
            tooltip_keyword,
        )
        return False

    @staticmethod
    def _find_taskbar_icon_in_overflow(keyword: str) -> Optional[object]:
        """在"显示隐藏的图标"溢出窗口内查找目标图标，按
        ``_TRAY_OVERFLOW_CLASS_CANDIDATES`` 顺序遍历候选窗口类名，命中任一
        即返回（见模块顶部候选列表注释）。全部未命中返回 ``None``。"""
        for class_name in _TRAY_OVERFLOW_CLASS_CANDIDATES:
            icon = WinUIAEngine._find_taskbar_icon_in(class_name, keyword)
            if icon is not None:
                return icon
        return None

    @staticmethod
    def _find_taskbar_icon_in(
        window_class_name: str, keyword: str, require_systray_class: bool = True,
    ) -> Optional[object]:
        """在指定顶层窗口（按 ClassName 查找）子树中搜索 Name 包含 ``keyword``
        的图标控件（DFS，深度限制 10 层，任务栏图标区结构较浅）。

        ``require_systray_class=True``（默认）时额外要求控件 ClassName 以
        ``SystemTray.`` 为前缀，即真正的系统通知区图标——真机验证发现
        （2026-07-21）``Shell_TrayWnd`` 子树内还包含任务栏固定/运行中应用
        图标（ClassName='Taskbar.TaskListButtonAutomationPeer'），其 Name
        可能包含同一关键字（如被测 App 名），若不限定 ClassName 前缀会
        误命中错误的图标（真机复现：右键点开了任务栏应用图标菜单而非系统
        托盘图标菜单）。"展开隐藏图标箭头"按钮同样是 ``SystemTray.
        NormalButton``，复用同一前缀限制不影响该场景。
        """
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(window_class_name, None)
            if not hwnd:
                return None
            root = _uia.ControlFromHandle(hwnd)
            if root is None:
                return None
        except Exception as exc:
            logger.debug("[WinUIA] _find_taskbar_icon_in(%s) 查找窗口失败: %s",
                         window_class_name, exc)
            return None

        def _walk(ctrl, depth: int):
            if depth > 10:
                return None
            try:
                name = ctrl.Name or ""
                cls = ctrl.ClassName or ""
                class_ok = (not require_systray_class
                            or cls.startswith(_SYSTEM_TRAY_CLASS_PREFIX))
                if keyword in name and class_ok:
                    return ctrl
            except Exception:
                return None
            try:
                children = ctrl.GetChildren()
            except Exception:
                return None
            for ch in children:
                r = _walk(ch, depth + 1)
                if r is not None:
                    return r
            return None

        try:
            return _walk(root, 0)
        except Exception:
            return None

    @staticmethod
    def _expand_hidden_icons_arrow() -> bool:
        """点击任务栏"显示隐藏的图标"展开箭头按钮（若存在）。找不到/点击失败
        返回 ``False``（best-effort，不抛异常）。"""
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(_TRAY_WND_CLASS, None)
            if not hwnd:
                return False
            root = _uia.ControlFromHandle(hwnd)
            if root is None:
                return False
        except Exception:
            return False
        arrow = None
        for button_name in _TRAY_OVERFLOW_BUTTON_NAME_CANDIDATES:
            arrow = WinUIAEngine._find_taskbar_icon_in(_TRAY_WND_CLASS, button_name)
            if arrow is not None:
                break
        if arrow is None:
            return False
        return WinUIAEngine._click_uia_control(arrow, right_click=False)

    @staticmethod
    def _click_uia_control(control, right_click: bool = False) -> bool:
        """点击/右键点击任意 uiautomation Control。

        左键点击（``right_click=False``）优先级（**2026-07-21 真机验证
        修订**，见 docs/plans/20260721100000-win-ui-interactive-
        restart.md §八）：
        1. **InvokePattern.Invoke()** 优先——对 ``control`` 本身，或找不到
           则向上查找最近支持 InvokePattern 的祖先（按 Name 精确匹配到的
           控件常是纯文本子节点，如 Qt 的 ``QLabel``，真正支持
           InvokePattern 的是其按钮祖先，见 ``_resolve_invokable``）。
           **真机验证的关键发现**：部分安全确认对话框按钮（如"退出应用"
           确认弹窗的"确定"）会**拒绝模拟坐标点击**（UI 上提示"不支持
           远程操作退出"，判定为疑似检测"非真实硬件输入"并拦截，坐标
           点击的 `pyautogui.click()`/`SendInput` 均不生效但也不抛异常，
           此前 `_click_uia_control` 误判为"点击成功"），但 UIA
           `InvokePattern`（可访问性 API 原生调用，不经过 SendInput 合成
           输入事件）可以绕过该检测，真正触发按钮点击——已真机重复验证
           2 次全部成功。
        2. 坐标点击兜底（InvokePattern 不可用/调用失败时，如托盘图标等
           非标准按钮控件，真机验证对这类控件坐标点击本身是有效的）。

        右键点击（``right_click=True``）没有 InvokePattern 等价语义，只能
        坐标点击（用于系统托盘图标唤出右键菜单）。
        """
        if not right_click:
            target = WinUIAEngine._resolve_invokable(control)
            if target is not None:
                try:
                    target.GetInvokePattern().Invoke()
                    return True
                except Exception as exc:
                    logger.debug("[WinUIA] _click_uia_control InvokePattern 失败: %s", exc)
        try:
            rect = control.BoundingRectangle
            if rect and HAS_PYAUTOGUI:
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                if right_click:
                    pyautogui.rightClick(cx, cy)
                else:
                    pyautogui.click(cx, cy)
                return True
        except Exception as exc:
            logger.debug("[WinUIA] _click_uia_control 坐标点击失败: %s", exc)
        try:
            control.Invoke()
            return True
        except Exception as exc:
            logger.debug("[WinUIA] _click_uia_control Invoke 失败: %s", exc)
            return False

    @staticmethod
    def _resolve_invokable(control, max_depth: int = 6) -> Optional[object]:
        """从 ``control`` 本身开始逐层向上查找最近支持 ``InvokePattern`` 的
        祖先控件（按 Name 精确匹配到的控件常是纯文本子节点，如 Qt 的
        ``QLabel``，真正支持 InvokePattern 的往往是其按钮祖先，见
        docs/plans/archive/01-engine-lifecycle-实施纪要.md §八）。
        找不到返回 ``None``（不代表失败，调用方会降级坐标点击）。"""
        ctrl = control
        for _ in range(max_depth):
            try:
                if ctrl.GetInvokePattern():
                    return ctrl
            except Exception:
                pass
            try:
                ctrl = ctrl.GetParentControl()
            except Exception:
                return None
            if ctrl is None:
                return None
        return None

    async def click_by_name_anywhere(self, name: str, timeout_s: float = 5.0) -> bool:
        """在"当前活跃的顶层菜单/对话框窗口"（Win32 类名 ``#32768``/``#32770``，
        即右键菜单、系统消息弹窗）中查找 Name 精确等于 ``name`` 的控件并点击。

        用于点击托盘右键菜单项（如"退出应用"）、确认弹窗按钮（如"确定"）—
        这些控件既不在 ``self._hwnd``（被测 App 主窗口）子树内，也不在任务栏
        子树内，而是各自独立的顶层弹出窗口，故需要单独的"按前台弹出窗口类名
        查找"逻辑，不复用 find_by_id。

        轮询 ``timeout_s`` 秒等待弹出窗口出现（菜单/弹窗展开有动画延迟），
        找到即点击返回 ``True``；超时未找到返回 ``False``（不抛异常）。
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_click_by_name_anywhere, name, timeout_s,
        )

    def _sync_click_by_name_anywhere(self, name: str, timeout_s: float) -> bool:
        if not HAS_UIA:
            return False
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            control = self._find_control_in_popup_windows(name)
            if control is not None:
                return self._click_uia_control(control, right_click=False)
            time.sleep(0.3)
        logger.warning("[WinUIA] click_by_name_anywhere('%s') 超时(%.1fs)未找到", name, timeout_s)
        return False

    @staticmethod
    def _find_control_in_popup_windows(name: str) -> Optional[object]:
        """枚举所有可见顶层窗口，筛选 Win32 类名匹配菜单/对话框（标准类名
        ``#32768``/``#32770``，或 Qt 自绘弹出/工具窗口命名模式，见
        ``_QT_POPUP_CLASS_RE`` 常量说明），在其中 DFS 搜索 Name == ``name``
        的控件。"""
        try:
            import ctypes
            hwnds: list[int] = []

            def _enum(hwnd, _):
                if not ctypes.windll.user32.IsWindowVisible(hwnd):
                    return True
                buf = ctypes.create_unicode_buffer(64)
                ctypes.windll.user32.GetClassNameW(hwnd, buf, 64)
                cls = buf.value
                if cls in _MENU_DIALOG_CLASS_NAMES or _QT_POPUP_CLASS_RE.match(cls):
                    hwnds.append(hwnd)
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
            ctypes.windll.user32.EnumWindows(WNDENUMPROC(_enum), 0)
        except Exception as exc:
            logger.debug("[WinUIA] _find_control_in_popup_windows 枚举窗口失败: %s", exc)
            return None

        for hwnd in hwnds:
            try:
                root = _uia.ControlFromHandle(hwnd)
                if root is None:
                    continue
            except Exception:
                continue
            found = WinUIAEngine._dfs_find_by_name(root, name, max_depth=10)
            if found is not None:
                return found
        return None

    @staticmethod
    def _dfs_find_by_name(root, name: str, max_depth: int = 10) -> Optional[object]:
        def _walk(ctrl, depth: int):
            if depth > max_depth:
                return None
            try:
                if (ctrl.Name or "") == name:
                    return ctrl
            except Exception:
                return None
            try:
                children = ctrl.GetChildren()
            except Exception:
                return None
            for ch in children:
                r = _walk(ch, depth + 1)
                if r is not None:
                    return r
            return None
        try:
            return _walk(root, 0)
        except Exception:
            return None

    # ══════════════════════════════════════════════════════════════════
    # 元素查找
    # ══════════════════════════════════════════════════════════════════

    # ── QtProbe Qt 探针通道（可选能力）───────────────────────────────
    #
    # 定位：同一窗口的第二个数据通道（不是第二个引擎）——进程生命周期仍由
    # UIA 侧负责（launch/restart/进程守护/托盘），本通道只做「连上 + 定位 +
    # 操作」。未 attach 时下面全部方法与属性均不影响原逻辑（零回归）。
    # 见 docs/plans/20261116203324-win-qt-probe-channel-plan.md。

    QT_PROBE_DEFAULT_URL = "http://127.0.0.1:3000/mcp"

    @property
    def has_qt_probe(self) -> bool:
        """探针通道是否已就绪（Resolver 的 QT_PROBE 策略据此做能力裁剪）。"""
        return self._qt_probe is not None

    def _qt_clients(self) -> list:
        """全部已装配探针客户端：主进程在前，伴生进程随后（去重、保序）。

        `_qt_probe` 单通道时代的全部语义都等价于"列表里只有主进程"这一种，
        因此多进程扩展对未启用伴生进程的环境零影响。
        """
        clients: list = [self._qt_probe] if self._qt_probe is not None else []
        for client in self._qt_probes.values():
            if client is not None and client is not self._qt_probe:
                clients.append(client)
        return clients

    def _qt_client_for_pid(self, pid: Optional[int]) -> Optional[object]:
        """按 pid 找对应探针客户端（找不到返回 None）。"""
        if not pid:
            return None
        for client in self._qt_clients():
            if getattr(client, "pid", None) == pid:
                return client
        return None

    async def attach_qt_probe(
        self, *, url: str = "", pid: Optional[int] = None, timeout_s: float = 3.0,
    ) -> bool:
        """连接 QtProbe 探针（best-effort，失败只记日志并返回 False）。

        注入不在本方法内发生：注入属部署期一次性动作（`--mode probe-provision`），
        运行期只连接（读自加载探针的端口/服务）。未做 provision 的机器会因
        `qt_health` 不通而返回 False，后续全部元素回落 UIA。

        首个成功连接成为主通道（`_qt_probe`）；此后传入**不同** pid 时该进程
        作为伴生通道存入 `_qt_probes`，主通道不变（多进程页面共用同一引擎）。
        """
        try:
            from uita.engine.win_uia import qt_client as _qt
        except Exception as exc:  # noqa: BLE001 — 客户端模块问题不应影响 UIA 主链路
            logger.warning("[WinUIA][qt] 客户端模块不可用: %s", exc)
            return False

        effective_url = (
            url or os.environ.get("UITA_QT_PROBE_URL", "") or self.QT_PROBE_DEFAULT_URL
        ).strip()
        target_pid = pid or self._qt_probe_pid()
        if not target_pid:
            logger.warning("[WinUIA][qt] 无法确定目标进程 PID（窗口未就绪？），跳过 attach")
            return False

        if self._qt_client_for_pid(target_pid) is not None:
            logger.debug("[WinUIA][qt] pid=%s 已有探针连接，跳过重复 attach", target_pid)
            return True

        client = _qt.QtProbeClient(effective_url, timeout=timeout_s, pid=target_pid)
        # daemon 只记录 agent 端口、不会自动连接：必须先 launch_or_inject 建立
        # 连接，否则 qt_health 恒返回 EADDRNOTAVAIL（看起来像网络故障）。
        health = await client.connect(attempts=3, backoff_s=0.8)
        if not health:
            status = await client.status()
            await client.close()
            logger.warning(
                "[WinUIA][qt] 探针连接失败（daemon 未起/未注入/权限不足/进程非 GUI 主进程？），"
                "本次降级 UIA：url=%s pid=%s status=%s",
                effective_url, target_pid, status,
            )
            return False

        if self._qt_probe is None:
            self._qt_probe = client
            role = "primary"
        else:
            self._qt_probes[int(target_pid)] = client
            role = "companion"
        logger.log(
            NOTICE, "[WinUIA][qt] 探针通道就绪(%s) app=%s pid=%s url=%s",
            role, self._app_name, target_pid, effective_url,
        )
        return True

    async def attach_qt_probe_for_process(
        self, exe_name: str, *, url: str = "", timeout_s: float = 3.0,
    ) -> bool:
        """按进程名装配伴生进程探针（如轻页面 `MiniHomePagePro.exe`）。

        pid 由 QtProbe daemon 的端口文件（`<ProgramData>\\QtProbe\\ports\\*.json`）
        解析——该文件是 provision/自加载的产物，运行期只读。进程不存在或无端口
        文件时返回 False（调用方按"该页面不可探"处理，不降级失败）。
        """
        try:
            from uita.engine.win_uia.qt_client import discover_probe_pid
        except Exception as exc:  # noqa: BLE001
            logger.warning("[WinUIA][qt] 客户端模块不可用: %s", exc)
            return False
        if not exe_name:
            return False
        target_pid = discover_probe_pid(exe_name)
        if not target_pid:
            logger.debug("[WinUIA][qt] 未找到 %s 的探针端口文件，跳过伴生通道装配", exe_name)
            return False
        return await self.attach_qt_probe(url=url, pid=target_pid, timeout_s=timeout_s)

    async def detach_qt_probe(self) -> None:
        """断开全部探针连接（主进程 + 伴生进程），不杀被测进程。"""
        clients = self._qt_clients()
        self._qt_probe = None
        self._qt_probes.clear()
        for client in clients:
            try:
                await client.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[WinUIA][qt] 断开连接异常（忽略）: %s", exc)

    def _qt_probe_pid(self) -> Optional[int]:
        """当前主窗口所属进程 PID（探针自加载在该进程内，用于句柄校验）。"""
        try:
            hwnd = self._get_hwnd()
        except Exception:
            hwnd = None
        if not hwnd:
            return None
        return self._get_pid_from_hwnd(hwnd)

    def _qt_client_of(self, element: Optional[ElementRef]) -> Optional[tuple[object, str]]:
        """解析 ElementRef 是否为本通道句柄 → (客户端, objId)；否则 None。

        句柄 pid 必须命中已装配的探针（主进程或伴生进程），否则视为失效
        （应用进程守护/升级会重启进程，旧 objId 可能指向新进程中的其他控件 → 误点）。
        """
        if element is None:
            return None
        from uita.engine.win_uia.qt_client import parse_handle

        parsed = parse_handle(getattr(element, "element_id", "") or "")
        if parsed is None:
            return None
        pid, obj_id = parsed
        client = self._qt_client_for_pid(pid)
        if client is None:
            known = [getattr(c, "pid", None) for c in self._qt_clients()]
            logger.warning(
                "[WinUIA][qt] 句柄 pid=%s 无对应探针连接（已知 %s），判失效、降级 UIA",
                pid, known,
            )
            return None
        return client, obj_id

    def _qt_handle_of(self, element: Optional[ElementRef]) -> Optional[tuple[int, str]]:
        """解析 ElementRef 是否为本通道句柄 → (pid, objId)；否则 None（兼容旧调用）。"""
        hit = self._qt_client_of(element)
        if hit is None:
            return None
        return getattr(hit[0], "pid", 0) or 0, hit[1]

    async def _qt_property(self, obj_id: str, client: Optional[object] = None) -> dict:
        """读控件属性（探针 `properties`），失败返回空 dict（调用方按缺省处理）。"""
        target = client or self._qt_probe
        if target is None:
            return {}
        try:
            props = await target.properties(obj_id)
        except Exception as exc:  # noqa: BLE001 — 属性读取失败不改变控制流
            logger.debug("[WinUIA][qt] properties 失败: %s", exc)
            return {}
        if not isinstance(props, dict):
            return {}
        nested = props.get("properties")
        if isinstance(nested, dict):
            return {**props, **nested}
        return props

    async def find_by_qt_path(
        self, qt_path: str, *, timeout_s: float = 5.0,
    ) -> Optional[ElementRef]:
        """按 rosetta_map 的 `qt` 字段（XPath 风格路径）定位控件。

        多进程场景按「主通道 → 伴生通道」顺序查找，首个命中即返回：该顺序保证
        主窗口元素仍走一次调用（与单通道时代等价），只有主通道未命中才多付一次
        探针往返；某条通道抛错时只记日志并继续下一条，不整体失败。
        """
        if not qt_path:
            return None
        clients = self._qt_clients()
        if not clients:
            return None
        from uita.engine.win_uia.qt_client import make_handle, rect_from

        for client in clients:
            client_pid = getattr(client, "pid", 0) or 0
            try:
                obj = await client.find(qt_path, timeout_s=timeout_s)
            except Exception as exc:  # noqa: BLE001 — 定位失败只降级，不抛
                logger.warning(
                    "[WinUIA][qt] find 失败（pid=%s，试下一条通道）: %r — %s",
                    client_pid, qt_path, exc,
                )
                continue
            if not obj or obj.get("id") is None:
                logger.debug("[WinUIA][qt] 未命中（pid=%s）: %r", client_pid, qt_path)
                continue
            obj_id = str(obj["id"])
            logger.debug("[WinUIA][qt] 命中（pid=%s）: %r → %s", client_pid, qt_path, obj_id)
            return ElementRef(
                element_id=make_handle(client_pid, obj_id),
                raw=None,
                tag=str(obj.get("className", "") or ""),
                text=str(obj.get("text", "") or ""),
                bbox=rect_from(obj),
            )
        return None

    async def find_by_id(self, element_id: str) -> Optional[ElementRef]:
        loop = asyncio.get_event_loop()
        control = await loop.run_in_executor(
            self._get_uia_executor(), self._sync_find_by_id, element_id,
        )
        if control is None:
            return None
        try:
            name = control.Name or ""
        except Exception:
            name = ""
        return ElementRef(element_id=element_id, raw=control, text=name)

    def _sync_find_by_id(self, element_id: str) -> Optional[object]:
        """UIA 树 DFS 定位元素。element_id 来自 rosetta_map 的 win 字段，
        格式为 QPath：@Name='xxx' 或 @AutomationId='yyy'，或纯文本 Name。

        2026-07-16 扩展（见 docs/plans/archive/01-engine-lifecycle-实施纪要.md）：
        新增 @ClassName='xxx' 与 @ChildText='xxx' 两个属性，用于定位无 Name/
        AutomationId 但有稳定 ClassName 的控件（如 Qt 应用的 CardButton），
        ChildText 用于在同 ClassName 多实例间按子孙节点文本进一步区分
        （如 @ClassName='CardButton'@ChildText='存储概览'）。

        2026-07-24 扩展（见 docs/plans/20260724190000-sample_web-win-settings-center-
        atomic-elements.md）：新增 @SiblingText='xxx' 属性，用于定位无 Name/
        AutomationId/TogglePattern、且说明文字是**同级兄弟节点**（而非子孙
        节点）的控件（如设置中心页的 QPushButton 开关按钮，其说明文字与开关
        是同一父节点下的兄弟 QLabel）。ChildText 搜子孙树，SiblingText 搜
        同级兄弟节点，两者互补，不可混用替代。
        """
        if not HAS_UIA:
            return None
        name = self._parse_qpath_attr(element_id, "Name")
        name_regex = self._parse_legacy_regex_attr(element_id, "Name")
        automation_id = self._parse_qpath_attr(element_id, "AutomationId")
        control_type = self._parse_qpath_attr(element_id, "ControlType")
        class_name = self._parse_qpath_attr(element_id, "ClassName")
        child_text = self._parse_qpath_attr(element_id, "ChildText")
        parent_class_name = self._parse_qpath_attr(element_id, "ParentClassName")
        parent_child_text = self._parse_qpath_attr(element_id, "ParentChildText")
        sibling_text = self._parse_qpath_attr(element_id, "SiblingText")
        max_depth_raw = self._parse_legacy_attr(element_id, "MaxDepth")
        try:
            max_depth = max(0, int(max_depth_raw)) if max_depth_raw else 20
        except ValueError:
            max_depth = 20
        # legacy_runner 方言：/Name='...' && ControlType='Button' && MaxDepth='10'
        name = name or self._parse_legacy_attr(element_id, "Name")
        automation_id = automation_id or self._parse_legacy_attr(element_id, "AutomationId")
        control_type = control_type or self._parse_legacy_attr(element_id, "ControlType")
        class_name = class_name or self._parse_legacy_attr(element_id, "ClassName")
        if control_type and not control_type.endswith("Control"):
            control_type += "Control"
        if not name and not name_regex and not automation_id and not class_name:
            # 纯文本：Name 匹配
            name = element_id.strip()
        locator = ControlLocator(
            name=name, name_regex=name_regex, automation_id=automation_id,
            control_type=control_type, class_name=class_name,
            child_text=child_text, sibling_text=sibling_text, max_depth=max_depth,
        )

        # window_hint 窗口作用域（见 docs/plans/20261119103000-win-window-hint-and-
        # pywinauto-adapter.md）：resolver 在 `window_scope()` 内调用本方法时，
        # 只在声明的窗口里找，**不触发**子窗口跟随 / 回退主窗口等启发式
        # （否则同名元素会被误命中到别的窗口）。
        scope = getattr(self, "_scope_state", None)
        if scope is not None:
            if scope is _SCOPE_MISS:
                return None  # 严格模式下 hint 未命中任何窗口
            return self._find_in_window(int(scope), locator, parent_class_name, parent_child_text)

        hwnd = self._ensure_hwnd()
        if not hwnd:
            return None
        try:
            root = _uia.ControlFromHandle(hwnd)
            if root is None:
                return None
        except Exception as exc:
            logger.debug("[WinUIA] ControlFromHandle 失败: %s", exc)
            return None

        # @ParentClassName/@ParentChildText 作用域限定（见 docs/plans/
        # docs/plans/archive/03-resolver-rosetta-实施纪要.md §2.1）：
        # 无 Name/AutomationId/ChildText 的子控件（如卡片内箭头 QPushButton）
        # 与同 ClassName 的其他实例无法区分，先按父容器 ClassName+ChildText
        # 定位到父容器，再在其子树内按剩余属性（ClassName 等）二次查找，
        # 避免全局同 ClassName 误命中。零回归：不含该属性时行为不变。
        if parent_class_name or parent_child_text:
            parent = self._find_uia_control(
                root, "", "", "", class_name=parent_class_name,
                child_text=parent_child_text, max_depth=20,
            )
            if parent is None:
                return None
            root = parent
            root_hwnd = None  # 父容器子树内搜索：始终走 uiautomation（pywinauto 无法以控件为根）
        else:
            root_hwnd = hwnd

        result = self._search_root(root, root_hwnd, locator)
        if result is not None:
            return result

        # 多进程页面兜底（见 docs/plans/20261118180000-win-uia-follow-child-window.md）：
        #   1) 把同应用进程树内的新子窗口压栈后重试 —— 覆盖"目标在子窗口、当前根在主窗口"；
        #   2) 若仍失败且栈深 >1（子窗口仍开着占住栈顶），回退到栈底主窗口再试一次 ——
        #      覆盖"子窗口开着、目标元素在主窗口"（否则子窗口一开，主窗口元素全部失联）。
        followed = self._try_follow_child_window()
        if followed:
            try:
                new_root = _uia.ControlFromHandle(self._hwnd)
            except Exception as exc:
                logger.debug("[WinUIA] 兜底子窗口 ControlFromHandle 失败: %s", exc)
                new_root = None
            if new_root is not None:
                result = self._search_root(new_root, self._hwnd, locator)
                if result is not None:
                    return result

        if len(self._hwnd_stack) > 1:
            try:
                main_root = _uia.ControlFromHandle(self._hwnd_stack[0])
            except Exception as exc:
                logger.debug("[WinUIA] 主窗口 ControlFromHandle 失败: %s", exc)
                main_root = None
            if main_root is not None:
                result = self._search_root(main_root, self._hwnd_stack[0], locator)
                if result is not None:
                    return result

        if followed:
            self._pop_followed_child_window()
        return None

    # ══════════════════════════════════════════════════════════════════
    # 窗口后端（uiautomation 默认 / pywinauto 灰度）与窗口内查找
    # 见 docs/plans/20261119103000-win-window-hint-and-pywinauto-adapter.md
    # ══════════════════════════════════════════════════════════════════

    def _get_backend(self) -> WindowBackend:
        """装配并缓存窗口后端；pywinauto 请求但不可用时回退 uiautomation（记 warning）。"""
        backend = getattr(self, "_win_backend", None)
        if backend is not None:
            return backend
        requested = resolve_backend_name(getattr(self, "_app_name", ""))
        backend = None
        if requested == BACKEND_PYWINAUTO:
            backend = create_pywinauto_backend(
                _uia, self._has_child_text, self._has_sibling_text,
            )
            if backend is None:
                logger.warning("[WinUIA] 已请求 %s 后端但不可用，回退 %s",
                                BACKEND_PYWINAUTO, BACKEND_UIAUTOMATION)
            else:
                logger.info("[WinUIA] 窗口后端: %s（灰度）", BACKEND_PYWINAUTO)
        if backend is None:
            backend = UiaLibBackend(_uia, self._find_uia_control)
        self._win_backend = backend
        return backend

    def _search_root(self, root, hwnd: Optional[int], locator: ControlLocator) -> Optional[object]:
        """在 root 子树内按 locator 搜索。

        默认（uiautomation 后端，或 hwnd 为 None 的父容器子树）直接走既有
        `_find_uia_control`，调用参数与改造前逐字节一致；仅 pywinauto 后端且以窗口
        为根时委托后端，后端异常则当次回退 uiautomation（不静默：记 warning）。
        """
        backend = self._get_backend() if hwnd else None
        if backend is not None and backend.name == BACKEND_PYWINAUTO:
            try:
                return backend.find_control(hwnd, locator)
            except Exception as exc:
                logger.warning("[WinUIA] pywinauto 查找异常，本次回退 uiautomation: %s: %s",
                               type(exc).__name__, exc)
        return self._find_uia_control(
            root, locator.name, locator.automation_id, locator.control_type,
            class_name=locator.class_name, child_text=locator.child_text,
            sibling_text=locator.sibling_text, name_regex=locator.name_regex,
            max_depth=locator.max_depth,
        )

    def _find_in_window(self, hwnd: int, locator: ControlLocator,
                        parent_class_name: str = "", parent_child_text: str = "") -> Optional[object]:
        """限定在指定 hwnd 窗口内查找（window_hint 作用域内使用，无启发式兜底）。"""
        try:
            root = _uia.ControlFromHandle(hwnd)
        except Exception as exc:
            logger.debug("[WinUIA] 作用域窗口 ControlFromHandle(%s) 失败: %s", hwnd, exc)
            return None
        if root is None:
            return None
        root_hwnd: Optional[int] = hwnd
        if parent_class_name or parent_child_text:
            parent = self._find_uia_control(
                root, "", "", "", class_name=parent_class_name,
                child_text=parent_child_text, max_depth=20,
            )
            if parent is None:
                return None
            root, root_hwnd = parent, None
        return self._search_root(root, root_hwnd, locator)

    # ── window_hint 窗口作用域 ────────────────────────────────────────

    @staticmethod
    def _window_hint_strict() -> bool:
        """`UIA_WINDOW_HINT_STRICT=1`：hint 未命中任何窗口时直接判元素未找到（默认降级旧启发式）。"""
        raw = (os.environ.get("UIA_WINDOW_HINT_STRICT") or "").strip().lower()
        return raw in ("1", "true", "on", "yes")

    @staticmethod
    def _window_hint_wait_s() -> float:
        """hint 窗口未出现时的最长等待秒数（`UIA_WINDOW_HINT_WAIT_S`，默认 0 = 不等待，
        由 `wait[]` 等上层重试负责轮询）。"""
        try:
            return max(0.0, float(os.environ.get("UIA_WINDOW_HINT_WAIT_S") or 0))
        except ValueError:
            return 0.0

    @asynccontextmanager
    async def window_scope(self, hint: WindowHint):
        """把后续 `find_by_id` / `count_by_id` 限定在 hint 声明的窗口内。

        引擎专用能力（不进 EngineBase，R3）：Resolver 经 `getattr` 探测后使用，
        mac / web 引擎无此能力，调用路径不变。进入时在 STA 线程解析目标 hwnd；
        退出时清除作用域，其后的无 hint 元素行为与改造前一致。
        """
        await self._run_on_sta(self._sync_enter_window_scope, hint)
        try:
            yield
        finally:
            self._scope_state = None

    def _sync_enter_window_scope(self, hint: WindowHint) -> None:
        hwnd = self._resolve_hint_hwnd(hint)
        if hwnd is not None:
            self._scope_state = hwnd
            return
        if self._window_hint_strict():
            logger.warning("[WinUIA] window_hint 未命中任何窗口（严格模式，元素视为未找到）: %s",
                           hint.describe())
            self._scope_state = _SCOPE_MISS
        else:
            logger.warning("[WinUIA] window_hint 未命中任何窗口，降级为旧窗口启发式: %s",
                           hint.describe())
            self._scope_state = None

    def _reset_window_hint_state(self) -> None:
        """应用关闭 / 终止后清空 window_hint 作用域与 hint→hwnd 缓存（句柄已失效）。"""
        self._scope_state = None
        self._hint_cache().clear()

    def _hint_cache(self) -> dict:
        cache = getattr(self, "_hint_hwnd_cache", None)
        if cache is None:
            cache = {}
            self._hint_hwnd_cache = cache
        return cache

    def _app_pid_set(self) -> set:
        """被测应用进程树 pid 集合（hwnd 栈底所属进程及其子孙）。"""
        base = (self._hwnd_stack[0] if self._hwnd_stack else self._hwnd)
        root_pid = self._get_pid_from_hwnd(base) if base else None
        if not root_pid:
            return set()
        try:
            return {root_pid} | {p.pid for p in psutil.Process(root_pid).children(recursive=True)}
        except Exception:
            return {root_pid}

    def _resolve_hint_hwnd(self, hint: WindowHint) -> Optional[int]:
        """hint → hwnd：role=main 取栈底；其余先查运行时缓存，再枚举匹配，可选短等待。"""
        if hint.is_main:
            if not self._ensure_hwnd():
                return None
            return self._hwnd_stack[0] if self._hwnd_stack else self._hwnd

        cache = self._hint_cache()
        cached = cache.get(hint)
        if cached and self._is_hwnd_valid(cached) and self._is_window_visible(cached):
            return cached
        cache.pop(hint, None)

        deadline = time.monotonic() + self._window_hint_wait_s()
        while True:
            self._ensure_hwnd()  # 先 reconcile，保证栈与进程树基线最新
            hwnd = self._match_hint_hwnd(hint)
            if hwnd is not None:
                cache[hint] = hwnd
                self._adopt_hint_window(hwnd)
                return hwnd
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.2)

    @staticmethod
    def _is_window_visible(hwnd: int) -> bool:
        try:
            import ctypes
            return bool(ctypes.windll.user32.IsWindowVisible(hwnd))
        except Exception:
            return False

    def _match_hint_hwnd(self, hint: WindowHint) -> Optional[int]:
        try:
            windows = self._get_backend().enumerate_windows()
        except Exception as exc:
            logger.warning("[WinUIA] 窗口枚举失败（%s: %s）", type(exc).__name__, exc)
            return None
        if not hint.process:
            # 未指定进程名时只认被测应用进程树，避免命中无关应用的同名窗口
            pids = self._app_pid_set()
            windows = [w for w in windows if w.pid in pids]
        try:
            import ctypes
            own_pid = int(ctypes.windll.kernel32.GetCurrentProcessId())
        except Exception:
            own_pid = 0
        matched = match_hint(hint, windows, own_pid=own_pid)
        if matched is None:
            return None
        logger.info("[WinUIA] window_hint %s → hwnd=%s title=%r pid=%s",
                    hint.describe(), matched.hwnd, matched.title, matched.pid)
        return matched.hwnd

    def _adopt_hint_window(self, hwnd: int) -> None:
        """命中窗口属于应用进程树且不在栈内时并入 hwnd 栈（关闭后由 reconcile 自动弹栈）。"""
        if hwnd in self._hwnd_stack or not self._hwnd_stack:
            return
        pid = self._get_pid_from_hwnd(hwnd)
        if pid and pid in self._app_pid_set():
            self._hwnd_stack.append(hwnd)
            self._hwnd = hwnd
            logger.info("[WinUIA] window_hint 窗口并入栈 hwnd=%s (栈深=%d)", hwnd, len(self._hwnd_stack))

    @staticmethod
    def _parse_qpath_attr(element_id: str, attr: str) -> str:
        m = re.search(rf"@{attr}\s*=\s*'([^']*)'", element_id)
        if m:
            return m.group(1)
        m = re.search(rf'@{attr}\s*=\s*"([^"]*)"', element_id)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _parse_legacy_attr(element_id: str, attr: str) -> str:
        m = re.search(rf"(?:^|[/&\s]){attr}\s*=\s*(['\"])(.*?)\1", element_id)
        return m.group(2) if m else ""

    @staticmethod
    def _parse_legacy_regex_attr(element_id: str, attr: str) -> str:
        m = re.search(rf"(?:^|[/&\s]){attr}\s*~=\s*(['\"])(.*?)\1", element_id)
        return m.group(2) if m else ""

    @staticmethod
    def _has_child_text(ctrl, text: str, depth: int = 0, max_depth: int = 6) -> bool:
        """检查 ctrl 的子孙树中是否存在 Name == text 的节点。

        深度限制 6 层（示例桌面应用等 Qt 应用的卡片→子文本通常 1~3 层），避免大型
        UIA 树的全树扫描性能问题。见 docs/plans/20260716120000-win-uia-class-
        text-locator.md §3.3。
        """
        if depth > max_depth:
            return False
        try:
            if (ctrl.Name or "") in _split_candidates(text):
                return True
            for ch in ctrl.GetChildren():
                if WinUIAEngine._has_child_text(ch, text, depth + 1, max_depth):
                    return True
        except Exception:
            pass
        return False

    @staticmethod
    def _has_sibling_text(ctrl, text: str) -> bool:
        """检查 ctrl 在父节点 children 列表中**最近的**前一个或后一个具名兄弟
        节点，其 Name 是否等于 text。用于定位说明文字是同级兄弟节点（而非
        子孙节点）的控件，如设置中心页无 Name 的 QPushButton 开关，其说明
        文字与开关本身是同一父节点下的兄弟 QLabel。

        仅比较**最近邻**的具名兄弟：优先向前（backward）找最近的具名兄弟并
        直接判定匹配与否；只有 ctrl 是列表中第一个（backward 找不到任何具名
        兄弟）时才回退向后（forward）查找。不同时检查两个方向，避免
        [说明A, 开关A, 说明B, 开关B] 这类交替排列场景下，开关A 被误认为与
        说明B（属于开关B 的说明文字）配对。见 docs/plans/20260724190000-
        win-settings-center-atomic-elements.md §3.1。
        """
        try:
            parent = ctrl.GetParentControl()
            if parent is None:
                return False
            children = list(parent.GetChildren())
            idx = None
            for i, c in enumerate(children):
                if c is ctrl:
                    idx = i
                    break
            if idx is None:
                return False
            candidates = _split_candidates(text)
            # 向前找最近的具名兄弟：找到即直接判定，不再看更远的
            for j in range(idx - 1, -1, -1):
                try:
                    sib_name = children[j].Name or ""
                except Exception:
                    continue
                if sib_name:
                    return sib_name in candidates
            # backward 完全无具名兄弟（ctrl 是首个），回退向后找最近的具名兄弟
            for j in range(idx + 1, len(children)):
                try:
                    sib_name = children[j].Name or ""
                except Exception:
                    continue
                if sib_name:
                    return sib_name in candidates
        except Exception:
            pass
        return False

    @staticmethod
    def _find_uia_control(root, name: str, automation_id: str,
                          control_type: str, class_name: str = "",
                          child_text: str = "", sibling_text: str = "",
                          name_regex: str = "",
                          max_depth: int = 20) -> Optional[object]:
        """DFS 遍历 UIA 树，匹配 Name / AutomationId / ControlType / ClassName，
        并可选按子孙节点文本（ChildText）或同级兄弟节点文本（SiblingText）
        进一步筛选。

        2026-07-16 扩展 class_name/child_text 两个匹配维度（见
        docs/plans/archive/01-engine-lifecycle-实施纪要.md），用于定位
        无 Name/AutomationId 但有稳定 ClassName 的控件（如 Qt CardButton），
        ChildText 用于区分同 ClassName 的多个实例。

        2026-07-24 扩展 sibling_text 匹配维度（见 docs/plans/20260724190000-
        win-settings-center-atomic-elements.md），用于定位说明文字是
        同级兄弟节点（而非子孙节点）的控件，如设置中心页的开关按钮。
        """
        def _walk(ctrl, depth: int):
            if depth > max_depth:
                return None
            try:
                ctrl_name = ctrl.Name or ""
                ctrl_aid = ctrl.AutomationId or ""
                ctrl_type = ctrl.ControlTypeName or ""
                ctrl_class = ctrl.ClassName or ""
            except Exception:
                return None
            # 匹配逻辑：所有指定属性需同时满足（AND 语义）
            # name/class_name 支持 `|` 分隔多语言候选值（R22c），如
            # @Name='开始扫描|Start Scan'，按候选顺序任一命中即可。
            name_ok = (not name or ctrl_name in _split_candidates(name))
            if name_regex:
                try:
                    name_ok = bool(re.search(name_regex, ctrl_name))
                except re.error:
                    name_ok = False
            aid_ok = (not automation_id or ctrl_aid == automation_id)
            type_ok = (not control_type or ctrl_type == control_type)
            class_ok = (not class_name or ctrl_class in _split_candidates(class_name))
            # ChildText/SiblingText 检查代价较高（树扫描），放在其他条件短路求值之后
            base_ok = name_ok and aid_ok and type_ok and class_ok
            child_ok = (
                not child_text
                or (base_ok and WinUIAEngine._has_child_text(ctrl, child_text))
            )
            sibling_ok = (
                not sibling_text
                or (base_ok and WinUIAEngine._has_sibling_text(ctrl, sibling_text))
            )
            if base_ok and child_ok and sibling_ok:
                return ctrl
            try:
                children = ctrl.GetChildren()
            except Exception:
                return None
            for ch in children:
                r = _walk(ch, depth + 1)
                if r is not None:
                    return r
            return None
        try:
            return _walk(root, 0)
        except Exception:
            return None

    async def find_by_text(self, text: str) -> Optional[ElementRef]:
        loop = asyncio.get_event_loop()
        control = await loop.run_in_executor(
            self._get_uia_executor(), self._sync_find_by_text, text,
        )
        if control is None:
            return None
        return ElementRef(element_id=f"text={text}", raw=control, text=text)

    def _sync_find_by_text(self, text: str) -> Optional[object]:
        return self._sync_find_by_id(f"@Name='{text}'")

    async def find_by_role(self, role: str, name: str = "") -> Optional[ElementRef]:
        loop = asyncio.get_event_loop()
        control = await loop.run_in_executor(
            self._get_uia_executor(), self._sync_find_by_role, role, name,
        )
        if control is None:
            return None
        try:
            ctrl_name = control.Name or ""
        except Exception:
            ctrl_name = ""
        eid = f"@ControlType='{role}'" + (f"@Name='{name}'" if name else "")
        return ElementRef(element_id=eid, raw=control, text=ctrl_name)

    def _sync_find_by_role(self, role: str, name: str = "") -> Optional[object]:
        ctrl_type = _CONTROL_TYPE_MAP.get(role.lower(), role)
        hwnd = self._ensure_hwnd()
        if not hwnd:
            return None
        try:
            root = _uia.ControlFromHandle(hwnd)
        except Exception:
            return None
        return self._find_uia_control(root, name, "", ctrl_type)

    async def find_all_by_role(self, role: str) -> list[ElementRef]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_find_all_by_role, role,
        )

    def _sync_find_all_by_role(self, role: str) -> list[ElementRef]:
        ctrl_type = _CONTROL_TYPE_MAP.get(role.lower(), role)
        hwnd = self._ensure_hwnd()
        if not hwnd:
            return []
        try:
            root = _uia.ControlFromHandle(hwnd)
        except Exception:
            return []
        results = []
        def _walk(ctrl, depth: int):
            if depth > 20:
                return
            try:
                if ctrl.ControlTypeName == ctrl_type:
                    name = ctrl.Name or ""
                    results.append(ElementRef(
                        element_id=f"role={role}[{len(results)}]", raw=ctrl, text=name,
                    ))
            except Exception:
                pass
            try:
                for ch in ctrl.GetChildren():
                    _walk(ch, depth + 1)
            except Exception:
                pass
        try:
            _walk(root, 0)
        except Exception:
            pass
        return results

    # ── 可访问性树摘要（L3.5 LLM_SELECTOR 使用） ─────────────────────

    async def get_accessibility_summary(self, max_nodes: int = 200) -> Optional[str]:
        """返回当前窗口 UIA 控件树摘要（JSON 字符串），供 L3.5 LLM_SELECTOR
        推断 selector 使用。见 docs/plans/archive/01-engine-lifecycle-实施纪要.md。
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_get_accessibility_summary, max_nodes,
        )

    def _sync_get_accessibility_summary(self, max_nodes: int) -> Optional[str]:
        import json
        hwnd = self._ensure_hwnd()
        if not hwnd:
            return None
        try:
            root = _uia.ControlFromHandle(hwnd)
        except Exception:
            return None
        nodes: list[dict] = []

        def _walk(ctrl, depth: int):
            if len(nodes) >= max_nodes or depth > 15:
                return
            try:
                if getattr(ctrl, "IsOffscreen", False):
                    return  # 跳过不可见节点及其子树
                entry: dict = {"role": ctrl.ControlTypeName}
                name = (ctrl.Name or "").strip()
                if name:
                    entry["name"] = name[:60]
                aid = getattr(ctrl, "AutomationId", "") or ""
                if aid:
                    entry["automation_id"] = aid
                cls = getattr(ctrl, "ClassName", "") or ""
                if cls:
                    entry["class_name"] = cls
                nodes.append(entry)
            except Exception:
                return
            try:
                for ch in ctrl.GetChildren():
                    if len(nodes) >= max_nodes:
                        return
                    _walk(ch, depth + 1)
            except Exception:
                pass

        try:
            _walk(root, 0)
        except Exception:
            return None
        if not nodes:
            return None
        return json.dumps(nodes, ensure_ascii=False)

    # ══════════════════════════════════════════════════════════════════
    # 操作
    # ══════════════════════════════════════════════════════════════════

    async def click(self, element: ElementRef) -> None:
        """双路径点击：L1 InvokePattern.Invoke()（原生动作，不依赖窗口前台/
        坐标系，与 MacAppKitEngine 的 Press() 优先语义对齐）→ L2 坐标点击兜底
        （2026-07-13 调整优先级，见 compat-fix 计划 §3.5）。

        qt 通道句柄（`qt:<pid>:<objId>`）走探针对象级点击（PostMessage，
        不抢真实鼠标、与窗口位置无关），其余逐字节沿用原逻辑。
        """
        qt = self._qt_client_of(element)
        if qt is not None:
            await qt[0].click(qt[1])
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._get_uia_executor(), self._sync_click, element)

    async def select(self, element: ElementRef) -> None:
        """执行 UIA SelectionItem.Select，兼容 legacy_runner click mode=select。"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._get_uia_executor(), self._sync_select, element)

    def _sync_select(self, element: ElementRef) -> None:
        control = element.raw
        if control is None:
            raise RuntimeError("UIA select: element.raw 为空")
        try:
            # RadioButtonControl 不直接暴露 Select()；SelectionItemPattern
            # 才是 UIA 标准原生选中入口。优先走 Pattern，避免超宽控件的
            # 坐标中心落到右侧空白。
            get_pattern = getattr(control, "GetSelectionItemPattern", None)
            if callable(get_pattern):
                pattern = get_pattern()
                if pattern is not None:
                    pattern.Select()
                    return
            control.Select()
        except Exception as exc:
            raise RuntimeError(f"UIA SelectionItem.Select 失败: {exc}") from exc

    def _sync_click(self, element: ElementRef) -> None:
        control = element.raw
        if control is None:
            return
        # 点击前尽力置前台（best-effort，纯可见性增强，不影响下方判定逻辑，见
        # docs/plans/archive/01-engine-lifecycle-实施纪要.md）
        hwnd = self._get_hwnd()
        if hwnd:
            self._bring_to_foreground(hwnd)
        # L1: InvokePattern（原生动作，最稳定；非 Button 语义控件无此 Pattern
        # 会抛异常，安全降级到坐标点击）
        try:
            control.Invoke()
            return
        except Exception:
            pass
        # L2: 坐标点击兜底
        try:
            rect = control.BoundingRectangle
            if rect and HAS_PYAUTOGUI:
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                pyautogui.click(cx, cy)
        except Exception:
            pass

    async def hover(self, element: ElementRef) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._get_uia_executor(), self._sync_hover, element)

    def _sync_hover(self, element: ElementRef) -> None:
        control = element.raw
        if control is None:
            return
        try:
            rect = control.BoundingRectangle
            if rect and HAS_PYAUTOGUI:
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                pyautogui.moveTo(cx, cy)
        except Exception:
            pass

    # ── 屏幕坐标驱动（device-exec 多模态定位用；R3：引擎专用能力放具体类）──
    async def click_at_screen_coords(self, x: float, y: float) -> None:
        """在**屏幕绝对坐标**（物理像素）点击；坐标由调用方按实测 scale 换算。"""
        await self._run_on_sta(self._sync_move_at_screen_coords, float(x), float(y))
        await self._run_on_sta(self._sync_click_at_screen_coords, float(x), float(y))

    async def hover_at_screen_coords(self, x: float, y: float) -> None:
        await self._run_on_sta(self._sync_move_at_screen_coords, float(x), float(y))

    async def type_text_at_screen_coords(self, x: float, y: float, text: str) -> None:
        """先在该坐标点击聚焦，再逐字符 SendInput 注入（不依赖剪贴板/输入法）。"""
        await self.click_at_screen_coords(x, y)
        for ch in text:
            await self._run_on_sta(self._input_char, ch)

    @staticmethod
    def _sync_move_at_screen_coords(x: float, y: float) -> None:
        try:
            import ctypes
            ctypes.windll.user32.SetCursorPos(int(x), int(y))
        except Exception as exc:  # noqa: BLE001 - 非 Windows/无权限时只留痕
            logger.debug("[WinUIA] 屏幕坐标移动失败: %s", exc)

    @staticmethod
    def _sync_click_at_screen_coords(x: float, y: float, hold_ms: int = 60) -> None:
        try:
            import ctypes
            import time as _time
            user32 = ctypes.windll.user32
            user32.SetCursorPos(int(x), int(y))
            _time.sleep(0.03)
            user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
            _time.sleep(hold_ms / 1000.0)
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
        except Exception as exc:  # noqa: BLE001
            logger.debug("[WinUIA] 屏幕坐标点击失败: %s", exc)

    async def type_text(self, element: ElementRef, text: str, clear: bool = True) -> None:
        """Win32 SendInput Unicode 注入 + 前置点击聚焦。
        不依赖剪贴板/输入法，支持中文/emoji。

        qt 通道句柄：聚焦改用探针对象级点击（避免 UIA 对无 raw 句柄的点击路径），
        文本注入仍走 SendInput（与聚焦方式无关，且不依赖探针侧输入原语）。
        """
        qt = self._qt_client_of(element)
        if qt is not None:
            await qt[0].click(qt[1])
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                self._get_uia_executor(), self._sync_type_after_focus, text, clear,
            )
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            self._get_uia_executor(), self._sync_type_text, element, text, clear,
        )

    def _sync_type_after_focus(self, text: str, clear: bool = True) -> None:
        """焦点已就绪后的纯文本注入（qt 通道复用，不再重复点击）。"""
        if clear:
            self._sync_press_key("ctrl+a")
            self._sync_press_key("backspace")
        for ch in text:
            self._input_char(ch)

    def _sync_type_text(self, element: ElementRef, text: str, clear: bool = True) -> None:
        # 1. 聚焦点击
        self._sync_click(element)
        # 2. 清除已有文本（Ctrl+A + Backspace）
        if clear:
            self._sync_press_key("ctrl+a")
            self._sync_press_key("backspace")
        # 3. 逐字符 SendInput Unicode 注入
        for ch in text:
            self._input_char(ch)

    async def type_focused(
        self,
        text: str,
        *,
        via_clipboard: bool = False,
        preserve_focus: bool = False,
    ) -> None:
        """Input text into the currently focused native control."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            self._get_uia_executor(),
            self._sync_type_focused,
            text,
            via_clipboard,
            preserve_focus,
        )

    def _sync_type_focused(
        self,
        text: str,
        via_clipboard: bool = False,
        preserve_focus: bool = False,
    ) -> None:
        if preserve_focus:
            self._sync_press_key("ctrl+a")
            time.sleep(0.2)
            self._sync_press_key("backspace")
            time.sleep(0.2)
            if via_clipboard:
                import pyperclip

                pyperclip.copy(text)
                time.sleep(0.1)
                self._sync_press_key("ctrl+v")
                time.sleep(0.2)
                return
            for ch in text:
                self._input_char(ch)
            return

        hwnd = self._get_hwnd()
        target_already_foreground = False
        input_already_focused = False
        focused_input = None
        try:
            import ctypes
            from ctypes import wintypes
            import win32gui

            foreground = ctypes.windll.user32.GetForegroundWindow()
            foreground_title = win32gui.GetWindowText(foreground) if foreground else ""
            expected_title = (self._window_handle_name or "").strip()
            target_already_foreground = bool(
                expected_title
                and expected_title.casefold() in foreground_title.casefold()
            )

            # Dynamic popup registrations can retain a stale child/owner HWND.
            # If UIA confirms an Edit already owns keyboard focus, keep that
            # focus instead of activating the cached HWND and stealing it.
            if HAS_UIA and _uia is not None:
                for _ in range(5):
                    focused = _uia.GetFocusedControl()
                    input_already_focused = bool(
                        focused
                        and focused.ControlTypeName == "EditControl"
                        and focused.HasKeyboardFocus
                    )
                    if input_already_focused:
                        focused_input = focused
                        break
                    point = wintypes.POINT()
                    if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
                        pointed = _uia.ControlFromPoint(point.x, point.y)
                        input_already_focused = bool(
                            pointed
                            and pointed.ControlTypeName == "EditControl"
                            and pointed.HasKeyboardFocus
                        )
                        if input_already_focused:
                            focused_input = pointed
                            break
                    time.sleep(0.1)
        except Exception:
            pass
        if (
            hwnd
            and not self._is_foreground(hwnd)
            and not target_already_foreground
            and not input_already_focused
        ):
            self._bring_to_foreground(hwnd)
            time.sleep(0.1)
        if focused_input is not None:
            try:
                value_pattern = focused_input.GetValuePattern()
                if value_pattern is not None and not value_pattern.IsReadOnly:
                    value_pattern.SetValue("")
                    if text:
                        value_pattern.SetValue(text)
                    return
            except Exception as exc:
                logger.debug(
                    "[WinUIA] type_focused ValuePattern 写入失败，回退键盘输入: %s",
                    exc,
                )
            try:
                focused_input.SetFocus()
                time.sleep(0.2)
            except Exception:
                pass
        self._sync_press_key("ctrl+a")
        time.sleep(0.2)
        self._sync_press_key("backspace")
        time.sleep(0.2)
        if not text:
            return

        if via_clipboard:
            import pyperclip

            pyperclip.copy(text)
            time.sleep(0.1)
            self._sync_press_key("ctrl+v")
            time.sleep(0.2)
            return

        for ch in text:
            self._input_char(ch)

    @staticmethod
    def _is_foreground(hwnd: int) -> bool:
        import ctypes

        return bool(ctypes.windll.user32.GetForegroundWindow() == hwnd)

    @staticmethod
    def _input_char(ch: str) -> None:
        """Win32 SendInput — KEYEVENTF_UNICODE 注入单个 UTF-16 码元。"""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32

            class KEYBDINPUT(ctypes.Structure):
                _fields_ = [
                    ("wVk", wintypes.WORD),
                    ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
                ]

            class MOUSEINPUT(ctypes.Structure):
                _fields_ = [
                    ("dx", wintypes.LONG),
                    ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
                ]

            class HARDWAREINPUT(ctypes.Structure):
                _fields_ = [
                    ("uMsg", wintypes.DWORD),
                    ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD),
                ]

            class INPUT_UNION(ctypes.Union):
                _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

            class INPUT(ctypes.Structure):
                _fields_ = [("type", wintypes.DWORD), ("u", INPUT_UNION)]

            KEYEVENTF_UNICODE = 0x0004
            KEYEVENTF_KEYUP = 0x0002
            INPUT_KEYBOARD = 1
            send_input = user32.SendInput
            send_input.argtypes = [
                wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int,
            ]
            send_input.restype = wintypes.UINT

            code_units = ch.encode("utf-16-le")
            for index in range(0, len(code_units), 2):
                code_unit = int.from_bytes(code_units[index:index + 2], "little")
                down = INPUT()
                down.type = INPUT_KEYBOARD
                down.u.ki = KEYBDINPUT(
                    0, code_unit, KEYEVENTF_UNICODE, 0, None,
                )
                up = INPUT()
                up.type = INPUT_KEYBOARD
                up.u.ki = KEYBDINPUT(
                    0, code_unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None,
                )
                inputs = (INPUT * 2)(down, up)
                sent = send_input(
                    2, ctypes.cast(inputs, ctypes.POINTER(INPUT)), ctypes.sizeof(INPUT),
                )
                if sent != 2:
                    logger.warning(
                        "[WinUIA] SendInput failed: code_unit=0x%04X sent=%s",
                        code_unit, sent,
                    )
        except Exception:
            # 兜底：pyautogui typewrite（依赖剪贴板）
            if HAS_PYAUTOGUI:
                pyautogui.typewrite(ch)

    async def press_key(self, key: str) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._get_uia_executor(), self._sync_press_key, key)

    def _sync_press_key(self, key: str) -> None:
        """Win32 keybd_event + pyautogui 兜底。"""
        key_lower = key.lower().strip()
        # 组合键（如 "alt+f4"）：修饰键必须真正"按住"（keydown 不立即
        # keyup），期间按下主键，再逆序释放修饰键，否则系统无法识别为
        # 组合快捷键（例如 Alt+F4 关闭窗口要求 Alt 处于按下状态时触发 F4）。
        if "+" in key_lower:
            parts = [p.strip() for p in key_lower.split("+")]
            modifiers = [p for p in parts if p in _MODIFIER_VKS]
            main_keys = [p for p in parts if p not in _MODIFIER_VKS]
            try:
                import ctypes
                user32 = ctypes.windll.user32
                for m in modifiers:
                    user32.keybd_event(_MODIFIER_VKS[m], 0, 0, 0)      # key down，按住
                for mk in main_keys:
                    self._sync_press_key(mk)                            # 主键正常按下弹起
                for m in reversed(modifiers):
                    user32.keybd_event(_MODIFIER_VKS[m], 0, 0x0002, 0)  # key up，逆序释放
                return
            except Exception:
                pass
            # ctypes 不可用等异常兜底：退回逐键顺序发送（不阻断执行）
            for p in parts:
                self._sync_press_key(p)
            return
        vk = _VK_MAP.get(key_lower)
        if vk is not None:
            try:
                import ctypes
                user32 = ctypes.windll.user32
                user32.keybd_event(vk, 0, 0, 0)       # key down
                user32.keybd_event(vk, 0, 0x0002, 0)   # key up
                return
            except Exception:
                pass
        # pyautogui 兜底
        if HAS_PYAUTOGUI:
            pyautogui.press(key_lower)

    async def scroll(self, element: Optional[ElementRef], direction: str, amount: int) -> None:
        """element 可为 None（scroll[direction] 无元素名调用，滑动当前窗口，
        见 docs/plans/archive/02-dsl-actions-实施纪要.md）。"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            self._get_uia_executor(), self._sync_scroll, element, direction, amount,
        )

    def _sync_scroll(self, element: Optional[ElementRef], direction: str, amount: int) -> None:
        control = element.raw if element is not None else None
        if control is not None:
            try:
                pattern = control.GetScrollPattern()
                if pattern is not None:
                    import uiautomation as auto
                    if direction.lower() in ("up", "down"):
                        pattern.Scroll(auto.ScrollAmount.NoAmount,
                                       auto.ScrollAmount.LargeIncrement if direction.lower() == "down"
                                       else auto.ScrollAmount.LargeDecrement)
                    elif direction.lower() in ("left", "right"):
                        pattern.Scroll(auto.ScrollAmount.LargeIncrement if direction.lower() == "right"
                                       else auto.ScrollAmount.LargeDecrement,
                                       auto.ScrollAmount.NoAmount)
                    return
            except Exception:
                pass
        # 兜底：ScrollPattern 不可用（如 Qt 自绘控件，无 UIA 滚动语义，真机验证
        # 见 docs/plans/archive/03-resolver-rosetta-实施纪要.md）
        # → 合成 Win32 鼠标滚轮事件。
        self._sync_scroll_wheel_fallback(element, direction, amount)

    def _sync_scroll_wheel_fallback(self, element: Optional[ElementRef], direction: str, amount: int) -> None:
        """向元素（或窗口，元素坐标不可用时）中心坐标发送 Win32 鼠标滚轮事件。

        `amount` 为像素语义（与 action_scroll.py / MacAppKitEngine 对齐）。
        Win32 滚轮最小单位是"刻度"（WHEEL_DELTA=120），单刻度实际滚动像素由
        系统"鼠标滚轮行数"设置决定、不透明，此处用经验值
        _WHEEL_PIXELS_PER_TICK 粗略换算为刻度数（真机验证：约 900px 窗口高度
        的 2/3≈600px 时使用 6~8 刻度可产生有效可感知滚动，见
        docs/plans/archive/03-resolver-rosetta-实施纪要.md）。
        """
        import ctypes
        cx, cy = self._resolve_scroll_center(element)
        if cx is None:
            return
        ticks = max(1, round(amount / self._WHEEL_PIXELS_PER_TICK))
        try:
            ctypes.windll.user32.SetCursorPos(int(cx), int(cy))
            wheel_delta = 120 * ticks  # WHEEL_DELTA = 120，一个滚轮刻度
            direction_lower = direction.lower()
            if direction_lower == "up":
                ctypes.windll.user32.mouse_event(0x0800, 0, 0, wheel_delta, 0)  # MOUSEEVENTF_WHEEL
            elif direction_lower == "down":
                ctypes.windll.user32.mouse_event(0x0800, 0, 0, -wheel_delta, 0)
            elif direction_lower in ("left", "right"):
                h_delta = wheel_delta if direction_lower == "right" else -wheel_delta
                ctypes.windll.user32.mouse_event(0x01000, 0, 0, h_delta, 0)  # MOUSEEVENTF_HWHEEL
        except Exception as exc:
            logger.debug("[WinUIA] 鼠标滚轮兜底滚动失败: %s", exc)

    def _resolve_scroll_center(self, element: Optional[ElementRef]) -> tuple:
        """优先使用已解析元素的有效矩形中心，无元素或矩形不可测时回退窗口中心。"""
        import math

        control = element.raw if element is not None else None
        if control is not None:
            try:
                r = control.BoundingRectangle
                values = (r.left, r.top, r.right, r.bottom)
                if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in values) and (
                    r.right > r.left and r.bottom > r.top
                ):
                    return ((r.left + r.right) / 2, (r.top + r.bottom) / 2)
            except Exception:
                pass
        hwnd = self._get_hwnd()
        if hwnd:
            try:
                import win32gui
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                if right > left and bottom > top:
                    return ((left + right) / 2, (top + bottom) / 2)
            except Exception:
                pass
        return (None, None)

    # ══════════════════════════════════════════════════════════════════
    # 断言辅助
    # ══════════════════════════════════════════════════════════════════

    async def get_text(self, element: ElementRef) -> str:
        qt = self._qt_handle_of(element)
        if qt is not None:
            props = await self._qt_property(qt[1])
            for key in ("text", "Text", "Name", "name", "value"):
                value = props.get(key)
                if value is not None:
                    return str(value)
            return getattr(element, "text", "") or ""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_get_text, element,
        )

    def _sync_get_text(self, element: ElementRef) -> str:
        control = element.raw
        if control is None:
            return ""
        try:
            return control.Name or ""
        except Exception:
            return ""

    async def get_attribute(self, element: ElementRef, attr: str) -> Optional[str]:
        qt = self._qt_client_of(element)
        if qt is not None:
            props = await self._qt_property(qt[1], qt[0])
            value = props.get(attr)
            return None if value is None else str(value)
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_get_attribute, element, attr,
        )

    def _sync_get_attribute(self, element: ElementRef, attr: str) -> Optional[str]:
        control = element.raw
        if control is None:
            return None
        try:
            return control.GetPropertyValue(attr)
        except Exception:
            return None

    async def is_visible(self, element: ElementRef) -> bool:
        """        qt 通道句柄：**不读探针的 visible 字段**（QtProbe 侧实测该字段与
        children 输出偶有矛盾），改用「objId 存在 + bounds 与窗口矩形相交」
        判定，与 UIA 侧语义对齐；几何不可得时按存在性放行（find 已保证存在）。

        仅对**主进程**句柄做窗口矩形相交判定：`get_window_rect()` 描述的是本引擎
        主窗口，与伴生进程（如轻页面 HUD）元素的屏幕坐标不同源，强行相交会恒判
        不可见；伴生进程元素按存在性放行。
        """
        qt = self._qt_client_of(element)
        if qt is not None:
            client, obj_id = qt
            rect = await client.bounds(obj_id)
            if rect is None:
                return True
            if client is not self._qt_probe:
                return True
            try:
                window = await self.get_window_rect()
            except Exception:
                window = None
            if not window or len(window) < 4:
                return True
            left = max(rect[0], window[0])
            top = max(rect[1], window[1])
            right = min(rect[0] + rect[2], window[0] + window[2])
            bottom = min(rect[1] + rect[3], window[1] + window[3])
            return right > left and bottom > top
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_is_visible, element,
        )

    def _sync_is_visible(self, element: ElementRef) -> bool:
        import math

        control = element.raw
        if control is None:
            return False
        try:
            if control.IsOffscreen:
                return False
        except Exception:
            return False
        try:
            rect = self._sync_get_element_rect(element)
            window = self._sync_get_window_rect()
            measurable = all(
                isinstance(r, (tuple, list)) and len(r) == 4
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in r)
                and r[2] > 0 and r[3] > 0
                for r in (rect, window)
            )
            if measurable:
                return self._sync_intersects_scroll_viewports(control, rect, window)
        except Exception:
            pass
        return True

    @staticmethod
    def _sync_intersects_scroll_viewports(control, rect: tuple, window: tuple) -> bool:
        import math

        left, top = max(rect[0], window[0]), max(rect[1], window[1])
        right = min(rect[0] + rect[2], window[0] + window[2])
        bottom = min(rect[1] + rect[3], window[1] + window[3])
        if right <= left or bottom <= top:
            return False
        seen: dict[int, object] = {}
        for _ in range(64):
            if id(control) in seen:
                return False
            seen[id(control)] = control
            try:
                get_parent = getattr(control, "GetParentControl", None)
                if not callable(get_parent):
                    return True
                parent = get_parent()
                if parent is None:
                    return True
                automation_id = getattr(parent, "AutomationId", "")
                if isinstance(automation_id, str) and automation_id.endswith(".qt_scrollarea_viewport"):
                    bounds = parent.BoundingRectangle
                    values = (bounds.left, bounds.top, bounds.right, bounds.bottom)
                    if not all(
                        isinstance(value, (int, float)) and not isinstance(value, bool)
                        and math.isfinite(value) for value in values
                    ) or bounds.right <= bounds.left or bounds.bottom <= bounds.top:
                        return False
                    left, top = max(left, bounds.left), max(top, bounds.top)
                    right, bottom = min(right, bounds.right), min(bottom, bounds.bottom)
                    if right <= left or bottom <= top:
                        return False
            except Exception:
                return False
            control = parent
        return False

    async def get_window_title(self) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_get_window_title,
        )

    def _sync_get_window_title(self) -> str:
        try:
            hwnd = self._get_hwnd()
            if hwnd:
                import ctypes
                buf = ctypes.create_unicode_buffer(256)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
                return buf.value or ""
        except Exception:
            pass
        return ""

    # ══════════════════════════════════════════════════════════════════
    # 内部辅助
    # ══════════════════════════════════════════════════════════════════

    def _ensure_hwnd(self) -> Optional[int]:
        """确保有有效的窗口 HWND（校验缓存有效性，失效则重新查找；含窗口栈
        自动跟随，见 docs/plans/archive/01-engine-lifecycle-实施纪要.md）。

        2026-07-13 修复：原实现缓存后永不刷新，窗口重建（如软复位产生新顶层
        窗口）后旧 hwnd 失效仍被复用，导致后续查找/点击静默失败或作用于错误
        窗口。改为每次先用 `IsWindow` 校验缓存，失效才重新查找（见
        docs/plans/archive/01-engine-lifecycle-实施纪要.md §3.3）。

        栈已初始化（首次 launch 成功找到主窗口后）时，优先走
        `_reconcile_hwnd_stack()`：检测弹窗打开自动压栈切换、弹窗关闭自动
        弹栈回退。栈为空（未 launch/异常路径）时完全走原有单 hwnd 逻辑，
        零回归。
        """
        if self._hwnd_stack:
            self._reconcile_hwnd_stack()
            if self._hwnd is not None:
                return self._hwnd
        if self._hwnd is not None:
            if self._is_hwnd_valid(self._hwnd):
                return self._hwnd
            logger.debug("[WinUIA] 缓存 hwnd=%s 失效，重新查找", self._hwnd)
            self._hwnd = None
        hwnd = self._find_main_hwnd_by_title()
        if hwnd:
            self._hwnd = hwnd
            if not self._hwnd_stack:
                self._hwnd_stack = [hwnd]
            return hwnd
        return None

    def _find_main_hwnd_by_title(self) -> Optional[int]:
        """按窗口标题查找主窗口 hwnd（纯只读查询，不修改任何实例状态）。

        精确匹配优先（`FindWindowW`）；未命中则枚举所有可见顶层窗口按标题
        子串匹配（跳过隐藏窗口，避免误匹配"AI助手"/"全局搜"等标题含主窗口
        名子串但不可见的工具条/弹窗子窗口，见 docs/plans/
        archive/03-resolver-rosetta-实施纪要.md），多个命中
        时优先选窗口面积最大的一个（主窗口通常明显更大，辅助面板/设置窗口
        通常较小，见 docs/plans/archive/01-engine-lifecycle-实施纪要.md）。

        从 `_ensure_hwnd()` 抽取（2026-07-31），供其与 `_reconcile_hwnd_stack()`
        的应用自重启兜底共同复用，见 docs/plans/20260731110000-engine-auto-
        reconnect-after-app-self-restart.md。
        """
        try:
            import ctypes
            hwnd = ctypes.windll.user32.FindWindowW(
                self._window_class_name or None,
                self._window_handle_name or None,
            )
            if hwnd:
                return hwnd
            # fallback：枚举所有顶层窗口（仅考虑可见窗口，避免误匹配隐藏的
            # 工具条/弹窗子窗口——如示例桌面应用的"AI助手"/"全局搜"/"新功能提示"
            # 等隐藏窗口标题均包含主窗口名子串，但 IsWindowVisible=False，见
            # docs/plans/archive/03-resolver-rosetta-实施纪要.md）
            titles = {}
            visible_top_level_hwnds = []
            def _enum(hwnd, _):
                if not ctypes.windll.user32.IsWindowVisible(hwnd):
                    return True  # 跳过不可见窗口，继续枚举
                visible_top_level_hwnds.append(hwnd)
                if self._window_class_name:
                    class_buf = ctypes.create_unicode_buffer(256)
                    ctypes.windll.user32.GetClassNameW(hwnd, class_buf, 256)
                    if class_buf.value != self._window_class_name:
                        return True

                buf = ctypes.create_unicode_buffer(256)
                ctypes.windll.user32.GetWindowTextW(hwnd, buf, 256)
                t = buf.value
                if t:
                    titles[t] = hwnd
                return True
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
            ctypes.windll.user32.EnumWindows(WNDENUMPROC(_enum), 0)
            # 多个可见窗口子串命中时，优先选窗口面积最大的一个（主窗口通常
            # 明显更大，辅助面板/设置窗口通常较小）
            candidates = [
                (title, h) for title, h in titles.items()
                if self._window_handle_name.lower() in title.lower()
            ]
            if candidates:
                def _window_area(hwnd) -> int:
                    try:
                        import win32gui
                        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                        return (right - left) * (bottom - top)
                    except Exception:
                        return 0  # GetWindowRect 失败 → 面积视为 0，排最后
                candidates.sort(key=lambda x: _window_area(x[1]), reverse=True)
                return candidates[0][1]
        except Exception as exc:
            logger.debug("[WinUIA] _find_main_hwnd_by_title 失败: %s", exc)
        return None

    def _reconcile_hwnd_stack(self) -> None:
        """hwnd 栈 reconcile：弹窗打开自动压栈，弹窗关闭自动弹栈回退。

        见 docs/plans/archive/01-engine-lifecycle-实施纪要.md。

        已知限制：新窗口检测判据为"与栈底主窗口同 PID 的可见顶层窗口"，若
        目标 App 弹窗与主窗口非同进程（多进程架构），不会被自动纳入栈，
        需真机验证后视情况补充判据。
        """
        # 1. 回退检测：栈顶 hwnd 若已失效，逐层弹出（保留栈底主窗口）
        while len(self._hwnd_stack) > 1 and not self._is_hwnd_valid(self._hwnd_stack[-1]):
            closed = self._hwnd_stack.pop()
            logger.info(
                "[WinUIA] 检测到窗口关闭 hwnd=%s，回退到上一窗口 (剩余栈深=%d)",
                closed, len(self._hwnd_stack),
            )

        if not self._hwnd_stack:
            self._hwnd = None
            return

        # 1.5 应用自重启兜底（2026-07-31，见 docs/plans/20260731110000-engine-
        #     auto-reconnect-after-app-self-restart.md）：tcs 用例执行期间
        #     被测 app 可能不经过 restart()/launch() 自行重启（如切换语言
        #     触发的自重启），新进程会创建全新 hwnd，栈底（主窗口）旧 hwnd
        #     已被系统销毁——但上面第 1 步的回退检测永远不会校验栈底
        #     （`len() > 1` 条件天然保护栈底），若不在此额外校验，旧栈底会
        #     被永久当作"有效"直接返回，后续所有查找均作用于已销毁句柄。
        #     此时按标题重新定位一次；命中则整栈重置为新主窗口（旧栈内其余
        #     层必然也已随进程退出失效，一并丢弃）；未命中（app 可能仍在
        #     重启过程中）则 `_hwnd=None`，保持栈不变，交给下次调用重试。
        if not self._is_hwnd_valid(self._hwnd_stack[0]):
            new_hwnd = self._find_main_hwnd_by_title()
            if new_hwnd is None:
                self._hwnd = None
                return
            logger.info(
                "[WinUIA] 主窗口 hwnd=%s 已失效（疑似应用自重启），重新定位为 hwnd=%s",
                self._hwnd_stack[0], new_hwnd,
            )
            self._hwnd_stack = [new_hwnd]

        # 2. 新窗口检测：找一个与栈底（主窗口）同 PID、不在栈内的可见顶层窗口
        main_pid = self._get_pid_from_hwnd(self._hwnd_stack[0])
        if main_pid is not None:
            candidate = self._find_new_owned_window(main_pid, set(self._hwnd_stack))
            if candidate is not None:
                self._hwnd_stack.append(candidate)
                logger.info(
                    "[WinUIA] 检测到新窗口 hwnd=%s，自动切换 (栈深=%d)",
                    candidate, len(self._hwnd_stack),
                )

        self._hwnd = self._hwnd_stack[-1]

    @staticmethod
    def _find_new_owned_window(main_pid: int, known_hwnds: set) -> Optional[int]:
        """枚举可见顶层窗口，找与 main_pid 同进程、且不在 known_hwnds 内的窗口。"""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            found: list[int] = []

            def _enum(hwnd, _):
                if hwnd in known_hwnds or not user32.IsWindowVisible(hwnd):
                    return True
                # Delphi exposes a visible zero-sized TApplication helper.
                # It is not interactive; never follow it as a popup window.
                from ctypes import wintypes
                rect = wintypes.RECT()
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return True
                if rect.right <= rect.left or rect.bottom <= rect.top:
                    return True
                pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value == main_pid:
                    found.append(hwnd)
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
            user32.EnumWindows(WNDENUMPROC(_enum), 0)
            return found[0] if found else None
        except Exception as exc:
            logger.debug("[WinUIA] _find_new_owned_window 失败: %s", exc)
            return None

    @staticmethod
    def _find_window_by_pid_tree(root_pid: int, known_hwnds: set) -> Optional[int]:
        """枚举可见顶层窗口，找属于 root_pid 或其任意子孙进程（psutil 进程树）、
        且不在 known_hwnds 内的窗口。

        区别于 `_find_new_owned_window()`（只精确匹配单一确切 PID，用于 hwnd 栈
        自动跟随场景）：某些 exe（尤其安装包/自解压程序）会先拉起一个
        launcher/stub 进程，再由它 spawn 出真正持有可见窗口的子进程（不同
        PID），单 PID 精确匹配会漏判这种场景（真机验证：cpu-z_2.18-cn.exe
        本身无窗口，其子进程 cpu-z_2.18-cn.tmp.exe 才持有"安装向导"窗口）。

        供 `_sync_launch()` 在标题匹配（`_ensure_hwnd()`）失败后作为第二级
        兜底使用（比第三级的全局可见窗口 diff 更精确：只认定 root_pid 进程树
        名下的窗口，避免误判到系统通知等不相关新窗口）。见
        docs/plans/archive/01-engine-lifecycle-实施纪要.md §10.2。
        """
        try:
            import ctypes
            try:
                root_proc = psutil.Process(root_pid)
                pid_set = {root_pid} | {p.pid for p in root_proc.children(recursive=True)}
            except psutil.NoSuchProcess:
                pid_set = {root_pid}

            user32 = ctypes.windll.user32
            found: list[int] = []

            def _enum(hwnd, _):
                if hwnd in known_hwnds or not user32.IsWindowVisible(hwnd):
                    return True
                pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value in pid_set:
                    found.append(hwnd)
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
            user32.EnumWindows(WNDENUMPROC(_enum), 0)
            return found[0] if found else None
        except Exception as exc:
            logger.debug("[WinUIA] _find_window_by_pid_tree 失败: %s", exc)
            return None

    def register_child_process_window(self, pid: int, timeout: float = 5.0) -> Optional[int]:
        """轮询等待指定 PID 的可见顶层窗口出现，找到后压入 `_hwnd_stack`。

        用于 `jump[]` 类主动拉起子进程场景：调用方（如 `apps/sample_web/actions.py::
        _run_quick_jump`）明确知道新窗口即将出现，且已知目标进程 PID，无需
        等待 `_reconcile_hwnd_stack` 的被动"同主窗口 PID"判据（该判据在
        `sample_web` 设置中心等多进程架构场景下不适用，见
        docs/plans/archive/01-engine-lifecycle-实施纪要.md §六/§七）。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self._find_new_owned_window(pid, set(self._hwnd_stack))
            if found is not None:
                self._hwnd_stack.append(found)
                self._hwnd = found
                logger.info(
                    "[WinUIA] 子进程窗口注入栈 hwnd=%s pid=%s (栈深=%d)",
                    found, pid, len(self._hwnd_stack),
                )
                return found
            time.sleep(0.2)
        logger.warning(
            "[WinUIA] 等待子进程(pid=%s)窗口超时(%ss)，未注入栈", pid, timeout,
        )
        return None

    # ══════════════════════════════════════════════════════════════════
    # 多进程页面：子窗口自动跟随（定位失败兜底）
    # 见 docs/plans/20261118180000-win-uia-follow-child-window.md
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _follow_child_window_enabled() -> bool:
        """`UIA_FOLLOW_CHILD_WINDOW`（默认开，0/false/off/no 关闭）。

        关闭即完全回退到"只搜主窗口"的原行为，零回归开关。
        """
        raw = (os.environ.get("UIA_FOLLOW_CHILD_WINDOW") or "").strip().lower()
        return raw not in ("0", "false", "off", "no")

    @staticmethod
    def _candidate_child_windows(root_pid: int, known_hwnds: set) -> list:
        """枚举 root_pid 及其子孙进程名下、可见、有标题、不在 known_hwnds 内的顶层窗口。"""
        try:
            import ctypes
            try:
                root_proc = psutil.Process(root_pid)
                pid_set = {root_pid} | {p.pid for p in root_proc.children(recursive=True)}
            except psutil.NoSuchProcess:
                pid_set = {root_pid}

            user32 = ctypes.windll.user32
            found: list = []

            def _enum(hwnd, _):
                if hwnd in known_hwnds or not user32.IsWindowVisible(hwnd):
                    return True
                pid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value not in pid_set:
                    return True
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                if buf.value.strip():
                    found.append(hwnd)
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
            user32.EnumWindows(WNDENUMPROC(_enum), 0)
            return found
        except Exception as exc:
            logger.debug("[WinUIA] _candidate_child_windows 失败: %s", exc)
            return []

    @staticmethod
    def _count_named_controls(hwnd: int, need: int = 1, max_depth: int = 6) -> int:
        """统计窗口 UIA 树里带 Name/AutomationId 的控件数（凑够 need 即停）。

        用于判定候选窗口"确有可用元素"，避免把空壳/无 UIA 桥接的窗口压进栈。
        """
        if not HAS_UIA or _uia is None:
            return 0
        try:
            root = _uia.ControlFromHandle(hwnd)
        except Exception:
            return 0
        if root is None:
            return 0
        count = 0
        stack = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            if depth > max_depth:
                continue
            try:
                if (node.Name or "") or (node.AutomationId or ""):
                    count += 1
                    if count >= need:
                        return count
                stack.extend((c, depth + 1) for c in (node.GetChildren() or []))
            except Exception:
                continue
        return count

    def _try_follow_child_window(self, need_named: int = 1) -> bool:
        """定位失败兜底：把同应用进程树内"新出现且 UIA 有元素"的子窗口压入 hwnd 栈。

        适用场景：点击后由独立进程承载的页面（如独立子窗口 = `AppHelper.exe`）。
        这类窗口 UIA 树完整可操作，但 `_reconcile_hwnd_stack` 的"同主窗口 PID"判据不覆盖
        多进程架构，默认查找根永远停在主窗口。

        保守约束：开关开启时执行；候选须可见、有标题、不在栈内、属于主窗口进程的子孙
        进程，且 UIA 树至少有 need_named 个带 Name/AutomationId 的控件。
        压栈成功返回 True；无合格候选返回 False 且不改动栈。
        """
        if not self._follow_child_window_enabled():
            return False
        if not self._hwnd_stack:
            # `_hwnd` 已由 launch()/attach_qt_probe() 定位、但 hwnd 栈尚未初始化时补齐。
            # 否则 `_ensure_hwnd()` 在"_hwnd 有效"分支直接返回，栈永远为空，本兜底永久失效。
            if self._hwnd is None:
                return False
            self._hwnd_stack = [self._hwnd]
        root_pid = self._get_pid_from_hwnd(self._hwnd_stack[0])
        if not root_pid:
            return False
        for hwnd in self._candidate_child_windows(root_pid, set(self._hwnd_stack)):
            if self._count_named_controls(hwnd, need=need_named) < need_named:
                logger.debug("[WinUIA] 子窗口 hwnd=%s UIA 无可判定控件，跳过", hwnd)
                continue
            self._hwnd_stack.append(hwnd)
            self._hwnd = hwnd
            logger.info("[WinUIA] 定位失败兜底：子窗口压栈 hwnd=%s (栈深=%d)",
                        hwnd, len(self._hwnd_stack))
            return True
        return False

    def _pop_followed_child_window(self) -> None:
        """兜底压栈后重试仍失败时回退一层，避免栈污染影响后续定位。"""
        if len(self._hwnd_stack) > 1:
            popped = self._hwnd_stack.pop()
            self._hwnd = self._hwnd_stack[-1]
            logger.debug("[WinUIA] 兜底子窗口重试失败，弹栈 hwnd=%s", popped)

    # ══════════════════════════════════════════════════════════════════
    # 定向窗口操作（close_window 定向关闭用；放具体类，不进 EngineBase —— R3）
    # 见 docs/plans/20261118190000-close-window-target-window.md
    # ══════════════════════════════════════════════════════════════════

    async def activate_window(self, hwnd: int) -> bool:
        """尽力把指定窗口置前并恢复显示，返回它是否已成为前台窗口（best-effort）。"""
        if not hwnd:
            return False
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._get_uia_executor(), self._sync_activate_window, hwnd)

    def _sync_activate_window(self, hwnd: int) -> bool:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            if not user32.IsWindow(hwnd):
                return False
            self._bring_to_foreground(hwnd)
            return int(user32.GetForegroundWindow() or 0) == int(hwnd)
        except Exception as exc:
            logger.debug("[WinUIA] activate_window 失败（忽略）: %s", exc)
            return False

    # 控制台/终端窗口类：快捷键类关闭手段落到它们身上会关掉测试进程自身
    _SELF_WINDOW_CLASSES = frozenset({
        "consolewindowclass",
        "cascadia_hosting_window_class",
        "pseudoconsolewindow",
        "windowsterminal",
        "conhostwindowclass",
    })

    async def find_window_by_title(self, keyword: str) -> Optional[int]:
        """按标题匹配可见顶层窗口。

        匹配优先级：**标题完全相等**者 → 标题含关键字者；同池内取**面积最小**者
        （弹窗/子面板远小于主窗口）。并排除「自身进程窗口」与「控制台/终端窗口类」，
        避免后续定向关闭把测试进程自身（宿主控制台）当成目标。
        """
        if not keyword:
            return None
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_find_window_by_title, keyword,
        )

    @classmethod
    def _sync_find_window_by_title(cls, keyword: str) -> Optional[int]:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            try:
                own_pid = int(ctypes.windll.kernel32.GetCurrentProcessId())
            except Exception:
                own_pid = 0
            get_class = getattr(user32, "GetClassNameW", None)
            exact: list = []
            fuzzy: list = []
            rejected: list = []

            def _enum(hwnd, _):
                if not user32.IsWindowVisible(hwnd):
                    return True
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                title = (buf.value or "").strip()
                if not title or keyword not in title:
                    return True
                if own_pid:
                    try:
                        pid_buf = ctypes.c_ulong(0)
                        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_buf))
                        if int(pid_buf.value) == own_pid:
                            rejected.append((int(hwnd), title, "自身进程"))
                            return True
                    except Exception:
                        pass
                if get_class is not None:
                    try:
                        class_buf = ctypes.create_unicode_buffer(256)
                        get_class(hwnd, class_buf, 256)
                        class_name = (class_buf.value or "").strip()
                        if class_name.lower() in cls._SELF_WINDOW_CLASSES:
                            rejected.append((int(hwnd), title, f"控制台类 {class_name}"))
                            return True
                    except Exception:
                        pass
                rect = ctypes.create_string_buffer(16)
                user32.GetWindowRect(hwnd, rect)
                left, top, right, bottom = ctypes.cast(
                    rect, ctypes.POINTER(ctypes.c_long * 4)).contents
                area = max(0, right - left) * max(0, bottom - top)
                (exact if title == keyword else fuzzy).append((area, int(hwnd), title))
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
            user32.EnumWindows(WNDENUMPROC(_enum), 0)
            if rejected:
                logger.info("[WinUIA] find_window_by_title(%r) 排除 %d 个自身/控制台窗口: %s",
                            keyword, len(rejected), rejected)
            pool = exact or fuzzy
            if not pool:
                return None
            pool.sort(key=lambda item: item[0])
            if len(pool) > 1:
                logger.info("[WinUIA] find_window_by_title(%r) 命中 %d 个%s，选面积最小 hwnd=%s %r；全部候选=%s",
                            keyword, len(pool), "（精确匹配）" if exact else "", pool[0][1],
                            pool[0][2], [item[2] for item in pool])
            else:
                logger.info("[WinUIA] find_window_by_title(%r) 命中 hwnd=%s %r",
                            keyword, pool[0][1], pool[0][2])
            return pool[0][1]
        except Exception as exc:
            logger.debug("[WinUIA] find_window_by_title 失败: %s", exc)
            return None

    async def window_of_element(self, element) -> Optional[int]:
        """取元素所属**顶层**窗口 hwnd（UIA `NativeWindowHandle` 为 0 时沿祖先链上溯）。"""
        raw = getattr(element, "raw", None)
        if raw is None:
            return None
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._get_uia_executor(), self._sync_window_of_element, raw)

    @staticmethod
    def _sync_window_of_element(raw) -> Optional[int]:
        import ctypes
        user32 = ctypes.windll.user32
        GA_ROOT = 2
        node = raw
        for _ in range(12):
            if node is None:
                return None
            try:
                handle = int(getattr(node, "NativeWindowHandle", 0) or 0)
            except Exception:
                handle = 0
            if handle:
                try:
                    root = user32.GetAncestor(handle, GA_ROOT)
                    return int(root or handle)
                except Exception:
                    return handle
            try:
                node = node.GetParentControl()
            except Exception:
                return None
        return None

    async def click_window_close_button(self, hwnd: int) -> bool:
        """在指定窗口内点关闭按钮（AutomationId/Name/标题栏右上角三类判据）。

        命中后优先 `InvokePattern.Invoke()`，失败退 `Click()`；找不到或无可用模式返回 False，
        由调用方降级快捷键（`close_window` 的 Alt+F4 路径）。
        """
        if not hwnd:
            return False
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_click_window_close_button, hwnd,
        )

    def _sync_click_window_close_button(self, hwnd: int) -> bool:
        if not HAS_UIA or _uia is None:
            return False
        try:
            import ctypes
            user32 = ctypes.windll.user32
            if not user32.IsWindow(hwnd):
                return False
            root = _uia.ControlFromHandle(hwnd)
            if root is None:
                return False
            rect = ctypes.create_string_buffer(16)
            user32.GetWindowRect(hwnd, rect)
            _left, wtop, wright, _bottom = ctypes.cast(
                rect, ctypes.POINTER(ctypes.c_long * 4)).contents

            candidates: list = []
            stack = [(root, 0)]
            while stack:
                node, depth = stack.pop()
                if depth > 12:
                    continue
                try:
                    ctype = node.ControlTypeName
                    auto_id = (node.AutomationId or "").lower()
                    name = node.Name or ""
                    if ctype == "ButtonControl":
                        low = name.lower()
                        if ("close" in auto_id or "关闭" in name or low == "close"
                                or low.startswith("close")):
                            candidates.append((0, node))
                        else:
                            try:
                                r = node.BoundingRectangle
                                if r and r.top - wtop < 80 and wright - r.right < 160:
                                    candidates.append((1, node))
                            except Exception:
                                pass
                    stack.extend((c, depth + 1) for c in (node.GetChildren() or []))
                except Exception:
                    continue

            if not candidates:
                logger.debug("[WinUIA] 窗口 hwnd=%s 未找到关闭按钮", hwnd)
                return False
            candidates.sort(key=lambda item: item[0])
            button = candidates[-1][1] if len(candidates) > 1 else candidates[0][1]
            try:
                button.GetInvokePattern().Invoke()
                logger.info("[WinUIA] 已 Invoke 关闭按钮 hwnd=%s name=%r",
                            hwnd, getattr(button, "Name", ""))
                return True
            except Exception:
                pass
            try:
                button.Click(simulateMove=False)
                logger.info("[WinUIA] 已 Click 关闭按钮 hwnd=%s", hwnd)
                return True
            except Exception as exc:
                logger.debug("[WinUIA] 关闭按钮点击失败: %s", exc)
                return False
        except Exception as exc:
            logger.debug("[WinUIA] click_window_close_button 失败: %s", exc)
            return False

    async def close_window_via_pattern(self, hwnd: int) -> bool:
        """用 UIA `WindowPattern.Close()` 关闭指定窗口（自绘标题栏/Qt 窗口唯一可靠手段）。"""
        if not hwnd:
            return False
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_close_window_via_pattern, hwnd,
        )

    def _sync_close_window_via_pattern(self, hwnd: int) -> bool:
        if not HAS_UIA or _uia is None:
            return False
        try:
            import ctypes
            if not ctypes.windll.user32.IsWindow(hwnd):
                return False
            root = _uia.ControlFromHandle(hwnd)
            if root is None:
                return False
            root.GetWindowPattern().Close()
            logger.info("[WinUIA] 已 WindowPattern.Close hwnd=%s", hwnd)
            return True
        except Exception as exc:
            logger.debug("[WinUIA] WindowPattern.Close 不可用 hwnd=%s: %s", hwnd, exc)
            return False

    async def post_window_close(self, hwnd: int) -> bool:
        """向指定窗口投递 `WM_CLOSE`（定向投递，不依赖前台焦点）。"""
        if not hwnd:
            return False
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_post_window_close, hwnd,
        )

    @staticmethod
    def _sync_post_window_close(hwnd: int) -> bool:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            if not user32.IsWindow(hwnd):
                return False
            WM_CLOSE = 0x0010
            ok = bool(user32.PostMessageW(int(hwnd), WM_CLOSE, 0, 0))
            logger.info("[WinUIA] 已投递 WM_CLOSE hwnd=%s ok=%s", hwnd, ok)
            return ok
        except Exception as exc:
            logger.debug("[WinUIA] post_window_close 失败 hwnd=%s: %s", hwnd, exc)
            return False

    async def is_window_alive(self, hwnd: int) -> bool:
        """窗口是否仍然存活且可见（关闭验证用：隐藏到托盘也视为已关闭）。"""
        if not hwnd:
            return False
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_is_window_alive, hwnd,
        )

    @staticmethod
    def _sync_is_window_alive(hwnd: int) -> bool:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            return bool(user32.IsWindow(hwnd)) and bool(user32.IsWindowVisible(hwnd))
        except Exception:
            return False

    async def is_foreground_window(self, hwnd: int) -> bool:
        """指定窗口当前是否为前台窗口。"""
        if not hwnd:
            return False
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_is_foreground_window, hwnd,
        )

    @staticmethod
    def _sync_is_foreground_window(hwnd: int) -> bool:
        try:
            import ctypes
            return int(ctypes.windll.user32.GetForegroundWindow() or 0) == int(hwnd)
        except Exception:
            return False

    async def reconcile_windows(self) -> None:
        """外部触发的窗口栈收敛（关闭定向窗口后调用，把栈顶失效帧弹回主窗口）。"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._get_uia_executor(), self._sync_reconcile_windows)

    def _sync_reconcile_windows(self) -> None:
        try:
            self._reconcile_hwnd_stack()
        except Exception as exc:
            logger.debug("[WinUIA] reconcile_windows 失败（忽略）: %s", exc)

    async def snapshot_visible_windows(self) -> "set[int]":
        """返回当前所有可见顶层窗口 hwnd 集合。

        供 `click[expect_new_window=true]` 点击前基线快照使用（见
        docs/plans/archive/03-resolver-rosetta-实施纪要.md §2.4）。
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(self._get_uia_executor(), self._sync_snapshot_visible_windows)

    @staticmethod
    def _sync_snapshot_visible_windows() -> "set[int]":
        try:
            import ctypes
            user32 = ctypes.windll.user32
            found: list[int] = []

            def _enum(hwnd, _):
                if user32.IsWindowVisible(hwnd):
                    found.append(hwnd)
                return True

            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
            user32.EnumWindows(WNDENUMPROC(_enum), 0)
            return set(found)
        except Exception as exc:
            logger.debug("[WinUIA] _sync_snapshot_visible_windows 失败: %s", exc)
            return set()

    async def register_any_new_window(self, known_hwnds: "set[int]", timeout: float = 5.0) -> Optional[int]:
        """轮询等待任意一个不在 known_hwnds 内的新可见顶层窗口出现（不限定
        PID，区别于 register_child_process_window 需要预先知道目标 PID），
        找到后压入 `_hwnd_stack`。

        用于 click[] 点击后不确定目标 PID 的弹窗场景（如设备状态卡片箭头
        按钮弹出跨进程窗口），见
        docs/plans/archive/03-resolver-rosetta-实施纪要.md §2.4。
        """
        loop = asyncio.get_event_loop()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = await self.snapshot_visible_windows()
            new_hwnds = current - known_hwnds - set(self._hwnd_stack)
            if new_hwnds:
                found = next(iter(new_hwnds))
                self._hwnd_stack.append(found)
                self._hwnd = found
                logger.info(
                    "[WinUIA] click 后检测到新窗口 hwnd=%s (栈深=%d)",
                    found, len(self._hwnd_stack),
                )
                return found
            await asyncio.sleep(0.2)
        logger.warning(
            "[WinUIA] click 后等待新窗口超时(%ss)，未注入栈", timeout,
        )
        return None

    @staticmethod
    def _is_hwnd_valid(hwnd: int) -> bool:
        """用 Win32 IsWindow 校验句柄是否仍指向存活窗口（轻量系统调用）。"""
        try:
            import ctypes
            return bool(ctypes.windll.user32.IsWindow(hwnd))
        except Exception:
            return False

    def _get_hwnd(self) -> Optional[int]:
        return self._ensure_hwnd()

    async def refresh_active_window(self) -> None:
        """刷新窗口栈引用（触发 _reconcile_hwnd_stack），供 scroll[direction]
        无元素名调用场景使用（无 resolver.resolve() 可依赖时的等价触发点，
        见 docs/plans/archive/02-dsl-actions-实施纪要.md）。"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._get_uia_executor(), self._get_hwnd)

    # ── 截图（二级降级：win32gui 窗口截图 → 全屏兜底）──────────────
    # 2026-07-13 移除未接线的 pywinauto 分支（self._window 从未真正赋值，
    # 该分支恒为死代码），见 compat-fix 计划 §3.4。

    _last_screenshot_is_window: bool = False
    # 最后一次窗口截图实际抓取的屏幕区域 (x, y, w, h)，物理像素；全屏降级/未截图为 None
    _last_screenshot_rect: Optional[tuple] = None

    # 截图总超时（秒）：_capture_win32/_capture_fullscreen 内部纯 GDI 调用，
    # 无子进程/无自带超时，正常应为毫秒级完成，21s 是充分宽松的上限。
    # 2026-08-21 新增，见计划文档 20260820160000-mac-win-engine-screenshot-
    # hang-isolation-and-shared-bounded-executor.md：此前 take_screenshot()
    # 完全没有外层超时保护，一旦卡死会让 STA 唯一 worker 永久占用，导致该
    # 引擎实例后续所有操作跟着假死。
    _SCREENSHOT_TIMEOUT = 21

    async def take_screenshot(self, path: str = "") -> Optional[bytes]:
        """经 STA 执行器调度（2026-07-13 统一，见 §sta-executor-unify 计划），
        与其余 UIA/COM 操作共用同一线程，符合类文档声明的架构约束。"""
        target_name = os.path.basename(path) if path else "_uia_win_screenshot.png"
        started_at = time.monotonic()
        logger.log(
            NOTICE,
            "[hang-trace] UIA_SCREENSHOT_SUBMIT app=%s hwnd=%s file=%s",
            self._app_name, self._hwnd, target_name,
        )
        data = await self._run_on_sta(
            self._sync_take_screenshot, path, timeout=self._SCREENSHOT_TIMEOUT,
        )
        logger.log(
            NOTICE,
            "[hang-trace] UIA_SCREENSHOT_RETURN app=%s hwnd=%s elapsed_ms=%d bytes=%d file=%s",
            self._app_name, self._hwnd,
            int((time.monotonic() - started_at) * 1000), len(data or b""), target_name,
        )
        return data

    def _sync_take_screenshot(self, path: str) -> Optional[bytes]:
        target = path or os.path.join(tempfile.gettempdir(), "_uia_win_screenshot.png")
        target_name = os.path.basename(target)
        started_at = time.monotonic()
        logger.log(
            NOTICE,
            "[hang-trace] UIA_SCREENSHOT_WORKER_START app=%s hwnd=%s file=%s",
            self._app_name, self._hwnd, target_name,
        )
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        self._last_screenshot_rect = None
        data = self._capture_win32(target)
        if not data:
            logger.log(
                NOTICE,
                "[hang-trace] UIA_SCREENSHOT_FALLBACK_START app=%s hwnd=%s file=%s",
                self._app_name, self._hwnd, target_name,
            )
            data = self._capture_fullscreen(target)
        logger.log(
            NOTICE,
            "[hang-trace] UIA_SCREENSHOT_WORKER_END app=%s hwnd=%s elapsed_ms=%d bytes=%d file=%s",
            self._app_name, self._hwnd,
            int((time.monotonic() - started_at) * 1000), len(data or b""), target_name,
        )
        return data

    def _capture_win32(self, target: str) -> Optional[bytes]:
        try:
            import win32gui
            from PIL import ImageGrab
            logger.log(
                NOTICE, "[hang-trace] UIA_SCREENSHOT_HWND_START app=%s cached_hwnd=%s",
                self._app_name, self._hwnd,
            )
            hwnd = self._get_hwnd()
            logger.log(
                NOTICE, "[hang-trace] UIA_SCREENSHOT_HWND_END app=%s hwnd=%s",
                self._app_name, hwnd,
            )
            if not hwnd:
                return None
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            logger.log(
                NOTICE,
                "[hang-trace] UIA_SCREENSHOT_GRAB_START app=%s hwnd=%s rect=%s,%s,%s,%s",
                self._app_name, hwnd, left, top, right, bottom,
            )
            img = ImageGrab.grab(bbox=(left, top, right, bottom))
            logger.log(
                NOTICE, "[hang-trace] UIA_SCREENSHOT_GRAB_END app=%s hwnd=%s size=%s",
                self._app_name, hwnd, getattr(img, "size", None),
            )
            img.save(target)
            if os.path.getsize(target) == 0:
                return None
            self._last_screenshot_is_window = True
            self._last_screenshot_rect = (
                float(left), float(top), float(right - left), float(bottom - top),
            )
            logger.debug("[WinUIA] win32gui 窗口截图: %s", target)
            with open(target, "rb") as f:
                return f.read()
        except Exception as exc:
            logger.debug("[WinUIA] win32gui 截图失败: %s", exc)
            return None

    async def grab_bgr(self):
        """抓取被测窗口当前画面为内存 BGR ndarray（不编码 PNG、不落盘、不回读解码）。

        供高频视觉指纹轮询使用：与 :meth:`take_screenshot` 取同一窗口区域，像素一致，
        只省掉 PNG 编码 + 写盘 + 读盘 + ``imdecode`` 这段与匹配无关的损耗。
        窗口句柄不可得/抓取失败返回 ``None``（调用方退回 ``take_screenshot`` 路径）；
        不降级全屏——全屏会把桌面/其它窗口带进匹配范围。引擎专用能力，不属 EngineBase（R3）。
        """
        return await self._run_on_sta(self._sync_grab_bgr, timeout=self._SCREENSHOT_TIMEOUT)

    def _sync_grab_bgr(self):
        try:
            import numpy as np
            import win32gui
            from PIL import ImageGrab

            hwnd = self._get_hwnd()
            if not hwnd:
                return None
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            img = ImageGrab.grab(bbox=(left, top, right, bottom)).convert("RGB")
            return np.ascontiguousarray(np.asarray(img)[:, :, ::-1])
        except Exception as exc:
            logger.debug("[WinUIA] grab_bgr 失败: %s", exc)
            return None

    def _capture_fullscreen(self, target: str) -> Optional[bytes]:
        try:
            from PIL import ImageGrab
            logger.log(
                NOTICE, "[hang-trace] UIA_FULLSCREEN_GRAB_START app=%s", self._app_name,
            )
            img = ImageGrab.grab()
            logger.log(
                NOTICE, "[hang-trace] UIA_FULLSCREEN_GRAB_END app=%s size=%s",
                self._app_name, getattr(img, "size", None),
            )
            img.save(target)
            if os.path.getsize(target) == 0:
                return None
            self._last_screenshot_is_window = False
            logger.debug("[WinUIA] 全屏截图: %s", target)
            with open(target, "rb") as f:
                return f.read()
        except Exception as exc:
            logger.warning("[WinUIA] 全屏截图失败: %s", exc)
            return None

    async def get_element_rect(self, element: ElementRef) -> Optional[tuple]:
        """经 STA 执行器调度：element.raw 是 uiautomation.Control（COM 对象），
        跨线程访问其属性有 marshaling 风险，必须与创建它的 find_by_id 等方法
        共用同一 STA 线程（2026-07-13 统一，见 §sta-executor-unify 计划）。

        qt 通道句柄（raw=None）改走探针 `bounds`，避免空 raw 静默返回 None
        导致 visible/rect 类断言被误判。
        """
        qt = self._qt_client_of(element)
        if qt is not None:
            return await qt[0].bounds(qt[1])
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_get_element_rect, element,
        )

    def _sync_get_element_rect(self, element: ElementRef) -> Optional[tuple]:
        try:
            raw = element.raw
            if raw is None:
                return None
            if hasattr(raw, "rectangle"):
                r = raw.rectangle()
                return (r.left, r.top, r.width(), r.height())
            elif hasattr(raw, "BoundingRectangle"):
                r = raw.BoundingRectangle
                return (r.left, r.top, r.width(), r.height())
            return None
        except Exception as exc:
            logger.debug("[WinUIA] get_element_rect 失败: %s", exc)
            return None

    async def get_last_screenshot_rect(self) -> Optional[tuple]:
        """最近一次**窗口**截图实际抓取的屏幕区域 (x, y, w, h)（引擎专用能力，R3）。

        截图与 get_window_rect() 之间窗口可能移动/切换（hwnd 重新解析），把截图像素换算回
        屏幕坐标时须以截图那一刻的区域为准。全屏降级或未截图返回 None。
        """
        rect = self._last_screenshot_rect
        return tuple(rect) if rect else None

    async def get_window_rect(self) -> Optional[tuple]:
        """经 STA 执行器调度（2026-07-13 统一，见 §sta-executor-unify 计划）。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_get_window_rect,
        )

    def _sync_get_window_rect(self) -> Optional[tuple]:
        try:
            hwnd = self._get_hwnd()
            if hwnd:
                import win32gui
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                return (left, top, right - left, bottom - top)
            return None
        except Exception as exc:
            logger.debug("[WinUIA] get_window_rect 失败: %s", exc)
            return None

    async def get_screen_rect(self) -> Optional[tuple]:
        """主屏矩形 ``(0, 0, w, h)``，物理像素（引擎专用能力，R3）。

        `--device-fullscreen` 下的坐标基准；与 `GetWindowRect` 同一套坐标系，
        因此无需再做 DPI 二次换算。取不到返回 ``None``（交由 coords 判缺口）。
        """
        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                self._get_uia_executor(), self._sync_get_screen_rect,
            )
        except Exception as exc:  # noqa: BLE001 - 取不到即视为能力缺口
            logger.debug("[WinUIA] get_screen_rect 失败: %s", exc)
            return None

    def _sync_get_screen_rect(self) -> Optional[tuple]:
        try:
            import win32api

            width = int(win32api.GetSystemMetrics(0))
            height = int(win32api.GetSystemMetrics(1))
            if width <= 0 or height <= 0:
                return None
            return (0, 0, width, height)
        except Exception as exc:  # noqa: BLE001 - 无 win32api 等环境问题
            logger.debug("[WinUIA] _sync_get_screen_rect 失败: %s", exc)
            return None

    async def take_fullscreen_screenshot(self, path: str = "") -> Optional[bytes]:
        """整屏截图（`--device-fullscreen` 专用；窗口模式不走这里）。

        经 STA 执行器调度，与其它截图/COM 操作共用线程；失败返回 ``b""``，
        与窗口截图失败路径同口径。
        """
        target_name = os.path.basename(path) if path else "_uia_win_fullscreen.png"
        logger.log(
            NOTICE,
            "[hang-trace] UIA_FULLSCREEN_SUBMIT app=%s hwnd=%s file=%s",
            self._app_name, self._hwnd, target_name,
        )
        data = await self._run_on_sta(
            self._sync_take_fullscreen_screenshot, path, timeout=self._SCREENSHOT_TIMEOUT,
        )
        return data

    def _sync_take_fullscreen_screenshot(self, path: str) -> Optional[bytes]:
        target = path or os.path.join(tempfile.gettempdir(), "_uia_win_fullscreen.png")
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
        try:
            data = self._capture_fullscreen(target)
            return data or b""
        finally:
            self._last_screenshot_is_window = False
            self._last_screenshot_rect = None

    # ══════════════════════════════════════════════════════════════════
    # 系统文件选择对话框（choose_file[] Action 支持）
    # 计划文档：docs/plans/archive/02-dsl-actions-实施纪要.md §3.5
    # ══════════════════════════════════════════════════════════════════

    async def has_open_file_dialog(self) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_has_open_file_dialog,
        )

    def _find_open_file_dialog_controls(self) -> tuple[Optional[int], object, object, object]:
        """定位标准 Common Dialog 及文件名输入框/打开按钮，不依赖前台焦点。"""
        if not HAS_UIA or _uia is None:
            return None, None, None, None
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnds: list[int] = []
            foreground = user32.GetForegroundWindow()
            if foreground:
                hwnds.append(int(foreground))

            def _enum(hwnd, _lparam):
                hwnd_int = int(hwnd)
                if hwnd_int not in hwnds and user32.IsWindowVisible(hwnd):
                    hwnds.append(hwnd_int)
                return True

            callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            user32.EnumWindows(callback_type(_enum), 0)
            for hwnd in hwnds:
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_buf, 256)
                if class_buf.value != "#32770":
                    continue
                root = _uia.ControlFromHandle(hwnd)
                if root is None:
                    continue
                filename_edit = self._find_uia_control(
                    root, "", "1148", "EditControl", max_depth=20,
                )
                if filename_edit is None:
                    continue
                open_button = self._find_uia_control(
                    root, "", "1", "ButtonControl", max_depth=20,
                )
                if open_button is None:
                    open_button = self._find_uia_control(
                        root, "打开|Open", "", "ButtonControl", max_depth=20,
                    )
                return hwnd, root, filename_edit, open_button
        except Exception as exc:
            logger.debug("[WinUIA] 定位文件选择对话框控件失败: %s", exc)
        return None, None, None, None

    def _find_uwp_open_file_dialog_hwnd(self) -> Optional[int]:
        """定位现代 UWP 文件选择器（FileOpenPicker/FileSavePicker）窗口。

        UWP 应用窗口类通常为 ``ApplicationFrameWindow``（非传统 Common
        Dialog 的 ``#32770``），``_find_open_file_dialog_controls`` 完全
        识别不到，导致 ``choose_file`` 首步即超时失败（见
        docs/plans/archive/01-engine-lifecycle-实施纪要.md）。

        识别策略：枚举可见窗口，跳过 ``#32770``（走原有路径），按标题关键词
        初筛，再用 UIA 树查找"文件名/File name"附近的``EditControl`` 二次
        确认，降低误识别为其他窗口的概率。
        """
        if not HAS_UIA or _uia is None:
            return None
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnds: list[int] = []

            def _enum(hwnd, _lparam):
                hwnd_int = int(hwnd)
                if user32.IsWindowVisible(hwnd) and hwnd_int not in hwnds:
                    hwnds.append(hwnd_int)
                return True

            callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            user32.EnumWindows(callback_type(_enum), 0)

            title_keywords = ("open", "打开", "选择", "浏览", "select", "browse")
            for hwnd in hwnds:
                class_buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, class_buf, 256)
                if class_buf.value == "#32770":
                    continue  # 传统 Common Dialog走原有路径，不在此重复处理
                title_buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, title_buf, 256)
                title_lower = (title_buf.value or "").lower()
                if not any(kw in title_lower for kw in title_keywords):
                    continue
                root = _uia.ControlFromHandle(hwnd)
                if root is None:
                    continue
                filename_edit = self._find_uia_control(
                    root, "文件名|File name", "", "EditControl", max_depth=20,
                )
                if filename_edit is None:
                    continue  # 未找到文件名输入框，判定非文件选择对话框
                return hwnd
        except Exception as exc:
            logger.debug("[WinUIA] 定位 UWP 文件选择对话框失败: %s", exc)
        return None

    def _sync_has_open_file_dialog(self) -> bool:
        """检测含标准文件名输入框的 Windows Common Dialog，或现代 UWP 文件选择器。"""
        hwnd, _root, _filename_edit, _open_button = self._find_open_file_dialog_controls()
        if hwnd is not None:
            return True
        return self._find_uwp_open_file_dialog_hwnd() is not None

    async def goto_path_in_file_dialog(self, path: str) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._get_uia_executor(), self._sync_goto_path_in_file_dialog, path,
        )

    def _sync_goto_path_in_file_dialog(self, path: str) -> bool:
        """向 Common Dialog 底部文件名输入框写入完整路径并回车；未命中时回退
        到 UWP 文件选择器的键盘导航路径（Ctrl+L 聚焦地址栏 + 逐字符注入 +
        Enter，见docs/plans/archive/01-engine-lifecycle-实施纪要.md）。
        """
        try:
            hwnd, _root, filename_edit, _open_button = self._find_open_file_dialog_controls()
            if hwnd is None or filename_edit is None:
                return self._sync_goto_path_in_uwp_file_dialog(path)
            self._bring_to_foreground(hwnd)
            try:
                control_type = filename_edit.ControlTypeName or ""
                automation_id = filename_edit.AutomationId or ""
            except Exception:
                control_type = ""
                automation_id = ""
            if control_type != "EditControl" or automation_id != "1148":
                logger.warning(
                    "[WinUIA] choose_file 文件名输入框不匹配(type=%r, automation_id=%r)",
                    control_type, automation_id,
                )
                return False
            value_pattern = filename_edit.GetValuePattern()
            if value_pattern is None:
                logger.warning("[WinUIA] choose_file 文件名输入框不支持 ValuePattern")
                return False
            value_pattern.SetValue(path)
            filename_edit.SetFocus()
            self._sync_press_key("enter")
            import ctypes
            user32 = ctypes.windll.user32
            deadline = time.time() + 3.0
            while time.time() < deadline:
                # 只验证本次操作的原始 HWND。系统上可能同时存在其他进程的
                # Common Dialog，按“任意 #32770”判断会在文件已选中时误报失败。
                if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
                    return True
                time.sleep(0.1)
            logger.warning("[WinUIA] choose_file 回车后原文件对话框仍未关闭(hwnd=%s)", hwnd)
            return False
        except Exception as exc:
            logger.warning("[WinUIA] _sync_goto_path_in_file_dialog failed: %s", exc)
            return False

    def _sync_goto_path_in_uwp_file_dialog(self, path: str) -> bool:
        """UWP文件选择器键盘导航回退路径（Common Dialog 未命中时使用）。

        ``Ctrl+L`` 是 Windows Shell 通用地址栏快捷方式，对 UWP
        FileOpenPicker/FileSavePicker 同样生效，故不依赖 UIA ValuePattern，
        改用逐字符 Unicode 注入（见 docs/plans/20260810150000-choose-file-
        focus-fix-mac-and-uwp.md）。
        """
        hwnd = self._find_uwp_open_file_dialog_hwnd()
        if hwnd is None:
            logger.warning("[WinUIA] choose_file 未找到 UWP 文件选择器窗口")
            return False
        try:
            self._bring_to_foreground(hwnd)
            time.sleep(0.3)
            self._sync_press_key("ctrl+l")
            time.sleep(0.2)
            for ch in path:
                self._input_char(ch)
            time.sleep(0.3)
            self._sync_press_key("enter")
            import ctypes
            user32 = ctypes.windll.user32
            deadline = time.time() + 3.0
            while time.time() < deadline:
                if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
                    return True
                time.sleep(0.1)
            logger.warning(
                "[WinUIA] choose_file(UWP) 回车后对话框仍未关闭(hwnd=%s)", hwnd,
            )
            return False
        except Exception as exc:
            logger.warning("[WinUIA] _sync_goto_path_in_uwp_file_dialog failed: %s", exc)
            return False
