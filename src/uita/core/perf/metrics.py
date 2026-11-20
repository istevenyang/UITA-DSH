"""perf 指标聚合（纯标准库，无 IO / 无副作用）。

**产出只给真实值（2026-10-08 用户要求）**：``summarize()`` 只给
``count / min / avg / max``——这四个数都能对应到"确实测到过的那一次运行"，
其中 ``max`` 就是真实出现过的最差一轮。原先的 ``median / p50 / p95`` 已从产物中移除：
百分位是**线性插值合成值**（如 3 轮样本算出的 p95 可能不对应任何一轮实测），
会把"是多少就报多少"变成"报一个谁也没测到过的数"；逐轮真实值始终完整保留在
``cases[].rounds[]`` 里，报告也直接以逐轮明细表呈现。

``percentile()`` 函数**保留**：它是与 ``core/report_portal/tasks/exec_stats.py::percentile``
逐行同口径的锚点（由单测守着），供服务端批次统计等其它场景复用；只是不再参与
perf 产物与判定。

派生统计 ``coefficient_of_variation()``（cv）已于 **2026-10-12 删除**（用户要求
"统计数据尽量简单一点"）：它是二次加工值而非实测值，"多轮波动有多大"直接读逐轮
明细（``perf_summary.json::cases[].rounds[]``）即可。

设计约定（与 SDK 内既有统计代码保持一致的硬规则）：
  - 缺失值一律记 ``None``，**绝不用 0 兜底**：0 会伪造出"极快/极小"的假结论，
    而平台不支持（如 Windows 无 ``num_fds``）或样本为空时，正确语义是"该指标未知"。
  - 所有统计函数对 ``None``/空序列均不抛异常，返回 ``None`` 或空结构。
"""
from __future__ import annotations

import statistics
from typing import Any, Iterable, Optional

__all__ = [
    "percentile",
    "clean_values",
    "summarize",
    "linear_regression_slope",
]


def clean_values(values: Optional[Iterable[Any]]) -> list[float]:
    """过滤 ``None``/非数值，返回 float 列表（保持原顺序）。"""
    if not values:
        return []
    out: list[float] = []
    for v in values:
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f:  # NaN
            continue
        out.append(f)
    return out


def percentile(sorted_vals: list[float], p: float) -> Optional[float]:
    """线性插值百分位数（``p`` 取 0~1）；空列表返回 ``None``。

    与 ``core/report_portal/tasks/exec_stats.py::percentile`` 逐行同口径。

    **不进 perf 产物**（见模块 docstring）：仅作为跨模块口径锚点保留，
    供服务端批次统计等其它调用方复用。
    """
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return sorted_vals[0]
    rank = p * (n - 1)
    lo = int(rank)
    hi = min(lo + 1, n - 1)
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def summarize(values: Optional[Iterable[Any]]) -> dict[str, Optional[float]]:
    """对一组样本给出**真实值口径**的 min/avg/max 与样本数。

    Returns:
        ``{"count": int, "min": float|None, "avg": ..., "max": ...}``
        空样本时除 ``count=0`` 外全为 ``None``。

        不含百分位（``p50/p95/median``）——理由见模块 docstring：百分位不是实测值，
        而逐轮真实值始终在 ``cases[].rounds[]`` 与报告的逐轮明细表里。
    """
    vals = clean_values(values)
    if not vals:
        return {"count": 0, "min": None, "avg": None, "max": None}
    ordered = sorted(vals)
    return {
        "count": len(ordered),
        "min": ordered[0],
        "avg": statistics.fmean(ordered),
        "max": ordered[-1],
    }


def linear_regression_slope(xs: Optional[Iterable[Any]], ys: Optional[Iterable[Any]]) -> Optional[float]:
    """最小二乘斜率（单位 = ``y 单位 / x 单位``）；样本 <2 或 x 无变化 → ``None``。

    用于稳态判据「RSS 是否单调增长」：以分钟为 x、MB 为 y，得到 MB/min。
    与 B.3 场景 2 门限对应（``rss_slope_mb_per_min`` ≤ 2）。
    """
    xv = clean_values(xs)
    yv = clean_values(ys)
    n = min(len(xv), len(yv))
    if n < 2:
        return None
    xv, yv = xv[:n], yv[:n]
    mean_x = statistics.fmean(xv)
    mean_y = statistics.fmean(yv)
    denom = sum((x - mean_x) ** 2 for x in xv)
    if denom == 0:
        return None
    numer = sum((x - mean_x) * (y - mean_y) for x, y in zip(xv, yv))
    return numer / denom



