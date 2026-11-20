"""core/perf/curve.py — 单轮内**按时间**的 CPU/内存曲线（内联 SVG，调试用）。

两个出口：

- :func:`render_curve_svg`：单轮调试曲线（CPU + RSS 双轴 + 事件竖线），回答"这一轮的
  render_ms 是怎么来的、拐点/点击/命中发生在哪一刻"。默认**不写进 HTML 报告**，
  只有 ``--perf-scenario-opt curves=debug`` 时才落盘为 ``curves/r{NN}_{page}.svg``；
- :func:`render_time_curves`：多序列共用时间轴的曲线（横轴 = 距窗口起点时长），供报告
  呈现「启动 → 点击」固定窗口内多轮采样形态（序列名/单位由调用方给，同图同一量纲）。

口径与报告其余部分一致：缺失点断开、不补 0；无数据给提示而不是空图。
"""
from __future__ import annotations

import html
import logging
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger("uita")

__all__ = ["render_curve_svg", "render_time_curves"]

_WIDTH = 720
_HEIGHT = 220
_PAD = 34

#: 多序列时间曲线的画布（比单轮调试曲线宽一点：图里通常叠 2–3 轮）
_MULTI_WIDTH = 900
_MULTI_HEIGHT = 240
_MULTI_PAD_LEFT = 56
_MULTI_PAD_RIGHT = 18
_MULTI_PAD_TOP = 16
_MULTI_PAD_BOTTOM = 32


def _fmt(value: float) -> str:
    """刻度文本：整数不带小数点，其余保留 1 位。"""
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


def _pair(row: Any) -> Optional[tuple[int, float, Optional[float]]]:
    """从采样行取 ``(t_ms, cpu, rss)``；缺 cpu 的行丢弃。"""
    if isinstance(row, dict):
        t, cpu, rss = row.get("t_ms"), row.get("cpu_percent"), row.get("rss_mb")
    else:
        t = getattr(row, "t_ms", None)
        cpu = getattr(row, "cpu_percent", None)
        rss = getattr(row, "rss_mb", None)
    if not isinstance(t, (int, float)) or not isinstance(cpu, (int, float)):
        return None
    return int(t), float(cpu), float(rss) if isinstance(rss, (int, float)) else None


