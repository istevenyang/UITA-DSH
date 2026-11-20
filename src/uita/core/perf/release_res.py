"""perf 素材「按盘符释放」的规划与体积估算（Windows 独有）。

场景：Windows 测试机的系统盘通常很小，而 10k/50k 档位的样本库是数十 GB **真实占用**
（Windows 无克隆语义，见 ``core/perf/lib_builder.py`` 模块 docstring）。``--release-res e``
让样本库落到 ``E:\\perf`` 而不是 ``%USERPROFILE%\\Pictures\\perf``。

规划规则（与 ``--release-res`` 的验收口径一一对应）：

1. 指定盘 ``剩余空间 ≥ 需要体积`` → 直接释放到 ``<盘>:\\perf``；
2. 否则在其他本地盘里找 ``剩余空间 ≥ 需要体积`` 的盘（剩余最多的优先）→ 释放到该盘，
   并打 ``warning`` 日志说明"指定盘不足、已改盘"；
3. 所有单盘都放不下 → 按每盘 ``min(剩余空间, 80% 总容量)`` 的额度**拆分**释放到多个盘，
   每个盘一个分片目录 ``<盘>:\\perf``；
4. 所有盘额度合计仍不足 → :class:`ReleaseResError`（**不**做"少放一点"的静默降级：
   份数不足会让 ``scan`` 的规模口径失真）。

本模块只依赖标准库 + ``core/perf/lib_builder.py``（同层，无外部依赖）。
"""
from __future__ import annotations

import logging
import os
import shutil
import string
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger("uita")

__all__ = [
    "DRIVE_RESERVE_RATIO",
    "RELEASE_DIRNAME",
    "ReleasePart",
    "ReleasePlan",
    "ReleaseResError",
    "SourceStats",
    "allocate_counts",
    "disk_usage",
    "drive_root",
    "estimate_required_bytes",
    "format_bytes",
    "list_drives",
    "normalize_drive",
    "plan_release",
    "same_drive",
    "scan_source",
    "summarize_plan",
]

#: 拆分释放时每盘可用额度上限（磁盘总容量的百分比）——"按磁盘容量的 80% 拆分释放"。
DRIVE_RESERVE_RATIO = 0.8

#: 释放目录名：``<盘>:\\perf``（与 ``lib_builder.AUTO_LIB_DIRNAME`` 一致，
#: 以便复用同一套销毁护栏：目录名 + 父目录 + 文件命名三重校验）。
RELEASE_DIRNAME = "perf"

#: 估算余量：抽样平均单张体积 × 份数 会因源素材大小分布而略有偏差，留 5% 余量。
SIZE_HEADROOM = 1.05

#: 体积估算的抽样上限（源素材可能有上万张，全量 stat 太慢）
SAMPLE_LIMIT = 64

#: 每个分片目录额外复制的视频数量上限（与 ``lib_builder.build_sample_lib`` 的默认值一致）
VIDEO_PER_PART = 3

#: ``GetDriveTypeW`` 返回值：可移动盘 / 固定盘（其余：网络盘 4、光驱 5、内存盘 6 一律跳过）
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3


class ReleaseResError(RuntimeError):
    """盘符非法 / 空间不足 / 无可用盘——调用方转成 ``StartupError``（启动失败）。"""


@dataclass(frozen=True)
class SourceStats:
    """源素材的抽样统计（体积估算的输入）。"""

    photo_count: int
    avg_photo_bytes: int
    video_bytes: int
    sampled: int = 0

    @property
    def empty(self) -> bool:
        return self.photo_count <= 0 or self.avg_photo_bytes <= 0


@dataclass(frozen=True)
class ReleasePart:
    """一个释放分片：目录 + 该分片最多能放多少份。"""

    root: str            # 释放目录（如 ``E:\\perf``）
    drive: str           # 盘根（如 ``E:\\``）
    usable_bytes: int    # 该分片可用于放样本的字节额度
    max_count: int       # 该分片最多能放多少份
    split: bool = False  # 是否属于"拆分释放"（有多个分片）


@dataclass
class ReleasePlan:
    """一次 perf 运行的释放规划（多单元编排共用同一份）。"""

    parts: list[ReleasePart]
    target_count: int
    required_bytes: int
    split: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def roots(self) -> list[str]:
        return [p.root for p in self.parts]

    def describe(self) -> str:
        return " + ".join(
            f"{p.root}（≤{p.max_count} 份 / {format_bytes(p.usable_bytes)}）" for p in self.parts
        )


