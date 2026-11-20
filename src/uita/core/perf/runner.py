"""``PerfRunner``：perf 模式多轮编排（生命周期 + 计时 + 采样 + 逐轮 TCS 执行）。

单轮流程（与计划文档 §核心逻辑 一致）::

    result_dir = make_run_result_dir(case_file, base_dir=perf_run_dir, run_ts=f"{ts}-r{n}")
    sampler.start()
    launch     = await self._launch_with_retries(...)   # 见下：最多 PERF_LAUNCH_ATTEMPTS 次
    collector  = await TCSInterpreter(case, result_dir=..., hook_names=[], engine=engine).run()
    sampler.stop()
    rounds.append(PerfRound(...))
    # 读回 result.json → 注入 "perf" 段 → 重写（只增不改，既有顶层键零变化）

启动失败处理（用户口径 2026-10-15，两条一起看）：**每次失败先取证（当时系统内存快照，口径与
stress 共用唯一实现）→ 杀进程 → 重试**，最多 3 次；**重试用尽**才中止本轮（跳过用例/静息/场景，
直接进下一轮），"过程中所有失败"都留在 ``PerfRound.launch_attempts`` 并完整展示在报告里。

四个关键设计决策（依据见计划文档）：
  1. 冷启动必须显式 ``force_hard_restart=True``：sample_media ``restart_between_cases=False``，
     ``is_first=False`` 时默认走软复位，测不到真实冷启动。
  2. 用 ``TCSInterpreter`` 而非 ``run_case_streaming``：注入外部 engine 时不冷启动、
     结束时**不 close**，生命周期正好由本 Runner 托管。
  3. perf 模式固定 ``hook_names=[]``（关截图降噪）；``--perf-keep-screenshot`` 时改用
     ``["screenshot"]`` 供排查。
  4. 多轮重复在 Runner 层实现（不改 DSL / 不加 TCS 语法）。

依赖方向（R1）：本模块位于 ``core/`` 内，只依赖 ``core/dsl``、``core/result``、
``core/lifecycle``、``core/interpreter``；不 import ``engine/``、``apps/``、``cli/``、``tools/``。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

from uita.core.perf.baseline import PerfBaseline, load_baseline
from uita.core.perf.console import human_ms, round_tag
from uita.core.perf.launch_timer import AppProcessProbe, LaunchTimer
from uita.core.perf.metrics import summarize
from uita.core.perf.sampler import platform_capabilities
from uita.core.perf.sampler import ResourceSampler, system_memory_snapshot

logger = logging.getLogger("uita")

PERF_SCHEMA_VERSION = "1.0"

#: 单轮启动的**总尝试次数**：1 次首发 + 2 次重试（用户口径 2026-10-15："任何场景启动失败增加
#: 两次重试，启动失败后杀掉进程再次尝试，如果再次失败就尝试第三次，如果还失败就结束直接开始
#: 下一场景"）。每次失败都取证；全部失败才中止本轮。改这个数=改报告里"最多 N 次"的口径。
PERF_LAUNCH_ATTEMPTS = 3

__all__ = ["PerfRound", "PerfCaseReport", "PerfRunResult", "PerfRunner"]


@dataclass
class PerfRound:
    """单轮结果（含 TCS 结果与资源指标）。"""

    round_index: int
    result_dir: str = ""
    result_json: str = ""
    launch_ms: float = 0.0
    startup_ms: Optional[float] = None
    launch_detail: dict[str, Any] = field(default_factory=dict)
    #: **启动失败现场取证**（用户口径 2026-10-15）：本轮启动失败（抛异常 / 就绪探针
    #: 超时未就绪）时，在**那一瞬间**抓的系统内存快照（口径与压测 ``stress`` 完全一致，
    #: 见 ``sampler.system_memory_snapshot``）+ 失败尝试期间的进程树峰值。
    #: 空 dict = 本轮启动没失败（不是"没测"，失败一定有值；读数不可用时各值为 ``None``）。
    launch_failure: dict[str, Any] = field(default_factory=dict)
    #: **本轮的每一次启动失败尝试**（含重试过程中失败、但后面重试成功的那些）：
    #: ``{"attempt": n, "attempts_total": N, "stage", "reason", "elapsed_ms", <系统内存快照>, ...}``。
    #: 与 ``launch_failure`` 的区别：``launch_failure`` 只在**全部尝试失败**（本轮中止）时非空，
    #: 本字段是"过程中所有失败"的完整流水（用户口径：过程中所有失败都要记录并完整展示在报告里）。
    launch_attempts: list[dict[str, Any]] = field(default_factory=list)
    #: **本轮启动前置准备**的事实（app 侧 ``perf/round_prepare.py::prepare(ctx)`` 的返回值）：
    #: 如"杀进程用了哪种方式 / 删缓存释放了多少字节 / 失败原因"。空 dict = 该 app 未提供
    #: 前置准备（不是"没测"）。清理失败不应让整批中断，但必须在这里可见。
    pre_round: dict[str, Any] = field(default_factory=dict)
    #: **启动失败重试之前**的额外前置准备事实（仅当 app 声明 ``PERF_ROUND_PREPARE_BEFORE_RETRY``）：
    #: 每次重试前一行 ``{"attempt": n, ...prepare 返回值...}``。空 list = 本次重试链路没有额外准备
    #: （不是"没测"）。与 ``pre_round`` 分开存，避免把"首发那一轮的事实"覆盖掉。
    pre_round_retries: list[dict[str, Any]] = field(default_factory=list)
    #: **本轮场景测量之后**的收尾事实（app 侧 ``perf/round_teardown.py::teardown(ctx)`` 的返回值）：
    #: 典型是"按合法路径退出被测应用"（sample_web 的 ``quit_app``）。空 dict = 该 app 未提供收尾
    #: （不是"没执行"）。收尾失败**不进** ``error_rounds``（测量已完成且有效），只在这里留痕。
    teardown: dict[str, Any] = field(default_factory=dict)
    resources: dict[str, Any] = field(default_factory=dict)
    #: 本轮**启动起点**的单调时钟毫秒（``int(time.monotonic()*1000)``，与
    #: ``ResourceSample.t_ms`` 同源）。场景要算"启动→某时刻"的资源窗口（如页面渲染场景的
    #: 启动加载期峰值/均值）时必须有这个基准，否则只能用场景被调用的时刻，漏掉启动段。
    t_launch_start_ms: int = 0
    #: 本轮原始采样点（JSONL）路径。空串 = 未落盘（采样关闭或写失败）。
    #: 用途：报告里的聚合值被质疑时，能回到"那一刻"逐点复核。
    samples_jsonl: str = ""
    # 按阶段切片（startup=launch_timer 窗口，case=用例执行窗口，idle=静息静置窗口，
    # scenario=场景测量窗口）
    resources_by_phase: dict[str, Any] = field(default_factory=dict)
    # 按场景切片的资源窗口（键=场景名，如 "scan"；值=该场景 JS 求值区间内的资源汇总，
    # 用于定位具体是哪个操作消耗了 CPU/内存，而不是和其余场景混在一起看）
    resources_by_scenario: dict[str, Any] = field(default_factory=dict)
    # 场景测量结果（如 fps：avg_fps/low_fps_ratio_pct/longest_frame_ms）
    scenarios: dict[str, Any] = field(default_factory=dict)
    step_durations_ms: list[float] = field(default_factory=list)
    case_duration_ms: float = 0.0
    tcs_status: str = "unknown"
    perf_status: str = "unknown"
    perf_violations: list = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_index": self.round_index,
            "result_dir": self.result_dir,
            "result_json": self.result_json,
            "launch_ms": self.launch_ms,
            "startup_ms": self.startup_ms,
            "launch_detail": dict(self.launch_detail),
            "launch_failure": dict(self.launch_failure),
            "launch_attempts": [dict(a) for a in self.launch_attempts],
            "pre_round": dict(self.pre_round),
            "pre_round_retries": [dict(item) for item in self.pre_round_retries],
            "teardown": dict(self.teardown),
            "resources": dict(self.resources),
            "t_launch_start_ms": self.t_launch_start_ms,
            "samples_jsonl": self.samples_jsonl,
            "resources_by_phase": {k: dict(v) for k, v in self.resources_by_phase.items()},
            "resources_by_scenario": {k: dict(v) for k, v in self.resources_by_scenario.items()},
            "scenarios": {k: dict(v) for k, v in self.scenarios.items()},
            "step_durations_ms": list(self.step_durations_ms),
            "case_duration_ms": self.case_duration_ms,
            "tcs_status": self.tcs_status,
            "perf_status": self.perf_status,
            "perf_violations": list(self.perf_violations),
            "error": self.error,
        }


@dataclass
class PerfCaseReport:
    """单条用例的多轮报告 + 聚合 + 基线判定。"""

    case_name: str = ""
    case_file: str = ""
    rounds: list[PerfRound] = field(default_factory=list)
    aggregate: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_name": self.case_name,
            "case_file": self.case_file,
            "rounds": [r.to_dict() for r in self.rounds],
            "aggregate": dict(self.aggregate),
            "verdict": dict(self.verdict),
        }


@dataclass
class PerfRunResult:
    """批次级结果（落盘 perf_summary.json / perf_report.html）。"""

    app: str = ""
    platform: str = ""
    run_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    repeat: int = 0
    cold_start: bool = True
    sample_interval_ms: int = 500
    enable_resource_sampling: bool = True
    steady_state_s: float = 0.0
    user_data_dir: str = ""
    #: 使用的预设名（``--perf-preset``；空 = 纯命令行手动指定场景）
    preset_name: str = ""
    #: 因缺素材等原因跳过的场景（[{"scenario","reason"}]）——报告必须展示，避免"少测了却看不出来"
    skipped_scenarios: list[dict[str, str]] = field(default_factory=list)
    #: 本次**声明要执行**的场景清单（预设展开后的最终清单，= ``PerfRunner(scenarios=...)``）。
    #: 用途：多档/多单元聚合报告据此**裁剪列集**——某档本次没声明跑某场景（如
    #: ``0k`` 档不跑 ``stress``）时不再渲染该列，也不报"未采集"（本就不在执行范围内）；
    #: "声明了跑但没采到数据"仍保留列 + 未采集原因（守报告纪律：空单元格必须自解释）。
    #: 空列表 = 无声明信息（旧产物 / 单测手写 PerfRunResult）→ 报告侧保守回落为"全列渲染"。
    planned_scenarios: list[str] = field(default_factory=list)
    #: 按场景隔离的选项（预设给的默认值）；与全局 ``scenario_options`` 合并时后者优先
    scenario_options_by_scenario: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 只在最后一轮执行一次的场景（如静息 idle）——必须进产物：否则看到
    #: ``scenarios.idle.rounds`` 与 ``repeat`` 不一致时，无法区分"设计如此"与"前几轮失败了"
    last_round_only: list[str] = field(default_factory=list)
    baseline: dict[str, Any] = field(default_factory=dict)
    cases: list[PerfCaseReport] = field(default_factory=list)
    run_dir: str = ""
    summary_path: str = ""
    report_path: str = ""
    #: 长表格（tidy format）跨平台聚合统计文件路径（见 ``core/perf/report.py::write_metrics_csv``），
    #: 用于把不同平台/机器跑出的结果按固定字段拼接对比；空串表示未生成（如旧调用方/单测未触发）。
    metrics_csv_path: str = ""
    git_rev: str = ""
    #: 主机规格 + 性能基准跑分（见 ``core/perf/host_bench.py``），每次 perf 调用只跑一次；
    #: 空 dict 表示未注入（如单测手写 PerfRunResult 未传 host_info），报告侧据此不渲染该卡片。
    host: dict[str, Any] = field(default_factory=dict)
    #: **批次级环境准备**的事实（app 侧 ``perf/run_setup.py::setup(ctx)`` 的返回值，见
    #: :meth:`PerfRunner._run_setup`）：如"客户端配置被改成哪个口径、哪个键没改、为什么没改成"。
    #: 空 dict = 该 app 未提供 run_setup（不是"没测"）。
    setup: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": PERF_SCHEMA_VERSION,
            "app": self.app,
            "platform": self.platform,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "repeat": self.repeat,
            "cold_start": self.cold_start,
            "sample_interval_ms": self.sample_interval_ms,
            "enable_resource_sampling": self.enable_resource_sampling,
            "steady_state_s": self.steady_state_s,
            "user_data_dir": self.user_data_dir,
            "preset_name": self.preset_name,
            "skipped_scenarios": [dict(s) for s in self.skipped_scenarios],
            "planned_scenarios": list(self.planned_scenarios),
            "scenario_options_by_scenario": {
                k: dict(v) for k, v in self.scenario_options_by_scenario.items()
            },
            "last_round_only": list(self.last_round_only),
            "baseline": dict(self.baseline),
            "cases": [c.to_dict() for c in self.cases],
            "run_dir": self.run_dir,
            "summary_path": self.summary_path,
            "report_path": self.report_path,
            "metrics_csv_path": self.metrics_csv_path,
            "git_rev": self.git_rev,
            "host": dict(self.host),
            "setup": dict(self.setup),
            "metric_scope": {
                "rss": "进程树合计（主进程 + children(recursive=True)，含 Electron helper）",
                "cpu": "进程树合计 CPU%（psutil 语义：单核可到 100%，多核合计可 >100%）",
                "num_fds": "Windows 无该语义 → null",
                # 平台能力随产物落盘：跨平台读数差异（fd 有无、RSS 口径、进程识别规则）必须自带说明
                "platform_capabilities": platform_capabilities(self.platform),
            },
        }


class PerfRunner:
    """多轮性能编排器。

    Args:
        app_name:                  被测 app（``sample_media`` 等）
        os_name:                   宿主机平台（``macos`` / ``windows``）
        case_files:                待执行 .tcs 文件绝对/相对路径列表
        repeat:                    同一用例重复轮次（默认 3）
        sample_interval_ms:        资源采样间隔
        cold_start:                True → 每轮 ``force_hard_restart=True`` 测真实冷启动
        warmup:                    True（默认）→ 正式测量轮之前额外完整跑一次冷启动+用例
                                    （不跑 perf 场景、不落盘、不计入统计），用于吸收本次
                                    perf 进程内第一次真实启动的一次性开销，避免它污染
                                    启动场景（launch_ms/startup_ms/resources_by_phase.startup）
                                    的跨轮统计。见
                                    docs/plans/archive/10-platform-scale-实施纪要.md
        baseline_path:             显式基线文件（最高优先级）
        enable_resource_sampling:  False → 仅测耗时，不做资源采样
        keep_screenshot:           True → ``hook_names=["screenshot"]``（默认关闭降噪）
        last_round_only_scenarios: 这些场景**只在最后一轮**执行一次（其余轮次跳过，且不写入该轮
                                    ``scenarios``/``resources_by_scenario``）。用于静息（idle）这类
                                    "什么都不干等 N 分钟"的场景：跨轮重复不带来额外信息量，只会线性
                                    拉长总时长，但又必须留在全量预设里（否则"一键跑全部"会少测静息）。
                                    非 ``scenarios`` 成员的名字会被忽略
        run_dir:                   产物根目录（默认 ``REPORT_DIR/perf/perf_<ts>``）
        host_info:                 主机规格+性能基准跑分（``core/perf/host_bench.py::benchmark_host()``
                                   的返回值），每次 perf 调用只跑一次，注入进 ``PerfRunResult.host``
        startup_ready_js:          app 侧注入的"启动就绪"JS（判定窗口左上角品牌是否渲染出来，
                                   见 ``core/perf/scenarios/app_loader.load_app_startup_ready_js``）；
                                   空串时退回通用口径（``document.readyState === 'complete'``）
        round_prepare:             app 侧「每轮启动前置准备」可调用对象（同步/异步均可），
                                   入参一份 ``ctx`` dict，返回值落 ``PerfRound.pre_round``。
                                   用于"杀进程 + 清缓存后再冷启动"这类场景：必须在
                                   ``prepare_engine_for_case()`` **之前**执行，故不是 scenario。
                                   ``None``（默认）= 该 app 不提供，行为与改动前完全一致
        round_prepare_before_retry: 启动失败重试（第 2/3 次尝试）**之前**是否再跑一遍
                                   ``round_prepare``（默认 ``False`` = 每轮只准备一次，零回归）。
                                   由 app 声明 ``PERF_ROUND_PREPARE_BEFORE_RETRY`` 决定，见
                                   ``app_loader.load_app_round_prepare_before_retry``；
                                   该次准备返回 ``abort_launch=True`` 时本轮直接判启动失败。
        run_setup:                 app 侧「**批次级**环境准备」可调用对象（同步/异步均可），
                                   在**第 1 轮启动之前**调用**一次**（入参 ``ctx``），返回值落
                                   ``PerfRunResult.setup`` → ``perf_summary.json`` 的 ``setup``。
                                   用于"整批只做一次、且必须在任何启动之前生效"的动作
                                   （典型：写被测客户端的启动期配置，客户端只在启动时读一次）。
                                   ``None``（默认）= 该 app 不提供，行为与改动前完全一致
        run_context:               调用方给的**批次级参数**（如 ``--use-cache`` / ``--load-mode``），
                                   原样合并进 ``run_setup`` 的 ``ctx``（``None`` 值剔除）。
                                   core 不认识这些键的语义（应用知识），只负责透传
        launch_probe:              app 侧「启动就绪探针」（鸭子类型，见
                                   ``app_loader.load_app_launch_probe``）：原生引擎无 CDP/JS 时
                                   用它判定就绪。``None`` = 沿用默认 ``AppProcessProbe``
        process_name:              资源采样的目标进程名（默认空 = 由 ``app_path`` 推导）。
                                   用于"启动入口进程"与"要监控的进程"不同的应用
                                   （如托盘进程拉起 UI 进程）
        on_round:                  每轮结束回调（打印进度用）
    """


    def __init__(
        self,
        *,
        app_name: str,
        os_name: str,
        case_files: list[str],
        repeat: int = 3,
        sample_interval_ms: int = 500,
        cold_start: bool = True,
        warmup: bool = True,
        baseline_path: str = "",
        baseline_tag: str = "",
        enable_resource_sampling: bool = True,
        keep_screenshot: bool = False,
        run_dir: str = "",
        metric_filter: Optional[list[str]] = None,
        cleanup_app: bool = True,
        launch_cdp_timeout_s: float = 60.0,
        scenarios: Optional[list[str]] = None,
        scenario_options: Optional[dict[str, Any]] = None,
        last_round_only_scenarios: Optional[list[str]] = None,
        steady_state_s: float = 0.0,
        user_data_dir: str = "",
        preset_name: str = "",
        skipped_scenarios: Optional[list[dict[str, str]]] = None,
        scenario_options_by_scenario: Optional[dict[str, dict[str, Any]]] = None,
        host_info: Optional[dict[str, Any]] = None,
        startup_ready_js: str = "",
        round_prepare: Optional[Callable[..., Any]] = None,
        round_prepare_before_retry: bool = False,
        round_teardown: Optional[Callable[..., Any]] = None,
        run_setup: Optional[Callable[..., Any]] = None,
        run_context: Optional[dict[str, Any]] = None,
        launch_probe: Any = None,
        process_name: str = "",
        on_round: Optional[Callable[[PerfCaseReport, PerfRound], None]] = None,
    ) -> None:
        self._app_name = app_name
        self._os_name = os_name
        self._case_files = list(case_files)
        self._repeat = max(1, int(repeat))
        self._sample_interval_ms = int(sample_interval_ms)
        self._cold_start = bool(cold_start)
        self._warmup_enabled = bool(warmup)
        self._baseline_path = baseline_path
        self._baseline_tag = (baseline_tag or "").strip()
        self._enable_sampling = bool(enable_resource_sampling)
        self._hook_names = ["screenshot"] if keep_screenshot else []
        self._metric_filter = list(metric_filter or [])
        self._cleanup_app = bool(cleanup_app)
        self._launch_cdp_timeout_s = float(launch_cdp_timeout_s)
        self._scenarios = list(scenarios or [])
        self._scenario_options = dict(scenario_options or {})
        self._last_round_only = [n for n in (last_round_only_scenarios or []) if n in self._scenarios]
        self._steady_state_s = max(0.0, float(steady_state_s))
        self._user_data_dir = (user_data_dir or "").strip()
        self._preset_name = (preset_name or "").strip()
        self._skipped_scenarios = [dict(s) for s in (skipped_scenarios or [])]
        self._scenario_options_by_scenario = {
            str(k): dict(v or {}) for k, v in (scenario_options_by_scenario or {}).items()
        }
        self._host_info = dict(host_info or {})
        self._startup_ready_js = (startup_ready_js or "").strip()
        self._round_prepare = round_prepare
        self._round_prepare_before_retry = bool(round_prepare_before_retry)
        self._round_teardown = round_teardown
        self._run_setup = run_setup
        self._run_context = {
            str(k): v for k, v in (run_context or {}).items() if v is not None
        }
        self._launch_probe = launch_probe
        self._process_name = (process_name or "").strip()
        self._on_round = on_round
        self._run_dir = run_dir
        self._run_id = ""
        self._engine: Any = None

    # ── 主流程 ────────────────────────────────────────────────────

    async def run(self) -> PerfRunResult:
        from uita.config import REPORT_DIR

        started = datetime.now()
        run_id = started.strftime("%Y%m%d-%H%M%S")
        # 统一收纳到 REPORT_DIR/perf/ 子目录下（不直接散落在 REPORT_DIR 顶层），
        # 与其余模式（batch/runner 等）的产物分区隔离，便于按 app 整批清理/归档。
        run_dir = self._run_dir or os.path.join(REPORT_DIR, "perf", f"perf_{run_id}")
        os.makedirs(run_dir, exist_ok=True)
        # 每轮 result_dir 以实际产物根目录为 base，故回写 self._run_dir
        self._run_dir = run_dir
        self._run_id = run_id

        baseline = load_baseline(
            self._app_name, explicit_path=self._baseline_path, tag=self._baseline_tag,
            platform=self._os_name,
        )
        if not baseline.loaded:
            logger.warning("[perf] 未加载到性能基线（perf_status 将恒为 unknown）: %s", baseline.error)

        result = PerfRunResult(
            app=self._app_name,
            platform=self._os_name,
            run_id=run_id,
            started_at=started.isoformat(timespec="seconds"),
            repeat=self._repeat,
            cold_start=self._cold_start,
            sample_interval_ms=self._sample_interval_ms,
            enable_resource_sampling=self._enable_sampling,
            steady_state_s=self._steady_state_s,
            user_data_dir=self._user_data_dir,
            preset_name=self._preset_name,
            skipped_scenarios=self._skipped_scenarios,
            # 声明清单（非"实际有数据"的清单）：报告侧据此区分"本档不跑该场景"与"跑了没采到"
            planned_scenarios=list(self._scenarios),
            scenario_options_by_scenario=self._scenario_options_by_scenario,
            last_round_only=list(self._last_round_only),
            baseline=baseline.to_dict(),
            run_dir=run_dir,
            git_rev=_git_rev(),
            host=self._host_info,
        )

        # 批次级环境准备（app 侧 run_setup）：必须早于**任何**一次启动——被测客户端可能
        # 只在启动时读取这些配置（如 CEF 磁盘缓存开关），跑起来再改就来不及了。
        result.setup = await self._run_batch_setup(run_dir)

        try:
            self._engine = await self._acquire_engine()
            for case_file in self._case_files:
                result.cases.append(await self._run_case(case_file, baseline))
        finally:
            await self._close_engine()

        result.finished_at = datetime.now().isoformat(timespec="seconds")

        from uita.core.perf.report import render_html, write_metrics_csv, write_summary

        result.summary_path = write_summary(result)
        result.report_path = render_html(result)
        result.metrics_csv_path = write_metrics_csv(result)

        # 批次结束日志（默认精简口径下必须可见：这是"这一批到底跑成什么样"的唯一一行）
        rounds = [r for case in result.cases for r in case.rounds]
        failed = [r for r in rounds if (r.tcs_status or "") in ("failed", "error")]
        errors = [r for r in rounds if r.error]
        logger.info(
            "[perf] 批次结束：%d 轮（用例通过 %d / 失败 %d / 异常 %d），总耗时 %s，报告目录=%s",
            len(rounds), len(rounds) - len(failed), len(failed), len(errors),
            human_ms((datetime.now() - started).total_seconds() * 1000.0), run_dir,
        )
        return result

    async def _acquire_engine(self) -> Any:
        """经 AppLifecycleStack 获取引擎（与 batch 模式同路径，保证收尾可清理）。"""
        from uita.core.lifecycle.app_lifecycle_stack import get_default_stack

        stack = get_default_stack()
        mgr = await stack.get_or_create(self._app_name, self._os_name)
        stack.register_root(self._app_name, self._os_name, mgr.engine)
        logger.info("[perf] 引擎 %s/%s（via AppLifecycleStack）", self._app_name, self._os_name)
        self._apply_user_data_dir(mgr.engine)
        return mgr.engine

    def _apply_user_data_dir(self, engine: Any) -> None:
        """把 ``--perf-user-data-dir`` 注入被测应用的启动参数（测试 profile 隔离）。

        做法：往 ``engine.app_process.extra_args`` 追加 ``--user-data-dir=...``。
        两者都是公开成员（``PlaywrightEngine.app_process`` 属性、``AppProcess.extra_args``
        字段），且 ``AppProcess._start()`` 会带上它——因此**不需要改 engine/AppProcess**（守 R2），
        且硬重启后依然生效。隔离 profile 是扫描/缩略图/搜索这类场景不污染用户图库索引的前提。
        """
        if not self._user_data_dir:
            return
        app_process = getattr(engine, "app_process", None)
        extra_args = getattr(app_process, "extra_args", None)
        if extra_args is None:
            logger.warning(
                "[perf] 引擎 %s 无 app_process.extra_args，--perf-user-data-dir 未生效"
                "（测量将使用用户默认 profile）",
                type(engine).__name__,
            )
            return
        arg = f"--user-data-dir={self._user_data_dir}"
        if arg not in extra_args:
            extra_args.append(arg)
        logger.info("[perf] 测试 profile 隔离: %s", arg)

    async def _close_engine(self) -> None:
        """收尾清理：关引擎 → 清栈 → （冷启动场景）关掉由本工具重启出来的被测应用。

        为什么必须关应用：``--perf-cold-start`` 每轮都由本工具 kill + 拉起应用，
        跑完不关会留下一个窗口（真机实测：下一轮首轮 old_pid 非空、用户机器被占用）。
        ``--perf-keep-app`` 可保留；``--no-perf-cold-start``（可能复用用户已有实例）
        时一律不关，避免误杀用户自己开的窗口。异常只告警，不影响判定。
        """
        if self._engine is not None:
            try:
                await self._engine.close()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[perf] 关闭引擎失败: %s", exc)
        try:
            from uita.core.lifecycle.app_lifecycle_stack import get_default_stack

            await get_default_stack().close_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[perf] 清理 AppLifecycleStack 失败: %s", exc)

        if not (self._cleanup_app and self._cold_start) or self._engine is None:
            return
        app_process = getattr(self._engine, "app_process", None)
        if app_process is None:
            return
        try:
            killed = await asyncio.to_thread(app_process.kill_all)
            logger.info("[perf] 收尾关闭被测应用: killed=%s", killed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[perf] 关闭被测应用失败: %s", exc)

    async def _run_batch_setup(self, run_dir: str) -> dict[str, Any]:
        """执行 app 侧「批次级环境准备」（``run_setup(ctx)``），事实落 ``PerfRunResult.setup``。

        只在**第 1 轮启动之前**调用一次；失败**不中止整批**（已进入测量的批次不该因为
        "配置没写成功"白跑一整轮再退出）：记 ``ok=False`` + ``error``，让报告与操作者都能看到
        "这次测量是在什么环境口径下跑的"。未提供该扩展点的 app 直接返回空 dict（零回归）。
        """
        if self._run_setup is None:
            return {}
        ctx: dict[str, Any] = {
            "app_name": self._app_name,
            "os_name": self._os_name,
            "run_dir": run_dir,
            "repeat": self._repeat,
            "cold_start": self._cold_start,
        }
        # 调用方给的批次级参数（命令行 --use-cache / --load-mode 等）原样透传：
        # core 不认识键的语义，语义与校验都在 app 侧 setup() 里。
        ctx.update(self._run_context)
        t0 = _monotonic_ms()
        try:
            out = self._run_setup(ctx)
            if inspect.isawaitable(out):
                out = await out
        except Exception as exc:  # noqa: BLE001 - 环境准备失败不该让整批不跑
            detail = f"{type(exc).__name__}: {exc}"
            logger.warning("[perf] 批次级环境准备失败（继续按现状开跑）: %s", detail)
            return {"ok": False, "error": detail, "elapsed_ms": _monotonic_ms() - t0}
        record = dict(out) if isinstance(out, dict) else {"result": out}
        record.setdefault("ok", True)
        record["elapsed_ms"] = _monotonic_ms() - t0
        logger.info("[perf] 批次级环境准备完成（%.0fms）: %s", record["elapsed_ms"], record)
        return record

    async def _run_round_prepare(self, round_record: PerfRound, *, retry_attempt: int = 0) -> None:
        """执行 app 侧「每轮启动前置准备 / 启动闸门」，事实写 ``round_record.pre_round``。

        契约（见 ``app_loader.load_app_round_prepare``）：``prepare(ctx)`` 同步或异步均可，
        ``ctx`` 是本轮上下文；返回值若为 dict 则原样落盘（框架不解释其字段，应用知识自描述）。

        ``retry_attempt > 0`` = 启动失败第 N 次重试前的**额外**准备（仅当 app 声明
        ``PERF_ROUND_PREPARE_BEFORE_RETRY``，见 :meth:`_run_round_prepare_for_retry`）：
        事实追加进 ``pre_round_retries[]``，不覆盖首发的 ``pre_round``。

        **失败策略：不中止整批**。大轮次循环（如 500 次启动）里单轮清理失败（缓存被占用、
        权限不足）就整体退出，会让"跑了几小时只拿到一半数据"更糟；这里记录 ``ok=False`` +
        ``error``，本轮照常启动测量（数据仍有参考价值），但失败事实同时写进
        ``round_record.error``（进 ``error_rounds``，报告可见），**不伪装成成功**。

        例外是 app 侧显式返回 ``abort_launch=True``（如 sample_web 的启动闸门判定"旧实例退不干净"）：
        此时本轮**不启动**（见 ``_run_case``），因为盲目启动只会撞单实例锁/启动超时。
        """
        if self._round_prepare is None:
            return
        t0 = _monotonic_ms()
        ctx = {
            "app_name": self._app_name,
            "os_name": self._os_name,
            "round_index": round_record.round_index,
            "repeat": self._repeat,
            "run_dir": self._run_dir,
            "result_dir": round_record.result_dir,
            "engine": self._engine,
            # 0 = 本轮首发启动前；N>0 = 启动失败第 N 次重试前（仅 PERF_ROUND_PREPARE_BEFORE_RETRY）
            "retry_attempt": retry_attempt,
        }
        # 批次级参数（--load-mode / --use-cache 等）与 _run_batch_setup 同口径透传：
        # app 侧每轮前置准备要用它决定"本轮注入什么客户端配置"，core 不认识键的语义。
        ctx.update(self._run_context)

        def _store(record: dict[str, Any]) -> None:
            """首发事实落 ``pre_round``；重试前准备追加进 ``pre_round_retries[]``。"""
            if retry_attempt > 0:
                round_record.pre_round_retries.append({"attempt": retry_attempt, **record})
            else:
                round_record.pre_round = record

        try:
            out = self._round_prepare(ctx)
            if inspect.isawaitable(out):
                out = await out
        except Exception as exc:  # noqa: BLE001 - 单轮准备失败不该中断整批
            detail = f"{type(exc).__name__}: {exc}"
            _store({"ok": False, "error": detail, "elapsed_ms": _monotonic_ms() - t0})
            round_record.error = f"前置准备失败: {detail}"
            logger.warning(
                "[perf] 第 %d 轮前置准备失败（本轮继续，失败已记录）: %s",
                round_record.round_index, detail,
            )
            return
        record = dict(out) if isinstance(out, dict) else {"result": out}
        record.setdefault("ok", True)
        record["elapsed_ms"] = _monotonic_ms() - t0
        _store(record)
        cache = record.get("cache") if isinstance(record.get("cache"), dict) else {}
        tag = round_tag(self._run_context, round_record.round_index)
        if cache:
            # 缓存清理单独成行（用户要看"这轮清了什么/清了多大/花了多久"）：
            # 事实来自 app 侧 round_prepare 的 cache 段；没有该事实的 app 不打（不伪造）。
            freed = cache.get("freed_bytes")
            size = f"{float(freed) / 1048576.0:.1f}MB" if isinstance(freed, (int, float)) else "未知"
            logger.info(
                "[perf] %s 缓存清理：释放 %s（清理耗时 %s）%s",
                tag, size, human_ms(cache.get("elapsed_ms")),
                f"；{cache.get('reason')}" if cache.get("reason") else "",
            )
        logger.info(
            "[perf] %s 前置准备完成（%s）%s",
            tag, human_ms(record["elapsed_ms"]),
            "；要求本轮不启动" if record.get("abort_launch") is True else "",
        )

    async def _run_round_teardown(self, round_record: PerfRound) -> None:
        """执行 app 侧「每轮收尾」（场景测量之后），事实写 ``round_record.teardown``。

        契约（见 ``app_loader.load_app_round_teardown``）：``teardown(ctx)`` 同步或异步均可，
        ``ctx`` 与 ``_run_round_prepare`` 同源（含批次 ``run_context`` 透传）；返回值若为 dict
        则原样落盘。

        **失败策略与 ``round_prepare`` 不同**：收尾失败**不**写 ``round_record.error``、不进
        ``error_rounds`` —— 测量已经完成且有效，把"应用没退干净"记成"本轮异常"会让报告把有效
        数据判成不可用。事实留在 ``teardown.ok=false`` + ``still_alive``，下一轮闸门兜底。
        """
        if self._round_teardown is None:
            return
        if round_record.launch_failure:
            # 应用根本没启动 → 谈不上退出（也不该把一个空动作写成"收尾失败"）
            return
        t0 = _monotonic_ms()
        ctx = {
            "app_name": self._app_name,
            "os_name": self._os_name,
            "round_index": round_record.round_index,
            "repeat": self._repeat,
            "run_dir": self._run_dir,
            "result_dir": round_record.result_dir,
            "engine": self._engine,
        }
        ctx.update(self._run_context)
        try:
            out = self._round_teardown(ctx)
            if inspect.isawaitable(out):
                out = await out
        except Exception as exc:  # noqa: BLE001 - 收尾失败不该让整批中断
            detail = f"{type(exc).__name__}: {exc}"
            round_record.teardown = {"ok": False, "error": detail}
            logger.warning("[perf] 第 %d 轮收尾失败（数据仍有效）: %s", round_record.round_index, detail)
            return
        record = dict(out) if isinstance(out, dict) else {"result": out}
        record.setdefault("ok", True)
        record["elapsed_ms"] = _monotonic_ms() - t0
        round_record.teardown = record
        logger.info(
            "[perf] 第 %d 轮收尾完成（%.0fms）%s",
            round_record.round_index, record["elapsed_ms"],
            f" gone_by={record.get('gone_by')}" if record.get("gone_by") else "",
        )

    async def _run_round_prepare_for_retry(self, round_record: PerfRound, attempt: int) -> bool:
        """启动失败第 ``attempt`` 次重试**之前**的前置准备；返回 ``True`` = 允许继续启动。

        仅当 app 声明 ``PERF_ROUND_PREPARE_BEFORE_RETRY=True`` 时才执行（默认 ``False`` → 直接放行，
        行为与耗时零变化）。为什么需要：重试走的是 ``force_hard_restart`` + 框架杀进程清场，**不经过**
        app 自己的退出路径；对单实例锁/驱动级自我保护的应用，旧实例仍在时再拉一次只会换个方式失败。

        app 侧返回 ``abort_launch=True`` → 返回 ``False``：本轮不再消耗剩余重试，直接判启动失败
        （"退不干净就不启动"，避免多烧两次启动超时）。
        """
        if not self._round_prepare_before_retry or self._round_prepare is None:
            return True
        await self._run_round_prepare(round_record, retry_attempt=attempt)
        last = round_record.pre_round_retries[-1] if round_record.pre_round_retries else {}
        if last.get("abort_launch") is True:
            logger.error(
                "[perf] 第 %d 轮第 %d 次重试前准备判定「不可启动」（旧实例仍未退出），"
                "本轮结束并进入下一轮",
                round_record.round_index, attempt,
            )
            return False
        return True

    def _options_for(self, name: str) -> dict[str, Any]:
        """合并"预设按场景选项"与"命令行全局选项"（**全局优先**）。

        必要性：``count``（thumbs / video_decode）、``quiet_ms``（dir_view / filter）等同名键
        在不同场景语义不同；预设默认值必须按场景隔离，而命令行是全局覆盖（用户显式意图）。
        """
        merged = dict(self._scenario_options_by_scenario.get(name) or {})
        merged.update(self._scenario_options)
        return merged

    def _run_options_for(
        self, name: str, sampler: ResourceSampler, *, round_index: int = 0,
        launch_start_ms: int = 0,
    ) -> dict[str, Any]:
        """本场景的最终选项 = :meth:`_options_for` + 按契约注入的**框架上下文**。

        命名注意：**不能**叫 ``_scenario_options``——那是 ``__init__`` 里的实例属性
        （dict，预设给的按场景选项），同名方法会被实例属性遮蔽，调用时变成
        ``'dict' object is not callable``（2026-10-08 真被单测抓出来过）。

        注入规则（core 只认契约，不认识具体场景名——守 R2）：仅当该场景 ``measure()``
        签名**显式声明**了对应参数时才注入，未声明者选项一字不动（零回归）。场景侧可自行
        按需消费（如 ``alive_check`` 的返回 dict 取 ``dict["alive"]``）。

        - ``alive_check``：``sampler.alive_signals``（**多信号**存活判定，返回信号明细 dict：
          名称扫描 / 已知 pid 存活 / 主进程 pid 是否在；2026-10-08 前只注入布尔
          ``has_target_process``，只看进程名，会把"名字规则失效但进程还在"误判成应用退出）。
          为什么需要：静息（idle）这类"什么都不干等 N 分钟"的场景，若被测应用中途退出，
          继续等只会采出一堆空样本 + 一份"计划 600s、实际 135s"的读数（2026-10-08 真机问题）；
          压测（stress）同样靠它判"应用是否真的已关闭 / 启动后是否又消失"。
        - ``sampler``：**正在采样的同一个** ``ResourceSampler`` 实例。场景要"关闭被测应用"
          时必须按 pid 强杀，而 pid 事实（主进程 + 递归子进程 + 已知 pid）只在采样器里；
          场景自建采样器从未采过样、``_last_seen`` 为空 → 按 pid 强杀恒为空集
          （压测真机事故：主进程已被 ``kill_all`` 杀掉，但残留子进程既杀不掉、
          又继续被存活判定看到，白等满超时后判定"关不掉应用"而中止整档压测）。
        - ``out_dir`` / ``tier`` / ``round_index``：本次运行的产物根目录、档位标签
          （``lib-50k`` → ``50K``）、当前轮次号——压测场景要用它们给日志 zip 命名
          （``${测试标题}_${测试次数}_${时间戳}.zip``，2026-10-12 用户要求）。
          这些值是**框架上下文**，用户经 ``--perf-scenario-opt`` 传的同名键一律被覆盖/剔除。
        """
        opts = self._options_for(name)
        from uita.core.perf.scenarios import accepted_options

        accepted = accepted_options(name)
        injected: dict[str, Any] = {
            "alive_check": sampler.alive_signals,
            "sampler": sampler,
            "out_dir": self._run_dir,
            "tier": self._tier_label(),
            "round_index": int(round_index or 0),
            "launch_start_ms": int(launch_start_ms or 0),
            # 批次级参数（--load-mode / --use-cache 等）原样透传：场景要用它给控制台行打
            # 档位标签（如 `[Both-1]`）。core 同样不解释键的语义，只透传（与 round_prepare 一致）。
            "run_context": dict(self._run_context),
        }
        for key, value in injected.items():
            if key in accepted:
                opts[key] = value
            elif key in opts:
                # 该场景不声明该参数 → 剔除，避免把框架注入项当作用户选项留在产物里
                opts.pop(key)
                logger.debug("[perf] 场景 %s 不接受 %s，已忽略该选项", name, key)
        return opts

    def _dump_samples(
        self,
        round_record: "PerfRound",
        sampler: ResourceSampler,
        *,
        case_file: str,
        round_index: int,
        t_launch_start: int,
    ) -> str:
        """把本轮原始采样点 + 场景事件写成 ``samples/rNN_<case>.jsonl``。

        Returns:
            写入路径；未配置产物目录或写失败时返回 ``""``（辅助产物，不中止本轮）。
        """
        if not self._run_dir:
            return ""
        from uita.core.perf.series import write_samples_jsonl

        events: list[dict[str, Any]] = []
        for scenario_name, payload in (round_record.scenarios or {}).items():
            if not isinstance(payload, dict):
                continue
            for item in payload.get("_events") or []:
                if isinstance(item, dict):
                    events.append({**item, "scenario": scenario_name})
        stem = re.sub(r"[^\w.-]+", "_", os.path.basename(str(case_file or "case")))
        path = os.path.join(
            self._run_dir, "samples", f"r{int(round_index) + 1:02d}_{stem}.jsonl",
        )
        return write_samples_jsonl(
            path,
            sampler.samples,
            meta={
                "case": str(case_file or ""),
                "round_index": int(round_index),
                "process_name": self._process_name,
                "interval_ms": self._sample_interval_ms,
                "t_launch_start_ms": int(t_launch_start),
                "scenarios": sorted((round_record.scenarios or {}).keys()),
            },
            events=events,
        )

    def _tier_label(self) -> str:
        """档位展示标签（``lib-50k`` → ``50K``）；无档位时空串（由场景侧兜底记 ``unknown``）。

        只做"去 ``lib-`` 前缀 + 大写"这种展示层转换，不改档位语义；日志 zip 的
        ``${测试标题}`` 用的就是它（与多档聚合报告里的 tier 名一致，便于对照）。
        """
        raw = (self._baseline_tag or "").strip()
        if raw.lower().startswith("lib-"):
            raw = raw[4:]
        return raw.upper()

    async def _run_scenarios(
        self, round_record: PerfRound, sampler: ResourceSampler, *, is_last_round: bool = True,
    ) -> None:
        """执行本轮的场景测量（如 fps/scan），结果写入 ``round_record.scenarios``。

        同时为每个场景单独打时间戳，切出该场景专属的资源采样窗口写入
        ``round_record.resources_by_scenario[name]``——这样"扫描过程 CPU/内存"这类问题
        才能定位到具体某个场景，而不是和其余场景混在一整段 ``scenario`` 阶段里。

        ``last_round_only_scenarios`` 里的场景在非最后一轮**整段跳过**（既不执行也不记录），
        使跨轮聚合只看到真正执行过的那一轮数据（``_aggregate_scenarios`` 以"哪些轮有该场景
        结果"为准，故无需额外特判）。

        单场景失败只记 ``error``/``unsupported``，不影响 TCS 用例结果与其余场景；
        资源窗口用 ``finally`` 记录，失败场景也留下"失败前后资源如何变化"的数据。

        选项走 :meth:`_run_options_for`（在预设/命令行选项之上按签名契约注入存活探测
        ``alive_check``）——静息类场景据此"应用退出即提前结束"，避免空等满时长。
        """
        from uita.core.perf.scenarios import ScenarioBridge, ScenarioUnsupported, run_scenario

        bridge = ScenarioBridge(self._engine)
        for name in self._scenarios:
            if not is_last_round and name in self._last_round_only:
                logger.info("[perf] 场景 %s 跳过本轮（配置为仅最后一轮执行一次）", name)
                continue
            t0 = _monotonic_ms()
            # 场景执行体是单次 bridge.evaluate()（应用内 JS 轮询直到收敛或撑到
            # timeout_ms），期间 Python 侧完全没有中间输出——scan 上限已拉宽到
            # 2 小时、gallery/dir_view 到 2 分钟（见 apps/sample_media/perf/scenarios），
            # 若不打"开始执行"日志，这段静默期与真实卡死/死锁在控制台上无法区分
            # （真机曾误报"卡住"，实为场景仍在合法轮询窗口内运行）。
            logger.info("[perf] 场景 %s 开始执行（最长等待见该场景 timeout_ms 配置）...", name)
            try:
                result = await self._measure_scenario(name, bridge, sampler, round_record)
                round_record.scenarios[name] = result
                if result.get("error"):
                    logger.warning("[perf] 场景 %s 本轮异常: %s", name, result["error"])
                else:
                    logger.info(
                        "[perf] 场景 %s 完成，耗时 %dms", name, _monotonic_ms() - t0,
                    )
            except ScenarioUnsupported as exc:
                round_record.scenarios[name] = {"unsupported": str(exc)}
                logger.info("[perf] 场景 %s 不可测: %s", name, exc)
            except Exception as exc:  # noqa: BLE001
                round_record.scenarios[name] = {"error": f"{type(exc).__name__}: {exc}"}
                logger.warning("[perf] 场景 %s 执行失败: %s", name, exc)
            finally:
                t1 = _monotonic_ms()
                round_record.resources_by_scenario[name] = sampler.summary_between(t0, t1)

    async def _call_scenario(
        self, name: str, bridge: Any, sampler: ResourceSampler, round_record: PerfRound,
    ) -> dict[str, Any]:
        """跑一次场景并归一化结果（非 dict 一律记成 ``error``，绝不把异常结构当数据）。"""
        from uita.core.perf.scenarios import run_scenario

        result = await run_scenario(
            name, bridge,
            **self._run_options_for(
                name, sampler,
                round_index=round_record.round_index,
                launch_start_ms=round_record.t_launch_start_ms,
            ),
        )
        if not isinstance(result, dict):
            return {"error": f"场景返回类型异常: {type(result).__name__}"}
        return result

    async def _measure_scenario(
        self, name: str, bridge: Any, sampler: ResourceSampler, round_record: PerfRound,
    ) -> dict[str, Any]:
        """跑场景；**瞬态失败就补救重试**，尽量别把数据留空（用户口径 2026-10-13）。

        真机现场：5K/10K/50K 共 7 轮在 ``search`` 起连续 ``TargetClosedError``
        （目标页/浏览器已关闭）→ 检索/筛选/帧率三块整档留空。而这类失败**是可救的**：
        引擎的 ``self._page`` 常只是指向了被销毁的旧页（``connectOverCDP`` 不跟踪新窗口），
        或应用进程已经退出。故两级补救，各重试一次：

          ① :func:`uita.core.perf.recovery.heal_engine_page` —— 让引擎重新指向可用页；
          ② 重新拉起应用（``prepare_engine_for_case(force_hard_restart=True)``）——
             仅当采样器确认**进程已不在**时才做（否则会把"页面问题"升级成"重启应用"，
             无谓地打断测量）。

        补救过程写进结果的 ``recovery``（试过哪几级 / 成功于哪一级 / 原始错误），
        两级都没救回才如实记 ``error`` + ``recovery.ok=false``——报告据此显示原因，
        不再出现"只有一个 — 谁也看不出为什么"。
        """
        from uita.core.perf.recovery import heal_engine_page, is_transient_error, scenario_error_text

        result = await self._call_scenario(name, bridge, sampler, round_record)
        error = scenario_error_text(result)
        if not error or not is_transient_error(error):
            return result

        recovery: dict[str, Any] = {"reason": error[:300], "steps": []}

        # ① 引擎页自愈（能自愈就不用重启应用，代价最小）
        ok, detail = await heal_engine_page(
            self._engine,
            verify=lambda: bridge.evaluate("location.href"),
            log_prefix=f"perf/{name}",
        )
        recovery["steps"].append({"action": "heal_page", "ok": bool(ok), "detail": detail[:200]})
        if ok:
            retry = await self._call_scenario(name, bridge, sampler, round_record)
            if not scenario_error_text(retry):
                logger.info("[perf] 场景 %s 首跑瞬态失败（%s），页面自愈后重试成功", name, error[:80])
                retry["recovery"] = {**recovery, "ok": True, "by": "heal_page"}
                return retry
            result = retry

        # ② 应用进程不在了 → 重新拉起再重试（"数据比这轮干净更重要"）
        alive = bool(await asyncio.to_thread(sampler.has_target_process))
        recovery["steps"].append({"action": "app_alive_check", "alive": alive})
        if not alive and await self._relaunch_after_failure(name):
            recovery["steps"].append({"action": "relaunch", "ok": True})
            retry = await self._call_scenario(name, bridge, sampler, round_record)
            if not scenario_error_text(retry):
                logger.info("[perf] 场景 %s 应用已退出，重新拉起后重试成功", name)
                retry["recovery"] = {**recovery, "ok": True, "by": "relaunch"}
                return retry
            result = retry
        elif not alive:
            recovery["steps"].append({"action": "relaunch", "ok": False})

        result = dict(result) if isinstance(result, dict) else {"error": str(result)}
        result["recovery"] = {**recovery, "ok": False}
        logger.warning(
            "[perf] 场景 %s 补救失败（自愈/重启均未成功），原因: %s", name, error[:120],
        )
        return result

    async def _relaunch_after_failure(self, name: str) -> bool:
        """补救用：重新拉起被测应用（返回是否成功）。异常只记日志，不中断本轮其余场景。"""
        from uita.core.lifecycle.engine_lifecycle import prepare_engine_for_case

        try:
            await prepare_engine_for_case(
                self._engine,
                case_name=f"perf-recover-{name}",
                is_first=False,
                tag_prefix="perf-recover",
                force_hard_restart=True,
            )
            logger.info("[perf] 场景 %s 补救：已重新拉起应用", name)
            return True
        except Exception as exc:  # noqa: BLE001 - 救不回来就如实记失败，不向上抛
            logger.warning("[perf] 场景 %s 补救：重新拉起应用失败: %s", name, exc)
            return False

    async def _run_warmup_round(self, case: Any, case_file: str) -> None:
        """热身轮：完整跑一次冷启动 + TCS 用例，**不跑任何 perf 场景/静置、不采样、不落盘**，
        结果全部丢弃，不计入统计。

        目的仅仅是吸收本次 perf 进程内"第一次真实启动"的一次性开销（磁盘缓存冷、
        首次 CDP 握手等），让后续 ``self._repeat`` 轮的"启动场景"（launch_ms/startup_ms/
        resources_by_phase.startup）口径一致、互相可比。**不改变** ``--perf-scenario``
        选中场景的执行范围——即使命令行选了 scan/thumbs/fps 等场景，热身轮也一律跳过。

        异常只记警告日志，不阻断后续正式测量轮（热身的价值是"减少首轮噪声"，不是
        正确性验证，失败也不该让整批 perf 直接失败）。
        """
        import shutil
        import tempfile

        from uita.core.interpreter import TCSInterpreter
        from uita.core.lifecycle.engine_lifecycle import prepare_engine_for_case

        tmp_dir = tempfile.mkdtemp(prefix="uita-perf-warmup-")
        t0 = _monotonic_ms()
        try:
            # 热身轮做同样的环境复位：否则它吸收的是"缓存命中"的一次性开销，与正式轮
            # （清缓存后冷启动）噪声类型不同，热身就白做了。事实丢弃（不计入统计）。
            await self._run_round_prepare(PerfRound(round_index=0))
            await prepare_engine_for_case(
                self._engine,
                case_name=case.name or os.path.basename(case_file),
                is_first=True,
                tag_prefix="perf-warmup",
                force_hard_restart=self._cold_start,
            )
            interpreter = TCSInterpreter(
                case,
                case_file=case_file,
                result_dir=tmp_dir,
                hook_names=self._hook_names,
                engine=self._engine,
            )
            await interpreter.run()
            logger.info("[perf] 热身轮完成（不计入统计），耗时 %dms", _monotonic_ms() - t0)
        except Exception as exc:  # noqa: BLE001 - 热身失败不阻断正式测量轮
            logger.warning("[perf] 热身轮异常（不计入统计，继续正式测量）: %s", exc)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    async def _launch_with_retries(
        self,
        *,
        round_record: PerfRound,
        sampler: ResourceSampler,
        case_name: str,
        round_index: int,
        t_launch_start: int,
    ) -> tuple[Optional[Any], int]:
        """启动 + **失败重试**，返回 ``(LaunchMetrics | None, 本次启动结束时刻)``。

        用户口径（2026-10-15）："任何场景启动失败增加两次重试，启动失败后杀掉进程再次尝试，
        如果再次失败就尝试第三次，如果还失败就结束直接开始下一场景……过程中所有失败要按照
        启动失败记录当前环境情况……数据要完整展示在 html 报告里"。

        每次失败都按**固定顺序**做三件事（顺序不可换）：

        1. **先取证**（`_launch_failure_record`）：那一刻的系统内存快照（口径与 stress 共用
           唯一实现）+ 该次尝试窗口的进程树峰值 → 追加进 ``round_record.launch_attempts``。
           "过程中所有失败"都要留痕——哪怕后面重试成功，那几次失败也是"这轮启动不稳"的证据。
        2. **杀进程**（`_kill_leftover_app` → `AppProcess.kill_all()`）：Electron 应用有
           单实例锁，半死的旧实例会把新实例的 `--remote-debugging-port` 参数吞掉后自行退出
           （CDP 端口永不打开）——不杀就直接重试，几乎必然再失败。
        3. **再启动**：重试一律 ``force_hard_restart=True``（``clean_launch`` 自带"杀+拉起"，
           与 `--perf-cold-start` 同一条成熟路径）。**R3 红线**：生命周期一律经
           ``prepare_engine_for_case``，绝不直接调 ``engine.launch()/restart()``。

        结果：任一次成功 → ``launch_failure`` 保持空 dict（本轮照常继续），返回成功的
        ``LaunchMetrics``；``PERF_LAUNCH_ATTEMPTS`` 次全失败 → 末次现场写进
        ``round_record.launch_failure``（下游据它中止本轮）。``t_launch_end`` 取**末次尝试**
        的结束时刻，故成功轮的 ``resources_by_phase.startup`` 覆盖"含重试的整段启动过程"
        （这轮启动的真实代价就是这些），而每轮上报的 ``launch_ms``/``startup_ms`` 仍只来自
        成功那次尝试自身的测量。
        """
        from uita.core.lifecycle.engine_lifecycle import prepare_engine_for_case

        total = max(1, int(PERF_LAUNCH_ATTEMPTS))
        attempts: list[dict[str, Any]] = []
        t_end = t_launch_start
        for attempt in range(1, total + 1):
            t_attempt_start = _monotonic_ms()
            stage = ""
            detail = ""
            # 重试前的前置准备（默认关闭；app 声明 PERF_ROUND_PREPARE_BEFORE_RETRY 才生效）。
            # 判定"不可启动"= 旧实例仍活着：再拉一次只会撞单实例锁/启动超时，直接结束本轮。
            if attempt > 1 and not await self._run_round_prepare_for_retry(round_record, attempt):
                t_end = _monotonic_ms()
                attempts.append(_launch_failure_record(
                    reason="重试前前置准备未完成（旧实例仍存活，未启动以免重复实例）",
                    stage="pre_round_abort", sampler=sampler,
                    t0_ms=t_attempt_start, t1_ms=t_end,
                    attempt=attempt, attempts_total=total,
                ))
                break
            try:
                # app 侧原生探针（进程 + 窗口类名等应用知识）优先；未提供则沿用默认探针
                probe = self._launch_probe or AppProcessProbe(
                    getattr(self._engine, "app_process", None),
                    engine=self._engine,
                    # app 侧就绪探针（窗口左上角品牌已渲染）；空串 → 通用 readyState 口径
                    ready_js=self._startup_ready_js,
                )
                launch_timer = LaunchTimer(
                    probe,
                    cdp_timeout=self._launch_cdp_timeout_s,
                )
                launch = await launch_timer.measure(
                    lambda: prepare_engine_for_case(
                        self._engine,
                        case_name=case_name,
                        is_first=(round_index == 0 and not self._warmup_enabled),
                        tag_prefix="perf",
                        # 首发沿用 --perf-cold-start 配置；重试一律硬重启（失败后先杀干净再拉起）
                        force_hard_restart=self._cold_start or attempt > 1,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 单次尝试失败不中断本轮，交给重试
                t_end = _monotonic_ms()
                stage = "launch_error"
                detail = f"{type(exc).__name__}: {exc}"
                attempts.append(_launch_failure_record(
                    reason=detail, stage=stage, sampler=sampler,
                    t0_ms=t_attempt_start, t1_ms=t_end,
                    attempt=attempt, attempts_total=total,
                ))
            else:
                t_end = _monotonic_ms()
                if not launch.timeout_stage:
                    round_record.launch_attempts = attempts
                    round_record.launch_ms = launch.launch_ms
                    round_record.startup_ms = launch.startup_ms
                    round_record.launch_detail = launch.to_dict()
                    if attempts:
                        logger.info(
                            "[perf] 第 %d 轮第 %d/%d 次尝试启动成功（前 %d 次失败已取证落盘，本轮继续）",
                            round_index + 1, attempt, total, len(attempts),
                        )
                    return launch, t_end
                # 启动**没抛异常但不就绪**（旧实例未退/新进程未现/CDP 未连/品牌未渲染，
                # 见 ``LaunchTimer.timeout_stage``）：同样是启动失败，一样取证 + 重试
                stage = f"timeout_{launch.timeout_stage}"
                detail = f"启动未就绪（{launch.timeout_stage}）：等待超时"
                attempts.append(_launch_failure_record(
                    reason=detail, stage=stage, sampler=sampler,
                    t0_ms=t_attempt_start, t1_ms=t_end,
                    attempt=attempt, attempts_total=total,
                ))
            retrying = attempt < total
            if retrying:
                logger.warning(
                    "[perf] 第 %d 轮第 %d/%d 次启动失败（%s）：%s → 杀进程后重试",
                    round_index + 1, attempt, total, stage, detail,
                )
            else:
                logger.error(
                    "[perf] 第 %d 轮启动失败：%d 次尝试全部失败（末次 %s：%s）→ 本轮中止，进入下一轮",
                    round_index + 1, total, stage, detail,
                )
            # 每次都杀：重试前是为了让新实例能拿到调试端口；重试用尽时是为了"本轮结束不留半死实例，
            # 否则下一轮首发若没开硬重启，就会撞上旧实例吞参数、白失败一轮"
            await self._kill_leftover_app(round_index + 1, attempt, retrying=retrying)
        round_record.launch_attempts = attempts
        round_record.launch_failure = dict(attempts[-1]) if attempts else {}
        return None, t_end

    async def _kill_leftover_app(self, round_no: int, attempt: int, *, retrying: bool = True) -> None:
        """启动失败后的清场：强杀遗留实例（best-effort，**绝不抛**）。

        为什么必须显式杀（见 `_launch_with_retries` 第 2 步）：只靠下一次 ``force_hard_restart``
        也能杀，但"启动失败 → 立刻杀干净"能把单实例锁/CDP 端口尽早释放，避免重试又撞上
        "旧实例吞参数"。走 core 既有实现 ``AppProcess.kill_all()``（macOS `pkill -x` /
        Windows·Linux psutil），不自造杀进程逻辑；清场失败只记警告——重试本身仍要通过
        ``force_hard_restart`` 的硬重启。

        ``retrying=False`` 表示"这是本轮最后一次失败，重试用尽"：杀干净是为了**下一轮**首发
        不撞上半个死掉的旧实例（此时日志措辞不同，别让人以为还要重试）。
        """
        app_path = _app_path(self._app_name)
        if not app_path:
            return
        try:
            from uita.core.lifecycle.app_process import AppProcess

            killed = await asyncio.to_thread(lambda: AppProcess(app_path=app_path).kill_all())
            logger.info(
                "[perf] 第 %d 轮第 %d 次启动失败清场（%s）：%s",
                round_no, attempt, "重试前" if retrying else "重试用尽，留给下一轮",
                "已强杀遗留实例" if killed else "未发现遗留实例",
            )
        except Exception as exc:  # noqa: BLE001 - 清场失败不该阻断重试
            logger.warning("[perf] 启动失败清场异常（忽略，继续重试）: %s", exc)

    async def _run_case(self, case_file: str, baseline: PerfBaseline) -> PerfCaseReport:
        from uita.core.dsl.parser import parse_file
        from uita.core.interpreter import TCSInterpreter
        from uita.core.result.collector import make_run_result_dir

        case = parse_file(case_file)
        report = PerfCaseReport(case_name=case.name or os.path.basename(case_file), case_file=case_file)

        if self._warmup_enabled:
            await self._run_warmup_round(case, case_file)

        for round_index in range(self._repeat):
            result_dir = make_run_result_dir(
                case_file, base_dir=self._run_dir, run_ts=f"{self._run_id or 'perf'}-r{round_index + 1}",
            )
            round_record = PerfRound(round_index=round_index + 1, result_dir=result_dir)
            round_record.result_json = os.path.join(result_dir, "result.json")
            sampler = ResourceSampler(
                app_path=_app_path(self._app_name),
                # 空串 = 由 app_path 推导（既有行为）；非空时监控指定进程
                # （启动入口进程 ≠ 要监控的进程，如托盘拉起 UI 进程的应用）
                process_name=self._process_name,
                interval_ms=self._sample_interval_ms,
                enabled=self._enable_sampling,
            )
            # 采样必须在 launch **之前**启动：否则启动期的 RSS/CPU 峰值全部丢失
            # （场景 1 要求「真机冷启动 CPU/内存」，这是此前实测的真实缺口）。
            t_launch_start = _monotonic_ms()
            round_record.t_launch_start_ms = t_launch_start
            t_launch_end = t_launch_start
            t_case_end = t_launch_start
            t_idle_start = t_launch_start
            t_idle_end = t_launch_start
            t_scenario_end = t_launch_start
            sampler.start()
            # 每轮前置准备（探测是否在运行 → quit_app 退出 → 校验）必须在启动**之前**完成
            await self._run_round_prepare(round_record)
            if round_record.pre_round.get("abort_launch") is True:
                # app 侧判定"旧实例退不干净"：**不盲目启动**——单实例锁下再拉一次新实例只会
                # 换来「应用启动超时 (30s)」，还会把 3 次启动重试全部烧光。本轮直接结束，
                # 下一轮重新走闸门（根因写在 ``launch_failure.reason``，报告可读）。
                _gate_reason = "前置准备未完成（旧实例仍存活），本轮不启动"
                _gate_detail = "；".join(
                    str(item) for item in (round_record.pre_round.get("errors") or []) if item
                )
                round_record.launch_failure = {
                    "stage": "pre_round_abort",
                    "reason": (
                        f"{_gate_reason}：{_gate_detail}" if _gate_detail else _gate_reason
                    )[:500],
                    "elapsed_ms": round_record.pre_round.get("elapsed_ms"),
                    # 0 次 = 一次启动都没发起（报告据此不写"3 次尝试全部失败"）
                    "attempt": 0,
                    "attempts_total": 0,
                }
                round_record.error = str(round_record.launch_failure["reason"])
                round_record.tcs_status = "skipped"
                t_case_end = _monotonic_ms()
                logger.error(
                    "[perf] 第 %d 轮前置准备判定不可启动（跳过启动与测量，进入下一轮）: %s",
                    round_index + 1, round_record.error,
                )
            else:
                try:
                    launch, t_launch_end = await self._launch_with_retries(
                        round_record=round_record,
                        sampler=sampler,
                        case_name=case.name or os.path.basename(case_file),
                        round_index=round_index,
                        t_launch_start=t_launch_start,
                    )
                    if launch is None:
                        # N 次尝试全部失败 → 本轮到此为止（下方 abort 分支跳过用例/静息/场景）。
                        # ``error`` 记末次原因（报告「轮次异常」可见、`error_rounds` 聚合照旧）；
                        # ``tcs_status`` 沿用改动前语义：抛异常→error，未就绪→skipped
                        last = round_record.launch_failure or {}
                        round_record.error = str(last.get("reason") or "启动失败")
                        round_record.tcs_status = (
                            "error" if str(last.get("stage") or "") == "launch_error" else "skipped"
                        )
                        t_case_end = _monotonic_ms()
                    else:
                        interpreter = TCSInterpreter(
                            case,
                            case_file=case_file,
                            result_dir=result_dir,
                            hook_names=self._hook_names,
                            engine=self._engine,
                        )
                        collector = await interpreter.run()
                        t_case_end = _monotonic_ms()

                        case_result = collector.finalize()
                        round_record.step_durations_ms = [
                            float(s.duration_ms) for s in case_result.steps
                        ]
                        round_record.case_duration_ms = float(case_result.duration_ms)
                        round_record.tcs_status = "passed" if case_result.is_passed else "failed"
                except Exception as exc:  # noqa: BLE001 - 单轮失败不应中断整批
                    t_case_end = _monotonic_ms()
                    round_record.error = f"{type(exc).__name__}: {exc}"
                    round_record.tcs_status = "error"
                    logger.error(
                        "[perf] 第 %d 轮执行异常: %s", round_index + 1, exc, exc_info=True,
                    )

            # ── 启动失败（**重试用尽后**）= 本轮立即结束，直接进入下一轮（用户口径 2026-10-15）
            # 「启动失败记录好数据就直接结束本轮测试，开始下一轮（如 10K 第二轮启动失败，
            #  记录好数据直接开始第三轮，不要在第二轮浪费时间）」（先重试 2 次，见
            #  ``_launch_with_retries``；这里处理"3 次都失败"的收尾）。
            # 现场数据（``launch_attempts`` + 启动期采样）已经落盘，继续跑静息静置（可能几十分钟）
            # 与场景测量毫无意义：应用根本没打开，只会白等超时 + 采出一堆空读数。
            # **注意只对"启动失败"这样处理**——TCS 用例失败 / 场景异常的应用是正常运行的，
            # 静息与场景测量照常做完（那才是它们的测量对象）。
            if round_record.launch_failure:
                _abort_round_after_launch_failure(
                    self._scenarios, round_record, round_index,
                    last_round_only=self._last_round_only,
                    is_last_round=(round_index == self._repeat - 1),
                )
                t_case_end = max(t_case_end, t_launch_end)
                t_idle_start = t_idle_end = t_case_end
                t_scenario_end = t_case_end
            else:
                # 静息期（场景 2）：静置期间持续采样 CPU/内存，取**独立**窗口——不与后面的
                # 场景测量窗口混在一起（此前的 bug：二者曾被合并计成同一段 "scenario"，
                # 导致"应用什么都不干时资源占用是多少"无法单独回答）。
                # 放在 try/except 之外：TCS 用例失败也应完成静置采样。
                t_idle_start = t_case_end
                if self._steady_state_s > 0:
                    logger.info("[perf] 静息监控开始，持续 %.1fs（期间持续采样 CPU/内存，无输出属正常静置等待）...", self._steady_state_s)
                    await asyncio.sleep(self._steady_state_s)
                    logger.info("[perf] 静息监控完成，耗时 %.1fs", self._steady_state_s)
                t_idle_end = _monotonic_ms()

                # 场景测量在采样窗口内执行：这样"滑动期 CPU/内存"才落在 scenario 阶段切片里。
                # 放在 try/except 之外：TCS 用例失败不应导致场景指标缺失（两者独立可比）。
                if self._scenarios:
                    try:
                        await self._run_scenarios(
                            round_record, sampler, is_last_round=(round_index == self._repeat - 1),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("[perf] 场景测量异常（不影响本轮用例结果）: %s", exc)
                t_scenario_end = _monotonic_ms()
            sampler.stop()

            # 每轮收尾（app 侧 round_teardown）：场景测完就按 app 声明的口径退出应用，
            # 不让它挂到下一轮闸门才退出。放在 sampler.stop() 之后 → 不进入任何采样窗口。
            await self._run_round_teardown(round_record)

            # 原始采样点落盘（辅助产物：失败只告警，不影响本轮结果）
            round_record.samples_jsonl = self._dump_samples(
                round_record, sampler, case_file=case_file, round_index=round_index,
                t_launch_start=t_launch_start,
            )

            round_record.resources = sampler.summary()
            round_record.resources_by_phase = {
                "startup": sampler.summary_between(t_launch_start, t_launch_end),
                "case": sampler.summary_between(t_launch_end, t_case_end),
                "idle": sampler.summary_between(t_idle_start, t_idle_end),
                "scenario": sampler.summary_between(t_idle_end, t_scenario_end),
            }
            report.rounds.append(round_record)
            logger.info(
                "[perf] %s 轮次结束：前置 %s | 启动 %s | 用例 %s | 静息 %s | 场景 %s | 收尾 %s，结果=%s",
                round_tag(self._run_context, round_record.round_index),
                human_ms((round_record.pre_round or {}).get("elapsed_ms")),
                human_ms(round_record.launch_ms),
                human_ms(t_case_end - t_launch_end),
                human_ms(t_idle_end - t_idle_start),
                human_ms(t_scenario_end - t_idle_end),
                human_ms((round_record.teardown or {}).get("elapsed_ms")),
                round_record.tcs_status or "unknown",
            )
            if self._on_round is not None:
                try:
                    self._on_round(report, round_record)
                except Exception:  # noqa: BLE001 - 进度回调不影响执行
                    pass

        report.aggregate = _aggregate(report)
        # 判定已断开（2026-10-08 用户要求"不要门禁、只要真实值"）：不再调用 evaluate()，
        # verdict 固定为"未启用"。结构保持不变，未来要重开门禁时把上一行换回 evaluate() 即可。
        report.verdict = _disabled_verdict(baseline)
        for r, status in _propagate_verdict(report):
            r.perf_status = status

        # 逐轮注入 result.json 的 "perf" 段（只增不改）
        for r in report.rounds:
            if r.result_json and os.path.isfile(r.result_json):
                _inject_perf_section(
                    r.result_json,
                    {
                        "schema_version": PERF_SCHEMA_VERSION,
                        "round_index": r.round_index,
                        "launch_ms": r.launch_ms,
                        "startup_ms": r.startup_ms,
                        "rss_mb_avg": r.resources.get("rss_mb_avg"),
                        "rss_mb_peak": r.resources.get("rss_mb_peak"),
                        "rss_slope_mb_per_min": r.resources.get("rss_slope_mb_per_min"),
                        "cpu_percent_avg": r.resources.get("cpu_percent_avg"),
                        "cpu_percent_peak": r.resources.get("cpu_percent_peak"),
                        "thread_count_peak": r.resources.get("thread_count_peak"),
                        "num_fds_peak": r.resources.get("num_fds_peak"),
                        "proc_count_peak": r.resources.get("proc_count_peak"),
                        "sample_count": r.resources.get("sample_count"),
                        "sample_interval_ms": r.resources.get("sample_interval_ms"),
                        "resources_by_phase": r.resources_by_phase,
                        "resources_by_scenario": r.resources_by_scenario,
                        "scenarios": r.scenarios,
                        # 预设与跳过项也进逐轮产物：单看某一轮的 result.json 就能知道
                        # "这条命令跑了什么、少测了什么"
                        "preset_name": self._preset_name,
                        "skipped_scenarios": [dict(s) for s in self._skipped_scenarios],
                        "perf_status": r.perf_status,
                        "perf_failed": r.perf_status == "fail",
                        "launch_detail": r.launch_detail,
                        # 启动失败现场（含系统内存快照，口径同 stress）；未失败时为空 dict
                        "launch_failure": r.launch_failure,
                        # 本轮**每一次**启动失败尝试的完整流水（含重试后成功的情况）
                        "launch_attempts": r.launch_attempts,
                        # 本轮启动前置准备（杀进程/清缓存）的事实；未提供该 app 侧模块时为空
                        "pre_round": r.pre_round,
                        # 启动失败重试前的额外前置准备（仅当 app 声明 PERF_ROUND_PREPARE_BEFORE_RETRY）
                        "pre_round_retries": r.pre_round_retries,
                        # 本轮场景测量之后的收尾事实（app 侧 round_teardown）；未提供时为空 dict
                        "teardown": r.teardown,
                        "violations": r.perf_violations,
                        "tcs_status": r.tcs_status,
                    },
                )

        return report


# ── 聚合与判定 ────────────────────────────────────────────────────


def _first_not_none(*values: Optional[float]) -> Optional[float]:
    for value in values:
        if value is not None:
            return value
    return None


def _monotonic_ms() -> int:
    """与 ``ResourceSample.t_ms`` 同源的单调时钟毫秒（阶段切片的边界必须同源）。"""
    return int(time.monotonic() * 1000)


def _launch_failure_record(
    *, reason: str, stage: str, sampler: Optional[ResourceSampler],
    t0_ms: int, t1_ms: int,
    attempt: int = 1, attempts_total: int = 1,
) -> dict[str, Any]:
    """启动失败现场取证：**系统内存快照（口径同 stress）** + 失败尝试期间的进程树峰值。

    为什么要有（2026-10-15 用户口径）："perf 模式如果启动失败，抓取当时的系统占用和内存剩余
    情况（和 stress 模式数据一致）并记录，最终报告展示在失败场景下"——只知道"启动失败"而不知
    当时系统被占了多少/还剩多少，就无法判断是"机器内存不够"还是"探针/引擎的问题"。

    字段（``stage``/``reason``/``attempt`` 为归因，其余为读数，读数缺失记 ``None`` 不记 0）：
      - ``stage``：``launch_error``（``prepare_engine_for_case`` 抛异常）/
        ``timeout_<cdp|old_pid|new_pid|render>``（没抛异常但就绪探针超时未就绪）；
      - ``reason``：异常类型 + 文本（截断 500 字符）或"未就绪"说明；
      - ``attempt`` / ``attempts_total``：这是第几次尝试、本轮最多尝试几次（报告据此写
        "第 2/3 次"、"3 次尝试全部失败"——数字来自数据而不是报告端硬编码）；
      - ``elapsed_ms``：这次失败尝试从发起到判失败/抛异常的墙钟耗时；
      - ``mem_total_mb`` / ``mem_used_mb`` / ``mem_available_mb`` / ``mem_percent`` /
        ``swap_used_mb`` / ``swap_percent``：系统内存快照（键名与 ``stress`` 逐级记录一致）；
      - ``app_rss_mb_peak`` / ``app_cpu_percent_peak``：失败尝试窗口内被测应用进程树峰值。

    **绝不抛异常**：取证是"记录事实"，失败也要如实留痕（读数不可用即 ``None``），
    不能因为取证本身出错而把一轮 perf 的失败原因换成别的东西。
    """
    record: dict[str, Any] = {
        "stage": str(stage),
        "reason": str(reason)[:500],
        "elapsed_ms": max(0, int(t1_ms) - int(t0_ms)),
        "attempt": max(1, int(attempt)),
        "attempts_total": max(1, int(attempts_total)),
    }
    record.update(system_memory_snapshot())
    summary: dict[str, Any] = {}
    if sampler is not None:
        try:
            summary = sampler.summary_between(int(t0_ms), int(t1_ms)) or {}
        except Exception as exc:  # noqa: BLE001 - 取证失败不阻断、不改写失败原因
            logger.debug("[perf] 启动失败现场：进程树采样汇总失败: %s", exc)
            summary = {}
    record["app_rss_mb_peak"] = summary.get("rss_mb_peak")
    record["app_cpu_percent_peak"] = summary.get("cpu_percent_peak")
    return record


def _abort_round_after_launch_failure(
    scenario_names: list[str],
    round_record: "PerfRound",
    round_index: int,
    *,
    last_round_only: Any = (),
    is_last_round: bool = True,
) -> None:
    """启动失败 → **本轮立即结束**（用户口径 2026-10-15："记录好数据就直接开始下一轮，
    不要在第二轮浪费时间"）：把本轮"声明要跑、但没跑成"的场景如实标成"已跳过"并打一条日志。

    为什么要留痕（守 §12.9「空单元格必须自解释」纪律）：不标的话报告里这些场景会显示
    "未采集（探针未返回数据）"——把"本轮启动失败、根本没跑"说成"探针没回数据"，与事实不符；
    标成 ``{"skipped": "<原因>"}`` 后，报告的空单元格写的是"已跳过（本轮启动失败…）"。

    ``last_round_only`` 的场景在非末轮**本就设计为不执行**（`stress`/`idle` 等），与本轮启动
    失败无关，故不记"因启动失败而跳过"（免得把设计行为说成异常）。

    只动"场景占位 + 日志"两件事：不写 `error`（启动失败的归因在 ``launch_failure`` 里）、
    不改 `tcs_status`（异常路径已记 ``error``、未就绪路径已记 ``skipped``）。
    """
    last = round_record.launch_failure or {}
    stage = str(last.get("stage") or "startup_failed")
    note = (
        f"本轮启动失败（{stage}，{len(round_record.launch_attempts)} 次尝试全部失败），"
        "已跳过（见报告「启动失败现场」）"
    )
    by_design = set(last_round_only or ())
    for name in scenario_names:
        if not is_last_round and name in by_design:
            continue
        round_record.scenarios.setdefault(name, {"skipped": note})
    logger.warning(
        "[perf] 第 %d 轮启动失败（%s）：现场数据已记录，跳过本轮用例/静息/场景，直接进入下一轮",
        round_index + 1, stage,
    )


#: 各场景专属资源字段的后缀（来自 ``PerfRound.resources_by_scenario[name]``，即该场景
#: JS 求值窗口内的资源采样汇总）。汇入 ``_aggregate_scenarios()`` 时**必须用场景名前缀**
#: （如 ``scan_rss_mb_peak``），否则多个场景各自的 ``rss_mb_peak`` 会在下面
#: ``**scenario_fields`` 展开进顶层扁平字典时同名互相覆盖（见 ``_aggregate()``）。
#: ``pid_fallback_samples``（2026-10-08）：名称规则失效时改用已知 pid 采集的样本数，
#: 平铺成 ``<场景>_pid_fallback_samples`` 后才能进报告的场景表——否则"这段读数不是
#: 名称口径"只在 CSV 的阶段维度可见，而用户看的是场景表。
_SCENARIO_RESOURCE_SUFFIXES: tuple[str, ...] = (
    "rss_mb_avg", "rss_mb_peak", "cpu_percent_avg", "cpu_percent_peak",
    "pid_fallback_samples",
)


def _aggregate_scenarios(rounds: list[PerfRound]) -> tuple[dict[str, Any], dict[str, Optional[float]]]:
    """跨轮聚合场景指标（含该场景专属的资源采样峰值/均值）。

    Returns:
        (block, flat)：``block`` 为报告用结构（每字段含 count/min/avg/max，**真实值口径**），
        ``flat`` 为跨轮标量（方向由场景模块的 ``AGGREGATE_PICK`` 决定：
        帧率这类"越大越好"的字段取最差轮次的 ``min``，其余取 ``max``——资源类新字段未在
        任何场景的 ``AGGREGATE_PICK`` 里声明，默认走 ``max``，即"跨轮最差峰值"）。
    """
    from uita.core.perf.scenarios import aggregate_fields, aggregate_pick

    # 保序 = 预设里的**执行顺序**（dict 首次出现序）：报告按执行顺序读才顺；
    # 2026-10-13 前是字母序（``sorted``），仅影响产物键顺序，不影响任何取值。
    names = list(dict.fromkeys(name for r in rounds for name in (r.scenarios or {})))
    block: dict[str, Any] = {}
    flat: dict[str, Optional[float]] = {}
    for name in names:
        results = [r.scenarios.get(name) or {} for r in rounds]
        resource_summaries = [r.resources_by_scenario.get(name) or {} for r in rounds]
        pick = aggregate_pick(name)
        # 以「空结果的字段集」为键全集：即使本轮全部失败，扁平字段也必须存在（记 None），
        # 这样基线判定能区分"未测"与"没这个字段"。
        field_keys = set(aggregate_fields(name, {}).keys())
        field_keys |= {f"{name}_{suffix}" for suffix in _SCENARIO_RESOURCE_SUFFIXES}
        collected: dict[str, list[float]] = {}
        for res in results:
            for key, value in aggregate_fields(name, res).items():
                field_keys.add(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    collected.setdefault(key, []).append(float(value))
        for rs in resource_summaries:
            for suffix in _SCENARIO_RESOURCE_SUFFIXES:
                value = rs.get(suffix)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    collected.setdefault(f"{name}_{suffix}", []).append(float(value))
        entry: dict[str, Any] = {
            "rounds": len(results),
            "unsupported": any(res.get("unsupported") for res in results),
            "unsupported_reason": next(
                (res.get("unsupported") for res in results if res.get("unsupported")), None,
            ),
            "errors": [res.get("error") for res in results if res.get("error")],
            "raw": results,
        }
        for key in sorted(field_keys):
            values = collected.get(key) or []
            entry[key] = summarize(values)
            if values:
                flat[key] = min(values) if pick.get(key) == "min" else max(values)
            else:
                flat[key] = None
        block[name] = entry
    return block, flat


def _aggregate(report: PerfCaseReport) -> dict[str, Any]:
    """跨轮聚合。缺失指标记 ``None``（不记 0）。"""
    rounds = report.rounds
    launch = summarize([r.launch_ms for r in rounds if r.launch_ms is not None])
    startup = summarize([r.startup_ms for r in rounds if r.startup_ms is not None])
    durations = summarize([r.case_duration_ms for r in rounds if r.case_duration_ms])
    step_all: list[float] = []
    for r in rounds:
        step_all.extend(r.step_durations_ms)

    def pick(metric_key: str, flag: str) -> tuple[Optional[float], int]:
        best_value: Optional[float] = None
        best_round = 0
        for r in rounds:
            v = r.resources.get(metric_key)
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            if best_value is None or (flag == "peak" and v > best_value) or (flag == "min" and v < best_value):
                best_value, best_round = float(v), r.round_index
        return best_value, best_round

    rss_avg = summarize([r.resources.get("rss_mb_avg") for r in rounds])
    cpu_avg = summarize([r.resources.get("cpu_percent_avg") for r in rounds])
    rss_peak, rss_peak_round = pick("rss_mb_peak", "peak")
    cpu_peak, cpu_peak_round = pick("cpu_percent_peak", "peak")
    thread_peak, thread_peak_round = pick("thread_count_peak", "peak")
    fds_peak, fds_peak_round = pick("num_fds_peak", "peak")

    tcs_failed_rounds = [r.round_index for r in rounds if r.tcs_status == "failed"]
    error_rounds = [r.round_index for r in rounds if r.error]

    # 阶段切片聚合：启动期（场景 1）/执行期/静息期（场景 2 的稳态判据靠 rss 斜率）
    startup_phases = [r.resources_by_phase.get("startup", {}) for r in rounds]
    case_phases = [r.resources_by_phase.get("case", {}) for r in rounds]
    idle_phases = [r.resources_by_phase.get("idle", {}) for r in rounds]

    def _phase_block(phases: list[dict[str, Any]]) -> dict[str, Any]:
        rss_peak = summarize([p.get("rss_mb_peak") for p in phases])
        rss_avg = summarize([p.get("rss_mb_avg") for p in phases])
        cpu_peak = summarize([p.get("cpu_percent_peak") for p in phases])
        cpu_avg = summarize([p.get("cpu_percent_avg") for p in phases])
        slopes = [p.get("rss_slope_mb_per_min") for p in phases]
        slopes = [s for s in slopes if isinstance(s, (int, float)) and not isinstance(s, bool)]
        return {
            "rss_mb_avg": rss_avg["avg"],
            "rss_mb_peak": rss_peak["max"],
            "cpu_percent_avg": cpu_avg["avg"],
            "cpu_percent_peak": cpu_peak["max"],
            "rss_slope_mb_per_min": max(slopes) if slopes else None,
            "sample_count": sum(int(p.get("sample_count") or 0) for p in phases),
            "error_samples": sum(int(p.get("error_samples") or 0) for p in phases),
        }

    startup_block = _phase_block(startup_phases)
    case_block = _phase_block(case_phases)
    idle_block = _phase_block(idle_phases)
    scenario_phases = [r.resources_by_phase.get("scenario", {}) for r in rounds]
    scenario_block = _phase_block(scenario_phases)
    scenarios_agg, scenario_fields = _aggregate_scenarios(rounds)

    return {
        "rounds": len(rounds),
        "resources_by_phase": {
            "startup": startup_block, "case": case_block, "idle": idle_block, "scenario": scenario_block,
        },
        "scenario_rss_mb_peak": scenario_block["rss_mb_peak"],
        "scenario_cpu_percent_peak": scenario_block["cpu_percent_peak"],
        "scenarios": scenarios_agg,
        **scenario_fields,
        "startup_rss_mb_avg": startup_block["rss_mb_avg"],
        "startup_rss_mb_peak": startup_block["rss_mb_peak"],
        "startup_cpu_percent_avg": startup_block["cpu_percent_avg"],
        "startup_cpu_percent_peak": startup_block["cpu_percent_peak"],
        # 静息期：应用什么都不干时的资源占用基线，用于区分"泄漏是场景操作导致的，
        # 还是即使空闲也在增长"。**两套机制共用这组扁平字段**（历史沿革）：
        #   A) idle 阶段 = `--perf-steady-state-s` 静置窗口（`resources_by_phase.idle`）；
        #   B) idle 场景 = `--perf-scenario idle`（`resources_by_scenario.idle`，经
        #      `_aggregate_scenarios` 加场景名前缀后恰好同名）。
        # 必须 `_first_not_none` 回落（而不是直接取 A）：A 未启用时其值为 None，而本 dict 里
        # `**scenario_fields` 在前、这几行在后 → 直接取 A 会把 B 真实采到的数据**覆盖成 null**
        # （真实缺陷：默认不传 `--perf-steady-state-s` 时，idle 场景数据完全不进基线判定与报告顶层）。
        # 优先级保持 A > B：阶段窗口是纯静置语义，且既有基线阈值/口径以它为准。
        "idle_rss_mb_avg": _first_not_none(
            idle_block["rss_mb_avg"], scenario_fields.get("idle_rss_mb_avg"),
        ),
        "idle_rss_mb_peak": _first_not_none(
            idle_block["rss_mb_peak"], scenario_fields.get("idle_rss_mb_peak"),
        ),
        "idle_cpu_percent_avg": _first_not_none(
            idle_block["cpu_percent_avg"], scenario_fields.get("idle_cpu_percent_avg"),
        ),
        "idle_cpu_percent_peak": _first_not_none(
            idle_block["cpu_percent_peak"], scenario_fields.get("idle_cpu_percent_peak"),
        ),
        # 稳态漂移：优先静息期窗口（`--perf-steady-state-s` 真正"什么都不干"的窗口才是
        # 干净的稳态语义，不会混入场景测量本身的内存分配噪声），其次场景期/执行期，
        # 最后全轮兜底。短窗口在 summarize_samples 里已被强制置 None。
        "rss_slope_mb_per_min": _first_not_none(
            idle_block["rss_slope_mb_per_min"],
            scenario_block["rss_slope_mb_per_min"],
            case_block["rss_slope_mb_per_min"],
            _phase_block([r.resources for r in rounds])["rss_slope_mb_per_min"],
        ),
        "launch_ms": launch,
        "startup_ms": startup,
        "case_duration_ms": durations,
        "step_duration_ms": summarize(step_all),
        "rss_mb_avg": rss_avg["avg"],
        "rss_mb_peak": rss_peak,
        "rss_mb_peak_round": rss_peak_round,
        "cpu_percent_avg": cpu_avg["avg"],
        "cpu_percent_peak": cpu_peak,
        "cpu_percent_peak_round": cpu_peak_round,
        "thread_count_peak": thread_peak,
        "thread_count_peak_round": thread_peak_round,
        "num_fds_peak": fds_peak,
        "num_fds_peak_round": fds_peak_round,
        # 派生统计（cv / round_cv_max）已于 2026-10-12 移除：用户要求"统计数据尽量简单"，
        # 且 cv 是二次加工值而非实测值；同一波动看逐轮明细即可（cases[].rounds[]）。
        "tcs_failed_rounds": tcs_failed_rounds,
        "error_rounds": error_rounds,
    }


#: 判定断开后的固定结论基底（见 :func:`_disabled_verdict`）。
_DISABLED_VERDICT: dict[str, Any] = {
    "status": "unknown",
    "gate": "disabled",
    "reason": "真实值口径：不自动判定（2026-10-08）",
    "mode": "off",
    "source": "",
    "violations": [],
    "checked": [],
    "unsupported": [],
    "only_keys": None,
}


def _disabled_verdict(baseline: PerfBaseline) -> dict[str, Any]:
    """未启用判定时的 ``verdict``（真实值口径）。

    仍写全字段（而不是删掉 verdict）的三个理由：① 产物 schema 与报告渲染保持稳定；
    ② ``_propagate_verdict()`` 无需加分支；③ ``source`` 仍能看出"本可用的基线文件是哪个"
    ——``load_baseline()`` 一直在跑（档位推断 ``available_tags()`` 依赖它）。

    重开门禁的方式：把 ``run()`` 里本函数调用换回
    ``evaluate(report.aggregate, baseline, only_keys=...)``（判定实现与单测都还在）。
    """
    return {**_DISABLED_VERDICT, "source": baseline.source}


def _propagate_verdict(report: PerfCaseReport) -> list[tuple[PerfRound, str]]:
    """把批次结论写回各轮（判定断开后一律 ``unknown``；重开门禁时按 verdict 回写）。"""
    status = report.verdict.get("status", "unknown")
    violations = report.verdict.get("violations", [])
    out: list[tuple[PerfRound, str]] = []
    for r in report.rounds:
        r.perf_violations = violations
        out.append((r, status))
    return out


def _inject_perf_section(result_json: str, perf: dict[str, Any]) -> None:
    """读回 result.json → 追加 ``perf`` 段 → 原样重写（既有键零变化）。

    先例：``core/runner/batch_runner.py`` 同样在写盘后二次修改 ``result.json``。
    """
    try:
        with open(result_json, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("[perf] 注入 perf 段失败（读取 %s）: %s", result_json, exc)
        return
    if not isinstance(data, dict):
        logger.warning("[perf] result.json 顶层不是对象，跳过 perf 注入: %s", result_json)
        return
    data["perf"] = perf
    try:
        with open(result_json, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        logger.warning("[perf] 写入 perf 段失败: %s", exc)


def _app_path(app_name: str) -> str:
    try:
        from uita.config import get_app_path

        return get_app_path(app_name) or ""
    except Exception:  # noqa: BLE001
        return ""


def _git_rev() -> str:
    """当前仓库 git revision（取不到则为空串，不阻断执行）。"""
    try:
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


async def run_perf(**kwargs: Any) -> PerfRunResult:
    """便捷入口（供 CLI/测试调用）。"""
    return await PerfRunner(**kwargs).run()
