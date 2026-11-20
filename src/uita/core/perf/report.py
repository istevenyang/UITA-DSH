"""perf 产物落盘：``perf_summary.json`` / ``perf_report.html`` + 上报 payload 预留。

产物约定（与计划文档 §4 一致）::

    <REPORT_DIR>/perf/perf_<YYYYMMDD-HHmmss>/
    ├── <case>.tcs.result/result.json     # 复用既有约定；新增顶层 "perf" 段
    ├── perf_summary.json                 # 批次级：各用例 × 各轮指标 + 聚合 + 基线对比（**完整字段**）
    ├── perf_metrics.csv                  # 长表格聚合统计（跨平台/跨机器拼接对比用，2026-09-21）
    └── perf_report.html                  # 单文件报告（内联 CSS，无外部依赖）

**多单元聚合**（``--perf-tier`` 未指定但给了 ``--perf-src`` 时按**单元序列**依次执行；序列由 app 侧
``UNIT_SEQUENCE`` 声明——sample_media 为 ``0k → 5k → 10k → 50k → stress``，``stress`` 独立成单元并以
10K 规模库为环境；app 未声明时回落 ``0k/5k/10k/50k``。见
``docs/plans/done/archive_202610.md`` 与
``docs/plans/archive/10-platform-scale-实施纪要.md``）::

    <REPORT_DIR>/perf/perf_<ts>/
    ├── tier-0k/perf_summary.json / perf_report.html / perf_metrics.csv  # 单单元完整产物（结构同上）
    ├── tier-5k/...   tier-10k/...   tier-50k/...   tier-stress/...
    ├── perf_summary.json                                 # 顶层聚合：各单元 to_dict() + host
    ├── perf_metrics.csv                                   # 顶层聚合：各单元长表格拼接（tier 列区分）
    └── perf_report.html                                  # 顶层聚合：总体健康度 + 分场景关键指标（行=指标/列=档位）；
                                                          #   列集按各单元声明执行的场景裁剪（见 _scenario_multi）

**HTML 报告体例（2026-10-13 用户口径：结论优先）**：
    前台只四块 —— 读表约定 → 总体健康度（6 行）→ 分场景关键指标（每场景白名单 + 1 行资源）→ 附注；
    逐轮明细 / 阶段表 / 全部字段 / 主机基准 / 指标词典**不再进 HTML**（原始数据查
    ``perf_summary.json`` / ``perf_metrics.csv``）。各场景前台展示哪些指标、中文叫什么，
    由 app 侧场景模块的 ``REPORT_KEYS`` / ``SCENARIO_LABEL`` 声明（应用知识住 app，core 只认机制）。
"""
from __future__ import annotations

import csv
import html
import json
import logging
import os
import re
import statistics
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("uita")

__all__ = [
    "build_ingest_payload",
    "render_html",
    "render_multi_tier_html",
    "write_merged_metrics_csv",
    "write_metrics_csv",
    "write_multi_tier_summary",
    "write_summary",
]

#: ``write_metrics_csv``/``write_merged_metrics_csv`` 的固定表头（长表格/tidy format，
#: 与具体场景/字段无关）——用于把不同平台/机器跑出的结果按固定字段拼接对比
#: （2026-09-21 用户要求"方便把 Windows 和 macOS 数据聚合到一起"）。
CSV_FIELDNAMES: tuple[str, ...] = (
    "platform", "app", "tier", "run_id", "started_at", "git_rev",
    "case_name", "metric", "stat", "value", "unit",
)

#: ``summarize()``（core/perf/metrics.py）输出的**真实值口径**统计字段名，按此顺序展开成多行。
#: 2026-10-08 起不含百分位（``median/p50/p95``）——用户要求"不要 Pxx、只要真实值"，
#: 理由见 ``core/perf/metrics.py`` 模块 docstring（百分位是插值合成值，不对应任何一次实测）。
_STAT_KEY_ORDER: tuple[str, ...] = ("count", "min", "avg", "max")

_STATUS_COLOR = {
    "pass": "#137333",
    "warn": "#b06000",
    "fail": "#c5221f",
    "unknown": "#5f6368",
}
_STATUS_TEXT = {"pass": "通过", "warn": "告警", "fail": "失败", "unknown": "未知"}

#: 多档聚合产物的 schema 版本（与单档 ``PERF_SCHEMA_VERSION`` 分开维护，避免跨模块循环 import；
#: report.py 位于 core/perf 内部但不在模块顶层依赖 runner.py，保持 R1 内部依赖顺序清晰）
_MULTI_TIER_SCHEMA_VERSION = "1.0"

#: "前置准备判定不可启动（一次启动都没发起）"的 ``launch_failure.stage`` 值。
#: 与 ``runner.py`` 里写入的值保持一致；报告据此避免把"没启动"写成"N 次尝试全部失败"。
PRE_ROUND_ABORT_STAGE = "pre_round_abort"

_STYLE = """
  :root { color-scheme: light; }
  body { font: 13px/1.6 -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         margin: 0; padding: 24px; background: #f6f7f9; color: #202124; }
  h1 { font-size: 20px; margin: 0 0 8px; }
  h2 { font-size: 15px; margin: 0 0 4px; }
  h3 { font-size: 13.5px; margin: 16px 0 6px; color: #3c4043; }
  h4 { font-size: 13px; margin: 12px 0 4px; color: #3c4043; }
  .meta { color: #5f6368; margin-bottom: 12px; }
  .meta code { background: #eceff1; padding: 1px 4px; border-radius: 3px; }
  .badge { color: #fff; border-radius: 10px; padding: 1px 8px; font-size: 12px; }
  .perf-chart, .perf-curve { margin: 10px 0 16px; padding: 10px 12px; background: #fbfcfe;
         border: 1px solid #e6e9ef; border-radius: 6px; }
  .perf-chart figcaption, .perf-curve figcaption { font-size: 12.5px; font-weight: 600;
         color: #3c4043; margin-bottom: 4px; }
  .legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 11.5px; color: #5f6368; }
  .legend-item { display: inline-flex; align-items: center; gap: 4px; }
  .legend-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
  .case { background: #fff; border: 1px solid #dadce0; border-radius: 8px; padding: 14px 18px; margin-bottom: 16px; }
  table { border-collapse: collapse; width: 100%; margin-bottom: 10px; font-size: 12.5px; }
  th, td { border: 1px solid #e0e0e0; padding: 5px 8px; text-align: right; white-space: nowrap; }
  th:first-child, td:first-child { text-align: left; }
  thead th { background: #f1f3f4; font-weight: 600; }
  .agg thead th { background: #eef7ee; }
  .note { background: #fef7e0; border-left: 3px solid #f9ab00; padding: 8px 12px; margin-top: 8px; font-size: 12.5px; }
  .note.muted { background: #f1f3f4; border-left-color: #9aa0a6; }
  .note.err { background: #fce8e6; border-left-color: #c5221f; }
  .note ul { margin: 4px 0 0 18px; padding: 0; }
  .note li { margin: 2px 0; }
  pre { margin: 4px 0 0; white-space: pre-wrap; font-size: 12px; }
  footer { color: #5f6368; font-size: 12px; margin-top: 18px; }
  details.detail-table { margin-top: 6px; }
  details.detail-table summary { cursor: pointer; color: #3c4043; font-size: 12.5px; padding: 2px 0; }
  details.detail-table table { margin-top: 6px; }
"""


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return html.escape(str(value))


def _badge(status: str) -> str:
    color = _STATUS_COLOR.get(status, _STATUS_COLOR["unknown"])
    text = _STATUS_TEXT.get(status, status)
    return f'<span class="badge" style="background:{color}">{html.escape(text)}</span>'



def write_summary(result: Any) -> str:
    """落盘 ``perf_summary.json``，返回文件路径。"""
    path = os.path.join(result.run_dir, "perf_summary.json")
    os.makedirs(result.run_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, ensure_ascii=False, indent=2)
    logger.info("[perf] 批次汇总 → %s", path)
    return path


def _infer_unit(metric: str) -> str:
    """按字段名后缀粗略推断单位，仅供阅读辅助，**不参与任何判定逻辑**。"""
    name = metric.lower()
    # 后缀与中缀都要认：场景扁平字段形如 ``render_ms_software_mgr``（单位在**中间**），
    # 而 ``_ms`` 结尾只覆盖 ``render_ms`` 这类纯单位名。
    if name.endswith("_ms") or "_ms_" in name:
        return "ms"
    if "_mb_s" in name:
        return "MB/s"
    if "_mb_" in name or name.endswith("_mb"):
        return "MB"
    if "cpu_percent" in name or name.endswith("_pct") or "_percent" in name:
        return "%"
    if "_cpu_" in name or name.startswith("cpu_"):
        return "%"
    if name.endswith("_gflops"):
        return "GFLOPS"
    return ""


def _metric_stat_rows(value: Any) -> list[tuple[str, Any]]:
    """把聚合结果里单个字段的值展开成 ``(stat, value)`` 列表；不可展开（字符串/列表/未知
    嵌套结构）返回空列表——CSV 只装标量与已知的统计结构，不硬编码具体字段名，
    新场景/新字段天然被覆盖。
    """
    if isinstance(value, dict):
        if "avg" in value:  # summarize() 输出的真实值统计结构（count/min/avg/max）
            return [(k, value.get(k)) for k in _STAT_KEY_ORDER if k in value]
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)) or value is None:
        return [("value", value)]
    return []


def _metrics_base_row(result: Any) -> dict[str, Any]:
    baseline = getattr(result, "baseline", None) or {}
    return {
        "platform": getattr(result, "platform", ""),
        "app": getattr(result, "app", ""),
        "tier": baseline.get("tag") or "",
        "run_id": getattr(result, "run_id", ""),
        "started_at": getattr(result, "started_at", ""),
        "git_rev": getattr(result, "git_rev", ""),
    }


def _metrics_rows(result: Any) -> list[dict[str, Any]]:
    """把一次 ``PerfRunResult`` 展开成长表格（tidy format）行集合。

    行结构固定为 ``platform/app/tier/run_id/started_at/git_rev/case_name/metric/stat/
    value/unit``——通用遍历 ``case.aggregate``，不为具体场景写死字段名（新场景自动覆盖）；
    ``case_name=""`` 的行是主机基准（与具体用例无关，一次一份）。
    """
    base = _metrics_base_row(result)
    rows: list[dict[str, Any]] = []

    host = getattr(result, "host", None) or {}
    if host:
        for key, value in (host.get("raw") or {}).items():
            rows.append({**base, "case_name": "", "metric": f"host_raw_{key}", "stat": "value",
                         "value": value, "unit": _infer_unit(key)})
        for key, value in (host.get("scores") or {}).items():
            rows.append({**base, "case_name": "", "metric": f"host_score_{key}", "stat": "value",
                         "value": value, "unit": ""})
        composite = host.get("composite_score")
        if composite is not None:
            rows.append({**base, "case_name": "", "metric": "host_composite_score", "stat": "value",
                         "value": composite, "unit": ""})

    for case in getattr(result, "cases", []) or []:
        agg = case.aggregate or {}
        for key, value in agg.items():
            if key == "scenarios":
                # 嵌套结构（每场景每字段的 count/min/avg/max）；其标量叶子字段已经通过
                # `**scenario_fields` 平铺进 agg 顶层（如 scan_duration_ms），会被本循环
                # 的通用规则收录，这里跳过避免重复。
                continue
            if key == "resources_by_phase":
                for phase, block in (value or {}).items():
                    if not isinstance(block, dict):
                        continue
                    for field_name, field_value in block.items():
                        if isinstance(field_value, bool):
                            continue
                        if not (isinstance(field_value, (int, float)) or field_value is None):
                            continue
                        metric = f"phase_{phase}_{field_name}"
                        rows.append({**base, "case_name": case.case_name, "metric": metric,
                                     "stat": "value", "value": field_value, "unit": _infer_unit(metric)})
                continue
            if key == "cv":
                for sub_key, sub_value in (value or {}).items():
                    metric = f"cv_{sub_key}"
                    rows.append({**base, "case_name": case.case_name, "metric": metric,
                                 "stat": "value", "value": sub_value, "unit": ""})
                continue
            for stat, stat_value in _metric_stat_rows(value):
                rows.append({**base, "case_name": case.case_name, "metric": key, "stat": stat,
                             "value": stat_value, "unit": _infer_unit(key)})
    return rows


