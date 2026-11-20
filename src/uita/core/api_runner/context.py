"""API-only CaseContext 与脱敏 ApiResultView。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from uita.core.api_runner.value_path import resolve_api_value_path
from uita.core.engine.api_base import ApiResponse
from uita.core.engine.api_endpoint_catalog import ApiExportSpec

_RE_EXACT_VARIABLE = re.compile(r"^\$\{(.+)\}$")
_RE_VARIABLE = re.compile(r"\$\{([^}]+)\}")


@dataclass(frozen=True)
class ApiResultView:
    status: int
    headers: Mapping[str, str]
    body: Any
    text: str
    elapsed_ms: float
    request_id: str | None = None

    @classmethod
    def from_response(
        cls,
        response: ApiResponse,
        redacted_fields: set[str] | frozenset[str],
        text_limit: int = 4096,
    ) -> "ApiResultView":
        body = _redact_value(response.json if response.json is not None else response.body, redacted_fields)
        headers = {
            key: "***" if _is_sensitive_field(key, redacted_fields) else str(value)
            for key, value in response.headers.items()
        }
        request_id = headers.get("x-request-id") or headers.get("X-Request-Id")
        try:
            import json
            text_source = json.loads(response.body)
        except (TypeError, ValueError):
            text_source = response.body
        text = str(_redact_value(text_source, redacted_fields))[:text_limit]
        return cls(
            status=response.status_code,
            headers=headers,
            body=body,
            text=text,
            elapsed_ms=response.elapsed_ms,
            request_id=request_id,
        )


def _is_sensitive_field(key: str, redacted_fields: set[str] | frozenset[str]) -> bool:
    normalized = key.casefold().replace("-", "").replace("_", "")
    configured = {field.casefold().replace("-", "").replace("_", "") for field in redacted_fields}
    if normalized in configured:
        return True
    return any(
        fragment in normalized
        for fragment in (
            "authorization",
            "cookie",
            "token",
            "secret",
            "password",
            "apikey",
            "accesskey",
            "downloadurl",
        )
    )


def _redact_value(value: Any, redacted_fields: set[str] | frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if _is_sensitive_field(key, redacted_fields) else _redact_value(item, redacted_fields)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, redacted_fields) for item in value]
    return value


class ApiCaseContext:
    """保存已脱敏 API 结果、运行变量与待清理资源。"""

    def __init__(self, run_id: str) -> None:
        self._values: dict[str, Any] = {"run_id": run_id}
        # 原始 API body 只允许导出下载节点按审核契约读取，绝不进入通用变量视图或报告。
        self._private_export_results: dict[str, tuple[Any, ApiExportSpec]] = {}
        self._private_download_paths: dict[str, str] = {}
        self.resources: list[dict[str, Any]] = []

    def set(self, name: str, value: Any) -> None:
        self._values[name] = value

    def set_api_result(
        self,
        name: str,
        public_value: ApiResultView,
        raw_body: Any,
        export_spec: ApiExportSpec | None,
    ) -> None:
        self._values[name] = public_value
        if export_spec is not None:
            self._private_export_results[name] = (raw_body, export_spec)

    def get_export_download_url(self, name: str) -> tuple[str, ApiExportSpec]:
        try:
            raw_body, export_spec = self._private_export_results[name]
        except KeyError as exc:
            raise KeyError("Export download source must be an API result with approved export metadata") from exc
        url = resolve_api_value_path(raw_body, export_spec.download_url_path, self.get)
        if not isinstance(url, str) or not url:
            raise KeyError("Approved export result does not contain a download URL")
        return url, export_spec

    def set_download_artifact(self, name: str, public_value: dict[str, Any], private_path: str) -> None:
        self._values[name] = public_value
        self._private_download_paths[name] = private_path

    def get_download_artifact_path(self, name: str) -> str:
        try:
            return self._private_download_paths[name]
        except KeyError as exc:
            raise KeyError("CSV comparison requires a controlled download result variable") from exc

    def clear_private(self) -> None:
        self._private_export_results.clear()
        self._private_download_paths.clear()

    def get(self, expression: str) -> Any:
        root, path = self._split_expression(expression)
        if root not in self._values:
            raise KeyError(f"Unknown API runtime variable {root!r}")
        value = self._values[root]
        if isinstance(value, ApiResultView):
            value = {
                "status": value.status,
                "headers": dict(value.headers),
                "body": value.body,
                "text": value.text,
                "elapsed_ms": value.elapsed_ms,
                "request_id": value.request_id,
            }
        return resolve_api_value_path(value, path, self.get) if path else value

    def render(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {key: self.render(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.render(item) for item in value]
        if not isinstance(value, str):
            return value
        exact = _RE_EXACT_VARIABLE.match(value)
        if exact:
            return self.get(exact.group(1).strip())
        return _RE_VARIABLE.sub(lambda match: str(self.get(match.group(1).strip())), value)

    @staticmethod
    def _split_expression(expression: str) -> tuple[str, str]:
        expression = expression.strip()
        if "[" not in expression:
            return expression, ""
        root, suffix = expression.split("[", 1)
        if not suffix.endswith("]"):
            raise KeyError("API variable path must end with ']'")
        return root.strip(), suffix[:-1].strip()
