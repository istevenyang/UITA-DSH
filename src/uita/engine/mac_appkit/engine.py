"""
MacAppKitEngine — macOS 原生 Accessibility API 引擎。

适用场景：纯原生 AppKit 应用（非 Electron/Web），如示例桌面应用。
驱动方式：atomacos（macOS AX API Python 绑定）+ asyncio.to_thread 异步桥接。

依赖安装：
    pip install atomacos pyobjc-framework-ApplicationServices

注册方式（apps/sample_web/register.py 等）：
    @register_engine("sample_web", "macos")
    class SampleDesktopEngine(MacAppKitEngine): ...

或直接用工厂函数：
    engine = MacAppKitEngine.for_bundle_id("com.example.demo", app_path="...")
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import time
from typing import Optional

from uita.core.engine.base import ElementRef, EngineBase
from uita.core.engine.registry import register_engine
from uita.engine.mac_appkit.ax_query import (
    AXPathError, AXPathSpec, is_axpath, is_ax_offset, is_scoped_axpath,
    parse_axpath, query_element, query_element_in_windows,
    query_element_with_offset,
)
from uita.engine.playwright.sync_base import SyncEngineBase
from uita.config import NOTICE

logger = logging.getLogger(__name__)

# 尝试导入 atomacos（运行时依赖，安装后可用）
try:
    import atomacos  # type: ignore
    _HAS_ATOMACOS = True
except ImportError:
    atomacos = None
    _HAS_ATOMACOS = False
    logger.warning(
        "atomacos 未安装，MacAppKitEngine 将不可用。"
        "安装方式：pip install atomacos pyobjc-framework-ApplicationServices"
    )


def _require_atomacos() -> None:
    if not _HAS_ATOMACOS:
        raise RuntimeError(
            "atomacos 未安装，无法使用 MacAppKitEngine。"
            "请运行：pip install atomacos pyobjc-framework-ApplicationServices"
        )


# 屏幕录制权限告警是否已打印过（模块级去重，整进程只告警一次，避免每次
# 截图前后都刷屏，见 docs/plans/20260817150000-mac-screenshot-desktop-
# background-permission-fix.md）
_screen_capture_access_warned = False


def _check_screen_capture_access() -> bool:
    """检测当前进程是否已获得 macOS『屏幕录制』（Screen Recording）权限。

    macOS 10.15+ 强制要求：未授权时 CGWindowListCreateImage/screencapture
    不会报错，而是静默返回桌面壁纸/空白背景（Apple 官方既定行为，非本项目
    bug）。macOS 15 Sequoia 起该权限需每月重新确认，到期后会静默失效，是
    "截图退化为桌面背景"最常见的触发场景。

    返回 True 表示已授权，或 API 不可用/检测异常时保守放行（不阻断截图，
    避免预检本身成为新的失败点）；返回 False 表示确认缺失权限，调用方应
    放弃本次注定产出错误图像的截图尝试。
    """
    global _screen_capture_access_warned
    try:
        import Quartz  # type: ignore

        preflight = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
        if preflight is None:
            return True  # 老版本 pyobjc 无此符号，保守放行
        if preflight():
            return True
    except Exception:
        return True  # 检测本身失败不应阻断截图，保守放行

    if not _screen_capture_access_warned:
        _screen_capture_access_warned = True
        logger.log(
            NOTICE,
            "[MacAppKit] ⚠️ 未获得『屏幕录制』权限，截图将只显示桌面背景而非"
            "窗口内容！请到 系统设置→隐私与安全性→屏幕录制 中为运行 UITA 的"
            "终端应用（Terminal/iTerm2 等）授权，若已授权过请重新勾选一次"
            "（macOS 15 起该权限需每月重新确认，到期会静默失效），"
            "授权后需完全退出并重新打开终端应用才能生效。",
        )
    return False


@register_engine("*", "macos_native")
class MacAppKitEngine(SyncEngineBase):
    """macOS 原生可访问性引擎（atomacos）。

    Args:
        bundle_id: 应用的 CFBundleIdentifier（如 "com.example.demo"）
        app_path:  可执行文件路径（未运行时自动启动）
        app_name:  进程名（用于日志）
        window_index: 目标窗口索引（默认 0，即第一个窗口）
    """

    def __init__(
        self,
        bundle_id: str = "",
        app_path: str = "",
        app_name: str = "",
        window_index: int = 0,
        companion_bundle_ids: Optional[list] = None,
        **kwargs,
    ) -> None:
        self._bundle_id = bundle_id
        self._app_path = app_path
        self._app_name = app_name or (bundle_id.split(".")[-1] if bundle_id else "app")
        self._window_index = window_index
        self._companion_bundle_ids: list = list(companion_bundle_ids or [])
        self._ax_app = None    # atomacos application ref（主进程）
        self._window = None    # atomacos window ref（始终 = 栈顶）
        self._window_stack: list = []  # 窗口栈：[(window_ref, title), ...]，见 R30 计划文档
        # docs/plans/archive/01-engine-lifecycle-实施纪要.md
        # 卫星进程 AX app ref 列表（每项为 atomacos app ref 或 None）
        self._satellite_apps: list = []

    # ── 工厂方法 ─────────────────────────────────────────────

    @classmethod
    def for_bundle_id(
        cls,
        bundle_id: str,
        app_path: str = "",
        app_name: str = "",
        window_index: int = 0,
        companion_bundle_ids: Optional[list] = None,
    ) -> "MacAppKitEngine":
        return cls(
            bundle_id=bundle_id,
            app_path=app_path,
            app_name=app_name,
            window_index=window_index,
            companion_bundle_ids=companion_bundle_ids,
        )

    # ── 元数据 ───────────────────────────────────────────────

    @property
    def app_name(self) -> str:
        return self._app_name or "*"

    @property
    def platform(self) -> str:
        return "macos_native"

    # ── 生命周期 ─────────────────────────────────────────────

    async def launch(self) -> None:
        _require_atomacos()
        await self._run_sync(self._sync_launch)

    def _sync_launch(self) -> None:
        """附着已运行实例；未运行时先启动再附着。"""
        # 1. 先尝试附着已运行实例
        if self._bundle_id:
            try:
                self._ax_app = atomacos.getAppRefByBundleId(self._bundle_id)
                self._window = self._get_window()  # 内部已用 _select_main_window() 智能选主窗口
                self._window_stack = [(self._window, self._safe_title(self._window))]
                logger.info("[MacAppKit] 附着应用: %s (bundle=%s)", self._app_name, self._bundle_id)
                # 已运行实例可能在后台；首个 CGEvent 此时只会激活窗口并吞掉
                # 实际点击。附着完成后先置前并等待事件循环稳定。
                self._sync_bring_to_foreground()
                time.sleep(0.3)
                # 附着卫星进程（best-effort，单个失败不阻断）
                self._attach_satellites()
                return
            except Exception as exc:
                logger.debug("[MacAppKit] 附着失败: %s，尝试启动...", exc)

        # 2. 附着失败 → 用 app_path 启动
        if not self._app_path or not os.path.exists(self._app_path):
            raise RuntimeError(
                f"[MacAppKit] 无法附着应用 bundle_id={self._bundle_id!r}，"
                f"且 app_path 不存在: {self._app_path!r}"
            )

        self._clear_quarantine_if_present()
        self._open_via_launchservices()
        logger.info("[MacAppKit] 已启动 %s，等待 AX 就绪...", self._app_path)

        # ── 前置诊断：检测辅助功能权限 ──
        _ax_trusted = self._check_ax_trusted()
        if not _ax_trusted:
            logger.log(NOTICE,
                "[MacAppKit] ⚠️ AXIsProcessTrusted=False — 当前进程未获得"
                "『辅助功能』权限，windows() 将始终为空！请到 系统设置→"
                "隐私与安全性→辅助功能 中授权运行 UITA 的终端/Python 进程",
            )

        # 轮询附着（最多 30s，每次 1s）：等窗口存在且有子元素（UI 已渲染）
        _last_stage = "init"  # 诊断：记录最后卡在哪一步
        for attempt in range(30):
            time.sleep(1.0)
            try:
                self._ax_app = atomacos.getAppRefByBundleId(self._bundle_id)
                wins = self._ax_app.windows()
                if not wins:
                    _last_stage = "no_windows"
                    # 尝试 activate() 唤醒：某些 app 启动后不主动创建窗口，
                    # 需要被"激活"才弹出主窗口（如应用后台模式首次启动）
                    if attempt in (2, 7, 15):
                        try:
                            self._ax_app.activate()
                        except Exception:
                            pass
                    if attempt % 5 == 4:  # 每 5s 输出一次诊断
                        # 额外检测进程是否存活
                        _alive = self._is_process_alive()
                        logger.log(NOTICE,
                            "[MacAppKit] 轮询 %d/30: AX app ref 已获取但 windows() 为空"
                            " (process_alive=%s, ax_trusted=%s)",
                            attempt + 1, _alive, _ax_trusted,
                        )
                    continue
                win = self._select_main_window(wins)
                children = win.AXChildren
                if children:
                    self._window = win
                    self._window_stack = [(self._window, self._safe_title(self._window))]
                    logger.log(NOTICE, "[MacAppKit] ✅启动并附着成功 (%.0fs): %s", attempt + 1, self._app_name)
                    return
                _last_stage = f"no_children(wins={len(wins)},title={self._safe_title(win)!r})"
                if attempt % 5 == 4:
                    logger.log(NOTICE,
                        "[MacAppKit] 轮询 %d/30: 窗口已出现(%d个) 但 AXChildren 为空 (title=%r)",
                        attempt + 1, len(wins), self._safe_title(win),
                    )
            except Exception as exc:
                _last_stage = f"exception({type(exc).__name__}: {exc})"
                if attempt % 5 == 4:
                    logger.log(NOTICE,
                        "[MacAppKit] 轮询 %d/30: 附着异常: %s", attempt + 1, exc,
                    )
                continue

        raise RuntimeError(
            f"[MacAppKit] 应用启动超时 (30s): {self._app_path} "
            f"[last_stage={_last_stage}, ax_trusted={_ax_trusted}]"
        )

    @staticmethod
    def _check_ax_trusted() -> bool:
        """检测当前进程是否已获得 macOS『辅助功能』(Accessibility) 权限。

        未授权时 atomacos 能获取 AX app ref（只需 PID），但 windows()/
        findAllR() 等 UI 树查询均返回空（系统静默拒绝，不抛异常）。
        检测失败时保守返回 True（不阻断启动流程）。

        优先调用带 kAXTrustedCheckOptionPrompt=True 的
        AXIsProcessTrustedWithOptions：若当前进程从未出现在『辅助功能』
        列表中，系统会弹出一次系统授权对话框（仅对"从未出现过"的进程
        生效；若之前已出现过但被手动取消勾选，系统不会重新弹窗，此时
        仍需用户手动去 系统设置→隐私与安全性→辅助功能 中重新勾选）。
        该 API 不可用时（旧版 pyobjc/非 macOS）退化为纯查询版本。
        """
        try:
            import ApplicationServices  # type: ignore  # noqa: PLC0415
            options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
            return bool(ApplicationServices.AXIsProcessTrustedWithOptions(options))
        except Exception:
            pass
        try:
            import ApplicationServices  # type: ignore  # noqa: PLC0415
            return bool(ApplicationServices.AXIsProcessTrusted())
        except Exception:
            return True  # API 不可用时保守放行

    def _is_process_alive(self) -> bool:
        """通过 pgrep 检测目标进程是否仍在运行（独立于 AX API）。"""
        try:
            result = subprocess.run(
                ["pgrep", "-x", self._app_name],
                capture_output=True,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _attach_satellites(self) -> None:
        """按 _companion_bundle_ids 列表附着伴生进程 AX app refs。

        单个 bundle 不存在或 attach 失败时记录 debug 并跳过（best-effort，
        AEMonitor 等卫星常驻但可能未启动时不应阻断主流程）。
        """
        self._satellite_apps = []
        for bid in self._companion_bundle_ids or []:
            try:
                app_ref = atomacos.getAppRefByBundleId(bid)
                self._satellite_apps.append(app_ref)
                logger.info("[MacAppKit] 卫星进程附着: %s", bid)
            except Exception as exc:
                logger.debug("[MacAppKit] 卫星进程 %s 附着失败（忽略）: %s", bid, exc)
                self._satellite_apps.append(None)

    def _search_windows(self):
        """返回 [主窗口] + [各卫星进程所有窗口] 的序列。

        主窗口始终第一项（_refresh_window 已 reconcile 栈顶），
        卫星窗口按 attach 顺序依次追加；None 或空 windows() 的卫星跳过。
        供 scoped AXPath 与裸 selector 的卫星兜底查询使用。
        """
        result = []
        if self._window is not None:
            result.append(self._window)
        for app_ref in self._satellite_apps or []:
            if app_ref is None:
                continue
            try:
                wins = list(app_ref.windows() or [])
            except Exception:
                continue
            result.extend(wins)
        return result

    def _get_window(self):
        wins = self._ax_app.windows()
        if not wins:
            raise RuntimeError(f"[MacAppKit] 应用无可用窗口: {self._app_name}")
        return self._select_main_window(wins)

    def _select_main_window(self, wins: list):
        """从候选窗口列表中选出"主窗口"，用于窗口栈初始化 base。

        背景（真机 bug 复现，见 docs/plans/20260730180000-mac-engine-attach-
        multi-window-misidentify-main-window-fix.md）：附着已运行实例时，若
        此刻应用已有多个窗口打开（如"设置"/"偏好设置"面板残留未关闭），盲取
        ``wins[0]`` 作为窗口栈栈底并不可靠——`app.windows()` 返回顺序不代表
        "谁是主窗口"。栈底一旦选错，后续 reconcile 会把真正的主窗口误当"新
        弹窗"压到栈顶，导致 find_by_id/click 实际作用于错误窗口。

        仅在候选数 > 1 且 ``self._window_index`` 为默认值 0 时启用智能判据；
        调用方显式传入非 0 ``window_index`` 视为明确指定，直接沿用既有
        ``wins[min(window_index, len-1)]`` 行为，不做任何智能判断（零回归，
        当前代码库中无实际使用者，仅保留接口兼容）。

        判据（真机验证：偏好设置/辅助面板类窗口 AXTitle 常为空字符串，主
        窗口通常有非空标题）：
        1. 优先选 AXTitle 非空的窗口；
        2. 候选仍多于 1 个（或全部标题为空）→ 取 AXSize 面积最大的一个
           （主窗口通常明显更大，辅助面板/偏好设置通常较小）；
        3. 单一候选 → 直接返回（原有行为不变）。
        """
        if len(wins) <= 1 or self._window_index != 0:
            return wins[min(self._window_index, len(wins) - 1)]

        candidates = wins
        titled = [w for w in candidates if self._safe_title(w)]
        if titled:
            candidates = titled

        def _area(w) -> float:
            try:
                size = w.AXSize
                return float(size.width) * float(size.height)
            except Exception:
                return -1.0

        return max(candidates, key=_area)

    async def close(self) -> None:
        """释放 AX 引用，不终止应用进程。"""
        self._ax_app = None
        self._window = None
        self._window_stack = []
        self._satellite_apps = []
        # 清理 _run_sync 的私有隔离执行器（见计划文档 20260820160000），
        # 避免残留线程阻塞进程退出；未创建过（从未调用过 _run_sync）时安全
        # 跳过，不报错。
        await self._shutdown_sync_guard()

    async def bring_to_foreground(self) -> None:
        """将应用置于前台（激活应用），与 Windows 版
        `WinUIAEngine.bring_to_foreground()`（SetForegroundWindow）对称。

        见 docs/plans/archive/09-apps-business-实施纪要.md。
        best-effort：应用未连接或激活失败时不抛异常，不阻断后续操作。
        """
        await self._run_sync(self._sync_bring_to_foreground)

    def _sync_bring_to_foreground(self) -> None:
        if self._ax_app is None:
            return
        try:
            self._ax_app.activate()
        except Exception as exc:
            logger.debug("[MacAppKit] bring_to_foreground 失败（忽略）: %s", exc)

    async def is_healthy(self) -> bool:
        if self._ax_app is None or self._window is None:
            return False
        try:
            # 不仅要窗口存在，还要有子元素（确认 UI 已渲染）
            def _check():
                title = self._window.AXTitle
                children = self._window.AXChildren
                return bool(title is not None and children)
            return await self._run_sync(_check)
        except Exception:
            return False

    async def restart(self) -> None:
        """强制重启：pkill 精确杀进程 → 重新拉起并附着。"""
        logger.log(NOTICE, "[MacAppKit] ➡️重启应用: %s", self._app_name)
        await self.close()
        await self._run_sync(self._sync_kill)
        await self.launch()

    async def terminate(self) -> None:
        """只终止进程，不重新拉起（区别于 restart() 的 kill+launch 组合）。

        可选生命周期钩子（getattr 探测模式，不加入 EngineBase 18 个抽象方法，
        R9：引擎专用能力写在具体引擎类）。复用现成的 _sync_kill()（pkill -x
        精确终止 + 轮询确认退出）。供 run_exe[] 场景的 AppLifecycleStack.evict()
        使用。

        计划文档：docs/plans/archive/01-engine-lifecycle-实施纪要.md
        """
        logger.info("[MacAppKit] 终止应用（不重启）: %s", self._app_name)
        await self.close()
        await self._run_sync(self._sync_kill)

    def _sync_kill(self) -> None:
        """用 pkill -x 精确终止主进程（macOS），等待完全退出。"""
        proc_name = self._app_name
        if sys.platform == "darwin":
            result = subprocess.run(
                ["pkill", "-x", proc_name],
                capture_output=True,
            )
            if result.returncode == 0:
                logger.info("[MacAppKit] pkill -x '%s' 成功，轮询等待退出", proc_name)
                # 轮询进程是否退出（替代固定 2s 硬等，通常 <0.5s 即退出）
                for _ in range(20):
                    check = subprocess.run(
                        ["pgrep", "-x", proc_name],
                        capture_output=True,
                    )
                    if check.returncode != 0:  # 进程已不存在
                        break
                    time.sleep(0.1)
                else:
                    logger.warning("[MacAppKit] pkill 后 2s 进程仍存在，继续执行")
            else:
                logger.warning("[MacAppKit] pkill -x '%s' 未找到进程（可能已退出）", proc_name)
        else:
            # Windows / Linux：psutil 按名称杀
            try:
                import psutil
                for proc in psutil.process_iter(["pid", "name", "exe"]):
                    try:
                        pname = proc.info.get("name") or ""
                        if "Helper" in pname:
                            continue
                        exe = proc.info.get("exe") or ""
                        if proc_name in pname and "/MacOS/" not in exe:
                            proc.kill()
                            logger.info("[MacAppKit] 已终止 PID=%s (%s)", proc.pid, pname)
                    except Exception:
                        continue
                time.sleep(1.5)
            except ImportError:
                logger.warning("[MacAppKit] psutil 不可用，无法终止进程")

    def _resolve_app_bundle_dir(self) -> Optional[str]:
        """从 self._app_path（可执行文件路径）反查 .app 包目录。

        例如 ".../Foo.app/Contents/MacOS/Foo" → ".../Foo.app"。
        self._app_path 未配置或路径中不含 .app 目录时返回 None。
        """
        if not self._app_path:
            return None
        parts = self._app_path.split("/")
        for i, p in enumerate(parts):
            if p.endswith(".app"):
                return "/".join(parts[: i + 1])
        return None

    def _clear_quarantine_if_present(self) -> None:
        """启动前清除目标 .app 包的 com.apple.quarantine 隔离标记（若存在）。

        根因：从浏览器/网络下载安装的 .app 会被 macOS 打上隔离标记，首次
        打开时无论签名/公证是否合法都会弹出 CoreServicesUIAgent 承载的
        "确定要打开从互联网下载的应用吗"确认框，需人工点击"打开"一次性
        放行；自动化测试场景下无人交互，弹窗超时后应用启动直接失败（真机
        复现见 docs/plans/archive/01-engine-lifecycle-实施纪要.md）。

        best-effort：属性不存在（未被隔离/已清除过）时 `xattr -d` 返回非
        零，不视为错误；.app 路径无法解析（未配置 app_path）时直接跳过，
        不阻断正常的"仅按 bundle_id 附着已运行实例"路径。仅清除顶层 .app
        包本身的属性（非递归 -r），未来若发现内嵌 LoginItems/Frameworks
        子 .app 也需要单独清除，再按需扩展。
        """
        bundle_dir = self._resolve_app_bundle_dir()
        if not bundle_dir or not os.path.exists(bundle_dir):
            return
        try:
            subprocess.run(
                ["xattr", "-d", "com.apple.quarantine", bundle_dir],
                capture_output=True, check=False,
            )
        except Exception as exc:
            logger.debug("[MacAppKit] 清除隔离标记失败（忽略，不阻断启动）: %s", exc)

    def _open_via_launchservices(self) -> None:
        """通过 LaunchServices（open -a）拉起应用，避免 TCC 重复授权弹窗。

        直接用 subprocess.Popen([app_path]) 启动时，父进程是 Python/Terminal，
        macOS TCC 守护进程会认为是"外部程序代劳启动"并重新校验辅助功能权限，
        导致每次重启都弹系统授权对话框。

        改用 `open -a <app_path>` 让 LaunchServices 代为拉起，
        TCC 认为是用户主动触发，已授权应用不会再次弹窗。
        """
        if self._bundle_id:
            # 优先用 bundle_id，更精确
            cmd = ["open", "-b", self._bundle_id]
        elif self._app_path:
            # 取 .app 包路径（去掉 Contents/MacOS/... 部分）
            app_bundle = self._resolve_app_bundle_dir() or self._app_path
            cmd = ["open", app_bundle]
        else:
            raise RuntimeError("[MacAppKit] 无法启动：bundle_id 和 app_path 均为空")

        subprocess.run(cmd, check=False, capture_output=True)
        logger.info("[MacAppKit] open via LaunchServices: %s", " ".join(cmd))

    # ── 元素查找 ─────────────────────────────────────────────

    def _assert_launched(self) -> None:
        if self._ax_app is None or self._window is None:
            raise RuntimeError("[MacAppKit] engine 未启动，请先调用 launch()")

    def _refresh_window(self) -> None:
        """刷新 _window 引用（含窗口栈自动跟随）。

        页面切换后旧的 AXUIElementRef 句柄失效（AXErrorInvalidUIElement），
        每次查找前调用此方法重新获取窗口引用，保证 findAllR 不崩溃。

        自 R30 计划 20260724160000 起，新增窗口栈 reconcile：检测到新窗口
        出现（如弹窗打开）自动压栈切换，检测到当前窗口已关闭自动弹栈回退。
        """
        try:
            self._reconcile_window_stack()
        except Exception as exc:
            logger.debug("[MacAppKit] _reconcile_window_stack 失败: %s，回退单窗口刷新", exc)
            try:
                self._window = self._get_window()
            except Exception as exc2:
                logger.debug("[MacAppKit] _refresh_window 失败: %s", exc2)

    def _reconcile_window_stack(self) -> None:
        """窗口栈 reconcile：弹窗打开自动压栈，弹窗关闭自动弹栈回退。

        见 docs/plans/archive/01-engine-lifecycle-实施纪要.md。

        **仅跟随主进程（self._ax_app）派生的窗口**（如设置面板/确认对话框），
        不纳入伴生进程（_companion_bundle_ids）窗口——伴生进程窗口（如 AEAgentMonitor
        的隐私保护提示通知）可能因任意截图副作用在任意 step 意外出现，若纳入压栈候选
        会导致 self._window 被意外顶替为伴生窗口，使主窗口在此期间从 AX_OFFSET 定位/
        get_window_rect()/L4 自愈坐标反查等范围内彻底消失。伴生进程元素查找/点击已由
        _search_windows()/_attach_satellites()（主窗口优先、miss 才回退伴生窗口，且不
        改变 self._window）完整覆盖，二者功能不重叠。见 docs/plans/
        archive/01-engine-lifecycle-实施纪要.md（收窄本机制，
        取代 docs/plans/20260729220000 中曾引入的伴生窗口压栈逻辑）。

        **引用刷新（2026-07-31 补丁，见 docs/plans/
        archive/01-engine-lifecycle-实施纪要.md）**：click 等操作可能
        在**同一个**窗口内部触发 UI 变更（如设置面板上弹出"确认重启"AXSheet——
        这类系统 alert 常以 Sheet 形式挂在父窗口的 AXChildren 下，而非独立
        AXWindow，不会被下方"新窗口检测"感知），此时缓存的旧 AXUIElementRef
        在变更瞬间可能短暂不可靠。因此即使栈顶窗口"仍在"（未触发压栈/弹栈），
        也会用本次 `app.windows()` 刚获取到的最新引用替换缓存引用（步骤③），
        避免后续 find_by_id/click 命中失败。
        """
        if not self._window_stack:
            # 尚未初始化（未走过 launch 流程的异常路径），退化为单窗口逻辑
            self._window = self._get_window()
            self._window_stack = [(self._window, self._safe_title(self._window))]
            return

        try:
            wins = list(self._ax_app.windows())
        except Exception:
            wins = []  # ax_app 本身失效（如旧 PID 已死），交给下方重连兜底处理

        # 0. 应用自重启兜底（2026-07-31，见 docs/plans/20260731110000-engine-
        #    auto-reconnect-after-app-self-restart.md）：tcs 用例执行期间被测
        #    app 可能不经过 restart()/launch() 自行重启（如切换语言触发的
        #    自重启），新进程持有新 PID，旧 self._ax_app/self._window 引用会
        #    静默失效——windows() 恒返回空列表但不报错。此时尝试按 bundle_id
        #    重新附着一次；成功则用新 ax_app/window 替换现有状态并继续走下方
        #    reconcile；失败（app 可能仍在重启过程中）则原样返回，不改变现有
        #    栈状态，交给下次调用（tcs wait[]/assert[timeout=] 天然重试）继续
        #    探测。正常场景（app 未重启，wins 本就非空）不触发，零回归。
        if not wins:
            if not self._try_reattach():
                return
            wins = list(self._ax_app.windows())

        # 1. 回退检测：栈顶窗口若已不在当前 windows() 中，逐层弹出（保留栈底）
        while len(self._window_stack) > 1:
            top_ref, _ = self._window_stack[-1]
            if any(self._same_window(top_ref, w) for w in wins):
                break
            self._window_stack.pop()
            logger.info(
                "[MacAppKit] 检测到窗口关闭，回退到上一窗口 (剩余栈深=%d)",
                len(self._window_stack),
            )

        # 2. 新窗口检测：wins 中找一个不在栈内任何层的窗口 → 压栈（每次只处理一个，
        #    多重弹窗靠下次调用继续检测）
        known_refs = [ref for ref, _ in self._window_stack]
        for w in wins:
            if not any(self._same_window(w, ref) for ref in known_refs):
                title = self._safe_title(w)
                self._window_stack.append((w, title))
                logger.info(
                    "[MacAppKit] 检测到新窗口出现，自动切换: %r (栈深=%d)",
                    title, len(self._window_stack),
                )
                break

        # 3. 引用刷新：栈顶窗口即使未发生压栈/弹栈，也用本次 wins 中匹配到的
        #    最新引用替换缓存引用（见上方文档字符串"引用刷新"说明）。对既有
        #    mock 单测零影响——mock 场景下"仍是同一窗口"复用同一对象，
        #    fresh is top_ref 恒成立，替换等价于不替换。
        top_ref, top_title = self._window_stack[-1]
        fresh = next((w for w in wins if self._same_window(top_ref, w)), None)
        if fresh is not None:
            self._window_stack[-1] = (fresh, top_title)

        self._window = self._window_stack[-1][0]

    def _try_reattach(self) -> bool:
        """被测应用疑似自重启（旧 AXUIElementRef 绑定的旧 PID 已失效，
        `self._ax_app.windows()` 静默返回空列表）时，按 `bundle_id` 重新查询
        一次 AX app ref。若新 ref 能查到非空窗口，说明应用已用新 PID 重新
        拉起完成：替换 `_ax_app`，用 `_select_main_window()` 重新选主窗口、
        重建单层 `_window_stack`、重新附着卫星进程。

        见 docs/plans/20260731110000-engine-auto-reconnect-after-app-self-
        restart.md。best-effort，失败返回 False，不抛异常，不改变现有状态
        （由调用方 `_reconcile_window_stack()` 保持原样，交给下次调用重试）。
        """
        if not self._bundle_id:
            return False
        try:
            new_ax_app = atomacos.getAppRefByBundleId(self._bundle_id)
            wins = list(new_ax_app.windows())
            if not wins:
                return False
            self._ax_app = new_ax_app
            self._window = self._select_main_window(wins)
            self._window_stack = [(self._window, self._safe_title(self._window))]
            self._attach_satellites()
            logger.info(
                "[MacAppKit] %s 窗口列表已空（疑似应用自重启），重新附着成功",
                self._app_name,
            )
            return True
        except Exception as exc:
            logger.debug("[MacAppKit] 重新附着尝试失败: %s", exc)
            return False

    @staticmethod
    def _same_window(a, b) -> bool:
        """窗口引用同一性比较：优先 identity/== ，异常时退化为标题比较兜底。

        已知风险：AXUIElementRef.__eq__ 若不可靠反映原生 CFEqual 同一性，
        退化到标题比较时同标题多窗口场景可能误判，需真机验证。
        """
        try:
            if a == b:
                return True
        except Exception:
            pass
        try:
            return MacAppKitEngine._safe_title(a) == MacAppKitEngine._safe_title(b)
        except Exception:
            return False

    @staticmethod
    def _safe_title(w) -> str:
        try:
            return str(w.AXTitle or "")
        except Exception:
            return ""

    async def find_by_id(self, element_id: str) -> Optional[ElementRef]:
        """element_id 为 AXPath 字符串，如 'AXButton[title=快速检索]'。

        支持 scoped AXPath（2026-07-29 新增）：
          `AXWindow[subrole=AXUnknown]/AXButton[title=快速检索]`
        scope 段在主+卫星窗口集合中筛选，target 段逐窗查询。
        非 scoped：主窗口命中即返回（既有行为零变化），miss 才查卫星。
        """
        self._assert_launched()

        # AX_OFFSET 偏移定位（AX 不可见元素，如开关按钮）—— 仅主窗口作用域
        if is_ax_offset(element_id):
            def _do_offset_query():
                self._refresh_window()
                return query_element_with_offset(self._window, element_id)
            raw = await self._run_sync(_do_offset_query)
            if raw is None:
                return None
            return ElementRef(element_id=element_id, raw=raw)

        if not is_axpath(element_id):
            # 非 AXPath 格式，回退到文本搜索
            return await self.find_by_text(element_id)
        try:
            spec: AXPathSpec = parse_axpath(element_id)
        except AXPathError as exc:
            logger.warning("[MacAppKit] AXPath 解析错误: %s", exc)
            return None

        def _do_query():
            self._refresh_window()
            windows = self._search_windows()
            if is_scoped_axpath(element_id):
                return query_element_in_windows(windows, element_id)
            # 非 scoped：主窗口优先（零行为变化），miss 才查卫星
            primary = query_element(self._window, spec)
            if primary is not None:
                return primary
            for w in windows[1:]:
                result = query_element(w, spec)
                if result is not None:
                    return result
            return None

        raw = await self._run_sync(_do_query)
        if raw is None:
            return None
        return ElementRef(element_id=element_id, raw=raw)

    async def find_by_text(self, text: str) -> Optional[ElementRef]:
        """在所有 AXStaticText 和 AXButton 中按文本部分匹配。

        主窗口优先（既有行为零变化），miss 才查卫星进程窗口。
        """
        self._assert_launched()

        def _search_in(window) -> Optional[object]:
            # 搜索 AXStaticText（value）
            for elem in window.findAllR(AXRole="AXStaticText"):
                try:
                    if text in (elem.AXValue or ""):
                        return elem
                except Exception:
                    pass
            # 搜索 AXButton（title）
            for elem in window.findAllR(AXRole="AXButton"):
                try:
                    if text in (elem.AXTitle or ""):
                        return elem
                except Exception:
                    pass
            return None

        def _search() -> Optional[object]:
            self._refresh_window()
            primary = _search_in(self._window)
            if primary is not None:
                return primary
            for w in self._search_windows()[1:]:
                result = _search_in(w)
                if result is not None:
                    return result
            return None

        raw = await self._run_sync(_search)
        if raw is None:
            return None
        return ElementRef(element_id=f"text={text}", raw=raw, text=text)

    async def find_by_role(self, role: str, name: str = "") -> Optional[ElementRef]:
        """按 AX role 查找，name 对应 AXTitle。

        主窗口优先（既有行为零变化），miss 才查卫星进程窗口。
        """
        self._assert_launched()
        ax_role = role if role.startswith("AX") else f"AX{role.capitalize()}"

        def _search_in(window):
            if name:
                results = window.findAllR(AXRole=ax_role, AXTitle=name)
            else:
                results = window.findAllR(AXRole=ax_role)
            return results[0] if results else None

        def _search():
            self._refresh_window()
            primary = _search_in(self._window)
            if primary is not None:
                return primary
            for w in self._search_windows()[1:]:
                result = _search_in(w)
                if result is not None:
                    return result
            return None

        raw = await self._run_sync(_search)
        if raw is None:
            return None
        return ElementRef(element_id=f"role={ax_role}", raw=raw)

    async def find_all_by_role(self, role: str) -> list[ElementRef]:
        """主窗口+所有卫星进程窗口的全部同名 role 元素（去重同 ref）。"""
        self._assert_launched()
        ax_role = role if role.startswith("AX") else f"AX{role.capitalize()}"

        def _search():
            self._refresh_window()
            seen = set()
            out = []
            for w in self._search_windows():
                try:
                    results = w.findAllR(AXRole=ax_role) or []
                except Exception:
                    continue
                for r in results:
                    key = id(r)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(r)
            return out

        results = await self._run_sync(_search) or []
        return [
            ElementRef(element_id=f"role={ax_role}[{i}]", raw=r)
            for i, r in enumerate(results)
        ]

    # ── Actions ──────────────────────────────────────────────

    async def click(self, element: ElementRef) -> None:
        await self._run_sync(self._sync_click, element.raw)

    def _sync_click(self, raw) -> None:
        # WebView 的无文字图标会作为 AXStaticText 暴露，Press() 可能静默成功但不
        # 触发 DOM click；这类节点必须使用其 AX 坐标发出真实鼠标事件。
        force_coordinate_click = getattr(raw, "AXRole", "") == "AXStaticText"
        if not force_coordinate_click:
            try:
                raw.Press()
                return
            except Exception:
                pass

        # 兜底/AXStaticText：CGEvent 坐标点击（AXImage 等不可 Press 的元素）。
        pos = getattr(raw, "AXPosition", None)
        size = getattr(raw, "AXSize", None)
        if pos is None or size is None:
            logger.warning("[MacAppKit] _sync_click: 元素无 AXPosition/AXSize，跳过")
            return
        cx = pos.x + size.width / 2
        cy = pos.y + size.height / 2
        # VirtualElement（AX_OFFSET 生成）需更长 hold：SampleApp SwiftUI toggle
        # 在 hold<100ms 时不响应（真机验证 50ms 失败、200ms 成功）。
        is_virtual = type(raw).__name__ == "VirtualElement"
        hold = 200 if is_virtual else 50
        self._cgclick(cx, cy, hold_ms=hold)
        time.sleep(0.1)

    @staticmethod
    def _cgclick(x: float, y: float, hold_ms: int = 50) -> None:
        """通过 CGEvent 模拟鼠标点击（前置 mouseMove + hold 间隔，提高可靠性）。"""
        try:
            import Quartz  # type: ignore
            # 前置 mouseMove：激活目标窗口焦点，避免点击被忽略
            move_event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved,
                Quartz.CGPoint(x=x, y=y), Quartz.kCGMouseButtonLeft,
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, move_event)
            time.sleep(0.03)
            # mouseDown
            event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseDown,
                Quartz.CGPoint(x=x, y=y), Quartz.kCGMouseButtonLeft,
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            # hold（避免 down-up 过快被系统丢弃）
            time.sleep(hold_ms / 1000.0)
            # mouseUp
            event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventLeftMouseUp,
                Quartz.CGPoint(x=x, y=y), Quartz.kCGMouseButtonLeft,
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        except ImportError:
            # Quartz 不可用，使用 osascript 兜底
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to click at {{{int(x)}, {int(y)}}}'],
                check=False, capture_output=True,
            )

    # ── 屏幕坐标驱动（device-exec 多模态定位用；R3：引擎专用能力放具体类）──
    async def click_at_screen_coords(self, x: float, y: float) -> None:
        """在**屏幕绝对坐标**（points）点击；坐标由调用方按实测 scale 换算。"""
        await self._run_sync(self._cgclick, float(x), float(y))

    async def hover_at_screen_coords(self, x: float, y: float) -> None:
        await self._run_sync(self._sync_move_at_screen_coords, float(x), float(y))

    async def type_text_at_screen_coords(self, x: float, y: float, text: str) -> None:
        """先在该坐标点击聚焦，再向当前焦点键入（不改写 AXValue，走键盘事件）。"""
        await self._run_sync(self._cgclick, float(x), float(y))
        await self.type_focused(text)

    def _sync_move_at_screen_coords(self, x: float, y: float) -> None:
        try:
            import Quartz  # type: ignore
            event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved,
                Quartz.CGPoint(x=x, y=y), Quartz.kCGMouseButtonLeft,
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        except ImportError:
            subprocess.run(
                ["osascript", "-e",
                 f'tell application "System Events" to set the position of the mouse to {{{int(x)}, {int(y)}}}'],
                check=False, capture_output=True,
            )

    async def type_text(self, element: ElementRef, text: str, clear: bool = True) -> None:
        await self._run_sync(self._sync_type, element.raw, text, clear)

    def _sync_type(self, raw, text: str, clear: bool) -> None:
        # 先点击聚焦，触发 App UI 响应机制（搜索框等控件需要 focus 才会响应 AXValue 变化）
        try:
            pos = raw.AXPosition
            size = raw.AXSize
            cx = pos.x + size.width / 2
            cy = pos.y + size.height / 2
            self._cgclick(cx, cy)
            time.sleep(0.15)
        except Exception:
            pass

        if clear:
            try:
                raw.AXValue = ""
                time.sleep(0.05)
            except Exception:
                pass
        try:
            raw.AXValue = text
        except Exception:
            # 兜底：键盘输入
            try:
                raw.Press()
            except Exception:
                pass
            atomacos.keyboard.typewrite(text)

    async def type_focused(self, text: str, *, via_clipboard: bool = False) -> None:
        """向当前焦点控件键入文本，不改写 AXValue。"""
        await self._run_sync(self._sync_type_focused, text, via_clipboard)

    def _sync_type_focused(self, text: str, via_clipboard: bool) -> None:
        if via_clipboard:
            subprocess.run(["pbcopy"], input=text, text=True, check=True)
            atomacos.keyboard.hotkey("command", "v")
            return
        atomacos.keyboard.typewrite(text)

    async def press_key(self, key: str) -> None:
        await self._run_sync(self._sync_press_key, key)

    def _sync_press_key(self, key: str) -> None:
        # 组合键（如 "command+w"/"command+,"）：用 atomacos.keyboard.hotkey
        # 真机同时按下多个键，真机已验证可用（见 _sync_goto_path_in_file_dialog
        # 的 hotkey("command", "shift", "g") 先例）。
        if "+" in key:
            parts = [p.strip().lower() for p in key.split("+")]
            atomacos.keyboard.hotkey(*parts)
            return
        # 单键（Escape/Enter/Tab/...）：必须用 press() 走 pyautogui 的
        # KEY_NAMES 映射（typewrite("escape")/typewrite("\x1b") 都会被吞，
        # 因为 typewrite 是模拟文本输入，特殊字符在 pyautogui 路径中失效）。
        # 2026-07-29 真机验证：手工按 Esc 可关轻页面 NSPopover，但
        # typewrite("\x1b") 程序发不出 Esc 事件；改 press("escape") 修复。
        _KEY_MAP = {
            "Enter": "enter", "Return": "enter",
            "Tab": "tab", "Escape": "escape", "Esc": "escape",
            "Space": "space", "Backspace": "backspace",
            "Delete": "delete",
        }
        key_name = _KEY_MAP.get(key, key.lower())
        atomacos.keyboard.press(key_name)

    async def hover(self, element: ElementRef) -> None:
        await self._run_sync(self._sync_hover, element.raw)

    def _sync_hover(self, raw) -> None:
        try:
            pos = raw.AXPosition
            size = raw.AXSize
            cx = pos.x + size.width / 2
            cy = pos.y + size.height / 2
            try:
                import Quartz  # type: ignore
                event = Quartz.CGEventCreateMouseEvent(
                    None, Quartz.kCGEventMouseMoved,
                    Quartz.CGPoint(x=cx, y=cy), Quartz.kCGMouseButtonLeft,
                )
                Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
            except ImportError:
                pass
        except Exception as exc:
            logger.debug("[MacAppKit] hover 失败: %s", exc)

    async def scroll(self, element: Optional[ElementRef], direction: str, amount: int) -> None:
        """element 可为 None（scroll[direction] 无元素名调用，滑动当前窗口，
        见 docs/plans/archive/02-dsl-actions-实施纪要.md）。"""
        raw = element.raw if element is not None else None
        await self._run_sync(self._sync_scroll, raw, direction, amount)

    def _sync_scroll(self, raw, direction: str, amount: int) -> None:
        try:
            if raw is not None:
                pos = raw.AXPosition
                size = raw.AXSize
            elif self._window is not None:
                # 无元素名调用（raw=None）→ 回退当前活跃窗口（栈顶）坐标。
                pos = self._window.AXPosition
                size = self._window.AXSize
            else:
                return
            cx = pos.x + size.width / 2
            cy = pos.y + size.height / 2
            # Quartz 滚轮方向：负 dy = 内容向上移动（用户视觉上的向下滚动），
            # 正 dy = 内容向下移动（向上滚动）。旧实现符号相反，导致
            # scroll[down] 在列表顶部实际不动，Safari 等底部项永远不可见。
            dy = amount if direction == "up" else -amount
            dx = -amount if direction == "left" else (amount if direction == "right" else 0)
            dy = 0 if direction in ("left", "right") else dy
            import Quartz  # type: ignore
            event = Quartz.CGEventCreateScrollWheelEvent(
                None, Quartz.kCGScrollEventUnitPixel, 2,
                int(dy / 10), int(dx / 10),
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        except Exception as exc:
            logger.debug("[MacAppKit] scroll 失败: %s", exc)

    # ── 断言辅助 ─────────────────────────────────────────────

    async def get_text(self, element: ElementRef) -> str:
        def _get():
            raw = element.raw
            try:
                v = raw.AXValue
                if v:
                    return str(v)
            except Exception:
                pass
            try:
                return str(raw.AXTitle or "")
            except Exception:
                return ""
        return await self._run_sync(_get)

    async def get_attribute(self, element: ElementRef, attr: str) -> Optional[str]:
        def _get():
            ax_attr = attr if attr.startswith("AX") else f"AX{attr.capitalize()}"
            try:
                return str(getattr(element.raw, ax_attr, None) or "")
            except Exception:
                return None
        return await self._run_sync(_get)

    async def is_visible(self, element: ElementRef) -> bool:
        def _check():
            try:
                hidden  = getattr(element.raw, "AXHidden", None)
                enabled = getattr(element.raw, "AXEnabled", None)
                # None 表示属性不存在：hidden=None → 视为未隐藏；enabled=None → 视为可用
                is_hidden  = bool(hidden)  if hidden  is not None else False
                is_enabled = bool(enabled) if enabled is not None else True
                result = (not is_hidden) and is_enabled
                logger.debug(
                    "[is_visible] id=%s hidden=%r enabled=%r → %s",
                    getattr(element.raw, "AXTitle", "?"), hidden, enabled, result
                )
                return result
            except Exception as exc:
                logger.debug("[is_visible] exception: %s", exc)
                return False
        return await self._run_sync(_check)

    async def is_hidden(self, element: ElementRef) -> bool:
        """判断元素是否被显式隐藏（AXHidden=True）。

        与 is_visible 的区别：本方法**只看 AXHidden，不看 AXEnabled**。
        - disabled 置灰控件（enabled=False, hidden 非 True）：仍渲染在屏幕上，
          is_hidden 返回 False（未隐藏）。
        - 显式隐藏元素（AXHidden=True）：is_hidden 返回 True。

        供 assert[..., visible=false] 精准判定"存在且未被隐藏（允许 disabled）"
        使用——既能断言置灰控件存在，又不掩盖真正被隐藏的元素。
        见 core/actions/judgers/existence.py 的 visible=false 分支。
        """
        if element.raw is None:
            # 纯坐标句柄（L3.8/L4 视觉定位）：视觉已确认可见 → 未隐藏
            return False

        def _check():
            try:
                hidden = getattr(element.raw, "AXHidden", None)
                result = bool(hidden) if hidden is not None else False
                logger.debug(
                    "[is_hidden] id=%s hidden=%r → %s",
                    getattr(element.raw, "AXTitle", "?"), hidden, result
                )
                return result
            except Exception as exc:
                logger.debug("[is_hidden] exception: %s", exc)
                return False

        return await self._run_sync(_check)

    async def get_element_rect(self, element) -> Optional[tuple]:
        """返回元素的**屏幕绝对逻辑坐标** (x, y, w, h)，单位为 points。

        标注时需要结合 get_window_rect() 换算为截图内的像素坐标。
        """
        def _get():
            try:
                pos  = element.raw.AXPosition
                size = element.raw.AXSize
                return (int(pos.x), int(pos.y), int(size.width), int(size.height))
            except Exception:
                return None
        return await self._run_sync(_get)

    async def get_window_rect(self) -> Optional[tuple]:
        """返回应用主窗口的**屏幕绝对逻辑坐标** (x, y, w, h)，单位为 points。

        供 annotator 将元素绝对坐标换算为截图内相对坐标。
        """
        def _get():
            try:
                if self._window is None:
                    return None
                pos  = self._window.AXPosition
                size = self._window.AXSize
                return (int(pos.x), int(pos.y), int(size.width), int(size.height))
            except Exception:
                return None
        return await self._run_sync(_get)

    async def get_last_screenshot_rect(self) -> Optional[tuple]:
        """最近一次**窗口**截图实际覆盖的屏幕区域 (x, y, w, h)，points（引擎专用能力，R3）。

        与 get_window_rect() 的区别：截图按『当前操作窗口』选 CGWindow（Sheet 优先、匹配失败
        兜底面积最大），可能与 self._window 不是同一窗口；把截图像素换算回屏幕坐标时必须用
        截图本身的区域。全屏降级或未截图返回 None。
        """
        rect = self._last_screenshot_rect
        return tuple(rect) if rect else None

    async def get_screen_rect(self) -> Optional[tuple]:
        """主屏矩形 ``(0, 0, w, h)``，points（引擎专用能力，R3）。

        `--device-fullscreen` 下的坐标基准：全屏截图按设备像素采集，换算成点击坐标
        需要「主屏 points 尺寸」建立比例。取不到返回 ``None``（由 coords 判缺口，
        不猜偏移）；多显示器一期只取主屏。
        """
        def _get():
            try:
                from AppKit import NSScreen

                frame = NSScreen.mainScreen().frame()
                width, height = int(frame.size.width), int(frame.size.height)
                if width <= 0 or height <= 0:
                    return None
                return (0, 0, width, height)
            except Exception as exc:  # noqa: BLE001 - 取不到即视为能力缺口
                logger.debug("[MacAppKit] get_screen_rect 失败: %s", exc)
                return None

        return await self._run_sync(_get)

    async def take_fullscreen_screenshot(self, path: str = "") -> Optional[bytes]:
        """整屏截图（`--device-fullscreen` 专用；窗口模式不走这里）。

        失败返回 ``b""`` 而不是抛异常：调用方按「空图 = 本步证据缺失」处理，
        与窗口截图失败路径同口径。成功时 ``_last_screenshot_is_window=False``，
        表示这张图不是窗口图（窗口模式会据此拒收，屏幕模式允许）。
        """
        def _capture():
            target = path or os.path.join(tempfile.gettempdir(), "_uia_mac_fullscreen.png")
            os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
            try:
                subprocess.run(
                    ["screencapture", "-x", target],
                    check=True, capture_output=True, timeout=10,
                )
                if not os.path.exists(target) or os.path.getsize(target) == 0:
                    raise RuntimeError("screencapture 输出文件为空")
                with open(target, "rb") as f:
                    raw = f.read()
                if not path:
                    os.unlink(target)
                return raw
            except Exception as exc:  # noqa: BLE001 - 截图失败不抛栈
                logger.warning("[MacAppKit] 全屏截图失败: %s", exc)
                return b""
            finally:
                self._last_screenshot_is_window = False
                self._last_screenshot_rect = None

        return await self._run_sync(_capture, timeout=self._SCREENSHOT_TIMEOUT)

    @staticmethod
    def _cg_window_bounds(wid: Optional[int]) -> Optional[tuple]:
        """CGWindowID → kCGWindowBounds (x, y, w, h)，points；取不到返回 None。"""
        if wid is None:
            return None
        try:
            import Quartz  # type: ignore
            infos = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionIncludingWindow, wid,
            )
            for info in list(infos or []):
                if info.get("kCGWindowNumber") != wid:
                    continue
                bounds = info.get("kCGWindowBounds") or {}
                w = float(bounds.get("Width", 0) or 0)
                h = float(bounds.get("Height", 0) or 0)
                if w > 0 and h > 0:
                    return (float(bounds.get("X", 0) or 0), float(bounds.get("Y", 0) or 0), w, h)
        except Exception as exc:
            logger.debug("[MacAppKit] 读取窗口边界失败 wid=%s: %s", wid, exc)
        return None

    async def get_window_title(self) -> str:
        self._assert_launched()
        def _get():
            try:
                return str(self._window.AXTitle or "")
            except Exception:
                return ""
        return await self._run_sync(_get)

    async def refresh_active_window(self) -> None:
        """刷新窗口栈引用（触发 _reconcile_window_stack），供 scroll[direction]
        无元素名调用场景使用（无 resolver.resolve() 可依赖时的等价触发点，
        见 docs/plans/archive/02-dsl-actions-实施纪要.md）。"""
        await self._run_sync(self._refresh_window)

    # ── 截图 ─────────────────────────────────────────────────

    # 最后一次截图是否为窗口截图（供标注系统判断坐标系）
    _last_screenshot_is_window: bool = False
    # 最后一次窗口截图实际覆盖的屏幕区域 (x, y, w, h)，points；全屏降级/未截图为 None。
    # 截图可能取 Sheet/对话框等衍生窗口（见 _get_app_window_id），与 get_window_rect()
    # 返回的 self._window 不一定是同一块，坐标换算须以它为准。
    _last_screenshot_rect: Optional[tuple] = None

    # 截图总超时（秒）：L1 Quartz（同步调用，通常毫秒级，但无自带超时）+
    # L2/L3 screencapture 子进程各自 timeout=10 + 余量。2026-08-21 新增，见
    # 计划文档 20260820160000-mac-win-engine-screenshot-hang-isolation-and-
    # shared-bounded-executor.md：此前 take_screenshot() 完全没有外层超时
    # 保护，L1 CGWindowListCreateImage 一旦卡死（如 WindowServer 异常）会
    # 无限期挂起，没有任何机制能打断。
    _SCREENSHOT_TIMEOUT = 21

    async def take_screenshot(self, path: str = "") -> Optional[bytes]:
        """截取被测应用窗口（无阴影），降级为全屏。

        优先使用 CGWindowListCreateImage + kCGWindowImageBoundsIgnoreFraming
        直接从 Quartz 获取无阴影窗口图，坐标系与 AXPosition 完全对齐。
        CGWindowListCreateImage 不可用时降级为全屏 screencapture。
        """
        def _capture():
            target = path or os.path.join(tempfile.gettempdir(), "_uia_mac_screenshot.png")
            target_dir = os.path.dirname(os.path.abspath(target))
            os.makedirs(target_dir, exist_ok=True)
            self._last_screenshot_rect = None

            # L1: Quartz CGWindowListCreateImage（无阴影，坐标精确）
            data = self._capture_via_quartz(target)
            if data:
                return data

            # L2: screencapture -l -o（-o 去窗口阴影，图像边界 = 窗口边界）
            wid = self._get_app_window_id()
            if wid is not None:
                try:
                    ret = subprocess.run(
                        ["screencapture", "-x", "-o", "-l", str(wid), target],
                        capture_output=True, timeout=10,
                    )
                    if ret.returncode == 0 and os.path.exists(target) and os.path.getsize(target) > 0:
                        logger.debug("[MacAppKit] screencapture 窗口截图 wid=%s", wid)
                        self._last_screenshot_is_window = True
                        self._last_screenshot_rect = self._cg_window_bounds(wid)
                        with open(target, "rb") as f:
                            raw = f.read()
                        if not path:
                            os.unlink(target)
                        return raw
                except Exception as exc:
                    logger.debug("[MacAppKit] screencapture -l 失败: %s", exc)

            # L3: 全屏 screencapture
            try:
                self._last_screenshot_is_window = False
                subprocess.run(
                    ["screencapture", "-x", target],
                    check=True, capture_output=True, timeout=10,
                )
                if not os.path.exists(target) or os.path.getsize(target) == 0:
                    raise RuntimeError("screencapture 输出文件为空")
                with open(target, "rb") as f:
                    raw = f.read()
                if not path:
                    os.unlink(target)
                return raw
            except Exception as exc:
                logger.warning("[MacAppKit] screencapture 全屏失败: %s", exc)
                self._last_screenshot_is_window = False
                return b""

        result = await self._run_sync(_capture, timeout=self._SCREENSHOT_TIMEOUT)
        # _run_sync 超时时返回 None（BoundedExecutor 语义），映射为既有的
        # "截图失败" 语义 b""，与内部各降级分支失败时的返回值保持一致。
        return result if result is not None else b""

    def _capture_via_quartz(self, target_path: str) -> Optional[bytes]:
        """用 CGWindowListCreateImage + CGBitmapContext 截无阴影窗口图。

        kCGWindowImageBoundsIgnoreFraming 去除阴影/边框，
        截图坐标原点 = 窗口内容左上角，与 AXPosition/AXSize 完全对齐，
        不需要任何阴影偏移修正。

        输出为 RGB PNG（无透明通道），mode="RGB"。

        健壮性前置：macOS 10.15+ 要求调用进程持有『屏幕录制』TCC 权限，
        权限缺失时 CGWindowListCreateImage 不会报错，而是静默返回桌面壁纸
        （见 docs/plans/20260817150000-mac-screenshot-desktop-background-
        permission-fix.md）。此处先做权限预检，缺失则直接短路返回 None，
        交由调用方走 L2/L3 兜底（同样受权限影响会继续失败，但避免本级做
        一次注定产出错误图像的徒劳调用，并保证问题可通过日志诊断）。
        """
        if not _check_screen_capture_access():
            return None
        try:
            import ctypes
            import Quartz  # type: ignore

            wid = self._get_app_window_id()
            if wid is None:
                return None

            cg_img = Quartz.CGWindowListCreateImage(
                Quartz.CGRectNull,
                Quartz.kCGWindowListOptionIncludingWindow,
                wid,
                Quartz.kCGWindowImageBoundsIgnoreFraming,
            )
            if cg_img is None:
                return None

            img_w = Quartz.CGImageGetWidth(cg_img)
            img_h = Quartz.CGImageGetHeight(cg_img)
            if img_w == 0 or img_h == 0:
                return None

            # CGImage → 像素 bytes（RGBA 排列，premultiplied）
            bytes_per_row = img_w * 4
            buf = (ctypes.c_uint8 * (bytes_per_row * img_h))()
            cs = Quartz.CGColorSpaceCreateDeviceRGB()
            ctx = Quartz.CGBitmapContextCreate(
                buf, img_w, img_h, 8, bytes_per_row, cs,
                Quartz.kCGImageAlphaPremultipliedLast | Quartz.kCGBitmapByteOrder32Big,
            )
            if ctx is None:
                return None
            Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, img_w, img_h), cg_img)

            # 转为 Pillow RGB 图并保存 PNG
            try:
                import numpy as np  # type: ignore
                arr = np.frombuffer(bytes(buf), dtype=np.uint8).reshape((img_h, img_w, 4))
                from PIL import Image as _Img
                img = _Img.fromarray(arr[:, :, :3], "RGB")
            except ImportError:
                # numpy 不可用：逐像素转换（慢但功能正确）
                from PIL import Image as _Img
                raw = bytes(buf)
                img = _Img.frombytes("RGBA", (img_w, img_h), raw).convert("RGB")

            img.save(target_path)
            if not os.path.exists(target_path) or os.path.getsize(target_path) == 0:
                return None

            self._last_screenshot_is_window = True
            self._last_screenshot_rect = self._cg_window_bounds(wid)
            logger.debug("[MacAppKit] Quartz 无阴影截图: %dx%d → %s", img_w, img_h, target_path)
            with open(target_path, "rb") as f:
                return f.read()

        except Exception as exc:
            logger.debug("[MacAppKit] _capture_via_quartz 失败: %s", exc)
            return None

    def _get_app_window_id(self) -> Optional[int]:
        """通过 CGWindowListCopyWindowInfo 查找『当前操作窗口』的 CGWindowID。

        计划文档：docs/plans/20260731100000-mac-engine-screenshot-follow-
        current-window-fix.md

        优先按 self._window（AX 引擎当前跟踪的窗口栈栈顶，见
        _reconcile_window_stack）的 pid + AXPosition/AXSize 边界精确匹配；
        self._window 为空/无 pid/边界匹配失败时兜底回退到原有『app_name 子串
        + 面积最大』启发式（逐字保留，零回归）。

        故障动机：主窗口 + 设置面板等衍生窗口同时存在时，仅靠 app_name 匹配 +
        面积最大判据会稳定选中主窗口，即便 self._window 已经正确指向设置面板
        （当前操作对象），导致截图内容与 AX 树/点击作用的窗口不一致，标注/
        L3.8 视觉指纹/L4 视觉定位/self-heal 坐标反查全部基于错误的图。

        **Sheet 优先（2026-07-31 补丁，见 docs/plans/
        archive/01-engine-lifecycle-实施纪要.md）**：
        `self._window` 若挂了 `AXSheet` 子元素（如"语言切换需重启"确认弹窗），
        该 Sheet 在 Accessibility 层是父窗口 `AXChildren` 的逻辑子节点，但在
        WindowServer 合成层是**完全独立的 CGWindow**（真机实测：父窗口截图
        只会拿到变暗的背景，Sheet 对话框本身内容缺失）。因此在按
        `self._window` 自身边界匹配之前，先尝试按 Sheet 自身的
        pid+AXPosition/AXSize 匹配；无 Sheet 时该分支立即返回 None，零行为
        变化。
        """
        try:
            import Quartz  # type: ignore
            win_list = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID,
            )
            if not win_list:
                return None

            wid = self._match_sheet_window_id(win_list)
            if wid is not None:
                return wid

            wid = self._match_window_id_by_ax_bounds(win_list)
            if wid is not None:
                return wid

            # 兜底：原有『app_name 子串 + 面积最大』启发式（self._window 为空或
            # 精确匹配失败时使用，逐字保留原有行为）
            candidates = []
            for info in win_list:
                owner = info.get("kCGWindowOwnerName", "") or ""
                # 匹配 bundle owner name（通常是应用显示名）
                if self._app_name and self._app_name in owner:
                    wid2  = info.get("kCGWindowNumber")
                    layer = info.get("kCGWindowLayer", 99)
                    bounds = info.get("kCGWindowBounds", {})
                    w = bounds.get("Width", 0)
                    h = bounds.get("Height", 0)
                    # 只取有意义尺寸的窗口（过滤菜单栏、托盘等）
                    if w > 100 and h > 100:
                        candidates.append((layer, -w * h, wid2))
            if not candidates:
                return None
            # layer 最小（最前层）、面积最大的窗口
            candidates.sort()
            return candidates[0][2]
        except Exception as exc:
            logger.debug("[MacAppKit] _get_app_window_id 失败: %s", exc)
            return None

    def _match_window_id_by_ax_bounds(self, win_list) -> Optional[int]:
        """按 self._window 的 pid + AXPosition/AXSize 精确匹配 CGWindowID。

        坐标系一致性：AXPosition/AXSize 与 kCGWindowBounds 均为『主屏左上角为
        原点，y 向下』的全局逻辑坐标（macOS AX API 与 WindowServer 共用同一
        坐标系，与 Cocoa NSView 翻转坐标系不同），无需换算，与
        _capture_via_quartz()/get_window_rect() 现有假设一致（见 docs/plans/
        archive/10-platform-scale-实施纪要.md）。

        容差 2pt 容忍浮点舍入；PID 不同/边界差值超出容差均视为不匹配，
        返回 None 交由调用方兜底。
        """
        if self._window is None:
            return None
        try:
            target_pid = self._window.pid
            pos = self._window.AXPosition
            size = self._window.AXSize
        except Exception as exc:
            logger.debug("[MacAppKit] 读取 self._window pid/边界失败: %s", exc)
            return None

        tolerance = 2.0
        for info in win_list:
            try:
                if info.get("kCGWindowOwnerPID") != target_pid:
                    continue
                bounds = info.get("kCGWindowBounds", {})
                if (
                    abs(bounds.get("X", 0) - pos.x) <= tolerance
                    and abs(bounds.get("Y", 0) - pos.y) <= tolerance
                    and abs(bounds.get("Width", 0) - size.width) <= tolerance
                    and abs(bounds.get("Height", 0) - size.height) <= tolerance
                ):
                    return info.get("kCGWindowNumber")
            except Exception:
                continue
        return None

    def _match_sheet_window_id(self, win_list) -> Optional[int]:
        """self._window 若存在 AXSheet 子元素，按其自身 pid+AXPosition/AXSize
        精确匹配 CGWindowID（Sheet 在 WindowServer 层是独立窗口，见
        docs/plans/archive/01-engine-lifecycle-实施纪要.md）。

        多个 Sheet 同时存在时取第一个——macOS 系统级约束下同一父窗口同一时刻
        只允许一个激活 Sheet，多重弹窗场景本就不该出现。找不到 AXSheet 子
        元素或匹配失败均返回 None，交由调用方降级到既有
        `_match_window_id_by_ax_bounds()` 分支（零回归）。
        """
        if self._window is None:
            return None
        try:
            sheets = self._window.findAllR(AXRole="AXSheet")
        except Exception:
            return None
        if not sheets:
            return None
        sheet = sheets[0]
        try:
            target_pid = sheet.pid
            pos = sheet.AXPosition
            size = sheet.AXSize
        except Exception as exc:
            logger.debug("[MacAppKit] 读取 AXSheet pid/边界失败: %s", exc)
            return None

        tolerance = 2.0
        for info in win_list:
            try:
                if info.get("kCGWindowOwnerPID") != target_pid:
                    continue
                bounds = info.get("kCGWindowBounds", {})
                if (
                    abs(bounds.get("X", 0) - pos.x) <= tolerance
                    and abs(bounds.get("Y", 0) - pos.y) <= tolerance
                    and abs(bounds.get("Width", 0) - size.width) <= tolerance
                    and abs(bounds.get("Height", 0) - size.height) <= tolerance
                ):
                    return info.get("kCGWindowNumber")
            except Exception:
                continue
        return None

    # ── 系统文件选择对话框（choose_file[] Action 支持）───────────────
    # 计划文档：docs/plans/archive/02-dsl-actions-实施纪要.md §3.4

    async def has_open_file_dialog(self) -> bool:
        return await self._run_sync(self._sync_has_open_file_dialog)

    def _sync_has_open_file_dialog(self) -> bool:
        """检测当前应用窗口内是否存在系统文件选择对话框（AXSheet）。

        macOS 原生"打开"对话框在点击触发按钮后通常以 AXSheet 形式挂靠在
        触发方 App 自身的窗口下（真机验证：桌面应用文件导入"添加"按钮场景），
        而非独立进程窗口，故复用本引擎已附着的 self._window 查找即可，不需要
        额外调用 atomacos.getFrontmostApp()。
        """
        if self._window is None:
            return False
        try:
            self._refresh_window()
            return bool(self._window.findAllR(AXRole="AXSheet"))
        except Exception as exc:
            logger.debug("[MacAppKit] _sync_has_open_file_dialog 失败: %s", exc)
            return False

    async def goto_path_in_file_dialog(self, path: str) -> bool:
        return await self._run_sync(self._sync_goto_path_in_file_dialog, path)

    def _file_dialog_sheet_count(self) -> int:
        """当前窗口挂靠的 AXSheet 数量（主文件对话框与"前往"框均计为 sheet）。

        窗口引用失效/查询异常时按 0 处理（对话框关闭瞬间旧引用即失效，
        视为已关闭，让确认循环正常退出）。
        """
        try:
            if self._window is None:
                return 0
            return len(self._window.findAllR(AXRole="AXSheet") or [])
        except Exception:
            return 0

    def _sync_goto_path_in_file_dialog(self, path: str) -> bool:
        """Cmd+Shift+G 打开"前往"框 → 输入路径 → 循环 Enter 直至对话框关闭。

        真机探测（2026-07-28，桌面应用文件导入"添加"场景）：Enter 的消费层级
        不固定——首个 Enter 常被路径自动补全/前往框文本确认消耗，第二个 Enter
        才关闭前往框完成导航，还需第三个 Enter 触发主对话框默认按钮"打开"。
        固定按两次 Enter 时文件夹经常未被选上（主对话框残留，按钮持续
        enabled=False）。故改为循环按 Enter，每次后检查 AXSheet 是否全部关闭
        （前往框与主对话框都算 sheet），上限 5 次防死循环。

        执行前先激活目标 App 到前台（``_sync_bring_to_foreground``），因为
        ``atomacos.keyboard`` 发送的是全局 CGEvent，不绑定特定 App——若目标
        App 被遮挡/最小化，键盘事件会命中错误窗口导致跳转失败（见
        docs/plans/archive/01-engine-lifecycle-实施纪要.md）。
        best-effort：激活失败不阻断后续键盘操作。
        """
        try:
            self._sync_bring_to_foreground()
            time.sleep(0.5)
            atomacos.keyboard.hotkey("command", "shift", "g")
            time.sleep(0.4)
            # 路径走剪贴板粘贴而非 typewrite：typewrite 底层是 pyautogui 的
            # ASCII 键位模拟，中文路径（如 "敏感图片"）会被静默吞掉，导致前往框
            # 只剩残缺路径、跳转失败。pbcopy + Cmd+V 可靠输入任意 Unicode
            # （与本引擎 _sync_type_focused(via_clipboard=True) 同款）。
            subprocess.run(["pbcopy"], input=path, text=True, check=True)
            atomacos.keyboard.hotkey("command", "v")
            time.sleep(0.4)
            for _ in range(5):
                atomacos.keyboard.typewrite("\r")
                time.sleep(0.7)
                if self._file_dialog_sheet_count() == 0:
                    return True
            logger.warning(
                "[MacAppKit] goto_path_in_file_dialog: 5 次 Enter 后对话框仍未关闭"
            )
            return False
        except Exception as exc:
            logger.warning("[MacAppKit] _sync_goto_path_in_file_dialog 失败: %s", exc)
            return False

    # ── L4 自愈：坐标反查真实 AXPath selector ─────────────────────────
    # 计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md

    # AXPath attr 名 → AX 原生属性名（与 ax_query.py::_ATTR_MAP 保持一致，
    # 按"稳定性"排序：AXIdentifier 最稳定，AXValue 最易变（动态数字）放最后）
    _SELF_HEAL_ATTR_PRIORITY: tuple = (
        ("identifier", "AXIdentifier"),
        ("title", "AXTitle"),
        ("description", "AXDescription"),
        ("value", "AXValue"),
    )

    async def resolve_native_selector_from_coords(self, cx: float, cy: float) -> Optional[str]:
        """从 take_screenshot() 图片局部像素坐标反查真实 AXPath selector（L4 自愈回写）。

        cx, cy：窗口内容左上角为原点的局部坐标（与 AXPosition/AXSize 对齐，
        与 take_screenshot() 坐标系一致）。内部加窗口偏移换算为屏幕绝对坐标后，
        用 AXUIElementCopyElementAtPosition 做 hit-test，沿 AXParent 上溯寻找
        第一个"可唯一识别"的祖先节点，拼出可被 ax_query.py::parse_axpath 解析
        的 AXPath 字符串。反查失败/异常均返回 None（不写垃圾数据）。
        """
        return await self._run_sync(
            self._sync_resolve_native_selector_from_coords, cx, cy,
        )

    def _sync_resolve_native_selector_from_coords(
        self, cx: float, cy: float, max_depth: int = 6,
    ) -> Optional[str]:
        if self._window is None:
            return None
        try:
            import ApplicationServices as AXS  # type: ignore
        except ImportError:
            logger.debug("[MacAppKit] ApplicationServices 未安装，跳过原生坐标反查")
            return None

        try:
            self._refresh_window()
            win_pos = self._window.AXPosition
            screen_x = float(win_pos.x) + float(cx)
            screen_y = float(win_pos.y) + float(cy)

            sys_wide = AXS.AXUIElementCreateSystemWide()
            err, elem = AXS.AXUIElementCopyElementAtPosition(
                sys_wide, screen_x, screen_y, None,
            )
            if err != 0 or elem is None:
                logger.debug(
                    "[self-heal] L4 hit-test 无结果: screen=(%.1f, %.1f) err=%s",
                    screen_x, screen_y, err,
                )
                return None

            axpath = self._build_axpath_via_ancestor_walk(AXS, elem, max_depth)
            if axpath:
                logger.info("[self-heal] L4 原生坐标反查成功: coords=(%.1f, %.1f) → %r",
                            cx, cy, axpath)
            else:
                logger.debug("[self-heal] L4 原生坐标反查未找到可唯一识别的祖先节点")
            return axpath
        except Exception as exc:
            logger.debug("[self-heal] _sync_resolve_native_selector_from_coords 异常: %s", exc)
            return None

    def _build_axpath_via_ancestor_walk(
        self, AXS, elem, max_depth: int,
    ) -> Optional[str]:
        """从 hit-test 命中的 AXUIElementRef 起，沿 AXParent 上溯，逐层尝试拼出
        唯一命中的 AXPath 字符串。命中元素本身视为第 0 层。

        每层依次尝试：
          1. 按 _SELF_HEAL_ATTR_PRIORITY 顺序取属性值 → 拼 "{role}[{attr}={val}]"
             → findAllR 校验数量==1 才采信
          2. 裸 role（无属性过滤）→ findAllR 校验数量==1 才采信
        全部失败则上溯到 AXParent 继续，达到 max_depth 仍无结果返回 None。
        """
        current = elem
        for _depth in range(max_depth):
            role = self._copy_ax_attr(AXS, current, "AXRole")
            if role:
                for friendly, ax_attr_name in self._SELF_HEAL_ATTR_PRIORITY:
                    val = self._copy_ax_attr(AXS, current, ax_attr_name)
                    if not val:
                        continue
                    candidate = f"{role}[{friendly}={val}]"
                    try:
                        kwargs = {"AXRole": role, ax_attr_name: val}
                        matches = self._window.findAllR(**kwargs)
                        if len(matches) == 1:
                            return candidate
                    except Exception:
                        continue

                # 裸 role 兜底：无任何属性可用但该 role 在窗口内全局唯一
                try:
                    matches = self._window.findAllR(AXRole=role)
                    if len(matches) == 1:
                        return role
                except Exception:
                    pass

            parent = self._copy_ax_attr(AXS, current, "AXParent")
            if parent is None:
                break
            current = parent

        return None

    @staticmethod
    def _copy_ax_attr(AXS, elem, attr_name: str):
        """封装 AXUIElementCopyAttributeValue，异常/err!=0 统一返回 None。"""
        try:
            err, value = AXS.AXUIElementCopyAttributeValue(elem, attr_name, None)
            if err != 0:
                return None
            if isinstance(value, str) and not value.strip():
                return None
            return value
        except Exception:
            return None
