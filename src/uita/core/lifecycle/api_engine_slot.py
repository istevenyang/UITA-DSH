"""core/lifecycle/api_engine_slot.py — switch[api]/switch_back[] 专用运行
时槛位。

与 AppLifecycleStack（UI app 间切换，core/lifecycle/app_lifecycle_
stack.py）完全独立并行、物理隔离、互不感知：``ApiEngineBase`` 不满足
``EngineBase`` 契约（无 ``launch()``/``restart()`` 等），若强行塞进
``AppLifecycleStack`` 的 ``ensure_alive()``/``recover()`` 逻辑（依赖
``EngineBase.launch()``），一旦触发健康检查失败会抛 ``AttributeError``。
本模块只管理 ``ApiEngineBase`` 实例的构造与释放，不涉及"健康检查/自动
恢复"语义。

见 docs/plans/done/archive_202608.md §2.2。
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from uita.core.api_runner.context import ApiCaseContext
from uita.core.engine.api_auth import ApiAuthProvider
from uita.core.engine.api_base import ApiEngineBase
from uita.core.engine.api_config import ApiRuntimeConfig
from uita.core.engine.api_endpoint_catalog import ApiEndpointCatalog
from uita.core.engine.api_factory import ApiEngineFactoryError, build_api_engine

logger = logging.getLogger("lifecycle.api_slot")

__all__ = ["ApiEngineSlotError", "ApiEngineSlot", "get_default_api_slot", "reset_default_api_slot"]


class ApiEngineSlotError(RuntimeError):
    """switch[api]/switch_back[] 生命周期操作失败（重复激活、构造失败等）。
    调用方（``SwitchAction``）捕获后转译为 ``ActionResult.fail()``。"""


class ApiEngineSlot:
    """switch[api] 专用运行时槛位：进程级单例，同一时刻最多持有一个激活的
    API 引擎会话。"""

    def __init__(self) -> None:
        self._engine: Optional[ApiEngineBase] = None
        self._auth_provider: Optional[ApiAuthProvider] = None
        self._case_context: Optional[ApiCaseContext] = None
        self._catalog: Optional[ApiEndpointCatalog] = None
        self._runtime_config: Optional[ApiRuntimeConfig] = None
        self._app_name: str = ""

    @property
    def is_active(self) -> bool:
        return self._engine is not None

    @property
    def app_name(self) -> str:
        return self._app_name

    @property
    def current(self) -> Optional[ApiEngineBase]:
        return self._engine

    @property
    def catalog(self) -> ApiEndpointCatalog:
        if self._catalog is None:
            raise ApiEngineSlotError("API 模式未激活，无 catalog 可用")
        return self._catalog

    @property
    def runtime_config(self) -> ApiRuntimeConfig:
        if self._runtime_config is None:
            raise ApiEngineSlotError("API 模式未激活，无 runtime_config 可用")
        return self._runtime_config

    @property
    def case_context(self) -> ApiCaseContext:
        if self._case_context is None:
            raise ApiEngineSlotError("API 模式未激活，无 case_context 可用")
        return self._case_context

    async def activate(self, app_name: str, platform: str) -> ApiEngineBase:
        """构造并激活一个新的 API 引擎会话。

        Raises:
            ApiEngineSlotError: 已处于激活状态，或引擎构造失败
                （``ApiEngineFactoryError`` 包装）。
        """
        if self.is_active:
            raise ApiEngineSlotError(
                f"已处于 API 模式（app={self._app_name!r}），"
                f"请先 switch_back[] 切回 UI 引擎"
            )
        try:
            bundle = await build_api_engine(app_name, platform)
        except ApiEngineFactoryError as exc:
            raise ApiEngineSlotError(f"API 引擎构造失败: {exc}") from exc

        self._engine = bundle.engine
        self._auth_provider = bundle.auth_provider
        self._catalog = bundle.catalog
        self._runtime_config = bundle.runtime_config
        self._case_context = ApiCaseContext(uuid.uuid4().hex)
        self._app_name = app_name
        logger.info("[api_slot] 已激活 API 模式 app=%s platform=%s", app_name, platform)
        return self._engine

    async def deactivate(self) -> None:
        """关闭 engine + auth_provider，清空全部字段；未激活时静默返回。"""
        if not self.is_active:
            return
        app_name = self._app_name
        try:
            if self._engine is not None:
                await self._engine.close()
        except Exception as exc:
            logger.warning("[api_slot] engine.close() 异常（忽略）: %s", exc)
        try:
            if self._auth_provider is not None:
                await self._auth_provider.close()
        except Exception as exc:
            logger.warning("[api_slot] auth_provider.close() 异常（忽略）: %s", exc)
        self._engine = None
        self._auth_provider = None
        self._catalog = None
        self._runtime_config = None
        self._case_context = None
        self._app_name = ""
        logger.info("[api_slot] 已退出 API 模式 app=%s", app_name)


_default_slot: Optional[ApiEngineSlot] = None


def get_default_api_slot() -> ApiEngineSlot:
    """进程级单例，与 ``get_default_stack()``（AppLifecycleStack）同构。"""
    global _default_slot
    if _default_slot is None:
        _default_slot = ApiEngineSlot()
    return _default_slot


def reset_default_api_slot() -> None:
    """仅供测试重置单例，避免测试间状态串染。"""
    global _default_slot
    _default_slot = None