def render_curve_svg(
    samples: Sequence[Any],
    *,
    title: str = "",
    events: Optional[Iterable[dict[str, Any]]] = None,
    width: int = _WIDTH,
    height: int = _HEIGHT,
) -> str:
    """渲染单轮时序曲线（左轴 CPU%、右轴 RSS MB，事件画竖线）。

    Args:
        samples: 采样点（``t_ms`` / ``cpu_percent`` / ``rss_mb``）。
        title: 图标题。
        events: ``[{"t_ms":..,"event":"click_done",...}]``；只用来标竖线。
        width / height: 画布尺寸。

    Returns:
        内联 SVG 字符串；无有效点时为一句提示。
    """
    rows = [p for p in (_pair(s) for s in samples or []) if p]
    if len(rows) < 2:
        return '<span class="hint">采样点不足，无法绘制时序曲线</span>'

    rows.sort(key=lambda r: r[0])
    t0, t1 = rows[0][0], rows[-1][0]
    span = max(1, t1 - t0)

    cpu_max = max(r[1] for r in rows) or 1.0
    rss_values = [r[2] for r in rows if r[2] is not None]
    rss_min = min(rss_values) if rss_values else 0.0
    rss_max = max(rss_values) if rss_values else 0.0
    if rss_max <= rss_min:
        rss_max = rss_min + 1.0

    plot_w = max(1, width - _PAD * 2)
    plot_h = max(1, height - _PAD * 2)

    def x_of(t_ms: int) -> float:
        return _PAD + (t_ms - t0) / span * plot_w

    def y_cpu(value: float) -> float:
        return _PAD + plot_h - value / max(1e-9, cpu_max) * plot_h

    def y_rss(value: float) -> float:
        return _PAD + plot_h - (value - rss_min) / (rss_max - rss_min) * plot_h

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" '
        f'aria-label="{html.escape(title or "资源曲线")}">'
    ]
    for frac in (0.0, 0.5, 1.0):
        y = _PAD + plot_h * frac
        parts.append(
            f'<line x1="{_PAD}" y1="{y:.1f}" x2="{_PAD + plot_w}" y2="{y:.1f}" '
            'stroke="#e6e9ef" stroke-width="1" />'
        )

    cpu_coords = " ".join(f"{x_of(r[0]):.1f},{y_cpu(r[1]):.1f}" for r in rows)
    parts.append(
        f'<polyline fill="none" stroke="#2f6fed" stroke-width="1.6" points="{cpu_coords}" />'
    )
    rss_coords = " ".join(f"{x_of(r[0]):.1f},{y_rss(r[2]):.1f}" for r in rows if r[2] is not None)
    if rss_coords:
        parts.append(
            f'<polyline fill="none" stroke="#e08a2f" stroke-width="1.4" '
            f'stroke-dasharray="4 3" points="{rss_coords}" />'
        )

    for event in events or []:
        t_ms = event.get("t_ms")
        if not isinstance(t_ms, (int, float)) or not (t0 <= t_ms <= t1):
            continue
        x = x_of(int(t_ms))
        label = str(event.get("event") or "")
        parts.append(
            f'<line x1="{x:.1f}" y1="{_PAD}" x2="{x:.1f}" y2="{_PAD + plot_h}" '
            f'stroke="#b0459a" stroke-width="1" stroke-dasharray="3 3">'
            f"<title>{html.escape(label)}</title></line>"
        )
        parts.append(
            f'<text x="{x + 3:.1f}" y="{_PAD + 10}" font-size="9" fill="#b0459a">'
            f"{html.escape(label)}</text>"
        )

    parts.append(
        f'<text x="{_PAD}" y="{height - 10}" font-size="10" fill="#6b7280">'
        f"0s</text>"
        f'<text x="{_PAD + plot_w}" y="{height - 10}" text-anchor="end" font-size="10" '
        f'fill="#6b7280">{span / 1000:.1f}s</text>'
    )
    parts.append(
        f'<text x="{_PAD}" y="{_PAD - 12}" font-size="10" fill="#2f6fed">'
        f"CPU 峰值 {cpu_max:.1f}%</text>"
    )
    if rss_values:
        parts.append(
            f'<text x="{_PAD + plot_w}" y="{_PAD - 12}" text-anchor="end" font-size="10" '
            f'fill="#e08a2f">RSS {rss_min:.0f}–{rss_max:.0f} MB</text>'
        )
    parts.append("</svg>")

    caption = f'<figcaption>{html.escape(title)}</figcaption>' if title else ""
    return f'<figure class="perf-curve">{caption}{"".join(parts)}</figure>'


def _normalize_lines(
    lines: Sequence[Any],
) -> list[tuple[str, list[tuple[int, float]]]]:
    """把外部传入的序列归一化成 ``[(名称, [(t_ms, 值), ...]), ...]``（非法点丢弃、按时间排序）。

    只认数值点（``bool`` 不算数）；空序列直接丢掉——避免图里出现"零点的空图例"。
    """
    out: list[tuple[str, list[tuple[int, float]]]] = []
    for item in lines or []:
        if not isinstance(item, dict):
            continue
        points: list[tuple[int, float]] = []
        for point in item.get("points") or []:
            try:
                t_ms, value = point[0], point[1]
            except (TypeError, IndexError, KeyError):
                continue
            if isinstance(t_ms, bool) or isinstance(value, bool):
                continue
            if not isinstance(t_ms, (int, float)) or not isinstance(value, (int, float)):
                continue
            points.append((int(t_ms), float(value)))
        if not points:
            continue
        points.sort(key=lambda p: p[0])
        out.append((str(item.get("name") or ""), points))
    return out


