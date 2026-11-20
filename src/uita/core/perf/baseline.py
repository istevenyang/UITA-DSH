"""性能基线加载与阈值判定。

**当前状态（2026-10-08）：判定能力保留，但已从 perf 流程中**断开接入**。**
用户明确要求"不要门禁、不要 Pxx、只要真实值"，故 ``runner`` 不再调用
:func:`evaluate`（``verdict`` 固定 ``unknown`` + ``gate=disabled``），聚合产物也不再有
``p50/p95`` 字段（见 ``core/perf/metrics.py`` docstring）。本模块**刻意不删除**：
``load_baseline()`` 仍被用于读取档位名（``available_tags()`` → ``--perf-baseline-tag``
的档位推断）与产物元信息，``evaluate()`` 的实现在未来要重开门禁时可直接接回。

加载优先级（遵循 R5「app 优先、全局兜底」）：
    1. ``--perf-baseline <file>``（显式指定，最高优先级）
    2. ``apps/<app>/perf_baseline.json``（App 级）
    3. ``settings.PERF_BASELINE_PATH``（全局兜底，可经 ``UITA_PERF_BASELINE`` 覆盖）

判定语义（若将来重新接入；关键约束：**不得改写用例通过率**）：
    | 情形                        | perf_status | 是否影响用例结果 |
    |-----------------------------|-------------|------------------|
    | 未提供基线 / 阈值项缺失      | ``unknown`` | 否（仅记录指标） |
    | 全部指标在阈值内            | ``pass``    | 否               |
    | 超阈值且 ``mode=warn``      | ``warn``    | 否（报告标黄）   |
    | 超阈值且 ``mode=strict``    | ``fail``    | 否（仅标 perf_failed） |
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("uita")

__all__ = [
    "PerfBaseline",
    "available_tags",
    "evaluate",
    "load_baseline",
    "THRESHOLD_RESOLVERS",
]

# 阈值键 → (聚合指标键, 聚合字段)。未知键不参与判定，并计入 unsupported（不静默忽略）。
# 注：``_p50/_p95`` 变体已于 2026-10-08 移除——聚合产物不再产出百分位字段
# （``metrics.summarize()`` 只给 count/min/avg/max），留着会出现"键存在但取不到值"的
# 脏状态；只保留真实值口径的 ``_max``（最差一轮实测）与标量键。
THRESHOLD_RESOLVERS: dict[str, tuple[str, str]] = {
    "launch_ms_min": ("launch_ms", "min"),
    "launch_ms_avg": ("launch_ms", "avg"),
    "launch_ms_max": ("launch_ms", "max"),
    "startup_ms_min": ("startup_ms", "min"),
    "startup_ms_avg": ("startup_ms", "avg"),
    "startup_ms_max": ("startup_ms", "max"),
    "case_duration_ms_min": ("case_duration_ms", "min"),
    "case_duration_ms_avg": ("case_duration_ms", "avg"),
    "case_duration_ms_max": ("case_duration_ms", "max"),
    "rss_mb_avg": ("rss_mb_avg", "value"),
    "rss_mb_peak": ("rss_mb_peak", "value"),
    "cpu_percent_avg": ("cpu_percent_avg", "value"),
    "cpu_percent_peak": ("cpu_percent_peak", "value"),
    "thread_count_peak": ("thread_count_peak", "value"),
    "num_fds_peak": ("num_fds_peak", "value"),
    # 注：``round_cv_max`` 已于 2026-10-12 移除（聚合产物不再产出 cv 派生统计，
    # 见 ``core/perf/metrics.py`` docstring），同 2026-10-08 移除百分位变体的理由。
    # 场景 1：启动期资源（真机冷启动 CPU/内存），由阶段切片产出
    "startup_rss_mb_avg": ("startup_rss_mb_avg", "value"),
    "startup_rss_mb_peak": ("startup_rss_mb_peak", "value"),
    "startup_cpu_percent_avg": ("startup_cpu_percent_avg", "value"),
    "startup_cpu_percent_peak": ("startup_cpu_percent_peak", "value"),
    # 场景 2：静息期资源（--perf-steady-state-s 静置窗口，"什么都不干"时的资源占用基线）
    "idle_rss_mb_avg": ("idle_rss_mb_avg", "value"),
    "idle_rss_mb_peak": ("idle_rss_mb_peak", "value"),
    "idle_cpu_percent_avg": ("idle_cpu_percent_avg", "value"),
    "idle_cpu_percent_peak": ("idle_cpu_percent_peak", "value"),
    # 稳态漂移（MB/min，越小越好）
    "rss_slope_mb_per_min": ("rss_slope_mb_per_min", "value"),
    # 场景 3：滑动帧率 / 掉帧 / 最长帧；场景期 CPU/RSS 峰值
    "fps_avg_fps_min": ("fps_avg_fps", "value"),
    "fps_low_fps_ratio_pct": ("fps_low_fps_ratio_pct", "value"),
    "fps_longest_frame_ms": ("fps_longest_frame_ms", "value"),
    "fps_frame_ms_avg": ("fps_frame_ms_avg", "value"),
    # 渲染有效性：滑动期可见卡片"缩略图已显示"的最低占比（越大越好）
    "fps_thumb_pct_min": ("fps_thumb_pct_min", "value"),
    "scenario_rss_mb_peak": ("scenario_rss_mb_peak", "value"),
    "scenario_cpu_percent_peak": ("scenario_cpu_percent_peak", "value"),
    # 场景 4 目录视图 / 5 缩略图 / 7 全局视图 / 8 搜索 / 9 筛选 / 10 扫描
    "dir_data_ready_ms": ("dir_data_ready_ms", "value"),
    "dir_covers_settled_ms": ("dir_covers_settled_ms", "value"),
    "dir_read_dir_ms": ("dir_read_dir_ms", "value"),
    "thumb_cold_ms": ("thumb_cold_ms", "value"),
    "thumb_cold_max_ms": ("thumb_cold_max_ms", "value"),
    "thumb_warm_ms": ("thumb_warm_ms", "value"),
    "gallery_first_img_ms": ("gallery_first_img_ms", "value"),
    "gallery_imgs_40_ms": ("gallery_imgs_40_ms", "value"),
    "gallery_settle_ms": ("gallery_settle_ms", "value"),
    "search_ipc_ms": ("search_ipc_ms", "value"),
    "search_e2e_first_img_ms": ("search_e2e_first_img_ms", "value"),
    "search_e2e_settle_ms": ("search_e2e_settle_ms", "value"),
    "filter_type_switch_ms": ("filter_type_switch_ms", "value"),
    "filter_source_switch_ms": ("filter_source_switch_ms", "value"),
    "filter_type_settle_ms": ("filter_type_settle_ms", "value"),
    "filter_source_settle_ms": ("filter_source_settle_ms", "value"),
    "scan_duration_ms": ("scan_duration_ms", "value"),
    "scan_time_to_target_ms": ("scan_time_to_target_ms", "value"),
    # 扫描过程专属资源窗口（该场景 JS 求值区间内的资源采样峰值/均值）
    "scan_rss_mb_avg": ("scan_rss_mb_avg", "value"),
    "scan_rss_mb_peak": ("scan_rss_mb_peak", "value"),
    "scan_cpu_percent_avg": ("scan_cpu_percent_avg", "value"),
    "scan_cpu_percent_peak": ("scan_cpu_percent_peak", "value"),
    "video_decode_cold_ms": ("video_decode_cold_ms", "value"),
    "video_decode_cold_max_ms": ("video_decode_cold_max_ms", "value"),
    "video_decode_warm_ms": ("video_decode_warm_ms", "value"),
    # 场景 6 预览点击 / 12 视频播放（跨窗口端到端）
    "preview_viewer_ms": ("preview_viewer_ms", "value"),
    "preview_ready_ms": ("preview_ready_ms", "value"),
    "preview_cards_ready_ms": ("preview_cards_ready_ms", "value"),
    "video_play_viewer_ms": ("video_play_viewer_ms", "value"),
    "video_play_ready_ms": ("video_play_ready_ms", "value"),
    "video_play_cards_ready_ms": ("video_play_cards_ready_ms", "value"),
    # 「产出了但判不了」的兜底：以下指标各场景已产出，若不在本表内，
    # 基线里写了也会被判 unsupported（=形同虚设）。实测被
    # tests/core/perf/test_baseline.py 的基线一致性用例抓出 scan_db_total_mb 缺失。
    "scan_indexed": ("scan_indexed", "value"),
    "scan_db_total_mb": ("scan_db_total_mb", "value"),
    "dir_cards": ("dir_cards", "value"),
    "thumb_bytes": ("thumb_bytes", "value"),
    "gallery_final_cards": ("gallery_final_cards", "value"),
    "gallery_final_imgs": ("gallery_final_imgs", "value"),
    "search_ipc_hits": ("search_ipc_hits", "value"),
    "filter_mutations": ("filter_mutations", "value"),
    "filter_childlist_mutations": ("filter_childlist_mutations", "value"),
    "preview_image_w": ("preview_image_w", "value"),
    "preview_image_h": ("preview_image_h", "value"),
    "video_play_ready_state": ("video_play_ready_state", "value"),
    # 压测 stress（2026-10-13 口径）：先挤占系统内存/CPU → 逐级试启动示例媒体应用 → 直到启动失败；
    # ``stress_load_mb`` = **实际累计注入量**（停止时的水位，不再冒充失败水位），
    # ``stress_fail_at_mb`` / ``stress_fail_available_mb`` = **启动失败瞬间**的注入量与系统**剩余可用**内存，
    # ``stress_system_mem_used_mb`` / ``stress_system_mem_percent`` = 失败瞬间的"已占用 / 占比"；
    # 未观测到启动失败（如 ``app_stop_failed``）时上述失败类字段为空、``stress_start_failed``=0。
    # 这些键当前多用于产物/报告展示，登记在案以免基线里写了却被判 unsupported。
    "stress_load_mb": ("stress_load_mb", "value"),
    "stress_fail_at_mb": ("stress_fail_at_mb", "value"),
    "stress_fail_available_mb": ("stress_fail_available_mb", "value"),
    "stress_last_ok_available_mb": ("stress_last_ok_available_mb", "value"),
    "stress_stop_available_mb": ("stress_stop_available_mb", "value"),
    "stress_start_failed": ("stress_start_failed", "value"),
    # 防压缩/防释放是否真生效（0 = 没锁住任何页 → 占用仍可能被压缩/换出）
    "stress_locked_mb": ("stress_locked_mb", "value"),
    "stress_last_ok_mb": ("stress_last_ok_mb", "value"),
    "stress_critical_gap_mb": ("stress_critical_gap_mb", "value"),
    "stress_start_attempts": ("stress_start_attempts", "value"),
    "stress_resident_ratio_min": ("stress_resident_ratio_min", "value"),
    "stress_elapsed_ms": ("stress_elapsed_ms", "value"),
    "stress_system_mem_used_mb": ("stress_system_mem_used_mb", "value"),
    "stress_system_mem_percent": ("stress_system_mem_percent", "value"),
    "stress_system_cpu_percent": ("stress_system_cpu_percent", "value"),
    "stress_log_files": ("stress_log_files", "value"),
    # 四轮加固：启动运行最少所需内存结论（见 apps/sample_media/perf/scenarios/stress.py
    # ::_derive_min_required）。登记在案以免基线里写了却被判 unsupported；
    # 越大越好/越小越好皆不绝对（取决于想保留多少余量），暂不归入 LOWER_BOUND_KEYS，
    # 默认上界语义即可（基线写的是"预期不超过多少"时才有意义，当前未真正接入门禁）。
    "stress_min_required_available_mb": ("stress_min_required_available_mb", "value"),
    "stress_min_required_total_mb": ("stress_min_required_total_mb", "value"),
    # 五轮加固：无法渲染页面时示例媒体应用**自身**内存/CPU 占用（见
    # apps/sample_media/perf/scenarios/stress.py 顶部文档"每次启动记录示例媒体应用自身内存/CPU 峰值"）。
    "stress_fail_app_rss_mb": ("stress_fail_app_rss_mb", "value"),
    "stress_fail_app_cpu_percent": ("stress_fail_app_cpu_percent", "value"),
    "stress_last_ok_app_rss_mb": ("stress_last_ok_app_rss_mb", "value"),
    "stress_last_ok_app_cpu_percent": ("stress_last_ok_app_cpu_percent", "value"),
}

# 这些键为「下界」语义（值 ≥ 阈值才通过），其余默认上界（值 ≤ 阈值通过）
LOWER_BOUND_KEYS: frozenset[str] = frozenset({
    "fps_avg_fps_min",
    # 规模/尺寸/就绪态类：命中数、卡片数、索引数、解码尺寸、readyState 都是「越大越好」
    "scan_indexed",
    "dir_cards",
    "gallery_final_cards",
    "gallery_final_imgs",
    "search_ipc_hits",
    "preview_image_w",
    "preview_image_h",
    "video_play_ready_state",
    # 压测驻留自证：驻留比越大越好（<0.9 说明占用被系统释放/压缩过）
    "stress_resident_ratio_min",
})


@dataclass
class PerfBaseline:
    """一份性能基线（含来源与加载状态，便于报告溯源）。"""

    source: str = ""
    loaded: bool = False
    app: str = ""
    platform: str = ""
    schema_version: str = "1.0"
    mode: str = "warn"
    thresholds: dict[str, float] = field(default_factory=dict)
    error: str = ""
    #: 生效的档位名（``--perf-baseline-tag``）；空串 = 只用顶层 ``thresholds``
    tag: str = ""
    #: 该档位实际覆盖了哪些键（用于报告里区分"按档判定"与"用通用阈值"）
    tag_overrides: list[str] = field(default_factory=list)
    #: 请求了档位但基线文件里没有该档 → 记原因，避免"以为按档判定却用了通用阈值"
    tag_warning: str = ""
    #: 请求判定的平台（``macos`` / ``windows``）
    requested_platform: str = ""
    #: 基线文件声明的平台与请求平台不一致时的说明（跨平台误用阈值是可观测风险）
    platform_warning: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "loaded": self.loaded,
            "app": self.app,
            "platform": self.platform,
            "schema_version": self.schema_version,
            "mode": self.mode,
            "thresholds": dict(self.thresholds),
            "error": self.error,
            "tag": self.tag,
            "tag_overrides": list(self.tag_overrides),
            "tag_warning": self.tag_warning,
            "requested_platform": self.requested_platform,
            "platform_warning": self.platform_warning,
        }


def _app_baseline_path(app_name: str, *, platform: str = "") -> str:
    """app 级基线路径；给了 platform 时优先平台专属文件。

    平台专属文件命名：``perf_baseline.<platform>.json``（如 ``perf_baseline.windows.json``）。
    **为什么必须支持**：性能阈值受平台影响极大（启动耗时、RSS 口径、进程模型都不同），
    用 macOS 校准的阈值判定 Windows 会得到系统性错判。缺平台专属文件时退回通用文件，
    并在产物里保留 ``platform_warning``（若通用文件声明了别的平台）。
    """
    from uita.config import WORKSPACE_DIR

    if not app_name:
        return ""
    base = os.path.join(WORKSPACE_DIR, "apps", app_name)
    if platform:
        specific = os.path.join(base, f"perf_baseline.{platform.lower()}.json")
        if os.path.isfile(specific):
            return specific
    return os.path.join(base, "perf_baseline.json")


def _platform_variant(path: str, platform: str) -> str:
    """``/x/perf_baseline.json`` + ``windows`` → ``/x/perf_baseline.windows.json``（不存在则原路径）。"""
    if not path or not platform:
        return path
    root, ext = os.path.splitext(path)
    if not ext:
        return path
    candidate = f"{root}.{platform.lower()}{ext}"
    return candidate if os.path.isfile(candidate) else path


def _global_baseline_path() -> str:
    try:
        from uita.config import PERF_BASELINE_PATH

        return PERF_BASELINE_PATH or ""
    except ImportError:  # pragma: no cover - config 始终可用
        return ""


def available_tags(app_name: str, *, platform: str = "", explicit_path: str = "") -> list[str]:
    """列出基线文件里已定义的档位名（``tagged_thresholds`` 的键）。

    用途：``--perf-tier`` 省略时按 ``--perf-lib`` 目录名自动匹配档位
    （如目录 ``/tmp/demo-lib-10k`` → 档位 ``lib-10k``）；也用于报错时提示可用档位。
    """
    platform_key = (platform or "").strip().lower()
    candidates = []
    if explicit_path:
        candidates.append(explicit_path)
        candidates.append(_platform_variant(explicit_path, platform_key))
    candidates.append(_app_baseline_path(app_name, platform=platform_key))
    global_path = _global_baseline_path()
    if global_path:
        candidates.append(global_path)
    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        tagged = data.get("tagged_thresholds") if isinstance(data, dict) else None
        if isinstance(tagged, dict):
            return sorted(str(k) for k in tagged)
        return []
    return []


def _parse_thresholds(raw: Any) -> dict[str, float]:
    thresholds: dict[str, float] = {}
    if not isinstance(raw, dict):
        return thresholds
    for k, v in raw.items():
        if isinstance(v, bool) or v is None:
            continue
        try:
            thresholds[str(k)] = float(v)
        except (TypeError, ValueError):
            logger.warning("[perf] 阈值 %s 非数值，已忽略: %r", k, v)
    return thresholds


def load_baseline(
    app_name: str, *, explicit_path: str = "", tag: str = "", platform: str = "",
) -> PerfBaseline:
    """按优先级加载基线；任何失败都返回 ``loaded=False`` 的基线（不抛异常）。

    Args:
        app_name: app 名（决定 ``apps/<app>/perf_baseline.json``）
        explicit_path: 显式基线文件（最高优先级）
        tag: 档位名（``tagged_thresholds.<tag>``）。**为什么需要档位**：部分指标与输入规模
            强相关——实测 ``scan_duration_ms`` 在 25 项 vs 5 万项下相差 **19 倍**
            （755ms → 14286ms），用单一阈值必然要么误报要么形同虚设。
            档位内的键**覆盖**同名通用键；未提供的档位会记 ``tag_warning``（不静默退回通用阈值）。
        platform: 宿主机平台（``macos`` / ``windows``）。给了则优先加载
            ``perf_baseline.<platform>.json``；若最终加载的文件声明了**别的**平台，
            会在 ``platform_warning`` 中说明（不静默按错平台的阈值判定）。
    """
    platform_key = (platform or "").strip().lower()
    candidates: list[str] = []
    if explicit_path:
        candidates.append(_platform_variant(explicit_path, platform_key))
        candidates.append(explicit_path)
    candidates.append(_app_baseline_path(app_name, platform=platform_key))
    global_path = _global_baseline_path()
    if global_path:
        candidates.append(_platform_variant(global_path, platform_key))
        candidates.append(global_path)

    for path in candidates:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("[perf] 基线文件解析失败，继续尝试下一个候选: %s (%s)", path, exc)
            return PerfBaseline(source=path, loaded=False, error=f"解析失败: {exc}")
        if not isinstance(data, dict):
            return PerfBaseline(source=path, loaded=False, error="基线文件顶层不是 JSON 对象")
        thresholds = _parse_thresholds(data.get("thresholds"))
        tag_overrides: list[str] = []
        tag_warning = ""
        if tag:
            tagged = data.get("tagged_thresholds")
            tag_block = tagged.get(tag) if isinstance(tagged, dict) else None
            if isinstance(tag_block, dict):
                overrides = _parse_thresholds(tag_block)
                tag_overrides = sorted(overrides)
                thresholds.update(overrides)
            else:
                known = sorted(tagged) if isinstance(tagged, dict) else []
                tag_warning = (
                    f"基线未定义档位 {tag!r}"
                    + (f"（已有: {', '.join(known)}）" if known else "（文件无 tagged_thresholds）")
                    + "，已退回通用阈值"
                )
                logger.warning("[perf] %s", tag_warning)
        mode = str(data.get("mode") or "warn").strip().lower()
        if mode not in ("warn", "strict"):
            mode = "warn"
        declared_platform = str(data.get("platform") or "")
        platform_warning = ""
        if platform_key and declared_platform and declared_platform.strip().lower() != platform_key:
            platform_warning = (
                f"当前平台 {platform_key}，但基线文件 {os.path.basename(path)} 声明平台 "
                f"{declared_platform}（阈值可能不适用；建议提供 perf_baseline.{platform_key}.json）"
            )
            logger.warning("[perf] %s", platform_warning)
        return PerfBaseline(
            source=path,
            loaded=True,
            app=str(data.get("app") or ""),
            platform=declared_platform,
            schema_version=str(data.get("schema_version") or "1.0"),
            mode=mode,
            thresholds=thresholds,
            tag=tag,
            tag_overrides=tag_overrides,
            tag_warning=tag_warning,
            requested_platform=platform_key,
            platform_warning=platform_warning,
        )

    return PerfBaseline(source="", loaded=False, error="未找到基线文件（app 级与全局兜底均不存在）")


def evaluate(
    aggregate: dict[str, Any],
    baseline: PerfBaseline,
    *,
    only_keys: Optional[list[str]] = None,
) -> dict[str, Any]:
    """按基线阈值判定聚合指标。

    Args:
        aggregate: 扁平聚合指标，形如
            ``{"startup_ms": {"count": 3, "min": 1200.0, "avg": 1980.5, "max": 3560.2},
            "rss_mb_peak": 508.9}``
        baseline:  ``load_baseline()`` 的返回值
        only_keys: 仅判定这些阈值键（``--perf-metrics`` 过滤）；``None`` = 全量。
            被过滤掉的键既不判定也不计入 unsupported（属用户显式选择）。

    Returns:
        ``{"status": "unknown"|"pass"|"warn"|"fail", "violations": [...], "checked": [...],
        "unsupported": [...], "mode": "warn", "source": "..."}``
    """
    result: dict[str, Any] = {
        "status": "unknown",
        "mode": baseline.mode,
        "source": baseline.source,
        "violations": [],
        "checked": [],
        "unsupported": [],
        "only_keys": list(only_keys) if only_keys else None,
    }
    if not baseline.loaded or not baseline.thresholds:
        return result

    allowed = set(only_keys) if only_keys else None
    for key, limit in sorted(baseline.thresholds.items()):
        if allowed is not None and key not in allowed:
            continue
        resolver = THRESHOLD_RESOLVERS.get(key)
        if resolver is None:
            result["unsupported"].append({"key": key, "reason": "未知阈值键（不在 THRESHOLD_RESOLVERS 内）"})
            continue
        metric_key, agg_field = resolver
        metric = aggregate.get(metric_key)
        value: Optional[float] = None
        if isinstance(metric, dict):
            raw = metric.get(agg_field)
            value = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
        elif isinstance(metric, (int, float)) and not isinstance(metric, bool):
            value = float(metric)
        if value is None:
            result["unsupported"].append({"key": key, "reason": f"指标 {metric_key} 缺失（可能平台不支持）"})
            continue
        lower_bound = key in LOWER_BOUND_KEYS  # 帧率这类"越大越好"的指标
        passed = value >= limit if lower_bound else value <= limit
        item = {
            "key": key, "metric": metric_key, "value": value, "limit": limit,
            "direction": "min" if lower_bound else "max", "passed": passed,
        }
        result["checked"].append(item)
        if not item["passed"]:
            result["violations"].append(item)

    if not result["checked"]:
        result["status"] = "unknown"
    elif result["violations"]:
        result["status"] = "fail" if baseline.mode == "strict" else "warn"
    else:
        result["status"] = "pass"
    return result
