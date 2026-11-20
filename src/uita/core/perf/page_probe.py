"""core/perf/page_probe.py — 单页「点击 → 等指纹 → 判加载完成」的测量编排。

一次 :func:`probe_page` 产出：

- ``render_ms``：**点击完成 → 指纹首命中**（主口径）。指纹轮询在点击动作开始执行时
  即并行启动（定频 ``fp_poll_ms``，默认 150ms），命中即刻记时，不等点击返回；
  若命中早于点击返回则 ``render_ms`` 钳为 0；
- ``total_ms`` / ``click.start_ms`` / ``click.resolve_ms`` / ``click.click_ms``：拆解点击本身的开销
  （``click.start_ms`` 是点击动作开始的单调时钟毫秒，「启动 → 点击」资源窗口的终点）；
- ``fingerprint``：命中的模板名 / 置信度 / 算法 / 尝试次数；
- ``l3``：视觉 LLM 的三态判定（pass/fail/unknown/skipped）+ provider + 耗时 + 送审截图；
- ``resources``：三个窗口的 CPU/RAM 统计（``sampler.summary_between`` 直接给峰值/均值）。

纪律：

- 页面声明了 ``wait_elements``（rosetta_map 的 UIA/DOM 元素名列表，任一出现即命中）时，
  等待改走控件树直查（:mod:`element_wait`，**每轮不截图、不做图像匹配**）；未声明才退回视觉指纹；
- 缺指纹（``no_fingerprint``）→ ``ok=False, reason="no_fingerprint"``，**立刻返回**不空等；
- 页面加载完成既看指纹也看 L3：默认以指纹首命中作为完成时刻（``require_l3_pass=False``），
  严格模式（``require_l3_pass=True``）才要求 L3 首次 pass；
- ``judge_enabled=False``：指纹首命中即判定加载完成，**不截图、不调 LLM、不等 ``settle_ms``**
  （``l3.verdict="skipped"``，``require_l3_pass`` 随之失效）——用于"指纹本身已能证明页面
  展示正确"的页面，避免为确认性判定多付一次 VLM 往返；
- ``unknown``（VLM 不可用/超时）**不算通过**，但也不因此判失败——``valid`` 单独表达
  "这次测量是否可用于统计"；
- core/perf 不 import ``core/runner``/``engine``（R1）：resolver / vision client / sampler
  全部由调用方注入，本模块只按鸭子类型使用。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from uita.core.perf.element_wait import wait_elements
from uita.core.perf.fingerprint_wait import wait_fingerprint
from uita.core.perf.page_judge import PageVerdict, judge_page_loaded

logger = logging.getLogger("uita")

__all__ = ["ProbeOptions", "probe_page"]


def _monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


@dataclass
class ProbeOptions:
    """单页测量参数（全部可在 app 预设里给默认值，命令行经 ``--perf-scenario-opt`` 覆盖）。"""

    fp_timeout_ms: int = 20000
    fp_poll_ms: int = 150
    fp_threshold: Optional[float] = None
    settle_ms: int = 300
    click_timeout_ms: int = 15000
    #: False = 指纹首命中即视为加载完成（不截图、不调 LLM、不等 settle_ms）
    judge_enabled: bool = True
    judge_provider: str = ""
    judge_retries: int = 1
    judge_timeout_s: int = 30
    judge_max_side: int = 1280
    judge_jpeg_quality: int = 80
    require_l3_pass: bool = False
    #: 控制台行前缀标签（如 ``[Both-1]`` = 预加载范围-轮次）：由调用方（场景）按批次上下文
    #: 生成，core 不解析其语义；空 = 不加前缀。**不是** ``--perf-scenario-opt`` 可覆盖的键。
    round_tag: str = ""

    @classmethod
    def from_dict(cls, data: Optional[dict[str, Any]]) -> "ProbeOptions":
        """从场景选项 dict 构造（未知键忽略；值按目标类型强制转换）。"""
        if not data:
            return cls()
        kwargs: dict[str, Any] = {}
        for field_name, default in _FIELDS.items():
            if field_name not in data or data[field_name] is None:
                continue
            raw = data[field_name]
            try:
                if isinstance(default, bool):
                    if isinstance(raw, str):
                        kwargs[field_name] = raw.strip().lower() in ("1", "true", "yes", "on")
                    else:
                        kwargs[field_name] = bool(raw)
                elif isinstance(default, int):
                    kwargs[field_name] = int(raw)
                elif isinstance(default, float):
                    kwargs[field_name] = float(raw)
                else:
                    kwargs[field_name] = raw
            except (TypeError, ValueError):
                logger.warning("[perf.probe] 选项 %s=%r 无法解析，使用默认值", field_name, raw)
        return cls(**kwargs)


_FIELDS: dict[str, Any] = {
    "fp_timeout_ms": 20000,
    "fp_poll_ms": 150,
    "fp_threshold": 0.7,
    "settle_ms": 300,
    "click_timeout_ms": 15000,
    "judge_enabled": True,
    "judge_provider": "",
    "judge_retries": 1,
    "judge_timeout_s": 30,
    "judge_max_side": 1280,
    "judge_jpeg_quality": 80,
    "require_l3_pass": False,
}


def _window(sampler: Any, t0: int, t1: int) -> dict[str, Any]:
    """取 ``[t0, t1)`` 的资源统计；采样器不可用/窗口无效时返回 ``{}``（不伪造 0）。"""
    if sampler is None or t1 <= t0:
        return {}
    try:
        summary = sampler.summary_between(int(t0), int(t1))
    except Exception as exc:  # noqa: BLE001 - 统计失败不该中断测量
        logger.debug("[perf.probe] summary_between(%s,%s) 失败: %s", t0, t1, exc)
        return {}
    if not isinstance(summary, dict):
        return {}
    return {
        "window_ms": [int(t0), int(t1)],
        "cpu_peak": summary.get("cpu_percent_peak"),
        "cpu_avg": summary.get("cpu_percent_avg"),
        "rss_peak_mb": summary.get("rss_mb_peak"),
        "rss_avg_mb": summary.get("rss_mb_avg"),
        "proc_count_peak": summary.get("proc_count_peak"),
        "thread_count_peak": summary.get("thread_count_peak"),
        "samples": summary.get("sample_count"),
    }


def _delta(loading: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """加载窗口相对基线的抬升量（缺失侧按 None 处理，不做 0 假设）。"""

    def _sub(a: Any, b: Any) -> Optional[float]:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            return round(float(a) - float(b), 2)
        return None

    return {
        "cpu_avg": _sub(loading.get("cpu_avg"), baseline.get("cpu_avg")),
        "rss_mb": _sub(loading.get("rss_avg_mb"), baseline.get("rss_avg_mb")),
        "rss_peak_mb": _sub(loading.get("rss_peak_mb"), baseline.get("rss_peak_mb")),
    }


def _emit(event_sink: Optional[Callable[..., Any]], t_ms: int, kind: str, **payload: Any) -> None:
    if event_sink is None:
        return
    try:
        event_sink(t_ms, kind, payload)
    except Exception as exc:  # noqa: BLE001 - 事件只服务曲线标注
        logger.debug("[perf.probe] event_sink(%s) 异常: %s", kind, exc)


async def probe_page(
    engine: Any,
    resolver: Any,
    *,
    page: dict[str, Any],
    click_element: str,
    opts: ProbeOptions,
    sampler: Any = None,
    vision_client: Any = None,
    event_sink: Optional[Callable[..., Any]] = None,
    shots_dir: str = "",
    page_key: str = "",
    knee_ms: int = 0,
    round_index: int = 0,
) -> dict[str, Any]:
    """测量一个页面的渲染完成耗时与加载期资源。

    Args:
        engine: 引擎（提供 ``click`` / ``take_screenshot``）。
        resolver: Resolver（提供 ``resolve`` 与 ``resolve_visual``）。
        page: 页面声明（``label`` / ``fingerprint_element`` / ``l3_hint`` / ``extra_rules``）。
        click_element: 入口元素名（rosetta_map 元素，不用坐标，守 R11）。
        opts: :class:`ProbeOptions`。
        sampler: 正在采样的 ``ResourceSampler``（可选；给了才产出资源窗口）。
        vision_client: 直接注入的视觉客户端（给了就不再按 provider 构造）。
        event_sink: ``(t_ms, kind, payload)`` 回调，用于曲线事件标注。
        shots_dir: 命中截图落盘目录（空则不落盘）。
        page_key: 页面键（进产物，如 ``software_mgr``）。
        knee_ms: CPU 拐点命中时刻（用于 ``pre_click`` 基线窗口；0 = 未知，不统计基线）。
        round_index: 本轮轮次序号（0 起）。命中截图按轮分目录文件命名——同名文件会被
            下一轮覆盖，导致跨轮复核时只剩最后一轮的截图。

    Returns:
        dict（可直接落 ``perf_summary.json``）。
    """
    label = str(page.get("label") or page_key or click_element)
    # 控制台行统一前缀：`[perf.probe] [Both-1] <元素名>：…`——同一批跑多档/多轮时，
    # 光看行内容分不清这一行属于哪一轮哪一档。
    _tag = str(getattr(opts, "round_tag", "") or "").strip()
    head = f"[perf.probe] {_tag}" if _tag else "[perf.probe]"
    result: dict[str, Any] = {
        "page": page_key or label,
        "label": label,
        "ok": False,
        "valid": False,
        "degraded": False,
        "degrade_reason": "",
        "reason": "",
        "error": None,
        "click": {},
        "render_ms": None,
        "total_ms": None,
        "fingerprint": {},
        "l3": {},
        "resources": {},
        "screenshot": "",
    }

    # ⓪ 置前被测窗口（引擎可选钩子，未实现则跳过）：引擎截图走"窗口矩形的
    # 屏幕区域抓取"（ImageGrab.grab），**窗口被其它窗口遮挡时抓到的是遮挡窗口
    # 的内容**——真机踩到：IDE 开着 perf_report.html 盖在应用上面，指纹轮询与
    # L3 判定截图全是 IDE，L3 给出"截图显示的是性能测试报告"的误判。测量全程
    # 本来就要点按钮（需要前台），这里显式置前是同一代价的必要动作。
    bring_to_front = getattr(engine, "bring_to_foreground", None)
    if callable(bring_to_front):
        try:
            await bring_to_front()
        except Exception as exc:  # noqa: BLE001 - 置前失败不应中断测量
            logger.debug("[perf.probe] bring_to_foreground 失败（忽略）: %s", exc)

    # ① 点击入口（元素识别，非坐标）。指纹轮询与点击动作**同时开始**：点击一开始就
    # 起轮询任务，页面一出现立刻记命中时刻（不再等点击返回后才开始看）。
    fingerprint_element = str(page.get("fingerprint_element") or "")
    wait_names = [
        str(n).strip() for n in (page.get("wait_elements") or []) if str(n or "").strip()
    ]
    deadline = _monotonic_ms() + max(1, int(opts.fp_timeout_ms))

    def _wait_coro(timeout_ms: int) -> Any:
        """配了 ``wait_elements`` → 控件树直查（不截图）；否则退回视觉指纹等待。"""
        if wait_names:
            return wait_elements(
                resolver, wait_names, timeout_ms=timeout_ms, poll_ms=opts.fp_poll_ms,
            )
        return wait_fingerprint(
            resolver,
            fingerprint_element,
            timeout_ms=timeout_ms,
            poll_ms=opts.fp_poll_ms,
            match_threshold=opts.fp_threshold,
            with_detail=True,
        )

    def _start_wait(timeout_ms: int) -> "asyncio.Future[Any]":
        return asyncio.ensure_future(_wait_coro(timeout_ms))

    pending_wait: Optional["asyncio.Future[Any]"] = None
    try:
        t_click_start = _monotonic_ms()
        _emit(event_sink, t_click_start, "click_start", page=page_key)
        try:
            resolve_result = await resolver.resolve(click_element)
        except Exception as exc:  # noqa: BLE001
            result.update(ok=False, reason="click_resolve_error", error=f"{type(exc).__name__}: {exc}")
            return result
        element = getattr(resolve_result, "element", None)
        t_resolved = _monotonic_ms()
        if not getattr(resolve_result, "found", False) or element is None:
            result.update(
                ok=False,
                reason="click_resolve_failed",
                click={"element": click_element, "resolve_ms": t_resolved - t_click_start},
            )
            logger.warning("%s %s：入口元素 %r 未解析到，跳过该页", head, label, click_element)
            return result

        deadline = _monotonic_ms() + max(1, int(opts.fp_timeout_ms))
        pending_wait = _start_wait(max(1, int(opts.fp_timeout_ms)))
        try:
            await engine.click(element)
        except Exception as exc:  # noqa: BLE001
            result.update(
                ok=False,
                reason="click_error",
                error=f"{type(exc).__name__}: {exc}",
                click={"element": click_element, "resolve_ms": t_resolved - t_click_start},
            )
            return result
        t_click_done = _monotonic_ms()
        _emit(event_sink, t_click_done, "click_done", page=page_key)
        result["click"] = {
            "element": click_element,
            # 点击动作开始的单调时钟毫秒（与 ``ResourceSample.t_ms`` 同源）：「启动 → 点击」
            # 资源窗口的终点，报告据此从原始采样点里切片。
            "start_ms": int(t_click_start),
            "resolve_ms": max(0, t_resolved - t_click_start),
            "click_ms": max(0, t_click_done - t_resolved),
        }

        # ② 等指纹（+ 可选严格模式下的 L3 复核）
        hit_at = 0
        hit_payload: dict[str, Any] = {}
        l3_payload: dict[str, Any] = {}
        verdict_image = b""
        shot_path = ""

        while True:
            if pending_wait is not None:
                hit = await pending_wait
                pending_wait = None
            else:
                hit = await _wait_coro(max(1, deadline - _monotonic_ms()))
            hit_payload = hit.to_dict()
            if not hit.hit:
                result.update(
                    ok=False,
                    reason=hit.reason,
                    fingerprint=hit_payload,
                    total_ms=max(0, _monotonic_ms() - t_click_start),
                )
                # 严格模式下可能已经判过若干次 L3：结论必须带回去，否则"为什么没通过"无从复核
                if l3_payload:
                    result["l3"] = l3_payload
                if hit.reason in ("no_fingerprint", "no_element"):
                    _emit(event_sink, _monotonic_ms(), "fp_no_fingerprint", page=page_key)
                # 没拿到渲染时间也要在控制台留一行（否则该页在精简输出里"凭空消失"）
                logger.info(
                    "%s %s：渲染完成时间 未取得（%s）",
                    head, fingerprint_element or label, hit.reason or "unknown",
                )
                return result

            hit_at = hit.hit_at_ms or _monotonic_ms()
            _emit(event_sink, hit_at, "fp_hit", page=page_key, template=hit_payload.get("template"))

            # ②b 关闭 LLM 判定：指纹首命中即加载完成，直接进后续步骤（不截图/不等待/不调 LLM）
            if not opts.judge_enabled:
                l3_payload = PageVerdict(verdict="skipped", reason="judge_disabled").to_dict()
                _emit(event_sink, _monotonic_ms(), "l3_skipped", page=page_key, verdict="skipped")
                break

            # ③ 命中即截图 + 送 L3 判定
            if opts.settle_ms > 0:
                await asyncio.sleep(opts.settle_ms / 1000.0)
            try:
                verdict_image = await engine.take_screenshot() or b""
            except Exception as exc:  # noqa: BLE001
                logger.debug("[perf.probe] 截图失败: %s", exc)
                verdict_image = b""

            if verdict_image and shots_dir:
                # round_index 由框架注入**已是 1-based**（runner 的 PerfRound.round_index），
                # 与 samples/rNN_*.jsonl 命名口径一致，不要再 +1。
                shot_path = "".join(
                    [
                        shots_dir.rstrip("/\\"), "/",
                        f"r{int(round_index):02d}_{page_key or 'page'}_fp.png",
                    ],
                )

            verdict = await judge_page_loaded(
                verdict_image,
                page_label=label,
                elements_hint=str(page.get("l3_hint") or ""),
                extra_rules=str(page.get("extra_rules") or ""),
                provider=opts.judge_provider,
                timeout_s=opts.judge_timeout_s,
                retries=opts.judge_retries,
                max_side=opts.judge_max_side,
                jpeg_quality=opts.judge_jpeg_quality,
                client=vision_client,
                shot_path=shot_path,
            )
            l3_payload = verdict.to_dict()
            _emit(event_sink, _monotonic_ms(), "l3_done", page=page_key, verdict=verdict.verdict)

            if not opts.require_l3_pass or verdict.passed:
                break
            if _monotonic_ms() >= deadline:
                break
            logger.info(
                "%s %s：L3 判定 %s（require_l3_pass=1），继续等待命中",
                head, label, verdict.verdict,
            )
    finally:
        # 点击失败 / 异常退出时不留悬挂的轮询任务
        if pending_wait is not None and not pending_wait.done():
            pending_wait.cancel()
            try:
                await pending_wait
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    if hit_at <= 0:
        result.update(ok=False, reason="no_hit_time")
        return result

    render_ms = max(0, hit_at - t_click_done)
    total_ms = max(0, hit_at - t_click_start)
    pre_click = _window(sampler, knee_ms, t_click_start) if knee_ms > 0 else {}
    loading = _window(sampler, t_click_start, hit_at)

    result.update(
        ok=True,
        valid=True,
        render_ms=render_ms,
        total_ms=total_ms,
        fingerprint=hit_payload,
        l3=l3_payload,
        screenshot=shot_path if verdict_image else "",
        resources={"pre_click": pre_click, "loading": loading, "delta": _delta(loading, pre_click)},
    )
    if not loading:
        result["degraded"] = True
        result["degrade_reason"] = "no_resource_window"
    # 控制台核心行：用**指纹元素名**（如"列表页_渲染完成"）而不是页面中文名——
    # 这一行报的是"该指纹首次命中"的耗时，元素名才能对上 rosetta_map 里被等的那个判定点。
    logger.info(
        "%s %s：渲染完成时间 %dms（点击 %.0fms + 解析 %.0fms），L3=%s",
        head, fingerprint_element or label, render_ms, result["click"].get("click_ms") or 0,
        result["click"].get("resolve_ms") or 0, l3_payload.get("verdict") or "-",
    )
    return result