def render_time_curves(
    lines: Sequence[Any],
    *,
    title: str = "",
    y_unit: str = "",
    x_unit: str = "s",
    width: int = _MULTI_WIDTH,
    height: int = _MULTI_HEIGHT,
) -> str:
    """渲染**多条序列共用时间轴**的曲线（横轴 = 距起点时长，从 0 起）。

    用途：「启动 → 点击」这类固定窗口里，把多轮（或多个来源）的采样点叠在同一张图上比形态。
    与 :func:`render_curve_svg` 的区别：那个固定画 CPU+RSS 两条线、只服务单轮调试；本函数
    序列名与单位由调用方给（同一张图只放同一量纲的线，避免双轴误读）。

    Args:
        lines: ``[{"name": str, "points": [(t_ms, 值), ...]}]``；``t_ms`` 为距窗口起点的毫秒。
        title: 图标题（同时作 ``aria-label``）。
        y_unit: Y 轴单位（``%`` / ``MB``），用于刻度与图例。
        x_unit: X 轴单位文本（默认秒）。
        width / height: 画布尺寸（``viewBox``，宽度自适应）。

    Returns:
        ``<figure class="perf-curve">`` 包裹的内联 SVG；无足够采样点（< 2）时返回提示文案，
        不渲染误导性的空图（与报告其余部分同一纪律）。
    """
    from uita.core.perf.chart import DEFAULT_COLORS, nice_ticks

    normalized = _normalize_lines(lines)
    total_points = sum(len(points) for _, points in normalized)
    if total_points < 2:
        return (
            '<span class="hint">采样点不足，无法绘制 '
            f'{html.escape(title or "时序曲线")}（需 ≥2 个有效点）</span>'
        )

    span = max(1, max(points[-1][0] for _, points in normalized))
    top = max(value for _, points in normalized for _, value in points)
    hi = top * 1.08 if top > 0 else 1.0
    lo = 0.0

    plot_w = max(1, width - _MULTI_PAD_LEFT - _MULTI_PAD_RIGHT)
    plot_h = max(1, height - _MULTI_PAD_TOP - _MULTI_PAD_BOTTOM)

    def x_of(t_ms: float) -> float:
        return _MULTI_PAD_LEFT + (t_ms / span) * plot_w

    def y_of(value: float) -> float:
        return _MULTI_PAD_TOP + plot_h - (value - lo) / (hi - lo) * plot_h

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}" width="100%" height="{height}" role="img" '
        f'aria-label="{html.escape(title or "资源曲线")}">'
    ]
    for tick in nice_ticks(lo, hi, 4):
        y = y_of(tick)
        if y < _MULTI_PAD_TOP - 0.5 or y > _MULTI_PAD_TOP + plot_h + 0.5:
            continue
        parts.append(
            f'<line x1="{_MULTI_PAD_LEFT}" y1="{y:.1f}" x2="{_MULTI_PAD_LEFT + plot_w}" '
            f'y2="{y:.1f}" stroke="#e6e9ef" stroke-width="1" />'
        )
        unit = html.escape(y_unit)
        parts.append(
            f'<text x="{_MULTI_PAD_LEFT - 8}" y="{y + 3.5:.1f}" text-anchor="end" '
            f'font-size="10" fill="#6b7280">{_fmt(tick)}{unit}</text>'
        )
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        t_ms = span * frac
        parts.append(
            f'<text x="{x_of(t_ms):.1f}" y="{_MULTI_PAD_TOP + plot_h + 14:.1f}" '
            f'text-anchor="middle" font-size="10" fill="#6b7280">'
            f"{t_ms / 1000:.1f}{html.escape(x_unit)}</text>"
        )
    parts.append(
        f'<line x1="{_MULTI_PAD_LEFT}" y1="{_MULTI_PAD_TOP + plot_h}" '
        f'x2="{_MULTI_PAD_LEFT + plot_w}" y2="{_MULTI_PAD_TOP + plot_h}" '
        'stroke="#cbd2dc" stroke-width="1" />'
    )

    legend: list[str] = []
    for order, (name, points) in enumerate(normalized):
        color = DEFAULT_COLORS[order % len(DEFAULT_COLORS)]
        coords = " ".join(f"{x_of(t):.1f},{y_of(v):.1f}" for t, v in points)
        parts.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="1.5" '
            f'stroke-linejoin="round" points="{coords}" />'
        )
        last_t, last_v = points[-1]
        parts.append(
            f'<circle cx="{x_of(last_t):.1f}" cy="{y_of(last_v):.1f}" r="2.2" fill="{color}">'
            f"<title>{html.escape(name)} {last_v:.1f}{html.escape(y_unit)}"
            f"（{last_t / 1000:.1f}{html.escape(x_unit)}）</title></circle>"
        )
        legend.append(
            f'<span class="legend-item"><span class="legend-dot" style="background:{color}"></span>'
            f"{html.escape(name or '—')}（末值 {last_v:.1f}{html.escape(y_unit)}）</span>"
        )
    parts.append("</svg>")

    caption = f"<figcaption>{html.escape(title)}</figcaption>" if title else ""
    return (
        f'<figure class="perf-curve">{caption}{"".join(parts)}'
        f'<div class="legend">{"".join(legend)}</div></figure>'
    )
