"""core/perf/series.py — 采样点与事件的时间序列落盘（JSONL）。

为什么需要：``ResourceSampler`` 的采样点只存在内存里（`:attr:`samples` 返回拷贝），
跑完只剩聚合值，"那一刻 CPU 到底长什么样"再也回不来。性能结论一旦被质疑
（"到底是不是在回落之后点的？"），没有原始点就无法复盘。

产物形态（每行一个 JSON 对象）::

    {"kind": "meta",   "meta": {...}}                 # 首行：进程名/间隔/轮次等
    {"kind": "sample", "t_ms": 1234, "rss_mb": 210.4, "cpu_percent": 42.1, ...}
    {"kind": "event",  "t_ms": 5678, "event": "click_done", "page": "software_mgr"}

纪律：``t_ms`` 与 ``ResourceSample.t_ms`` 同源（``int(time.monotonic()*1000)``），
事件与采样点因此可以直接叠在同一根时间轴上。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Iterable, Optional, Sequence

logger = logging.getLogger("uita")

__all__ = [
    "SAMPLE_FIELDS",
    "append_event",
    "read_samples_jsonl",
    "select_window",
    "write_samples_jsonl",
]

#: 落盘的采样字段白名单（避免把引擎内部临时字段一起写出去）。
SAMPLE_FIELDS: tuple[str, ...] = (
    "t_ms",
    "rss_mb",
    "cpu_percent",
    "thread_count",
    "num_fds",
    "proc_count",
    "pid_fallback",
)


def _sample_row(sample: Any) -> dict[str, Any]:
    """把一只样本归一化为白名单 dict（对象/dict 都吃）。"""
    row: dict[str, Any] = {"kind": "sample"}
    for field in SAMPLE_FIELDS:
        if isinstance(sample, dict):
            row[field] = sample.get(field)
        else:
            row[field] = getattr(sample, field, None)
    return row


def write_samples_jsonl(
    path: str,
    samples: Sequence[Any],
    *,
    meta: Optional[dict[str, Any]] = None,
    events: Optional[Iterable[dict[str, Any]]] = None,
) -> str:
    """把采样点与事件写成 JSONL；返回实际写入的路径（失败返回空串）。

    失败只告警不抛：这是**辅助产物**，不该让一次已经跑完的性能测量在最后一步崩掉。
    """
    if not path:
        return ""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        rows: list[dict[str, Any]] = [_sample_row(s) for s in (samples or [])]
        rows.extend({"kind": "event", **ev} for ev in (events or []))
        # 采样点与事件**按时间合并排序**：分开落盘会让时间轴读起来是两段，
        # 复盘时"这个事件发生在第几个点之间"要人工对齐。
        rows.sort(key=lambda r: r.get("t_ms") if isinstance(r.get("t_ms"), (int, float)) else -1)
        with open(path, "w", encoding="utf-8") as handle:
            if meta:
                handle.write(json.dumps({"kind": "meta", "meta": meta}, ensure_ascii=False) + "\n")
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        return path
    except Exception as exc:  # noqa: BLE001
        logger.warning("[perf.series] 采样点落盘失败（%s）: %s", path, exc)
        return ""


def read_samples_jsonl(path: str) -> list[dict[str, Any]]:
    """读回 JSONL（逐行解析，坏行跳过并计数）。文件不存在返回空列表。"""
    rows: list[dict[str, Any]] = []
    if not path or not os.path.exists(path):
        return rows
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
    except OSError as exc:
        logger.debug("[perf.series] 读取失败（%s）: %s", path, exc)
    return rows


def select_window(
    rows: Iterable[dict[str, Any]], t0_ms: int, t1_ms: int, *, kind: str = "sample",
) -> list[dict[str, Any]]:
    """取 ``[t0, t1)`` 内的行（左闭右开，与 ``sampler.summary_between`` 同口径）。"""
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if row.get("kind") != kind:
            continue
        t = row.get("t_ms")
        if not isinstance(t, (int, float)):
            continue
        if int(t0_ms) <= t < int(t1_ms):
            out.append(row)
    return out


def append_event(path: str, t_ms: int, event: str, **payload: Any) -> None:
    """追加一条事件（用于边跑边写；比整轮重写更省内存）。"""
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        row = {"kind": "event", "t_ms": int(t_ms), "event": str(event)}
        row.update(payload)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.debug("[perf.series] 事件追加失败: %s", exc)
