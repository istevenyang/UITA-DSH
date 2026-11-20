"""场景测量与引擎之间的**鸭子类型**桥接。

为什么需要桥接（而不是直接调用引擎）：
    R1 规定 ``core/perf/`` 不得 import ``engine/``。但场景 3（滑动帧率）、场景 4–12
    （目录/缩略图/预览/搜索/筛选/视频）都需要在渲染进程里执行 JS / 轮询 DOM，
    这是 ``PlaywrightEngine`` 的**引擎专用能力**（``engine.evaluate`` /
    ``engine.evaluate_on_target``，见 ``engine/playwright/engine.py:1470/1476``）。

    因此这里只做「按名取能力」的鸭子类型适配：引擎有 ``evaluate`` 就用，没有
    （原生引擎 WinUIA/MacAppKit，或 dry-run 的假引擎）就如实报告不可用，
    由调用方决定降级（记 ``unsupported`` 而不是伪造 0）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("uita")

__all__ = ["ScenarioBridge"]


class ScenarioBridge:
    """引擎 JS 能力适配器（不 import 任何 engine 模块）。"""

    def __init__(self, engine: Any = None) -> None:
        self._engine = engine

    @property
    def engine(self) -> Any:
        """底层引擎（只读暴露：场景需要读取 `app_process.cdp_port` 等公开成员）。"""
        return self._engine

    @property
    def available(self) -> bool:
        return callable(getattr(self._engine, "evaluate", None))

    @property
    def engine_name(self) -> str:
        return type(self._engine).__name__ if self._engine is not None else ""

    async def evaluate(self, js: str, arg: Any = None) -> Any:
        """在当前页面执行 JS；不可用/失败时抛 ``ScenarioUnsupported``/``RuntimeError``。"""
        if not self.available:
            raise ScenarioUnsupported(
                f"引擎 {self.engine_name or '(none)'} 无 JS 求值能力（evaluate），该场景跳过",
            )
        if arg is None:
            return await self._engine.evaluate(js)
        return await self._engine.evaluate(js, arg)

    async def evaluate_on_target(self, title_substr: str, js: str) -> Optional[str]:
        """在指定标题的独立窗口（如 media-viewer）上执行 JS。

        不可用/失败时返回 ``"ERROR: ..."`` 文本（与引擎同约定，便于统一解析）；
        引擎完全不支持时返回 ``None``。
        """
        fn = getattr(self._engine, "evaluate_on_target", None)
        if not callable(fn):
            return None
        try:
            return await fn(title_substr, js)
        except Exception as exc:  # noqa: BLE001 - 跨窗口失败不应中断整轮
            logger.debug("[perf] evaluate_on_target(%s) 失败: %s", title_substr, exc)
            return f"ERROR: evaluate_on_target 失败: {exc}"

    async def evaluate_on_target_url(self, url_substr: str, js: str) -> Optional[str]:
        """在 **URL 含指定子串** 的独立窗口上执行 JS（查看器窗口标题为空时唯一可靠方式）。

        返回约定同 :meth:`evaluate_on_target`。
        """
        fn = getattr(self._engine, "evaluate_on_target_url", None)
        if not callable(fn):
            return None
        try:
            return await fn(url_substr, js)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[perf] evaluate_on_target_url(%s) 失败: %s", url_substr, exc)
            return f"ERROR: evaluate_on_target_url 失败: {exc}"

    @property
    def supports_url_target(self) -> bool:
        """引擎是否支持按 URL 跨窗口求值（查看器标题为空时必须）。"""
        return callable(getattr(self._engine, "evaluate_on_target_url", None))


class ScenarioUnsupported(RuntimeError):
    """场景在当前引擎/环境下不可测（不是失败，应记为 unsupported）。"""
