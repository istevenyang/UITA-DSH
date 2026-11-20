"""macOS 浏览器系统级操作（受管 Chrome 拉起 / 窗口前置最大化 / 原生文件面板）。

本模块把 PlaywrightEngine 中一切**依赖 macOS 系统框架**的实现从引擎里剥离出来，
使 `engine.py` 保持平台无关、内容干净：引擎只在 async 方法里做"页面/上下文状态
编排 + 线程委托"，所有 osascript / Quartz / AppKit / atomacos / lsof / Chrome 可执行
文件路径等 macOS-only 细节全部集中在这里。

设计约定：
- 全部为**无状态模块级函数**，不持有引擎实例；引擎状态（page/context/native_pid/
  channel）由调用方以参数传入、以返回值回写，依赖方向单一（engine → 本模块）。
- 每个函数在非 darwin 平台或任一步失败时安全返回（False/0/None），绝不抛出，
  以便引擎侧"失败仅记日志、不影响主流程"的既有契约不变。
- 仅供 `engine/playwright/engine.py` 内部使用，不是 EngineBase 契约的一部分
  （R2/R3：引擎专用能力放具体引擎，跨平台差异按平台隔离）。

Windows 对应能力仍在各自引擎（win_uia）内，本模块不涉及。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from typing import Optional

logger = logging.getLogger("uita")

# 受管 Chrome 使用的独立 user-data-dir，避免污染用户日常 Chrome profile。
_MANAGED_CHROME_USER_DATA_DIR = os.path.join(
    os.path.expanduser("~"), ".uita-managed-chrome"
)
_CHROME_BIN = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def is_macos() -> bool:
    return sys.platform == "darwin"


def detect_cdp_owner_pid(cdp_url: str) -> Optional[int]:
    """返回监听 CDP 调试端口的 Chrome 进程 PID（用于锁定受管进程）。

    从 cdp_url 解析端口，用 lsof 找 LISTEN 该端口的进程。失败返回 None
    （前置逻辑会回退到按窗口标题匹配）。仅传入数字端口进 lsof，无 shell 注入。
    """
    if not is_macos():
        return None
    try:
        from urllib.parse import urlparse

        port = urlparse(cdp_url if "://" in cdp_url else f"http://{cdp_url}").port
        if not port:
            return None
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{int(port)}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in result.stdout.splitlines()[1:]:
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                return int(fields[1])
    except Exception as exc:  # noqa: BLE001
        logger.debug("[macos_browser] detect_cdp_owner_pid failed: %s", exc)
    return None


def probe_cdp_endpoint_healthy(cdp_url: str) -> bool:
    """校验 CDP 端点是否为一个可被 Playwright connect_over_cdp 接管的健康浏览器端点。

    仅"端口在 LISTEN"（detect_cdp_owner_pid 命中）并不足以保证能 attach——例如日常
    Chrome 抢占了端口、或残留的降级 / Chrome-for-Testing 端点，会在 connect_over_cdp
    发 `Browser.setDownloadBehavior` 时报 "Browser context management is not supported"。

    这里通过 `GET /json/version` 校验：健康的顶层浏览器端点会返回含 `Browser` 和
    `webSocketDebuggerUrl`（browser 级 ws）的 JSON。缺 `webSocketDebuggerUrl` 说明不是
    可接管的 browser 端点。任何异常视为不健康（False）。
    """
    try:
        import json as _json
        import urllib.request
        from urllib.parse import urlparse

        parsed = urlparse(cdp_url if "://" in cdp_url else f"http://{cdp_url}")
        base = f"http://{parsed.hostname}:{parsed.port}"
        with urllib.request.urlopen(f"{base}/json/version", timeout=3) as resp:
            info = _json.loads(resp.read().decode("utf-8", "replace"))
        # browser 级 ws 端点存在 = 可被 connect_over_cdp 接管的完整浏览器端点。
        healthy = bool(info.get("webSocketDebuggerUrl"))
        if not healthy:
            logger.warning(
                "[macos_browser] CDP 端点不健康（/json/version 无 webSocketDebuggerUrl）: %s",
                info.get("Browser"),
            )
        return healthy
    except Exception as exc:  # noqa: BLE001
        logger.debug("[macos_browser] probe_cdp_endpoint_healthy failed: %s", exc)
        return False


def kill_managed_chrome(pid: Optional[int]) -> bool:
    """终止引擎自己拉起的受管 Chrome 进程（用例结束清理）。

    仅应对"引擎 spawn 的受管 Chrome"调用——外部预置 / 用户手动启动的 Chrome 不归
    引擎关闭（谁启动谁负责）。先 SIGTERM 优雅退出，短暂等待后仍在则 SIGKILL 兜底。
    非 darwin 或 pid 无效直接返回 False，任何异常吞掉不影响主流程。
    """
    if not is_macos() or not pid:
        return False
    try:
        import signal

        os.kill(pid, signal.SIGTERM)
        for _ in range(20):  # 最多等 2s 优雅退出
            time.sleep(0.1)
            try:
                os.kill(pid, 0)  # 探测存活；不存在会抛 OSError
            except OSError:
                logger.info("[macos_browser] 受管 Chrome PID=%d 已优雅退出", pid)
                return True
        os.kill(pid, signal.SIGKILL)
        logger.info("[macos_browser] 受管 Chrome PID=%d 已强制终止", pid)
        return True
    except ProcessLookupError:
        return True  # 已不存在，视作成功
    except Exception as exc:  # noqa: BLE001
        logger.debug("[macos_browser] kill_managed_chrome failed: %s", exc)
        return False


def ensure_managed_chrome(port: int, cdp_url: str) -> int:
    """确保受管 Chrome 已在指定调试端口以**可接管的健康 CDP 端点**运行。

    返回值语义（供引擎决定 close 时是否关闭）：
      - 返回 **正数 PID**：本次由引擎新拉起的受管 Chrome（引擎负责在 close 时终止）；
      - 返回 **0**：复用了已存在且健康的端点（外部预置/前次遗留，引擎**不**负责关闭）。

    与旧实现的关键区别：不再仅凭"端口 LISTEN"就复用——还要 probe_cdp_endpoint_healthy
    校验该端点能被 connect_over_cdp 接管。若端口被占但端点不健康（如日常 Chrome 抢占
    9222、残留降级端点），会先终止占用进程，再拉起干净的独立受管 Chrome，从根本上避免
    "Browser context management is not supported" 连接失败。

    失败抛 RuntimeError（由引擎侧决定如何处理——attach 前置条件缺失应显式失败）。
    """
    # 端口已被占用：仅当端点健康才复用；否则清掉占用者重拉。
    existing_pid = detect_cdp_owner_pid(cdp_url)
    if existing_pid is not None:
        if probe_cdp_endpoint_healthy(cdp_url):
            logger.info(
                "[macos_browser] managed_chrome 已在运行且端点健康，复用调试端口 %d (PID=%d)",
                port, existing_pid,
            )
            return 0  # 复用既有健康端点，引擎不负责关闭
        logger.warning(
            "[macos_browser] 端口 %d 被 PID=%d 占用但 CDP 端点不可接管，先终止再重拉受管 Chrome",
            port, existing_pid,
        )
        kill_managed_chrome(existing_pid)
        # 等待端口释放，避免新进程绑定失败。
        for _ in range(20):
            if detect_cdp_owner_pid(cdp_url) is None:
                break
            time.sleep(0.2)
    if not os.path.exists(_CHROME_BIN):
        raise RuntimeError(
            f"managed_chrome_cdp: 未找到 Google Chrome 可执行文件: {_CHROME_BIN}；"
            "请确认已安装 Google Chrome"
        )
    # 直接用可执行文件 Popen 起独立实例，而非 `open -na`。macOS `open -na` 在已有
    # Chrome 运行时会复用现有实例并忽略 --args（导致 --remote-debugging-port 不生效、
    # 9222 从不 LISTEN），这正是拖拽找不到受管窗口的根因。独立 --user-data-dir
    # 保证起的是全新独立进程（与用户日常 Chrome 互不干扰），调试端口稳定 LISTEN。
    cmd = [
        _CHROME_BIN,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={_MANAGED_CHROME_USER_DATA_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    logger.info("[macos_browser] 自动拉起受管 Chrome: %s", " ".join(cmd))
    try:
        # detached 后台进程；不等待其退出（Chrome 常驻）。
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"managed_chrome_cdp: 自动拉起受管 Chrome 失败: {exc}"
        ) from exc
    # 轮询等待调试端口就绪 **且** 端点健康（受管 Chrome 冷启动可能较慢，最多等 ~30s）。
    for _ in range(60):
        if detect_cdp_owner_pid(cdp_url) is not None and probe_cdp_endpoint_healthy(cdp_url):
            owner = detect_cdp_owner_pid(cdp_url) or proc.pid
            logger.info("[macos_browser] 受管 Chrome 调试端口 %d 已就绪 (PID=%d)", port, owner)
            return owner  # 引擎新拉起，负责在 close 时终止
        time.sleep(0.5)
    # 拉起超时：清掉可能半启动的进程，避免残留。
    kill_managed_chrome(proc.pid)
    raise RuntimeError(
        f"managed_chrome_cdp: 已发起启动但调试端口 {port} 30s 内未就绪；"
        "请检查 Google Chrome 是否已安装、以及是否有同 user-data-dir 的实例阻塞启动"
    )


def maximize_front_chrome_window(pid: int) -> bool:
    """把 Chrome 最前窗口铺满主屏可视区（非全屏，保留标题栏/红绿灯）。

    走 Chrome 原生 AppleScript 字典的 `set bounds of front window`（Apple Events），
    而非 AX 写 AXPosition/AXSize——实测 Chrome 的 AXWindow 拒绝 AX 尺寸写入（写后回读
    不变），而 AppleScript set bounds 稳定生效；AXZoomButton.AXPress 又会触发 macOS
    全屏（独立 Space），不符合"非全屏最大化"预期。

    依赖调用时序：调用方须已把受管 Chrome 置为最前，故 `front window` 即受管窗口
    （受管与日常 Chrome 同名，无法只按 PID 在 AppleScript 里区分）。pid 仅用于日志。

    可视区取 Finder desktop bounds（含菜单栏，故 top 下压到菜单栏之下 25px）；
    bottom 为 Dock 之上。任一步失败安全返回 False，不抛错。
    """
    if not is_macos():
        return False
    try:
        vf = subprocess.run(
            ["osascript", "-e",
             'tell application "Finder" to return bounds of window of desktop'],
            capture_output=True, text=True, timeout=8,
        )
        try:
            vx0, vy0, vx1, vy1 = [int(x) for x in vf.stdout.strip().split(", ")]
        except Exception:  # noqa: BLE001 — 解析失败用保守兜底
            vx0, vy0, vx1, vy1 = 0, 25, 1440, 900
        top = max(vy0, 25)  # 压到菜单栏之下
        set_script = (
            'tell application "Google Chrome" to set bounds of front window '
            f"to {{{vx0}, {top}, {vx1}, {vy1}}}"
        )
        r = subprocess.run(
            ["osascript", "-e", set_script],
            capture_output=True, text=True, timeout=8,
        )
        ok = r.returncode == 0 and not r.stderr.strip()
        logger.info(
            "[macos_browser] maximize(applescript) pid=%d bounds=(%d,%d,%d,%d) ok=%s err=%s",
            pid, vx0, top, vx1, vy1, ok, r.stderr.strip(),
        )
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.debug("[macos_browser] maximize front chrome window failed: %s", exc)
    return False


def bring_browser_to_foreground(
    channel: Optional[str], preferred_pid: Optional[int], title_contains: str
) -> Optional[int]:
    """按 channel + 窗口标题把浏览器窗口前置，返回实际前置的进程 PID（失败 None）。

    macOS 上可能存在多个 Google Chrome 进程（日常 Chrome + 独立受管 profile）。
    优先前置 preferred_pid（已锁定的受管进程），否则按 layer-0 窗口标题匹配。
    调用方拿到返回 PID 后回写为受管 PID。纯窗口前置，不涉及引擎页面状态。
    """
    if not is_macos():
        return None
    channel_to_bundle = {
        "chrome": "com.google.Chrome",
        "msedge": "com.microsoft.edgemac",
    }
    bundle_id = channel_to_bundle.get(channel or "")
    if not bundle_id:
        return None
    try:
        import Quartz
        from AppKit import NSApplicationActivateIgnoringOtherApps, NSRunningApplication

        applications = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
        pids = {int(application.processIdentifier()) for application in applications or []}
        if not pids:
            return None
        target = title_contains.casefold()
        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID,
        ) or []
        # 若已预先锁定受管进程 PID，则优先前置该 PID，避免 next() 误取到日常 Chrome，
        # 导致后续文件选择器/键盘事件发错进程。
        pid = None
        if preferred_pid is not None and preferred_pid in pids:
            # 优先：受管 PID 的 layer-0 窗口且标题命中 title_contains（最精确）。
            title_hit = any(
                int(window.get("kCGWindowLayer", 0)) == 0
                and int(window.get("kCGWindowOwnerPID", -1)) == preferred_pid
                and target in str(window.get("kCGWindowName") or "").casefold()
                for window in windows
            )
            # 兜底：CDP attach 复用受管 Chrome 时，其活动 tab 的 DOM 标题已是目标页，
            # 但 macOS 窗口标题（kCGWindowName）常滞留为 "Google Chrome" 不刷新，使标题
            # 匹配落空。此时只要受管 PID 确有 layer-0 可见窗口，即直接前置该受管进程——
            # PID 本身已是精确目标，无需再依赖易失效的 OS 窗口标题。
            has_managed_window = any(
                int(window.get("kCGWindowLayer", 0)) == 0
                and int(window.get("kCGWindowOwnerPID", -1)) == preferred_pid
                for window in windows
            )
            if title_hit or has_managed_window:
                pid = preferred_pid
                if not title_hit:
                    logger.info(
                        "[macos_browser] 受管 Chrome PID=%d 窗口标题未含'%s'（CDP attach 常见），"
                        "按受管 PID 直接前置", preferred_pid, title_contains,
                    )
        if pid is None:
            pid = next(
                (
                    int(window["kCGWindowOwnerPID"])
                    for window in windows
                    if int(window.get("kCGWindowLayer", 0)) == 0
                    and int(window.get("kCGWindowOwnerPID", -1)) in pids
                    and target in str(window.get("kCGWindowName") or "").casefold()
                ),
                None,
            )
        if pid is None:
            return None
        application = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if not application or not application.activateWithOptions_(NSApplicationActivateIgnoringOtherApps):
            return None
        # AppleScript targets a numeric PID only; title text never enters script construction.
        command = (
            'tell application "System Events" to tell '
            f'(first process whose unix id is {pid}) to set frontmost to true'
        )
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", command],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0 or not application.isActive():
            return None
        return pid
    except Exception as exc:  # noqa: BLE001
        logger.debug("[macos_browser] browser foreground failed: %s", exc)
        return None


def file_dialog_open(native_pid: Optional[int]) -> bool:
    """探测受管 Chrome 进程是否弹出了原生文件选择/上传面板。"""
    if native_pid is None:
        return False
    try:
        import atomacos

        app = atomacos.getAppRefByPid(native_pid)
        for window in app.windows() or []:
            for sheet in window.findAllR(AXRole="AXSheet"):
                button_titles = {
                    str(getattr(button, "AXTitle", "") or "")
                    for button in sheet.findAllR(AXRole="AXButton")
                }
                # 单文件选择面板按钮为"打开/取消"；整个文件夹上传面板按钮为
                # "上传/取消"（Upload/Cancel）——两类都需识别，否则文件夹上传会被
                # 误判为"对话框未弹出"。
                if (
                    ({"打开", "取消"} <= button_titles)
                    or ({"Open", "Cancel"} <= button_titles)
                    or ({"上传", "取消"} <= button_titles)
                    or ({"Upload", "Cancel"} <= button_titles)
                ):
                    return True
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("[macos_browser] native file dialog probe failed: %s", exc)
        return False


def file_dialog_sheet_count(native_pid: Optional[int]) -> int:
    """当前 Chrome PID 所有窗口挂靠的 AXSheet 总数。

    "前往文件夹"临时子框与主"打开"面板都以 AXSheet 形式存在，两者都要计入——只有
    全部关闭（数量归零）才代表文件已选定、面板已消失。查询异常时按 0 处理（面板
    关闭瞬间旧引用即失效，视为已关闭）。
    """
    if native_pid is None:
        return 0
    try:
        import atomacos

        app = atomacos.getAppRefByPid(native_pid)
        count = 0
        for window in app.windows() or []:
            count += len(window.findAllR(AXRole="AXSheet") or [])
        return count
    except Exception:  # noqa: BLE001
        return 0


def confirm_folder_upload_if_present() -> bool:
    """点击 Chrome"将 N 个文件上传到此网站？"确认框的"上传"按钮。

    该确认框是 Chrome 浏览器级原生 View（独立 AXWindow，非 DOM、非系统文件面板），
    仅在"整个文件夹"上传时弹出，用于二次确认整目录外发。真机 AX 结构：
    - AXWindow: AXTitle="将 N 个文件上传到此网站？"
    - 内含 AXButton: AXDescription="取消" / AXDescription="上传"（文本在 desc 而非 title）

    Chrome 多进程架构下确认框窗口可能挂在与文件面板不同的进程，故遍历所有 Chrome PID
    查找。未找到（如单文件上传）直接返回 False，不做任何按键，避免误触。短暂轮询等待
    确认框出现（面板关闭到确认框弹出有渲染延迟）。
    """
    try:
        import atomacos

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            out = subprocess.run(
                ["pgrep", "-f", "Google Chrome"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            pids = [int(p) for p in out.split() if p.strip().isdigit()]
            for pid in pids:
                try:
                    app = atomacos.getAppRefByPid(pid)
                    windows = app.windows() or []
                except Exception:  # noqa: BLE001
                    continue
                for window in windows:
                    try:
                        title = str(getattr(window, "AXTitle", "") or "")
                    except Exception:  # noqa: BLE001
                        title = ""
                    if "上传到此网站" not in title and "upload" not in title.lower():
                        continue
                    for button in window.findAllR(AXRole="AXButton") or []:
                        desc = str(getattr(button, "AXDescription", "") or "")
                        if desc in ("上传", "Upload"):
                            button.Press()
                            logger.info(
                                "[macos_browser] 已确认 Chrome 文件夹上传框 (title=%s)", title,
                            )
                            return True
            time.sleep(0.3)
        logger.debug("[macos_browser] 未检测到 Chrome 文件夹上传确认框（单文件上传或无需确认）")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("[macos_browser] Chrome 文件夹上传确认失败: %s", exc)
        return False


def choose_file_in_dialog(native_pid: Optional[int], path: str) -> bool:
    """通过 macOS "打开"面板的"前往文件夹"命令选择单个文件/目录。

    对齐 mac_appkit 引擎真机验证过的逻辑（MacAppKitEngine._sync_goto_path_in_file_dialog）：

    1. Cmd+Shift+G 打开"前往文件夹"框后，直接输入**完整文件路径**（含文件名）；
       macOS 面板支持一次性输入到具体文件，无需拆成父目录+文件名两段。
    2. Enter 的消费层级不固定——首个 Enter 常被"前往框"文本确认消耗，需再按一次才关闭
       前往框完成导航，还可能需要第三个 Enter 触发主面板默认按钮"打开"。故循环按 Enter，
       每次后检查 AXSheet 是否全部关闭（前往框与主面板都算 sheet），上限 5 次防死循环。
    """
    # path 可为单个文件（单文件上传）或目录（整个文件夹上传）——两者都用"前往文件夹"
    # 输入完整路径 + 循环 Enter 的方式确认，故用 exists 而非 isfile。
    if native_pid is None or not os.path.exists(path):
        return False
    try:
        import atomacos

        app = atomacos.getAppRefByPid(native_pid)
        if file_dialog_sheet_count(native_pid) == 0:
            return False
        # atomacos.keyboard 发送全局 CGEvent，不绑定特定 App——先把目标 Chrome 激活到
        # 前台，避免按键命中错误窗口。
        app.activate()
        time.sleep(0.5)

        atomacos.keyboard.hotkey("command", "shift", "g")
        time.sleep(0.4)
        # 路径必须走剪贴板粘贴而非 typewrite：atomacos.keyboard.typewrite 底层是 pyautogui
        # 的 ASCII 键位模拟，遇到中文（如 "敏感图片"）会被静默吞掉，导致前往框里只剩
        # ".../.png" 这类残缺路径、跳转失败。pbcopy + Cmd+V 是 macOS 上可靠输入任意
        # Unicode 的唯一方式（与 mac_appkit 引擎 _sync_type_focused(via_clipboard=True) 同款）。
        subprocess.run(["pbcopy"], input=path, text=True, check=True)
        atomacos.keyboard.hotkey("command", "v")
        time.sleep(0.4)
        for _ in range(5):
            atomacos.keyboard.typewrite("\r")
            time.sleep(0.7)
            if file_dialog_sheet_count(native_pid) == 0:
                # 系统面板已关闭。若为"整个文件夹"上传，Chrome 会再弹一个浏览器级原生
                # 确认框（"将 N 个文件上传到此网站？" + 取消/上传），需额外确认；单文件
                # 上传不弹此框，探测不到即安全跳过（零副作用）。
                confirm_folder_upload_if_present()
                return True
        logger.warning(
            "[macos_browser] native file dialog: 5 次 Enter 后面板仍未关闭 (path=%s)", path,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("[macos_browser] native file dialog choose failed: %s", exc)
        return False
