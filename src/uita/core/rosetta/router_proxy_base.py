"""RouterProxyStoreBase — router_proxy 原始数据存储与远端数据库抽象接口。

设计原则：
  - 默认使用 `LocalJsonRouterProxyStore` 读取本地 `apps/<app>/router_proxy.json`。
  - 仅提供远端数据库存储抽象基类 `RemoteDbRouterProxyStoreBase` 与装配钩子，不内置具体数据库驱动实现。
  - 外部可通过 `register_router_proxy_store()` 或环境变量 `UITA_ROUTER_PROXY_STORE_CLASS="pkg.mod:ClassName"`
    （配合 `UITA_USE_LOCAL_ROSETTA=0` 与 `UITA_REMOTE_DB_URL`）注入自定义远端数据库实现；
    远端不可用或未配置时自动降级回退至本地 JSON。
"""
from __future__ import annotations

import importlib
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Callable, Optional, Type, Union

from uita.core.rosetta.base import _app_name_from_path

logger = logging.getLogger(__name__)


class RouterProxyStoreBase(ABC):
    """router_proxy.json 原始数据读取与路由回写抽象接口（未经解析的原始 dict）。"""

    @abstractmethod
    def load_raw(self, path: str) -> dict:
        """根据路径（或由路径推断的 app_name）返回原始 router_proxy 字典。"""
        ...

    def apply_heal_routes(
        self,
        app_name: str,
        os_name: str,
        routes: dict[str, str],
        reset_ids: Optional[set[str]] = None,
    ) -> int:
        """可选扩展：将运行期自愈产生的动态路由策略回写至存储层。"""
        return 0


class LocalJsonRouterProxyStore(RouterProxyStoreBase):
    """本地 JSON 文件读取实现（默认启用）。"""

    def load_raw(self, path: str) -> dict:
        if not os.path.exists(path):
            return {}
        with open(path, encoding="utf-8") as f:
            return json.load(f)


class RemoteDbRouterProxyStoreBase(RouterProxyStoreBase, ABC):
    """基于远端数据库的 RouterProxy 存储抽象基类（仅定义接口契约，不含具体数据库实现）。

    使用方继承本类并实现 `fetch_from_db(app_name)`（以及可选的 `upsert_routes_to_db`），
    即可对接 MySQL / PostgreSQL / 配置中心等外部存储。
    """

    def __init__(self, db_url: Optional[str] = None) -> None:
        from uita.config import UITA_REMOTE_DB_URL

        self.db_url: str = (db_url if db_url is not None else UITA_REMOTE_DB_URL).strip()

    @abstractmethod
    def fetch_from_db(self, app_name: str) -> Optional[dict]:
        """从远端数据库查询指定 app_name 的 router_proxy 原始结构。

        返回 None 或抛出异常时，调用方将自动回退至本地 JSON。
        """
        ...

    def upsert_routes_to_db(
        self,
        app_name: str,
        os_name: str,
        routes: dict[str, str],
        reset_ids: Optional[set[str]] = None,
    ) -> int:
        """可选实现：将自愈路由策略回写至远端数据库。"""
        return 0

    def load_raw(self, path: str) -> dict:
        app_name = _app_name_from_path(path)
        data = self.fetch_from_db(app_name)
        if data is None or not isinstance(data, dict):
            raise LookupError(f"远端数据库未返回 app={app_name!r} 的有效 router_proxy 数据")
        return data

    def apply_heal_routes(
        self,
        app_name: str,
        os_name: str,
        routes: dict[str, str],
        reset_ids: Optional[set[str]] = None,
    ) -> int:
        return self.upsert_routes_to_db(app_name, os_name, routes, reset_ids=reset_ids)


# ── 自定义远端 Store 注册与动态加载 ──────────────────────────────────────────
_CUSTOM_ROUTER_PROXY_STORE_FACTORY: Optional[Callable[[], RouterProxyStoreBase]] = None


