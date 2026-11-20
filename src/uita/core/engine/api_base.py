"""ApiEngineBase — API 引擎抽象契约。

与 core/engine/base.py 的 EngineBase（UI 引擎）完全平行、互不继承：两套引擎体系
物理隔离，本文件不 import EngineBase，不影响 EngineBase 现有 18 个方法契约（R9）。

本期（docs/plans/archive/01-engine-lifecycle-实施纪要.md 第一期）只落地最基础的
「发起请求 / 探活 / 关闭」三类能力，不含鉴权、重试、会话管理——这些属于"上层适配"
范畴，留给后续分期的 ApiAuthProvider 方案。

零外部依赖，仅标准库（R2：core/ 不得 import engine/、apps/、cli/、tools/）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class ApiResponse:
    """HTTP 响应的统一封装，供各 ApiEngineBase 实现返回。"""

    status_code: int
    headers: dict
    body: str
    json: Optional[dict] = None
    elapsed_ms: float = 0.0


@dataclass(frozen=True)
class ApiDownloadArtifact:
    """受控导出文件下载结果；不保存或暴露源 URL。"""

    path: str
    relative_path: str
    byte_size: int
    content_type: str
    sha256: str


class ApiEngineBase(ABC):
    """API 引擎抽象基类。

    子类须实现全部 4 个抽象方法，不得静默失败（对齐 R10 的精神：占位实现必须
    显式 raise NotImplementedError，不得留空 pass）。
    """

    @property
    @abstractmethod
    def app_name(self) -> str:
        """该引擎所属的 app 名称（如 "sample-api"）。"""
        raise NotImplementedError

    @abstractmethod
    async def request(
        self,
        method: str,
        url: str,
        headers: Optional[dict] = None,
        body: Optional[str] = None,
        timeout: int = 30,
    ) -> ApiResponse:
        """发起一次 HTTP 请求，返回统一封装的 ApiResponse。"""
        raise NotImplementedError

    async def download_export(
        self,
        url: str,
        artifact_dir: str,
        filename: str,
        allowed_hosts: tuple[str, ...],
        max_bytes: int,
        timeout: int,
    ) -> ApiDownloadArtifact:
        """下载经审核的导出文件。

        可选扩展能力；不支持的引擎必须显式失败，不得静默降级。
        """
        raise NotImplementedError(f"{type(self).__name__} does not support export download")

    @abstractmethod
    async def is_healthy(self) -> bool:
        """探测被测服务是否可用。"""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """释放底层连接资源（连接池/会话等）。"""
        raise NotImplementedError