def write_metrics_csv(result: Any) -> str:
    """落盘长表格（tidy format）``perf_metrics.csv``，返回文件路径。

    用途（2026-09-21 用户要求）：把不同平台/机器跑出的结果按**固定字段**拼接对比——
    两份分别在 macOS/Windows 跑出的 CSV 表头完全一致，可直接拼接后按 ``platform`` 列
    筛选。不引入 pandas（R13 零新增第三方依赖），用标准库 ``csv`` 手写。
    """
    path = os.path.join(result.run_dir, "perf_metrics.csv")
    os.makedirs(result.run_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for row in _metrics_rows(result):
            writer.writerow(row)
    logger.info("[perf] 聚合统计(CSV) → %s", path)
    return path


def write_merged_metrics_csv(results: list[Any], run_dir: str) -> str:
    """把多个 ``PerfRunResult``（如多档位编排的 0k/5k/10k/50k 各档）合并成一份顶层
    ``perf_metrics.csv``——字段与单档产物完全一致（``tier`` 列已能区分各档），
    是"跨平台聚合对比"的关键产物：两次不同机器/平台跑出的顶层文件可直接拼接。
    """
    path = os.path.join(run_dir, "perf_metrics.csv")
    os.makedirs(run_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDNAMES)
        writer.writeheader()
        for result in results:
            for row in _metrics_rows(result):
                writer.writerow(row)
    logger.info("[perf] 多档合并统计(CSV) → %s", path)
    return path


def render_html(result: Any) -> str:
    """渲染单文件 HTML 报告（内联样式），返回文件路径。"""
    path = os.path.join(result.run_dir, "perf_report.html")
    os.makedirs(result.run_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_html(result))
    logger.info("[perf] 性能报告 → %s", path)
    return path


def write_multi_tier_summary(
    tiers: list[tuple[str, Any]], run_dir: str, *, host: Optional[dict[str, Any]] = None,
) -> str:
    """落盘多档聚合 ``perf_summary.json``（顶层含各档 ``PerfRunResult.to_dict()`` + host）。"""
    path = os.path.join(run_dir, "perf_summary.json")
    os.makedirs(run_dir, exist_ok=True)
    data = {
        "schema_version": _MULTI_TIER_SCHEMA_VERSION,
        "mode": "multi-tier",
        "host": dict(host or {}),
        "tiers": [{"tier": name, **result.to_dict()} for name, result in tiers],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    logger.info("[perf] 多档聚合汇总 → %s", path)
    return path



def render_multi_tier_html(
    tiers: list[tuple[str, Any]], run_dir: str, *, host: Optional[dict[str, Any]] = None,
    cross_charts: Optional[list[dict[str, Any]]] = None,
) -> str:
    """渲染多单元聚合报告（结论优先体例，2026-10-13）：

    总体健康度（行=指标 / 列=单元）→ 分场景关键指标（同结构）→ **跨单元对比图**
    （横坐标=单元名，如预加载范围 ``off/blank/soft/game/both``）→ 附注 → 页脚。
    逐轮明细/阶段表/全部字段/主机基准/指标词典**不再进 HTML**（原始数据查
    ``perf_summary.json`` / ``perf_metrics.csv``）；各档的完整单档报告仍在 ``tier-*/perf_report.html``。
    """
    path = os.path.join(run_dir, "perf_report.html")
    os.makedirs(run_dir, exist_ok=True)
    app_name = tiers[0][1].app if tiers else ""
    tier_names = [_tier_title(name) for name, _ in tiers]
    per_tier = _per_tier_aggregates(tiers)
    scenario_names: list[str] = []
    for name, _ in tiers:
        # 不排序：aggregate['scenarios'] 是**保序** dict（= 预设里的执行顺序），报告按执行顺序读才顺
        for scenario in ((per_tier.get(_tier_title(name)) or {}).get("scenarios") or {}):
            if scenario not in scenario_names:
                scenario_names.append(scenario)

    # 各档「本次声明执行」的场景清单（``PerfRunResult.planned_scenarios``）：供场景表裁剪列集，
    # 使"本档本次不跑该场景"（多单元编排下 0k/5k/10k/50k 档不跑 stress）不报成红色「未采集」。
    # 空集合 = 该档无声明信息（旧产物/单测手写）→ 报告侧保守保留其列（零回归）。
    plans = {
        _tier_title(name): set(getattr(result, "planned_scenarios", None) or [])
        for name, result in tiers
    }

    sections = [
        f'<section class="case"><h2>总体健康度</h2>{_health_multi(per_tier, tier_names)}</section>',
    ]
    scenario_blocks = [
        _scenario_multi(name, per_tier, tier_names, plans=plans) for name in scenario_names
    ]
    scenario_html = "".join(b for b in scenario_blocks if b)
    if scenario_html:
        sections.append(f'<section class="case"><h2>分场景关键指标</h2>{scenario_html}</section>')
    # 失败场景（含重试全过程）：多档报告也要**完整表**（用户口径"数据要完整展示在 html 报告里"），
    # 附注另给一条指针（见 _notes_multi），不靠读者猜"附注那行数字在哪看细节"
    cross_html = _cross_unit_charts(tiers, tier_names, cross_charts)
    if cross_html:
        sections.append(
            f'<section class="case"><h2>跨单元对比（横坐标=单元）</h2>{cross_html}</section>',
        )
    # 逐挡位采样曲线：每挡位 × 每页「启动 → 点击」的过程形态（峰值/均值答不了"怎么涨的"）
    warmup_html = _warmup_curves_section(tiers, tier_names)
    if warmup_html:
        sections.append(
            '<section class="case"><h2>QMUI 预热期资源采样曲线（按挡位）</h2>'
            f"{warmup_html}</section>",
        )
    launch_html = _launch_failure_section_multi(tiers, tier_names)
    if launch_html:
        sections.append(f'<section class="case"><h2>启动失败与重试</h2>{launch_html}</section>')
    notes = _notes_multi(tiers, per_tier, tier_names, scenario_names, host=host)
    if notes:
        sections.append(f'<section class="case"><h2>附注</h2>{notes}</section>')

    rounds = max((int(getattr(r, "repeat", 0) or 0) for _, r in tiers), default=0)
    meta_bits = []
    if tiers:
        first = tiers[0][1]
        spec = ((getattr(first, "host", None) or {}).get("spec") or {})
        meta_bits = [
            f'平台 <b>{html.escape(_platform_title(first.platform))}</b>',
            (f'机器 <b>{html.escape(str(spec.get("label")))}</b>' if spec.get("label") else ""),
            f'单元 <b>{html.escape(" / ".join(tier_names))}</b>',
            f'每单元轮次 <b>{rounds}</b>',
            (f'时间 <b>{html.escape(str(first.started_at))}</b> → <b>{html.escape(str(getattr(tiers[-1][1], "finished_at", "")))}</b>'),
        ]
    content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>UITA 客户端性能报告（多单元聚合）— {html.escape(str(app_name))}</title>
<style>{_STYLE}</style>
</head>
<body>
  <h1>UITA 客户端性能报告 · 多单元聚合 — {html.escape(str(app_name))}</h1>
  <div class="meta">{" · ".join(b for b in meta_bits if b)}</div>
  {_reading_note(rounds)}
  {"".join(sections)}
  {_footer_note()}
</body>
</html>
"""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    logger.info("[perf] 多档聚合报告 → %s", path)
    return path

def build_ingest_payload(result: Any) -> dict[str, Any]:
    """产出稳定 schema 的 ingest payload（**当前不做任何网络上报**）。

    字段命名包含 ``event_id`` 幂等键、``product_code``、``generated_at`` UTC，
    供可选外部报告聚合服务消费；本阶段仅提供函数与 schema 稳定性。
    """
    metrics: list[dict[str, Any]] = []
    for case in getattr(result, "cases", []):
        agg = case.aggregate or {}
        # 上报口径同为真实值（2026-10-08）：取**实测最差一轮**（max）与均值（avg），
        # 不再上报百分位（下游若按 p95 建仓需同步调整）。
        for key, field in (
            ("launch_ms", "max"),
            ("launch_ms", "avg"),
            ("startup_ms", "max"),
            ("startup_ms", "avg"),
            ("case_duration_ms", "max"),
            ("case_duration_ms", "avg"),
        ):
            stat = agg.get(key) or {}
            if isinstance(stat, dict) and stat.get(field) is not None:
                metrics.append({
                    "code": key,
                    "value": round(float(stat[field]), 2),
                    "unit": "ms",
                    "agg": field,
                    "case": case.case_name,
                })
        for key, unit in (("rss_mb_peak", "MB"), ("cpu_percent_avg", "percent")):
            value = agg.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics.append({
                    "code": key,
                    "value": round(float(value), 2),
                    "unit": unit,
                    "agg": "value",
                    "case": case.case_name,
                })
    return {
        "schema_version": "1.0",
        "event_id": f"client-perf-run:{uuid.uuid4().hex}",
        "product_code": getattr(result, "app", ""),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_rev": getattr(result, "git_rev", ""),
        "case": {
            "name": "|".join(c.case_name for c in getattr(result, "cases", [])),
            "file": "|".join(c.case_file for c in getattr(result, "cases", [])),
            "rounds": getattr(result, "repeat", 0),
        },
        "metrics": metrics,
        "baseline": {
            "source": (result.baseline or {}).get("source", ""),
            "status": _overall_status(result),
        },
    }


def _overall_status(result: Any) -> str:
    statuses = [(c.verdict or {}).get("status", "unknown") for c in getattr(result, "cases", [])]
    if not statuses:
        return "unknown"
    for level in ("fail", "warn", "pass"):
        if level in statuses:
            return level
    return "unknown"



# ── 报告前台（2026-10-13 结论优先体例）────────────────────────────
#
# 用户口径（2026-10-13 原话）：「整那么多细碎的耗时干啥？一个启动搞一大堆耗时。」
# 故前台只保留四块：读表约定 → 总体健康度（6 行）→ 分场景关键指标（每场景若干行 + 1 行资源）
# → 附注。逐轮明细 / 阶段表 / 全部字段 / 主机基准 / 指标词典**已从 HTML 移除**，
# 原始数据改查 ``perf_summary.json`` / ``perf_metrics.csv``。
# 关键**非统计**字段（stopped_by / process_alive / converged …）以一行小字保留——它们就是结论本身。

#: 「总体健康度」固定 6 行（跨场景；该档没跑的场景记 —）。用户明确要求"不要细碎项"。
_HEALTH_METRICS: tuple[tuple[str, str, str], ...] = (
    ("启动耗时", "startup_ms", "ms"),
    ("内存占用峰值", "rss_mb_peak", "MB"),
    ("CPU 均值", "cpu_percent_avg", "%"),
    ("扫描耗时", "scan_duration_ms", "ms"),
    ("缩略图冷加载", "thumb_cold_ms", "ms"),
    ("筛选类型切换", "filter_type_switch_ms", "ms"),
)

#: 关键**非统计**字段（结论本身，放不进统计表）：(字段名, 中文标签)。
_KEY_FACTS: tuple[tuple[str, str], ...] = (
    ("stopped_by", "停止原因"),
    ("fail_kind", "启动失败类型"),
    ("process_alive", "进程存活"),
    ("window_closed", "主窗口已关闭"),
    ("ended_early", "提前结束"),
    ("converged", "已收敛"),
    ("unsupported_reason", "不可测原因"),
    ("mem_cap_reached", "已达内存占用上限"),
    ("mem_cap_ok", "占用上限下仍可启动"),
    ("min_required_bound", "最少所需内存结论口径"),
)

#: **启动失败现场**（``PerfRound.launch_failure``）在报告里展开的列：(字段名, 中文表头)。
#: 字段名与口径**与压测 ``stress`` 逐级记录的系统快照一致**（用户口径 2026-10-15），
#: 故两边数字可直接对照；单位由 ``_infer_unit`` 从后缀推断，不在这里写死。
_FAILURE_FIELDS: tuple[tuple[str, str], ...] = (
    ("mem_used_mb", "系统已占用 (MB)"),
    ("mem_total_mb", "系统总内存 (MB)"),
    ("mem_available_mb", "系统剩余可用 (MB)"),
    ("mem_percent", "系统内存占比 (%)"),
    ("swap_used_mb", "Swap 已用 (MB)"),
    ("app_rss_mb_peak", "被测应用内存峰值 (MB)"),
    ("app_cpu_percent_peak", "被测应用CPU峰值 (%)"),
)


def _report_keys(name: str) -> dict[str, str]:
    """场景的报告关键指标白名单（延迟 import：``scenarios`` 包导入即注册，避免模块级循环）。"""
    from uita.core.perf.scenarios import report_keys

    return report_keys(name)


def _report_detail(name: str) -> dict[str, Any]:
    """场景的「逐级/逐项明细表」契约（延迟 import，理由同 :func:`_report_keys`）。"""
    from uita.core.perf.scenarios import report_detail

    return report_detail(name)


def _scenario_label(name: str) -> str:
    """场景中文名（延迟 import，理由同上）。"""
    from uita.core.perf.scenarios import scenario_label

    return scenario_label(name)


def _platform_title(name: Any) -> str:
    """平台名规范化（``windows`` → ``Windows``，``macos`` → ``macOS``）。"""
    text = str(name or "")
    return {"macos": "macOS", "windows": "Windows", "linux": "Linux"}.get(text.lower(), text)


#: 只有"纯数字 + k"才是规模档位（``0k`` → ``0K``）；其余单元名（如预加载范围 ``blank``）
#: 原样展示——按尾字母判断会把 ``blank`` 变成 ``BLANK``（真机产物里读起来像另一个东西）。
_SIZE_TIER_RE = re.compile(r"\d+k", re.IGNORECASE)


def _tier_title(name: Any) -> str:
    """规模档位名统一成 ``0K``/``50K`` 形式；非档位单元名（``blank``/``soft``）原样返回。"""
    text = str(name)
    return text.upper() if _SIZE_TIER_RE.fullmatch(text) else text


def _num(value: Any, unit: str = "") -> str:
    """表格数值格式：千分位；时长/内存取整、占比 1 位小数、比率最多 2 位并去尾零。"""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if not isinstance(value, (int, float)):
        return html.escape(str(value))
    val = float(value)
    if unit in ("ms", "MB"):
        if abs(val) < 100:                      # 小值保留 1 位（23.4ms 比 23ms 有用），大值取整
            text = f"{val:,.1f}"
            return text[:-2] if text.endswith(".0") else text
        return f"{val:,.0f}"
    if unit == "%":
        text = f"{val:,.1f}"
        return text[:-2] if text.endswith(".0") else text
    if val == int(val):
        return f"{int(val):,}"
    return f"{round(val, 2):,.2f}".rstrip("0").rstrip(".")


def _stats_view(agg: dict[str, Any]) -> dict[str, Any]:
    """把聚合结果拍平成「指标名 → 统计 dict / 标量」，供报告统一渲染「均值 / 最差」。

    关键：场景字段在 ``aggregate`` **顶层**只是"跨轮最差值"标量（``summarize`` 后的 flat 值），
    真正的 ``count/min/avg/max`` 住在 ``aggregate["scenarios"][场景名][字段]`` 里。
    报告要显示"均值 / 最差"，故用场景块里的统计 dict 覆盖同名顶层标量。
    """
    view: dict[str, Any] = dict(agg)
    for block in (agg.get("scenarios") or {}).values():
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if isinstance(value, dict) and "avg" in value:
                view[key] = value
    return view


def _triple(stat: Any, unit: str) -> str:
    """把统计 dict 渲染成「均值 / 最小 / 最大」；标量原样一个数；空统计记 —。

    多轮口径（2026-10-14 八轮，用户口径"执行多次测试时明细展示 平均/最小/最大值"）：
    有 ``min`` 时给全三个数（读者要同时看"典型值"和"波动两端"）；旧产物只有
    ``avg/max``（无 ``min``）时退回「均值 / 最差」两段，不伪造最小。
    """
    if isinstance(stat, dict):
        if "avg" not in stat and "max" not in stat:
            return "—"
        if stat.get("min") is not None:
            return (
                f"{_num(stat.get('avg'), unit)} / {_num(stat.get('min'), unit)}"
                f" / {_num(stat.get('max'), unit)}"
            )
        return f"{_num(stat.get('avg'), unit)} / {_num(stat.get('max'), unit)}"
    return _num(stat, unit)


def _pair(agg: dict[str, Any], key: str) -> str:
    """渲染「均值 / 最小 / 最大」；标量字段（本身已是跨轮聚合）只渲染一个数；缺失记 —。

    精度由字段名推断的单位决定（``_infer_unit``）：时长/内存取整、占比 1 位小数，
    避免出现 ``1,057.43`` / ``181.06`` 这类"原始读数"式的噪声。
    """
    return _triple(agg.get(key), _infer_unit(key))


def _reading_note(rounds: int = 0) -> str:
    suffix = f"（每档 {rounds} 轮）" if rounds else ""
    return (
        '<div class="note muted"><b>读表约定</b>：表内「A / B / C」= <b>跨轮均值 / 最小 / 最大</b>；'
        f"只给一个数的指标本身已是跨轮聚合值{suffix}。逐级明细表同理（每级一行、同级跨轮聚合）。"
        "CPU 为进程树合计（psutil 语义，4 核满载 = 400%）；内存为进程树合计峰值。"
        "启动耗时 = 发起启动 → 主窗口出现且左上角渲染出应用品牌。</div>"
    )


def _footer_note() -> str:
    return (
        '  <footer>本报告只列关键指标；逐轮原始值、全部场景字段、主机基准与指标口径见同目录 '
        "<code>perf_summary.json</code> / <code>perf_metrics.csv</code>。"
        "<br />资源口径：进程树合计（主进程 + 子进程，含 Electron helper），不跨工具直接对比。"
        "<br />性能判定未启用：只呈现实测值，不自动比阈值，也不改写用例通过率。</footer>\n"
    )


def _per_tier_aggregates(tiers: list[tuple[str, Any]]) -> dict[str, dict[str, Any]]:
    """各档主用例的聚合 dict（键为 ``0K``/``5K``/… 形式的档位名）。"""
    out: dict[str, dict[str, Any]] = {}
    for name, result in tiers:
        cases = list(getattr(result, "cases", []) or [])
        out[_tier_title(name)] = dict(cases[0].aggregate or {}) if cases else {}
    return out


def _stat_value(value: Any) -> Optional[float]:
    """把聚合值归一成一个数：``{"count","min","avg","max"}`` → avg；标量原样返回。"""
    if isinstance(value, dict):
        value = value.get("avg")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _cross_unit_charts(
    tiers: list[tuple[str, Any]], tier_names: list[str],
    spec: Optional[list[dict[str, Any]]],
) -> str:
    """跨单元对比图：**横坐标 = 单元名**（如预加载范围），纵坐标 = 该指标在各单元的均值。

    图组声明来自 app 侧（``apps/<app>/perf/presets.py::CROSS_UNIT_CHARTS``，见
    ``app_loader.load_app_cross_unit_charts``）——"哪几项指标该放一张图、单位是什么"是
    应用知识（守 R2）；core 只按声明取 ``aggregate[key].avg`` 画图。

    某单元缺该指标（该单元没跑这个场景/没采到）→ 该点缺失、折线断开，**不补 0**。

    取值必须走 :func:`_stats_view`：场景字段在 ``aggregate`` 顶层只是"跨轮最差值"标量
    （耗时类场景声明 ``AGGREGATE_PICK=min`` 时那就是**最小**耗时），统计三件套住在
    ``aggregate["scenarios"][场景名][字段]``；不过这一层就会把最小值当均值画出来。
    """
    if not spec or not tier_names:
        return ""
    from uita.core.perf.chart import ChartSeries, render_line_chart

    per_unit = {name: _stats_view(agg) for name, agg in _per_tier_aggregates(tiers).items()}
    blocks: list[str] = []
    for group in spec:
        if not isinstance(group, dict):
            continue
        series_spec = [x for x in (group.get("series") or []) if isinstance(x, dict)]
        if not series_spec:
            continue
        unit_text = str(group.get("unit") or "")
        series = []
        for item in series_spec:
            key = str(item.get("key") or "").strip()
            if not key:
                continue
            values = [_stat_value((per_unit.get(name) or {}).get(key)) for name in tier_names]
            series.append(
                ChartSeries(str(item.get("label") or key), values, unit=unit_text),
            )
        blocks.append(
            render_line_chart(
                series,
                title=str(group.get("title") or "跨单元对比"),
                y_unit=unit_text,
                x_label="单元",
                x_labels=list(tier_names),
            ),
        )
    return '<div class="charts">' + "".join(blocks) + "</div>" if blocks else ""


def _health_multi(per_tier: dict[str, dict[str, Any]], tier_names: list[str]) -> str:
    """总体健康度表：行 = 关键指标，列 = 档位（取值走 ``_stats_view``，故显示"均值 / 最差"）。"""
    rows = []
    for label, key, unit in _HEALTH_METRICS:
        cells = "".join(
            f"<td>{_pair(_stats_view(per_tier.get(name) or {}), key)}</td>" for name in tier_names
        )
        suffix = f" ({unit})" if unit else ""
        rows.append(f"<tr><td>{html.escape(label)}{suffix}</td>{cells}</tr>")
    head = "".join(f"<th>{html.escape(name)}</th>" for name in tier_names)
    return (
        f'<table class="agg"><thead><tr><th>指标</th>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _health_single(agg: dict[str, Any]) -> str:
    rows = []
    for label, key, unit in _HEALTH_METRICS:
        suffix = f" ({unit})" if unit else ""
        rows.append(f"<tr><td>{html.escape(label)}{suffix}</td><td>{_pair(agg, key)}</td></tr>")
    return (
        '<table class="agg"><thead><tr><th>指标</th><th>均值 / 最小 / 最大</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _metric_value(agg: dict[str, Any], key: str, field_name: str) -> Optional[float]:
    """取聚合值：``dict``（count/min/avg/max）取子字段，标量原样返回；非数值记 ``None``。"""
    val = agg.get(key)
    raw = val.get(field_name) if isinstance(val, dict) else val
    return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None


def _covers_resources(name: str, keys: dict[str, str]) -> bool:
    """该场景的白名单是否已经覆盖"CPU 均值 / 内存峰值"（如 idle）→ 避免重复一行。"""
    return f"{name}_cpu_percent_avg" in keys and f"{name}_rss_mb_peak" in keys


def _resource_cell(block: dict[str, Any], name: str) -> str:
    """场景资源行：该场景的 CPU 均值 / 内存占用峰值（取跨轮最高均值 / 最高峰值）。"""
    cpu = _metric_value(block, f"{name}_cpu_percent_avg", "max")
    mem = _metric_value(block, f"{name}_rss_mb_peak", "max")
    return f"{_num(cpu, '%')} / {_num(mem, 'MB')}"


def _facts_text(block: dict[str, Any]) -> str:
    """关键**非统计**字段（结论本身）的纯文本形式；都没有则返回空串。"""
    raw = block.get("raw") or []
    first = raw[0] if raw and isinstance(raw[0], dict) else {}
    facts = []
    for key, label in _KEY_FACTS:
        value = first.get(key)
        if value is None or value == "":
            continue
        facts.append(f"{label} = {_num(value)}")
    return " · ".join(facts)


def _key_facts_line(block: dict[str, Any]) -> str:
    """一行小字：把关键**非统计**字段（结论本身）列出来；都没有则返回空串。"""
    text = _facts_text(block)
    return f'<div class="note muted">运行口径：{html.escape(text)}</div>' if text else ""


def _dedupe_errors(errors: Any) -> str:
    """错误文本去重 + 计数：同一错误在 3 轮里重复出现只列一次（``（×3 轮）``）。"""
    counts: dict[str, int] = {}
    for item in (errors or []):
        text = str(item).strip()
        if text:
            counts[text] = counts.get(text, 0) + 1
    parts = [text if times == 1 else f"{text}（×{times} 轮）" for text, times in counts.items()]
    return " · ".join(parts)


def _round_attempts(round_obj: Any) -> list[dict[str, Any]]:
    """单轮的**每一次**启动失败尝试（``launch_attempts``）。

    兼容性：改动前的产物只有 ``launch_failure``（单次、且等于"本轮中止"），此时回落成
    "一条尝试记录"，报告照旧只显示一行——旧产物/旧单测零回归。
    """
    items = getattr(round_obj, "launch_attempts", None) or []
    rows = [dict(i) for i in items if isinstance(i, dict) and i]
    if rows:
        return rows
    fail = getattr(round_obj, "launch_failure", None)
    return [dict(fail)] if isinstance(fail, dict) and fail else []


def _launch_attempt_rows(rounds: Any) -> list[dict[str, Any]]:
    """展开全部"启动失败尝试"（带轮次号），供表格/摘要使用。"""
    out: list[dict[str, Any]] = []
    for r in (rounds or []):
        idx = int(getattr(r, "round_index", 0) or 0)
        for i, item in enumerate(_round_attempts(r), start=1):
            out.append({"round_index": idx, "attempt": int(item.get("attempt") or i), **item})
    return out


def _attempts_total(items: list[dict[str, Any]]) -> int:
    """本轮最多尝试几次（**取自数据**：报告端不硬编码 3，改 runner 常量即自动跟随）。"""
    totals = [int(i.get("attempts_total") or 0) for i in items]
    return max(totals) if totals else len(items)


def _failure_bits(item: dict[str, Any]) -> str:
    """把一条失败记录压成"已占用 x/y MB、剩余可用 z MB、占比 p%"（缺失项跳过，不打印 — ）。"""
    bits: list[str] = []
    used, total = item.get("mem_used_mb"), item.get("mem_total_mb")
    if used is not None or total is not None:
        bits.append(f"已占用 {_num(used, 'MB')}/{_num(total, 'MB')}MB")
    if item.get("mem_available_mb") is not None:
        bits.append(f"剩余可用 {_num(item.get('mem_available_mb'), 'MB')}MB")
    if item.get("mem_percent") is not None:
        bits.append(f"占比 {_num(item.get('mem_percent'), '%')}%")
    return "、".join(bits) or "系统内存未读到"


def _failure_kind(item: dict[str, Any]) -> str:
    """一次失败的归类文本：``stage：reason``。"""
    stage = str(item.get("stage") or "").strip()
    reason = str(item.get("reason") or "").strip()
    return f"{stage}：{reason}" if stage and reason else (stage or reason)


def _round_launch_lines(rounds: Any) -> list[tuple[int, str]]:
    """每轮一句话结论：``(轮次, 文案)`` —— 单档块与多档附注共用同一口径。

    - 重试用尽（``launch_failure`` 非空）：`3 次尝试全部失败（a → b → c）→ 本轮中止…`；
    - 重试后成功：`第 1 次启动失败（a）→ 第 2 次重试成功，本轮继续`。
    """
    lines: list[tuple[int, str]] = []
    for r in (rounds or []):
        items = _round_attempts(r)
        if not items:
            continue
        idx = int(getattr(r, "round_index", 0) or 0)
        total = _attempts_total(items)
        kinds = " → ".join(_failure_kind(i) or "启动失败" for i in items)
        if getattr(r, "launch_failure", None):
            if all(str(i.get("stage") or "") == PRE_ROUND_ABORT_STAGE for i in items):
                # 一次启动都没发起（app 侧启动闸门判定"旧实例退不干净"）：不能写成
                # "N 次尝试全部失败"，那会把"没启动"说成"启动了 N 次"。
                lines.append((
                    idx,
                    f"前置准备未完成（一次启动都没发起）：{kinds} → 本轮中止（跳过用例/静息/场景）",
                ))
                continue
            lines.append((
                idx,
                f"{len(items)} 次尝试全部失败（{kinds}）→ 本轮中止（跳过用例/静息/场景）；"
                f"末次现场：{_failure_bits(items[-1])}",
            ))
        else:
            lines.append((
                idx,
                f"第 {len(items)}/{total} 次尝试启动失败（{kinds}）→ 下一次重试成功，本轮继续；"
                + "；".join(_failure_bits(i) for i in items),
            ))
    return lines


def _launch_failure_summary(rounds: Any) -> str:
    """一行摘要（多档聚合报告的附注用：列表里不塞表格）。"""
    return "；".join(f"第 {idx} 轮：{text}" for idx, text in _round_launch_lines(rounds))


def _launch_attempt_table_html(rows: list[dict[str, Any]], *, tier_col: bool = False) -> str:
    """启动失败现场表：**每一次失败尝试一整行**（系统占用/剩余/占比/Swap/应用峰值全量）。

    用户口径（2026-10-15）："过程中所有失败要按照启动失败记录当前环境情况……并且数据要
    完整展示在 html 报告里"——故不做"只显示末次"的裁剪：重试过程中失败的每一次都在表里。
    """
    head = "".join(f"<th>{html.escape(label)}</th>" for _, label in _FAILURE_FIELDS)
    tier_head = "<th>档位</th>" if tier_col else ""
    body = []
    for item in rows:
        cells = "".join(
            f"<td>{_num(item.get(key), _infer_unit(key))}</td>" for key, _ in _FAILURE_FIELDS
        )
        attempt = f"{item.get('attempt')}/{_attempts_total([item])}"
        tier_cell = f"<td>{html.escape(str(item.get('tier') or ''))}</td>" if tier_col else ""
        body.append(
            f"<tr>{tier_cell}<td>{item['round_index']}</td><td>{html.escape(attempt)}</td>"
            f"<td>{html.escape(_failure_kind(item) or '启动失败')}</td>{cells}</tr>"
        )
    return (
        f'<table class="agg"><thead><tr>{tier_head}<th>轮次</th><th>尝试</th>'
        f"<th>失败归类</th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _launch_failure_block_html(rows: list[dict[str, Any]], lines: list[str], *, tier_col: bool = False) -> str:
    """**失败场景**块：每次失败的系统/应用现场 + 每轮结论（单档与多档共用）。"""
    total = _attempts_total(rows)
    items = "".join(f"<li>{html.escape(t)}</li>" for t in lines)
    return (
        '<div class="note err"><b>启动失败现场</b>'
        f"（每次失败都是那一刻的系统内存快照，口径与压测 stress 一致；启动失败自动杀进程重试，"
        f"最多 {total} 次，全部失败才中止本轮）"
        f"<ul>{items}</ul>"
        f"{_launch_attempt_table_html(rows, tier_col=tier_col)}</div>"
    )


def _launch_failure_note(rounds: Any) -> str:
    """单档报告的失败场景块（数据取逐轮原始记录，见 :func:`_round_attempts`）。"""
    rows = _launch_attempt_rows(rounds)
    if not rows:
        return ""
    lines = [
        f"第 {idx} 轮：{text}" for idx, text in _round_launch_lines(rounds)
    ]
    return _launch_failure_block_html(rows, lines)


def _launch_failure_section_multi(tiers: list[tuple[str, Any]], tier_names: list[str]) -> str:
    """多档聚合报告的失败场景块：每一次失败尝试一整行（多一列"档位"）。

    顶层报告此前只在附注给一行——用户要求"数据要完整展示在 html 报告里"，故单档/多档
    都给**完整表**；附注保留一条指针（附注是"异常清单"的读法，不能被大表替代）。
    """
    rows: list[dict[str, Any]] = []
    lines: list[str] = []
    for label, (_name, result) in zip(tier_names, tiers):
        for case in (getattr(result, "cases", None) or []):
            rounds = getattr(case, "rounds", None) or []
            for row in _launch_attempt_rows(rounds):
                rows.append({**row, "tier": label})
            lines.extend(
                f"{label} 第 {idx} 轮：{text}" for idx, text in _round_launch_lines(rounds)
            )
    if not rows:
        return ""
    return _launch_failure_block_html(rows, lines, tier_col=True)


def _scenario_notes(block: dict[str, Any]) -> str:
    """场景级异常与运行口径（异常必须可见；其它只留一行）。"""
    notes = []
    if block.get("unsupported"):
        notes.append(
            '<div class="note muted">该场景不可测：'
            f'{html.escape(str(block.get("unsupported_reason") or ""))}</div>'
        )
    errors = _dedupe_errors(block.get("errors"))
    if errors:
        notes.append(f'<div class="note err"><b>场景异常</b>：{html.escape(errors)}</div>')
    facts = _key_facts_line(block)
    if facts:
        notes.append(facts)
    return "".join(notes)


def _scenario_lacks_data(block: dict[str, Any], name: str) -> bool:
    """该档此场景是否"白名单指标一个都没采到"（决定要不要给出未采集原因）。"""
    keys = _report_keys(name)
    if not keys:
        return False
    for key in keys:
        value = block.get(key)
        if isinstance(value, dict):
            if value.get("avg") is not None or value.get("max") is not None:
                return False
        elif value is not None:
            return False
    return True


def _short_reason(text: Any, *, limit: int = 120) -> str:
    """把场景原因压成报告里能读的一行：去掉内嵌的原始采样 dict（``（最差瞬间 {...}）``）再截断。"""
    out = str(text or "").strip()
    for marker in ("（最差瞬间", "(最差瞬间", "（最差", "（worst"):
        pos = out.find(marker)
        if pos > 0:
            out = out[:pos].strip()
            break
    return out[:limit]


def _missing_reason(block: dict[str, Any], name: str) -> str:
    """该档此场景"为什么是空的"——有数据、或确实无原因时返回空串。

    用户口径（2026-10-13）："动不动就留空是什么毛病" —— 空的单元格必须能自己解释：
    是**不可测**（引擎能力/读数不可采信）、**场景异常**（如 ``TargetClosedError`` 应用/页面中途失效），
    还是**探针没回数据**。原因取自聚合块里的 ``unsupported_reason`` / ``errors``（逐轮去重计数）。
    """
    if not _scenario_lacks_data(block, name):
        return ""
    # 「本轮根本没跑」优先于其它原因：启动失败时本轮直接结束（见 runner._run_case 的 abort 分支），
    # 场景占位记 ``skipped``。不说清楚就会把"没跑"读成"探针没回数据"（口径不符）。
    skipped = _short_reason(block.get("skipped"), limit=200)
    if skipped:
        return f"已跳过（{skipped}）"
    if block.get("unsupported"):
        reason = _short_reason(block.get("unsupported_reason"))
        return f"不可测（{reason}）" if reason else "不可测（引擎能力不足或读数不可采信）"
    errors = _dedupe_errors(block.get("errors"))
    if errors:
        return _short_reason(errors, limit=200)
    return "未采集（探针未返回数据）"


def _group_detail_items(raw: list[Any], source: str, key: str) -> list[list[dict[str, Any]]]:
    """把跨轮的 ``raw[][source]`` 列表分组，同一「行标识」的条目分到同一组（保持首次出现顺序）。

    有 ``key`` 时按该字段值分组（如 stress 按 ``step`` 级数对齐，不依赖"各轮级数相同"——
    某轮更早 ``alloc_failed``/``app_start_failed`` 导致级数更少，缺的级就少几个跨轮样本，
    不报错、不对错位）；无 ``key`` 时按列表位置分组（要求各轮该 list 顺序语义一致）。
    """
    groups: dict[Any, list[dict[str, Any]]] = {}
    order: list[Any] = []
    for round_data in raw:
        if not isinstance(round_data, dict):
            continue
        items = [it for it in (round_data.get(source) or []) if isinstance(it, dict)]
        for pos, item in enumerate(items):
            group_key = item.get(key) if key else pos
            if group_key not in groups:
                groups[group_key] = []
                order.append(group_key)
            groups[group_key].append(item)
    return [groups[k] for k in order]


def _merge_detail_value(values: list[Any]) -> Any:
    """多轮同格值合并：全数值 → ``summarize()`` 统计 dict（供 :func:`_triple` 渲染均值/最小/最大）；
    否则按去重计数拼接文本（单值不加后缀，多值如 ``success×2 / failed×1``）；全空 → ``None``。
    """
    present = [v for v in values if v is not None and v != ""]
    if not present:
        return None
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        from uita.core.perf.metrics import summarize

        return summarize(present)
    counts: dict[str, int] = {}
    order: list[str] = []
    for v in present:
        text = str(v)
        if text not in counts:
            counts[text] = 0
            order.append(text)
        counts[text] += 1
    if len(order) == 1:
        return order[0]
    return " / ".join(f"{t}×{counts[t]}" for t in order)


def _detail_rows(block: dict[str, Any], detail: dict[str, Any]) -> list[dict[str, Any]]:
    """从场景聚合块的 ``raw``（逐轮原始 ``measure()`` 返回值）取出「逐级/逐项」明细行。

    2026-10-14 八轮：不再只取 ``raw[0]``——声明 ``REPORT_DETAIL`` 的场景可能跑多轮
    （如 stress 不再 last_round_only、跟随 ``--perf-repeat``），须跨轮对齐同一行（靠 ``key``，
    见 :func:`_group_detail_items`）再逐字段合并（:func:`_merge_detail_value`）；只跑 1 轮时
    合并退化为该轮原值（数值统计 min=avg=max，分类字段原样），行为与旧版一致。
    行标识字段（``key``）本身不参与合并，直接取组内第一条的原值。
    """
    raw = block.get("raw") or []
    source = str(detail.get("source") or "")
    key = detail.get("key") or None
    fields = detail["fields"]
    rows: list[dict[str, Any]] = []
    for items in _group_detail_items(raw, source, key):
        row: dict[str, Any] = {}
        for field in fields:
            if field == key:
                row[field] = items[0].get(field) if items else None
                continue
            row[field] = _merge_detail_value([it.get(field) for it in items])
        rows.append(row)
    return rows


def _detail_cell(value: Any, key: str) -> str:
    """明细表单元格：统计 dict 走「均值/最小/最大」，数值按字段名推断单位格式化，
    字符串（含 :func:`_merge_detail_value` 已拼好的去重计数文本）直接转义，空值记 ``—``。
    """
    if value is None or value == "":
        return "—"
    if isinstance(value, dict) and ("avg" in value or "max" in value):
        return _triple(value, _infer_unit(key))
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return _num(value, _infer_unit(key))
    return html.escape(str(value))


def _detail_table(name: str, block: dict[str, Any]) -> str:
    """单档场景的「逐级/逐项明细表」（声明 ``REPORT_DETAIL`` 的场景才渲染；无数据返回空串）。"""
    detail = _report_detail(name)
    if not detail:
        return ""
    rows = _detail_rows(block, detail)
    if not rows:
        return ""
    fields = detail["fields"]
    head = "".join(f"<th>{html.escape(zh)}</th>" for zh in fields.values())
    body = "".join(
        "<tr>" + "".join(f"<td>{_detail_cell(row.get(key), key)}</td>" for key in fields) + "</tr>"
        for row in rows
    )
    return (
        '<details class="detail-table" open><summary>逐级明细</summary>'
        f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></details>'
    )


def _detail_table_multi(name: str, blocks: list[dict[str, Any]], tier_names: list[str]) -> str:
    """多档场景的「逐级/逐项明细表」：每档一张小表（档位不同、级数通常不同，无法合并成一张）。"""
    detail = _report_detail(name)
    if not detail:
        return ""
    fields = detail["fields"]
    tables = []
    for tier, block in zip(tier_names, blocks):
        rows = _detail_rows(block, detail)
        if not rows:
            continue
        head = "".join(f"<th>{html.escape(zh)}</th>" for zh in fields.values())
        body = "".join(
            "<tr>" + "".join(f"<td>{_detail_cell(row.get(key), key)}</td>" for key in fields) + "</tr>"
            for row in rows
        )
        tables.append(
            f"<h5>{html.escape(tier)}</h5>"
            f'<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>'
        )
    if not tables:
        return ""
    return f'<details class="detail-table" open><summary>逐级明细</summary>{"".join(tables)}</details>'


def _scenario_single(name: str, block: dict[str, Any], agg: dict[str, Any]) -> str:
    """单档场景块：白名单指标（中文标签）+ 1 行资源 + 异常/运行口径 + 逐级明细（声明则有）。"""
    keys = _report_keys(name)
    body = []
    for key, zh in keys.items():
        body.append(f"<tr><td>{html.escape(zh)}</td><td>{_pair(block, key)}</td></tr>")
    if not keys:
        body.append(
            '<tr><td colspan="2">该场景未声明关键指标'
            "（完整字段见 perf_summary.json / perf_metrics.csv）</td></tr>"
        )
    if not _covers_resources(name, keys):
        body.append(f'<tr><td>CPU 均值 / 内存占用峰值</td><td>{_resource_cell(block, name)}</td></tr>')
    return (
        f"<h4>{html.escape(_scenario_label(name))}</h4>"
        '<table class="agg"><thead><tr><th>指标</th><th>均值 / 最小 / 最大</th></tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table>{_scenario_notes(block)}{_detail_table(name, block)}"
    )


def _scenario_multi(
    name: str, per_tier: dict[str, dict[str, Any]], tier_names: list[str],
    *, plans: Optional[dict[str, set[str]]] = None,
) -> str:
    """多档场景表：行 = 该场景白名单指标（+1 行资源），列 = 档位；全档无数据则整块跳过。

    ``plans``（档位 → 该档**本次声明执行**的场景清单，来自 ``PerfRunResult.planned_scenarios``）
    用于**裁剪列集**：某档本次没声明跑该场景（如多单元编排里 ``0k`` 档不跑 ``stress``）时
    不渲染该列、也不报「未采集」——本就不在执行范围内，报出来只会被读成采集异常；
    而"声明了跑、但没采到数据"仍**保留列并给出来因**（守 §12.9「空单元格必须自解释」纪律）。
    某档无声明信息（``planned_scenarios`` 为空：旧产物 / 单测手写 ``PerfRunResult``）→
    保守保留该列（既有产物与既有单测行为零变化）。
    """
    keys = _report_keys(name)
    pairs = [(t, per_tier.get(t) or {}) for t in tier_names]
    if plans:
        kept = [
            (t, agg) for t, agg in pairs
            if not (plans.get(t) or set()) or name in (plans.get(t) or set())
        ]
        # 极端兜底：若所有档都声明"不跑该场景"（不该发生，scenario_names 取自实际有数据者），
        # 保持全列而不是渲染一张空表头都没有的怪表。
        if kept:
            pairs = kept
    cols = [t for t, _ in pairs]
    blocks = [((agg.get("scenarios") or {}).get(name) or {}) for _, agg in pairs]

    if not any(blocks):
        return ""
    rows = []
    for key, zh in keys.items():
        cells = "".join(f"<td>{_pair(block, key)}</td>" for block in blocks)
        rows.append(f"<tr><td>{html.escape(zh)}</td>{cells}</tr>")
    if not keys:
        rows.append(
            f'<tr><td colspan="{len(cols) + 1}">该场景未声明关键指标'
            "（完整字段见 perf_summary.json / perf_metrics.csv）</td></tr>"
        )
    if not _covers_resources(name, keys):
        rows.append(
            "<tr><td>CPU 均值 / 内存占用峰值</td>"
            + "".join(f"<td>{_resource_cell(block, name)}</td>" for block in blocks)
            + "</tr>"
        )
    head = "".join(f"<th>{html.escape(t)}</th>" for t in cols)
    notes = []
    for tier, block in zip(cols, blocks):
        text = _facts_text(block)
        if text:
            notes.append(f"{tier}：{text}")
    note_html = (
        '<div class="note muted">运行口径：' + html.escape(" ｜ ".join(notes)) + "</div>"
        if notes else ""
    )
    # "为什么是空的"必须写在报告里（用户口径 2026-10-13：不许动不动留空还不说原因）
    missing = [
        f"{tier}：{text}"
        for tier, block in zip(cols, blocks)
        if (text := _missing_reason(block, name))
    ]
    missing_html = (
        '<div class="note err"><b>未采集</b>：' + html.escape(" ｜ ".join(missing)) + "</div>"
        if missing else ""
    )
    return (
        f"<h3>{html.escape(_scenario_label(name))}</h3>"
        f'<table class="agg"><thead><tr><th>指标</th>{head}</tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>{note_html}{missing_html}"
        f"{_detail_table_multi(name, blocks, cols)}"
    )


def _baseline_tag_text(baseline: dict[str, Any]) -> str:
    """档位与覆盖项（事后"用了哪套阈值"的复核依据）。"""
    tag = str(baseline.get("tag") or "")
    if not tag:
        return ""
    overrides = list(baseline.get("tag_overrides") or [])
    extra = f"，覆盖 {len(overrides)} 项：{html.escape(', '.join(str(o) for o in overrides))}" if overrides else ""
    return f'（档位 <b>{html.escape(tag)}</b>{extra}）'


def _notes_block(result: Any) -> str:
    """附注：跳过的场景 / 档位与平台告警 / 基线判定状态。"""
    notes = []
    for item in (getattr(result, "skipped_scenarios", None) or []):
        notes.append(
            f'<div class="note">已跳过场景 <b>{html.escape(str(item.get("scenario")))}</b>：'
            f'{html.escape(str(item.get("reason")))}</div>'
        )
    baseline = getattr(result, "baseline", None) or {}
    for key, label in (("tag_warning", "基线档位告警"), ("platform_warning", "基线平台告警")):
        if baseline.get(key):
            notes.append(f'<div class="note err">{label}：{html.escape(str(baseline[key]))}</div>')
    notes.append(
        '<div class="note muted">性能判定未启用（只呈现实测值）；基线文件：'
        f'<code>{html.escape(str(baseline.get("source") or "未加载"))}</code>'
        + _baseline_tag_text(baseline)
        + "</div>"
    )
    return "".join(notes)


def _notes_multi(
    tiers: list[tuple[str, Any]], per_tier: dict[str, dict[str, Any]],
    tier_names: list[str], scenario_names: list[str], *, host: Optional[dict[str, Any]] = None,
) -> str:
    """多档附注：告警合并成一行、异常按"档位 + 场景"去重成列表（同类错误只列一次）。"""
    warnings: list[str] = []
    anomalies: list[str] = []
    for label, (_name, result) in zip(tier_names, tiers):
        for item in (getattr(result, "skipped_scenarios", None) or []):
            anomalies.append(f"{label} 跳过场景 {item.get('scenario')}：{item.get('reason')}")
        baseline = getattr(result, "baseline", None) or {}
        for key in ("tag_warning", "platform_warning"):
            if baseline.get(key):
                warnings.append(f"{label}：{baseline[key]}")
        for case in (getattr(result, "cases", None) or []):
            rounds = getattr(case, "rounds", None) or []
            text = _dedupe_errors([r.error for r in rounds])
            if text:
                anomalies.append(f"{label} 用例轮次异常：{text}")
            # 启动失败现场（含重试）：完整表在上方「启动失败现场」section，附注只留一条指针
            attempts = _launch_attempt_rows(rounds)
            if attempts:
                aborted = sum(1 for r in rounds if getattr(r, "launch_failure", None))
                tail = f"，{aborted} 个轮次中止（重试用尽）" if aborted else "（重试后已成功）"
                anomalies.append(
                    f"{label} 启动失败现场：{len(attempts)} 次失败尝试{tail}，详见上方「启动失败与重试」表"
                )
    for name in scenario_names:
        for tier, agg in per_tier.items():
            block = (agg.get("scenarios") or {}).get(name) or {}
            text = _dedupe_errors(block.get("errors"))
            if text:
                anomalies.append(f"{tier} {_scenario_label(name)}：{text}")
    notes: list[str] = []
    if warnings:
        items = "".join(f"<li>{html.escape(w)}</li>" for w in warnings)
        notes.append(f'<div class="note err"><b>基线告警</b><ul>{items}</ul></div>')
    if anomalies:
        items = "".join(f"<li>{html.escape(a)}</li>" for a in anomalies)
        notes.append(f'<div class="note err"><b>异常与跳过</b><ul>{items}</ul></div>')
    first_baseline = (getattr(tiers[0][1], "baseline", None) or {}) if tiers else {}
    notes.append(
        '<div class="note muted">性能判定未启用（只呈现实测值）；基线文件：'
        f'<code>{html.escape(str(first_baseline.get("source") or "未加载"))}</code>'
        + _baseline_tag_text(first_baseline)
        + "</div>"
    )
    return "".join(notes)


#: 「逐轮趋势」块的最小轮次：轮次太少时曲线没有信息量，反而把报告撑长
TREND_MIN_ROUNDS = 20

#: 趋势块展示的指标：(单轮取值键, 中文标签, 单位)。取值优先 ``PerfRound`` 直接字段，
#: 其余取 ``PerfRound.resources``（进程树采样汇总）。
_TREND_METRICS: tuple[tuple[str, str, str], ...] = (
    ("launch_ms", "启动连接耗时", "ms"),
    ("startup_ms", "启动就绪耗时", "ms"),
    ("rss_mb_peak", "RSS 峰值", "MB"),
    ("cpu_percent_avg", "CPU 均值", "%"),
    ("thread_count_peak", "线程数峰值", ""),
    ("proc_count_peak", "进程数峰值", ""),
)


#: 场景侧逐轮趋势指标：``(场景名, 点路径, 中文标签, 单位)``。页面集合（有哪些页）是
#: **app 知识**，不能写死在 core——故按 ``pages.*`` 逐页动态展开，见 :func:`_trend_rows`。
_TREND_SCENARIO_METRICS: tuple[tuple[str, str, str, str], ...] = (
    ("page_render", "startup_loading.cpu_peak", "启动加载期 CPU 峰值", "%"),
    ("page_render", "startup_loading.rss_peak_mb", "启动加载期内存峰值", "MB"),
)


def _round_metric(round_obj: Any, key: str) -> Optional[float]:
    """从单轮记录取一个可比的数值；缺失记 ``None``（**不记 0**，也不猜）。"""
    if key in ("launch_ms", "startup_ms"):
        value = getattr(round_obj, key, None)
    else:
        value = (getattr(round_obj, "resources", {}) or {}).get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _trend_rows(rounds: list[Any]) -> list[tuple[str, str, list[Optional[float]]]]:
    """收集逐轮趋势的 ``(中文标签, 单位, 逐轮取值)`` 行（固定指标 + 场景侧动态指标）。

    场景侧动态部分是 ``render_ms``：页面集合由 app 的页面声明表决定（可能 2 个也可能 5 个），
    所以逐页展开，而不是把 ``render_ms_software_mgr`` 之类具名指标写进 core 常量。
    """
    rows: list[tuple[str, str, list[Optional[float]]]] = [
        (label, unit, [_round_metric(r, key) for r in rounds])
        for key, label, unit in _TREND_METRICS
    ]
    for scenario, path, label, unit in _TREND_SCENARIO_METRICS:
        rows.append((label, unit, [_round_scenario_field(r, scenario, path) for r in rounds]))
    scenario = _page_render_scenario(rounds)
    for key, page_label in _page_render_pages(rounds, scenario):
        rows.append((
            f"{page_label} 渲染耗时", "ms",
            [_round_scenario_field(r, scenario, f"pages.{key}.render_ms") for r in rounds],
        ))
    return rows


def _sparkline(
    values: list[Optional[float]], *, width: int = 640, height: int = 56, pad: int = 4,
) -> str:
    """把逐轮序列渲染成内联 SVG 折线（无 JS / 无外部资源）。

    缺失轮次**断开折线**而不是补 0：失败轮补 0 会画出"耗时暴跌"的假象
    （见报告纪律：空单元格必须自解释）。
    """
    points = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(points) < 2:
        return '<span class="hint">数据点不足</span>'
    lo = min(v for _, v in points)
    hi = max(v for _, v in points)
    span = (hi - lo) or 1.0
    step = (width - 2 * pad) / max(1, len(values) - 1)

    def _xy(index: int, value: float) -> tuple[float, float]:
        x = pad + index * step
        y = height - pad - (value - lo) / span * (height - 2 * pad)
        return x, y

    segments: list[list[str]] = []
    current: list[str] = []
    previous: Optional[int] = None
    for index, value in points:
        if previous is not None and index != previous + 1 and len(current) > 1:
            segments.append(current)
            current = []
        x, y = _xy(index, value)
        current.append(f"{x:.1f},{y:.1f}")
        previous = index
    if len(current) > 1:
        segments.append(current)
    if not segments:
        # 全是孤立点（相邻轮都失败）：至少把点标出来，别渲染成空白
        return (
            f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
            f'role="img" aria-label="逐轮趋势">'
            + "".join(
                f'<circle cx="{_xy(i, v)[0]:.1f}" cy="{_xy(i, v)[1]:.1f}" r="1.6" fill="#2f6fed" />'
                for i, v in points
            )
            + "</svg>"
        )
    lines = "".join(
        f'<polyline fill="none" stroke="#2f6fed" stroke-width="1.4" '
        f'points="{" ".join(seg)}" />'
        for seg in segments
    )
    first_x, first_y = _xy(*points[0])
    last_x, last_y = _xy(*points[-1])
    dots = (
        f'<circle cx="{first_x:.1f}" cy="{first_y:.1f}" r="2" fill="#8a94a6" />'
        f'<circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="2" fill="#2f6fed" />'
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="逐轮趋势">{lines}{dots}</svg>'
    )


def _trend_block(case: Any) -> str:
    """单档报告的「逐轮趋势」块（轮次 ≥ :data:`TREND_MIN_ROUNDS` 才渲染）。

    为什么需要：长跑（如 500 次"清缓存 → 冷启动"循环）要回答的是"随轮次是否劣化"，
    聚合均值/峰值答不了——第 500 轮慢 3 倍，均值可能只涨 50%。故按轮次画折线，
    并给首轮/末轮/中位/最差四个锚点（都是实测值，不做任何加工）。
    """
    rounds = list(getattr(case, "rounds", []) or [])
    if len(rounds) < TREND_MIN_ROUNDS:
        return ""
    rows: list[str] = []
    for label, unit, values in _trend_rows(rounds):
        numbers = [v for v in values if v is not None]
        if len(numbers) < 2:
            continue
        first = next(v for v in values if v is not None)
        last = next(v for v in reversed(values) if v is not None)
        suffix = f" {unit}" if unit else ""
        rows.append(
            "<tr>"
            f"<td>{html.escape(label)}</td>"
            f"<td>{_sparkline(values)}</td>"
            f"<td>{_fmt(first)}{suffix}</td>"
            f"<td>{_fmt(last)}{suffix}</td>"
            f"<td>{_fmt(statistics.median(numbers))}{suffix}</td>"
            f"<td>{_fmt(max(numbers))}{suffix}</td>"
            "</tr>"
        )
    if not rows:
        return ""
    return (
        f'<details class="detail-table" open><summary>逐轮趋势（共 {len(rounds)} 轮）</summary>'
        '<table class="trend"><tr><th>指标</th><th>趋势（第 1 → N 轮）</th>'
        "<th>首轮</th><th>末轮</th><th>中位</th><th>最差</th></tr>"
        + "".join(rows)
        + "</table>"
        '<div class="hint">折线断点表示该轮未采到值（失败/跳过），不是 0；'
        "灰色圆点为首轮、蓝色为末轮。</div></details>"
    )


def _fmt_unit(value: Any, unit: str) -> str:
    """数值 + 单位；缺失值只显示 ``—``（不带单位，避免"——%" 这种半成品）。"""
    return "—" if value is None else f"{_fmt(value)}{unit}"


def _peak_avg_pair(peak: Any, avg: Any, unit: str, delta: Any = None) -> str:
    """「峰 / 均」成对显示；``delta`` 非空时再补一个相对基线差值。

    报告列宽有限，峰/均拆两列会把表格撑到不可读；成对写还顺带表达了
    "峰值在前、均值在后" 的固定读法（表头已声明）。
    """
    text = f"{_fmt_unit(peak, unit)} / {_fmt_unit(avg, unit)}"
    if delta is None:
        return text
    return f"{text}（{'+' if isinstance(delta, (int, float)) and delta > 0 else ''}{_fmt_unit(delta, unit)}）"


_L3_BADGES = {
    "pass": ("通过", "#2f9e6f"),
    "fail": ("未通过", "#d93025"),
    "unknown": ("未测到", "#8a94a6"),
    "skipped": ("已关闭", "#8a94a6"),
}


def _l3_badge(verdict: Any) -> str:
    """LLM 判定三态徽章（``unknown`` 必须与 pass/fail 视觉可区分）。"""
    text, color = _L3_BADGES.get(str(verdict or "").strip(), ("—", "#8a94a6"))
    return f'<span class="badge" style="background:{color}">{text}</span>'


def _round_scenario_field(round_obj: Any, scenario: str, path: str) -> Optional[float]:
    """从 ``round.scenarios[scenario]`` 按点路径取一个数值（缺失返回 ``None``）。

    为什么需要：逐轮折线要的是**每一轮的真值**，而聚合字段（``case.aggregate``）只有
    跨轮统计量。两者来源同一份场景结果，只是粒度不同——不是两套口径。
    """
    node: Any = (getattr(round_obj, "scenarios", {}) or {}).get(scenario)
    for part in str(path).split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        return float(node)
    return None


def _page_render_scenario(rounds: list[Any]) -> str:
    """探测承载「页面渲染」数据的场景名（payload 里带非空 ``pages`` 映射的那个）。

    具体场景名（如 ``page_render``）与页面集合都是 **app 知识**，core 不写死：
    按数据形状识别，新增 app 无需改 core。
    """
    for round_obj in rounds:
        scenarios = getattr(round_obj, "scenarios", {}) or {}
        if not isinstance(scenarios, dict):
            continue
        for name, payload in scenarios.items():
            if isinstance(payload, dict) and isinstance(payload.get("pages"), dict) and payload["pages"]:
                return str(name)
    return ""


def _page_render_pages(rounds: list[Any], scenario: str = "") -> list[tuple[str, str]]:
    """收集本档出现过的页面 ``(key, label)``（按首次出现顺序去重）。"""
    scenario = scenario or _page_render_scenario(rounds)
    seen: dict[str, str] = {}
    if not scenario:
        return []
    for round_obj in rounds:
        pages = ((getattr(round_obj, "scenarios", {}) or {}).get(scenario) or {}).get("pages") or {}
        if not isinstance(pages, dict):
            continue
        for key, payload in pages.items():
            if key in seen:
                continue
            label = str((payload or {}).get("label") or key) if isinstance(payload, dict) else key
            seen[key] = label
    return list(seen.items())


def _page_render_charts(case: Any, scenario: str = "") -> str:
    """按**测试次数**（横坐标）画三张折线图：启动加载期 CPU / 内存、页面渲染耗时。

    数据来源：逐轮 ``round.scenarios[scenario]``（点路径）。断点=该轮无有效数据，不补 0。
    """
    from uita.core.perf.chart import ChartSeries, render_line_chart

    rounds = list(getattr(case, "rounds", []) or [])
    scenario = scenario or _page_render_scenario(rounds)
    pages = _page_render_pages(rounds, scenario)
    series: list[str] = []
    if not scenario:
        return ""

    payloads = [
        ((getattr(r, "scenarios", {}) or {}).get(scenario) or {}) for r in rounds
    ]
    if not any(isinstance(p, dict) and p.get("startup_loading") for p in payloads):
        return ""
    if len(rounds) < 2:
        # 单轮没有「随测试次数变化」可言：给明确提示，而不是留一段空白让人以为图挂了
        return (
            '<div class="hint">仅 1 轮，无法成图（横坐标 = 测试次数，'
            "需 <code>--perf-repeat</code> ≥ 2）；该轮实测值见下方明细表。</div>"
        )

    def _col(path: str) -> list[Optional[float]]:
        return [_round_scenario_field(r, scenario, path) for r in rounds]

    cpu_peak, cpu_avg = _col("startup_loading.cpu_peak"), _col("startup_loading.cpu_avg")
    rss_peak, rss_avg = _col("startup_loading.rss_peak_mb"), _col("startup_loading.rss_avg_mb")
    if any(v is not None for v in cpu_peak + cpu_avg):
        series.append(render_line_chart(
            [ChartSeries("CPU 峰值", cpu_peak, unit="%"),
             ChartSeries("CPU 均值", cpu_avg, unit="%")],
            title="启动加载期 CPU（启动 → CPU 回落前）", y_unit="%",
        ))
    if any(v is not None for v in rss_peak + rss_avg):
        series.append(render_line_chart(
            [ChartSeries("内存峰值", rss_peak, unit="MB"),
             ChartSeries("内存均值", rss_avg, unit="MB")],
            title="启动加载期内存（启动 → CPU 回落前）", y_unit="MB",
        ))
    render_series = [
        ChartSeries(label, _col(f"pages.{key}.render_ms"), unit="ms") for key, label in pages
    ]
    if any(any(v is not None for v in item.values) for item in render_series):
        series.append(render_line_chart(
            render_series, title="页面渲染耗时（点击完成 → 指纹首命中）", y_unit="ms",
        ))
    return "".join(series)


#: 「启动 → 点击」曲线里单条线最多点数（500ms 采样 = 200s 覆盖；超出等间隔保形抽稀）。
_WARMUP_CURVE_MAX_POINTS = 400


def _read_round_samples(
    round_obj: Any, cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """读本轮的原始采样点（按文件路径缓存：同一轮多页只解析一次）。"""
    path = str(getattr(round_obj, "samples_jsonl", "") or "")
    if not path:
        return []
    if path in cache:
        return cache[path]
    from uita.core.perf.series import read_samples_jsonl

    rows: list[dict[str, Any]] = []
    try:
        rows = read_samples_jsonl(path)
    except Exception as exc:  # noqa: BLE001 - 辅助产物读失败只降级该图，不影响整份报告
        logger.debug("[perf] 读取采样点失败（%s）: %s", path, exc)
    cache[path] = rows
    return rows


def _decimate_points(
    points: list[tuple[int, float]], limit: int = _WARMUP_CURVE_MAX_POINTS,
) -> list[tuple[int, float]]:
    """等间隔保形抽稀（首末点必留）：控制单条线的 SVG 体积，不改形态。"""
    if limit <= 2 or len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    picked = sorted({int(round(index * step)) for index in range(limit)})
    return [points[index] for index in picked]


def _round_page_windows(round_obj: Any, scenario: str) -> list[dict[str, Any]]:
    """本轮每页的「启动 → 点击」窗口 ``[t0, t1)`` 与不可用原因。

    - ``t0`` = 该页**冷启动发起时刻**：第 1 页用本轮启动起点（``t_launch_start_ms``），
      第 2 页起用页间重启的 ``relaunch.launch_start_ms``（缺则回落本轮启动起点）；
    - ``t1`` = 该页**点击动作开始时刻**（``pages.<key>.click.start_ms``）。
    任一缺失/无效时给 ``reason``，由报告如实写出，不出空图也不补 0。
    """
    payload = ((getattr(round_obj, "scenarios", {}) or {}).get(scenario) or {})
    pages = payload.get("pages") or {}
    if not isinstance(pages, dict):
        return []
    round_launch = int(getattr(round_obj, "t_launch_start_ms", 0) or 0)
    out: list[dict[str, Any]] = []
    for key, page in pages.items():
        if not isinstance(page, dict):
            continue
        label = str(page.get("label") or key)
        click = page.get("click") or {}
        relaunch = page.get("relaunch") or {}
        t0 = relaunch.get("launch_start_ms") or round_launch
        t1 = click.get("start_ms")
        reason = ""
        if str(page.get("reason") or "") == "relaunch_failed":
            reason = "页间重启失败，该页未点击"
        elif not isinstance(t0, (int, float)) or isinstance(t0, bool) or t0 <= 0:
            reason = "缺少该页冷启动时刻"
        elif not isinstance(t1, (int, float)) or isinstance(t1, bool) or t1 <= 0:
            reason = "缺少点击开始时刻（旧产物未记录）"
        elif t1 <= t0:
            reason = "窗口无效（点击早于启动）"
        out.append({
            "key": str(key), "label": label,
            "t0": int(t0 or 0), "t1": int(t1 or 0), "reason": reason,
        })
    return out


def _sample_ms_values(rounds: list[Any]) -> list[int]:
    """各轮注入的客户端预热窗口 ``sample_ms``（去重升序；未注入时为空）。"""
    values: list[int] = []
    for round_obj in rounds:
        pre_round = getattr(round_obj, "pre_round", None) or {}
        warmup = pre_round.get("warmup") if isinstance(pre_round, dict) else None
        value = (warmup or {}).get("sample_ms") if isinstance(warmup, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and int(value) > 0:
            if int(value) not in values:
                values.append(int(value))
    return sorted(values)


def _warmup_series(
    rounds: list[Any], scenario: str,
) -> dict[str, dict[str, Any]]:
    """按页面汇总「启动 → 点击」窗口内的采样序列（每轮一条线）。

    Returns:
        ``{页面键: {"label", "lines": [{"name", "cpu", "rss"}], "windows": [[t0,t1]],
        "reasons": [<为什么这一轮/这一页没有曲线>]}}``；按首次出现顺序。
    """
    from uita.core.perf.series import select_window

    cache: dict[str, list[dict[str, Any]]] = {}
    pages: dict[str, dict[str, Any]] = {}
    if not scenario:
        return pages
    for round_obj in rounds:
        index = int(getattr(round_obj, "round_index", 0) or 0)
        tag = f"第 {index} 轮" if index else "某轮"
        rows: Optional[list[dict[str, Any]]] = None
        for window in _round_page_windows(round_obj, scenario):
            entry = pages.setdefault(
                window["key"],
                {"label": window["label"], "lines": [], "windows": [], "reasons": []},
            )
            if window["reason"]:
                entry["reasons"].append(f"{tag}：{window['reason']}")
                continue
            if rows is None:
                rows = _read_round_samples(round_obj, cache)
            if not rows:
                entry["reasons"].append(
                    f"{tag}：无原始采样文件（samples/*.jsonl 缺失或本轮未采样）",
                )
                continue
            t0, t1 = int(window["t0"]), int(window["t1"])
            points = select_window(rows, t0, t1)
            cpu: list[tuple[int, float]] = []
            rss: list[tuple[int, float]] = []
            for row in points:
                t_ms = row.get("t_ms")
                if not isinstance(t_ms, (int, float)) or isinstance(t_ms, bool):
                    continue
                cpu_value = row.get("cpu_percent")
                if isinstance(cpu_value, (int, float)) and not isinstance(cpu_value, bool):
                    cpu.append((int(t_ms) - t0, float(cpu_value)))
                rss_value = row.get("rss_mb")
                if isinstance(rss_value, (int, float)) and not isinstance(rss_value, bool):
                    rss.append((int(t_ms) - t0, float(rss_value)))
            if len(cpu) < 2 and len(rss) < 2:
                entry["reasons"].append(
                    f"{tag}：窗口内有效采样点不足 2 个（{len(points)} 个采样行）",
                )
                continue
            entry["lines"].append({
                "name": tag,
                "cpu": _decimate_points(cpu),
                "rss": _decimate_points(rss),
            })
            entry["windows"].append([t0, t1])
    return pages


def _warmup_curves_block(result: Any, *, tier_label: str = "") -> str:
    """「QMUI 预热期资源采样曲线」：每页 CPU / 内存各一张（每条线 = 一轮）。

    窗口固定为**该页「启动 → 点击」**（点击模块列表 / 游戏中心），横轴 = 距启动时刻。
    数据源是每轮已落盘的 ``samples/*.jsonl``（与 ``perf_summary.json`` 的峰值/均值同源），
    因此不新增任何 payload 字段；读不到就如实写原因。
    """
    rounds: list[Any] = []
    for case in (getattr(result, "cases", None) or []):
        rounds.extend(getattr(case, "rounds", None) or [])
    if not rounds:
        return ""
    scenario = _page_render_scenario(rounds)
    pages = _warmup_series(rounds, scenario)
    if not pages:
        return ""
    from uita.core.perf.curve import render_time_curves

    interval = getattr(result, "sample_interval_ms", None)
    interval_text = (
        f"{int(interval)}ms"
        if isinstance(interval, (int, float)) and not isinstance(interval, bool) and interval > 0
        else "间隔未记录"
    )
    sample_ms = _sample_ms_values(rounds)
    prefix = f"{tier_label} · " if tier_label else ""
    blocks: list[str] = []
    for _key, entry in pages.items():
        label = str(entry.get("label") or _key)
        head = html.escape(f"{prefix}{label}")
        windows = entry.get("windows") or []
        span_text = ""
        if windows:
            durations = sorted(round((t1 - t0) / 1000.0, 1) for t0, t1 in windows)
            span_text = (
                f"窗口 {durations[0]}s"
                if len(durations) == 1
                else f"窗口 {durations[0]}–{durations[-1]}s"
            )
        meta_bits = [f"进程树 qmui", f"采样间隔 {interval_text}"]
        if span_text:
            meta_bits.append(span_text)
        if sample_ms:
            meta_bits.append("客户端预热窗口 sample_ms=" + " / ".join(str(v) for v in sample_ms))
        blocks.append(f"<h5>{head}：启动 → 点击</h5>")
        if entry.get("lines"):
            cpu_lines = [{"name": line["name"], "points": line["cpu"]} for line in entry["lines"]]
            rss_lines = [{"name": line["name"], "points": line["rss"]} for line in entry["lines"]]
            blocks.append(render_time_curves(
                cpu_lines,
                title=f"{prefix}{label} 预热期 CPU 占用（{'，'.join(meta_bits)}）",
                y_unit="%",
            ))
            blocks.append(render_time_curves(
                rss_lines,
                title=f"{prefix}{label} 预热期内存占用（{'，'.join(meta_bits)}）",
                y_unit="MB",
            ))
            blocks.append(
                '<div class="hint">横轴 = 距该页冷启动的时长；每条线 = 一轮；'
                "窗口终点 = 点击动作开始时刻（不含页面加载过程）。</div>",
            )
        reasons = entry.get("reasons") or []
        if reasons:
            blocks.append(
                '<div class="note muted">未出图：' + html.escape(" ｜ ".join(reasons)) + "</div>",
            )
    return "".join(blocks)


def _warmup_curves_section(tiers: list[tuple[str, Any]], tier_names: list[str]) -> str:
    """多挡位报告的曲线区块（每挡位一组：页 × CPU/内存）。"""
    blocks: list[str] = []
    for label, (_name, result) in zip(tier_names, tiers):
        block = _warmup_curves_block(result, tier_label=label)
        if block:
            blocks.append(block)
    return "".join(blocks)


def _page_render_table(case: Any, scenario: str = "") -> str:
    """每页每轮明细（render_ms + 加载窗口资源 + L3 三态）。"""
    rounds = list(getattr(case, "rounds", []) or [])
    scenario = scenario or _page_render_scenario(rounds)
    pages = _page_render_pages(rounds, scenario)
    if not pages:
        return ""
    rows: list[str] = []
    for round_obj in rounds:
        payload = ((getattr(round_obj, "scenarios", {}) or {}).get(scenario) or {})
        page_map = payload.get("pages") or {}
        startup = payload.get("startup_loading") or {}
        for key, label in pages:
            page = page_map.get(key) or {}
            fingerprint = page.get("fingerprint") or {}
            l3 = page.get("l3") or {}
            resources = page.get("resources") or {}
            pre_click = resources.get("pre_click") or {}
            loading = resources.get("loading") or {}
            delta = resources.get("delta") or {}
            render_ms = page.get("render_ms")
            status = "ok" if page.get("ok") else str(page.get("reason") or "未执行")
            rows.append(
                "<tr>"
                f"<td>{html.escape(str(getattr(round_obj, 'round_index', 0) + 1))}</td>"
                f"<td>{html.escape(label)}</td>"
                f"<td>{_fmt_unit(render_ms, '')}</td>"
                f"<td>{_peak_avg_pair(startup.get('cpu_peak'), startup.get('cpu_avg'), '%')}</td>"
                f"<td>{_peak_avg_pair(startup.get('rss_peak_mb'), startup.get('rss_avg_mb'), ' MB')}</td>"
                f"<td>{_fmt_unit(pre_click.get('cpu_avg'), '%')}</td>"
                f"<td>{_peak_avg_pair(loading.get('cpu_peak'), loading.get('cpu_avg'), '%')}</td>"
                f"<td>{_peak_avg_pair(loading.get('rss_peak_mb'), loading.get('rss_avg_mb'), ' MB', delta.get('rss_mb'))}</td>"
                f"<td>{html.escape(str(fingerprint.get('template') or '—'))}</td>"
                f"<td>{_l3_badge(l3.get('verdict'))}</td>"
                f"<td>{html.escape(status)}</td>"
                "</tr>"
            )
    if not rows:
        return ""
    return (
        '<h3>页面渲染明细（逐轮）</h3>'
        '<table class="page-render"><tr><th>轮次</th><th>页面</th><th>渲染耗时(ms)</th>'
        "<th>启动加载期 CPU 峰/均</th><th>启动加载期 RSS 峰/均</th><th>点击前 CPU 均值</th>"
        "<th>单页加载 CPU 峰/均</th><th>单页加载 RSS 峰/均（Δ vs 点击前）</th>"
        "<th>命中指纹</th><th>LLM 判定</th><th>状态</th></tr>"
        + "".join(rows)
        + "</table>"
        '<div class="hint">渲染耗时 = 点击完成 → 视觉指纹首命中（采样/截图粒度约 ±150ms）；'
        "启动加载期窗口 = 启动 → CPU 回落（拐点超时则退到首次点击时刻）；"
        "LLM 判定为截图加载完成的独立校验，<b>unknown</b> 表示没测到（不算通过也不算失败）；"
        "<b>已关闭</b> 表示该批次用指纹作为唯一完成判据（未发起 LLM 判定）。</div>"
    )


def _case_section(case: Any, result: Any = None) -> str:
    """单档报告里的一个用例块：总体健康度 + 分场景关键指标 + 该用例异常。"""
    agg = case.aggregate or {}
    scenarios = agg.get("scenarios") or {}
    parts = [f"<h3>总体健康度</h3>{_health_single(_stats_view(agg))}"]
    if scenarios:
        blocks = "".join(
            _scenario_single(name, scenarios[name] or {}, agg) for name in scenarios
        )
        parts.append(f"<h3>分场景关键指标</h3>{blocks}")
    # 页面渲染场景：按测试次数（横坐标）画正式折线图（默认恒开，≥2 轮才出）
    charts = _page_render_charts(case)
    if charts:
        parts.append(f"<h3>逐轮折线图</h3>{charts}")
    # 逐挡位采样曲线（单档同样出）：固定窗口「启动 → 点击」内的进程树 CPU/内存过程形态
    if result is not None:
        warmup = _warmup_curves_block(result)
        if warmup:
            parts.append(f"<h3>QMUI 预热期资源采样曲线（启动 → 点击）</h3>{warmup}")
    page_table = _page_render_table(case)
    if page_table:
        parts.append(page_table)
    # 长跑（如 500 次启动循环）看的是"随轮次是否劣化"——聚合均值答不了，这里给逐轮折线
    trend = _trend_block(case)
    if trend:
        parts.append(trend)
    launch_note = _launch_failure_note(getattr(case, "rounds", None))
    if launch_note:
        parts.append(launch_note)
    errors = [r.error for r in (getattr(case, "rounds", []) or []) if r.error]
    if errors:
        parts.append(
            '<div class="note err"><b>轮次异常</b><pre>'
            + html.escape("\n".join(str(e) for e in errors)) + "</pre></div>"
        )
    return (
        f'<section class="case"><h2>{html.escape(case.case_name)} '
        f"{_badge((case.verdict or {}).get('status', 'unknown'))}</h2>"
        f"{''.join(parts)}</section>"
    )


def _render_result_section(result: Any, *, anchor: str = "") -> str:
    """渲染单档报告主体（不含 doctype/head/style）；``anchor`` 非空时包一层可跳转的 section。"""
    cases = list(getattr(result, "cases", []) or [])
    spec = ((getattr(result, "host", None) or {}).get("spec") or {})
    meta_bits = [
        f'平台 <b>{html.escape(_platform_title(result.platform))}</b>',
        (f'机器 <b>{html.escape(str(spec.get("label")))}</b>' if spec.get("label") else ""),
        f'轮次 <b>{result.repeat}</b>',
        f"冷启动 <b>{'是' if result.cold_start else '否'}</b>",
        f'采样间隔 <b>{result.sample_interval_ms}ms</b>',
        f'预设 <b>{html.escape(result.preset_name or "（命令行指定）")}</b>',
        f'时间 <b>{html.escape(str(result.started_at))}</b> → <b>{html.escape(str(result.finished_at))}</b>',
        f'run_id <code>{html.escape(str(result.run_id))}</code>',
        (f'git <code>{html.escape(str(result.git_rev))}</code>' if result.git_rev else ""),
    ]
    body = (
        f'<h1>UITA 客户端性能报告 — {html.escape(str(result.app))} {_badge(_overall_status(result))}</h1>'
        f'<div class="meta">{" · ".join(b for b in meta_bits if b)}</div>'
        f"{_reading_note(int(getattr(result, 'repeat', 0) or 0))}"
        + (
            "".join(_case_section(c, result) for c in cases)
            if cases else '<section class="case">无用例结果</section>'
        )
        + _notes_block(result)
        + _footer_note()
    )
    if anchor:
        return f'\n  <section id="{html.escape(anchor)}" class="tier-section">\n{body}\n  </section>'
    return body


def _html(result: Any) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>UITA 客户端性能报告 — {html.escape(str(result.app))}</title>
<style>{_STYLE}</style>
</head>
<body>
{_render_result_section(result)}
</body>
</html>
"""
