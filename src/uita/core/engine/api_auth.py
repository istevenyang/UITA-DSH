"""API-only App 的认证 Provider 抽象和注册表。

Core 只定义凭据的内存载体与生命周期契约；具体环境变量读取、TC3/JWT/APISIX
协议实现均在 apps/<api_app>/register.py 或具体 engine 中完成。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Type

from uita.core.registry_common import resolve_universal_platform


@dataclass
class ApiAuthContext:
    """仅在内存中保存的认证结果，严禁序列化到结果、日志或异常。"""

    headers: dict[str, str] = field(default_factory=dict)
    credentials: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    # 可选：按接口 path 生成动态请求头的 resolver（如私有化 x-tc-subappid 反查）。
    # 由 App 适配层（Provider）构造注入，通用 HttpEngine 在每次 request() 时调用。
    path_header_resolver: Optional[Callable[[str], dict[str, str]]] = None


class ApiAuthProvider(ABC):
    """API-only App 的认证生命周期。"""

    @abstractmethod
    async def initialize(self) -> ApiAuthContext:
        """从受控环境读取凭据并建立认证上下文；缺失时必须失败关闭。"""
        raise NotImplementedError

    async def refresh_if_needed(self, _response_status: int) -> bool:
        """明确识别认证过期时最多刷新一次；默认禁止刷新。"""
        return False

    async def close(self) -> None:
        """释放进程内认证状态。"""


class ApiAuthProviderNotFoundError(Exception):
    """API-only App 未注册认证 Provider。"""


_REGISTRY: dict[tuple[str, str], Type[ApiAuthProvider]] = {}


class ApiAuthRegistry:
    @staticmethod
    def register(app_name: str, platform: str, provider_cls: Type[ApiAuthProvider]) -> None:
        resolved = resolve_universal_platform(platform)
        _REGISTRY[(app_name.lower(), resolved.lower())] = provider_cls

    @staticmethod
    def get(app_name: str, platform: str) -> Type[ApiAuthProvider]:
        key = (app_name.lower(), platform.lower())
        try:
            return _REGISTRY[key]
        except KeyError as exc:
            raise ApiAuthProviderNotFoundError(
                f"No API auth provider registered for app={app_name!r}, platform={platform!r}."
            ) from exc

    @staticmethod
    def clear() -> None:
        """仅供单元测试重置全局注册状态。"""
        _REGISTRY.clear()


def register_api_auth(app_name: str, platform: str = "universal"):
    """注册 API-only App 的认证 Provider。"""

    def decorator(cls: Type[ApiAuthProvider]) -> Type[ApiAuthProvider]:
        ApiAuthRegistry.register(app_name, platform, cls)
        return cls

    return decorator
