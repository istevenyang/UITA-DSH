"""进程树资源采样（RSS / CPU% / 线程数 / 句柄数）。

口径（与风险缓解条目一致，报告需原样标注）：
  - **进程树合计**：主进程 + ``children(recursive=True)``。Electron 的渲染/GPU/utility
    helper 均计入，故数值显著大于单进程 RSS，**不可跨工具直接对比**。
  - **CPU%**：各进程 ``cpu_percent(interval=None)`` 之和；psutil 语义下每核可到 100%，
    故多核机器上进程树合计可 >100%（报告须说明）。
  - **平台能力差异**：Windows 无 ``num_fds`` 语义 → 记 ``None``（不记 0）。

设计约束：
  - 不依赖、也不修改 ``AppProcess`` 私有方法 ``_find_app_procs()``；主进程识别规则
    在本模块内自实现，对齐 ``core/lifecycle/app_process.py`` 既有口径
    （``.app`` 推导进程名 / 排除 ``Helper`` 与 ``--type=`` 子进程 / 跳过 zombie）。
  - 采样在后台线程按固定间隔进行，不阻塞事件循环；``psutil.Process`` 实例按 pid 缓存，
    否则 ``cpu_percent(None)`` 每次新建实例都会返回 0。0（实测教训）。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from uita.core.perf.metrics import linear_regression_slope, summarize

logger = logging.getLogger("uita")

try:  # pragma: no cover - 环境相关
    import psutil  # type: ignore

    _HAS_PSUTIL = True
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore
    _HAS_PSUTIL = False

__all__ = [
    "ResourceSample",
    "ResourceSampler",
    "canonical_process_name",
    "derive_process_name",
    "is_main_process",
    "platform_capabilities",
    "summarize_samples",
    "system_memory_snapshot",
]

#: 空样本"现场诊断"里最多探测几个已知 pid（真机进程树通常 1 主 + 若干 helper；
#: 全列会把 error 文案撑爆，4 个足以判断"读不到"还是"改名了"）。
_DIAG_MAX_PIDS = 4
#: 诊断里单个实读值（name/exe）的最大长度。
_DIAG_VALUE_MAXLEN = 60


def derive_process_name(app_path: str) -> str:
    """从 app_path 推导主进程名（对齐 ``AppProcess._detect_app_name``）。

    ``/Applications/DemoGallery.app/Contents/MacOS/DemoGallery`` → ``DemoGallery``；
    ``C:\\Program Files\\DemoGallery\\DemoGallery.exe`` → ``DemoGallery``（去扩展名）。
    """
    if not app_path:
        return ""
    m = re.search(r"/([^/]+)\.app(/|$)", app_path)
    if m:
        return m.group(1)
    # Windows / 非 .app 形态：取文件名去扩展名
    normalized = app_path.replace("\\", "/")
    base = normalized.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if base else ""


def canonical_process_name(value: str) -> str:
    """进程名归一化（跨平台比较用）：小写 + 去 ``.exe``。

    **必需**（Windows 真机缺陷，2026-09-18 由单测发现）：
    ``derive_process_name()`` 会去掉扩展名得到 ``DemoGallery``，
    而 psutil 在 Windows 上 ``Process.name()`` 返回 **``DemoGallery.exe``**、
    ``exe`` 字段是 ``C:\\...\\DemoGallery.exe``。直接比较会**永远不匹配**
    → 采样树为空 → 所有资源指标变成 ``null``（Windows 上静默失效）。
    """
    name = (value or "").strip().lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def is_main_process(name: str, exe: str, cmdline: list[str], process_name: str) -> bool:
    """是否为被测 App 的主进程（排除 Electron helper 与 ``--type=`` 子进程）。

    平台差异（实测）：
      - macOS：helper 进程名含 ``Helper``（``DemoGallery Helper (Renderer)``）→ 按名排除；
      - Windows：helper 进程名与主进程**同名**（都是 ``DemoGallery.exe``），
        只能靠 ``--type=renderer|gpu-process|utility`` 命令行参数排除（本函数已覆盖）。
    """
    target = canonical_process_name(process_name)
    if not target:
        return False
    pname = (name or "").strip()
    if "Helper" in pname:  # Electron helper（macOS 命名）
        return False
    if any(str(a).startswith("--type=") for a in (cmdline or [])):
        return False
    if canonical_process_name(pname) == target:
        return True
    exe_name = (exe or "").replace("\\", "/").rsplit("/", 1)[-1]
    return canonical_process_name(exe_name) == target


def platform_capabilities(os_name: str) -> dict[str, Any]:
    """本平台的**采集能力**（写入产物 `metric_scope`，避免跨平台读数被误读）。

    差异来源（均为 psutil/OS 语义差异，不是实现选择）：
      - ``num_fds``：Windows 无 fd 语义（psutil 无 `num_fds()`）→ 记 ``null``；
      - ``rss``：psutil ``memory_info().rss`` 在 Windows 等价于 Private Working Set
        口径（与 macOS 的 resident 不完全同义），**跨平台数值不可直接比较**；
      - ``cpu``：Windows 上 ``cpu_percent()`` 以「逻辑处理器总数」为分母，
        进程树合计可远超 100%（macOS 同样可 >100%，但基数不同）。
    """
    platform = (os_name or "").strip().lower()
    is_windows = platform in ("windows", "win", "win32")
    caps: dict[str, Any] = {
        "platform": platform,
        "num_fds": not is_windows,
        "process_tree_sampling": True,
        "cross_window_targets": True,      # 经 CDP /json/list 发现独立窗口（平台无关）
        "notes": [],
    }
    if is_windows:
        caps["notes"] = [
            "num_fds 记 null（Windows 无 fd 语义）",
            "RSS 为 psutil 口径（Windows≈Private Working Set），与 macOS resident 不同义，跨平台不可直接比较",
            "主进程与 helper 同名（*.exe），靠 --type= 参数区分（macOS 靠 Helper 命名）",
        ]
    return caps


def system_memory_snapshot() -> dict[str, Optional[float]]:
    """系统级内存 / swap 快照（**失败现场取证**用；键名与口径与压测 ``stress`` 完全一致）。

    为什么放 core 且只有一份实现（2026-10-15 用户口径：perf 模式启动失败时"和 stress 模式
    数据一致"）：perf 模式某轮**启动失败**时要留下"那一刻系统被占了多少 / 还剩多少"，
    这与 ``stress`` 场景逐级记录的水位必须是**同一套读数**——否则两个数字不可比，
    "启动失败是内存不够"的推断就站不住。``stress`` 场景因此改为委托本函数（禁止各写一份）。

    读数语义（psutil 原值换算，单位 MB）：
      - ``mem_used_mb`` = ``total - available``（**可用**口径，不是 ``free``：Linux/macOS 的
        ``free`` 不含可回收缓存，会系统性偏低）；
      - ``mem_available_mb`` = ``available``（"还剩多少能分出去"，正是启动失败要回答的问题）；
      - ``mem_percent`` = 系统内存占用率（psutil 语义）。

    读数失败（无 psutil / 系统调用异常）→ 对应键为 ``None``：**不抛异常、也不记 0**
    （"未测到"与"真的占用 0"必须可区分）。
    """
    snap: dict[str, Optional[float]] = {
        "mem_total_mb": None, "mem_used_mb": None, "mem_available_mb": None,
        "mem_percent": None, "swap_used_mb": None, "swap_percent": None,
    }
    if not _HAS_PSUTIL:
        return snap
    try:
        vm = psutil.virtual_memory()  # type: ignore[union-attr]
        snap.update(
            mem_total_mb=round(vm.total / 1048576.0, 1),
            mem_used_mb=round((vm.total - vm.available) / 1048576.0, 1),
            mem_available_mb=round(vm.available / 1048576.0, 1),
            mem_percent=round(float(vm.percent), 1),
        )
    except Exception as exc:  # noqa: BLE001 - 读数失败不该中断调用方流程
        logger.debug("[perf] 系统内存读取失败: %s", exc)
    try:
        sw = psutil.swap_memory()  # type: ignore[union-attr]
        snap.update(
            swap_used_mb=round(sw.used / 1048576.0, 1),
            swap_percent=round(float(sw.percent), 1),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[perf] swap 读取失败: %s", exc)
    return snap


def _signals(
    *,
    alive: bool,
    decidable: bool,
    name_scan: Optional[bool],
    pid_alive: Optional[bool],
    main_pid_alive: Optional[bool],
    pids_known: int,
    pids_alive: int,
    reason: str,
    alive_pids: Optional[list[int]] = None,
) -> dict[str, Any]:
    """组装 :meth:`ResourceSampler.alive_signals` 的返回结构（字段固定，便于落产物/单测）。

    ``alive_pids``：判定「活着」的那些已知 pid（升序）。只给计数无法定位到具体进程——
    关闭流程（stress）需要「到底哪个进程还没死」才能精准强杀并留下可读证据。
    """
    return {
        "alive": alive,
        "decidable": decidable,
        "name_scan": name_scan,
        "pid_alive": pid_alive,
        "main_pid_alive": main_pid_alive,
        "pids_known": pids_known,
        "pids_alive": pids_alive,
        "alive_pids": sorted(int(p) for p in (alive_pids or [])),
        "reason": reason,
    }


@dataclass
class ResourceSample:
    """单次采样结果（进程树合计）。"""

    t_ms: int = 0
    rss_mb: Optional[float] = None
    cpu_percent: Optional[float] = None
    thread_count: Optional[int] = None
    num_fds: Optional[int] = None
    proc_count: int = 0
    error: str = ""
    #: 本样本是否由 **pid 兜底**采到（名称扫描为空、改用"上次成功采样看到的 pid"采集）。
    #: 必要性（2026-10-08 真机：静息 557 样本里 286 个空样本）：兜底读数与名称口径读数
    #: 的进程集合不完全等价，必须可辨识，否则跨口径比较失真且无人察觉。
    pid_fallback: bool = False


@dataclass
class ResourceSampler:
    """后台定时采样进程树资源；``enabled=False`` 时全部指标为 ``None``。"""

    app_path: str = ""
    process_name: str = ""
    interval_ms: int = 500
    enabled: bool = True
    _samples: list[ResourceSample] = field(default_factory=list, init=False, repr=False)
    _proc_cache: dict[int, Any] = field(default_factory=dict, init=False, repr=False)
    #: 上一次**成功采样**时看到的进程：``pid → (create_time, 是否主进程)``。
    #: 用于 :meth:`alive_signals` 的 pid 信号（不依赖进程名/排除规则），
    #: ``create_time`` 用于防 pid 被系统复用后误判"还活着"（2026-10-08）。
    _last_seen: dict[int, tuple[float, bool]] = field(default_factory=dict, init=False, repr=False)
    #: 最近一次进程枚举的错误（非空 = 枚举失败/不完整）。空树 + 非空错误 ⇒ **不可判定**，
    #: 绝不能当成"应用已退出"（否则一次 psutil 抖动就会砍掉整个静息窗口）。
    _last_scan_error: str = field(default="", init=False, repr=False)
    #: pid 兜底是否已打过 warning（避免 500ms 一条刷屏，但保证控制台**能**看到规则失效）
    _fallback_logged: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.process_name:
            self.process_name = derive_process_name(self.app_path)

    # ── 单次采样（可单测）────────────────────────────────────────────

    def _scan_tree(self) -> tuple[list[Any], set[int]]:
        """按进程名扫描主进程 + 递归子进程，返回 ``(tree, 主进程 pid 集合)``。

        进程枚举本身也包异常：``psutil.process_iter()`` 在遍历期间可能对某些进程
        抛异常（真机出现过），若冒泡出去会**静默杀掉采样线程**（2026-10-08 加保护）。
        """
        if not _HAS_PSUTIL:
            return [], set()
        self._last_scan_error = ""
        main_procs: list[Any] = []
        try:
            for proc in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
                try:
                    if proc.status() == psutil.STATUS_ZOMBIE:
                        continue
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                except Exception:  # noqa: BLE001 - status() 在部分平台会抛非 psutil 异常
                    continue
                info = proc.info or {}
                if not is_main_process(
                    info.get("name") or "", info.get("exe") or "", info.get("cmdline") or [],
                    self.process_name,
                ):
                    continue
                main_procs.append(proc)
        except Exception as exc:  # noqa: BLE001 - 枚举中断 → 返回已扫到的部分，不抛穿
            self._last_scan_error = f"{type(exc).__name__}: {exc}"
            logger.debug(
                "[perf] 进程枚举中断（返回已扫到的 %d 个）: %s", len(main_procs), exc,
            )

        tree: list[Any] = []
        for proc in main_procs:
            tree.append(proc)
            try:
                tree.extend(proc.children(recursive=True))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception:  # noqa: BLE001
                continue
        return tree, {p.pid for p in main_procs}

    def _iter_tree(self) -> list[Any]:
        """主进程 + 递归子进程的 psutil.Process 列表（:meth:`_scan_tree` 的兼容封装）。"""
        return self._scan_tree()[0]

    @staticmethod
    def _safe_children(proc: Any) -> list[Any]:
        """``proc.children(recursive=True)`` 的异常安全封装（拿不到就当无子进程）。"""
        try:
            return list(proc.children(recursive=True))
        except Exception:  # noqa: BLE001
            return []

    def _pid_fallback_tree(self) -> tuple[list[Any], set[int]]:
        """名称扫描为空时，用**已知 pid**（上次成功采样看到的进程）兜底重建进程树。

        为什么需要（2026-10-08 真机：静息 557 个样本里 286 个是空样本、有效窗口只剩
        146.6s / 302.4s）：存活判定有"名称扫描 OR 已见 pid"两个信号，而**读数采集只有
        名称扫描一条路**——于是"名称规则失效但进程还在"时，会出现"判对了存活却采不到数"，
        且空样本会一直持续到静置结束。本方法用 pid 直接采，把这段读数救回来。

        Returns:
            ``(tree, main_pids)``；无 psutil / 无已知 pid / 全部不可访问时返回 ``([], set())``
            （调用方据此走原空样本分支，行为与改动前一致）。
        """
        if not _HAS_PSUTIL or not self._last_seen:
            return [], set()
        tree: list[Any] = []
        main_pids: set[int] = set()
        seen: set[int] = set()
        for pid, (create_time, is_main) in list(self._last_seen.items()):
            if not self._pid_alive(pid, create_time):
                continue
            proc = self._proc_cache.get(pid)
            if proc is None:
                try:
                    proc = psutil.Process(pid)
                except Exception:  # noqa: BLE001 - 打不开该 pid 就跳过，不影响其余
                    continue
                self._proc_cache[pid] = proc
            # 按 pid 去重：`_last_seen` 里可能同时有主进程与其子进程，
            # 各自 children() 会互相包含 → 不去重会把同一进程的 RSS 重复累加。
            for candidate in [proc, *self._safe_children(proc)]:
                if candidate.pid in seen:
                    continue
                seen.add(candidate.pid)
                tree.append(candidate)
                if candidate.pid == pid and is_main:
                    main_pids.add(candidate.pid)
        return tree, main_pids

    @staticmethod
    def _read_attr(proc: Any, attr: str) -> str:
        """读进程属性用于诊断（``name``/``exe``），任何失败都如实记为 ``<异常类型>``。

        优先取 ``proc.info[attr]``（``process_iter(attrs=[...])`` 的形态，替身与真机都有），
        取不到再退化为方法/属性访问——两者在"属性被 AccessDenied 置 None"时都给出可判读的证据。
        """
        info = getattr(proc, "info", None)
        if isinstance(info, dict) and info.get(attr) is not None:
            return str(info[attr])[:_DIAG_VALUE_MAXLEN]
        value = getattr(proc, attr, None)
        if value is None:
            return "<不可读>"
        try:
            if callable(value):
                value = value()
        except Exception as exc:  # noqa: BLE001 - 异常类型本身就是证据
            return f"<{type(exc).__name__}>"
        if value is None:
            return "<None>"
        return str(value)[:_DIAG_VALUE_MAXLEN]

    def _empty_scan_diagnostics(self) -> str:
        """名称扫描为空时的**现场取证**（拼进空样本文案，供真机区分根因）。

        必要性：`is_main_process()` 只看 psutil 读到的 ``name``/``exe``，返回 False 可能是
        ①属性读不到（``AccessDenied``/``None``）或 ②进程名/路径真的变了。旧文案两句都不记，
        真机只能猜（2026-10-08 用户质询"为什么还有空样本"时无法回答）。
        """
        parts = [f"期望进程名={self.process_name or '(空)'}"]
        try:
            parts.append(f"系统进程数={len(psutil.pids())}")
        except Exception as exc:  # noqa: BLE001 - 枚举能力本身也可能不可用
            parts.append(f"系统进程数=读取失败({type(exc).__name__})")
        probes: list[str] = []
        for pid, (_create_time, is_main) in list(self._last_seen.items())[:_DIAG_MAX_PIDS]:
            tag = "(main)" if is_main else ""
            try:
                proc = psutil.Process(pid)
            except Exception as exc:  # noqa: BLE001
                probes.append(f"pid={pid}{tag} 打开失败({type(exc).__name__})")
                continue
            probes.append(
                f"pid={pid}{tag} name={self._read_attr(proc, 'name')} "
                f"exe={self._read_attr(proc, 'exe')}"
            )
        if probes:
            parts.append("已知 pid 实读: " + ", ".join(probes))
        return "；诊断: " + " ".join(parts)

    def _pid_alive(self, pid: int, create_time: float) -> bool:
        """单个 pid 是否仍存活（带 ``create_time`` 校验，防 pid 被系统复用）。"""
        try:
            proc = psutil.Process(pid)
            if create_time:
                try:
                    if abs(float(proc.create_time()) - create_time) > 0.5:
                        return False        # create_time 不符 → 是复用后的另一个进程
                except Exception:  # noqa: BLE001 - 取不到 create_time 时退化为"仅比 pid"
                    pass
            return True
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True                     # 存在但不可读 → 保守算存活（绝不误砍测量窗口）
        except Exception:  # noqa: BLE001 - 其它异常按"不可判定"处理，不计入存活
            return False

    def _known_pid_probe(self) -> tuple[int, Optional[bool], list[int]]:
        """探 `_last_seen` 里的 pid，返回 ``(存活数量, 主进程是否存活, 存活 pid 列表)``。"""
        alive_count = 0
        main_alive: Optional[bool] = None
        alive_pids: list[int] = []
        for pid, (create_time, is_main) in list(self._last_seen.items()):
            alive = self._pid_alive(pid, create_time)
            if alive:
                alive_count += 1
                alive_pids.append(int(pid))
                if is_main:
                    main_alive = True
            elif is_main and main_alive is None:
                main_alive = False
        return alive_count, main_alive, alive_pids

    def alive_signals(self, *, name_scan_hits: Optional[list[Any]] = None) -> dict[str, Any]:
        """被测应用是否还活着——**多信号**判定（任一信号为真即视为存活）。

        为什么不能只看进程名（2026-10-08 真机争议）：按进程名扫描是"规则匹配"，
        当进程还在、但名称/排除规则失效或 psutil 读不到 `name/exe` 时同样会扫空——
        旧实现据此判定"应用已退出"，把本来有效的静息窗口砍掉（用户现场看到应用仍在后台）。
        因此增加**不依赖名字**的第二信号：上一次成功采到的 pid 是否仍存活。

        信号（``alive = name_scan or pid_alive``）：
          - ``name_scan``：按进程名扫描命中（与资源采样同一口径）；
          - ``pid_alive``：已见 pid 仍存活（带 ``create_time`` 校验防 pid 复用；
            ``AccessDenied`` 保守算存活）。

        Args:
            name_scan_hits: 调用方**已经扫过**的结果（如空样本分支），传入可避免重复扫全表。

        Returns:
            ``{"alive", "decidable", "name_scan", "pid_alive", "main_pid_alive",
            "pids_known", "pids_alive", "alive_pids", "reason"}``；不可判定（未启用采样 / 无 psutil /
            探测异常）时 ``decidable=False`` 且 ``alive=True``——调用方**不应**据此中断测量。
        """
        if not self.enabled or not _HAS_PSUTIL:
            return _signals(
                alive=True, decidable=False, name_scan=None, pid_alive=None,
                main_pid_alive=None, pids_known=len(self._last_seen), pids_alive=0,
                reason="不可判定（资源采样未启用或无 psutil）——按存活处理",
            )
        if name_scan_hits is None:
            try:
                hits = self._iter_tree()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[perf] alive_signals 名称扫描异常: %s", exc)
                return _signals(
                    alive=True, decidable=False, name_scan=None, pid_alive=None,
                    main_pid_alive=None, pids_known=len(self._last_seen), pids_alive=0,
                    reason=f"不可判定（名称扫描异常，按存活处理）: {exc}",
                )
        else:
            hits = list(name_scan_hits)

        if not hits and self._last_scan_error:
            # 枚举失败导致的"空"== 不可判定：绝不能据此判定应用已退出
            return _signals(
                alive=True, decidable=False, name_scan=None, pid_alive=None,
                main_pid_alive=None, pids_known=len(self._last_seen), pids_alive=0,
                reason=f"不可判定（进程枚举失败，按存活处理）: {self._last_scan_error}",
            )

        name_scan = bool(hits)
        pids_alive, main_alive, alive_pids = self._known_pid_probe()
        alive = name_scan or pids_alive > 0
        if name_scan:
            reason = "名称扫描命中被测进程"
        elif pids_alive > 0:
            reason = (
                "名称扫描为空，但已知 pid 仍存活"
                f"（{pids_alive}/{len(self._last_seen)}，主进程 pid "
                f"{'在' if main_alive else '不在'}）——进程名/排除规则可能已变化"
            )
        else:
            reason = "名称扫描为空且已知 pid 均已消失（应用可能已退出）"
        return _signals(
            alive=alive, decidable=True, name_scan=name_scan, pid_alive=pids_alive > 0,
            main_pid_alive=main_alive, pids_known=len(self._last_seen), pids_alive=pids_alive,
            reason=reason, alive_pids=alive_pids,
        )

    def has_target_process(self) -> bool:
        """被测应用当前是否存在（:meth:`alive_signals` 的布尔薄封装，契约不变）。

        Returns:
            ``True``  — 任一存活信号为真，**或不可判定**（未启用采样 / 无 psutil /
                        探测异常）——调用方**不应**据此中断测量；
            ``False`` — 名称扫描为空**且**已知 pid 全部消失，确认不存在。
        """
        return bool(self.alive_signals().get("alive", True))

    def target_pids(self) -> dict[str, list[int]]:
        """当前被测应用的 pid 集合——**与读数同一进程名口径**，供外部强杀/诊断用。

        为什么需要（2026-10-13 真机）：按**配置里的进程名**杀进程会在两种情况下漏杀——
        ① 映像名与 ``app_process.app_name`` 不一致（按名强杀是精确匹配）；
        ② 主进程已死但子进程仍在（``is_main_process`` 会排除 ``--type=`` 子进程）。
        外部强杀改成"杀掉**探测到的** pid"后，两种情况都能收敛。

        Returns:
            ``{"main": [...], "tree": [...], "known": [...]}``
              - ``main``：名称扫描命中的主进程 pid；
              - ``tree``：主进程 + 递归子进程 pid（与资源读数同一集合）；
              - ``known``：:attr:`_last_seen` 里**仍存活**的 pid（名称规则失效时的兜底信号）。
            无 psutil / 枚举异常 → 各键为空列表；**空不等于"应用不存在"**（可能是枚举失败），
            调用方不得据此判定结论。
        """
        if not _HAS_PSUTIL:
            return {"main": [], "tree": [], "known": []}
        try:
            tree, main_pids = self._scan_tree()
        except Exception as exc:  # noqa: BLE001 - 枚举失败按"没有可用 pid"处理
            logger.debug("[perf] target_pids 名称扫描异常: %s", exc)
            tree, main_pids = [], set()
        known: list[int] = []
        for pid, (create_time, _is_main) in list(self._last_seen.items()):
            try:
                if self._pid_alive(int(pid), create_time):
                    known.append(int(pid))
            except Exception:  # noqa: BLE001 - 单 pid 判定失败不影响其余
                continue
        return {
            "main": sorted(int(p) for p in main_pids),
            "tree": sorted({int(p.pid) for p in tree}),
            "known": sorted(known),
        }

    def sample_once(self) -> Optional[ResourceSample]:
        """采一次；无 psutil / 未找到进程时返回带 ``error`` 的空样本。

        空样本**也必须带 ``t_ms``**：``summarize_samples()`` 用样本时间跨度算
        ``rss_slope_window_s``，若空样本的 ``t_ms`` 为 0，会把有效窗口虚大
        （实测：真实有效 135s 的静息被算成 600s，2026-10-08 修复）。
        """
        if not self.enabled:
            return None
        now_ms = int(time.monotonic() * 1000)
        if not _HAS_PSUTIL:
            return ResourceSample(t_ms=now_ms, error="psutil 不可用")
        tree, main_pids = self._scan_tree()
        #: 本样本是否走了 pid 兜底（进产物，让"这段读数不是名称口径"可见）
        pid_fallback = False
        if not tree:
            if self._last_scan_error:
                # 枚举失败 ≠ 应用退出：如实说明"本次读数缺失"，不产出误导性结论
                return ResourceSample(
                    t_ms=now_ms,
                    error=f"进程枚举失败，本次读数缺失（不视为应用退出）: {self._last_scan_error}",
                )
            # 名称扫描为空 → 先用**已知 pid** 兜底采集（2026-10-08 真机：静息 557 个样本里
            # 286 个因"名称规则失效但进程仍在"变成空样本，有效窗口被压到 146.6s/302.4s）。
            # 兜底也拿不到读数，才走下面的空样本分支（文案分因 + 现场诊断）。
            tree, main_pids = self._pid_fallback_tree()
            pid_fallback = bool(tree)
            if pid_fallback and not self._fallback_logged:
                self._fallback_logged = True
                logger.warning(
                    "[perf] 名称扫描为空（进程名/排除规则可能已变化），已改用已知 pid 兜底采样"
                    "（该段读数标记为 pid_fallback）；期望进程名=%s，兜底进程数=%d",
                    self.process_name or "(空)", len(tree),
                )
        if not tree:
            # 文案**分因**（2026-10-08）："名称扫不到"有两种截然不同的原因，
            # 不区分会让"进程还在、只是规则/名称变了"被读成"应用已退出"。
            signals = self.alive_signals(name_scan_hits=[])
            if signals.get("pid_alive"):
                return ResourceSample(
                    t_ms=now_ms,
                    error=(
                        "名称扫描为空，且已知 pid 兜底也取不到读数（pid 仍存活）"
                        "——资源读数暂缺，不建议据此判定应用已退出"
                        + self._empty_scan_diagnostics()
                    ),
                )
            return ResourceSample(
                t_ms=now_ms,
                error=(
                    "未找到目标进程（名称扫描为空，且已知 pid 均已消失——应用可能已退出）"
                    + self._empty_scan_diagnostics()
                ),
            )

        rss_bytes = 0
        cpu_percent = 0.0
        thread_count = 0
        num_fds: Optional[int] = 0
        counted = 0
        for proc in tree:
            pid = proc.pid
            cached = self._proc_cache.get(pid)
            if cached is None:
                self._proc_cache[pid] = proc
                cached = proc
            try:
                mem = cached.memory_info()
                rss_bytes += mem.rss
                cpu_percent += cached.cpu_percent(interval=None) or 0.0
                thread_count += cached.num_threads()
                counted += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._proc_cache.pop(pid, None)
                continue
            except Exception:  # noqa: BLE001
                continue
            if num_fds is not None:
                try:
                    num_fds += cached.num_fds()
                except (AttributeError, NotImplementedError):
                    num_fds = None  # Windows 无该语义
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
                except Exception:  # noqa: BLE001
                    num_fds = None

        # 清理已退出 pid 的缓存，避免长期运行时无限增长
        alive = {p.pid for p in tree}
        for pid in list(self._proc_cache):
            if pid not in alive:
                self._proc_cache.pop(pid, None)

        # 记录"本次成功采到的进程"→ alive_signals() 的 pid 信号据此判定
        # （不依赖进程名/排除规则，用于兜住"名称扫不到但进程还在"的情形）。
        seen: dict[int, tuple[float, bool]] = {}
        for proc in tree:
            try:
                create_time = float(proc.create_time())
            except Exception:  # noqa: BLE001 - 取不到则记 0，判定时退化为"仅比 pid"
                create_time = 0.0
            seen[proc.pid] = (create_time, proc.pid in main_pids)
        if seen:
            self._last_seen = seen

        if counted == 0:
            return ResourceSample(
                t_ms=now_ms,
                error=(
                    "进程树内所有进程均不可访问（AccessDenied）"
                    + self._empty_scan_diagnostics()
                ),
                pid_fallback=pid_fallback,
            )
        return ResourceSample(
            t_ms=now_ms,
            rss_mb=round(rss_bytes / 1048576.0, 2),
            cpu_percent=round(cpu_percent, 2),
            thread_count=thread_count,
            num_fds=num_fds,
            proc_count=counted,
            pid_fallback=pid_fallback,
        )

    # ── 后台采样 ──────────────────────────────────────────────────

    def _loop(self) -> None:
        interval_s = max(self.interval_ms, 50) / 1000.0
        while not self._stop_event.is_set():
            try:
                sample = self.sample_once()
            except Exception as exc:  # noqa: BLE001 - 单次采样异常绝不允许静默杀掉采样线程
                logger.warning("[perf] 资源采样异常（跳过该次，采样继续）: %s", exc)
                sample = None
            if sample is not None:
                with self._lock:
                    self._samples.append(sample)
            self._stop_event.wait(interval_s)

    def start(self) -> None:
        """启动后台采样（立即采一次，保证极短用例也有样本）。"""
        if not self.enabled or not _HAS_PSUTIL:
            return
        try:
            first = self.sample_once()
        except Exception as exc:  # noqa: BLE001 - 首采异常不该让整轮 perf 起不来
            logger.warning("[perf] 首次数采样异常（忽略）: %s", exc)
            first = None
        if first is not None:
            with self._lock:
                self._samples.append(first)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="uita-perf-sampler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """停止采样并等待线程退出（异常安全）。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    # ── 汇总 ─────────────────────────────────────────────────────

    @property
    def samples(self) -> list[ResourceSample]:
        with self._lock:
            return list(self._samples)

    def summary(self) -> dict[str, Any]:
        """进程树资源汇总（全样本）；无样本时各指标为 ``None``（不记 0）。"""
        return summarize_samples(
            self.samples, enabled=self.enabled, interval_ms=self.interval_ms,
        )

    def summary_between(self, t0_ms: int, t1_ms: int) -> dict[str, Any]:
        """按时间窗切片汇总（场景 1「启动期」/场景 2「稳态期」共用同一口径）。

        Args:
            t0_ms / t1_ms: 与 ``ResourceSample.t_ms`` 同源的单调时钟毫秒边界（左闭右开）
        """
        window = [s for s in self.samples if t0_ms <= s.t_ms < t1_ms]
        return summarize_samples(window, enabled=self.enabled, interval_ms=self.interval_ms)


