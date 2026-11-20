"""
AppRegistry — stores per-app, per-platform launch configuration.

AppPlatformConfig holds:
  - app_name        : str  (e.g. "sample_web")
  - platform        : str  (e.g. "macOS")
  - launch_strategy : LaunchStrategy  (CDP / native / noop)
  - rosetta_map_path: str  (path to rosetta_map.json)
  - nav_map_path    : str  (path to nav_map.json)
  - extra           : dict (arbitrary extra config)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from uita.core.registry_common import resolve_universal_platform


class LaunchStrategy(Enum):
    CDP = "cdp"           # Connect via Chrome DevTools Protocol (Electron / web)
    NATIVE_MAC = "native_mac"   # atomacos (Mac accessibility)
    NATIVE_WIN = "native_win"   # pywinauto (Windows UIA)
    NOOP = "noop"         # No-op (already running, just attach)


@dataclass
class AppPlatformConfig:
    app_name: str
    platform: str
    launch_strategy: LaunchStrategy = LaunchStrategy.NOOP
    rosetta_map_path: str = ""
    nav_map_path: str = ""
    cdp_url: str = "http://localhost:9222"
    extra: dict = field(default_factory=dict)
    app_version: str = ""   # 当前被测 App 版本（如 "2.1.3"），空串=未声明，走默认 rosetta_map


_REGISTRY: dict[tuple[str, str], AppPlatformConfig] = {}


class AppNotFoundError(Exception):
    pass


class AppRegistry:
    @staticmethod
    def register(config: AppPlatformConfig) -> None:
        """config.platform 为 "universal" 时，在注册的这一刻解析为当前宿主机
        真实平台（见 core.registry_common.resolve_universal_platform），并回写
        config.platform（保证内省时看到真实平台而非哨兵值）；_REGISTRY 中永远
        只存具体平台 key，get() 无需感知 universal 概念。"""
        resolved_platform = resolve_universal_platform(config.platform)
        config.platform = resolved_platform
        key = (config.app_name.lower(), resolved_platform.lower())
        _REGISTRY[key] = config

    @staticmethod
    def get(app_name: str, platform: str) -> AppPlatformConfig:
        key = (app_name.lower(), platform.lower())
        if key not in _REGISTRY:
            raise AppNotFoundError(
                f"No app config registered for app='{app_name}' platform='{platform}'. "
                f"Registered: {list(_REGISTRY.keys())}"
            )
        return _REGISTRY[key]

    @staticmethod
    def all_keys() -> list[tuple[str, str]]:
        return list(_REGISTRY.keys())


def get_extra_config(app_name: str, platform: str, key: str, default: Optional[object] = None):
    """按 (app_name, platform) 查找 AppPlatformConfig.extra 中的某个 key。

    app 未注册（AppNotFoundError）或 key 不存在时，均返回 default，不抛异常。
    供 core/rosetta/base.py 等"app 级配置优先、否则回退调用方传入的默认值"
    场景使用；default 由调用方传入（如全局 settings 兜底值），本函数不内置
    任何业务默认值。

    见 docs/plans/archive/03-resolver-rosetta-实施纪要.md。
    """
    try:
        cfg = AppRegistry.get(app_name, platform)
    except AppNotFoundError:
        return default
    return cfg.extra.get(key, default)
