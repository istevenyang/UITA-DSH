"""perf 场景失败后的**补救机制**（core 只提供机制，不 import engine/apps，守 R1）。

背景（2026-10-13 真机）：5K/10K/50K 共 **7 轮** 在 ``search`` 起连续 ``TargetClosedError``
（目标页/浏览器已关闭），于是"检索 / 筛选 / 帧率"三块关键指标整档留空。用户口径：
**"不管怎么样把数据采集全，别动不动留空"**。故本模块给出两级补救原语：

  ① :func:`heal_engine_page` —— 让引擎重新指向一个可用页面（引擎页被销毁后重建、
     而 ``connectOverCDP`` 不跟踪新窗口时，引擎的 ``self._page`` 会一直指向已关闭的旧页；
     自愈能力由引擎自带，按鸭子类型调用，core 不硬依赖引擎实现）；
  ② 重新拉起应用 —— 需要 ``core/lifecycle``，由调用方（runner）执行，
     本模块只负责"判断该不该救"。

"该不该救"由 :func:`is_transient_error` 判定：只有**瞬态**的"目标/页面失效"才值得重试，
业务型失败（元素找不到、断言不成立）重试只会浪费时间并掩盖真问题。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger("uita")

__all__ = [
    "TRANSIENT_PATTERNS",
    "heal_engine_page",
    "is_transient_error",
    "scenario_error_text",
]

#: 瞬态"目标/页面失效"特征（全部小写匹配；Playwright/CDP 两套措辞都覆盖）
TRANSIENT_PATTERNS: tuple[str, ...] = (
    "targetclosederror",
    "target page, context or browser has been closed",
    "target closed",
    "page closed",
    "context closed",
    "browser has been closed",
    "session closed",
    "execution context was destroyed",
    "cannot find context with specified id",
)

#: 引擎自愈能力（按序尝试；名字对齐 ``engine/playwright/engine.py``）
_HEAL_METHODS: tuple[str, ...] = ("ensure_page_alive", "refresh_active_page")


def is_transient_error(text: Any) -> bool:
    """该错误文本是否属于"目标/页面失效"这类**可补救**的瞬态失败。"""
    low = str(text or "").lower()
    if not low:
        return False
    return any(pattern in low for pattern in TRANSIENT_PATTERNS)


def scenario_error_text(result: Any) -> str:
    """从场景结果里取"失败原因"文本（非 dict / 无 error 时返回空串）。

    只认 ``error``：``unsupported`` 是**合法**结论（引擎能力不足/读数不可采信），
    不是失败，不该触发补救重试（重试还会把"确实不可测"变成"反复重试"）。
    """
    if not isinstance(result, dict):
        return str(result or "")
    return str(result.get("error") or "").strip()


async def heal_engine_page(
    engine: Any,
    *,
    verify: Optional[Callable[[], Any]] = None,
    log_prefix: str = "perf",
) -> tuple[bool, str]:
    """按鸭子类型调用引擎自愈能力，让引擎重新指向一个可用页面。

    Args:
        engine: 引擎对象（可为 ``None``）；只做 ``getattr`` 能力探测，零硬依赖。
        verify: 自愈后的确认回调（通常 ``lambda: bridge.evaluate("location.href")``）。
            ``None`` = 不校验（调用方自己会重试场景，重试成功即证明自愈有效）。
        log_prefix: 日志前缀（各调用方场景名不同）。

    Returns:
        ``(是否恢复, 细节)``；细节 = 生效的方法名或 verify 的返回值（截断 200 字符）。
    """
    if engine is None:
        return False, ""
    for name in _HEAL_METHODS:
        fn = getattr(engine, name, None)
        if not callable(fn):
            continue
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001 - 自愈失败只是"没救回来"，不向上抛
            logger.debug("[%s] 引擎页自愈 %s() 失败: %s", log_prefix, name, exc)
            continue
        if verify is None:
            return True, name
        try:
            value = await verify()
        except Exception as exc:  # noqa: BLE001 - 校验失败 → 换下一个自愈方法
            logger.debug("[%s] 引擎页自愈 %s() 后校验失败: %s", log_prefix, name, exc)
            continue
        text = "" if value is None else str(value)
        if not text.strip():
            continue
        logger.info("[%s] 引擎当前页已失效，%s() 自愈成功（%s）", log_prefix, name, text[:120])
        return True, text[:200]
    return False, ""
