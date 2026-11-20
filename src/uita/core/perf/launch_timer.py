"""冷启动分段计时：旧 PID 消失 → 新主进程出现 → CDP 可连 → 页面渲染完成。

为什么要独立计时（现状缺口）：
    ``AppProcess._wait_cdp()`` 的 ``elapsed`` 起点在轮询循环内部，**不含**
    ``_start()``（Popen/exec）之前的耗时，因此日志里的"等待 CDP"不是端到端冷启动。
    本模块把 ``prepare_engine_for_case()``（生命周期入口）整体包裹起来测墙钟，
    并在等待期间后台轮询四个里程碑，得到可解释的分段耗时。

``startup_ms`` 定义（2026-09-20 扩展，2026-10-12 收紧）：
    「旧进程消失（或 CDP 断开）→ 新进程**窗口出现且窗口左上角渲染出应用品牌**」。

    演进过程（两次口径调整，勿混淆）：
      * 2026-09-20：终点从「CDP 可连」延后到「CDP 可连 **且** ``document.readyState ===
        'complete'``」——原因是 CDP 可连只代表 Chromium 进程已起、调试协议已握手，
        不代表页面已渲染完成；
      * 2026-10-12：用户要求「启动耗时仅统计出现窗口需要的时间，窗口左上角渲染出
        『示例示例媒体应用』即可」——``readyState=complete`` 代表**整页资源加载完成**
        （含全部图片/异步分片），比"用户能看到窗口左上角的品牌标识"晚得多，口径偏大。
        故终点改为「主窗口出现 **且** 左上角品牌已渲染」。

    品牌判定是**应用知识**（选择器/文案/位置），故 core 只提供机制：调用方（app 侧
    ``apps/<app>/perf/startup_ready.py``）通过 ``ready_js`` 注入一段自包含 JS，
    返回值真值即"已就绪"（也接受 ``{"ready": bool, ...}`` 形态并原样落盘明细）。
    未注入 ``ready_js``（其它 app / 未提供该模块）时**逐字节退回** ``document.readyState
    === 'complete'`` 的 2026-09-20 口径；探针不可用（原生 Mac/Win UIA 引擎，或未注入
    ``engine``）时**立即回退**（``render_ready_ms == cdp_ready_ms``），对既有调用方零回归。

不侵入 lifecycle：本模块只接收一个「可等待对象工厂」，不 import 任何 lifecycle 内部实现，
探针（pid/CDP/渲染状态）通过构造参数注入，便于单测替换。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("uita")

__all__ = ["LaunchMetrics", "LaunchTimer", "AppProcessProbe"]


@dataclass
class LaunchMetrics:
    """一轮冷启动的分段耗时（单位 ms；未知记 ``None``）。"""

    launch_ms: float = 0.0
    startup_ms: Optional[float] = None
    old_pid: Optional[int] = None
    new_pid: Optional[int] = None
    old_gone_ms: Optional[float] = None
    new_appear_ms: Optional[float] = None
    cdp_ready_ms: Optional[float] = None
    #: 窗口左上角品牌渲染完成时刻（app 侧 ``ready_js`` 判定；未注入时退回
    #: ``document.readyState === 'complete'`` 的 2026-09-20 口径）。独立落盘便于事后诊断
    #: "卡在哪一段"。
    render_ready_ms: Optional[float] = None
    #: 本次用的是哪种就绪探针（``"custom"``=app 侧 ``ready_js`` / ``"readyState"``=通用兜底 /
    #: ``""``=探针不可用），随产物落盘以便"这次启动耗时是按什么口径量出来的"自证。
    render_probe: str = ""
    #: 品牌探针的明细（命中信号/元素矩形等，app 侧 JS 决定字段）；供报告与事后审计。
    render_detail: dict[str, Any] = field(default_factory=dict)
    timeout_stage: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AppProcessProbe:
    """默认探针：调用 ``AppProcess`` 的公开方法与静态方法（不改动该模块）。

    Args:
        app_process: ``PlaywrightEngine.app_process`` 返回的 ``AppProcess`` 实例；
            ``None`` 时所有探针返回未知值（仅能测 ``launch_ms``）。
        engine: 可选，供 :meth:`render_ready` 调用其 ``evaluate()`` 求值就绪 JS
            （鸭子类型，只用 ``getattr`` 探测，不 import 任何 ``engine/`` 模块，守 R1/R2）。
            ``None`` 或引擎无 ``evaluate`` 方法（如原生 Mac/Win UIA 引擎）时
            :meth:`render_ready` 恒返回 ``None``（不可测）。
        ready_js: 可选，**app 侧**注入的"启动就绪"自包含 JS（如
            ``apps/sample_media/perf/startup_ready.py::READY_JS``：判定窗口左上角品牌
            「示例示例媒体应用」是否已渲染）。空串时退回通用口径
            （``document.readyState === 'complete'``）——core 不含任何应用知识（守 R2）。
    """

    def __init__(self, app_process: Any = None, *, engine: Any = None, ready_js: str = "") -> None:
        self._ap = app_process
        self._engine = engine
        self._ready_js = (ready_js or "").strip()
        #: 末次就绪探针的明细（app 侧 JS 的返回 dict 原样保留，供产物自证用了哪个信号）
        self.last_render_detail: dict[str, Any] = {}

    @property
    def render_probe(self) -> str:
        """本次就绪探针的口径名（``custom``=app 侧 JS / ``readyState``=通用兜底）。"""
        return "custom" if self._ready_js else "readyState"

    @property
    def available(self) -> bool:
        return self._ap is not None

    def current_pid(self) -> Optional[int]:
        if self._ap is None:
            return None
        try:
            info = self._ap.find()
        except Exception as exc:  # noqa: BLE001 - 探针失败不应影响被测流程
            logger.debug("[perf] probe.current_pid 失败: %s", exc)
            return None
        if isinstance(info, dict):
            pid = info.get("pid")
            return int(pid) if pid else None
        return None

    def cdp_ready(self) -> bool:
        if self._ap is None:
            return False
        try:
            from uita.core.lifecycle.app_process import AppProcess

            port = int(getattr(self._ap, "cdp_port", 0) or 0)
            if port <= 0:
                return False
            return AppProcess.port_open(port) and AppProcess.cdp_ready(port)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[perf] probe.cdp_ready 失败: %s", exc)
            return False

    async def render_ready(self) -> Optional[bool]:
        """窗口是否已出现且左上角品牌已渲染出来（就绪 JS 判定）。

        注入 ``ready_js`` 时求值该 JS（app 侧知识）；未注入时用通用口径
        ``document.readyState === 'complete'``（2026-09-20 的旧口径，零回归）。

        Returns:
            ``True``  — 已就绪（窗口出现且左上角品牌已渲染出来）；
            ``False`` — 未就绪（求值成功但条件不满足，或求值异常/页面尚未挂载）；
            ``None``  — 不可测（``engine`` 未注入，或引擎无 JS 求值能力，如原生
                        Mac/Win UIA 引擎）——调用方应回退为更早的口径。
        """
        evaluate_fn = getattr(self._engine, "evaluate", None)
        if not callable(evaluate_fn):
            return None
        try:
            result = await evaluate_fn(self._ready_js or "document.readyState")
        except Exception as exc:  # noqa: BLE001 - 求值失败视为未就绪，继续轮询
            logger.debug("[perf] probe.render_ready 求值失败: %s", exc)
            return False
        return self._interpret_render(result)

    def _interpret_render(self, result: Any) -> bool:
        """把就绪 JS 的返回值归一化为布尔（``dict`` 形态同时留存明细）。"""
        if isinstance(result, dict):
            self.last_render_detail = dict(result)
            return bool(result.get("ready", False))
        if self._ready_js:
            return bool(result)
        return result == "complete"


class LaunchTimer:
    """包裹生命周期入口调用，测「墙钟 + 四段里程碑」。

    Args:
        probe:                  pid/CDP/渲染探针（默认 ``None`` = 无探针，只测 launch_ms）
        poll_interval:          里程碑轮询间隔（秒）
        old_pid_timeout:        等待旧 PID 消失的上限（秒）
        cdp_timeout:            等待「CDP 就绪 + 渲染完成」的上限（秒）；``None`` 表示沿用
                                ``prepare()`` 返回后的兜底等待（默认 60s）
        clock:                  单调时钟注入点（单测可替换）
    """

    def __init__(
        self,
        probe: Any = None,
        *,
        poll_interval: float = 0.1,
        old_pid_timeout: float = 20.0,
        cdp_timeout: Optional[float] = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = probe
        self._poll_interval = poll_interval
        self._old_pid_timeout = old_pid_timeout
        self._cdp_timeout = cdp_timeout
        self._clock = clock

    async def measure(self, prepare: Callable[[], Awaitable[Any]]) -> LaunchMetrics:
        """执行 ``prepare()`` 并返回分段耗时。

        Args:
            prepare: 无参可等待工厂（如 ``lambda: prepare_engine_for_case(...)``）。
                     必须是**工厂**而非已创建的协程，以便计时从调用前开始。
        """
        t0 = self._clock()
        metrics = LaunchMetrics()
        probe = self._probe
        # 探针是否可用：probe=None 或 probe.available=False（如引擎未配置 app_path、
        # 非 Electron 引擎）时**完全跳过** pid/CDP/渲染轮询与兜底等待，否则会在
        # cdp_timeout（默认 60s/轮）内空转，把每轮耗时凭空拉长一分钟。
        probe_usable = probe is not None and bool(getattr(probe, "available", True))
        old_pid = probe.current_pid() if probe_usable else None
        metrics.old_pid = old_pid
        # 渲染探测是否可用：探针无 render_ready 方法（如自定义/测试用简化探针）时
        # 视为不支持，立即回退旧口径（render_ready_ms == cdp_ready_ms）。
        has_render_probe = probe_usable and callable(getattr(probe, "render_ready", None))
        # 就绪探针口径落盘（"这次启动耗时是按什么判定的"）：探针自带 render_probe 属性
        # （AppProcessProbe：custom=app 侧 ready_js / readyState=通用兜底）；测试用的
        # 简化探针没有该属性 → 记空串（不猜）。
        if probe_usable:
            # 口径名只要探针声明了就落盘（app 侧原生探针不实现 render_ready 也要能自证）
            metrics.render_probe = str(getattr(probe, "render_probe", "") or "")

        # released：「本轮重启已被观测到」，是判断 CDP 就绪属于**新实例**的前提。
        # 必需的原因（真机实测缺陷）：硬重启前旧实例仍在运行，其 CDP 天然 ready，
        # 若不加区分就会把旧实例的就绪时刻当成本轮启动完成 → startup_ms 变成负数
        # （实测 -277ms）。故要求「先看到旧实例消失，或先看到 CDP 掉线」。
        state = {"old_gone": None, "new_appear": None, "cdp_ready": None, "render_ready": None,
                 "new_pid": None, "released": False, "cdp_down_seen": False}
        stop_flag = asyncio.Event()

        async def _check_render(now: float) -> None:
            """cdp_ready 已确定后检查渲染是否完成；探针不支持则立即回退旧口径。"""
            if state["cdp_ready"] is None or state["render_ready"] is not None:
                return
            if not has_render_probe:
                state["render_ready"] = state["cdp_ready"]
                return
            try:
                result = await probe.render_ready()
            except Exception as exc:  # noqa: BLE001 - 渲染探测异常不应中断计时
                logger.debug("[perf] probe.render_ready 调用异常: %s", exc)
                result = False
            if result is None:
                state["render_ready"] = state["cdp_ready"]
            elif result:
                state["render_ready"] = now

        async def _observe(now: float) -> None:
            """采一次四态状态机（pid 消失 / 新 pid 出现 / CDP 就绪 / 渲染完成）。"""
            cur = None
            if state["old_gone"] is None and old_pid is not None:
                cur = probe.current_pid()
                if cur is None or cur != old_pid:
                    state["old_gone"] = now
                    state["released"] = True
            if state["new_appear"] is None:
                if cur is None:
                    cur = probe.current_pid()
                if cur is not None and cur != old_pid:
                    state["new_appear"] = now
                    state["new_pid"] = cur
            if state["cdp_ready"] is None:
                if _probe_ready(probe):
                    # ``new_appear`` 也算"本轮重启已被观测到"：启动前应用**本来未运行**
                    # （old_pid 为 None，如每轮前置准备已把它杀掉）时永远看不到"旧实例消失"，
                    # 若只认 ``released``/``cdp_down_seen``，就会一直等到 cdp 超时
                    # （实测：进程/窗口起得比首个 poll 还快 → 明明就绪却判未就绪）。
                    if state["released"] or state["cdp_down_seen"] or state["new_appear"] is not None:
                        state["cdp_ready"] = now
                        if state["new_pid"] is None:
                            state["new_pid"] = probe.current_pid()
                else:
                    state["cdp_down_seen"] = True
                    if old_pid is None:
                        state["released"] = True  # 应用本来未运行 → 本轮启动已开始
            if state["cdp_ready"] is not None and state["render_ready"] is None:
                await _check_render(now)

        async def _poll() -> None:
            while not stop_flag.is_set():
                await _observe(self._clock() - t0)
                await asyncio.sleep(self._poll_interval)

        poll_task: Optional[asyncio.Task] = None
        if probe_usable:
            poll_task = asyncio.create_task(_poll())

        try:
            await prepare()
        finally:
            metrics.launch_ms = round((self._clock() - t0) * 1000, 2)
            # 兜底等待：prepare() 内部通常已等到 CDP 就绪/渲染完成；此处覆盖"提前返回"的
            # 实现差异（如 prepare() 最后一条语句设置探针状态后不再 yield，后台 poll_task
            # 来不及观测到）。等待目标从"仅 CDP 就绪"扩展为"CDP 就绪 且 渲染完成"。
            if probe_usable and self._cdp_timeout is not None:
                deadline = self._clock() + self._cdp_timeout
                while state["render_ready"] is None and self._clock() < deadline:
                    now = self._clock() - t0
                    if state["cdp_ready"] is None:
                        ready = _probe_ready(probe)
                        if ready and (
                            state["released"] or state["cdp_down_seen"]
                            or state["new_appear"] is not None
                        ):
                            state["cdp_ready"] = now
                            if state["new_pid"] is None:
                                state["new_pid"] = probe.current_pid()
                        elif not ready:
                            state["cdp_down_seen"] = True
                            if old_pid is None:
                                state["released"] = True
                    if state["cdp_ready"] is not None:
                        await _check_render(now)
                        if state["render_ready"] is not None:
                            break
                    await asyncio.sleep(self._poll_interval)
            stop_flag.set()
            if poll_task is not None:
                poll_task.cancel()
                try:
                    await poll_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

        metrics.old_gone_ms = _ms(state["old_gone"])
        metrics.new_appear_ms = _ms(state["new_appear"])
        metrics.cdp_ready_ms = _ms(state["cdp_ready"])
        metrics.render_ready_ms = _ms(state["render_ready"])
        metrics.new_pid = state["new_pid"]
        # 就绪探针明细（品牌探针的命中信号/元素矩形等）——只在真的观测到就绪时才有意义，
        # 故与 render_ready_ms 同条件落盘，避免"本轮未就绪却带着上一轮的明细"。
        if metrics.render_ready_ms is not None:
            detail = getattr(probe, "last_render_detail", None)
            if isinstance(detail, dict) and detail:
                metrics.render_detail = dict(detail)

        if metrics.cdp_ready_ms is not None:
            # 优先用渲染完成时刻（= 窗口左上角品牌就绪 / 旧口径的 readyState complete）；
            # 超时未确认时兜底用 CDP 就绪时刻（CDP 确实已就绪，只是就绪确认超时，
            # 不应让 startup_ms 整体记 None）。
            end_ms = metrics.render_ready_ms if metrics.render_ready_ms is not None else metrics.cdp_ready_ms
            base = metrics.old_gone_ms if metrics.old_gone_ms is not None else 0.0
            # 夹紧到非负：状态机已避免误用旧实例就绪，此处兜底防止时钟/竞态产生负值
            metrics.startup_ms = max(0.0, round(end_ms - base, 2))
        if probe_usable:
            if metrics.cdp_ready_ms is None:
                metrics.timeout_stage = "cdp"
            elif old_pid is not None and metrics.old_gone_ms is None:
                metrics.timeout_stage = "old_pid"
            elif metrics.new_appear_ms is None:
                metrics.timeout_stage = "new_pid"
            elif metrics.render_ready_ms is None:
                metrics.timeout_stage = "render"
        return metrics


def _ms(seconds: Optional[float]) -> Optional[float]:
    return None if seconds is None else round(seconds * 1000, 2)


def _probe_ready(probe: Any) -> bool:
    """应用是否已"可交互"——优先 ``probe.ready()``（app 侧原生探针），否则 ``cdp_ready()``。

    为什么加这一层：默认探针（``AppProcessProbe``）按「CDP 端口可连 + JS 求值」判定就绪，
    只对 Electron 类应用成立；原生引擎（WinUIA / MacAppKit）没有 CDP，app 侧探针改用
    「主进程 + 窗口类名」这类应用知识判定。两者都提供时 ``ready`` 优先——它才是"这个
    应用算启动完成"的权威定义。异常一律按"未就绪"处理（继续轮询，不中断计时）。
    """
    fn = getattr(probe, "ready", None)
    if callable(fn):
        try:
            return bool(fn())
        except Exception as exc:  # noqa: BLE001 - 探针异常不该中断计时
            logger.debug("[perf] probe.ready 失败: %s", exc)
            return False
    fn = getattr(probe, "cdp_ready", None)
    if not callable(fn):
        return False
    try:
        return bool(fn())
    except Exception as exc:  # noqa: BLE001
        logger.debug("[perf] probe.cdp_ready 失败: %s", exc)
        return False
