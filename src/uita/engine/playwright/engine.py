"""
PlaywrightEngine v2 — Electron/Web CDP 引擎（二期升级版）。

新增能力（相比一期）：
  - AppProcess 注入：app_path 不为空时自动启动/等待 CDP
  - restart() / reconnect()：硬重启 / 仅重连
  - reset_page_state(level)：soft / medium / hard 三档页面复位
  - 三级截图兜底：L1 Playwright → L2 CDP → L3 PyAutoGUI
  - 多窗口管理：snapshot_pages / detect_and_switch_new_page / ensure_page_alive / refresh_active_page

CDP 连接方式：
    /Applications/SampleApp.app/Contents/MacOS/SampleApp --remote-debugging-port=9222
"""
from __future__ import annotations

import asyncio
import collections
import logging
import os
import subprocess
import sys
import time
from typing import Optional, Tuple

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

from uita.core.engine.base import ElementRef, EngineBase
from uita.core.engine.registry import register_engine
from uita.core.engine.sync_bridge import BoundedExecutor
from uita.engine.playwright import _macos_browser

logger = logging.getLogger(__name__)


def _is_js_expr(selector: str) -> bool:
    """判断 selector 是否为 JS 表达式（非标准 Playwright locator）。

    识别规则：以 ``(``（IIFE）、``document.``（DOM API）、``()``（箭头函数）开头。
    Playwright locator 语法（CSS ``#id`` / XPath ``//div`` / ``text=`` / ``role=``）
    均不以前述前缀开头，不会误判。
    """
    s = selector.strip()
    return s.startswith("((") or s.startswith("document.") or s.startswith("()")


_APP_NAME = "sample_web"
# Playwright CDP 引擎本身跨平台（user_data_dir 已有 darwin/win32 分支，零 macOS
# 专属代码），因此使用 universal 注册——在 register() 时由 resolve_universal_platform()
# 解析为当前宿主机真实平台（macos/windows）写入 _REGISTRY。与 apps/sample_web/register.py
# 的 EngineRegistry.register("sample_web", "universal", PlaywrightEngine) 幂等重复（无害）。
# _APP_NAME 仅供装饰器使用（注册时声明该引擎类服务哪个 app）；实例化时由
# build_engine_for_app() 传入 app_name kwarg，支持同一引擎类服务多个 app
# （如未来 demo_desktop 等其他 Electron app 复用 PlaywrightEngine）。
# 计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md §3.2
_SKIP_URLS = ("chrome-error", "devtools://", "about:blank", "chrome-extension://")

# ── channel 降级兜底（chrome ⇄ msedge 互为备选） ─────────────────
# 见 docs/plans/archive/03-resolver-rosetta-实施纪要.md
_CHANNEL_FALLBACK_PAIR = {"chrome": "msedge", "msedge": "chrome"}
_CLOSE_TIMEOUT_S = 10.0

# ── 网络诊断（失败响应/请求捕获，供 core/hooks/builtin/network_diagnostics.py
# 在 step 失败时提取并追加进错误原因 + 打包上传 报告平台）──────────────────
# 见 docs/plans/archive/04-report_portal-report-实施纪要.md
_NETWORK_LOG_MAXLEN = 50       # 环形缓冲容量：控制内存占用，仅保留最近 N 条
_NETWORK_BODY_TRUNCATE = 2048  # request/response body 截断长度（字符数）
# 2026-08-19 三态分级补充：全量 API 响应（不论 status，供业务错误规则扫描）
# + 前端 JS 异常（console error/pageerror）两个独立缓冲，见
# docs/plans/archive/04-report_portal-report-实施纪要.md §七
_ALL_RESPONSES_LOG_MAXLEN = 200
_FRONTEND_ERROR_LOG_MAXLEN = 50
# 仅这两类 resource_type 视为"业务接口调用"，过滤 image/css/font 等静态资源，
# 避免 _all_responses_log 被无关噪音占满
_API_RESOURCE_TYPES = ("xhr", "fetch")

_CHANNEL_WELL_KNOWN_PATHS: dict[str, dict[str, list[str]]] = {
    "chrome": {
        "macos": ["/Applications/Google Chrome.app"],
        "windows": [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ],
    },
    "msedge": {
        "macos": ["/Applications/Microsoft Edge.app"],
        "windows": [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ],
    },
}


def _is_channel_available(channel: str, platform: str) -> bool:
    """探测 channel 对应浏览器是否已安装（best-effort，启动前探测而非事后捕获
    launch() 异常重试——避免异常信息不稳定导致误判，也避免二次报错掩盖真实问题）。

    未知 channel（不在白名单，如自定义 "custom_browser"）或当前平台无已知探测路径
    （如 linux）时保守返回 True——不拦截，维持改造前"直接交给 chromium.launch()
    报真实错误"的行为，避免探测机制本身成为新的故障点。
    """
    paths = _CHANNEL_WELL_KNOWN_PATHS.get(channel, {}).get(platform)
    if not paths:
        return True
    return any(os.path.exists(p) for p in paths)


def _resolve_effective_channel(channel: str, platform: str) -> str:
    """按已装浏览器情况解析实际生效的 channel，chrome/msedge 互为兜底。

    - 未配置 channel（空串）→ 原样返回，不介入（走 Playwright bundled Chromium）
    - 配置的 channel 已安装 → 原样返回，不做任何切换（零多余行为）
    - 配置的 channel 未安装 + 兄弟 channel 已安装 → 降级到兄弟 channel
    - 二者均不可判定为已安装 → 原样返回配置值（交给 chromium.launch() 报真实
      错误，不吞掉原始问题）；Windows 平台在此兜底分支额外多试一次 msedge
      （系统自带，安装确定性最高于 chrome）
    """
    normalized_platform = platform.strip().lower()
    if not channel or _is_channel_available(channel, normalized_platform):
        return channel

    sibling = _CHANNEL_FALLBACK_PAIR.get(channel)
    if sibling and _is_channel_available(sibling, normalized_platform):
        logger.warning("[engine] channel=%r 未探测到已安装，降级为 %r", channel, sibling)
        return sibling

    if normalized_platform == "windows" and channel != "msedge":
        # 走到这里说明 channel 与其兄弟渠道均未探测到安装（上面两个分支都没有
        # return）；Windows 系统自带 Edge，安装确定性高于探测路径本身，
        # 因此无需再次探测，直接兜底选择 msedge。
        logger.warning(
            "[engine] channel=%r 及兄弟渠道均未探测到安装，Windows 默认兜底到 msedge", channel
        )
        return "msedge"

    return channel


