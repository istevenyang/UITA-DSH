"""``core/perf`` — 客户端性能采集（``--mode perf``）。

模块划分（依赖方向：``core/perf`` 只依赖 ``core/dsl``、``core/result``、
``core/lifecycle``、``core/interpreter``；不 import ``engine/``、``apps/``、``cli/``、``tools/``）：

| 模块 | 职责 |
|---|---|
| ``metrics.py``      | 指标聚合（**真实值口径** count/min/avg/max，2026-10-08 起不含百分位、2026-10-12 起不含 cv 派生统计；``percentile()`` 仅作 ``core/report_portal/tasks/exec_stats.py`` 口径锚点保留） |
| ``sampler.py``      | 进程树资源采样（RSS / CPU% / 线程数 / fd） |
| ``launch_timer.py`` | 冷启动分段计时（旧 PID 消失 → 新主进程出现 → CDP 可连 → 窗口左上角品牌渲染完成） |
| ``logs.py``         | 应用日志目录 → zip 打包（压测产物，纯 stdlib） |
| ``console.py``      | 控制台精简输出（只留轮次/节点耗时/失败，过程日志只进文件；纯 stdlib） |
| ``memload.py``      | 内存负载机制（逐页非零 + re-touch + best-effort 锁页 + **驻留自证**；纯 stdlib + psutil） |
| ``baseline.py``     | 基线加载（App 级优先、全局兜底）与阈值判定 |
| ``runner.py``       | ``PerfRunner`` 多轮编排（生命周期 + 计时 + 采样 + 逐轮 TCS + 场景上下文注入） |
| ``report.py``       | 落盘 ``perf_summary.json`` / ``perf_report.html`` + ``build_ingest_payload()`` 预留 |

计划文档：``docs/plans/done/archive_202609.md``
"""
from __future__ import annotations

__all__ = [
    "baseline",
    "console",
    "launch_timer",
    "memload",
    "metrics",
    "report",
    "runner",
    "sampler",
]
