"""InstallRegistry — (app_name, platform) → InstallConfig 注册中心。

与 AppRegistry / EngineRegistry / CollectorRegistry / LaunchPolicyRegistry
对称的第五件注册中心，供 `--mode install`（core/lifecycle/app_installer.py）
消费：各 App 在 `apps/<name>/register.py` 中注册自己的安装元信息（macOS
覆盖安装目标路径/进程名，Windows exe 名/候选安装目录/env 缓存 key 等），
`core/lifecycle/app_installer.py` 不含任何 app 专属字符串（R2 零外部依赖）。

设计对齐 core/platform/app_registry.py::AppRegistry：
  - register()：platform 为 "universal" 时在注册的这一刻解析为当前宿主机
    真实平台（见 core.registry_common.resolve_universal_platform）。
  - get()：未注册抛 InstallConfigNotFoundError（不静默失败，注册流程
    应显式声明支持的 app，避免 --mode install 对未配置的 app 产生误导性行为）。

见 docs/plans/archive/07-db-data-实施纪要.md。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from uita.core.registry_common import resolve_universal_platform


@dataclass
class InstallConfig:
    """单个 (app_name, platform) 的安装元信息。"""

    app_name: str
    platform: str  # "macOS" | "Windows"（register() 时若为 "universal" 会被解析为具体平台）

    # ── macOS ──
    mac_app_dst_path: str = ""     # 覆盖安装目标路径，如 /Applications/示例桌面应用.app
    mac_process_name: str = ""     # pkill -x 用的进程名（覆盖安装前关闭运行中实例）

    # ── Windows ──
    win_exe_name: str = ""             # 安装完成后用于重新定位真实路径的 exe 文件名
    win_install_roots: List[str] = field(default_factory=list)
    win_shortcut_paths: List[str] = field(default_factory=list)
    win_registry_keywords: List[str] = field(default_factory=list)
    win_env_cache_key: str = ""        # 安装成功后写入 .env.local 的 key
    win_process_name: str = ""         # 覆盖安装前关闭运行中实例的进程名
    win_silent_args: List[str] = field(default_factory=lambda: ["/S"])


class InstallConfigNotFoundError(Exception):
    """未找到指定 (app_name, platform) 的 InstallConfig。"""


_REGISTRY: dict[tuple[str, str], InstallConfig] = {}


class InstallRegistry:
    @staticmethod
    def register(config: InstallConfig) -> None:
        """config.platform 为 "universal" 时，在注册的这一刻解析为当前宿主机
        真实平台，并回写 config.platform；_REGISTRY 中永远只存具体平台 key。"""
        resolved_platform = resolve_universal_platform(config.platform)
        config.platform = resolved_platform
        key = (config.app_name.lower(), resolved_platform.lower())
        _REGISTRY[key] = config

    @staticmethod
    def get(app_name: str, platform: str) -> InstallConfig:
        key = (app_name.lower(), platform.lower())
        if key not in _REGISTRY:
            raise InstallConfigNotFoundError(
                f"No install config registered for app='{app_name}' platform='{platform}'. "
                f"Registered: {list(_REGISTRY.keys())}"
            )
        return _REGISTRY[key]

    @staticmethod
    def try_get(app_name: str, platform: str) -> "InstallConfig | None":
        key = (app_name.lower(), platform.lower())
        return _REGISTRY.get(key)

    @staticmethod
    def all_keys() -> list[tuple[str, str]]:
        return list(_REGISTRY.keys())
