"""perf AI 分析：口径词典（"这些数字该怎么读"的唯一来源）。

为什么要有它：AI 分析报告要"给每条数据一句结论"，而结论的前提是**口径**——
``startup_ms`` 与 ``launch_ms`` 差在哪、CPU% 为什么能超过 100%、"—" 到底是不是 0、
缩略图的"均值"为什么被热轮稀释……这些口径原本散落在 ``core/perf/report.py`` 的注释
与各场景模块里，模型看不到就必然自己编。故这里把它们收敛成一份**结构化词典**，
随 facts 一起交给分析 agent，并由它渲染报告的「附A 采集口径」骨架。

分层（守 R2）：通用口径住 ``core``（本文件），**应用专有口径**住
``apps/<app>/perf/analysis_notes.py::EXTRA_CALIBERS``（如 sample_media 的"缩略图覆盖率闸门"），
按 app 动态加载（机制同 ``REPORT_KEYS`` / ``PRESETS``）；app 未声明即零影响。
"""
from __future__ import annotations

import importlib
import logging
from typing import Any, Iterable

logger = logging.getLogger("uita")

__all__ = [
    "APP_ANALYSIS_NOTES_TEMPLATE",
    "CALIBERS",
    "EXTRA_CALIBERS_ATTR",
    "caliber_ids",
    "caliber_index",
    "load_app_analysis_notes",
    "load_calibers",
]

#: app 侧"专有口径"模块路径模板（``EXTRA_CALIBERS`` 常量）
APP_ANALYSIS_NOTES_TEMPLATE = "apps.{app}.perf.analysis_notes"
EXTRA_CALIBERS_ATTR = "EXTRA_CALIBERS"

#: 通用口径（``id`` 稳定，报告与 facts 都按 id 引用；``zh`` 是给读者/模型看的标题）
CALIBERS: tuple[dict[str, str], ...] = (
    {
        "id": "stat_triple",
        "zh": "「均值 / 最小值 / 最大值」怎么读",
        "text": (
            "报告表内 `A / B / C` = 该指标**跨轮（--perf-repeat 轮）的均值 / 最小值 / 最大值**；"
            "只给一个数的指标，本身已是跨轮聚合值。`*_peak` = 跨轮最高峰值，`*_avg` = 跨轮平均，"
            "比率/计数类字段 = 跨轮最差值（口径见 core/perf/metrics.py::summarize）。"
        ),
    },
    {
        "id": "missing_value",
        "zh": "「—」的语义",
        "text": (
            "`—` = **该档该指标没有有效数据**（场景未执行完 / 被中断 / 空库无样本），"
            "**不是 0，也不做任何推测**。每个 `—` 都必须在报告里给出原因"
            "（不可测 / 场景异常 / 探针未返回数据 / 已跳过）。"
        ),
    },
    {
        "id": "startup_ms",
        "zh": "启动耗时只认 startup_ms",
        "text": (
            "`startup_ms` = 发起启动 → 主窗口出现**且**左上角品牌渲染完成（app 侧就绪探针；"
            "无该模块的 app 退回 `document.readyState === 'complete'`）。"
            "产物里另有 `launch_ms`，它是**测试框架口径**（含「关掉旧实例 + 等旧进程退出」），"
            "会因上一轮收尾耗时剧烈抖动，**不代表用户体感**，只在口径说明里出现。"
        ),
    },
    {
        "id": "cpu_percent",
        "zh": "CPU% 是 psutil 语义（可 >100%）",
        "text": (
            "psutil 语义：单逻辑核满载 = 100%，多核累加。N 逻辑核的机器**满载 = 100×N%**"
            "（如 2 核机器 = 200%）。判读前必须先看 `host.spec` 的核数，"
            "否则会把「接近满载」读成「超额 200%」或反之。"
        ),
    },
    {
        "id": "rss_scope",
        "zh": "内存口径 = 进程树合计",
        "text": (
            "`rss_mb_*` = 被测应用**进程树合计**（主进程 + 子进程，含 Electron helper / 渲染进程），"
            "单位 MB，非单进程读数；跨工具/跨产品不可直接比大小。"
            "占比结论要用 `host` 里的系统总内存作分母。"
        ),
    },
    {
        "id": "scan_padding",
        "zh": "扫描耗时含固定收敛 padding",
        "text": (
            "`scan_duration_ms` = `scan_time_to_target_ms`（真实扫描到目标张数）+ "
            "`converge_padding_ms`（固定收敛确认等待，与库容无关，通常 ~2s）。"
            "**含 padding 的单张成本会掩盖真实趋势**：必须同时给「真实扫描」与「总耗时」两个口径，"
            "单张成本用真实扫描算（总耗时/张数是错的）。"
        ),
    },
    {
        "id": "hot_round_dilution",
        "zh": "冷加载均值会被热轮稀释",
        "text": (
            "缩略图 / 视频解码这类走 OS 文件缓存的场景，**只有第 1 轮是真冷**，"
            "r2/r3 命中缓存后降到毫秒级；跨轮「均值」里混了热轮，"
            "**看冷加载必须看 r1 单轮值**（冷热比可达 1:300）。"
        ),
    },
    {
        "id": "stress_semantics",
        "zh": "压测（stress）语义",
        "text": (
            "先关掉应用 → **逐级挤占系统内存**（默认步进 250MB，防压缩/防释放）→ 每级尝试**冷启动**应用 → "
            "直到**启动失败**；失败瞬间记录系统已占用 / 总内存 / 剩余可用 / 占比 + Swap。"
            "无占用上限、无内存阈值；`fail_kind=start_timeout` 表示「启动超时失败」（不是内存分配失败）。"
        ),
    },
    {
        "id": "launch_failure",
        "zh": "启动失败现场与重试",
        "text": (
            "启动失败自动重试（1 次首发 + 2 次重试）：**每一次失败都记录那一刻的系统内存快照**"
            "（`launch_attempts[]`，字段与 stress 逐级记录同口径），重试用尽才中止本轮"
            "（跳过本轮余下步骤与场景）；报告必须把**每一次失败**都完整呈现，不能只给末次。"
        ),
    },
    {
        "id": "multi_tier",
        "zh": "多单元/多档位的读法",
        "text": (
            "默认多单元编排（未给 `--perf-tier` 但给了 `--perf-src`）按 app 声明的 `UNIT_SEQUENCE` "
            "依次执行（sample_media：`0k → 5k → 10k → 50k → stress`），各单元产物在 `tier-<name>/`，"
            "顶层是聚合报告。**某单元本次未声明执行某场景时不要报「未采集」**；"
            "声明了跑但无数据才要给出原因。"
        ),
    },
    {
        "id": "baseline_disabled",
        "zh": "性能判定未启用",
        "text": (
            "本框架当前**不做达标/违规判定**（`verdict.mode=off`、`gate=disabled`、`thresholds={}`），"
            "只呈现实测值。因此「问题优先级 P0/P1/P2」是**分析结论**，不是框架判定结果——"
            "必须给现象 / 证据 / 影响 / 建议四段，并明确证据出处（指标名 + 档位 + 轮次）。"
        ),
    },
    {
        "id": "host_bench",
        "zh": "主机算力基准的用途与局限",
        "text": (
            "`host` 含主机规格标签与三项基准（CPU GFLOPS / 内存带宽 MB/s / 磁盘 IO MB/s）及归一化分。"
            "**非行业标准跑分，只用于同批次/跨机器相对对比**：跨批次比数值前必须先用它核对算力"
            "（不同机器/核数下的绝对耗时不可直接比大小）。"
        ),
    },
)


