"""内存负载机制：把"占用"变成**真实驻留**，并尽量不被系统释放或压缩。

为什么需要独立模块（2026-10-13）：
    压测（``stress``）口径由"**运行中**的应用何时被杀"改为"系统内存被挤占后应用**何时打不开**"，
    后者的结论完全取决于「占用是否真实驻留」——偏偏这一步最容易假：
      * **惰性分配**：``malloc``/``bytearray`` 只给虚拟地址，不落物理页 → 读数虚高；
      * **零页丢弃/合并**：整页全零会被系统识别为零页（Windows 零页、Linux KSM 同页合并）→ 占用不生效；
      * **换页 / 内存压缩**：Windows 有内存压缩（Memory Compression）、三平台都有 swap →
        分配了 N GB 而实际只驻留 M GB，此时"启动失败水位"是假的。

    故本模块除"分配 + 逐页写非零"外，额外做四件事：
      1. **页首唯一且非零**：每页首字节写 ``(page_index % 251) + 1``——既保证整页非全零（不被零页丢弃），
         又让相邻页内容不同（不被同页合并）；
      2. **禁止被释放/压缩**（用户口径 2026-10-13："stress 占用的内存可能被压缩或被 Windows 释放"）：
         - **硬钉最小工作集**：Windows ``SetProcessWorkingSetSizeEx(min=已持有量, flags=HARDWS_MIN_ENABLE)``
           —— 这是"Windows 不允许把我们的页挤出工作集"的关键一步；Linux/macOS 用 ``mlock``；
         - **锁页**：Windows ``VirtualLock``（防换出 + 防压缩）/ POSIX ``mlock``（受配额限制，拿到多少算多少）；
         - **内存优先级**：Windows ``SetProcessInformation(ProcessMemoryPriority)``（NORMAL 已是上限，
           记录在产物里备查）；
         - **后台 keep-alive 触页**：一级加压之间要启停应用（十几~几十秒），这段静默期页会变冷 →
           内存压缩/standby 裁剪会收走它们；故子进程起一条后台线程周期性 re-touch，让页保持 active+dirty
           （``keepalive_rounds`` 可复核线程是否真在跑）；
      3. **re-touch**：每次判定前重写页首（值 +1 循环），把已被换出/压缩的页**拉回驻留 + 置脏**；
         锁页与工作集设置**随加压增量追加**（幂等、只处理新增区块），失败**只记不抛**；
      4. **驻留自证**：每次 grow/touch 后读子进程**真实驻留物理内存**（``psutil`` 的 rss 在 Windows 上
         即 ``WorkingSetSize``），供上层算 ``resident_ratio = resident / held``；
         比值显著小于 1 时，说明"占用已被系统释放或压缩"，上层须在结论中警示。

依赖与分层（守 R1/R13）：
    只用标准库（``multiprocessing`` / ``ctypes`` / ``threading``）与**已在 requirements.txt 中**的
    ``psutil``；不 import ``engine/``、``apps/``、``cli/``，也不含任何应用知识（进程名/日志目录等住 app 侧场景）。
"""
from __future__ import annotations

import ctypes
import logging
import multiprocessing
import random
import sys
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("uita")

__all__ = [
    "KEEPALIVE_INTERVAL_MS", "MemLoad", "MemLoadChild", "read_resident_mb", "resident_ratio",
]

#: 触页粒度（字节）：4KB = 常见页大小，保证每页真的落到物理内存
PAGE_BYTES = 4096
#: 页首唯一值取模基（质数，避免与页大小整除产生周期性重复）
_PAGE_TAG_MOD = 251
#: 子进程 ack 等待上限（秒）：分配大块 / 系统换页严重时可能很慢，给足余量
ACK_TIMEOUT_S = 120.0
#: **硬** ack 上限（秒）：子进程仍存活时允许把等待延长到这里（2026-10-13 加固 W5）
HARD_ACK_TIMEOUT_S = 900.0
#: 每次延长的粒度（秒）
_ACK_EXTEND_STEP_S = 30.0
#: 驻留比低于该值即视为"占用被系统释放/压缩过"
RESIDENT_RATIO_FLOOR = 0.9
#: 后台 keep-alive 触页间隔（毫秒）：0 = 关闭（只用于对照实验）
KEEPALIVE_INTERVAL_MS = 1000

#: 高熵填充池页数：256 页 × 4KB = **1MB** 随机块，按块切片平铺（C 级 memcpy，无大临时对象）
_POOL_PAGES = 256
#: 页首唯一值基表（`(i % 251) + 1`：既非零又不重复；C 级平铺用）
_HEAD_BASE = bytes((i % _PAGE_TAG_MOD) + 1 for i in range(_PAGE_TAG_MOD))

_POOL_BLOCK: Optional[bytes] = None


def _pool_block() -> bytes:
    """高熵填充块（**1MB 全随机**，惰性构建一次）。

    为什么要它（2026-10-13 加固 W1，最关键的"不够强势"）：原实现每页只写**页首 1 字节**，
    整页是"1 个非零 + 4095 个零"——这正是 **Windows 内存压缩（XPRESS/LZNT）压得最狠**的内容，
    4KB 能压到几十字节；于是"我们占了 8GB"而系统并不真缺内存，压不出压力（真机
    ``resident_ratio`` 掉到 0.55 就是这么来的）。改成**页内容高熵不可压缩**后，压缩器无从下手，
    系统只剩"换页 / 拒绝分配"两条路——这才叫强势。
    """
    global _POOL_BLOCK
    if _POOL_BLOCK is None:
        rng = random.Random(0x5EED_2026)
        _POOL_BLOCK = rng.randbytes(_POOL_PAGES * PAGE_BYTES)
    return _POOL_BLOCK


