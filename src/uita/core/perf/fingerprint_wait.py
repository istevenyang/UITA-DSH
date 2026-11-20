"""core/perf/fingerprint_wait.py — 轮询等待「视觉指纹出现」，并给出命中详情。

为什么不用 TCS 的 ``wait``：``action_wait`` 的轮询间隔是 1s 且只回 ``duration_ms``，
测 1–3s 的页面渲染时间粒度不够、也拿不到"哪张指纹命中/置信度"。性能测量需要
100–200ms 粒度 + 命中详情，故单独提供本模块（两条路径共用 ``Resolver.resolve_visual``
底层，语义不会漂移）。

匹配语义（"5–10 张任一命中即成功"）由 **rosetta_map 元素级 multi-pattern 数组**提供
（``pattern_win: [z85..., z85...]`` → ``ensure_from_patterns`` → ``match()`` 全模板择优），
本模块不含候选逻辑，只负责"轮询到命中"。

纪律：
- 该元素没配指纹（``has_initial_visual_fingerprint`` 为假）→ **立即返回**
  ``reason="no_fingerprint"``，不空等超时、不伪造 0；
- 每轮只做"抓内存画面 + L3.8 匹配"（``Resolver.visual_present``：不编码 PNG、不落盘、不构造
  坐标换算器）；Resolver/引擎不支持时退回"截图 + 匹配"（``resolve_visual``）。两条路径都不碰
  SEMANTIC/ROLE/LLM 策略；
- 纯鸭子类型依赖 Resolver，core/perf 不 import runner/engine（R1）。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("uita")

__all__ = ["FingerprintHit", "wait_fingerprint"]


@dataclass
class FingerprintHit:
    """一次"等指纹"的结果。"""

    hit: bool
    elapsed_ms: int = 0
    hit_at_ms: int = 0
    attempts: int = 0
    template: str = ""
    confidence: Optional[float] = None
    algo: str = ""
    bbox: Any = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "hit": bool(self.hit),
            "elapsed_ms": int(self.elapsed_ms),
            "hit_at_ms": int(self.hit_at_ms),
            "attempts": int(self.attempts),
            "template": self.template,
            "confidence": self.confidence,
            "algo": self.algo,
            "bbox": list(self.bbox) if isinstance(self.bbox, (list, tuple)) else self.bbox,
            "reason": self.reason,
        }


def _monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


async def _maybe_await(value: Any) -> Any:
    """兼容同步/异步的鸭子类型调用（Resolver 侧方法可能是 async，也可能不是）。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _has_fingerprint(resolver: Any, name: str) -> Optional[bool]:
    """该元素是否配了视觉指纹；Resolver 未提供判定能力时返回 ``None``（"不知道"）。"""
    probe = getattr(resolver, "has_initial_visual_fingerprint", None)
    if not callable(probe):
        return None
    try:
        return bool(probe(name))
    except Exception as exc:  # noqa: BLE001 - 判定失败按"不知道"处理，不误报无指纹
        logger.debug("[perf.fingerprint_wait] has_initial_visual_fingerprint 异常: %s", exc)
        return None


async def _present_live(resolver: Any, name: str, threshold: Optional[float]) -> Optional[bool]:
    """优先走"内存画面直接比对"（``Resolver.visual_present``，不截图落盘）。

    返回 ``True``/``False`` = 已判定；``None`` = 该能力不可用（Resolver 无此方法 / 引擎抓不到
    内存画面 / 返回非布尔），调用方退回 ``resolve_visual``（截图 + 匹配）。
    """
    probe = getattr(resolver, "visual_present", None)
    if not callable(probe):
        return None
    value = await _maybe_await(probe(name, threshold))
    return value if isinstance(value, bool) else None


async def _detail(resolver: Any, name: str, threshold: Optional[float]) -> dict[str, Any]:
    """命中后取详情（模板名/置信度/算法/bbox）；能力缺失时返回空 dict。"""
    getter = getattr(resolver, "visual_match_detail", None)
    if not callable(getter):
        return {}
    try:
        detail = await _maybe_await(getter(name, threshold))
    except Exception as exc:  # noqa: BLE001 - 详情只影响报告可读性，不影响命中判定
        logger.debug("[perf.fingerprint_wait] visual_match_detail 异常（忽略）: %s", exc)
        return {}
    return dict(detail) if isinstance(detail, dict) else {}