def summarize_samples(
    samples: list[ResourceSample],
    *,
    enabled: bool = True,
    interval_ms: int = 0,
    min_slope_window_s: float = 60.0,
) -> dict[str, Any]:
    """对一组样本做汇总（模块级函数，便于按阶段切片复用同一口径）。

    缺失值一律 ``None``；``error_samples`` 单独计数（如应用尚未启动时的
    "未找到目标进程"空样本），它们不参与峰值/均值计算，但必须可见——
    否则「启动期只有 1 个空样本」会被误读成「启动期资源为 0」。

    **RSS 斜率只在窗口足够长时才给值**：实测过 2–4s 的窗口算出 4035 MB/min 的
    荒谬值（把启动爬升/首屏渲染当成"稳态漂移"）。稳态度量必须靠
    ``--perf-steady-state-s`` 静置出足够长的窗口（B.3 场景 2 建议 ≥5min）。
    """
    if not samples:
        return {
            "enabled": enabled,
            "sample_count": 0,
            "error_samples": 0,
            "sample_interval_ms": interval_ms,
            "rss_mb_avg": None,
            "rss_mb_peak": None,
            "rss_slope_window_s": None,
            "rss_slope_min_window_s": min_slope_window_s,
            "rss_slope_mb_per_min": None,
            "cpu_percent_avg": None,
            "cpu_percent_peak": None,
            "thread_count_peak": None,
            "num_fds_peak": None,
            "proc_count_peak": None,
            "pid_fallback_samples": 0,
            "errors": ["未采集到任何样本"],
        }
    rss = summarize([s.rss_mb for s in samples])
    cpu = summarize([s.cpu_percent for s in samples])
    threads = summarize([s.thread_count for s in samples])
    fds = summarize([s.num_fds for s in samples])
    procs = summarize([s.proc_count for s in samples])
    errors = sorted({s.error for s in samples if s.error})
    # 窗口跨度**只认有效样本**（``rss_mb is not None``）：空样本（"未找到目标进程"）
    # 不代表任何可观测窗口，混进来会把有效窗口虚大（2026-10-08 修复）。
    valid = [s for s in samples if s.rss_mb is not None]
    window_s = (
        round((max(s.t_ms for s in valid) - min(s.t_ms for s in valid)) / 1000.0, 1)
        if len(valid) >= 2 else 0.0
    )
    # RSS 漂移斜率（MB/min）：仅在窗口足够长时给值（否则会把启动爬升当稳态漂移）
    slope = None
    if window_s >= min_slope_window_s:
        slope = linear_regression_slope(
            [s.t_ms / 60000.0 for s in valid], [s.rss_mb for s in valid],
        )
    return {
        "enabled": enabled,
        "sample_count": len(samples),
        "error_samples": sum(1 for s in samples if s.error),
        "sample_interval_ms": interval_ms,
        "rss_mb_avg": rss["avg"],
        "rss_mb_peak": rss["max"],
        "rss_slope_window_s": window_s,
        "rss_slope_min_window_s": min_slope_window_s,
        "rss_slope_mb_per_min": None if slope is None else round(slope, 3),
        "cpu_percent_avg": cpu["avg"],
        "cpu_percent_peak": cpu["max"],
        "thread_count_peak": int(threads["max"]) if threads["max"] is not None else None,
        "num_fds_peak": int(fds["max"]) if fds["max"] is not None else None,
        "proc_count_peak": int(procs["max"]) if procs["max"] is not None else None,
        # pid 兜底样本数：>0 表示这段读数的进程集合来自"已知 pid"而非名称扫描口径，
        # 跨口径比较时必须能看到（2026-10-08 真机"名称规则失效"场景）。
        "pid_fallback_samples": sum(1 for s in samples if s.pid_fallback),
        "errors": errors,
    }
