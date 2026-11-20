"""core/perf/chart.py — 按**测试次数**（横坐标）的多序列折线图（内联 SVG）。

与 ``report._sparkline`` 的区别：那个是 640×56、无坐标轴/图例的迷你图，只嵌在
「逐轮趋势」表格里（且 20 轮才出）；本模块产出**带坐标轴刻度、图例、每点 tooltip**
的可读图，用于交付"启动加载期 CPU/RAM 峰值均值"与"页面渲染耗时"随轮次的变化。

纪律（与报告其余部分一致）：

- 纯内联 SVG：无 JS、无外部资源（单文件报告可直接发人/上传）；
- 缺失轮**断开折线**，绝不补 0（补 0 会画出"耗时暴跌"的假象）；
- 数据不足时给明确提示，不渲染误导性的空图；
- 文本一律 ``html.escape``（元素名/标题可能含 ``<``）。
"""
from __future__ import annotations

import html
import logging
import math
from dataclasses import dataclass, field
from typing import Optional, Sequence

logger = logging.getLogger("uita")

__all__ = ["ChartSeries", "nice_ticks", "render_line_chart"]

#: 默认配色（与报告既有的蓝/灰/橙/绿保持同一色系）。
DEFAULT_COLORS: tuple[str, ...] = ("#2f6fed", "#8a94a6", "#e08a2f", "#2f9e6f", "#b0459a")

_MAX_X_LABELS = 8
_PAD_LEFT = 56
_PAD_RIGHT = 18
_PAD_TOP = 14
_PAD_BOTTOM = 34


@dataclass
class ChartSeries:
    """一条序列：长度必须等于轮次数，``None`` 表示该轮无有效数据。"""

    name: str
    values: list[Optional[float]] = field(default_factory=list)
    color: str = ""
    unit: str = ""

    def points(self) -> list[tuple[int, float]]:
        return [
            (i, float(v)) for i, v in enumerate(self.values)
            if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]


def nice_ticks(lo: float, hi: float, count: int = 4) -> list[float]:
    """在 ``[lo, hi]`` 上取 ~``count`` 个"好看"的刻度（1/2/2.5/5 × 10ⁿ）。"""
    if not math.isfinite(lo) or not math.isfinite(hi):
        return []
    if hi <= lo:
        hi = lo + 1.0
    raw = (hi - lo) / max(1, count)
    if raw <= 0:
        return [lo, hi]
    exp = math.floor(math.log10(raw))
    base = raw / (10 ** exp)
    for candidate in (1.0, 2.0, 2.5, 5.0, 10.0):
        if base <= candidate:
            step = candidate * (10 ** exp)
            break
    else:  # pragma: no cover - 浮点兜底
        step = 10.0 ** (exp + 1)
    start = math.floor(lo / step) * step
    ticks: list[float] = []
    value = start
    # 上限保护：极端数据（如 1e9）下也不生成上万条网格线
    while value <= hi + step * 0.5 and len(ticks) < 32:
        if value >= lo - step * 1e-9:
            ticks.append(round(value, 10))
        value += step
    return ticks