async def wait_fingerprint(
    resolver: Any,
    name: str,
    *,
    timeout_ms: int = 20000,
    poll_ms: int = 200,
    match_threshold: Optional[float] = None,
    with_detail: bool = True,
) -> FingerprintHit:
    """轮询等待某元素的视觉指纹出现（任一模板命中即成功）。

    Args:
        resolver: 具备 ``resolve_visual`` 的 Resolver（可额外提供
            ``has_initial_visual_fingerprint`` / ``visual_match_detail`` 以启用快速失败与详情）。
        name: rosetta_map 元素名（其 ``pattern_*`` 数组即指纹组）。
        timeout_ms: 超时上限。
        poll_ms: 定频轮询周期（相邻两轮**起点**间隔，含该轮所有模板的截图 + 匹配耗时）；
            单轮耗时超过周期时下一轮立即开始，不叠加额外 sleep。
        match_threshold: 本次调用的匹配阈值（``None`` = 用 L3.8 默认 0.7）。
        with_detail: 命中后是否再取一次详情（多一次匹配，仅命中时发生，成本可忽略）。

    Returns:
        :class:`FingerprintHit`；``reason`` ∈ ``matched`` / ``timeout`` / ``no_fingerprint`` / ``error``。
    """
    started = _monotonic_ms()
    interval_s = max(0.02, float(poll_ms) / 1000.0)
    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0

    if _has_fingerprint(resolver, name) is False:
        logger.info("[perf.fingerprint_wait] 元素 %r 未配置视觉指纹，跳过等待", name)
        return FingerprintHit(hit=False, attempts=0, reason="no_fingerprint")

    attempts = 0
    errors = 0
    over_budget = 0
    while True:
        attempts += 1
        round_started = time.monotonic()
        try:
            found = await _present_live(resolver, name, match_threshold)
            if found is None:
                result = await _maybe_await(resolver.resolve_visual(name, match_threshold))
                found = bool(getattr(result, "found", False))
                element = getattr(result, "element", None)
                if found and element is None:
                    # 命中但没有句柄：按"未命中"继续轮询（不把异常结构当成功）
                    found = False
            if found:
                now = _monotonic_ms()
                hit = FingerprintHit(
                    hit=True,
                    elapsed_ms=now - started,
                    hit_at_ms=now,
                    attempts=attempts,
                    reason="matched",
                )
                if with_detail:
                    detail = await _detail(resolver, name, match_threshold)
                    hit.template = str(detail.get("template") or "")
                    conf = detail.get("confidence")
                    hit.confidence = float(conf) if isinstance(conf, (int, float)) else None
                    hit.algo = str(detail.get("algo") or "")
                    hit.bbox = detail.get("bbox")
                logger.info(
                    "[perf.fingerprint_wait] 命中 %r（%d 次尝试 / %dms）%s",
                    name, attempts, hit.elapsed_ms,
                    f" 模板={hit.template} conf={hit.confidence}" if hit.template else "",
                )
                return hit
        except Exception as exc:  # noqa: BLE001 - 单轮异常继续轮询，超时后如实报告
            errors += 1
            logger.debug("[perf.fingerprint_wait] 第 %d 次轮询异常: %s", attempts, exc)

        if time.monotonic() >= deadline:
            # 全部尝试都抛异常 → 是"测不了"（error），不是"页面没出来"（timeout）
            reason = "error" if errors and errors >= attempts else "timeout"
            logger.info(
                "[perf.fingerprint_wait] 未命中 %r：%s（%d 次尝试 / %dms）",
                name, reason, attempts, _monotonic_ms() - started,
            )
            return FingerprintHit(
                hit=False, elapsed_ms=_monotonic_ms() - started, attempts=attempts, reason=reason,
            )
        # 定频：每轮起点间隔 = poll_ms（含该轮全部模板的截图 + 匹配耗时），只补足剩余；
        # 该轮耗时已超过 poll_ms 时不再额外 sleep（仅让出事件循环），并记一次超预算。
        remaining_s = interval_s - (time.monotonic() - round_started)
        if remaining_s <= 0:
            over_budget += 1
            logger.debug(
                "[perf.fingerprint_wait] 第 %d 轮耗时超过轮询间隔 %dms（累计超预算 %d 轮）",
                attempts, int(poll_ms), over_budget,
            )
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(remaining_s)
