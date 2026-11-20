"""LogCollectorBase — per-app 日志采集器抽象基类。

各 App 在 apps/<name>/<name>_log_collector.py 中实现，
在 register.py 中通过 CollectorRegistry.register 注册。

与框架级 Hook 的区别：
  - 框架级 Hook（如 screenshot）：全局生效，通过 @register_hook 注册
  - App 级 Collector（如日志采集）：per-app 注册，通过 CollectorRegistry 定位
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uita.core.hooks.base import HookContext


class LogCollectorBase(ABC):
    """per-app 日志采集器抽象基类。

    子类需实现 collect()，返回产出文件路径列表。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """采集器名称（用于日志标识）。"""
        ...

    @abstractmethod
    async def collect(self, ctx: "HookContext") -> list[str]:
        """采集日志，返回产出文件路径列表。

        Args:
            ctx: Hook 上下文（含 engine / report_dir 等信息）

        Returns:
            产出文件绝对路径列表（如 logs.zip 路径）；无产出时返回空列表
        """
        ...
