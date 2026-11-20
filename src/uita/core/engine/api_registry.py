"""ApiEngineRegistry — (app_name, platform) -> ApiEngineBase 子类 的注册查找表。

与 core/engine/registry.py 的 EngineRegistry（UI 引擎注册表）完全对称、彼此独立：
两套注册表并存不互相干扰，不改动 EngineRegistry 任何现有代码（R5 边界）。

本期（docs/plans/archive/01-engine-lifecycle-实施纪要.md 第一期）只落地注册/查找机制
本身，不在任何 apps/*/register.py 中调用 register_api_engine——具体 app 何时接入
由后续分期决定。

Usage:
    @register_api_engine("sample-api", "universal")
    class SampleApiEngine(HttpEngine): ...

    engine_cls = ApiEngineRegistry.get("sample-api", "macos")
"""
from __future__ import annotations

from typing import Type

from uita.core.registry_common import resolve_universal_platform

from .api_base import ApiEngineBase

_REGISTRY: dict[tuple[str, str], Type[ApiEngineBase]] = {}


class ApiEngineNotFoundError(Exception):
    pass


class ApiEngineRegistry:
    @staticmethod
    def register(app_name: str, platform: str, engine_cls: Type[ApiEngineBase]) -> None:
        """platform 为 "universal" 时，在注册的这一刻解析为当前宿主机真实平台
        （见 core.registry_common.resolve_universal_platform），_REGISTRY 中永远
        只存具体平台 key，get() 无需感知 universal 概念。与 EngineRegistry.register
        的行为完全对称。
        """
        resolved_platform = resolve_universal_platform(platform)
        key = (app_name.lower(), resolved_platform.lower())
        _REGISTRY[key] = engine_cls

    @staticmethod
    def get(app_name: str, platform: str) -> Type[ApiEngineBase]:
        key = (app_name.lower(), platform.lower())
        if key not in _REGISTRY:
            raise ApiEngineNotFoundError(
                f"No API engine registered for app='{app_name}' platform='{platform}'. "
                f"Registered: {list(_REGISTRY.keys())}"
            )
        return _REGISTRY[key]

    @staticmethod
    def all_keys() -> list[tuple[str, str]]:
        return list(_REGISTRY.keys())

    @staticmethod
    def clear() -> None:
        """仅供测试用例重置注册表，避免用例间状态泄漏。"""
        _REGISTRY.clear()


def register_api_engine(app_name: str, platform: str = "universal"):
    """Class decorator for registering an ApiEngineBase implementation."""

    def decorator(cls: Type[ApiEngineBase]) -> Type[ApiEngineBase]:
        ApiEngineRegistry.register(app_name, platform, cls)
        return cls

    return decorator