#: Windows ``PROCESS_INFORMATION_CLASS``（processthreadsapi.h）——优先级是 **0**，别写成 1
#: （``1`` = ``ProcessMemoryExclusion``，期望 ``PROCESS_MEMORY_EXCLUSION_INFO``，传 4 字节结构体会
#: 直接 ``ERROR_INVALID_PARAMETER(87)``；2026-10-13 真机就是这个错）
_WIN_PROCESS_MEMORY_PRIORITY = 0
#: ``MEMORY_PRIORITY``：1=VERY_LOW / 2=LOW / 3=MEDIUM / 4=BELOW_NORMAL / 5=NORMAL（5 已是上限）
_WIN_MEMORY_PRIORITY_NORMAL = 5
#: ``SetProcessWorkingSetSizeEx`` 的 flags（winnt.h）：硬性最小工作集 = 0x1，硬性最大 = 0x4
_WIN_HARDWS_MIN_ENABLE = 0x1
_WIN_HARDWS_MAX_ENABLE = 0x4
_WIN_HARDWS_MAX_DISABLE = 0x8
#: 工作集**上限**相对"已持有量"的余量（MB）：只硬钉下限，上限留活路（否则解释器自身开销一超就被掐）
_WS_MAX_SLACK_MB = 256
#: 工作集**下限**相对"已持有量"的抬升量（MB，2026-10-13 加固 W2）：
#: ``VirtualLock`` 的可锁额度 ≈ 进程最小工作集，而解释器自身开销也吃这个额度 →
#: 按 ``held`` 设下限时**末几块恰好锁不住**（真机 ``ERROR_WORKING_SET_QUOTA(1453)``）。
#: 抬高一个余量后配额才覆盖全部已持有页；系统不接受时自动退回 ``held``（降级留痕 `ws_min_mode`）。
_WS_MIN_OVERHEAD_MB = 64
#: 常见 Win32 错误码 → 名字（写进产物，排障不用再查表）
_WIN_ERRORS: dict[int, str] = {
    5: "ERROR_ACCESS_DENIED",
    6: "ERROR_INVALID_HANDLE",
    8: "ERROR_NOT_ENOUGH_MEMORY",
    87: "ERROR_INVALID_PARAMETER",
    998: "ERROR_NOACCESS",
    1453: "ERROR_WORKING_SET_QUOTA",
    1455: "ERROR_WORKING_SET_LIMIT_RANGE",
}


class _WinMemPriority(ctypes.Structure):
    """``MEMORY_PRIORITY_INFORMATION { ULONG MemoryPriority; }``。

    显式用 ``ctypes.c_uint32`` 而不是 ``wintypes.DWORD``：后者是 ``c_ulong``，在非 Windows 上
    宽度不同（macOS 上 8 字节），会让结构体大小算错——而单测就跑在 macOS 上。
    """

    _fields_ = [("MemoryPriority", ctypes.c_uint32)]


def _win_last_error() -> int:
    """``GetLastError()``（配 ``WinDLL(use_last_error=True)``）；取不到记 0。"""
    getter = getattr(ctypes, "get_last_error", None)
    if not callable(getter):  # pragma: no cover - 仅非 Windows
        return 0
    try:
        return int(getter())
    except Exception:  # noqa: BLE001
        return 0


def _win_err_text(code: Any) -> str:
    """``err=1453(ERROR_WORKING_SET_QUOTA)`` 形式的可读错误（未知码只给数字）。"""
    try:
        value = int(code)
    except Exception:  # noqa: BLE001
        return f"err={code}"
    name = _WIN_ERRORS.get(value)
    return f"err={value}({name})" if name else f"err={value}"


