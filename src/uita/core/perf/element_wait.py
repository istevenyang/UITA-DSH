"""core/perf/element_wait.py — 定频轮询「界面元素出现」（引擎控件树直查，不截图）。

与 :mod:`fingerprint_wait` 的区别：指纹等待每轮都要"截图 + 模板匹配"（80–300ms/轮，且占用 CPU，
会扰动被测页面加载）；本模块每轮只做 ``resolver.resolve_exact(name)``（L1 控件树/DOM 直查），
无截图、无图像匹配、不降级到 L2/L3/视觉/LLM。

语义：
- ``names`` 是候选元素名列表，**任一出现即命中**；一轮内按声明顺序依次查，命中即返回
  （不做该轮剩余候选的查询）；
- 定频：相邻两轮起点间隔 = ``poll_ms``，单轮（含全部候选）耗时计入该周期，超出则下一轮立即开始；
- 命中时刻 ``hit_at_ms`` 取**命中那一刻**的单调毫秒，供 ``render_ms`` 精确计算；
- 返回结构复用 :class:`FingerprintHit`（``template`` = 命中的元素名，``algo="uia"``），
  上层 ``page_probe`` 无需区分来源；
- ``names`` 为空 → 立即 ``reason="no_element"``，不空等；全部轮询抛异常 → ``error``。

core/perf 不 import runner/engine（R1）：resolver 由调用方注入，只按鸭子类型使用。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Sequence

from uita.core.perf.fingerprint_wait import FingerprintHit

logger = logging.getLogger("uita")

__all__ = ["wait_elements"]


def _monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def wait_elements(
    resolver: Any,
    names: Sequence[str],
    *,
    timeout_ms: int = 20000,
    poll_ms: int = 200,
) -> FingerprintHit:
    """轮询等待候选元素之一出现（引擎控件树直查，不截图）。

    Args:
        resolver: 具备 ``resolve_exact(name)`` 的 Resolver（缺该方法时退化为 ``resolve(name)``）。
        names: 候选元素名（rosetta_map 元素），任一出现即命中。
        timeout_ms: 超时上限。
        poll_ms: 定频周期（相邻两轮起点间隔）。

    Returns:
        :class:`FingerprintHit`；``reason`` ∈ ``matched`` / ``timeout`` / ``no_element`` / ``error``。
    """
    candidates = [str(n).strip() for n in (names or []) if str(n or "").strip()]
    if not candidates:
        return FingerprintHit(hit=False, attempts=0, reason="no_element")

    finder = getattr(resolver, "resolve_exact", None) or resolver.resolve
    started = _monotonic_ms()
    interval_s = max(0.02, float(poll_ms) / 1000.0)
    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000.0

    attempts = 0
    errors = 0
    while True:
        attempts += 1
        round_started = time.monotonic()
        for name in candidates:
            try:
                result = await _maybe_await(finder(name))
            except Exception as exc:  # noqa: BLE001 - 单次查询异常继续轮询，超时后如实报告
                errors += 1
                logger.debug("[perf.element_wait] 查询 %r 异常: %s", name, exc)
                continue
            if bool(getattr(result, "found", False)) and getattr(result, "element", None) is not None:
                now = _monotonic_ms()
                hit = FingerprintHit(
                    hit=True,
                    elapsed_ms=now - started,
                    hit_at_ms=now,
                    attempts=attempts,
                    template=name,
                    algo="uia",
                    reason="matched",
                )
                logger.info(
                    "[perf.element_wait] 命中 %r（%d 轮 / %dms）", name, attempts, hit.elapsed_ms,
                )
                return hit

        if time.monotonic() >= deadline:
            # 每次查询都抛异常 → "测不了"（error），不是"页面没出来"（timeout）
            reason = "error" if errors and errors >= attempts * len(candidates) else "timeout"
            logger.info(
                "[perf.element_wait] 未命中 %s：%s（%d 轮 / %dms）",
                candidates, reason, attempts, _monotonic_ms() - started,
            )
            return FingerprintHit(
                hit=False, elapsed_ms=_monotonic_ms() - started, attempts=attempts, reason=reason,
            )

        remaining_s = interval_s - (time.monotonic() - round_started)
        if remaining_s <= 0:
            logger.debug(
                "[perf.element_wait] 第 %d 轮耗时超过周期 %dms（候选 %d 个）",
                attempts, int(poll_ms), len(candidates),
            )
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(remaining_s)
