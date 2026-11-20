"""工具执行上下文：一次 DSH 会话持有的宿主资源句柄。

设计约束：

- 上下文只持有**句柄**，不负责创建引擎；引擎一律经
  ``AppLifecycleStack.enter()``（内部即 ``prepare_engine_for_case``，R3）获得，
  由 ``app_prepare`` 工具写入本对象。
- 工具通过本对象读写"最近一次观察"，harness 据此构造下一轮模型输入。
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any, Optional


def host_os_name() -> str:
    """按宿主推断 UITA 平台名（``macOS`` / ``Windows`` / ``Linux``）。"""
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform.startswith("win"):
        return "Windows"
    return "Linux"


@dataclass
class ToolContext:
    """会话级工具上下文。"""

    session: Any = None
    app_name: str = ""
    os_name: str = ""
    workdir: str = ""
    engine: Any = None
    prepared: bool = False
    last_observation: dict[str, Any] = field(default_factory=dict)
    _resolver: Any = None
    _resolver_key: tuple[str, str, int] = ("", "", 0)

    def __post_init__(self) -> None:
        if not self.os_name:
            self.os_name = host_os_name()

    # ------------------------------------------------------------------
    # 引擎
    # ------------------------------------------------------------------

    def set_engine(self, engine: Any, app_name: str = "") -> None:
        if app_name:
            self.app_name = app_name
        self.engine = engine
        self.prepared = engine is not None
        self._resolver = None

    def drop_engine(self) -> None:
        self.engine = None
        self.prepared = False
        self._resolver = None
        self.last_observation = {}

    async def cleanup_engine(self) -> None:
        """无头收尾时安全关闭已准备的引擎实例（异常只降级，不抛）。"""
        if not self.prepared and self.engine is None:
            return
        target = (self.app_name or "").strip()
        try:
            if target:
                from uita.core.lifecycle.app_lifecycle_stack import get_default_stack

                await get_default_stack().evict(target, self.os_name)
        except Exception:  # noqa: BLE001 - 收尾异常不覆盖会话结论
            pass
        finally:
            self.drop_engine()

    def require_engine(self) -> Any:
        """取当前引擎；未准备时抛 :class:`DshToolError`（由工具转失败结果）。"""
        from .registry import DshToolError

        if self.engine is None:
            raise DshToolError("引擎未准备：请先调用 app_prepare（生命周期统一入口）")
        return self.engine

    # ------------------------------------------------------------------
    # 定位器（按 (app, os, engine) 缓存，切换引擎自动失效）
    # ------------------------------------------------------------------

    def get_resolver(self, engine: Any = None) -> Any:
        from .registry import DshToolError

        target = engine if engine is not None else self.engine
        if target is None:
            raise DshToolError("引擎未准备：无法构建定位器")
        if not self.app_name:
            raise DshToolError("未声明目标 app：无法加载 rosetta_map")
        key = (self.app_name, self.os_name, id(target))
        if self._resolver is not None and self._resolver_key == key:
            return self._resolver
        from uita.core.runner.resolver_factory import build_resolver_for_app

        self._resolver = build_resolver_for_app(target, self.app_name, self.os_name)
        self._resolver_key = key
        return self._resolver

    # ------------------------------------------------------------------
    # 观察
    # ------------------------------------------------------------------

    def set_observation(self, observation: dict[str, Any]) -> None:
        self.last_observation = dict(observation or {})

    def evidence_dir(self) -> Optional[str]:
        store = getattr(self.session, "store", None)
        if store is None:
            return None
        return store.ensure()
