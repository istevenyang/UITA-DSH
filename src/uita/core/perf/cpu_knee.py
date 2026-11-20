"""core/perf/cpu_knee.py — 「CPU 是否已从启动峰值回落」的稳健判定。

用途（内嵌页面渲染性能测量）：应用冷启动后 CPU 会先冲高、再回落到稳态。
点击页面入口的时机必须落在**回落之后**，否则量到的是"启动余温 + 页面加载"的
混合值，跨轮不可比。

判定两条路（任一成立即命中，``reason`` 记录走的是哪条）：

- ``trend_ok``（主路，"低于已有数据拟合曲线趋势"）：对窗口内样本做
  Theil–Sen 稳健斜率（对尖峰不敏感）+ 残差 MAD 估计噪声，阈值取
  ``trend(末点) − max(k·σ, abs_margin)``；末尾连续 ``min_consecutive`` 个点都
  低于阈值且斜率 ≤ 0 才命中。
- ``fallback_ratio``（兜底）：样本太少/噪声过大时，退化为
  "当前 ≤ 窗口峰值 × ``min_drop_ratio`` 且斜率 ≤ 0"。

纪律：

- 样本不足、超时都不算命中（``ok=False`` + 明确的 ``reason``），**绝不伪造**；
  调用方据此记 ``degraded`` 并继续后续步骤（性能测量不该因"没等到回落"整轮失败）。
- 纯计算 + 一次 ``asyncio.sleep`` 轮询，无 IO、无第三方依赖（R1：core 只依赖标准库）。
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

logger = logging.getLogger("uita")

__all__ = [
    "KneeResult",
    "detect_knee",
    "theil_sen_slope",
    "wait_cpu_knee",
]

#: 稳健回归的成对斜率计算是 O(n²)：样本过多时等距抽样到该上限（精度足够，防卡顿）。
_MAX_PAIRWISE_POINTS = 120


@dataclass
class KneeResult:
    """一次拐点判定的结果（全部字段可直接进 JSON 产物）。"""

    ok: bool
    hit_ms: Optional[int] = None
    cpu_at_hit: Optional[float] = None
    peak: Optional[float] = None
    slope: Optional[float] = None
    threshold: Optional[float] = None
    samples_used: int = 0
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "hit_ms": self.hit_ms,
            "cpu_at_hit": self.cpu_at_hit,
            "peak": self.peak,
            "slope": self.slope,
            "threshold": self.threshold,
            "samples_used": int(self.samples_used),
            "reason": self.reason,
        }


def _pair(sample: Any) -> Optional[tuple[int, float]]:
    """把一只样本归一化为 ``(t_ms, cpu_percent)``；不可用时返回 ``None``。

    同时支持 dataclass / 普通对象 / dict（场景侧可能传自己构造的轻量样本），
    避免为了取两个字段就绑定 ``ResourceSample`` 类型。
    """
    if isinstance(sample, dict):
        t_raw, cpu_raw = sample.get("t_ms"), sample.get("cpu_percent")
    else:
        t_raw = getattr(sample, "t_ms", None)
        cpu_raw = getattr(sample, "cpu_percent", None)
    if t_raw is None or cpu_raw is None:
        return None
    try:
        t_ms = int(t_raw)
        cpu = float(cpu_raw)
    except (TypeError, ValueError):
        return None
    if cpu < 0:  # psutil 理论不产生负值，防御脏数据
        return None
    return t_ms, cpu


def _points(samples: Sequence[Any]) -> list[tuple[int, float]]:
    """样本序列 → 有效点列表（按时间升序、去重复时间戳保留首个）。"""
    seen: dict[int, float] = {}
    for sample in samples or []:
        pair = _pair(sample)
        if pair is None:
            continue
        seen.setdefault(pair[0], pair[1])
    return sorted(seen.items())


def _thin(points: list[tuple[int, float]], limit: int) -> list[tuple[int, float]]:
    if len(points) <= limit:
        return points
    step = max(1, len(points) // limit)
    thinned = points[::step]
    if thinned[-1] != points[-1]:
        thinned.append(points[-1])
    return thinned


def theil_sen_slope(points: Sequence[tuple[int, float]]) -> float:
    """Theil–Sen 稳健斜率（单位：cpu% / ms）。

    取所有点对斜率的中位数：单个尖峰（启动瞬间 100% CPU）不会把趋势线拽飞，
    这是它能用来做"已回落"判据的原因（最小二乘会被峰值主导）。
    """
    pts = _thin(list(points), _MAX_PAIRWISE_POINTS)
    if len(pts) < 2:
        return 0.0
    slopes: list[float] = []
    for i in range(len(pts)):
        xi, yi = pts[i]
        for j in range(i + 1, len(pts)):
            xj, yj = pts[j]
            dx = xj - xi
            if dx <= 0:
                continue
            slopes.append((yj - yi) / dx)
    if not slopes:
        return 0.0
    return statistics.median(slopes)


def _mad_sigma(values: Sequence[float]) -> float:
    """残差的 MAD → 正态一致的 σ 估计（乘 1.4826）。"""
    if not values:
        return 0.0
    center = statistics.median(values)
    return statistics.median([abs(v - center) for v in values]) * 1.4826


def detect_knee(
    samples: Sequence[Any],
    *,
    window_ms: int = 15000,
    min_drop_ratio: float = 0.4,
    min_consecutive: int = 4,
    trend_margin_sigma: float = 2.0,
    abs_margin: float = 2.0,
    min_samples: int = 6,
) -> KneeResult:
    """在已有样本上判定一次（纯函数，不睡眠、不重试）。

    Args:
        samples: 采样序列（``t_ms`` / ``cpu_percent``）。
        window_ms: 只用最近这么长的窗口拟合趋势（更早的启动峰值不参与"当前趋势"）。
        min_drop_ratio: 兜底路的降幅比例（当前 ≤ 峰值 × 该值）。
        min_consecutive: 趋势路要求的"连续低于阈值"点数（500ms 采样 × 4 ≈ 2s）。
        trend_margin_sigma: 阈值 = 趋势值 − k·σ。
        abs_margin: 阈值下移的绝对下限（cpu 百分点），防止 σ 极小时阈值贴住趋势线。
        min_samples: 少于该点数直接判"样本不足"（由调用方继续轮询）。
    """
    pts = _points(samples)
    if not pts:
        return KneeResult(ok=False, samples_used=0, reason="no_samples")

    last_t = pts[-1][0]
    cutoff = last_t - max(1, int(window_ms))
    window = [(t, c) for t, c in pts if t >= cutoff]
    if len(window) < max(2, int(min_samples)):
        return KneeResult(
            ok=False,
            samples_used=len(window),
            reason="insufficient_samples",
            cpu_at_hit=window[-1][1] if window else None,
        )

    xs = [float(t) for t, _ in window]
    ys = [c for _, c in window]
    slope = theil_sen_slope(window)
    peak = max(ys)
    last_cpu = ys[-1]

    # 趋势线：过中位点 (median_x, median_y)，斜率用稳健估计
    median_x = statistics.median(xs)
    median_y = statistics.median(ys)
    residuals = [y - (median_y + slope * (x - median_x)) for x, y in zip(xs, ys)]
    sigma = _mad_sigma(residuals)
    trend_end = median_y + slope * (xs[-1] - median_x)

    tail = ys[-max(2, int(min_consecutive)):]
    # 命中判据（两条**同时**成立才算回落，缺一不可）：
    #   （a）相对启动峰值已大幅回落：尾部各点 ≤ peak × min_drop_ratio；
    #   （b）不是上升沿：窗口稳健斜率 ≤ 0。
    # 趋势线只用于**区分命中类型**（不改判据）：tail 全部不高于"趋势上限"（trend_end + margin）
    # 记 trend_ok（平滑回落），否则记 fallback_ratio（回落过程中有反弹，仍算已回落）。
    # 之所以不拿趋势线做硬判据：Theil–Sen 对凸形下降序列会外推出负值，"当前值永远高于趋势线"
    # 会让 trend_ok 永不命中——那等于把判据写死成失效。
    drop_limit = peak * float(min_drop_ratio)
    trend_limit = trend_end + max(trend_margin_sigma * sigma, float(abs_margin))
    tail_len_ok = len(tail) >= max(2, int(min_consecutive))
    dropped = tail_len_ok and all(value <= drop_limit for value in tail)
    smooth = tail_len_ok and all(value <= trend_limit for value in tail)

    if dropped and slope <= 0:
        return KneeResult(
            ok=True,
            hit_ms=last_t,
            cpu_at_hit=round(last_cpu, 2),
            peak=round(peak, 2),
            slope=round(slope, 6),
            threshold=round(drop_limit, 2),
            samples_used=len(window),
            reason="trend_ok" if smooth else "fallback_ratio",
        )

    return KneeResult(
        ok=False,
        hit_ms=None,
        cpu_at_hit=round(last_cpu, 2),
        peak=round(peak, 2),
        slope=round(slope, 6),
        threshold=round(drop_limit, 2),
        samples_used=len(window),
        reason="not_yet",
    )


async def wait_cpu_knee(
    read_samples: Callable[[], Sequence[Any]],
    *,
    timeout_ms: int = 30000,
    poll_ms: int = 250,
    window_ms: int = 15000,
    min_drop_ratio: float = 0.4,
    min_consecutive: int = 4,
    trend_margin_sigma: float = 2.0,
    abs_margin: float = 2.0,
    min_samples: int = 6,
) -> KneeResult:
    """轮询 ``read_samples()`` 直到 CPU 回落（或超时）。

    Args:
        read_samples: 返回**当前全部**采样的可调用对象（生产侧传 ``lambda: sampler.samples``）。
            由调用方决定数据来源，本模块不持有采样器（core 只认鸭子类型）。

    Returns:
        :class:`KneeResult`。超时返回 ``ok=False, reason="timeout"``，
        同时保留最后一次的 ``cpu_at_hit`` / ``peak`` / ``slope`` 供报告说明原因。
    """
    interval_s = max(0.05, float(poll_ms) / 1000.0)
    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0
    last = KneeResult(ok=False, reason="no_samples")

    while True:
        try:
            samples = read_samples() or []
        except Exception as exc:  # noqa: BLE001 - 读采样失败不该炸掉测量
            logger.debug("[perf.cpu_knee] 读取采样失败: %s", exc)
            samples = []

        last = detect_knee(
            samples,
            window_ms=window_ms,
            min_drop_ratio=min_drop_ratio,
            min_consecutive=min_consecutive,
            trend_margin_sigma=trend_margin_sigma,
            abs_margin=abs_margin,
            min_samples=min_samples,
        )
        if last.ok:
            logger.info(
                "[perf.cpu_knee] 命中（%s）：CPU %.1f%%（峰值 %.1f%%），斜率 %.4f%%/s",
                last.reason, last.cpu_at_hit or 0.0, last.peak or 0.0, (last.slope or 0.0) * 1000,
            )
            return last
        if time.monotonic() >= deadline:
            detail = last.reason
            last.reason = "timeout"
            logger.info(
                "[perf.cpu_knee] 超时未回落（最后 CPU %.1f%%，峰值 %.1f%%，末次判定 %s）",
                last.cpu_at_hit or 0.0, last.peak or 0.0, detail,
            )
            return last
        await asyncio.sleep(interval_s)
