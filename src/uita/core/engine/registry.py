"""
EngineRegistry — maps (app_name, platform) → EngineBase factory.

Usage:
    # Register (usually done in engine/*/engine.py at import time)
    @register_engine("sample_web", "macOS")
    class MacAppKitEngine(EngineBase): ...

    # Resolve
    engine = EngineRegistry.get("sample_web", "macOS")
"""
from __future__ import annotations

from typing import Callable, Type

from uita.core.registry_common import resolve_universal_platform

from .base import EngineBase

# Type alias for engine factory: callable that returns an EngineBase instance
EngineFactory = Callable[..., EngineBase]

_REGISTRY: dict[tuple[str, str], Type[EngineBase]] = {}


class EngineNotFoundError(Exception):
    pass


class EngineRegistry:
    @staticmethod
    def register(app_name: str, platform: str, engine_cls: Type[EngineBase]) -> None:
        """platform 为 "universal" 时，在注册的这一刻解析为当前宿主机真实平台
        （见 core.registry_common.resolve_universal_platform），_REGISTRY 中永远
        只存具体平台 key，get() 无需感知 universal 概念。"""
        resolved_platform = resolve_universal_platform(platform)
        key = (app_name.lower(), resolved_platform.lower())
        _REGISTRY[key] = engine_cls

    @staticmethod
    def get(app_name: str, platform: str) -> Type[EngineBase]:
        key = (app_name.lower(), platform.lower())
        if key not in _REGISTRY:
            raise EngineNotFoundError(
                f"No engine registered for app='{app_name}' platform='{platform}'. "
                f"Registered: {list(_REGISTRY.keys())}"
            )
        return _REGISTRY[key]

    @staticmethod
    def all_keys() -> list[tuple[str, str]]:
        return list(_REGISTRY.keys())


def register_engine(app_name: str, platform: str):
    """Class decorator for registering an engine implementation."""
    def decorator(cls: Type[EngineBase]) -> Type[EngineBase]:
        EngineRegistry.register(app_name, platform, cls)
        return cls
    return decorator
