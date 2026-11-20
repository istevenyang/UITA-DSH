"""API-only App 的非敏感运行配置注册表。

API-only App 不注册 UI AppRegistry，不依赖 UI Engine/Resolver。此模块只保存
App 已声明的环境变量名称、Host allowlist 和运行策略，绝不保存或读取凭据值。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from uita.core.registry_common import resolve_universal_platform


@dataclass(frozen=True)
class ApiRuntimeConfig:
    """API-only App 在 Runner 初始化前注册的非敏感配置。"""

    app_name: str
    platform: str = "universal"
    target: str = ""
    endpoint_env: str = ""
    region_env: str = ""
    credential_envs: tuple[str, ...] = ()
    host_allowlist: tuple[str, ...] = ()
    download_host_allowlist: tuple[str, ...] = ()
    timeout_s: float = 15.0
    download_timeout_s: float = 30.0
    max_download_bytes: int = 5 * 1024 * 1024
    max_concurrency: int = 1
    auth_profile: str = ""
    redacted_fields: frozenset[str] = frozenset(
        {"authorization", "cookie", "set-cookie", "token", "secret", "password", "secretid", "secretkey"}
    )
    retry_policy: Mapping[str, object] = field(default_factory=dict)


class ApiRuntimeConfigNotFoundError(Exception):
    """API-only App 未注册运行配置。"""


_REGISTRY: dict[tuple[str, str], ApiRuntimeConfig] = {}


class ApiRuntimeRegistry:
    """(app, platform) -> ApiRuntimeConfig。"""

    @staticmethod
    def register(config: ApiRuntimeConfig) -> None:
        platform = resolve_universal_platform(config.platform)
        normalized = ApiRuntimeConfig(
            app_name=config.app_name,
            platform=platform,
            target=config.target or config.app_name,
            endpoint_env=config.endpoint_env,
            region_env=config.region_env,
            credential_envs=tuple(config.credential_envs),
            host_allowlist=tuple(host.lower() for host in config.host_allowlist),
            download_host_allowlist=tuple(host.lower() for host in config.download_host_allowlist),
            timeout_s=float(config.timeout_s),
            download_timeout_s=float(config.download_timeout_s),
            max_download_bytes=int(config.max_download_bytes),
            max_concurrency=int(config.max_concurrency),
            auth_profile=config.auth_profile,
            redacted_fields=frozenset(field.lower() for field in config.redacted_fields),
            retry_policy=dict(config.retry_policy),
        )
        _REGISTRY[(normalized.app_name.lower(), platform.lower())] = normalized

    @staticmethod
    def get(app_name: str, platform: str) -> ApiRuntimeConfig:
        key = (app_name.lower(), platform.lower())
        try:
            return _REGISTRY[key]
        except KeyError as exc:
            raise ApiRuntimeConfigNotFoundError(
                f"No API runtime configuration registered for app={app_name!r}, platform={platform!r}."
            ) from exc

    @staticmethod
    def clear() -> None:
        """仅供单元测试重置全局注册状态。"""
        _REGISTRY.clear()

    @staticmethod
    def all_keys() -> list[tuple[str, str]]:
        return list(_REGISTRY)
