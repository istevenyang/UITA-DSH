"""主机性能基准：CPU / 内存带宽 / 磁盘 IO 三项轻量基准 + 加权综合跑分。

零新增第三方依赖（R13）：只用 ``requirements.txt`` 已声明的 ``numpy``/``psutil``。

**用途**：同一份 ``perf_summary.json`` 在不同主机上跑出来的绝对耗时/RSS 数字不可比——
不知道这次结果差是"改动变慢了"还是"换了台弱一点的机器"。本模块在每次 ``--mode perf``
调用最早阶段（跑任何用例/构建样本库之前）跑一次基准，把主机规格与相对跑分随报告落盘、
且在终端打印，供事后审计。

**权重设计依据**（通读 ``~/demogallery-pc/openspec/specs`` 性能相关 capability 的结论，
2026-09-20）：
  - ``local-gallery-thumbnail-browsing``（缩略图/视频解码、后台预生成）+
    ``local-gallery-incremental-patch-performance``（增量 delta 内存 patch）
    → CPU 密集型工作负载显著，是图库交互体验的主要瓶颈。
  - ``local-photo-index-query-performance`` + 全量扫描（>20000 条目大目录）/
    数万张持久化缩略图缓存读写 → 磁盘 IO 是扫描与缓存路径的主要瓶颈。
  - 内存主要影响大库场景下的容量与稳定性，差异化程度相对低于前两者。
  - 综合定为 **CPU 40% / 磁盘 IO 35% / 内存 25%**。

**局限性（必须在报告中同样说明）**：这不是行业标准跑分工具，只是粗粒度、可审计的
相对参考，仅用于"同批次/跨机器"的相对对比，不能作为绝对性能评级。``numpy.dot``
的多线程程度依赖运行环境的 BLAS 后端（OpenBLAS/MKL/Accelerate），不同环境的 GFLOPS
基线可能有系统性差异。
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import tempfile
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("uita")

try:  # pragma: no cover - 环境相关
    import psutil  # type: ignore

    _HAS_PSUTIL = True
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore
    _HAS_PSUTIL = False

try:  # pragma: no cover - 环境相关
    import numpy as np  # type: ignore

    _HAS_NUMPY = True
except ImportError:  # pragma: no cover
    np = None  # type: ignore
    _HAS_NUMPY = False

__all__ = [
    "HOST_BENCH_WEIGHTS",
    "REFERENCE_ANCHORS",
    "benchmark_host",
    "collect_host_spec",
]

#: 各基准子项权重（见模块 docstring「权重设计依据」）
HOST_BENCH_WEIGHTS: dict[str, float] = {"cpu": 0.40, "disk": 0.35, "memory": 0.25}

#: 归一化参考锚点（经验值，非行业标准；score = raw / anchor * 100）
REFERENCE_ANCHORS: dict[str, float] = {
    "cpu_gflops": 20.0,
    "memory_mb_s": 8000.0,
    "disk_mb_s": 300.0,
}


def collect_host_spec() -> dict[str, Any]:
    """采集主机规格标签（如 ``"8C16G"``）及明细字段。

    ``label`` 优先用物理核心数（更贴近真实算力），缺失时退回逻辑核心数；
    任一子项不可用时该字段为 ``None``，``label`` 记 ``"?"``（不猜测）。
    """
    logical = os.cpu_count() or None
    physical: Optional[int] = None
    mem_gb: Optional[int] = None
    if _HAS_PSUTIL:
        try:
            physical = psutil.cpu_count(logical=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[perf] 采集物理核心数失败: %s", exc)
        try:
            mem_gb = round(psutil.virtual_memory().total / (1024 ** 3))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[perf] 采集内存总量失败: %s", exc)

    cores_for_label = physical or logical
    label = f"{cores_for_label}C{mem_gb}G" if cores_for_label and mem_gb else "?"

    cpu_name = ""
    try:
        cpu_name = platform.processor() or platform.machine() or ""
    except Exception:  # noqa: BLE001
        cpu_name = ""

    return {
        "label": label,
        "logical_cores": logical,
        "physical_cores": physical,
        "memory_gb": mem_gb,
        "platform": platform.system(),
        "cpu_name": cpu_name,
    }


def _bench_cpu(duration_s: float = 0.5) -> Optional[float]:
    """CPU 基准：方阵乘法测 GFLOPS（BLAS 多线程天然反映多核算力，不用手写多进程）。"""
    if not _HAS_NUMPY:
        return None
    try:
        n = 400
        a = np.random.rand(n, n).astype(np.float64)
        b = np.random.rand(n, n).astype(np.float64)
        flops_per_matmul = 2 * (n ** 3)
        iterations = 0
        t0 = time.perf_counter()
        deadline = t0 + duration_s
        while time.perf_counter() < deadline:
            np.dot(a, b)
            iterations += 1
        elapsed = time.perf_counter() - t0
        if elapsed <= 0 or iterations == 0:
            return None
        gflops = (flops_per_matmul * iterations) / elapsed / 1e9
        return round(gflops, 2)
    except Exception as exc:  # noqa: BLE001 - 基准失败绝不影响主流程
        logger.warning("[perf] CPU 基准测试失败: %s", exc)
        return None


def _bench_memory(duration_s: float = 0.4, size_mb: int = 256) -> Optional[float]:
    """内存基准：大数组循环拷贝测带宽（MB/s，读+写合计）。"""
    if not _HAS_NUMPY:
        return None
    try:
        n = (size_mb * 1024 * 1024) // 8  # float64 = 8 bytes
        src = np.random.rand(n)
        dst = np.empty_like(src)
        iterations = 0
        t0 = time.perf_counter()
        deadline = t0 + duration_s
        while time.perf_counter() < deadline:
            np.copyto(dst, src)
            iterations += 1
        elapsed = time.perf_counter() - t0
        if elapsed <= 0 or iterations == 0:
            return None
        total_mb = size_mb * iterations * 2  # 读 + 写
        return round(total_mb / elapsed, 1)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[perf] 内存基准测试失败: %s", exc)
        return None


def _bench_disk(size_mb: int = 128) -> Optional[float]:
    """磁盘 IO 基准：临时目录顺序写 + 顺序读，测 MB/s（用完立即删除临时目录）。

    只落在系统临时目录（``tempfile.mkdtemp``），不落在用户/工作区目录；
    单文件删除，不触发批量删除护栏（见 ``core/perf/lib_builder.py`` 同类教训）。
    """
    tmp_dir: Optional[str] = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="uita-perf-diskbench-")
        path = os.path.join(tmp_dir, "bench.bin")
        chunk = os.urandom(1024 * 1024)

        t0 = time.perf_counter()
        with open(path, "wb") as fh:
            for _ in range(size_mb):
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        write_elapsed = time.perf_counter() - t0

        t1 = time.perf_counter()
        with open(path, "rb") as fh:
            while fh.read(1024 * 1024):
                pass
        read_elapsed = time.perf_counter() - t1

        total_elapsed = write_elapsed + read_elapsed
        if total_elapsed <= 0:
            return None
        return round((size_mb * 2) / total_elapsed, 1)
    except OSError as exc:
        logger.warning("[perf] 磁盘基准测试失败: %s", exc)
        return None
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _normalize(value: Optional[float], anchor: float) -> Optional[float]:
    if value is None or anchor <= 0:
        return None
    return round(value / anchor * 100.0, 1)


def _composite_score(scores: dict[str, Optional[float]]) -> Optional[float]:
    """按剩余有效权重归一化计算综合分（部分子项缺失时仍可算出）。"""
    valid = {k: v for k, v in scores.items() if v is not None}
    if not valid:
        return None
    weight_sum = sum(HOST_BENCH_WEIGHTS[k] for k in valid)
    if weight_sum <= 0:
        return None
    weighted = sum(v * HOST_BENCH_WEIGHTS[k] for k, v in valid.items())
    return round(weighted / weight_sum, 1)


def benchmark_host(on_log: Optional[Callable[[str], None]] = None) -> dict[str, Any]:
    """执行主机基准测试并返回结果（规格 + 原始值 + 归一化分 + 权重 + 综合分）。

    在 ``--mode perf`` 处理入口最早阶段调用一次（早于用例执行/样本库构建）；
    任何子项异常都降级为该项 ``None``，绝不抛异常影响主流程。

    Args:
        on_log: 日志回调（默认不打印；CLI 层传 ``print`` 以在终端展示进度）。
    """
    log = on_log or (lambda _msg: None)

    spec = collect_host_spec()
    log(
        f"[perf] 主机规格: {spec['label']}"
        f"（逻辑核 {spec['logical_cores']}，物理核 {spec['physical_cores']}，"
        f"内存 {spec['memory_gb']}GB，{spec['platform']}）",
    )

    log("[perf] 开始主机性能基准测试…")
    cpu_gflops = _bench_cpu()
    log(f"[perf] CPU 基准: {cpu_gflops if cpu_gflops is not None else '不可用'} GFLOPS")
    memory_mb_s = _bench_memory()
    log(f"[perf] 内存带宽基准: {memory_mb_s if memory_mb_s is not None else '不可用'} MB/s")
    disk_mb_s = _bench_disk()
    log(f"[perf] 磁盘 IO 基准: {disk_mb_s if disk_mb_s is not None else '不可用'} MB/s")

    scores = {
        "cpu": _normalize(cpu_gflops, REFERENCE_ANCHORS["cpu_gflops"]),
        "memory": _normalize(memory_mb_s, REFERENCE_ANCHORS["memory_mb_s"]),
        "disk": _normalize(disk_mb_s, REFERENCE_ANCHORS["disk_mb_s"]),
    }
    composite = _composite_score(scores)
    if composite is not None:
        log(f"[perf] 主机综合跑分: {composite}（CPU 40% / 磁盘 35% / 内存 25%，满分基准=100）")
    else:
        log("[perf] 主机综合跑分: 不可用（全部基准子项均失败）")

    return {
        "spec": spec,
        "raw": {"cpu_gflops": cpu_gflops, "memory_mb_s": memory_mb_s, "disk_mb_s": disk_mb_s},
        "scores": scores,
        "weights": dict(HOST_BENCH_WEIGHTS),
        "anchors": dict(REFERENCE_ANCHORS),
        "composite_score": composite,
    }