def format_bytes(num: Any) -> str:
    """人类可读体积（MB/GB，保留 1 位；<1MB 记 KB）。"""
    try:
        value = float(num)
    except (TypeError, ValueError):
        return "—"
    if value < 0:
        return "—"
    for unit, scale in (("GB", 1024.0 ** 3), ("MB", 1024.0 ** 2), ("KB", 1024.0)):
        if value >= scale:
            return f"{value / scale:.1f}{unit}"
    return f"{value:.0f}B"


def normalize_drive(raw: Any) -> str:
    """盘符归一化：``e`` / ``E`` / ``e:`` / ``E:\\`` → ``E``。

    只接受**单个字母**（"abcd这类数据"）：多余字符直接报错——盘符拼错时静默退化到
    "默认目录"会让用户以为资源真的释放到了目标盘。
    """
    text = str(raw or "").strip()
    if not text:
        raise ReleaseResError("--release-res 需要盘符（如 e）")
    letter, rest = text[0], text[1:]
    if rest.strip(" :\\/\t") != "":
        raise ReleaseResError(
            f"--release-res 只接受单个盘符（如 e / E: / E:\\），收到 {text!r}",
        )
    if not (letter.isascii() and letter.isalpha()):
        raise ReleaseResError(f"--release-res 需要字母盘符（如 e），收到 {text!r}")
    return letter.upper()


def drive_root(letter: str) -> str:
    """盘符 → 盘根（``E`` → ``E:\\``）。"""
    return f"{normalize_drive(letter)}:{os.sep}"


def _canon_root(root: str) -> str:
    """盘根归一化用于比较：反斜杠→斜杠、去尾分隔符与尾冒号、小写。

    ``normcase`` 在 POSIX 上是恒等函数，而"盘符大小写不敏感 + ``E:`` 与 ``E:\\`` 等价"
    是 Windows 语义，故这里显式处理（该模块服务 Windows 盘符语义，与宿主平台无关）。
    """
    text = os.path.normpath(str(root or "")).replace("\\", "/").rstrip("/")
    return (text[:-1] if text.endswith(":") else text).lower()


def same_drive(a: str, b: str) -> bool:
    """比较两个盘根是否指向同一磁盘（大小写/分隔符/尾冒号不敏感）。"""
    return _canon_root(a) == _canon_root(b)


def _get_drive_type(root: str) -> int:
    """Windows 盘符类型（``GetDriveTypeW``）；非 Windows / 调用失败按"固定盘"处理。"""
    if sys.platform != "win32":
        return _DRIVE_FIXED
    try:
        import ctypes

        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 拿不到类型按固定盘处理（宁可多列一个盘，不少列）
        return _DRIVE_FIXED


def list_drives() -> list[str]:
    """本机可用盘根列表（Windows：可移动盘 + 固定盘；其他平台：``/``）。

    光驱/网络盘/内存盘一律排除：往光驱"释放"必然失败，往网络盘释放会让 perf 读数
    混入网络 IO 的影响。
    """
    if sys.platform != "win32":
        return [os.path.abspath(os.sep)]
    roots: list[str] = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:{os.sep}"
        try:
            if not os.path.exists(root):
                continue
        except OSError:
            continue
        if _get_drive_type(root) in (_DRIVE_REMOVABLE, _DRIVE_FIXED):
            roots.append(root)
    return roots


def disk_usage(root: str) -> tuple[int, int]:
    """``(剩余字节, 总字节)``；盘不可用时抛 :class:`ReleaseResError`。"""
    try:
        usage = shutil.disk_usage(root)
    except OSError as exc:
        raise ReleaseResError(f"无法读取磁盘空间 {root}: {exc}") from exc
    return int(usage.free), int(usage.total)