def _declare_win_prototypes(dll: Any) -> None:
    """声明 kernel32 相关函数的 ``argtypes``/``restype``（幂等；替身对象同样适用）。

    **必须声明**（2026-10-13 真机 err=87 的成因之一）：x64 上 ``GetCurrentProcess()`` 返回伪句柄
    ``(HANDLE)-1``，ctypes 未声明 ``restype`` 时按 ``c_int`` 取值 → **截断成 32 位**
    （``0xFFFFFFFF``）→ 传给 ``SetProcessWorkingSetSizeEx`` / ``SetProcessInformation``
    就是无效句柄，必然失败。
    """
    specs: dict[str, tuple[list[Any], Any]] = {
        "GetCurrentProcess": ([], ctypes.c_void_p),
        "SetProcessWorkingSetSizeEx": (
            [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint32], ctypes.c_int,
        ),
        "SetProcessInformation": (
            [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int,
        ),
        "VirtualLock": ([ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int),
        "VirtualUnlock": ([ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int),
    }
    for name, (argtypes, restype) in specs.items():
        fn = getattr(dll, name, None)
        if fn is None:
            continue
        try:
            fn.argtypes = list(argtypes)
            fn.restype = restype
        except Exception:  # noqa: BLE001 - 替身/极老环境设不了也不该中断锁页
            continue


def resident_ratio(resident_mb: Any, held_mb: Any) -> Optional[float]:
    """``resident_mb / held_mb``（读数缺失或 ``held_mb<=0`` 时记 ``None``，不猜）。

    ``bool`` 不算数值（``True`` 是 ``int`` 的子类，会算出 0.01 这种假读数）。
    """
    if isinstance(resident_mb, bool) or isinstance(held_mb, bool):
        return None
    if not isinstance(resident_mb, (int, float)) or not isinstance(held_mb, (int, float)):
        return None
    if float(held_mb) <= 0:
        return None
    return round(float(resident_mb) / float(held_mb), 3)


def read_resident_mb(pid: Optional[int] = None) -> Optional[float]:
    """读进程**真实驻留物理内存**（MB）；``pid=None`` 表示读自己。

    ``psutil.Process.memory_info().rss`` 在 Windows 上就是 ``WorkingSetSize``
    （即"当前真占着物理内存的量"，不含已被换出/压缩的部分），语义正是我们要的。
    """
    try:
        import psutil  # type: ignore
    except ImportError:  # pragma: no cover - 依赖缺失时如实记 None
        return None
    try:
        proc = psutil.Process(pid) if pid is not None else psutil.Process()
        return round(float(proc.memory_info().rss) / 1048576.0, 1)
    except Exception as exc:  # noqa: BLE001 - 读数失败不该中断压测
        logger.debug("[perf] memload: 驻留量读取失败(pid=%s): %s", pid, exc)
        return None


# ── 锁页（best-effort；失败只记不抛）─────────────────────────────


def _lock_posix(chunks: list[bytearray], *, locked_chunks: int = 0) -> dict[str, Any]:
    """POSIX ``mlock``：把**尚未锁定**的区块锁进物理内存（受 ``RLIMIT_MEMLOCK`` 限制，多半失败）。"""
    info: dict[str, Any] = {"supported": True, "ok": False, "locked_mb": 0.0, "error": "",
                            "locked_chunks": 0}
    try:
        import ctypes
        import resource
    except ImportError as exc:  # pragma: no cover
        info.update(supported=False, error=f"import 失败: {exc}")
        return info
    # 先尝试把软上限抬到硬上限（非 root 通常不允许无限，失败不影响继续尝试）
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
        if soft != hard:
            resource.setrlimit(resource.RLIMIT_MEMLOCK, (hard, hard))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[perf] memload: 提升 RLIMIT_MEMLOCK 失败（忽略）: %s", exc)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        mlock = libc.mlock
        mlock.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        mlock.restype = ctypes.c_int
    except Exception as exc:  # noqa: BLE001
        info.update(supported=False, error=f"取 mlock 符号失败: {exc}")
        return info
    done = max(0, min(int(locked_chunks), len(chunks)))
    locked = sum(len(c) for c in chunks[:done])
    for chunk in chunks[done:]:
        try:
            buf = (ctypes.c_char * len(chunk)).from_buffer(chunk)   # 不拷贝
            rc = mlock(ctypes.addressof(buf), ctypes.c_size_t(len(chunk)))
        except Exception as exc:  # noqa: BLE001
            info["error"] = f"mlock 调用异常: {type(exc).__name__}: {exc}"
            break
        if rc != 0:
            errno = ctypes.get_errno()
            info["error"] = f"mlock 失败 errno={errno}"
            break
        locked += len(chunk)
        done += 1
    info["locked_mb"] = round(locked / 1048576.0, 1)
    info["locked_chunks"] = done
    info["ok"] = locked > 0
    if not info["ok"] and not info["error"]:
        info["error"] = "无区块可锁"
    return info


def _lock_windows(
    chunks: list[bytearray], *, kernel32: Any = None, last_error: Any = None,
    locked_chunks: int = 0,
) -> dict[str, Any]:
    """Windows：**硬钉最小工作集** + 内存优先级 + ``VirtualLock``（best-effort，逐级降级）。

    真机事故（2026-10-13：``lock_summary.ok=false, err=87``）与三处**已修**的成因：
      1. ``SetProcessInformation`` 的 InformationClass 传了 ``1``（= ``ProcessMemoryExclusion``，
         期望 ``PROCESS_MEMORY_EXCLUSION_INFO``），而 ``ProcessMemoryPriority`` 才是 ``0``
         → 结构体大小不匹配 → ``ERROR_INVALID_PARAMETER(87)``；
      2. 未声明 ``argtypes/restype`` → x64 上伪句柄被截断成 32 位（见 :func:`_declare_win_prototypes`）；
      3. ``SetProcessWorkingSetSizeEx`` 用了 ``MIN_ENABLE|MIN_DISABLE``（自相矛盾）且 ``min == max``
         （连上限也钉死 → 解释器自身开销一超就被掐）。
    现口径：``min = 已持有量``（硬下限 → **Windows 不允许把我们的页挤出工作集**）、
    ``max = min + _WS_MAX_SLACK_MB``、flags = ``HARDWS_MIN_ENABLE | HARDWS_MAX_DISABLE``；
    ``VirtualLock`` 只锁 ``locked_chunks`` **之后**的新块（重复调用会空耗配额）。

    Args:
        chunks: 已持有区块（每块一个 ``bytearray``）。
        kernel32: 注入用的 kernel32 替身（``None`` = 真加载 ``WinDLL``；单测走替身）。
        last_error: 注入用的 ``GetLastError`` 取数函数（``None`` = 走 ``ctypes.get_last_error``）。
        locked_chunks: 已确认锁定的区块数（增量锁，幂等）。
    """
    info: dict[str, Any] = {
        "supported": True, "ok": False, "locked_mb": 0.0, "error": "",
        "locked_chunks": 0, "lock_calls": 0, "lock_failures": 0, "hard_min_enabled": False,
        "ws_min_mb": None, "ws_max_mb": None, "ws_min_mode": "", "ws_ok": False, "ws_error": "",
        "priority_ok": None, "priority_error": "",
        "policy": "hard_ws_min + VirtualLock + MemoryPriority",
    }
    if kernel32 is None:
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - 非 Windows / 极小概率加载失败
            info.update(supported=False, error=f"加载 kernel32 失败: {exc}")
            return info
    held_bytes = sum(len(c) for c in chunks)
    if held_bytes <= 0:
        info["error"] = "无区块可锁"
        return info
    get_error = last_error if callable(last_error) else _win_last_error
    _declare_win_prototypes(kernel32)
    get_process = getattr(kernel32, "GetCurrentProcess", None)
    if not callable(get_process):
        info.update(supported=False, error="kernel32 缺少 GetCurrentProcess")
        return info
    handle = get_process()

    # ① 内存优先级（NORMAL=5 已是上限；作用有限但一并记录，便于复核"我们能做的都做了"）
    set_info = getattr(kernel32, "SetProcessInformation", None)
    if callable(set_info):
        try:
            prio = _WinMemPriority(_WIN_MEMORY_PRIORITY_NORMAL)
            ok = set_info(
                handle, _WIN_PROCESS_MEMORY_PRIORITY, ctypes.byref(prio), ctypes.sizeof(prio),
            )
            info["priority_ok"] = bool(ok)
            if not ok:
                info["priority_error"] = _win_err_text(get_error())
        except Exception as exc:  # noqa: BLE001
            info["priority_ok"] = False
            info["priority_error"] = f"{type(exc).__name__}: {exc}"

    # ② 硬钉最小工作集：这一步才是"**禁止 Windows 释放/挤走**"的关键
    #    加固 W2：先按 `held + 余量` 设下限（配额需覆盖解释器自身开销，否则末几块锁不住）；
    #    系统不接受（如 ERROR_WORKING_SET_LIMIT_RANGE）则退回 `held` 再试一次，降级留痕。
    set_ws = getattr(kernel32, "SetProcessWorkingSetSizeEx", None)
    if callable(set_ws):
        flags = _WIN_HARDWS_MIN_ENABLE | _WIN_HARDWS_MAX_DISABLE
        overhead = _WS_MIN_OVERHEAD_MB * 1048576
        attempts = (
            ("held_plus_overhead", held_bytes + overhead),
            ("held_fallback", held_bytes),
        )
        for mode, min_bytes in attempts:
            max_bytes = min_bytes + _WS_MAX_SLACK_MB * 1048576
            info["ws_min_mb"] = round(min_bytes / 1048576.0, 1)
            info["ws_max_mb"] = round(max_bytes / 1048576.0, 1)
            info["ws_min_mode"] = mode
            try:
                ok = set_ws(
                    handle, ctypes.c_size_t(min_bytes), ctypes.c_size_t(max_bytes),
                    ctypes.c_uint32(flags),
                )
            except Exception as exc:  # noqa: BLE001
                info["ws_error"] = f"{type(exc).__name__}: {exc}"
                continue
            info["ws_ok"] = bool(ok)
            info["hard_min_enabled"] = bool(ok)
            if ok:
                info["ws_error"] = ""
                break
            info["ws_error"] = _win_err_text(get_error())
            logger.debug("[perf] memload: 工作集下限 %s 失败（%s），退回更低下限重试",
                         mode, info["ws_error"])

    # ③ VirtualLock 逐块（只锁未锁过的；单进程可锁量 ≈ 最小工作集，故 ② 先成功才有意义）
    #    配额不足时**只跳过该块、继续试后面的**（不 break）：块大小一致，但已被系统回收/碎片化
    #    的块重试可能成功；全部失败也会如实记锁不到的量（加固 W2）。
    done = max(0, min(int(locked_chunks), len(chunks)))
    locked = sum(len(c) for c in chunks[:done])
    vlock = getattr(kernel32, "VirtualLock", None)
    if callable(vlock):
        for index in range(done, len(chunks)):
            chunk = chunks[index]
            info["lock_calls"] = int(info["lock_calls"]) + 1
            try:
                buf = (ctypes.c_char * len(chunk)).from_buffer(chunk)
                rc = vlock(ctypes.addressof(buf), ctypes.c_size_t(len(chunk)))
            except Exception as exc:  # noqa: BLE001
                info["error"] = f"VirtualLock 异常: {type(exc).__name__}: {exc}"
                break
            if not rc:
                info["error"] = f"VirtualLock 失败 {_win_err_text(get_error())}"
                info["lock_failures"] = int(info.get("lock_failures") or 0) + 1
                continue
            locked += len(chunk)
            done = index + 1
    info["locked_mb"] = round(locked / 1048576.0, 1)
    info["locked_chunks"] = done
    # "生效" = 工作集硬下限设成功（禁止被挤走）**或**真锁住了页（禁止换出/压缩）
    info["ok"] = bool(info["hard_min_enabled"] or (isinstance(info["locked_mb"], float) and info["locked_mb"] > 0))
    if not info["ok"] and not info["error"]:
        info["error"] = info["ws_error"] or info["priority_error"] or "未生效（工作集与锁页均失败）"
    elif info["ok"] and info.get("lock_failures"):
        info["note"] = (f"{info['lock_failures']} 块 VirtualLock 未成功（配额限制），"
                        "已靠工作集硬下限兜住")
    return info


def _try_lock(chunks: list[bytearray], *, locked_chunks: int = 0) -> dict[str, Any]:
    """按平台尝试防压缩/防释放；不支持的平台 / 任何异常都返回可审计的 dict（不抛）。"""
    try:
        if sys.platform == "win32":
            return _lock_windows(chunks, locked_chunks=locked_chunks)
        if sys.platform in ("darwin", "linux"):
            return _lock_posix(chunks, locked_chunks=locked_chunks)
        return {"supported": False, "ok": False, "locked_mb": 0.0, "locked_chunks": 0,
                "error": f"未适配平台 {sys.platform}"}
    except Exception as exc:  # noqa: BLE001 - 防压缩措施永远不能中断压测
        return {"supported": False, "ok": False, "locked_mb": 0.0, "locked_chunks": 0,
                "error": f"{type(exc).__name__}: {exc}"}


# ── 子进程 ──────────────────────────────────────────────────────


class MemLoadChild:
    """内存负载子进程（模块级类：``spawn`` 上下文要求可 pickle）。

    协议（父 → 子）：``("grow", mb, lock)`` / ``("touch",)`` / ``("stop",)``；
    子 → 父：``("ok"|"fail"|"error", info_dict)`` / ``("bye", info_dict)``。

    为什么"逐块分配"而不是一次申请一大坨：口径要求"逐步增加直到应用打不开"，
    只有逐块才能测出**临界点**；也才能在某一级分配失败时保留此前所有已持有区块。
    """

    def __init__(self, lock_pages: bool = True, keepalive_ms: int = KEEPALIVE_INTERVAL_MS) -> None:
        self._lock_pages = bool(lock_pages)
        self._chunks: list[bytearray] = []
        self._held_mb = 0
        self._lock_info: dict[str, Any] = {
            "supported": None, "ok": False, "locked_mb": 0.0, "error": "",
            "locked_chunks": 0, "hard_min_enabled": False, "ws_min_mb": None,
        }
        #: 已确认锁定的区块数（增量锁：复用旧结果，避免重复 VirtualLock 空耗配额）
        self._locked_chunks = 0
        self._tag = 1
        #: 区块序号（用于高熵填充的**相位轮转 + 页唯一化**：相邻块内容必须不同）
        self._chunk_seq = 0
        #: ``refill`` 次数（"被系统回收后的反击"留痕，供上层自证防回收是否真在跑）
        self._refill_rounds = 0
        #: 后台 keep-alive 触页（防页变冷 → 被内存压缩 / standby 裁剪收走）
        self._keepalive_ms = max(0, int(keepalive_ms))
        self._keepalive_stop = threading.Event()
        self._keepalive_thread: Optional[threading.Thread] = None
        self._keepalive_rounds = 0
        self._guard = threading.Lock()
        # 记账基线：本进程"什么都没吃"时的 RSS（解释器自身占用）。驻留自证要比的是**增量**，
        # 否则小步进测试下基线会淹没增量（真机 GB 级加压时影响可忽略，但口径要一致）。
        self._base_resident_mb = read_resident_mb()

    # -- 内部工具 --------------------------------------------------

    def _fill(self, chunk: bytearray, chunk_index: int = 0) -> None:
        """**高熵填充**整块（加固 W1）：页内容全随机 → 压缩器无从下手。

        全 C 级切片（无逐页 Python 循环）：

          ① **平铺随机块**：1MB 全随机块按 **32MB 窗口**反复切片赋值 —— 复杂度 O(size/32MB)
             次 C 调用；窗口上限保证临时对象最多 32MB（机器已吃紧时不能再叠一个 GB 级临时块）。
          ② **按块轮转相位**：相位 = 块序号 × 4KB，**相邻块内容不同** → 防"内容完全相同的页被
             系统合并成同一物理页"（内存合并 / 隐式共享）。
          ③ **页唯一化**：每页头 4 字节写「块序号<<20 | 页序号」→ 任意两页都不逐字节相同，
             页内其余 4092 字节全是熵（既非零页、又不可压缩）。

        为什么这三条都必须有（"不够强势"的根因，2026-10-13 加固 W1）：旧实现每页只写页首 1 字节，
        整页是「1 个非零 + 4095 个零」，正是 Windows 内存压缩（XPRESS/LZNT）压得最狠的内容
        （4KB→几十字节）；于是"占了 8GB"而系统并不真缺内存，压不出压力（真机 resident_ratio 0.55）。
        """
        body = _pool_block()
        size = len(chunk)
        span = len(body)
        # ② 相位轮转（4KB 粒度，页对齐）
        shift = (int(chunk_index) * PAGE_BYTES) % span
        rotated = body[shift:] + body[:shift] if shift else body
        # ① 32MB 窗口平铺：window 由 rotated 重复而成，之后整段整段地 memcpy
        window_mb = 32
        reps = max(1, (window_mb * 1048576) // span)
        window = rotated * reps
        wlen = len(window)
        pos = 0
        while pos < size:
            end = min(pos + wlen, size)
            chunk[pos:end] = window[: end - pos]
            pos = end
        # ③ 页头唯一化：4 字节/页 → 用 4 条扩展切片（C 级 scatter），不做 Python 逐页循环
        pages = size // PAGE_BYTES
        if pages > 0:
            mask = 0xFF
            for byte_index in range(4):
                shift_bits = byte_index * 8
                head = bytes(
                    ((((int(chunk_index) << 20) + page) >> shift_bits) & mask)
                    for page in range(pages)
                )
                start = byte_index
                chunk[start:start + pages * PAGE_BYTES:PAGE_BYTES] = head

    def _touch_heads(self, chunk: bytearray) -> None:
        """**廉价置脏**（加固 W3）：只改每页首字节，但用扩展切片（C 级 scatter）。

        原实现是 `for offset in range(0, len(chunk), PAGE_BYTES)` —— 8GB = **200 万次 Python
        迭代/轮**，每秒一轮就把一个核吃满、还挤占内存带宽，反而削弱压迫强度。
        扩展切片赋值 `chunk[0::PAGE_BYTES] = pattern` 由 C 层完成同样的"每页写 1 字节"，
        微秒级；配合 W1 的高熵页体，压缩器依然无从下手（页体本来就是随机的）。

        值随轮次轮转（`_tag`）→ 页保持 **dirty**（脏页不会被 standby 裁剪直接丢，必须回写）。
        """
        size = len(chunk)
        pages = size // PAGE_BYTES
        if pages <= 0:
            return
        base = _HEAD_BASE
        shift = (self._tag * 37) % len(base)
        rotated = base[shift:] + base[:shift]
        pattern = (rotated * (pages // len(base) + 1))[:pages]
        chunk[0:pages * PAGE_BYTES:PAGE_BYTES] = pattern

    def _touch_all_heads(self) -> None:
        """轮转 tag 并给全部区块**廉价置脏**（供 keep-alive 高频调用）。"""
        with self._guard:
            chunks = list(self._chunks)
            self._advance_tag()
            for chunk in chunks:
                self._touch_heads(chunk)

    def _advance_tag(self) -> None:
        self._tag = self._tag + 1 if self._tag < _PAGE_TAG_MOD else 1

    # -- keep-alive（防被压缩/裁剪）---------------------------------

    def start_keepalive(self) -> None:
        """启动后台触页线程（幂等；``keepalive_ms<=0`` 时不动）。

        为什么必须有（用户 2026-10-13："stress 占用的内存可能被压缩或被 Windows 释放"）：
        一级加压之间要**启停应用**（十几~几十秒），这段静默期里页会变冷 → Windows 的内存压缩
        /standby 裁剪会把它们收走（真机 ``resident_ratio`` 掉到 0.55 就是这么来的）。
        持续触页让页保持 active + dirty，压缩器与裁剪器不会挑中它们；
        ``keepalive_rounds`` 用来复核这条线程是否真在跑（为 0 说明没生效）。
        """
        if self._keepalive_ms <= 0 or self._keepalive_thread is not None:
            return
        self._keepalive_stop.clear()
        thread = threading.Thread(target=self._keepalive_loop, name="memload-keepalive", daemon=True)
        self._keepalive_thread = thread
        thread.start()
        logger.info("[perf] memload: keep-alive 触页线程已启动（每 %dms 重写一次页首）", self._keepalive_ms)

    def _keepalive_loop(self) -> None:
        interval = max(0.05, self._keepalive_ms / 1000.0)
        while not self._keepalive_stop.wait(interval):
            try:
                self._touch_all_heads()      # 加固 W3：C 级 scatter，不再逐页 Python 循环
                self._keepalive_rounds += 1
            except Exception as exc:  # noqa: BLE001 - 触页失败绝不能让这条线程死掉
                logger.debug("[perf] memload: keep-alive 触页异常: %s", exc)
        # 静默期结束后给全部页重新套一次锁页/工作集下限（页可能已变冷被解锁）
        try:
            if self._lock_pages:
                self.lock()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[perf] memload: keep-alive 收尾补锁异常: %s", exc)

    def stop_keepalive(self, timeout_s: float = 2.0) -> None:
        """停止 keep-alive 线程（幂等；``release``/子进程退出路径都会调）。"""
        self._keepalive_stop.set()
        thread = self._keepalive_thread
        self._keepalive_thread = None
        if thread is not None:
            try:
                thread.join(timeout=max(0.0, float(timeout_s)))
            except Exception:  # noqa: BLE001
                pass

    def info(self) -> dict[str, Any]:
        """当前状态快照（含**驻留自证**与**防压缩措施**字段）。

        ``resident_mb`` 是"当前 RSS 相对启动基线的增量"——即**这次加压真正占住的物理内存**；
        ``resident_ratio = resident_mb / held_mb``，显著小于 1 即说明占用被系统释放/压缩过。
        """
        resident = read_resident_mb()
        base = self._base_resident_mb
        delta = None
        if isinstance(resident, (int, float)) and isinstance(base, (int, float)):
            delta = round(float(resident) - float(base), 1)
        return {
            "held_mb": self._held_mb,
            "resident_mb": delta,
            "resident_rss_mb": resident,
            "resident_base_mb": base,
            "resident_ratio": resident_ratio(delta, self._held_mb),
            "chunks": len(self._chunks),
            "lock_supported": self._lock_info.get("supported"),
            "lock_ok": self._lock_info.get("ok"),
            "locked_mb": self._lock_info.get("locked_mb"),
            "lock_error": self._lock_info.get("error") or "",
            "locked_chunks": self._lock_info.get("locked_chunks", self._locked_chunks),
            "lock_failures": self._lock_info.get("lock_failures", 0),
            "ws_min_mb": self._lock_info.get("ws_min_mb"),
            "ws_max_mb": self._lock_info.get("ws_max_mb"),
            "ws_min_mode": self._lock_info.get("ws_min_mode", ""),
            "hard_min_enabled": self._lock_info.get("hard_min_enabled"),
            "page_fill": "incompressible_random",
            "pool_pages": _POOL_PAGES,
            "keepalive_ms": self._keepalive_ms,
            "keepalive_rounds": self._keepalive_rounds,
            "refill_rounds": self._refill_rounds,
        }

    # -- 指令 ------------------------------------------------------

    def grow(self, mb: int, lock: Optional[bool] = None) -> tuple[str, dict[str, Any]]:
        """再吃 ``mb`` MB；返回 ``("ok"|"fail"|"error", info)``。"""
        size = max(1, int(mb))
        try:
            chunk = bytearray(size * 1048576)      # 惰性虚拟地址，下面 _fill 才真落物理页
            with self._guard:
                self._chunk_seq += 1
                seq = self._chunk_seq
                self._fill(chunk, seq)             # 加固 W1：高熵填充（可压缩性 ≈ 0）
                self._chunks.append(chunk)
                self._held_mb += size
        except MemoryError:
            # 分配失败本身就是有效结论（"机器真的给不出了"），如实回报不抛
            return "fail", self.info()
        except Exception as exc:  # noqa: BLE001
            return "error", {"error": f"{type(exc).__name__}: {exc}", **self.info()}
        want_lock = self._lock_pages if lock is None else bool(lock)
        if want_lock:
            self.lock()
        return "ok", self.info()

    def touch(self) -> tuple[str, dict[str, Any]]:
        """re-touch：把被换出/压缩的页**拉回驻留并置脏**（每级判定前调用）。

        加固 W3：用扩展切片（C 级 scatter）只写页首，不再做百万次 Python 循环；
        重新触过的页可能已被系统解锁/换出 → 触完**再补一次增量锁 + 工作集下限**（幂等）。
        """
        try:
            self._touch_all_heads()
            if self._lock_pages:
                self.lock()
        except Exception as exc:  # noqa: BLE001
            return "error", {"error": f"{type(exc).__name__}: {exc}", **self.info()}
        return "ok", self.info()

    def refill(self) -> tuple[str, dict[str, Any]]:
        """**全内容重写 + 重新锁页**：对"占用已被系统压缩/回收"的主动反击（加固 W1/W2）。

        为什么需要（用户 2026-10-13："防止占用的内存被 windows 系统回收"）：
        `touch` 只置脏页首（快），但若某段范围**已被内存压缩/裁剪收走**，只改 1 字节不足以
        让它回到"不可压缩"的状态；`refill` 把**每页全 4096 字节重写成新的高熵内容**
        （相位随轮次变化 → 内容全新）并重新套用锁页与工作集硬下限，把物理内存夺回来。
        上层在 `resident_ratio < RESIDENT_RATIO_FLOOR`（压缩/回收已发生）时调用。
        """
        try:
            with self._guard:
                chunks = list(self._chunks)
                self._advance_tag()
                self._refill_rounds += 1
                for chunk in chunks:
                    self._fill(chunk, self._chunk_seq + self._refill_rounds)
            if self._lock_pages:
                self.lock()
        except MemoryError:
            return "fail", self.info()
        except Exception as exc:  # noqa: BLE001
            return "error", {"error": f"{type(exc).__name__}: {exc}", **self.info()}
        return "ok", self.info()

    def lock(self) -> dict[str, Any]:
        """对**尚未锁定**的区块做一次 best-effort 防压缩/防释放（幂等；失败只记不抛）。

        随加压增量追加：``held_mb`` 变大后工作集硬下限要跟着抬高（否则新页仍会被挤走）。
        """
        self._lock_info = _try_lock(self._chunks, locked_chunks=self._locked_chunks)
        self._locked_chunks = int(self._lock_info.get("locked_chunks") or 0)
        return self._lock_info

    def release(self) -> dict[str, Any]:
        """释放全部内存（先停 keep-alive，再 ``chunks.clear()``），返回释放明细。"""
        self.stop_keepalive()
        held = self._held_mb
        with self._guard:
            count = len(self._chunks)
            self._chunks.clear()
            self._held_mb = 0
        self._locked_chunks = 0
        return {"held_mb_before": held, "chunks_before": count, "held_mb_after": 0}

    # -- 子进程入口 ------------------------------------------------

    def serve(self, conn: Any) -> None:
        """子进程主循环：收指令 → 执行 → 回 ack；``stop`` 或管道断开即退出。

        进入即启动 keep-alive 触页线程（防"页变冷 → 被内存压缩/裁剪"），退出路径（含异常）停掉它。
        """
        self.start_keepalive()
        try:
            while True:
                try:
                    msg = conn.recv()
                except (EOFError, OSError):
                    break
                if not isinstance(msg, (list, tuple)) or not msg:
                    break
                cmd = str(msg[0])
                if cmd == "grow":
                    kind, info = self.grow(int(msg[1]), msg[2] if len(msg) > 2 else None)
                elif cmd == "touch":
                    kind, info = self.touch()
                elif cmd == "refill":
                    kind, info = self.refill()
                elif cmd == "stop":
                    try:
                        conn.send(("bye", {"released": self.release(), **self.info()}))
                    except (OSError, ValueError):
                        pass
                    break
                else:
                    kind, info = "error", {"error": f"未知指令 {cmd}"}
                try:
                    conn.send((kind, info))
                except (OSError, ValueError):
                    break
        finally:
            # 异常路径也必须把内存还给系统（否则父进程终止前系统一直吃紧）
            try:
                self.stop_keepalive()
            except Exception:  # noqa: BLE001
                pass
            try:
                self.release()
            except Exception:  # noqa: BLE001
                pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _mem_worker(  # pragma: no cover - 子进程入口
    conn: Any, lock_pages: bool = True, keepalive_ms: int = KEEPALIVE_INTERVAL_MS,
) -> None:
    """``multiprocessing`` 子进程入口（薄包装，便于单测直接构造 ``MemLoadChild``）。"""
    MemLoadChild(lock_pages=lock_pages, keepalive_ms=keepalive_ms).serve(conn)


# ── 父侧句柄 ────────────────────────────────────────────────────


class MemLoad:
    """内存负载的父侧句柄：起子进程、逐级加压、读驻留、释放（任何路径都不残留）。

    与旧实现（``apps/sample_media/perf/scenarios/stress.py`` 内的私有 ``_Load``）的差别：
      * 内存部分抽到 core 机制层（跨 app 复用 + 可独立单测）；
      * 每级都带回**驻留自证**与**锁页结果**，不再只有一个 ``held_mb``。
    """

    def __init__(
        self, ctx: Any, *, lock_pages: bool = True, keepalive_ms: int = KEEPALIVE_INTERVAL_MS,
    ) -> None:
        self._ctx = ctx
        self._lock_pages = bool(lock_pages)
        self._keepalive_ms = max(0, int(keepalive_ms))
        self.proc: Any = None
        self.conn: Any = None
        self.held_mb = 0
        self.last_info: dict[str, Any] = {}

    def start(self) -> None:
        parent_conn, child_conn = self._ctx.Pipe()
        self.conn = parent_conn
        self.proc = self._ctx.Process(
            target=_mem_worker, args=(child_conn, self._lock_pages, self._keepalive_ms), daemon=True,
        )
        self.proc.start()
        child_conn.close()      # 父进程不持有子端（否则子进程永远等不到 EOF）

    def _live_child(self) -> bool:
        """子进程是否**仍在跑**（用于区分"真故障"与"只是慢"）。"""
        proc = self.proc
        if proc is None:
            return False
        try:
            return bool(proc.is_alive())
        except Exception:  # noqa: BLE001
            return False

    def _call(self, msg: tuple, timeout_s: Optional[float] = None) -> tuple[str, Any]:
        """发一条指令并等 ack（``timeout_s=None`` → 用模块级 ``ACK_TIMEOUT_S``，便于单测覆盖）。

        加固 W5（2026-10-13）：**子进程仍存活时把等待延长**（每次 `_ACK_EXTEND_STEP_S`，硬上限
        `HARD_ACK_TIMEOUT_S`）。原因：内存吃紧时子进程"正在真干活"却来不及回 ack（换页风暴），
        旧实现直接判 `timeout` 让上层把**有意义的压力现场**当故障中止——这与"占满直到给不出"的
        口径正相反。只有**子进程已死**（或硬上限到）才返回 `timeout`，并把实等时长记进 `waited_s`。
        """
        wait_s = ACK_TIMEOUT_S if timeout_s is None else float(timeout_s)
        if self.conn is None:
            return "error", {"error": "内存子进程不可用"}
        try:
            self.conn.send(msg)
        except (OSError, ValueError) as exc:
            return "error", {"error": f"发送 {msg[0]} 失败: {type(exc).__name__}: {exc}"}
        waited = 0.0
        try:
            while not self.conn.poll(wait_s):
                waited += wait_s
                if not self._live_child() or waited >= HARD_ACK_TIMEOUT_S:
                    return "timeout", {
                        "error": f"{msg[0]} 等待 ack 超时（已等 {round(waited, 1)}s）",
                        "waited_s": round(waited, 1),
                        "child_alive": self._live_child(),
                    }
                logger.warning(
                    "[perf] memload: %s 尚未回 ack（已等 %.0fs）但子进程仍存活 → 继续等"
                    "（内存吃紧时回 ack 会变慢，不能当故障中止）", msg[0], waited,
                )
                wait_s = min(_ACK_EXTEND_STEP_S, HARD_ACK_TIMEOUT_S - waited)
            reply = self.conn.recv()
        except (EOFError, OSError) as exc:
            return "error", {"error": f"等待 ack 失败: {type(exc).__name__}: {exc}"}
        if not isinstance(reply, (list, tuple)) or len(reply) < 2 or not isinstance(reply[1], dict):
            return "error", {"error": f"子进程返回格式异常: {reply!r}"}
        self.last_info = dict(reply[1])
        if waited:
            self.last_info["waited_s"] = round(waited, 1)
        held = self.last_info.get("held_mb")
        if isinstance(held, (int, float)):
            self.held_mb = int(held)
        return str(reply[0]), self.last_info

    def grow(self, mb: int) -> tuple[str, dict[str, Any]]:
        """请子进程再吃 ``mb`` MB；返回 ``(状态, info)``（info 含 resident/lock 自证字段）。"""
        return self._call(("grow", int(mb), None))

    def touch(self) -> tuple[str, dict[str, Any]]:
        """re-touch 已持有区块（判定前调用，把被换出/压缩的页拉回驻留）。"""
        return self._call(("touch",))

    def refill(self) -> tuple[str, dict[str, Any]]:
        """**全内容重写 + 补锁**：对"占用已被系统压缩/回收"的主动反击（见 ``MemLoadChild.refill``）。"""
        return self._call(("refill",))

    @property
    def refill_rounds(self) -> int:
        value = self.last_info.get("refill_rounds")
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    @property
    def resident_mb(self) -> Optional[float]:
        value = self.last_info.get("resident_mb")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def resident_ratio(self) -> Optional[float]:
        value = self.last_info.get("resident_ratio")
        return float(value) if isinstance(value, (int, float)) else None

    def release(self, timeout_s: float = 10.0) -> dict[str, Any]:
        """释放负载（幂等；异常只记不抛，绝不因清理失败掩盖主结果）。"""
        detail: dict[str, Any] = {
            "held_mb_before": self.held_mb, "stopped": False,
            "released": {}, "alive_after": None,
        }
        if self.conn is not None:
            try:
                self.conn.send(("stop",))
                if self.conn.poll(timeout_s):
                    reply = self.conn.recv()
                    if isinstance(reply, (list, tuple)) and len(reply) > 1:
                        detail["stopped"] = str(reply[0]) == "bye"
                        if isinstance(reply[1], dict):
                            detail["released"] = dict(reply[1].get("released") or {})
            except (EOFError, OSError, ValueError) as exc:
                detail["error"] = f"{type(exc).__name__}: {exc}"
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
        if self.proc is not None:
            try:
                self.proc.join(timeout=5.0)
                if self.proc.is_alive():
                    self.proc.terminate()
                    self.proc.join(timeout=5.0)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[perf] memload: 回收子进程异常: %s", exc)
            detail["alive_after"] = bool(self.proc.is_alive())
        detail["released_at"] = round(time.time(), 3)
        return detail