def caliber_ids() -> tuple[str, ...]:
    """全部通用口径 id（供单测与文档核对）。"""
    return tuple(item["id"] for item in CALIBERS)


def caliber_index(calibers: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """口径列表 → ``{id: {"zh": ..., "text": ...}}``（重复 id 后者覆盖前者，便于 app 覆写）。"""
    out: dict[str, dict[str, Any]] = {}
    for item in calibers:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip()
        if not cid:
            continue
        out[cid] = {"zh": str(item.get("zh") or cid), "text": str(item.get("text") or "")}
    return out


def load_app_analysis_notes(
    app_name: str, *, module_template: str | None = None,
) -> list[dict[str, Any]]:
    """动态加载 app 侧专有口径（``EXTRA_CALIBERS``），无则返回 ``[]``。

    与 ``load_app_presets`` 同款策略：**app 未提供是常态**（跳过）；但模块内部的 import 错误
    会原样抛出（配置/代码错误必须暴露，不静默吞掉）。
    """
    app = (app_name or "").strip()
    if not app:
        return []
    module_path = (module_template or APP_ANALYSIS_NOTES_TEMPLATE).format(app=app)
    try:
        module = importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        missing = str(getattr(exc, "name", "") or "")
        if missing and (missing == module_path or module_path.startswith(missing + ".")):
            logger.debug("[perf-ai] app=%s 未提供专有口径模块 %s（跳过）", app, module_path)
            return []
        raise
    raw = getattr(module, EXTRA_CALIBERS_ATTR, None)
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError(f"{module_path}.{EXTRA_CALIBERS_ATTR} 必须是列表，实际是 {type(raw).__name__}")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            raise ValueError(f"{module_path}.{EXTRA_CALIBERS_ATTR}[{index}] 必须含非空 id")
        out.append({"id": str(item["id"]).strip(), "zh": str(item.get("zh") or ""), "text": str(item.get("text") or "")})
    logger.info("[perf-ai] app=%s 追加专有口径 %d 条", app, len(out))
    return out


def load_calibers(app_name: str = "") -> list[dict[str, Any]]:
    """通用口径 + app 专有口径（app 同名 id 覆写 core 版本）。"""
    merged = [dict(item) for item in CALIBERS]
    merged.extend(load_app_analysis_notes(app_name))
    return merged