@register_engine(_APP_NAME, "universal")
class PlaywrightEngine(EngineBase):
    """Playwright CDP 引擎（Electron/Web，跨平台 universal 注册）。"""

    def __init__(
        self,
        cdp_url: str = "http://localhost:9222",
        app_path: str = "",
        cdp_port: int = 9222,
        target_html: Optional[str | list[str]] = None,
        platform: str = "macos",
        app_name: str = _APP_NAME,
        url: str = "",
        channel: str = "",
        storage_state: str = "",
        start_maximized: bool = False,
    ):
        self._cdp_url = cdp_url
        self._cdp_port = cdp_port
        self._target_html = target_html
        # Web 独立启动模式（非 Electron CDP attach）：url 非空时 launch() 走
        # _launch_web()，自行拉起浏览器并导航，不依赖已运行的本地进程/CDP 端口。
        # channel（如 "msedge"/"chrome"）传给 chromium.launch()；storage_state
        # 为登录态文件路径，传给 new_context()（文件不存在时忽略，走匿名 context）。
        # start_maximized 仅影响有头独立 Web 模式，不影响 CDP attach。
        # 见 docs/plans/archive/01-engine-lifecycle-实施纪要.md。
        self._url = url
        self._channel = channel
        self._effective_channel = channel
        # macOS 原生文件选择器仅操作此 PID，避免把键盘事件发给其他同名 Chrome。
        self._native_browser_pid: Optional[int] = None
        # 引擎自己拉起的受管 Chrome PID（managed_chrome_cdp 模式，且非复用外部端点时）。
        # >0 表示"本引擎 spawn，close 时须终止"；None 表示无需关闭（复用/未拉起）。
        self._managed_chrome_pid: Optional[int] = None
        # 浏览器接入模式：由 interpreter 在 launch 前按用例头字段 browser_mode 注入。
        # ""=常规 launch；"managed_chrome_cdp"=自动拉起 9222 受管 Chrome 并 attach。
        # 用例自声明，不依赖任何外部环境变量或命令行参数（自动化测试命令保持统一）。
        self._browser_mode: str = ""
        self._storage_state = storage_state
        self._start_maximized = start_maximized
        # 平台标识：universal 注册的引擎在 register() 时已解析为宿主平台字符串
        # 存入 _REGISTRY，但实例本身不知道自己服务哪个平台——由 build_engine_for_app()
        # 在构造时传入，供 LaunchPolicy/Resolver 等 (app_name, platform) 路由组件
        # 正确匹配（2026-07-06 跨平台注册补丁）。
        self._platform = platform
        # app_name 实例属性：同一 PlaywrightEngine 类可服务多个 Electron app
        # app_name 实例属性：同一 PlaywrightEngine 类可服务多个 Electron app，
        # 实例化时由 build_engine_for_app() 注入。默认 _APP_NAME
        # 向后兼容（不传 app_name 的旧调用方仍拿到 "sample_web"）。供 LaunchPolicy/
        # Resolver/LogCollector 等按 (app_name, platform) 路由的组件正确匹配，
        # 以及 user_data_dir 拼接正确路径（不同 app 有不同 user-data-dir）。
        self._app_name = app_name

        # AppProcess（仅当 app_path 非空时启用自动启动）
        self._app_process = None
        if app_path:
            from uita.core.lifecycle.app_process import AppProcess
            self._app_process = AppProcess(
                app_path=app_path,
                cdp_port=cdp_port,
            )

        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._main_page: Optional[Page] = None
        # 页面导航历史栈（多窗口管理）：栈顶为当前页面的前驱页面
        # push 时机：detect_and_switch_new_page 检测到新页面时
        # pop 时机：ensure_page_alive 发现当前页面不可用时
        self._page_stack: list[Page] = []

        # 网络诊断环形缓冲：仅记录失败响应（status>=400）与请求失败
        # （DNS/超时/连接拒绝等），供 get_recent_network_errors() 按时间窗口
        # 查询。挂载于 BrowserContext 级别（覆盖该 context 下所有 page/弹窗），
        # 不是 EngineBase 契约方法（R9：引擎专用能力放具体类）。
        self._network_log: collections.deque = collections.deque(
            maxlen=_NETWORK_LOG_MAXLEN
        )
        # 2026-08-19 三态分级补充（见 network_diagnostics.py §七）：
        # ① 全量 API 响应（不论 status，仅 xhr/fetch），供业务错误规则扫描
        #    HTTP 200 但 body 是业务错误码的场景
        self._all_responses_log: collections.deque = collections.deque(
            maxlen=_ALL_RESPONSES_LOG_MAXLEN
        )
        # ② 前端 JS 异常（console error + 未捕获 pageerror），供区分"疑似
        #    后端接口异常" vs "疑似前端脚本/渲染异常"
        self._frontend_error_log: collections.deque = collections.deque(
            maxlen=_FRONTEND_ERROR_LOG_MAXLEN
        )

        # L3 截图兜底专用私有隔离执行域（非 macOS 分支，见
        # _screenshot_l3_generic()）。复用通用 BoundedExecutor（2026-08-21
        # 重构，见 docs/plans/20260820160000-mac-win-engine-screenshot-hang-
        # isolation-and-shared-bounded-executor.md，消除与 mac/win 引擎的
        # 重复实现），实例级、惰性创建，与 asyncio 进程共享默认执行器隔离，
        # 避免 pyautogui 截图卡死时占用的线程拖慢进程内其他 run_in_executor
        # 调用（含其他引擎实例/其他 action 的截图）。
        self._l3_guard = BoundedExecutor(max_workers=2, thread_name_prefix="pw-screenshot-l3")

    # ── 元数据 ──────────────────────────────────────────────────

    @property
    def app_name(self) -> str:
        return self._app_name

    @property
    def platform(self) -> str:
        return self._platform

    @property
    def active_page(self) -> Optional["Page"]:
        """当前活跃 Page（供 agent_rosetta CDPBackend 复用 CDP 连接，不建新端口）。"""
        return self._page

    @property
    def app_process(self):
        """底层 AppProcess（供厚封装 Action 如 quit_app[] 复用，验证进程是否真正退出）。
        未配置 app_path 时为 None。"""
        return self._app_process

    @property
    def native_browser_pid(self) -> Optional[int]:
        """受管浏览器进程 PID（CDP attach/自动拉起受管 Chrome 时锁定的 9222 端口占用者）。

        供跨应用原生拖拽等 action 直接取"引擎已锁定的受管 Chrome 进程"，使落点与
        引擎严格一致，替代旧的靠环境变量 + bundle_id 任取的脆弱定位。常规 launch
        模式下为 None（沿用 bundle_id 定位，零影响）。
        """
        return self._native_browser_pid

    @property
    def browser_mode(self) -> str:
        return self._browser_mode

    @browser_mode.setter
    def browser_mode(self, value: str) -> None:
        """由 interpreter 在 launch 前按用例头字段 browser_mode 注入。"""
        self._browser_mode = (value or "").strip()

    # ── 生命周期 ────────────────────────────────────────────────

    async def launch(self) -> None:
        """启动引擎：可选自动拉起应用 → CDP 连接 → 定位目标页面。

        self._url 非空时走独立浏览器启动模式（_launch_web()），与下方 Electron
        CDP attach 路径完全独立、互斥，见
        docs/plans/archive/01-engine-lifecycle-实施纪要.md。
        """
        if self._url:
            await self._launch_web()
            return

        # 1. 若配置了 app_path，先确保 CDP 端口就绪
        if self._app_process:
            from uita.config import CDP_WAIT_TIMEOUT
            port = await self._app_process.ensure_cdp(timeout=CDP_WAIT_TIMEOUT)
            self._cdp_url = f"http://127.0.0.1:{port}"

        # 2. Playwright CDP 连接（带 3 次重试）
        logger.info("[engine] 连接 CDP: %s", self._cdp_url)
        self._playwright = await async_playwright().start()
        last_err = None
        for attempt in range(1, 4):
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    self._cdp_url, timeout=30000
                )
                break
            except Exception as exc:
                last_err = exc
                logger.warning("[engine] CDP 连接失败 (%d/3): %s", attempt, exc)
                await asyncio.sleep(2)
        else:
            raise RuntimeError(f"CDP 连接失败: {last_err}") from last_err

        if not self._browser.contexts:
            raise RuntimeError("CDP 已连接但无 BrowserContext")
        self._context = self._browser.contexts[0]
        self._attach_network_listeners()

        # 3. 定位目标页面
        self._page = await self._find_page()
        self._main_page = self._page
        self._page_stack = []   # launch 后栈归零
        logger.info("[engine] 已连接页面: %s", self._page.url)

    async def _launch_web(self) -> None:
        """独立浏览器模式：不依赖 AppProcess/已运行进程，自行启动浏览器并导航到
        self._url（如纯 Web 站点测试场景）。

        close()/is_healthy()/reset_page_state()/多窗口管理/截图三级兜底/
        find_by_id/click/get_accessibility_summary 等全部复用现有实现——它们
        都只依赖 self._page/self._browser/self._context，与"页面从何而来"无关。

        见 docs/plans/archive/01-engine-lifecycle-实施纪要.md。

        特例（2026-09-02）：当环境变量 UITA_PW_ATTACH_CDP 非空，**或**用例头字段
        声明 browser_mode: managed_chrome_cdp 时，改为 CDP attach 到一个"受 SampleAgent
        客户端管控的真实 Chrome"（带 --remote-debugging-port）。用例声明模式下，
        引擎会**自动拉起** 9222 受管 Chrome 再 attach（无需任何外部命令/环境变量，
        自动化测试执行命令保持统一）；环境变量模式则要求外部预置。用于 DLP 外发
        拦截这类"必须在受管真实浏览器进程内发起上传才会被拦截"的特殊验证用例；
        普通 web 用例既不设该环境变量、也不声明 browser_mode，逐字节走原
        chromium.launch() 路径，零影响。见 _launch_web_attach_cdp()。
        """
        attach_cdp_url = os.environ.get("UITA_PW_ATTACH_CDP", "").strip()
        auto_managed = self._browser_mode == "managed_chrome_cdp"
        if attach_cdp_url or auto_managed:
            # 用例声明模式：无外部 cdp_url 时用固定受管调试端口；且由引擎自动拉起
            # 受管 Chrome（attach 前确保 9222 有受管进程在跑），实现零额外操作。
            if not attach_cdp_url:
                attach_cdp_url = "http://127.0.0.1:9222"
                port = self._cdp_port_from_url(attach_cdp_url)
                if port is None:
                    raise RuntimeError(
                        f"managed_chrome_cdp: 无法从 cdp_url 解析端口: {attach_cdp_url!r}"
                    )
                # 受管 Chrome 拉起为 macOS-only 系统操作，下沉到 _macos_browser。
                # 返回值 >0 表示本引擎新拉起（close 时须终止）；0 表示复用既有健康端点。
                spawned_pid = await asyncio.to_thread(
                    _macos_browser.ensure_managed_chrome, port, attach_cdp_url
                )
                if spawned_pid:
                    self._managed_chrome_pid = spawned_pid
            await self._launch_web_attach_cdp(attach_cdp_url)
            return

        logger.info("[engine] web_launch 独立浏览器模式，url=%s", self._url)
        self._playwright = await async_playwright().start()
        launch_kwargs: dict = {}
        effective_channel = _resolve_effective_channel(self._channel, self._platform)
        self._effective_channel = effective_channel
        if effective_channel:
            launch_kwargs["channel"] = effective_channel
        # headless 控制：默认有头（本地调试可见浏览器窗口），
        # CI 设 UITA_WEB_HEADLESS=true 走无头
        headless = os.environ.get("UITA_WEB_HEADLESS", "").strip().lower() in ("1", "true", "yes")
        launch_kwargs["headless"] = headless
        # 禁用浏览器权限弹窗（edge://permission-request-dialog 等），
        # 避免弹窗触发页面切换导致元素定位失败
        launch_kwargs["args"] = [
            "--disable-notifications",
            "--disable-popup-blocking",
            "--disable-infobars",
            "--disable-features=PermissionChip,PermissionRequestDialog,"
            "NotificationPermissionRevocation",
            # 自动授予所有权限请求（不弹窗直接允许）
            "--enable-features=AutoGrantPermissions",
            "--unsafely-treat-insecure-origin-as-secure=*",
        ]
        if self._start_maximized and not headless:
            launch_kwargs["args"].append("--start-maximized")
        self._browser = await self._playwright.chromium.launch(**launch_kwargs)
        context_kwargs: dict = {}
        if self._storage_state and os.path.exists(self._storage_state):
            context_kwargs["storage_state"] = self._storage_state
        # 预授予常见权限，避免网页权限请求弹窗阻塞自动化
        context_kwargs["permissions"] = [
            "notifications",
            "geolocation",
            "camera",
            "microphone",
            "clipboard-read",
            "clipboard-write",
        ]
        context_kwargs["ignore_https_errors"] = True
        if self._start_maximized and not headless:
            context_kwargs["no_viewport"] = True
        self._context = await self._browser.new_context(**context_kwargs)
        self._attach_network_listeners()
        self._page = await self._context.new_page()
        await self._page.goto(self._url, wait_until="domcontentloaded")
        self._main_page = self._page
        self._page_stack = []
        logger.info("[engine] web_launch 已打开: %s", self._url)

    @staticmethod
    def _cdp_port_from_url(cdp_url: str) -> Optional[int]:
        """从形如 http://127.0.0.1:9222 的 cdp_url 中解析端口号（平台无关）。"""
        try:
            from urllib.parse import urlparse

            port = urlparse(cdp_url).port
            return int(port) if port else None
        except Exception:  # noqa: BLE001
            return None

    async def _launch_web_attach_cdp(self, cdp_url: str) -> None:
        """CDP attach 到用户预先启动的真实受管 Chrome（DLP 拦截验证专用）。

        与 _launch_web() 的 chromium.launch() 根本区别：不新起浏览器进程，而是
        connect_over_cdp 连接一个已经运行、受 SampleAgent 客户端管控的真实 Chrome
        （启动命令示例：
            open -na "Google Chrome" --args --remote-debugging-port=9222 \
                 --user-data-dir="$HOME/.uita-managed-chrome"
        ）。只有在这种真实受管进程内发起的上传，才会被 SampleAgent DLP 策略拦截；
        Playwright 自己 launch 的临时匿名 profile 进程不受管、不会触发拦截。

        导航复用已有 context/page（真实浏览器已有窗口），必要时新建 page 并
        goto(self._url)，其余能力（截图/点击/网络监听等）与 _launch_web() 完全一致。
        """
        logger.info("[engine] web_attach_cdp 连接受管真实 Chrome: cdp_url=%s url=%s",
                    cdp_url, self._url)
        # CDP attach 连的必然是真实 Chrome（9222 调试端口进程），强制 channel=chrome，
        # 以便 bring_browser_to_foreground() 能按 com.google.Chrome 定位并前置窗口
        # （否则 _effective_channel 沿用 app extra 的空/其它值 → channel_to_bundle 命中失败）。
        self._effective_channel = "chrome"
        self._playwright = await async_playwright().start()
        last_err = None
        for attempt in range(1, 4):
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(
                    cdp_url, timeout=30000
                )
                break
            except Exception as exc:
                last_err = exc
                logger.warning("[engine] web_attach_cdp 连接失败 (%d/3): %s", attempt, exc)
                await asyncio.sleep(2)
        else:
            # 分级诊断：区分"端口无人监听（Chrome 没启动 / 调试端口未开）"与
            # "端口有 Chrome 但 CDP 端点不可被 Playwright 接管"（如日常 Chrome 抢占端口、
            # Chrome-for-Testing / 降级端点），两者的处置完全不同。
            owner_pid = await asyncio.to_thread(
                _macos_browser.detect_cdp_owner_pid, cdp_url
            )
            if owner_pid is None:
                diag = (
                    f"调试端口无人监听（{cdp_url} 未开放）——Chrome 未以调试端口启动。"
                    " 请启动带独立 user-data-dir 的受管 Chrome，例如："
                    " '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'"
                    " --remote-debugging-port=9222 --user-data-dir=$HOME/.uita-managed-chrome"
                    "（勿用 open -na，已有 Chrome 运行时它会复用实例并忽略 --args）"
                )
            else:
                diag = (
                    f"端口被 PID={owner_pid} 占用，但该 CDP 端点无法被 Playwright 接管"
                    " （'Browser context management is not supported'）——通常是日常"
                    " Chrome 抢占了调试端口，或端点已降级。请先退出占用该端口的 Chrome，"
                    " 再用独立 --user-data-dir 重新启动受管 Chrome。"
                )
            raise RuntimeError(
                f"web_attach_cdp 连接失败: {diag}；底层错误: {last_err}"
            ) from last_err

        # 锁定"监听调试端口的那个 Chrome 进程 PID"为受管目标。macOS 上可能同时存在
        # 日常 Chrome 与本独立受管 profile（标题相同），后续窗口前置/原生文件选择器
        # 必须锁定本进程，否则键盘与文件对话框事件会发到日常 Chrome。
        self._native_browser_pid = await asyncio.to_thread(
            _macos_browser.detect_cdp_owner_pid, cdp_url
        )
        if self._native_browser_pid is not None:
            logger.info("[engine] web_attach_cdp 锁定受管 Chrome PID=%d", self._native_browser_pid)

        if not self._browser.contexts:
            raise RuntimeError("web_attach_cdp 已连接但无 BrowserContext（真实 Chrome 无窗口？）")
        self._context = self._browser.contexts[0]
        self._attach_network_listeners()
        # 复用真实浏览器已有页面；无非工具页则新开一个。
        existing = await self._collect_pages()
        self._page = existing[0]["page"] if existing else await self._context.new_page()
        # attach 复用受管 Chrome 既有持久 context，无法像 launch 那样用
        # new_context(storage_state=...) 自动注入登录态，故在此手动注入
        # storage_state（cookies + 各 origin 的 localStorage），使控制台步骤在
        # 受管 Chrome 里也具备云服务端登录态（文叔叔上传步骤才能触发真实 DLP 拦截）。
        # 注入需在导航到业务 URL 之前完成，见 _inject_storage_state_into_attached_context()。
        await self._inject_storage_state_into_attached_context()
        await self._page.goto(self._url, wait_until="domcontentloaded")
        self._main_page = self._page
        self._page_stack = []
        logger.info("[engine] web_attach_cdp 已打开: %s", self._page.url)
        # 受管 Chrome 首次拉起时默认窗口偏小，attach 后立即铺满主屏可视区，保证 web
        # 步骤截图完整、拖拽落点稳定。窗口最大化为 macOS-only 系统操作，下沉到
        # _macos_browser.maximize_front_chrome_window（依赖此前 attach 时受管窗口已在
        # 前台）。失败仅记日志不影响流程。
        if self._native_browser_pid is not None:
            try:
                ok = await asyncio.to_thread(
                    _macos_browser.maximize_front_chrome_window, self._native_browser_pid
                )
                logger.info("[engine] web_attach_cdp 受管 Chrome 窗口最大化: %s", ok)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[engine] web_attach_cdp 最大化窗口失败（忽略）: %s", exc)

    async def _inject_storage_state_into_attached_context(self) -> None:
        """把 self._storage_state 注入到 CDP attach 复用的持久 context。

        launch 模式由 new_context(storage_state=...) 自动完成；attach 模式复用既有
        context 无此入口，须手动：① context.add_cookies 注入全部 cookie；② 逐个
        origin 导航后用 JS 写入 localStorage。仅在 storage_state 文件存在时执行；
        失败降级为记日志不抛（登录态缺失会在后续控制台步骤自然暴露），零回归到
        "未注入"行为。"""
        import json as _json

        path = self._storage_state
        if not path or not os.path.exists(path):
            logger.info("[engine] attach 无 storage_state 可注入（path=%r），跳过", path)
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                state = _json.load(fh)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[engine] attach 读取 storage_state 失败，跳过注入: %s", exc)
            return

        cookies = state.get("cookies") or []
        if cookies:
            try:
                await self._context.add_cookies(cookies)
                logger.info("[engine] attach 已注入 %d 个 cookie", len(cookies))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[engine] attach 注入 cookie 失败（部分或全部）: %s", exc)

        for origin in state.get("origins") or []:
            origin_url = origin.get("origin")
            items = origin.get("localStorage") or []
            if not origin_url or not items:
                continue
            try:
                await self._page.goto(origin_url, wait_until="domcontentloaded")
                await self._page.evaluate(
                    """(items) => {
                        for (const it of items) {
                            try { localStorage.setItem(it.name, it.value); } catch (e) {}
                        }
                    }""",
                    items,
                )
                logger.info("[engine] attach 已注入 %s 的 %d 项 localStorage",
                            origin_url, len(items))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[engine] attach 注入 localStorage 失败 origin=%s: %s",
                               origin_url, exc)

    @staticmethod
    async def _force_cleanup_playwright_driver(stop_fn: object) -> None:
        """Terminate exact Playwright driver when graceful stop times out."""
        manager = getattr(stop_fn, "__self__", None)
        connection = getattr(manager, "_connection", None)
        transport = getattr(connection, "_transport", None)
        proc = getattr(transport, "_proc", None)

        if proc is not None and getattr(proc, "returncode", None) is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=3.0)
                logger.warning("[hang-trace] Playwright driver force-killed after stop timeout")
            except Exception as exc:
                logger.warning("[hang-trace] Playwright driver force-kill failed: %s", exc)

        stopped = getattr(transport, "_stopped_future", None)
        if stopped is not None and not stopped.done():
            stopped.set_result(None)

        if connection is not None:
            try:
                connection.cleanup("Playwright stop timed out")
            except Exception as exc:
                logger.warning("[hang-trace] Playwright connection cleanup failed: %s", exc)

    async def close(self, kill_app: bool = False) -> None:
        """断开连接；kill_app=True 时同时终止应用进程。"""
        for obj, method in [(self._browser, "close"), (self._playwright, "stop")]:
            if obj:
                started = time.monotonic()
                logger.log(25, "[hang-trace] START stage=playwright_close method=%s", method)
                try:
                    await asyncio.wait_for(
                        getattr(obj, method)(), timeout=_CLOSE_TIMEOUT_S,
                    )
                    logger.log(
                        25, "[hang-trace] END stage=playwright_close method=%s elapsed_ms=%d",
                        method, int((time.monotonic() - started) * 1000),
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[hang-trace] TIMEOUT stage=playwright_close method=%s "
                        "elapsed_ms=%d timeout_s=%.1f; continue cleanup",
                        method, int((time.monotonic() - started) * 1000),
                        _CLOSE_TIMEOUT_S,
                    )
                    if method == "stop":
                        await self._force_cleanup_playwright_driver(
                            getattr(obj, method),
                        )
                except (asyncio.CancelledError, Exception) as exc:
                    logger.warning(
                        "[hang-trace] ERROR stage=playwright_close method=%s elapsed_ms=%d error=%s",
                        method, int((time.monotonic() - started) * 1000), exc,
                    )
        self._page_stack = []
        self._network_log.clear()
        self._all_responses_log.clear()
        self._frontend_error_log.clear()
        self._browser = self._playwright = self._context = self._page = self._main_page = None
        if kill_app and self._app_process:
            await asyncio.to_thread(self._app_process.kill_by_port)
        # 终止引擎自己拉起的受管 Chrome（managed_chrome_cdp 且非复用外部端点）。
        # connect_over_cdp 的 browser.close() 只断开 CDP、不杀进程，故须显式 kill，
        # 否则受管 Chrome 会残留。收尾主路径 close_all() → engine.close() 不带 kill_app，
        # 故这里**不**依赖 kill_app：只要 _managed_chrome_pid 有值就关。restart() 会在
        # 调用 close() 前后保护该 PID（临时置 None 再恢复），避免硬重启误杀待复用实例。
        # 复用外部/前次遗留端点时 _managed_chrome_pid 为 None，不误关不属于本引擎的 Chrome。
        if self._managed_chrome_pid is not None:
            pid = self._managed_chrome_pid
            self._managed_chrome_pid = None
            await asyncio.to_thread(_macos_browser.kill_managed_chrome, pid)
        # 清理 L3 截图私有隔离执行域（BoundedExecutor.shutdown() 内部已处理
        # cancel_futures/wait=False 语义，未创建过时安全跳过）。
        self._l3_guard.shutdown()

    async def is_healthy(self) -> bool:
        if self._page is None:
            return False
        try:
            await asyncio.wait_for(self._page.evaluate("1"), timeout=3)
            return True
        except Exception:
            return False

    async def restart(self) -> None:
        """杀进程 + 重新 launch（硬重启）。

        managed_chrome_cdp 模式下保护受管 Chrome：硬重启只重连 CDP，不终止那个
        独立受管 Chrome 进程（跨 close() 保存/恢复 _managed_chrome_pid），避免每次
        restart 都杀掉再重拉——受管 Chrome 冷启动慢、且会丢失已注入的登录态上下文。
        launch() 里 ensure_managed_chrome 命中健康端点会直接复用，零重复拉起。
        """
        preserved_managed_pid = self._managed_chrome_pid
        self._managed_chrome_pid = None  # 防止 close() 杀掉待复用的受管 Chrome
        await self.close()
        self._managed_chrome_pid = preserved_managed_pid
        if self._app_process:
            await asyncio.to_thread(self._app_process.kill_by_port)
        await self.launch()

    async def reconnect(self) -> None:
        """仅重连 CDP，不杀进程（page 已存在则直接返回）。"""
        if self._page is not None:
            return
        await self.launch()

    # ── 网络诊断（失败响应/请求捕获） ──────────────────────────────
    # PlaywrightEngine 专属能力（非 EngineBase 契约，R9：引擎专用能力放具体
    # 类），供 core/hooks/builtin/network_diagnostics.py 在 step 失败时提取
    # 使用。挂载于 BrowserContext 级别（覆盖该 context 下全部 page/弹窗，
    # 无需在每次切换/新开页面时重新挂载）。见
    # docs/plans/archive/04-report_portal-report-实施纪要.md

    def _attach_network_listeners(self) -> None:
        """在当前 self._context 上挂载 response/requestfailed 监听，并挂载
        前端 JS 异常监听（console error / pageerror）。

        launch()（Electron CDP attach）与 _launch_web()（独立浏览器）均在
        self._context 赋值后立即调用；restart()/reconnect() 会重新 launch()，
        新 context 会重新挂载（旧 context 已随 close() 销毁，无需手动摘除）。

        前端异常监听是 Page 级事件（BrowserContext 无 console/pageerror），
        因此：① 挂 `context.on("page", ...)` 自动覆盖注册之后新建的页面；
        ② 额外遍历一次 `context.pages` 覆盖注册之前已存在的页面（Electron
        CDP attach 场景下页面可能早于本次挂载就已打开），两者互补不遗漏。
        见 docs/plans/archive/04-report_portal-report-实施纪要.md §七。
        """
        if not self._context:
            return
        try:
            self._context.on("response", self._on_response)
            self._context.on("requestfailed", self._on_request_failed)
            self._context.on("page", self._attach_frontend_error_listeners)
            for _existing_page in list(getattr(self._context, "pages", [])):
                self._attach_frontend_error_listeners(_existing_page)
        except Exception as exc:
            logger.warning("[engine] 网络监听挂载失败（忽略，不影响主流程）: %s", exc)

    def _attach_frontend_error_listeners(self, page) -> None:
        """在单个 Page 上挂载 console/pageerror 监听（供新开页面 & 已存在
        页面复用）。异常吞掉不上抛，不影响主流程。"""
        try:
            page.on("console", self._on_console_message)
            page.on("pageerror", self._on_page_error)
        except Exception as exc:
            logger.debug("[engine] 前端异常监听挂载失败（忽略）: %s", exc)

    async def _on_response(self, response) -> None:
        """记录网络响应，异常吞掉不上抛——网络诊断功能本身故障绝不能影响
        用例主流程判定。

        双写两个缓冲：
          - `_network_log`（第一期，行为不变）：仅 status>=400 的响应，
            供 get_recent_network_errors() 使用。
          - `_all_responses_log`（2026-08-19 新增）：仅 resource_type 为
            xhr/fetch 的响应（不论 status，过滤图片/css/字体等静态资源
            降噪），供业务错误规则扫描 HTTP 200 但 body 是业务错误码的场景。
        两个条件均不满足（非 API 资源 且 状态正常）时早退，不构造记录，
        与改造前"status<400 直接 return"的既有行为完全等价（现有单测的
        mock request 未显式设置 resource_type 时非字符串 "xhr"/"fetch"，
        is_api 恒为 False，零回归）。
        """
        try:
            request = response.request
            is_api = getattr(request, "resource_type", "") in _API_RESOURCE_TYPES
            is_error_status = response.status >= 400
            if not is_api and not is_error_status:
                return
            try:
                response_body = await response.text()
            except Exception:
                response_body = ""
            record = {
                "url": response.url,
                "method": request.method,
                "status": response.status,
                "status_text": response.status_text,
                "request_headers": dict(request.headers),
                "request_body": (request.post_data or "")[:_NETWORK_BODY_TRUNCATE],
                "response_headers": dict(response.headers),
                "response_body": response_body[:_NETWORK_BODY_TRUNCATE],
                "timestamp": time.time(),
                "failure_text": "",
            }
            if is_api:
                self._all_responses_log.append(record)
            if is_error_status:
                self._network_log.append(record)
        except Exception as exc:
            logger.debug("[engine] _on_response 记录网络日志异常（忽略）: %s", exc)

    def _on_console_message(self, message) -> None:
        """记录浏览器 console.error 输出（前端异常常见信号之一）。仅记录
        type=="error"，过滤 log/warning/info 等噪音。"""
        try:
            if getattr(message, "type", "") != "error":
                return
            page = getattr(message, "page", None)
            self._frontend_error_log.append({
                "kind": "console",
                "text": (getattr(message, "text", "") or "")[:_NETWORK_BODY_TRUNCATE],
                "url": getattr(page, "url", "") or "",
                "timestamp": time.time(),
            })
        except Exception as exc:
            logger.debug("[engine] _on_console_message 记录异常（忽略）: %s", exc)

    def _on_page_error(self, error) -> None:
        """记录未捕获的页面 JS 异常（Error 对象，message/stack）。"""
        try:
            message = getattr(error, "message", "") or str(error)
            stack = getattr(error, "stack", "") or ""
            text = f"{message}\n{stack}" if stack else message
            self._frontend_error_log.append({
                "kind": "pageerror",
                "text": text[:_NETWORK_BODY_TRUNCATE],
                "url": "",
                "timestamp": time.time(),
            })
        except Exception as exc:
            logger.debug("[engine] _on_page_error 记录异常（忽略）: %s", exc)

    async def _on_request_failed(self, request) -> None:
        """记录请求失败（DNS/超时/连接拒绝/CORS 等，无 HTTP 响应）。"""
        try:
            self._network_log.append({
                "url": request.url,
                "method": request.method,
                "status": None,
                "status_text": "",
                "request_headers": dict(request.headers),
                "request_body": (request.post_data or "")[:_NETWORK_BODY_TRUNCATE],
                "response_headers": {},
                "response_body": "",
                "timestamp": time.time(),
                "failure_text": request.failure or "",
            })
        except Exception as exc:
            logger.debug("[engine] _on_request_failed 记录网络日志异常（忽略）: %s", exc)

    def get_recent_network_errors(self, since_ts: float | None = None) -> list[dict]:
        """返回环形缓冲中的失败网络记录，可选按时间窗口过滤。

        Args:
            since_ts: 仅返回 timestamp >= since_ts 的记录；None 返回全部缓冲内容。

        Returns:
            记录列表，按发生顺序排列，每条含 url/method/status/status_text/
            request_headers/request_body/response_headers/response_body/
            timestamp/failure_text。空缓冲或无匹配记录返回 []。
        """
        if since_ts is None:
            return list(self._network_log)
        return [r for r in self._network_log if r["timestamp"] >= since_ts]

    def get_recent_responses(self, since_ts: float | None = None) -> list[dict]:
        """返回环形缓冲中的全量 API 响应记录（xhr/fetch，不论 status），
        可选按时间窗口过滤。字段结构与 `get_recent_network_errors()` 一致。

        供 `core/hooks/builtin/network_diagnostics.py` 扫描"HTTP 200 但
        body 是业务错误码"场景（业务错误规则需要看到 2xx 记录，第一期的
        `_network_log` 只存 4xx/5xx，覆盖不到）。见
        docs/plans/archive/04-report_portal-report-实施纪要.md §七。
        """
        if since_ts is None:
            return list(self._all_responses_log)
        return [r for r in self._all_responses_log if r["timestamp"] >= since_ts]

    def get_recent_frontend_errors(self, since_ts: float | None = None) -> list[dict]:
        """返回环形缓冲中的前端 JS 异常记录（console error + 未捕获
        pageerror），可选按时间窗口过滤。

        每条记录含 kind（"console"/"pageerror"）/text/url/timestamp。
        """
        if since_ts is None:
            return list(self._frontend_error_log)
        return [r for r in self._frontend_error_log if r["timestamp"] >= since_ts]

    # ── 页面状态复位 ────────────────────────────────────────────

    async def reset_page_state(self, level: str = "soft") -> None:
        """复位页面状态（三档）。

        soft:   goto 原始 URL（推荐，保留登录态）
        medium: soft + 清 sessionStorage
        hard:   medium + 清 localStorage + IndexedDB + Cache（会丢登录态）
        """
        if not self._page:
            logger.warning("[engine] reset_page_state: 无活跃页面，跳过")
            return
        if level not in ("soft", "medium", "hard"):
            raise ValueError(f"未知 reset level: {level}")

        origin_url = self._page.url.split("#")[0].split("?")[0]

        if level == "hard":
            try:
                await self._page.evaluate("""async () => {
                    try { localStorage.clear(); } catch(e) {}
                    if (indexedDB.databases) {
                        const dbs = await indexedDB.databases();
                        await Promise.all(dbs.map(db => new Promise(res => {
                            const r = indexedDB.deleteDatabase(db.name);
                            r.onsuccess = r.onerror = r.onblocked = () => res();
                        })));
                    }
                    if (window.caches) {
                        const keys = await caches.keys();
                        await Promise.all(keys.map(k => caches.delete(k)));
                    }
                }""")
                logger.info("[engine] hard: localStorage/IndexedDB/Cache 已清")
            except Exception as exc:
                logger.warning("[engine] hard 清除异常: %s", exc)

        if level in ("medium", "hard"):
            try:
                await self._page.evaluate("sessionStorage.clear()")
                logger.info("[engine] %s: sessionStorage 已清", level)
            except Exception as exc:
                logger.warning("[engine] sessionStorage 清除异常: %s", exc)

        try:
            await self._page.goto(origin_url, wait_until="domcontentloaded")
            logger.info("[engine] %s: 已回到 %s", level, origin_url)
        except Exception as exc:
            logger.warning("[engine] goto 失败，回退 reload: %s", exc)
            try:
                await self._page.reload(wait_until="domcontentloaded")
            except Exception as exc2:
                logger.warning("[engine] reload 也失败: %s", exc2)
        # 复位后清空页面栈（回到根状态）
        self._page_stack = []
        logger.debug("[engine] reset_page_state: _page_stack 已清空")

    # ── 多窗口管理 ──────────────────────────────────────────────

    def snapshot_pages(self) -> set:
        """返回当前所有有效 page 的集合快照（同步）。"""
        if not self._browser:
            return set()
        return {
            p
            for ctx in self._browser.contexts
            for p in ctx.pages
            if not any(p.url.startswith(s) for s in _SKIP_URLS)
        }

    # ── 页面栈管理 ───────────────────────────────────────────────

    def push_page_stack(self) -> None:
        """将当前 self._page 压入页面历史栈（切换到新页面之前调用）。

        去重：同一 Page 对象不重复入栈。
        """
        if self._page and self._page not in self._page_stack:
            self._page_stack.append(self._page)
            logger.debug("[engine] push_page_stack: depth=%d  page=%s",
                         len(self._page_stack),
                         getattr(self._page, "url", "?")[:60])

    async def pop_to_previous_page(self) -> Optional[Page]:
        """弹出上一个存活页面并切换到它。

        - 跳过已关闭（evaluate 超时）的 Page；
        - 栈空时回退到 _main_page；
        - 返回切换后的 Page（若无任何可用页面返回 None）。
        """
        while self._page_stack:
            prev = self._page_stack.pop()
            try:
                await asyncio.wait_for(prev.evaluate("1"), timeout=2)
                self._page = prev
                logger.info("[engine] pop_to_previous_page: 已回退 page=%s  remaining_stack=%d",
                            getattr(prev, "url", "?")[:80], len(self._page_stack))
                return prev
            except Exception:
                logger.debug("[engine] pop_to_previous_page: 跳过已关闭页面")
                continue
        # 栈空：回退到 _main_page
        if self._main_page:
            self._page = self._main_page
            logger.info("[engine] pop_to_previous_page: 栈空，回退 main_page=%s",
                        getattr(self._main_page, "url", "?")[:80])
            return self._main_page
        return None

    async def detect_and_switch_new_page(
        self, before_pages: set, wait_ms: int = 500
    ) -> bool:
        """检测 click 后是否弹出新窗口，有则 push 当前页到栈并切换到新页面。"""
        if not self._context:
            return False
        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)

        new_pages = self.snapshot_pages() - before_pages
        if not new_pages:
            return False

        new_page = next(iter(new_pages))
        try:
            await asyncio.wait_for(new_page.evaluate("1"), timeout=2)
        except Exception:
            return False

        # 切换前把当前页压栈，实现"关闭后可回退"
        self.push_page_stack()
        self._page = new_page
        logger.info("[engine] 切换到新窗口（旧页已入栈 depth=%d）: %s",
                    len(self._page_stack), new_page.url[:80])
        return True

    async def ensure_page_alive(self) -> None:
        """确保 self._page 可用，不可用时优先 pop 页面栈，栈空则回退 _main_page。"""
        if self._page is None:
            return
        try:
            await asyncio.wait_for(self._page.evaluate("1"), timeout=2)
        except Exception:
            logger.info("[engine] ensure_page_alive: 当前页不可用，尝试 pop_to_previous_page ...")
            await self.pop_to_previous_page()

    async def refresh_active_page(
        self, target_html: Optional[str | list[str]] = None
    ) -> Page:
        """重新定位目标页面（target_html 临时覆盖）。"""
        if not self._browser:
            raise RuntimeError("未连接，请先 launch()")
        saved, self._target_html = self._target_html, (target_html or self._target_html)
        try:
            self._page = await self._find_page()
        finally:
            self._target_html = saved
        return self._page

    # ── 元素查找 ────────────────────────────────────────────────

    def _page_or_raise(self) -> Page:
        if self._page is None:
            raise RuntimeError("engine 未启动，请先调用 launch()")
        return self._page

    @staticmethod
    async def _first_visible(locator):
        """返回第一个可见候选；全部隐藏时返回 None，让 resolver 继续后续策略。"""
        count = await locator.count()
        for index in range(count):
            candidate = locator.nth(index) if count > 1 else locator.first
            if await candidate.is_visible():
                return candidate
        return None

    async def find_by_id(self, element_id: str) -> Optional[ElementRef]:
        page = self._page_or_raise()

        # JS 表达式（IIFE / document.* / arrow function）→ page.evaluate()
        # 返回真值 = 元素"存在"（无 DOM handle，纯判定语义，click/type 需配合坐标或
        # 另写 locator，assert[] 可直接用）；异常/返回假值 = NOT_FOUND。
        if _is_js_expr(element_id):
            try:
                result = await page.evaluate(element_id)
                if result:
                    return ElementRef(element_id=element_id, raw=None)
            except Exception as exc:
                logger.debug("find_by_id JS expr error: %s", exc)
            return None

        # 标准 Playwright locator
        try:
            locator = await self._first_visible(page.locator(element_id))
            if locator is None:
                return None
            return ElementRef(element_id=element_id, raw=locator)
        except Exception as exc:
            logger.debug("find_by_id('%s') error: %s", element_id, exc)
            return None

    async def count_by_id(self, element_id: str) -> int:
        """Return precise count using Playwright locator.count()."""
        page = self._page_or_raise()
        try:
            return await page.locator(element_id).count()
        except Exception as exc:
            logger.debug("count_by_id('%s') error: %s", element_id, exc)
            return 0

    async def find_by_text(self, text: str) -> Optional[ElementRef]:
        page = self._page_or_raise()
        try:
            locator = await self._first_visible(page.get_by_text(text, exact=False))
            if locator is None:
                return None
            return ElementRef(
                element_id=f"text={text}",
                raw=locator,
                text=text,
            )
        except Exception as exc:
            logger.debug("find_by_text('%s') error: %s", text, exc)
            return None

    async def find_by_role(self, role: str, name: str = "") -> Optional[ElementRef]:
        page = self._page_or_raise()
        try:
            kwargs = {"name": name} if name else {}
            locator = await self._first_visible(page.get_by_role(role, **kwargs))  # type: ignore
            if locator is None:
                return None
            return ElementRef(
                element_id=f"role={role}",
                raw=locator,
            )
        except Exception as exc:
            logger.debug("find_by_role error: %s", exc)
            return None

    async def find_by_label(self, text: str) -> Optional[ElementRef]:
        """通过关联 <label> 文本定位元素（L2.5 LABEL_MATCH，见计划文档
        docs/plans/20260803150000-label-match-playwright-get-by-label.md）。

        element_id 前缀 "label=" 属于语义描述符（非真实 CSS），自愈写回时须经
        core/resolver/resolver.py::_try_resolve_real_selector() 反查真实 CSS
        （该函数已识别 "label=" 前缀，见计划文档 §3.5b），不可直接写库。
        """
        page = self._page_or_raise()
        try:
            locator = await self._first_visible(page.get_by_label(text))
            if locator is None:
                return None
            return ElementRef(
                element_id=f"label={text}",
                raw=locator,
            )
        except Exception as exc:
            logger.debug("find_by_label('%s') error: %s", text, exc)
            return None

    async def resolve_css_selector(self, locator) -> Optional[str]:
        """从 Playwright Locator 反查唯一 CSS selector（L2/L3 自愈回写用）。

        仅 PlaywrightEngine 实现，不放入 EngineBase（遵循宪法 R9）。
        优先级：id > data-testid > aria-label > 稳定 class 组合 > None。
        反查结果须通过唯一性验证（count==1），避免歧义 selector 写库。

        Returns:
            唯一 CSS selector 或 None（取不到 / 不唯一 / 异常）
        """
        if locator is None:
            return None
        try:
            sel = await locator.evaluate("""el => {
                if (el.id) return '#' + CSS.escape(el.id);
                const testid = el.getAttribute('data-testid');
                if (testid) return '[data-testid="' + CSS.escape(testid) + '"]';
                const label = el.getAttribute('aria-label');
                if (label) return '[aria-label="' + label + '"]';
                const classes = [...el.classList]
                    .filter(c => !/^(active|hover|focus|selected|disabled|loading|open|visible)$/.test(c))
                    .slice(0, 3);
                if (classes.length > 0)
                    return el.tagName.toLowerCase() + classes.map(c => '.' + CSS.escape(c)).join('');
                return null;
            }""")
            if not sel:
                return None
            count = await self.count_by_id(sel)
            if count == 1:
                return sel
            logger.debug("[CSS反查] selector=%r 命中 %d 个元素，不唯一，跳过", sel, count)
            return None
        except Exception as exc:
            logger.debug("[CSS反查] resolve_css_selector 异常（忽略）: %s", exc)
            return None

    async def resolve_css_selector_from_coords(self, cx: float, cy: float) -> Optional[str]:
        """从坐标反查 CSS selector（LLM_VISUAL 自愈回写用）。

        通过 elementFromPoint 获取坐标处 DOM 节点并提取稳定 selector，
        反查结果同样须通过唯一性验证（count==1）。

        Returns:
            唯一 CSS selector 或 None（取不到 / 不唯一 / 异常）
        """
        try:
            page = self._page_or_raise()
            sel = await page.evaluate(
                """([x, y]) => {
                    const el = document.elementFromPoint(x, y);
                    if (!el) return null;
                    if (el.id) return '#' + CSS.escape(el.id);
                    const testid = el.getAttribute('data-testid');
                    if (testid) return '[data-testid="' + CSS.escape(testid) + '"]';
                    const label = el.getAttribute('aria-label');
                    if (label) return '[aria-label="' + label + '"]';
                    const classes = [...el.classList]
                        .filter(c => !/^(active|hover|focus|selected|disabled|loading|open|visible)$/.test(c))
                        .slice(0, 3);
                    if (classes.length > 0)
                        return el.tagName.toLowerCase() + classes.map(c => '.' + CSS.escape(c)).join('');
                    return null;
                }""",
                [cx, cy],
            )
            if not sel:
                return None
            count = await self.count_by_id(sel)
            if count == 1:
                return sel
            logger.debug("[CSS反查] coords(%.1f,%.1f) → %r 不唯一（%d 个），跳过", cx, cy, sel, count)
            return None
        except Exception as exc:
            logger.debug("[CSS反查] resolve_css_selector_from_coords 异常（忽略）: %s", exc)
            return None

    async def find_all_by_role(self, role: str) -> list[ElementRef]:
        page = self._page_or_raise()
        try:
            locators = await page.get_by_role(role).all()  # type: ignore
            return [
                ElementRef(element_id=f"role={role}[{i}]", raw=loc)
                for i, loc in enumerate(locators)
            ]
        except Exception as exc:
            logger.debug("find_all_by_role error: %s", exc)
            return []

    async def find_by_id_scoped(self, element_id: str, container: str) -> Optional[ElementRef]:
        """在 container selector 的 scope 内查找 element_id（供 Resolver 的
        active_container 两级降级使用，非 EngineBase 契约，仅本引擎专属，
        见 docs/plans/20260703102900-...）。"""
        page = self._page_or_raise()
        try:
            locator = await self._first_visible(page.locator(container).locator(element_id))
            if locator is None:
                return None
            return ElementRef(element_id=f"{container} >> {element_id}", raw=locator)
        except Exception as exc:
            logger.debug("find_by_id_scoped(%r, %r) error: %s", element_id, container, exc)
            return None

    # ── Actions ────────────────────────────────────────────────

    async def click(self, element: ElementRef) -> None:
        """点击元素。优先标准 click（5s 超时），若元素因不可见/被遮挡而失败
        （如 ant-design hover 触发的按钮、登录弹窗遮罩层拦截，Electron hover
        变更后常见），降级为 force=True 点击（跳过可见性校验）。

        首次 click 设 5s 超时而非 Playwright 默认 30s——元素被遮挡时等满 30s
        才降级 force 是巨大时间浪费，5s 足以覆盖正常动画/过渡场景。"""
        try:
            await element.raw.click(timeout=5000)
        except Exception:
            if not await element.raw.is_visible():
                logger.debug("[engine] click 失败且元素已不可见，禁止 force click")
                raise
            logger.debug("[engine] click 失败但元素仍可见，重试 force=True")
            await element.raw.click(force=True)

    async def get_viewport_metrics(self) -> dict:
        """返回 viewport CSS 尺寸、DPR 与推算的截图像素尺寸（引擎专用能力）。

        为什么需要它（2026-09-24 真机故障）：``take_screenshot()`` 产出的是
        **设备像素**（Retina 下 2400×1600），而 ``*_at_viewport_coords`` 接收的是
        **CSS 像素**（viewport 1200×800）。多模态判读若直接把截图像素坐标喂进来，
        坐标会越界 → 静默点空。调用方应先取本方法拿到 ``dpr``，把判读坐标
        ``÷ dpr`` 后再驱动，而不是从截图尺寸反推。

        注意：``EngineBase`` 的 18 个抽象方法**未因此变更**（宪法 R3：引擎专用
        能力放具体类，不放基类）。

        取不到时返回空 dict（调用方须容错），不抛异常。
        """
        page = self._page or self._main_page
        if page is None:
            return {}
        try:
            m = await page.evaluate(
                "() => ({w: window.innerWidth, h: window.innerHeight,"
                " d: window.devicePixelRatio || 1})"
            )
            dpr = float(m.get("d") or 1) or 1.0
            cw, ch = float(m.get("w") or 0), float(m.get("h") or 0)
            return {
                "css_width": cw,
                "css_height": ch,
                "dpr": dpr,
                "screenshot_width": cw * dpr,
                "screenshot_height": ch * dpr,
            }
        except Exception as exc:  # noqa: BLE001 — 取不到就让调用方走无校验路径
            logger.debug("[engine] get_viewport_metrics 失败: %s", exc)
            return {}

    async def _normalize_viewport_coords(
        self, x: float, y: float, who: str,
    ) -> Tuple[float, float]:
        """把传入坐标归一到 **CSS 像素**，并消除"点空但不报错"的静默失败。

        2026-09-24 真机故障复盘（`report/sentinel/branch-release_20260924+
        20260924_151632/07-device-verification.md`）：多模态按 2400×1600 截图判读
        出 ``(319,1192)``，而 viewport 只有 1200×800 —— ``y=1192`` 越界，
        ``elementFromPoint`` 返回 ``null``（**不报错**），兜底 ``page.mouse.click``
        点在视口外（**也不报错**）→ 界面零变化，4 次驱动全废、耗掉 8 个超额回合，
        且**无法区分**「坐标错」与「元素没响应」。

        三条规则：
          ① 坐标落在 viewport 内 → **原样使用**（既有 `.tcs` 用例走这条，行为不变）；
          ② 越界但 ``÷ DPR`` 后落入 viewport → 判定调用方传了**设备像素**，
             自动换算并 ``warning`` 留痕（不静默改写）；
          ③ 换算后仍越界 → **抛 ``ValueError``**（含传入值/viewport/DPR/建议值），
             让调用方立刻知道坐标系错了，而不是点空。

        metrics 取不到时跳过校验、原样返回（保可用性，不阻断点击）。
        """
        m = await self.get_viewport_metrics()
        cw, ch, dpr = m.get("css_width") or 0, m.get("css_height") or 0, m.get("dpr") or 1
        if not cw or not ch:
            return x, y  # 拿不到 viewport，跳过校验（不阻断）
        if 0 <= x <= cw and 0 <= y <= ch:
            return x, y
        if dpr and dpr != 1:
            sx, sy = x / dpr, y / dpr
            if 0 <= sx <= cw and 0 <= sy <= ch:
                logger.warning(
                    "[engine] %s 收到疑似**设备像素**坐标 (%.0f, %.0f)：超出 viewport "
                    "CSS 尺寸 %.0fx%.0f，但 ÷DPR(%.2f) 后落入范围 → 已自动换算为 "
                    "(%.1f, %.1f)。调用方应先按 get_viewport_metrics()['dpr'] 换算"
                    "（截图是设备像素，本接口收 CSS 像素）。",
                    who, x, y, cw, ch, dpr, sx, sy,
                )
                return sx, sy
        raise ValueError(
            f"{who}: 坐标 ({x:.0f}, {y:.0f}) 超出 viewport CSS 尺寸 "
            f"{cw:.0f}x{ch:.0f}（DPR={dpr:.2f}，截图像素约 "
            f"{cw * dpr:.0f}x{ch * dpr:.0f}）；÷DPR 后 "
            f"({x / dpr if dpr else x:.0f}, {y / dpr if dpr else y:.0f}) 仍越界。"
            f"截图判读坐标须 ÷DPR 换算为 CSS 像素后再传入。"
        )

    async def click_at_viewport_coords(self, x: float, y: float) -> None:
        """在页面 viewport 坐标系内精确点击（L4 视觉定位专用）。

        与 pyautogui 不同，这里直接用 Playwright page.mouse.click()，
        坐标是 **CSS 像素**（viewport 坐标），不涉及屏幕绝对坐标/窗口偏移。

        ★ 2026-07-11 修复（测试管理平台#1070204784135051377 真机发现）：裸 page.mouse.click()
        在 Electron + Vue 路由环境下无法触发 SPA 导航（Vue Router 监听 click 事件而非
        mousedown）。改用 document.elementFromPoint 取真实 DOM 元素后调 .click()，
        走完整事件链（mousedown → mouseup → click → SPA 路由监听器）。

        ★ 2026-09-24 加固：入口做坐标系归一与越界校验（见
        ``_normalize_viewport_coords``）——此前越界坐标会静默点空，调用方无从察觉。
        """
        x, y = await self._normalize_viewport_coords(x, y, "click_at_viewport_coords")
        page = self._page or self._main_page
        # ── L4 坐标点击：优先用 elementFromPoint + el.click() 触发完整事件链 ──
        try:
            handle = await page.evaluate_handle(
                "([x, y]) => document.elementFromPoint(x, y)", [x, y]
            )
            el_handle = handle.as_element() if handle else None
            if el_handle is not None:
                await el_handle.click(force=True)
                return
            # 坐标处无元素：此前静默落到兜底，调用方以为点了但什么都没发生。
            # 坐标已过越界校验，故这里多为「该点确实没有可命中元素」（遮罩/空白区）。
            logger.warning(
                "[engine] click_at_viewport_coords (%.1f, %.1f) 处 elementFromPoint "
                "无元素（空白区/遮罩层？）→ 降级裸 mouse.click，可能不产生界面变化",
                x, y,
            )
        except Exception as exc:
            logger.debug(
                "[engine] click_at_viewport_coords elementFromPoint 分支异常，降级: %s", exc,
            )
        # 兜底：裸 page.mouse.click()（非 SPA 场景的最后手段）
        await page.mouse.click(x, y)

    async def hover_at_viewport_coords(self, x: float, y: float) -> None:
        """在 viewport 坐标处触发 hover（L4 视觉定位专用）。

        先用 elementFromPoint 获取真实 DOM 元素后调 .hover()，
        保证触发 CSS :hover 样式和 JS mouseenter/mouseover 事件链。

        坐标为 **CSS 像素**；入口做坐标系归一与越界校验（2026-09-24 加固）。
        """
        x, y = await self._normalize_viewport_coords(x, y, "hover_at_viewport_coords")
        page = self._page or self._main_page
        try:
            handle = await page.evaluate_handle(
                "([x, y]) => document.elementFromPoint(x, y)", [x, y]
            )
            el_handle = handle.as_element() if handle else None
            if el_handle is not None:
                await el_handle.hover(force=True)
                return
            logger.warning(
                "[engine] hover_at_viewport_coords (%.1f, %.1f) 处无元素 → 降级 mouse.move",
                x, y,
            )
        except Exception as exc:
            logger.debug(
                "[engine] hover_at_viewport_coords elementFromPoint 异常，降级: %s", exc,
            )
        # 兜底：裸 page.mouse.move()（模拟鼠标移入）
        await page.mouse.move(x, y)

    async def type_at_viewport_coords(self, x: float, y: float, text: str, clear: bool = True) -> None:
        """点击 viewport 坐标聚焦后输入文字（L4 视觉定位专用）。

        坐标为 **CSS 像素**；入口做坐标系归一与越界校验（2026-09-24 加固）。
        """
        x, y = await self._normalize_viewport_coords(x, y, "type_at_viewport_coords")
        page = self._page or self._main_page
        await page.mouse.click(x, y)
        if clear:
            import sys
            modifier = "Meta" if sys.platform == "darwin" else "Control"
            await page.keyboard.press(f"{modifier}+a")
        await page.keyboard.type(text)

    async def type_text(self, element: ElementRef, text: str, clear: bool = True) -> None:
        if clear:
            await element.raw.clear()
        await element.raw.type(text)

    async def type_focused(self, text: str, *, via_clipboard: bool = False) -> None:
        """Mirror legacy_runner ``type``: input text into current Playwright focus."""
        keyboard = self._page_or_raise().keyboard
        import sys
        modifier = "Meta" if sys.platform == "darwin" else "Control"
        await keyboard.press(f"{modifier}+a")
        await keyboard.press("Backspace")
        if not text:
            return
        if via_clipboard:
            import pyperclip

            await asyncio.to_thread(pyperclip.copy, text)
            await asyncio.sleep(0.2)
            await keyboard.down(modifier)
            try:
                await asyncio.sleep(0.15)
                await keyboard.press("v")
                await asyncio.sleep(0.15)
            finally:
                await keyboard.up(modifier)
        else:
            await keyboard.type(text)

    async def fill_text(self, element: ElementRef, text: str) -> None:
        """填充文本。优先标准 fill（含可见性校验），若因元素不可见超时，
        降级为 click(force=True) 激活 + 全选清空 + type() 输入。"""
        try:
            await element.raw.fill(text, timeout=10000)
        except Exception as exc:
            if "not visible" in str(exc).lower() or "timeout" in str(exc).lower():
                if not await element.raw.is_visible():
                    logger.debug("[engine] fill 失败且元素不可见，禁止 force 输入: %s", exc)
                    raise
                logger.debug("[engine] fill 失败但元素仍可见，降级为 click+type: %s", exc)
                await element.raw.click(force=True)
                # 全选已有内容并删除，模拟 fill 的清空行为。
                import sys
                modifier = "Meta" if sys.platform == "darwin" else "Control"
                await self._page_or_raise().keyboard.press(f"{modifier}+a")
                await self._page_or_raise().keyboard.press("Backspace")
                await element.raw.type(text)
            else:
                raise

    async def goto_url(self, url: str, *, wait_until: str = "domcontentloaded") -> None:
        await self._page_or_raise().goto(url, wait_until=wait_until)

    async def reload_page(self, *, wait_until: str = "domcontentloaded") -> None:
        await self._page_or_raise().reload(wait_until=wait_until)

    async def wait_load_state(self, state: str, *, timeout_ms: float = 30000) -> None:
        await self._page_or_raise().wait_for_load_state(state, timeout=timeout_ms)

    async def press_key(self, key: str) -> None:
        await self._page_or_raise().keyboard.press(key)

    async def bring_browser_to_foreground(
        self, title_contains: str, *, maximize: bool = False
    ) -> bool:
        """Front current standalone browser window and remember its native PID.

        On macOS, multiple Google Chrome processes may exist. Matching the visible
        window title first prevents a system file chooser from being opened in one
        process while keystrokes are delivered to another one.

        maximize=True 时，前置成功后把该受管进程的主窗口铺满主屏可视区（非全屏
        Space，仅最大化）。CDP attach 复用的 Chrome 常停在小窗口，后续系统级拖拽
        的 window_center 落点又小又易偏出网页拖放区；提前最大化让页面布局稳定、
        落点稳定，也更贴近用户真实使用状态。最大化失败不影响前置结果。
        """
        if sys.platform != "darwin":
            return False
        # 先把 Playwright 操作的目标 page 切到前台标签，使 OS 层窗口标题（kCGWindowName
        # 只反映当前激活标签）变为目标页标题。CDP attach 复用受管 Chrome 时，前台标签
        # 可能停留在别的页面（如登录态注入时导航过的云服务端页），导致按标题匹配
        # title_contains 命中失败。bring_to_front 失败不阻断，继续走 OS 前置兜底。
        if self._page is not None:
            try:
                await self._page.bring_to_front()
                await asyncio.sleep(0.3)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[engine] bring_to_front before foreground failed: %s", exc)
        # 窗口前置为 macOS-only 系统操作，下沉到 _macos_browser；已锁定的受管 PID
        # 作为首选目标传入，返回实际前置的 PID 后回写（launch 模式尚未锁定时用它锁定）。
        result_pid = await asyncio.to_thread(
            _macos_browser.bring_browser_to_foreground,
            self._effective_channel,
            self._native_browser_pid,
            title_contains,
        )
        if result_pid is not None:
            self._native_browser_pid = result_pid
        result = result_pid is not None
        if result and maximize and self._native_browser_pid is not None:
            await asyncio.to_thread(
                _macos_browser.maximize_front_chrome_window, self._native_browser_pid
            )
            await asyncio.sleep(0.5)
        return result

    async def has_open_file_dialog(self) -> bool:
        """Detect the native chooser opened by the current browser page."""
        if sys.platform == "darwin":
            return await asyncio.to_thread(
                _macos_browser.file_dialog_open, self._native_browser_pid
            )
        if os.name != "nt":
            return False
        from uita.engine.win_uia.engine import WinUIAEngine

        helper = WinUIAEngine(app_name="\u6253\u5f00")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            helper._get_uia_executor(), helper._sync_has_open_file_dialog,
        )

    async def goto_path_in_file_dialog(self, path: str) -> bool:
        """Select a path through the native chooser belonging to current browser."""
        if sys.platform == "darwin":
            return await asyncio.to_thread(
                _macos_browser.choose_file_in_dialog, self._native_browser_pid, path
            )
        if os.name != "nt":
            return False
        from uita.engine.win_uia.engine import WinUIAEngine

        helper = WinUIAEngine(app_name="\u6253\u5f00")
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            helper._get_uia_executor(), helper._sync_goto_path_in_file_dialog, path,
        )

    async def hover(self, element: ElementRef) -> None:
        """悬停元素。优先标准 hover（5s 超时），若元素因不可见而失败，
        降级为 force=True hover（跳过可见性校验）。"""
        try:
            await element.raw.hover(timeout=5000)
        except Exception:
            logger.debug("[engine] hover 失败（可能元素不可见），重试 force=True")
            await element.raw.hover(force=True)

    async def scroll(self, element: Optional[ElementRef], direction: str, amount: int) -> None:
        """element 可为 None（scroll[direction] 无元素名调用，滑动当前页面/窗口，
        见 docs/plans/archive/02-dsl-actions-实施纪要.md）。
        Web 场景整页滚动本身不需要先 hover 到某元素，element=None 时跳过 hover。"""
        page = self._page_or_raise()
        delta_map = {
            "up": (0, -amount), "down": (0, amount),
            "left": (-amount, 0), "right": (amount, 0),
        }
        dx, dy = delta_map.get(direction.lower(), (0, amount))
        if element is not None and element.raw is not None:
            await element.raw.hover()
        await page.mouse.wheel(dx, dy)

    # ── 断言辅助 ────────────────────────────────────────────────

    async def get_text(self, element: ElementRef) -> str:
        if element.raw is None:
            return ""  # 坐标型结果（L3.8/L4），无 DOM 引用，无法读文本
        return await element.raw.inner_text()

    async def get_attribute(self, element: ElementRef, attr: str) -> Optional[str]:
        return await element.raw.get_attribute(attr)

    async def is_visible(self, element: ElementRef) -> bool:
        try:
            return await element.raw.is_visible()
        except Exception:
            return False

    async def get_window_title(self) -> str:
        return await self._page_or_raise().title()

    # ── JS 逃生舱 / DOM 覆盖层感知 ────────────────────────────────
    # 均为 PlaywrightEngine 专属方法（非 EngineBase 契约，R9：引擎专用能力
    # 写在具体引擎类），供 apps/<app>/actions.py（自定义 JS 探针）与
    # Resolver 的 active_container 两级降级使用。见 docs/plans/20260703102900-...

    async def evaluate(self, js: str, arg=None):
        """执行任意 JS 表达式/函数，返回结果。仅本引擎拥有；调用方需先
        isinstance(ctx.engine, PlaywrightEngine) 判断后才能调用。"""
        page = self._page_or_raise()
        return await (page.evaluate(js, arg) if arg is not None else page.evaluate(js))

    async def evaluate_on_target(self, title_substr: str, js_code: str) -> str:
        """在指定标题的 CDP target 上执行 JS（跨 Electron BrowserWindow）。

        Playwright connectOverCDP 不自动跟踪后续新开的 Electron BrowserWindow，
        本方法通过 CDP HTTP 端点发现 target，创建新 CDP 连接在其上执行 JS。

        用途：操控独立 Electron 预览窗口（如 DemoGallery 点击照片打开的媒体查看器）。

        Args:
            title_substr: 目标窗口标题的子串（如 "图片查看器"）
            js_code: 要执行的 JS 表达式

        Returns:
            JS 执行结果的字符串表示；target 未找到时返回 "ERROR: target not found: ..."
        """
        import json as _json
        import urllib.request

        # 1. 查找目标 target
        try:
            resp = urllib.request.urlopen(
                f"{self._cdp_url}/json/list", timeout=5,
            )
            targets = _json.loads(resp.read())
        except Exception as exc:
            return f"ERROR: CDP query failed: {exc}"

        target_page = None
        for t in targets:
            if title_substr in t.get("title", ""):
                target_page = t
                break
        if not target_page:
            return f"ERROR: target not found: {title_substr}"

        # 2. 创建新的 CDP 连接（会发现所有当前 targets，包括新 Electron 窗口）
        browser = await self._playwright.chromium.connect_over_cdp(self._cdp_url)
        try:
            # 3. 在 contexts 中查找目标页面
            viewer_page = None
            for ctx in browser.contexts:
                for page in ctx.pages:
                    try:
                        title = await page.title()
                        if title_substr in title:
                            viewer_page = page
                            break
                    except Exception:
                        pass
                if viewer_page:
                    break

            if not viewer_page:
                return f"ERROR: page not accessible: {title_substr}"

            # 4. 执行 JS
            result = await viewer_page.evaluate(js_code)
            return str(result) if result is not None else "null"
        finally:
            # disconnect 不影响主引擎连接（connectOverCDP 的 close 仅断开此连接）
            try:
                await browser.close()
            except Exception:
                pass

    async def evaluate_on_target_url(self, url_substr: str, js_code: str) -> str:
        """在 **URL 含指定子串** 的 CDP target 上执行 JS（跨 Electron BrowserWindow）。

        与 ``evaluate_on_target`` 的唯一差异：按 URL 匹配而非窗口标题。

        为什么必须有这个变体（实测教训）：DemoGallery 媒体查看器窗口的 CDP ``title`` 是
        **空字符串**，而 ``evaluate_on_target`` 用的是 ``title_substr in title`` —— 传入
        ``""`` 时对任何 target 都成立，会命中列表里的**第一个** target（主窗口），
        于是 JS 跑在主窗口里、量出来的却是"查看器已就绪"的假结论。
        查看器窗口的 URL 稳定含 ``media-viewer``，按 URL 匹配可精确命中。

        Args:
            url_substr: 目标窗口 URL 的子串（如 ``"media-viewer"``）
            js_code: 要执行的 JS 表达式

        Returns:
            JS 执行结果的字符串表示；未命中时返回 ``"ERROR: ..."``
        """
        import json as _json
        import urllib.request

        if not url_substr:
            return "ERROR: url_substr 不能为空（空串会命中任意 target）"

        # 1. 查找目标 target（按 URL）
        try:
            resp = urllib.request.urlopen(
                f"{self._cdp_url}/json/list", timeout=5,
            )
            targets = _json.loads(resp.read())
        except Exception as exc:
            return f"ERROR: CDP query failed: {exc}"

        if not any(url_substr in str(t.get("url", "")) for t in targets):
            return f"ERROR: target not found by url: {url_substr}"

        # 2. 创建新的 CDP 连接（会发现所有当前 targets，包括新 Electron 窗口）
        browser = await self._playwright.chromium.connect_over_cdp(self._cdp_url)
        try:
            # 3. 在 contexts 中按 URL 查找目标页面
            viewer_page = None
            for ctx in browser.contexts:
                for page in ctx.pages:
                    try:
                        if url_substr in page.url:
                            viewer_page = page
                            break
                    except Exception:
                        pass
                if viewer_page:
                    break

            if not viewer_page:
                return f"ERROR: page not accessible by url: {url_substr}"

            # 4. 执行 JS
            result = await viewer_page.evaluate(js_code)
            return str(result) if result is not None else "null"
        finally:
            # disconnect 不影响主引擎连接（connectOverCDP 的 close 仅断开此连接）
            try:
                await browser.close()
            except Exception:
                pass

    async def get_url(self) -> str:
        """返回当前页面 URL（Playwright 专属，非 EngineBase 契约），供 P2
        UrlJudger 使用。见 docs/plans/archive/02-dsl-actions-实施纪要.md。"""
        return self._page_or_raise().url

    _OVERLAY_VISIBLE_JS = (
        "(sel) => [...document.querySelectorAll(sel)]"
        ".some(e => e.offsetParent !== null)"
    )

    async def snapshot_overlay_selectors(
        self, candidates: list[str], ignore: set[str] = frozenset()
    ) -> set[str]:
        """返回当前可见覆盖层 selector 集合（click 前快照）。"""
        result: set[str] = set()
        for sel in candidates:
            if sel in ignore:
                continue
            try:
                visible = await self.evaluate(self._OVERLAY_VISIBLE_JS, sel)
            except Exception as exc:
                logger.debug("[overlay] snapshot 检测 '%s' 异常（忽略）: %s", sel, exc)
                continue
            if visible:
                result.add(sel)
        return result

    async def detect_new_overlay(
        self, candidates: list[str], before: set[str], ignore: set[str] = frozenset()
    ) -> Optional[str]:
        """click 后调用：找出新出现的可见覆盖层 selector（找不到返回 None）。

        若同时出现多个新覆盖层，按 candidates 声明顺序取第一个（确定性优先级，
        避免同一批次结果因 set 无序而不稳定）。
        """
        after = await self.snapshot_overlay_selectors(candidates, ignore)
        new_ones = after - before
        if not new_ones:
            return None
        for sel in candidates:
            if sel in new_ones:
                return sel
        return next(iter(new_ones))

    async def is_overlay_gone(self, selector: str) -> bool:
        """判断 active_container 对应覆盖层是否已消失。"""
        try:
            visible = await self.evaluate(self._OVERLAY_VISIBLE_JS, selector)
        except Exception as exc:
            logger.debug("[overlay] is_overlay_gone 检测 '%s' 异常（视为已消失）: %s", selector, exc)
            return True
        return not visible

    # ── DOM 摘要（L3.5 LLM_SELECTOR 使用） ───────────────────────

    async def get_accessibility_summary(self, max_nodes: int = 200) -> Optional[str]:
        """提取当前页面可见 DOM 摘要，供 L3.5 LLM_SELECTOR 推断 selector。

        返回 JSON 数组字符串，每项含 tag/cls/id/text/aria/role/placeholder 等字段。
        仅包含可见元素（offsetParent !== null），截断到 max_nodes 个节点。
        """
        js = """(maxNodes) => {
            var nodes = [];
            var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
            var count = 0;
            while (walker.nextNode() && count < maxNodes) {
                var el = walker.currentNode;
                if (el.offsetParent === null && el.tagName !== 'BODY') continue;
                var entry = {tag: el.tagName.toLowerCase()};
                if (el.className && typeof el.className === 'string' && el.className.length > 0)
                    entry.cls = el.className.substring(0, 80);
                if (el.id) entry.id = el.id;
                var text = (el.innerText || '').trim();
                if (text && text.length < 30 && el.children.length === 0) entry.text = text;
                var aria = el.getAttribute('aria-label');
                if (aria) entry.aria = aria;
                var role = el.getAttribute('role');
                if (role) entry.role = role;
                var ph = el.getAttribute('placeholder');
                if (ph) entry.placeholder = ph;
                var dataOnboarding = el.getAttribute('data-onboarding');
                if (dataOnboarding) entry.onboarding = dataOnboarding;
                nodes.push(entry);
                count++;
            }
            return JSON.stringify(nodes);
        }"""
        try:
            page = self._page or self._main_page
            if not page:
                return None
            result = await page.evaluate(js, max_nodes)
            return result if result else None
        except Exception:
            return None

    # ── 三级截图兜底 ────────────────────────────────────────────

    _SCREENSHOT_TIMEOUT = 25  # 总超时（秒）：L1(3s)+L2(8s)+L3(8s)+IO余量，
                               # 2026-08-20 由 50 收紧，见计划文档 §3.1
    _L3_TIMEOUT = 8  # L3 独立超时（秒），不再吃总超时剩余预算

    async def take_screenshot(self, path: str = "") -> Optional[bytes]:
        """三级截图兜底：L1 Playwright → L2 CDP → L3 PyAutoGUI。

        2026-08-21 诊断日志增强（见计划文档 20260821160000-playwright-
        screenshot-failure-diagnostic-logging.md）：三级全部失败时追加总
        耗时 + `_diagnose_screenshot_failure()` 快照，不改变返回值语义
        （仍返回 ``b""``），仅增强失败路径可观测性，便于事后排查"为什么
        三级截图会同时超时"。
        """
        started = time.monotonic()
        try:
            return await asyncio.wait_for(
                self._screenshot_impl(path),
                timeout=self._SCREENSHOT_TIMEOUT,
            )
        except (asyncio.TimeoutError, Exception) as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "[engine] 截图失败 (%s): %s elapsed_ms=%d",
                type(exc).__name__, exc, elapsed_ms,
            )
            logger.warning(
                "[engine] 截图失败诊断快照: %s", self._diagnose_screenshot_failure(),
            )
            return b""

    async def _screenshot_impl(self, path: str) -> bytes:
        # L1: Playwright API
        buf = await self._screenshot_l1(path)
        if buf is not None:
            return buf

        # L2: CDP Page.captureScreenshot（跳过 GPU 合成）
        buf = await self._screenshot_l2(path)
        if buf is not None:
            return buf

        # L3: PyAutoGUI 全屏兜底
        buf = await self._screenshot_l3(path)
        if buf is not None:
            return buf

        return b""

    async def _screenshot_l1(self, path: str) -> Optional[bytes]:
        opts: dict = {"timeout": 3000}
        if path:
            opts["path"] = path
        started = time.monotonic()
        try:
            return await self._page_or_raise().screenshot(**opts)
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "[engine] L1 截图失败(%s)，触发 L2: %s elapsed_ms=%d",
                type(exc).__name__, exc, elapsed_ms,
            )
            return None

    async def _screenshot_l2(self, path: str) -> Optional[bytes]:
        import base64
        cdp = None
        started = time.monotonic()
        try:
            cdp = await asyncio.wait_for(
                self._page_or_raise().context.new_cdp_session(self._page),
                timeout=5,
            )
            result = await asyncio.wait_for(
                cdp.send("Page.captureScreenshot", {
                    "format": "png",
                    "fromSurface": False,
                    "captureBeyondViewport": False,
                }),
                timeout=8,
            )
            buf = base64.b64decode(result["data"])
            if path:
                with open(path, "wb") as f:
                    f.write(buf)
            logger.info("[engine] L2 CDP 截图成功")
            return buf
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.warning(
                "[engine] L2 CDP 截图失败(%s)，触发 L3: %s elapsed_ms=%d",
                type(exc).__name__, exc, elapsed_ms,
            )
            return None
        finally:
            if cdp:
                try:
                    await cdp.detach()
                except Exception:
                    pass

    async def _screenshot_l3(self, path: str) -> Optional[bytes]:
        """L3 全屏截图兜底，按平台分发到可靠终止的实现。

        2026-08-20 修复（见计划文档 20260820150000-playwright-screenshot-l3-
        thread-leak-and-annotation-coords-fix.md）：原实现用
        ``loop.run_in_executor(None, pyautogui.screenshot)`` 把同步阻塞调用
        派发到 asyncio **进程级共享默认线程池**，外层 ``asyncio.wait_for()``
        超时后只是放弃等待该 Future，底层线程本身无法被强行终止——若
        ``pyautogui.screenshot()`` 反复卡死（如本次真机复现，L3 被外层总
        超时"腰斩"耗时近 40s），会在共享线程池里持续堆积无法回收的线程，
        拖慢进程内其他截图/其他引擎实例的 ``run_in_executor`` 调用。
        """
        if sys.platform == "darwin":
            return await self._screenshot_l3_macos(path)
        return await self._screenshot_l3_generic(path)

    async def _screenshot_l3_macos(self, path: str) -> Optional[bytes]:
        """macOS L3：直接用可 kill 的真实子进程调用 screencapture。

        pyautogui/pyscreeze 在 macOS 上的截图底层本就是
        ``subprocess.call(['screencapture', '-x', tmpFile])``（见
        ``pyscreeze._screenshot_osx``），只是被同步阻塞调用包住导致不可取消。
        改为 ``asyncio.create_subprocess_exec`` 自行起子进程，超时后
        ``proc.kill()`` 发 SIGKILL，一定能终止，不存在"线程无法取消"问题。
        """
        import uuid
        import tempfile

        target = path or os.path.join(
            tempfile.gettempdir(), f"_pw_l3_{uuid.uuid4().hex}.png",
        )
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "screencapture", "-x", target,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=self._L3_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                "[engine] L3 screencapture 超时(%ds)，强制 kill 子进程 pid=%s"
                "（若 kill 后进程仍残留，可用 `ps aux | grep screencapture` "
                "排查，见计划文档 20260821160000）",
                self._L3_TIMEOUT, getattr(proc, "pid", "?"),
            )
            if proc is not None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            return None
        except Exception as exc:
            logger.warning("[engine] L3 screencapture 异常: %s", exc)
            return None

        if not os.path.exists(target) or os.path.getsize(target) == 0:
            logger.warning("[engine] L3 screencapture 输出文件为空/不存在")
            return None
        try:
            with open(target, "rb") as f:
                buf = f.read()
        finally:
            if not path:
                try:
                    os.unlink(target)
                except OSError:
                    pass
        logger.info("[engine] L3 screencapture 全屏截图成功")
        return buf

    async def _screenshot_l3_generic(self, path: str) -> Optional[bytes]:
        """非 macOS（Windows 等）L3：pyautogui 全屏截图，隔离到私有执行域。

        Windows 上 pyautogui.screenshot() 走 PIL.ImageGrab.grab()（直接 GDI
        调用，非子进程），极少出现 macOS 那种"screencapture 子进程挂起"的
        情况，暂不需要换底层实现；仍改为投递到实例私有 ``BoundedExecutor``
        （2026-08-21 重构，复用 mac/win 引擎同款通用组件，见计划文档
        20260820160000）+ 独立超时，避免占用共享执行器（隔离故障，非根治
        不可取消——见计划文档风险评估）。
        """
        try:
            import io
            import pyautogui  # type: ignore
            img = await self._l3_guard.run(pyautogui.screenshot, timeout=self._L3_TIMEOUT)
            if img is None:
                # BoundedExecutor.run() 超时时返回 None（已在其内部记录
                # warning 日志，此处不重复告警）。
                return None
            buf_io = io.BytesIO()
            img.save(buf_io, format="PNG")
            buf = buf_io.getvalue()
            if path:
                img.save(path)
            logger.info("[engine] L3 PyAutoGUI 全屏截图成功")
            return buf
        except Exception as exc:
            logger.warning("[engine] L3 PyAutoGUI 截图失败: %s", exc)
            return None

    def _diagnose_screenshot_failure(self) -> str:
        """三级截图全部失败时采集的轻量诊断快照（2026-08-21 新增，见计划
        文档 20260821160000-playwright-screenshot-failure-diagnostic-
        logging.md）。

        同步方法，不引入额外异步等待/阻塞风险。每个字段独立
        try/except，单个字段采集异常仅记录为该字段的 ``xxx_error=...``，
        不影响其余字段采集，方法本身绝不上抛（诊断功能故障不能拖累主
        流程判定）。

        采集内容：
          - page_url / page_closed：当前页面是否已关闭、URL
          - browser_connected：CDP 连接是否仍存活
          - context_pages：当前 context 下的页面数量
          - frontend_errors_last10s / api_calls_last10s：失败前 10 秒内
            的前端 JS 异常数 / API 调用数（复用已有环形缓冲，辅助判断
            当时渲染进程是否在忙于处理大量请求/异常）
        """
        parts: list[str] = []

        try:
            page = self._page or self._main_page
            if page is None:
                parts.append("page=None")
            else:
                parts.append(f"page_url={getattr(page, 'url', '?')!r}")
                parts.append(f"page_closed={page.is_closed()}")
        except Exception as exc:
            parts.append(f"page_error={type(exc).__name__}:{exc}")

        try:
            if self._browser is None:
                parts.append("browser=None")
            else:
                parts.append(f"browser_connected={self._browser.is_connected()}")
        except Exception as exc:
            parts.append(f"browser_error={type(exc).__name__}:{exc}")

        try:
            if self._context is None:
                parts.append("context_pages=None")
            else:
                parts.append(f"context_pages={len(self._context.pages)}")
        except Exception as exc:
            parts.append(f"context_error={type(exc).__name__}:{exc}")

        now = time.time()
        try:
            recent_frontend_errors = [
                r for r in self._frontend_error_log
                if now - r.get("timestamp", now) <= 10
            ]
            parts.append(f"frontend_errors_last10s={len(recent_frontend_errors)}")
        except Exception as exc:
            parts.append(f"frontend_errors_error={type(exc).__name__}:{exc}")

        try:
            recent_api_calls = [
                r for r in self._all_responses_log
                if now - r.get("timestamp", now) <= 10
            ]
            parts.append(f"api_calls_last10s={len(recent_api_calls)}")
        except Exception as exc:
            parts.append(f"api_calls_error={type(exc).__name__}:{exc}")

        return " ".join(parts)

    # ── 内部工具 ────────────────────────────────────────────────

    async def _find_page(self) -> Page:
        """定位目标 page：按 target_html 匹配 或 取第一个非工具页。"""
        items = await self._collect_pages()
        if not items:
            for _ in range(15):
                await asyncio.sleep(2)
                items = await self._collect_pages()
                if items:
                    break
            else:
                raise RuntimeError("无可用页面（等待 30s 超时）")

        # 按 target_html 匹配
        if self._target_html:
            targets = (
                [self._target_html]
                if isinstance(self._target_html, str)
                else list(self._target_html)
            )
            for tgt in targets:
                hits = [i for i in items if tgt in i["url"]]
                if hits:
                    logger.info("[engine] 按 target_html=%s 命中: %s", tgt, hits[0]["url"][:60])
                    return hits[0]["page"]
            avail = ", ".join(i["url"][:40] for i in items)
            raise RuntimeError(f"找不到 target_html={targets}，可用: {avail}")

        # 默认取第一个
        logger.info("[engine] 默认 page: %s", items[0]["url"][:60])
        return items[0]["page"]

    async def _collect_pages(self) -> list[dict]:
        if not self._browser:
            return []
        items = []
        for ctx in self._browser.contexts:
            for pg in ctx.pages:
                if any(pg.url.startswith(s) for s in _SKIP_URLS):
                    continue
                try:
                    items.append({"page": pg, "url": pg.url})
                except Exception:
                    pass
        return items

    # ── 属性暴露 ────────────────────────────────────────────────

    @property
    def page(self) -> Optional[Page]:
        return self._page

    @property
    def browser(self) -> Optional[Browser]:
        return self._browser

    @property
    def user_data_dir(self) -> str:
        """Electron 应用的 user-data-dir 路径（供 LogCollector 使用）。

        默认路径按平台推导：
          - macOS: ~/Library/Application Support/<app_name>
          - Windows: %APPDATA%/<app_name>
          - Linux: ~/.config/<app_name>

        若 AppProcess 未来支持 --user-data-dir 参数，此处可扩展。
        """
        import sys
        app = self._app_name
        if sys.platform == "darwin":
            return os.path.expanduser(f"~/Library/Application Support/{app}")
        elif sys.platform == "win32":
            return os.path.join(os.environ.get("APPDATA", ""), app)
        else:
            return os.path.expanduser(f"~/.config/{app}")

    async def get_element_rect(self, element: ElementRef) -> Optional[tuple]:
        """返回元素边界框 (x, y, w, h)（供截图标注使用）。

        超时设为 2000ms（截图标注专用，不影响操作本身）：click/type 等操作执行完毕后
        元素可能已从 DOM 移除（如关闭下拉菜单），Playwright 默认的 30s 等待会造成
        明显的用例卡顿，短超时确保标注失败时快速降级，不阻塞后续步骤。
        """
        if not element or not element.raw:
            return None
        try:
            box = await element.raw.bounding_box(timeout=2000)
            if box:
                return (
                    int(box["x"]), int(box["y"]),
                    int(box["width"]), int(box["height"]),
                )
        except Exception as exc:
            logger.debug("[engine] get_element_rect 失败: %s", exc)
        return None

    async def __aenter__(self):
        await self.launch()
        return self

    async def __aexit__(self, *_):
        await self.close()
