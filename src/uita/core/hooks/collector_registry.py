"""CollectorRegistry — per-app 日志采集器注册中心。

与 EngineRegistry / AppRegistry 对称：
  - AppRegistry:      (app_name, platform) → AppPlatformConfig
  - EngineRegistry:   (app_name, platform) → EngineBase
  - CollectorRegistry: (app_name, platform) → LogCollectorBase

各 App 在 apps/<name>/register.py 中注册自己的 LogCollector，
框架在 batch_end 阶段根据当前用例的 app_name + platform 查找并执行。
"""
from __future__ import annotations

from typing import Type

from uita.core.hooks.collector_base import LogCollectorBase
from uita.core.registry_common import resolve_universal_platform

_REGISTRY: dict[tuple[str, str], Type[LogCollectorBase]] = {}


class CollectorNotFoundError(Exception):
    pass


class CollectorRegistry:
    """(app_name, platform) → LogCollectorBase 注册中心。"""

    @staticmethod
    def register(app_name: str, platform: str, collector_cls: Type[LogCollectorBase]) -> None:
        """platform 为 "universal" 时，在注册的这一刻解析为当前宿主机真实平台
        （见 core.registry_common.resolve_universal_platform），_REGISTRY 中永远
        只存具体平台 key，get()/try_get() 无需感知 universal 概念。"""
        resolved_platform = resolve_universal_platform(platform)
        key = (app_name.lower(), resolved_platform.lower())
        _REGISTRY[key] = collector_cls

    @staticmethod
    def get(app_name: str, platform: str) -> Type[LogCollectorBase]:
        key = (app_name.lower(), platform.lower())
        if key not in _REGISTRY:
            raise CollectorNotFoundError(
                f"No log collector registered for app='{app_name}' platform='{platform}'. "
                f"Registered: {list(_REGISTRY.keys())}"
            )
        return _REGISTRY[key]

    @staticmethod
    def try_get(app_name: str, platform: str) -> Type[LogCollectorBase] | None:
        """查找采集器，未注册时返回 None（不抛异常）。"""
        key = (app_name.lower(), platform.lower())
        return _REGISTRY.get(key)

    @staticmethod
    def all_keys() -> list[tuple[str, str]]:
        return list(_REGISTRY.keys())


def register_collector(app_name: str, platform: str):
    """装饰器：注册 per-app 日志采集器。

    用法::

        @register_collector("sample_web", "macOS")
        class SampleLogCollector(LogCollectorBase): ...
    """
    def decorator(cls: Type[LogCollectorBase]) -> Type[LogCollectorBase]:
        CollectorRegistry.register(app_name, platform, cls)
        return cls
    return decorator
