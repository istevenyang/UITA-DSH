"""core/perf/console.py — perf 控制台精简输出（只留轮次 / 节点耗时 / 失败）。

为什么要单开一层：一次 ``--mode perf`` 批跑的控制台被过程日志淹没——
``[hang-trace]``（每个 step 的 START/END/WATCHDOG 十余条）、引擎附着轮询、
截图追踪等 ``NOTICE`` 记录，以及 app 侧逐轮闸门的 ``INFO`` 记录。真正要看的
「当前第几轮 / 走到哪个节点 / 各节点耗时 / 哪一轮失败」反而要人肉在几百行里翻。

口径（只改**控制台**，不改任何日志的写入内容与级别）：

- 日志文件 ``logs/<date>.log`` 由 root ``FileHandler``（恒 ``DEBUG``）完整保留
  全部明细 → 诊断能力不降级，精简只是"不再往控制台重复一份"；
- 精简后**放行**：``WARNING`` 及以上（失败/降级必须可见）+ 消息以 ``[perf`` /
  ``[app`` 开头的记录（框架跑批进度、场景节点耗时、app 侧每轮闸门）+ ``print()``
  打的 ``[perf] ...`` 规划/产物行（不经 logging，天然不受影响）；
- 精简后**丢弃**：``[hang-trace]`` 全部记录，以及其余 ``INFO`` / ``NOTICE``
  过程日志。白名单语义（不是黑名单）——新增日志默认不进控制台。

开关（R5：非敏感配置，env 声明默认值）：

- ``UITA_PERF_CONCISE`` 未设 或 ``1`` → 精简（默认）；
- ``UITA_PERF_CONCISE=0`` → 原样输出全部过程日志（排障 / 首次接入时用）。

纯标准库（``logging`` / ``os``），不依赖引擎/应用（R1）。
"""
from __future__ import annotations

import io
import logging
import os
from typing import Any

__all__ = [
    "CONCISE_ENV",
    "CORE_MESSAGE_PREFIXES",
    "CoreOnlyFilter",
    "concise_enabled",
    "enable_concise_console",
    "human_ms",
    "is_core_record",
    "round_tag",
]

#: 精简开关环境变量（``0`` / ``false`` / ``no`` / ``off`` = 关闭精简）
CONCISE_ENV = "UITA_PERF_CONCISE"

#: 精简后仍放行的消息前缀：``[perf]*`` = 框架跑批/场景节点，``[app]*`` = app 侧闸门
CORE_MESSAGE_PREFIXES: tuple[str, ...] = ("[perf", "[app")

#: 控制台一律丢弃的消息标记（文件里仍有，诊断不降级）：
#: - ``[hang-trace]``：每个 step 十余条的卡死追踪，内容最密的噪声；
#: - ``[perf.fingerprint_wait]``：与 ``[perf.probe] <元素名>：渲染完成时间 …`` 重复的中间过程
#:   （它的轮询次数/模板/conf 留在文件里），且它拿不到轮次标签，留着会把带标签的决定行冲散。
_DROPPED_MARKERS: tuple[str, ...] = (
    "[hang-trace]", "[perf.fingerprint_wait]", "[perf.element_wait]",
)

#: 已装过 filter 的 handler 标记（幂等：重复调用不叠加 filter）
_APPLIED_ATTR = "_uita_perf_concise"

_TRUTHY_OFF = ("0", "false", "no", "off")


def concise_enabled() -> bool:
    """当前是否启用精简控制台（未设环境变量 = 启用）。"""
    raw = (os.environ.get(CONCISE_ENV) or "").strip().lower()
    if not raw:
        return True
    return raw not in _TRUTHY_OFF


def is_core_record(record: logging.LogRecord) -> bool:
    """该记录是否属于"核心信息"（精简模式下唯一放行判据）。"""
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001 - 格式化失败（参数不匹配）不该让日志系统炸
        message = str(record.msg)
    if any(marker in message for marker in _DROPPED_MARKERS):
        return False
    if record.levelno >= logging.WARNING:
        return True
    return message.lstrip().startswith(CORE_MESSAGE_PREFIXES)


class CoreOnlyFilter(logging.Filter):
    """只放行核心信息的 filter（WARNING+ / ``[perf`` / ``[app``）。"""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003 - 覆写父类签名
        return is_core_record(record)


def _is_console_handler(handler: logging.Handler) -> bool:
    """控制台 handler：``StreamHandler`` 且不是写文件的 ``FileHandler``。

    再排除 ``stream`` 为内存缓冲（``io.StringIO``）的 handler：那不是控制台，而是
    测试框架的日志捕获器。装上去的 filter 跟着 handler 对象长期存活（比单条用例
    活得久），会把此后所有捕获按白名单拦掉；表现为别的用例 ``caplog`` 突然全空。
    """
    if isinstance(handler, logging.FileHandler):
        return False
    if not isinstance(handler, logging.StreamHandler):
        return False
    return not isinstance(getattr(handler, "stream", None), io.StringIO)


def _apply_to_handlers(handlers: list[logging.Handler]) -> bool:
    """给控制台 handler 装 filter 并把阈值降到 ``INFO``；返回是否至少生效一个。

    为什么降阈值：核心里程碑（``[perf] 第 N 轮前置准备完成`` / ``[perf.probe]
    render_ms=…``）本身是 ``INFO``，控制台默认阈值是 ``NOTICE``（25）——不降就
    看不见，噪音改由 filter 白名单拦。
    """
    applied = False
    for handler in handlers:
        if not _is_console_handler(handler):
            continue
        if getattr(handler, _APPLIED_ATTR, False):
            applied = True
            continue
        handler.addFilter(CoreOnlyFilter())
        if handler.level > logging.INFO:
            handler.setLevel(logging.INFO)
        setattr(handler, _APPLIED_ATTR, True)
        applied = True
    return applied


def enable_concise_console() -> bool:
    """按开关启用精简控制台；返回是否生效（幂等，重复调用无副作用）。"""
    if not concise_enabled():
        return False
    return _apply_to_handlers(list(logging.getLogger().handlers))


def round_tag(run_context: Any, round_index: int) -> str:
    """本轮控制台标签：``[<预加载范围>-<轮次>]``，如 ``[Both-1]`` / ``[Game-2]``。

    为什么需要：一批跑 ``--load-mode all`` 时同一屏里混着 blank/off/soft/game/both
    各档的多轮输出，光看行内容分不清"这一行属于哪一档哪一轮"。档位名取自批次上下文
    （``run_context.load_mode``，core 不解释其语义，只透传）；app 没有这个维度时退化为
    ``[Round-2]``——只丢档位，不丢轮次。
    """
    context = run_context if isinstance(run_context, dict) else {}
    mode = str(context.get("load_mode") or "").strip().lower()
    index = int(round_index or 0)
    if not mode:
        return f"[Round-{index}]"
    return f"[{mode[:1].upper()}{mode[1:]}-{index}]"


def human_ms(value: Any) -> str:
    """毫秒 → 人类可读（``842ms`` / ``8.4s`` / ``1m02s``）；不可用返回 ``-``。

    用于控制台行（轮次/批次结语）：整数毫秒在"几分钟"量级上没法一眼读懂。
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "-"
    ms = max(0, int(value))
    if ms < 1000:
        return f"{ms}ms"
    if ms < 60_000:
        return f"{ms / 1000.0:.1f}s"
    minutes, rest_ms = divmod(ms, 60_000)
    return f"{minutes}m{rest_ms / 1000.0:04.1f}s"