def register_router_proxy_store(
    store_or_cls: Optional[
        Union[RouterProxyStoreBase, Type[RouterProxyStoreBase], Callable[[], RouterProxyStoreBase]]
    ],
) -> None:
    """注册自定义远端 RouterProxyStore 实例、子类或工厂函数（传 None 清空注册）。"""
    global _CUSTOM_ROUTER_PROXY_STORE_FACTORY
    if store_or_cls is None:
        _CUSTOM_ROUTER_PROXY_STORE_FACTORY = None
    elif isinstance(store_or_cls, RouterProxyStoreBase):
        _CUSTOM_ROUTER_PROXY_STORE_FACTORY = lambda: store_or_cls
    elif isinstance(store_or_cls, type) and issubclass(store_or_cls, RouterProxyStoreBase):
        _CUSTOM_ROUTER_PROXY_STORE_FACTORY = store_or_cls
    elif callable(store_or_cls):
        _CUSTOM_ROUTER_PROXY_STORE_FACTORY = store_or_cls
    else:
        raise TypeError(f"不支持的 RouterProxyStore 注册类型: {type(store_or_cls)!r}")


def _load_router_store_from_spec(spec: str) -> Optional[RouterProxyStoreBase]:
    """按 'module:ClassName' 或 'module.ClassName' 动态加载 RouterProxyStoreBase 实例。"""
    sep = ":" if ":" in spec else "."
    if sep not in spec:
        raise ValueError(
            f"无效的 UITA_ROUTER_PROXY_STORE_CLASS 格式: {spec!r}（期望 'pkg.mod:ClassName'）"
        )
    mod_name, attr_name = spec.rsplit(sep, 1)
    mod = importlib.import_module(mod_name)
    cls_or_fn = getattr(mod, attr_name)
    instance = cls_or_fn() if callable(cls_or_fn) else cls_or_fn
    if not isinstance(instance, RouterProxyStoreBase):
        raise TypeError(f"{spec!r} 未返回 RouterProxyStoreBase 实例")
    return instance


def resolve_remote_router_proxy_store() -> Optional[RouterProxyStoreBase]:
    """解析当前配置的远端 RouterProxyStore（未启用或未配置时返回 None）。"""
    use_local = os.environ.get("UITA_USE_LOCAL_ROSETTA", "1").strip() == "1"
    if use_local and _CUSTOM_ROUTER_PROXY_STORE_FACTORY is None:
        return None
    if _CUSTOM_ROUTER_PROXY_STORE_FACTORY is not None:
        return _CUSTOM_ROUTER_PROXY_STORE_FACTORY()
    spec = os.environ.get("UITA_ROUTER_PROXY_STORE_CLASS", "").strip()
    if not spec:
        return None
    return _load_router_store_from_spec(spec)


class DbRouterProxyStore(RemoteDbRouterProxyStoreBase):
    """远端数据库 RouterProxyStore 占位基类（未注入实现时抛 NotImplementedError）。"""

    def fetch_from_db(self, app_name: str) -> Optional[dict]:
        raise NotImplementedError(
            "DbRouterProxyStore 仅提供接口契约，请继承 RemoteDbRouterProxyStoreBase 并通过 "
            "register_router_proxy_store() 或环境变量 UITA_ROUTER_PROXY_STORE_CLASS 注入具体数据库实现"
        )


class DbFallbackRouterProxyStore(RouterProxyStoreBase):
    """远端数据库优先、本地 JSON 自动兜底的组合存储。"""

    def __init__(
        self,
        remote_store: Optional[RouterProxyStoreBase] = None,
        local_store: Optional[RouterProxyStoreBase] = None,
    ) -> None:
        self._remote_store = remote_store
        self._local_store = local_store or LocalJsonRouterProxyStore()

    def load_raw(self, path: str) -> dict:
        remote = self._remote_store
        if remote is None:
            try:
                remote = resolve_remote_router_proxy_store()
            except Exception as exc:
                logger.warning(
                    "[RouterProxyStore] 加载远端 Store 配置失败，回退本地 JSON: %s", exc
                )
                remote = None

        if remote is not None:
            try:
                return remote.load_raw(path)
            except Exception as exc:
                logger.warning(
                    "[RouterProxyStore] 远端读取失败（path=%s），自动回退本地 JSON: %s",
                    path, exc,
                )
        return self._local_store.load_raw(path)
