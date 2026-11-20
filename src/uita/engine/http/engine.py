"""HttpEngine — ApiEngineBase 的默认实现，基于 httpx.AsyncClient。

适用场景：任何提供 HTTP/REST 接口的被测服务（含 SampleAgent 私有化 JWT 会话接口）。

职责边界（对齐总架 §2.11.4）：
  - 通用 HTTP transport：请求收发、响应解析、连接关闭；
  - 认证头注入：消费 ApiAuthContext（静态 headers + 可选 per-path resolver 动态头）；
  - 认证材料/会话/签名等适配逻辑由 App 侧 ApiAuthProvider 提供，本引擎不感知具体协议。

构造参数兼容两种用法：
  1. 简单用法：HttpEngine(app_name="x", base_url="https://host")
  2. api_runner 标准用法：HttpEngine(app_name, endpoint, region, auth_context, allowed_hosts)
     —— endpoint 可为 host 或 host:port；verify 从 auth_context.metadata["verify_ssl"] 读（默认 True）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from uita.core.engine.api_auth import ApiAuthContext
from uita.core.engine.api_base import ApiDownloadArtifact, ApiEngineBase, ApiResponse

logger = logging.getLogger(__name__)


class HttpEngine(ApiEngineBase):
    """默认 API 引擎实现，基于 httpx.AsyncClient。"""

    def __init__(
        self,
        app_name: str,
        base_url: str = "",
        endpoint: str = "",
        region: str = "",
        auth_context: Optional[ApiAuthContext] = None,
        allowed_hosts: tuple[str, ...] = (),
    ) -> None:
        self._app_name = app_name
        self._base_url = (base_url or self._normalize_base_url(endpoint)).rstrip("/")
        self._auth_context = auth_context
        self._allowed_hosts = {host.lower() for host in allowed_hosts}
        self._verify = True
        if auth_context is not None and auth_context.metadata:
            self._verify = str(auth_context.metadata.get("verify_ssl", "1")).lower() not in ("0", "false", "no")
        self._client = httpx.AsyncClient(base_url=self._base_url, verify=self._verify, timeout=30.0)

    @staticmethod
    def _normalize_base_url(endpoint: str) -> str:
        value = (endpoint or "").strip()
        if not value:
            return ""
        parsed = urlparse(value if "://" in value else f"https://{value}")
        if parsed.scheme != "https" or not parsed.hostname or parsed.path not in ("", "/") or parsed.query:
            raise ValueError("API endpoint must be an HTTPS host without path or query")
        host = parsed.hostname
        port_suffix = f":{parsed.port}" if parsed.port else ""
        return f"https://{host}{port_suffix}"

    @property
    def app_name(self) -> str:
        return self._app_name

    def _check_allowlist(self, target: str) -> None:
        if not self._allowed_hosts:
            return
        parsed = urlparse(target)
        hostname = parsed.hostname or ""
        port_suffix = f":{parsed.port}" if parsed.port else ""
        if hostname.lower() not in self._allowed_hosts and f"{hostname}{port_suffix}".lower() not in self._allowed_hosts:
            raise ValueError("Request target is not in the configured allowlist")

    async def request(
        self,
        method: str,
        url: str,
        headers: Optional[dict] = None,
        body: Optional[str] = None,
        timeout: int = 30,
    ) -> ApiResponse:
        # 组装认证头：静态 auth_context.headers + per-path 动态 resolver + 调用方显式 headers
        merged_headers: dict[str, str] = {}
        if self._auth_context is not None:
            merged_headers.update(self._auth_context.headers or {})
            resolver = self._auth_context.path_header_resolver
            if resolver is not None:
                merged_headers.update(resolver(url) or {})
        merged_headers.update(headers or {})
        if body is not None:
            merged_headers.setdefault("Content-Type", "application/json")

        target = self._base_url + url if url.startswith("/") else url
        self._check_allowlist(target)

        # httpx INFO 会输出完整请求 URL（含内网 host:port），抑制日志
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)

        start = time.monotonic()
        resp = await self._client.request(
            method,
            url,
            headers=merged_headers,
            content=body,
            timeout=float(timeout),
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        try:
            json_body = resp.json()
        except ValueError:
            json_body = None
        request_id = ""
        response_headers = dict(resp.headers)
        for key in ("x-request-id", "X-Request-Id", "requestid"):
            if key in response_headers:
                request_id = str(response_headers[key])
                break
        return ApiResponse(
            status_code=resp.status_code,
            headers=response_headers,
            body=resp.text,
            json=json_body,
            elapsed_ms=elapsed_ms,
        )

    async def is_healthy(self) -> bool:
        try:
            resp = await self._client.get(self._base_url or "/", timeout=5)
            return resp.status_code < 500
        except httpx.HTTPError:
            logger.warning("HttpEngine.is_healthy() failed for app=%s base_url=%s", self._app_name, self._base_url)
            return False

    async def download_export(
        self,
        url: str,
        artifact_dir: str,
        filename: str,
        allowed_hosts: tuple[str, ...],
        max_bytes: int,
        timeout: int,
    ) -> ApiDownloadArtifact:
        """受控导出文件下载（私有化：相对路径 + 会话头）。

        私有化导出返回相对路径（如 /capi/FileStorage/CreateDownload/xxx.csv?signature=...），
        需拼 base_url + JWT 会话头下载。安全约束对齐 SaaS：
          - 仅同 host（内网 allowlist），禁止外部 URL；
          - 文件名安全校验、路径逃逸防护、大小限制、受限 Content-Type；
          - 只写入 artifact_dir，失败清理现场。
        """
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\.csv", filename):
            raise ValueError("Export artifact filename is invalid")
        if max_bytes <= 0 or timeout <= 0:
            raise ValueError("Export download limits are invalid")

        # 仅接受相对路径；拼 base_url 后目标必须仍在 allowlist 内（同 host）
        if not url.startswith("/"):
            raise ValueError("Private export download URL must be a relative path")
        target = self._base_url + url
        self._check_allowlist(target)

        artifact_root = Path(artifact_dir).resolve()
        artifact_root.mkdir(parents=True, exist_ok=True)
        destination = (artifact_root / filename).resolve()
        if destination.parent != artifact_root:
            raise ValueError("Export artifact path escapes its artifact directory")

        # 组装认证头（JWT 会话 + 动态 subappid resolver）
        headers: dict[str, str] = {}
        if self._auth_context is not None:
            headers.update(self._auth_context.headers or {})
            resolver = self._auth_context.path_header_resolver
            if resolver is not None:
                headers.update(resolver(url) or {})

        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=float(timeout), verify=self._verify) as client:
                async with client.stream("GET", target, headers=headers) as response:
                    if response.is_redirect:
                        raise ValueError("Export download redirect is not allowed")
                    if response.status_code != 200:
                        raise ValueError("Export download returned a non-success status")
                    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    # text/plain 为私有化导出实际返回类型（内容为 CSV，含 BOM，实测软件/报表导出）
                    if content_type not in {"text/csv", "application/csv", "application/octet-stream", "text/plain"}:
                        raise ValueError("Export download content type is not allowed")
                    content_length = response.headers.get("content-length")
                    if content_length and (not content_length.isdigit() or int(content_length) > max_bytes):
                        raise ValueError("Export download exceeds the configured size limit")
                    digest = hashlib.sha256()
                    written = 0
                    with destination.open("xb") as handle:
                        async for chunk in response.aiter_bytes():
                            written += len(chunk)
                            if written > max_bytes:
                                raise ValueError("Export download exceeds the configured size limit")
                            digest.update(chunk)
                            handle.write(chunk)
        except FileExistsError as exc:
            raise ValueError("Export artifact filename already exists") from exc
        except httpx.HTTPError as exc:
            destination.unlink(missing_ok=True)
            raise ValueError("Export download request failed") from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return ApiDownloadArtifact(
            path=str(destination),
            relative_path=f"artifacts/{filename}",
            byte_size=written,
            content_type=content_type,
            sha256=digest.hexdigest(),
        )

    async def close(self) -> None:
        await self._client.aclose()
        if self._auth_context is not None:
            self._auth_context.credentials.clear()
            self._auth_context.headers.clear()
