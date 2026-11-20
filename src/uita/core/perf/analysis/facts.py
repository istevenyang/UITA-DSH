"""perf AI 分析：事实层（遍历式覆盖全部数据，作为数值锚与覆盖校验基准）。

存在理由（用户口径 2026-10-15："**所有的数据都要有分析结论**（因为增加了数据采集）"）：

  1. **数值锚**：分析 agent 直写 HTML 时，报告里的每个读数都必须来自本文件产出的 fact
     （`fact_id` 稳定、`display` 是与框架报告同一套格式化口径的字符串），从而把
     "数字来自框架、文字来自模型" 这条界线钉死；
  2. **覆盖基准**：每条 fact 都要在报告里出现（`data-fact-id="<id>"`），
     `analysis/verify.py` 据此算「结论覆盖 N/M（缺口 K）」，缺口显式可见（用户口径：显式化但不阻断）；
  3. **口径携带**：`calibers`（`analysis/calibers.py`）随 facts 一起交付，避免模型自己发明口径。

设计取舍：

  - **遍历式**而非白名单式：新增场景 / 新增 `REPORT_KEYS` 字段 / 新增 raw 字段**自动纳入**覆盖，
    不需要改本文件（"所有数据都有结论"才可能长期成立）；
  - **复用 `core/perf/report.py` 的口径函数**（`_HEALTH_METRICS` / `_stats_view` / `_num` /
    `_report_keys` / `_missing_reason` / `_round_attempts` …）：报告与分析报告对同一数据的读法
    必须逐字一致，绝不允许两套格式化实现（出现"框架报告 4,918、分析报告 4918.3"这种事故）；
  - `required` 分级（两级覆盖，避免"分母大到没人能写全 → 缺口清单全是噪声"）：
    · **必覆盖层**（计入覆盖分母）：总体健康度 / 场景指标 / 场景资源 / 采集状态与缺失原因 /
      轮级标量 / 启动失败现场 / 逐级明细 / 主机基准——这些是 1013 报告里**逐格给结论**的数据；
    · **参考层**（不计入分母，但完整给出）：逐轮场景原始读数（`scenario_raw`，可达数百项）、
      元信息、与上一批次对比——它们是**证据**，报告按需引用即可（原始值已随
      `perf_summary.json` / `perf_metrics.csv` 上云）。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Iterable, Optional

from uita.core.perf.analysis.calibers import load_calibers
from uita.core.perf.report import (
    _FAILURE_FIELDS,
    _HEALTH_METRICS,
    _KEY_FACTS,
    _dedupe_errors,
    _detail_rows,
    _infer_unit,
    _launch_attempt_rows,
    _missing_reason,
    _num,
    _platform_title,
    _report_detail,
    _report_keys,
    _scenario_label,
    _stats_view,
    _tier_title,
)

logger = logging.getLogger("uita")

__all__ = [
    "FACTS_SCHEMA_VERSION",
    "collect_facts",
    "find_previous_run",
    "render_facts_md",
    "write_facts",
]

FACTS_SCHEMA_VERSION = "1.0"

#: 计入覆盖分母的 fact 范围（必覆盖层；其余为"参考层"，完整给出但不计入分母）
_REQUIRED_SCOPES = frozenset({
    "health", "metric", "resource", "status", "round", "launch", "detail", "host",
})


def _now_iso() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def _tier_name(name: Any) -> str:
    """档位名规范化：去掉 ``tier-`` 前缀并统一 ``0k`` → ``0K``（与报告列名逐字一致）。

    容错原因：调用方可能传目录名（``tier-10k``）也可能传单元名（``10k``）——
    两者都出现过（``_handle_perf_multi_tier`` 传单元名、手工调试常传目录名）。
    """
    text = str(name or "").strip()
    if text.lower().startswith("tier-"):
        text = text[5:]
    return _tier_title(text)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _display(value: Any, unit: str = "") -> str:
    """与框架报告同一套数字格式化（``report._num``），"—" 语义完全一致。"""
    return _num(value, unit)


def _stat_display(stat: Any, unit: str = "") -> str:
    """统计结构 → ``均值 / 最小值 / 最大值``（与报告 ``_triple`` 同口径）。"""
    if isinstance(stat, dict):
        if stat.get("min") is not None and stat.get("avg") is not None:
            return f"{_num(stat.get('avg'), unit)} / {_num(stat.get('min'), unit)} / {_num(stat.get('max'), unit)}"
        if stat.get("avg") is not None or stat.get("max") is not None:
            return f"{_num(stat.get('avg'), unit)} / {_num(stat.get('max'), unit)}"
        return "—"
    return _display(stat, unit)


def _scalar_display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if _is_number(value):
        return _display(value)
    return str(value)


class _FactBuilder:
    """fact 累积器：统一 id 前缀、required 计算与去重。"""

    def __init__(self, *, multi_tier: bool) -> None:
        self.multi_tier = multi_tier
        self.items: list[dict[str, Any]] = []
        self._seen: set[str] = set()

    def add(
        self, path: str, *, scope: str, tier: str = "", zh: str = "", unit: str = "",
        value: Any = None, stat: Any = None, display: str = "", data_status: str = "ok",
        gap: str = "", caliber_id: str = "", scenario: str = "", case: str = "",
        round_index: Optional[int] = None, attempt: Optional[int] = None,
        numbers: Optional[dict[str, Any]] = None, extra: Optional[dict[str, Any]] = None,
    ) -> None:
        fact_id = f"{tier}/{path}" if (self.multi_tier and tier) else path
        if fact_id in self._seen:
            return
        self._seen.add(fact_id)
        item: dict[str, Any] = {
            "fact_id": fact_id,
            "scope": scope,
            "required": scope in _REQUIRED_SCOPES,
            "zh": zh or path,
            "unit": unit or "",
            "display": display,
            "data_status": data_status,
        }
        if tier:
            item["tier"] = tier
        if scenario:
            item["scenario"] = scenario
        if case:
            item["case"] = case
        if round_index is not None:
            item["round"] = round_index
        if attempt is not None:
            item["attempt"] = attempt
        if value is not None:
            item["value"] = value
        if stat is not None:
            item["stat"] = stat
        if gap:
            item["gap"] = gap
        if caliber_id:
            item["caliber_id"] = caliber_id
        if numbers:
            item["numbers"] = {k: v for k, v in numbers.items() if _is_number(v)}
        if extra:
            item.update(extra)
        self.items.append(item)

    def coverage(self) -> dict[str, Any]:
        required = [i for i in self.items if i.get("required")]
        by_status: dict[str, int] = {}
        by_scope: dict[str, int] = {}
        required_by_scope: dict[str, int] = {}
        for item in self.items:
            by_status[item["data_status"]] = by_status.get(item["data_status"], 0) + 1
            by_scope[item["scope"]] = by_scope.get(item["scope"], 0) + 1
            if item.get("required"):
                required_by_scope[item["scope"]] = required_by_scope.get(item["scope"], 0) + 1
        return {
            "total": len(self.items),
            "required": len(required),
            "optional": len(self.items) - len(required),
            "by_status": by_status,
            "by_scope": by_scope,
            "required_by_scope": required_by_scope,
        }


def _case_rounds(result: Any) -> list[tuple[str, list[Any]]]:
    out: list[tuple[str, list[Any]]] = []
    for case in (getattr(result, "cases", None) or []):
        out.append((str(getattr(case, "case_name", "") or ""), list(getattr(case, "rounds", None) or [])))
    return out


def _add_meta(builder: _FactBuilder, tiers: list[tuple[str, Any]], *, app_name: str) -> dict[str, Any]:
    """元信息 + 主机基准（主机基准计入覆盖：报告「数据源与可比性」必须给机器算力）。"""
    first = tiers[0][1] if tiers else None
    host = dict(getattr(first, "host", None) or {}) if first is not None else {}
    spec = dict(host.get("spec") or {})
    raw = dict(host.get("raw") or {})
    scores = dict(host.get("scores") or {})

    builder.add("meta/app", scope="meta", zh="应用", display=str(app_name or getattr(first, "app", "") or ""))
    builder.add("meta/platform", scope="meta", zh="平台",
                display=_platform_title(getattr(first, "platform", "") if first is not None else ""))
    builder.add("meta/git_rev", scope="meta", zh="构建版本（git）",
                display=str(getattr(first, "git_rev", "") or ""))
    builder.add("meta/rounds", scope="meta", zh="每档轮次", value=getattr(first, "repeat", None),
                display=_scalar_display(getattr(first, "repeat", None)))
    builder.add("meta/preset", scope="meta", zh="预设",
                display=str(getattr(first, "preset_name", "") or "（命令行指定）"))
    builder.add("meta/started_at", scope="meta", zh="批次起始", display=str(getattr(first, "started_at", "") or ""))
    builder.add("meta/finished_at", scope="meta", zh="批次结束",
                display=str(getattr(tiers[-1][1], "finished_at", "") or "") if tiers else "")

    if host:
        # 主机基准：**遍历式**采集（字段随 `host_bench` 演进自动纳入，不写死键名——
        # 2026-10-15 就踩过 `mem_mb_s` 与真实键 `memory_mb_s` 不一致导致整组事实缺失）。
        builder.add("host/spec", scope="host", zh="主机规格", display=str(spec.get("label") or ""),
                    caliber_id="host_bench")
        for key, value in spec.items():
            if isinstance(value, (dict, list)) or value in (None, ""):
                continue
            unit = "GB" if str(key).endswith("_gb") else ""
            builder.add(f"host/spec/{key}", scope="host", zh=f"主机规格：{key}", unit=unit,
                        value=value if _is_number(value) else None, display=_scalar_display(value),
                        caliber_id="host_bench",
                        numbers={key: value} if _is_number(value) else None)
        for key, value in raw.items():
            if isinstance(value, (dict, list)) or value in (None, ""):
                continue
            unit = "MB/s" if str(key).endswith("_mb_s") else ("GFLOPS" if str(key).endswith("gflops") else "")
            builder.add(f"host/raw/{key}", scope="host", zh=f"主机基准：{key}", unit=unit,
                        value=value if _is_number(value) else None, display=_display(value, unit),
                        caliber_id="host_bench",
                        numbers={key: value} if _is_number(value) else None)
        for key, value in scores.items():
            builder.add(f"host/score/{key}", scope="host", zh=f"归一化分（{key}）",
                        value=value if _is_number(value) else None, display=_scalar_display(value),
                        caliber_id="host_bench",
                        numbers={key: value} if _is_number(value) else None)
        composite = host.get("composite_score")
        if composite is not None:
            builder.add("host/composite_score", scope="host", zh="综合算力分", value=composite,
                        display=_display(composite), caliber_id="host_bench",
                        numbers={"composite_score": composite})
        # 分母：系统总内存（内存占比结论的必需分母；键名各版本不一，故兼容取第一个命中的）
        total_mb = None
        for key in ("system_mem_total_mb", "total_mem_mb", "mem_total_mb"):
            if _is_number(raw.get(key)):
                total_mb = raw.get(key)
                break
        if total_mb is None and _is_number(spec.get("memory_gb")):
            total_mb = float(spec["memory_gb"]) * 1024.0
        if total_mb is not None:
            builder.add("host/system_mem_total_mb", scope="host", zh="系统总内存", unit="MB",
                        value=total_mb, display=_display(total_mb, "MB"), caliber_id="rss_scope",
                        numbers={"system_mem_total_mb": total_mb})
    return {"host": host, "spec": spec}


def _per_tier_aggregates_local(
    tier_list: list[tuple[str, Any]], tier_names: list[str],
) -> dict[str, dict[str, Any]]:
    """档位 → 主用例聚合（**按已规范化的档位名索引**）。

    不能直接复用 ``report._per_tier_aggregates``：它内部再跑一次 ``_tier_title``，
    与这里的 ``_tier_name`` 规范化叠加后可能不一致（``tier-10k`` 这类目录名会错位），
    导致整组场景事实凭空消失（静默缺数据 = 最危险的失败模式）。
    """
    out: dict[str, dict[str, Any]] = {}
    for name, result in zip(tier_names, [r for _, r in tier_list]):
        cases = list(getattr(result, "cases", None) or [])
        out[name] = dict(getattr(cases[0], "aggregate", None) or {}) if cases else {}
    return out


def _add_health(builder: _FactBuilder, per_tier: dict[str, dict[str, Any]], tier_names: list[str]) -> None:
    for tier in tier_names:
        agg = per_tier.get(tier) or {}
        view = _stats_view(agg)
        for label, key, unit in _HEALTH_METRICS:
            stat = view.get(key)
            builder.add(f"health/{key}", scope="health", tier=tier, zh=label, unit=unit, stat=stat,
                        display=_stat_display(stat, unit), caliber_id="stat_triple",
                        data_status="ok" if _has_stat(stat) else "missing")


def _has_stat(stat: Any) -> bool:
    if isinstance(stat, dict):
        return stat.get("avg") is not None or stat.get("max") is not None
    return _is_number(stat)


def _status_of(block: dict[str, Any], name: str, reason: str) -> str:
    """缺失原因 → 分类标签（``report._missing_reason`` 的措辞就是分类依据，直接对齐它）。

    分类口径必须与报告一致：报告用「已跳过（…）」「不可测（…）」区分"没跑"与"测不了"，
    事实层若自行判断（只看 ``unsupported`` 标志）就会把"不可测"错标成"missing"，
    进而让 AI 把可解释的缺失写成"探针异常"。
    """
    if not reason:
        return "ok"
    if reason.startswith("已跳过") or "已跳过（" in reason:
        return "skipped"
    if reason.startswith("不可测") or "不可测（" in reason:
        return "unsupported"
    if _dedupe_errors(block.get("errors")):
        return "failed"
    return "missing"


def _add_scenarios(builder: _FactBuilder, per_tier: dict[str, dict[str, Any]], tier_names: list[str]) -> list[str]:
    """场景层：状态 + 白名单指标 + 资源行 + 逐级明细。返回场景执行顺序（保持各档出现顺序）。"""
    order: list[str] = []
    for tier in tier_names:
        agg = per_tier.get(tier) or {}
        for name, block in (agg.get("scenarios") or {}).items():
            if name not in order:
                order.append(name)
            block = block if isinstance(block, dict) else {}
            label = _scenario_label(name)
            reason = _missing_reason(block, name)
            builder.add(
                f"scenario/{name}/status", scope="status", tier=tier, scenario=name,
                zh=f"{label}：本轮采集状态", display=reason or "已采集",
                data_status=_status_of(block, name, reason), gap=reason,
            )
            keys = _report_keys(name)
            for key, zh in keys.items():
                stat = block.get(key)
                unit = _infer_unit(key)
                builder.add(f"scenario/{name}/{key}", scope="metric", tier=tier, scenario=name,
                            zh=zh, unit=unit, stat=stat if isinstance(stat, dict) else None,
                            value=None if isinstance(stat, dict) else stat,
                            display=_stat_display(stat, unit), caliber_id="stat_triple",
                            data_status="ok" if _has_stat(stat) else "missing",
                            gap=None if _has_stat(stat) else reason,
                            numbers={"avg": (stat or {}).get("avg") if isinstance(stat, dict) else stat,
                                     "max": (stat or {}).get("max") if isinstance(stat, dict) else stat})
            if not _covers_resources(name, keys):
                cpu = _metric_max(block, f"{name}_cpu_percent_avg")
                mem = _metric_max(block, f"{name}_rss_mb_peak")
                builder.add(f"scenario/{name}/resource", scope="resource", tier=tier, scenario=name,
                            zh=f"{label}：CPU 均值 / 内存占用峰值", display=f"{_num(cpu, '%')} / {_num(mem, 'MB')}",
                            numbers={"cpu_percent": cpu, "rss_mb": mem},
                            data_status="ok" if (cpu is not None or mem is not None) else "missing",
                            gap=None if (cpu is not None or mem is not None) else reason,
                            caliber_id="rss_scope")
            detail = _report_detail(name)
            if detail:
                for row in _detail_rows(block, detail):
                    row_key = row.get(detail.get("key")) if detail.get("key") else None
                    for field, zh in detail["fields"].items():
                        value = row.get(field)
                        unit = _infer_unit(field)
                        path = f"scenario/{name}/detail/{row_key}/{field}"
                        builder.add(path, scope="detail", tier=tier, scenario=name, zh=zh, unit=unit,
                                    stat=value if isinstance(value, dict) else None,
                                    value=None if isinstance(value, dict) else value,
                                    display=_stat_display(value, unit),
                                    data_status="ok" if (value is not None and value != "") else "missing")
    return order


def _metric_max(block: dict[str, Any], key: str) -> Any:
    value = block.get(key)
    if isinstance(value, dict):
        return value.get("max")
    return value if _is_number(value) else None


def _covers_resources(name: str, keys: dict[str, str]) -> bool:
    return f"{name}_cpu_percent_avg" in keys and f"{name}_rss_mb_peak" in keys


def _add_rounds(builder: _FactBuilder, tiers: list[tuple[str, Any]], tier_names: list[str]) -> None:
    """逐轮层：轮级标量 + 该轮各场景 raw 的标量叶子（"所有数据"的最细一层）。"""
    for (tier, result) in zip(tier_names, [r for _, r in tiers]):
        for case_name, rounds in _case_rounds(result):
            for r in rounds:
                idx = int(getattr(r, "round_index", 0) or 0)
                for field, zh, unit, value in (
                    ("launch_ms", "框架口径连接耗时", "ms", getattr(r, "launch_ms", None)),
                    ("startup_ms", "启动耗时", "ms", getattr(r, "startup_ms", None)),
                    ("case_duration_ms", "用例耗时", "ms", getattr(r, "case_duration_ms", None)),
                    ("tcs_status", "用例状态", "", getattr(r, "tcs_status", None)),
                ):
                    status = "ok"
                    if value is None or value == "":
                        status = "missing"
                    builder.add(f"round/{idx}/{field}", scope="round", tier=tier, case=case_name,
                                round_index=idx, zh=zh, unit=unit,
                                value=value if _is_number(value) else None,
                                display=_scalar_display(value), data_status=status,
                                numbers={field: value} if _is_number(value) else None)
                err = str(getattr(r, "error", "") or "").strip()
                if err:
                    builder.add(f"round/{idx}/error", scope="round", tier=tier, case=case_name,
                                round_index=idx, zh="轮次异常", display=err, data_status="failed", gap=err)
                for scen_name, block in (getattr(r, "scenarios", None) or {}).items():
                    if not isinstance(block, dict):
                        continue
                    for key, value in block.items():
                        if not (_is_number(value) or isinstance(value, (str, bool))):
                            continue  # 列表/嵌套（如 stress steps）由 detail 层承载
                        path = f"round/{idx}/{scen_name}/{key}"
                        unit = _infer_unit(key)
                        builder.add(path, scope="scenario_raw", tier=tier, scenario=scen_name,
                                    round_index=idx, zh=f"{_scenario_label(scen_name)}：{key}",
                                    unit=unit, value=value if _is_number(value) else None,
                                    display=_scalar_display(value),
                                    data_status="ok",
                                    numbers={key: value} if _is_number(value) else None)


def _add_launch_failures(builder: _FactBuilder, tiers: list[tuple[str, Any]], tier_names: list[str]) -> None:
    for tier, (_name, result) in zip(tier_names, tiers):
        for case_name, rounds in _case_rounds(result):
            for row in _launch_attempt_rows(rounds):
                idx = int(row.get("round_index") or 0)
                attempt = int(row.get("attempt") or 1)
                for field, zh in _FAILURE_FIELDS:
                    value = row.get(field)
                    unit = _infer_unit(field)
                    builder.add(f"launch/{idx}/{attempt}/{field}", scope="launch", tier=tier, case=case_name,
                                round_index=idx, attempt=attempt, zh=zh, unit=unit,
                                value=value if _is_number(value) else None,
                                display=_display(value, unit),
                                data_status="ok" if value is not None else "missing",
                                caliber_id="launch_failure",
                                numbers={field: value} if _is_number(value) else None)
                kind = f"{row.get('stage') or ''}：{row.get('reason') or ''}".strip("：")
                if kind:
                    builder.add(f"launch/{idx}/{attempt}/kind", scope="launch", tier=tier, case=case_name,
                                round_index=idx, attempt=attempt, zh="失败归类", display=kind,
                                caliber_id="launch_failure")


def _add_key_facts(builder: _FactBuilder, per_tier: dict[str, dict[str, Any]], tier_names: list[str]) -> None:
    """关键**非统计**字段（结论本身：停止原因 / 进程存活 / 已收敛 …）。"""
    for tier in tier_names:
        block = per_tier.get(tier) or {}
        raw = block.get("raw") or []
        first = raw[0] if raw and isinstance(raw[0], dict) else {}
        for key, zh in _KEY_FACTS:
            value = first.get(key)
            if value is None or value == "":
                continue
            builder.add(f"key_fact/{key}", scope="round", tier=tier, zh=zh, display=_scalar_display(value))


def _add_scenario_errors(builder: _FactBuilder, per_tier: dict[str, dict[str, Any]], tier_names: list[str]) -> None:
    for tier in tier_names:
        agg = per_tier.get(tier) or {}
        for name, block in (agg.get("scenarios") or {}).items():
            block = block if isinstance(block, dict) else {}
            text = _dedupe_errors(block.get("errors"))
            if text:
                builder.add(f"scenario/{name}/errors", scope="status", tier=tier, scenario=name,
                            zh=f"{_scenario_label(name)}：场景异常", display=text,
                            data_status="failed", gap=text)


def find_previous_run(
    run_dir: str, *, app_name: str, platform: str, report_root: str,
) -> str:
    """在 ``report/perf/`` 下找**同 app + 同平台**的上一批次产物目录（找不到返回空串）。

    用途：1013 报告的大量结论是"与上一批次对比"；这里用既有产物即可支撑，**不新增埋点**。
    批次目录名形如 ``perf_<YYYYMMDD-HHmmss>``，按名字倒序即时间倒序（同格式可直接排序）。
    """
    if not report_root or not os.path.isdir(report_root):
        return ""
    current = os.path.basename(os.path.normpath(run_dir or ""))
    candidates = sorted(
        (name for name in os.listdir(report_root)
         if name.startswith("perf_") and os.path.isdir(os.path.join(report_root, name))),
        reverse=True,
    )
    for name in candidates:
        if name == current:
            continue
        path = os.path.join(report_root, name)
        summary = _load_summary(path)
        if not summary:
            continue
        if app_name and str(summary.get("app") or "") != app_name:
            continue
        if platform and _platform_title(summary.get("platform") or "") != _platform_title(platform):
            continue
        return path
    return ""


def _load_summary(run_root: str) -> dict[str, Any]:
    """读批次根/单档目录的 ``perf_summary.json``（多单元顶层结构为 ``{"tiers": [...]}``）。"""
    for rel in ("perf_summary.json", os.path.join("tier-10k", "perf_summary.json")):
        path = os.path.join(run_root, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def _tier_aggregates_from_dir(run_dir: str) -> dict[str, dict[str, Any]]:
    """从**任意形态的批次目录**抽「档位 → 主用例聚合」（复用 :func:`scan_run_dir`）。

    为什么不用顶层 ``perf_summary.json`` 一个来源：真实批次经常**只剩 ``tier-*/``**
    （只保留了各单元目录，顶层聚合汇总被裁掉/未落盘）。只看顶层会把"上一批次"判成
    "无可对比数据"，趋势段就凭空消失了。
    """
    out: dict[str, dict[str, Any]] = {}
    for name, result in scan_run_dir(run_dir)[0]:
        cases = list(getattr(result, "cases", None) or [])
        if cases:
            out[_tier_name(name)] = dict(getattr(cases[0], "aggregate", None) or {})
    return out


def _add_compare(
    builder: _FactBuilder, current: dict[str, dict[str, Any]], prev_dir: str, *,
    platform: str, requested: str = "auto",
) -> dict[str, Any]:
    """与上一批次的关键指标对比（仅 ``_HEALTH_METRICS`` 白名单；缺项如实记 none，不推测）。"""
    if not prev_dir:
        reason = ("未启用（--perf-ai-compare-run none）" if requested in ("none", "off", "0")
                  else "未找到同 app + 同平台的上一批次产物")
        return {"source": "", "reason": reason, "metrics": []}
    prev = _tier_aggregates_from_dir(prev_dir)
    if not prev:
        return {"source": prev_dir, "reason": "上一批次产物存在但没有可对比的聚合数据",
                "metrics": []}
    metrics: list[dict[str, Any]] = []
    for tier, agg in current.items():
        prev_agg = prev.get(tier)
        for label, key, unit in _HEALTH_METRICS:
            cur_stat = _stats_view(agg).get(key)
            cur = cur_stat.get("avg") if isinstance(cur_stat, dict) else cur_stat
            old_stat = _stats_view(prev_agg or {}).get(key) if prev_agg else None
            old = old_stat.get("avg") if isinstance(old_stat, dict) else old_stat
            if not (_is_number(cur) and _is_number(old)):
                continue
            delta = ((float(cur) - float(old)) / float(old) * 100.0) if old else None
            metrics.append({
                "tier": tier, "metric": key, "zh": label, "unit": unit,
                "prev": old, "cur": cur, "delta_pct": delta,
            })
    for item in metrics:
        delta = item.get("delta_pct")
        display = f"上批 {_display(item['prev'], item['unit'])} → 本批 {_display(item['cur'], item['unit'])}"
        if _is_number(delta):
            display += f"（{delta:+.1f}%）"
        builder.add(f"compare/{item['tier']}/{item['metric']}", scope="compare", tier=item["tier"],
                    zh=f"{item['zh']}（对比上一批次）", unit=item["unit"], value=item["cur"],
                    display=display, numbers={"prev": item["prev"], "cur": item["cur"], "delta_pct": delta})
    return {"source": prev_dir, "platform": _platform_title(platform), "metrics": metrics,
            "reason": "" if metrics else "上一批次有产物但无同名档位/同名指标可对比"}


def _nsify_round(data: dict[str, Any]) -> Any:
    """轮记录 → 属性可访问对象（``getattr(r, "round_index")`` 口径）。

    ``scenarios`` **保持 dict**：逐轮场景块是"场景名 → 指标"的映射，facts 层按
    ``.items()`` 遍历；一并转成对象会让整层静默消失。
    """
    payload = {key: value for key, value in data.items() if key != "scenarios"}
    return SimpleNamespace(scenarios=data.get("scenarios") or {}, **payload)


def _nsify_case(data: dict[str, Any]) -> Any:
    """用例记录 → 属性可访问对象（``aggregate`` 保持 dict，见 :func:`_nsify_round`）。"""
    payload = {key: value for key, value in data.items() if key not in ("aggregate", "rounds")}
    return SimpleNamespace(
        aggregate=data.get("aggregate") or {},
        rounds=[_nsify_round(item) for item in (data.get("rounds") or []) if isinstance(item, dict)],
        **payload,
    )


def _nsify_tier(data: dict[str, Any]) -> Any:
    payload = {key: value for key, value in data.items() if key != "cases"}
    return SimpleNamespace(
        cases=[_nsify_case(item) for item in (data.get("cases") or []) if isinstance(item, dict)],
        **payload,
    )


def _tier_dir_sort_key(name: str) -> tuple[int, int, str]:
    """目录名 → 排序键：``0k < 5k < 10k < 50k < stress``。

    纯字典序会把 ``10k`` 排到 ``5k`` 前面（报告列序会与执行顺序不一致，读者会读成"5K 最慢"）。
    数字档位按数值排，非数字单元（如 ``stress``）排最后。
    """
    match = re.fullmatch(r"(\d+)\s*[kK]?", name.strip())
    if match:
        return (0, int(match.group(1)), name)
    return (1, 0, name)


def scan_run_dir(run_dir: str) -> tuple[list[tuple[str, Any]], list[str]]:
    """扫描已落盘批次 → ``(档位列表, 未纳入分析的目录说明)``。

    三种产物形状都要认：

    1. 顶层 ``perf_summary.json`` 且 ``mode=multi-tier`` → 取 ``tiers[].tier`` 作档位名；
    2. 顶层无聚合汇总（只留 ``tier-*/``）→ 扫描 ``tier-*/perf_summary.json``，档位名取目录名去掉
       ``tier-`` 前缀（"只保留了各单元目录"的产物也能分析）；
    3. 单档批次（``run_dir`` 自身就是一份单档 ``perf_summary.json``）→ 档位名单个。

    第二个返回值是**数据缺口**（如某个 ``tier-*/`` 只有逐轮 ``result.json`` 没有
    ``perf_summary.json``）：必须被看见并写进分析报告，不得静默当"这个档位不存在"——
    否则读者会把"没分析"读成"没问题"。
    """
    if not run_dir or not os.path.isdir(run_dir):
        return [], []
    top = _load_summary(run_dir)
    if isinstance(top.get("tiers"), list) and top["tiers"]:
        out: list[tuple[str, Any]] = []
        for item in top["tiers"]:
            if isinstance(item, dict):
                out.append((str(item.get("tier") or f"tier-{len(out) + 1}"), _nsify_tier(item)))
        return out, []

    scanned: list[tuple[str, Any]] = []
    skipped: list[str] = []

    def _dir_key(name: str) -> tuple[int, int, str]:
        return _tier_dir_sort_key(name[len("tier-"):] if name.startswith("tier-") else name)

    try:
        entries = sorted(os.listdir(run_dir), key=_dir_key)
    except OSError:
        return [], []
    for name in entries:
        sub = os.path.join(run_dir, name)
        if not os.path.isdir(sub) or name.startswith("perf_ai_analysis"):
            continue
        data = _load_summary(sub)
        tier_name = name[len("tier-"):] if name.startswith("tier-") else name
        if not data:
            if name.startswith("tier-"):
                skipped.append(f"`{name}/` 无 `perf_summary.json`（疑似该单元未跑完或产物不完整）"
                               f"→ 该档位未纳入本批分析")
            continue
        scanned.append((tier_name, _nsify_tier(data)))
    if scanned:
        return scanned, skipped

    if top:
        return [(os.path.basename(os.path.normpath(run_dir)) or "run", _nsify_tier(top))], skipped
    return [], skipped


def load_tiers_from_run_dir(run_dir: str) -> list[tuple[str, Any]]:
    """``scan_run_dir`` 的简化形式（只要档位列表；空列表 = 没有可分析产物）。"""
    return scan_run_dir(run_dir)[0]


def collect_facts(
    tiers: Iterable[tuple[str, Any]], *, app_name: str = "", run_dir: str = "",
    platform: str = "", compare_run: str = "auto", report_root: str = "",
) -> dict[str, Any]:
    """采集全部事实（返回可直接 json.dump 的 dict）。

    Args:
        tiers: ``[(档位名, PerfRunResult)]``（与 ``report.render_multi_tier_html`` 同入参）。
        compare_run: ``"auto"``（自动找同 app + 同平台上一批次）/ ``"none"`` / 显式批次目录。
        report_root: 批次根目录（``auto`` 对比时在其中查找上一批次）。
    """
    tier_list = list(tiers)
    tier_names = [_tier_name(name) for name, _ in tier_list]
    multi = len(tier_names) > 1
    builder = _FactBuilder(multi_tier=multi)
    per_tier = _per_tier_aggregates_local(tier_list, tier_names)
    app = app_name or (str(getattr(tier_list[0][1], "app", "")) if tier_list else "")
    platform = platform or (str(getattr(tier_list[0][1], "platform", "")) if tier_list else "")

    host_info = _add_meta(builder, tier_list, app_name=app)
    _add_health(builder, per_tier, tier_names)
    scenario_order = _add_scenarios(builder, per_tier, tier_names)
    _add_key_facts(builder, per_tier, tier_names)
    _add_scenario_errors(builder, per_tier, tier_names)
    _add_rounds(builder, tier_list, tier_names)
    _add_launch_failures(builder, tier_list, tier_names)

    for tier, (_name, result) in zip(tier_names, tier_list):
        baseline = dict(getattr(result, "baseline", None) or {})
        builder.add(f"meta/baseline/{tier}", scope="meta", tier=tier, zh="基线档位/文件",
                    display=f"{baseline.get('tag') or '—'}（{baseline.get('source') or '未加载'}）")
        planned = sorted(getattr(result, "planned_scenarios", None) or [])
        if planned:
            builder.add(f"meta/planned_scenarios/{tier}", scope="meta", tier=tier,
                        zh="本次声明执行的场景", display="、".join(planned))
        for item in (getattr(result, "skipped_scenarios", None) or []):
            builder.add(
                f"meta/skipped_scenarios/{tier}/{item.get('scenario')}", scope="status", tier=tier,
                scenario=str(item.get("scenario") or ""), zh="跳过的场景",
                display=str(item.get("reason") or ""), data_status="skipped",
                gap=str(item.get("reason") or ""),
            )

    prev_dir = ""
    if compare_run == "auto":
        prev_dir = find_previous_run(run_dir, app_name=app, platform=platform, report_root=report_root)
    elif compare_run and compare_run not in ("none", "off", "0"):
        prev_dir = compare_run
    compare = _add_compare(builder, per_tier, prev_dir, platform=platform, requested=compare_run)

    return {
        "schema_version": FACTS_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "app": app,
        "platform": _platform_title(platform),
        "run_dir": run_dir,
        "mode": "multi-tier" if multi else "single-tier",
        "tiers": tier_names,
        "scenarios": scenario_order,
        "host": host_info.get("host") or {},
        "calibers": load_calibers(app),
        "compare": compare,
        "facts": builder.items,
        "coverage": builder.coverage(),
    }


def _md_table(items: list[dict[str, Any]], *, show_tier: bool = False) -> list[str]:
    lines = ["| fact_id | 含义 | 取值 | 状态 | 备注 |", "| --- | --- | --- | --- | --- |"]
    for item in items:
        tier = item.get("tier") or ""
        prefix = f"[{tier}] " if (show_tier and tier) else ""
        note = item.get("gap") or ""
        lines.append(
            f"| `{item.get('fact_id')}` | {prefix}{item.get('zh')} | {item.get('display')} | "
            f"{item.get('data_status')} | {note} |"
        )
    return lines


def render_facts_md(doc: dict[str, Any], *, reference: bool = False) -> str:
    """把 facts 渲染成**紧凑的人读版**（分析 agent 的输入；比 300KB JSON 便宜得多）。

    结构直接对齐 1013 报告：口径词典 → 总体健康度 → **分场景（每场景一张表，就是报告 §三 的数据来源）**
    → 逐轮记录 → 启动失败现场 → 主机基准 → 元信息 → 对比批次。

    每个数据行首列即 ``fact_id``——agent 直接把它写进 HTML 的 ``data-fact-id``，
    覆盖校验才成立；这一点在开头「写作契约」里显式写明。

    Args:
        reference: 是否附上**参考层**（逐轮原始读数 800 项、元信息、对比批次）。
            默认 ``False``——主文件只装"必覆盖层"，把体量留给真正要逐格给结论的数据；
            参考层落 ``facts-raw.md``，agent 按需读取（省 token 的关键一招）。
    """
    facts = list(doc.get("facts") or [])
    lines: list[str] = []
    lines.append(f"# perf 分析事实包（app={doc.get('app')} / platform={doc.get('platform')} / "
                 f"mode={doc.get('mode')}）")
    lines.append("")
    lines.append(f"- 批次目录：`{doc.get('run_dir')}`")
    lines.append(f"- 档位：{'、'.join(doc.get('tiers') or []) or '—'}")
    lines.append(f"- 生成时间：{doc.get('generated_at')}")
    cov = doc.get("coverage") or {}
    lines.append(f"- 事实总数：{cov.get('total')}（**必覆盖 {cov.get('required')}** / 参考层 {cov.get('optional')}）")
    compare = doc.get("compare") or {}
    if compare.get("metrics"):
        lines.append(f"- 对比批次：`{compare.get('source')}`（{len(compare['metrics'])} 项可比指标）")
    else:
        lines.append(f"- 对比批次：无（{compare.get('reason') or '未启用'}）")
    lines.append("")
    lines.append("> **写作契约（硬性）**：")
    lines.append("> 1. 报告里**每一个数据格子都要写 `data-fact-id=\"<上表 fact_id>\"`**，"
                 "读数必须与本表「取值」列逐字一致；")
    lines.append("> 2. 不得引入本表之外的读数（没有的事实宁可在「待澄清」里说明，也不要编）；")
    lines.append("> 3. **参考层**（逐轮原始读数/元信息/对比）不计入覆盖校验，按需引用即可"
                 + ("。" if reference else "，见同目录 `facts-raw.md`。"))
    lines.append("")

    lines.append("## 一、口径词典（所有结论必须符合这些口径）")
    lines.append("")
    for item in (doc.get("calibers") or []):
        lines.append(f"- **{item.get('zh')}**（`{item.get('id')}`）：{item.get('text')}")
    lines.append("")

    by_scope: dict[str, list[dict[str, Any]]] = {}
    for item in facts:
        by_scope.setdefault(str(item.get("scope") or "other"), []).append(item)

    health = by_scope.get("health") or []
    lines.append(f"## 二、总体健康度（{len(health)} 项，必覆盖）")
    lines.append("")
    lines += _md_table(health, show_tier=len(doc.get("tiers") or []) > 1)
    lines.append("")

    grouped: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {}
    raw_scopes = ("status", "metric", "resource", "detail", "scenario_raw")
    for scope in raw_scopes:
        if scope == "scenario_raw" and not reference:
            continue
        for item in by_scope.get(scope) or []:
            key = (str(item.get("tier") or ""), str(item.get("scenario") or ""))
            grouped.setdefault(key, {}).setdefault(scope, []).append(item)
    lines.append(f"## 三、分场景（{len(grouped)} 个 档位×场景 块；报告 §三 的数据来源）")
    lines.append("")
    for (tier, scenario), blocks in grouped.items():
        lines.append(f"### [{tier or '—'}] {scenario or '—'}")
        lines.append("")
        for scope, title in (("status", "采集状态"), ("metric", "指标"), ("resource", "资源"),
                             ("detail", "逐级明细"), ("scenario_raw", "逐轮原始读数（参考层）")):
            items = blocks.get(scope) or []
            if not items:
                continue
            lines.append(f"**{title}**（{len(items)} 项）")
            lines.append("")
            lines += _md_table(items)
            lines.append("")

    sections = [("round", "四、逐轮记录（轮级标量 / 异常 / 关键非统计字段）"),
                ("launch", "五、启动失败现场（每一次尝试都要有结论）"),
                ("host", "六、主机算力基准（跨批次比数值前必须核对）")]
    if reference:
        sections += [("meta", "七、元信息（可比性前提）"), ("compare", "八、与上一批次对比")]
    for scope, title in sections:
        items = by_scope.get(scope) or []
        if not items:
            continue
        lines.append(f"## {title}（{len(items)} 项）")
        lines.append("")
        lines += _md_table(items, show_tier=len(doc.get("tiers") or []) > 1)
        lines.append("")

    if not reference:
        optional = [i for i in facts if not i.get("required")]
        lines.append(f"## 参考层（{len(optional)} 项，不计入覆盖校验）")
        lines.append("")
        lines.append("逐轮场景原始读数 / 元信息 / 与上一批次对比 → 同目录 **`facts-raw.md`**"
                     "（按需读取；报告里的读数仍须与 `facts.json` 一致）。")
        lines.append("")

    lines.append("## 附：原始产物位置（深挖可用，但结论引用的读数必须与上表一致）")
    lines.append("")
    lines.append("`perf_summary.json`（逐轮完整 raw、列表/嵌套结构如 stress 逐级记录）、"
                 "`perf_metrics.csv`（长表格）、`tier-*/`（逐档产物）、本目录 `facts.json`（本表机器版）。")
    lines.append("")
    return "\n".join(lines)


def write_facts(doc: dict[str, Any], out_dir: str) -> dict[str, str]:
    """落盘三件套：``facts.json``（全量）/ ``facts.md``（必覆盖层）/ ``facts-raw.md``（参考层）。

    拆分的唯一目的是**省 token**：必覆盖层约 5 万字符（agent 必读），
    参考层约 8 万字符（含 800 项逐轮原始读数，按需读）。
    """
    os.makedirs(out_dir, exist_ok=True)
    targets = {
        "json": ("facts.json", None),
        "md": ("facts.md", render_facts_md(doc)),
        "raw_md": ("facts-raw.md", render_facts_md(doc, reference=True)),
    }
    paths: dict[str, str] = {}
    for key, (name, text) in targets.items():
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            if text is None:
                json.dump(doc, fh, ensure_ascii=False, indent=2)
            else:
                fh.write(text)
        paths[key] = path
    logger.info("[perf-ai] 事实包 → %s（facts %d 条 / 必覆盖 %s）", out_dir,
                len(doc.get("facts") or []), (doc.get("coverage") or {}).get("required"))
    return paths