def scan_source(src_dir: str, *, sample_limit: int = SAMPLE_LIMIT) -> SourceStats:
    """抽样统计源素材体积（照片平均单张字节 + 视频总量）。

    抽样而非全量 stat：源目录可能有上万张照片，全量 ``getsize`` 在慢盘上要几十秒。
    等距抽样（``[::step]``）比"取前 N 张"更接近真实分布（用户素材常按时间排序、
    前后体积差异明显）。
    """
    from uita.core.perf.lib_builder import PHOTO_EXTS, VIDEO_EXTS, _list_sources

    if not src_dir or not os.path.isdir(src_dir):
        raise ReleaseResError(f"复制源目录不存在: {src_dir or '(空)'}")
    photos = _list_sources(src_dir, PHOTO_EXTS)
    if not photos:
        raise ReleaseResError(
            f"复制源目录内没有可识别的照片（{', '.join(PHOTO_EXTS)}）: {src_dir}",
        )
    step = max(1, len(photos) // max(1, int(sample_limit)))
    sampled = photos[::step][: max(1, int(sample_limit))]
    sizes: list[int] = []
    for path in sampled:
        try:
            sizes.append(int(os.path.getsize(path)))
        except OSError:
            continue
    if not sizes:
        raise ReleaseResError(f"无法读取源素材体积（全部 stat 失败）: {src_dir}")
    video_bytes = 0
    for path in _list_sources(src_dir, VIDEO_EXTS)[:VIDEO_PER_PART]:
        try:
            video_bytes += int(os.path.getsize(path))
        except OSError:
            continue
    return SourceStats(
        photo_count=len(photos),
        avg_photo_bytes=max(1, sum(sizes) // len(sizes)),
        video_bytes=video_bytes,
        sampled=len(sizes),
    )


def estimate_required_bytes(stats: SourceStats, target_count: int) -> int:
    """按份数估算释放体积（照片份数 × 抽样平均单张 × 余量 + 视频总量）。"""
    count = max(0, int(target_count))
    return int(stats.avg_photo_bytes * count * SIZE_HEADROOM) + int(stats.video_bytes)


def _per_file_bytes(stats: SourceStats) -> int:
    """单份占用的估算字节（含余量，用于换算每个分片能放多少份）。"""
    return max(1, int(round(stats.avg_photo_bytes * SIZE_HEADROOM)))


def _make_part(
    root: str, *, capacity_bytes: int, stats: SourceStats, split: bool,
) -> ReleasePart:
    """构造分片：容量扣除视频占用后换算成"最多几份"。"""
    budget = max(0, int(capacity_bytes) - int(stats.video_bytes))
    return ReleasePart(
        root=root,
        drive=os.path.dirname(os.path.abspath(root)) or root,
        usable_bytes=max(0, int(capacity_bytes)),
        max_count=budget // _per_file_bytes(stats),
        split=split,
    )


def plan_release(
    letter: str,
    target_count: int,
    stats: SourceStats,
    *,
    reserve_ratio: float = DRIVE_RESERVE_RATIO,
    drives: Optional[Iterable[str]] = None,
) -> ReleasePlan:
    """规划释放位置（规则见模块 docstring 的 1~4 条）。

    Args:
        letter: 用户指定盘符（``e`` / ``E:`` 等，经 :func:`normalize_drive` 归一化）。
        target_count: 本次要释放的份数（多单元编排传**最大单元**的份数）。
        stats: :func:`scan_source` 的抽样统计。
        reserve_ratio: 拆分时每盘可用额度占**总容量**的上限（默认 80%）。
        drives: 盘根列表（单测注入用；缺省 = :func:`list_drives`）。

    Raises:
        ReleaseResError: 盘符不存在 / 源素材不可用 / 所有盘额度合计仍不足。
    """
    normalized = normalize_drive(letter)
    count = max(0, int(target_count))
    target_root = drive_root(normalized)
    all_roots = [str(r) for r in (drives if drives is not None else list_drives()) if str(r).strip()]
    if not any(same_drive(r, target_root) for r in all_roots):
        raise ReleaseResError(
            f"盘符 {normalized}: 不存在或不是可用的本地磁盘"
            f"（可用盘: {', '.join(all_roots) or '无'}）",
        )

    required = estimate_required_bytes(stats, count)
    free, total = disk_usage(target_root)
    if free >= required:
        return ReleasePlan(
            parts=[_make_part(
                os.path.join(target_root, RELEASE_DIRNAME),
                capacity_bytes=free, stats=stats, split=False,
            )],
            target_count=count,
            required_bytes=required,
        )

    notes: list[str] = []
    message = (
        f"⚠ 指定盘 {normalized}: 空间不足（需要 {format_bytes(required)}，"
        f"剩余 {format_bytes(free)}）→ 在其他磁盘中寻找可容纳的盘"
    )
    notes.append(message)
    logger.warning("[perf] %s", message)

    candidates: list[tuple[str, int, int]] = []
    for root in all_roots:
        if same_drive(root, target_root):
            continue
        try:
            other_free, other_total = disk_usage(root)
        except ReleaseResError:
            continue
        candidates.append((root, other_free, other_total))

    fits = [item for item in candidates if item[1] >= required]
    if fits:
        root, other_free, _other_total = max(fits, key=lambda item: item[1])
        message = (
            f"⚠ 改用 {root}（剩余 {format_bytes(other_free)} ≥ 需要 {format_bytes(required)}）"
            "——本次样本库不在指定盘上，产物里的样本库路径会反映实际位置"
        )
        notes.append(message)
        logger.warning("[perf] %s", message)
        return ReleasePlan(
            parts=[_make_part(
                os.path.join(root, RELEASE_DIRNAME),
                capacity_bytes=other_free, stats=stats, split=False,
            )],
            target_count=count,
            required_bytes=required,
            notes=notes,
        )

    # 所有单盘都放不下 → 按 min(剩余空间, 80% 总容量) 拆分（指定盘优先，其余按额度降序）
    ordered = [(target_root, free, total)] + sorted(candidates, key=lambda item: item[1], reverse=True)
    parts: list[ReleasePart] = []
    for root, other_free, other_total in ordered:
        capacity = min(int(other_free), int(other_total * float(reserve_ratio)))
        part = _make_part(
            os.path.join(root, RELEASE_DIRNAME), capacity_bytes=capacity, stats=stats, split=True,
        )
        if part.max_count <= 0:
            continue
        parts.append(part)

    capacity_total = sum(p.max_count for p in parts)
    allocated_bytes = sum(p.usable_bytes for p in parts)
    if capacity_total < count:
        detail = "；".join(
            f"{p.drive} 可放 {p.max_count} 份（{format_bytes(p.usable_bytes)}）" for p in parts
        ) or "（没有可用磁盘）"
        raise ReleaseResError(
            f"所有磁盘都放不下 {count} 份（约 {format_bytes(required)}）：{detail}"
            f"（合计 {capacity_total} 份 / {format_bytes(allocated_bytes)}）。"
            f"请降低 --perf-tier、清理磁盘，或改用 --release-res 指定更大的盘",
        )

    message = (
        f"⚠ 无单盘可容纳（需要 {format_bytes(required)}）→ 按各盘可用额度"
        f"（≤{float(reserve_ratio) * 100:.0f}% 容量）拆分释放到 {len(parts)} 个盘："
        + "；".join(f"{p.root} ≤{p.max_count} 份" for p in parts)
    )
    notes.append(message)
    logger.warning("[perf] %s", message)
    return ReleasePlan(
        parts=parts,
        target_count=count,
        required_bytes=required,
        split=True,
        notes=notes,
    )


def allocate_counts(plan: ReleasePlan, target_count: int) -> list[int]:
    """把某个档位的份数分配到各分片（按规划顺序填满再下一个）。

    顺序填充而非按容量等比分配：多单元编排按 0k → 5k → 10k → 50k 递增时，
    只有"尾部追加"才能让 ``build_sample_lib`` 的增量/裁剪语义保持单调（等比分配会让
    每档都在所有分片上重算，产生无谓的删除-重复制）。
    """
    remaining = max(0, int(target_count))
    counts: list[int] = []
    for part in plan.parts:
        take = min(max(0, int(part.max_count)), remaining)
        counts.append(int(take))
        remaining -= int(take)
    if remaining > 0:
        raise ReleaseResError(
            f"释放规划容量不足：还需 {remaining} 份（规划上限 {sum(p.max_count for p in plan.parts)} 份），"
            "请缩小 --perf-tier 或清理磁盘",
        )
    return counts


def summarize_plan(plan: ReleasePlan) -> str:
    """一行规划摘要（控制台/日志用）。"""
    mode = "拆分" if plan.split else "单盘"
    return (
        f"{mode}释放 {plan.target_count} 份（约 {format_bytes(plan.required_bytes)}）→ {plan.describe()}"
    )