def _fmt(value: float) -> str:
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def render_line_chart(
    series: Sequence[ChartSeries],
    *,
    title: str,
    y_unit: str = "",
    x_label: str = "测试次数",
    width: int = 900,
    height: int = 260,
    y_from_zero: bool = True,
    x_labels: Optional[Sequence[str]] = None,
) -> str:
    """渲染一张多序列折线图（内联 SVG 字符串）。

    Args:
        series: 序列列表（长度 = 轮次数）。
        title: 图标题（同时作为 ``aria-label``）。
        y_unit: Y 轴单位（``%`` / ``MB`` / ``ms``）。
        x_label: X 轴标题。
        width / height: 画布尺寸（``viewBox``，实际宽度自适应父容器）。
        y_from_zero: Y 轴是否从 0 起（CPU/内存/耗时都建议 True，避免小波动被放大）。
        x_labels: 有序类别标签（如 ``["off", "blank", "soft"]``）。给了就把 X 轴从
            "轮次序号"改成这些取值（每个序列长度 = 标签数）；不给仍是轮次序号。

    Returns:
        ``<figure>`` 包裹的 SVG；数据不足时返回一句 ``<span class="hint">`` 提示。
    """
    valid_counts = [len(s.points()) for s in series or []]
    rounds = max((len(s.values) for s in series or []), default=0)
    if not series or sum(valid_counts) == 0:
        return '<span class="hint">该指标暂无有效数据，无法成图</span>'
    categorical = [str(x) for x in (x_labels or [])]
    if categorical:
        rounds = max(rounds, len(categorical))
    if rounds < 2:
        return (
            '<span class="hint">仅 1 个数据点，无法成图（至少需要 2 个）</span>'
            if categorical else
            '<span class="hint">仅 1 轮，无法成图（至少需要 2 轮）</span>'
        )

    all_values = [v for s in series for _, v in s.points()]
    lo = 0.0 if y_from_zero else min(all_values)
    hi = max(all_values)
    if hi <= lo:
        hi = lo + 1.0
    # 顶部留 8% 余量，避免最高点贴边
    hi = hi + (hi - lo) * 0.08

    plot_w = max(1, width - _PAD_LEFT - _PAD_RIGHT)
    plot_h = max(1, height - _PAD_TOP - _PAD_BOTTOM)

    def x_of(index: int) -> float:
        return _PAD_LEFT + (plot_w * index / max(1, rounds - 1))

    def y_of(value: float) -> float:
        return _PAD_TOP + plot_h - (value - lo) / (hi - lo) * plot_h

    parts: list[str] = []
    parts.append(
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" '
        f'role="img" aria-label="{html.escape(title)}">'
    )

    # ── Y 轴刻度 + 网格线
    ticks = nice_ticks(lo, hi, 4)
    for tick in ticks:
        y = y_of(tick)
        if y < _PAD_TOP - 0.5 or y > _PAD_TOP + plot_h + 0.5:
            continue
        parts.append(
            f'<line x1="{_PAD_LEFT}" y1="{y:.1f}" x2="{_PAD_LEFT + plot_w}" y2="{y:.1f}" '
            'stroke="#e6e9ef" stroke-width="1" />'
        )
        parts.append(
            f'<text x="{_PAD_LEFT - 8}" y="{y + 3.5:.1f}" text-anchor="end" '
            f'font-size="10" fill="#6b7280">{_fmt(tick)}</text>'
        )

    # ── X 轴刻度（类别模式标取值；轮次模式标序号，抽稀到 ≤8 个标签，
    # 但**必须标出最后一项**，否则读者会以为数据只跑到中途）
    if categorical:
        step = max(1, math.ceil(len(categorical) / max(1, _MAX_X_LABELS)))
        for index in range(0, len(categorical), step):
            x = x_of(index)
            parts.append(
                f'<text x="{x:.1f}" y="{_PAD_TOP + plot_h + 16:.1f}" text-anchor="middle" '
                f'font-size="10" fill="#6b7280">{html.escape(categorical[index])}</text>'
            )
    else:
        step = max(1, math.ceil((rounds - 1) / max(1, _MAX_X_LABELS - 1)))
        x_indices = list(range(0, rounds, step))
        if x_indices[-1] != rounds - 1:
            x_indices.append(rounds - 1)
        for index in x_indices:
            x = x_of(index)
            parts.append(
                f'<text x="{x:.1f}" y="{_PAD_TOP + plot_h + 16:.1f}" text-anchor="middle" '
                f'font-size="10" fill="#6b7280">{index + 1}</text>'
            )
    parts.append(
        f'<text x="{_PAD_LEFT + plot_w / 2:.1f}" y="{height - 6}" text-anchor="middle" '
        f'font-size="11" fill="#374151">{html.escape(x_label)}</text>'
    )
    if y_unit:
        parts.append(
            f'<text x="12" y="{_PAD_TOP + 4}" font-size="10" fill="#6b7280">'
            f'{html.escape(y_unit)}</text>'
        )

    # ── 轴骨架
    parts.append(
        f'<line x1="{_PAD_LEFT}" y1="{_PAD_TOP + plot_h}" x2="{_PAD_LEFT + plot_w}" '
        f'y2="{_PAD_TOP + plot_h}" stroke="#cbd2dc" stroke-width="1" />'
    )
    parts.append(
        f'<line x1="{_PAD_LEFT}" y1="{_PAD_TOP}" x2="{_PAD_LEFT}" '
        f'y2="{_PAD_TOP + plot_h}" stroke="#cbd2dc" stroke-width="1" />'
    )

    # ── 序列（断线：相邻轮不连续就断开）
    for order, item in enumerate(series):
        color = item.color or DEFAULT_COLORS[order % len(DEFAULT_COLORS)]
        pts = item.points()
        if not pts:
            continue
        segments: list[list[tuple[int, float]]] = []
        current: list[tuple[int, float]] = []
        previous: Optional[int] = None
        for index, value in pts:
            if previous is not None and index != previous + 1 and current:
                segments.append(current)
                current = []
            current.append((index, value))
            previous = index
        if current:
            segments.append(current)

        for segment in segments:
            if len(segment) >= 2:
                coords = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, v in segment)
                parts.append(
                    f'<polyline fill="none" stroke="{color}" stroke-width="1.6" '
                    f'stroke-linejoin="round" points="{coords}" />'
                )
        for index, value in pts:
            unit = html.escape(item.unit or y_unit)
            where = (
                html.escape(categorical[index])
                if categorical and index < len(categorical) else f"第{index + 1}轮"
            )
            parts.append(
                f'<circle cx="{x_of(index):.1f}" cy="{y_of(value):.1f}" r="2.2" fill="{color}">'
                f'<title>{where} {html.escape(item.name)} {_fmt(value)}{unit}</title>'
                "</circle>"
            )

    parts.append("</svg>")

    # ── 图例（名称 + 末值）
    legend: list[str] = []
    for order, item in enumerate(series):
        color = item.color or DEFAULT_COLORS[order % len(DEFAULT_COLORS)]
        pts = item.points()
        last = f"{_fmt(pts[-1][1])}{html.escape(item.unit or y_unit)}" if pts else "—"
        legend.append(
            f'<span class="legend-item"><span class="legend-dot" style="background:{color}"></span>'
            f"{html.escape(item.name)}（{'末项' if categorical else '末轮'} {last}）</span>"
        )

    return (
        f'<figure class="perf-chart"><figcaption>{html.escape(title)}</figcaption>'
        + "".join(parts)
        + f'<div class="legend">{"".join(legend)}</div>'
        + (
            '<div class="hint">折线断点表示该项无有效数据（未跑/失败），不是 0。</div></figure>'
            if categorical else
            '<div class="hint">折线断点表示该轮无有效数据（失败/跳过），不是 0。</div></figure>'
        )
    )
