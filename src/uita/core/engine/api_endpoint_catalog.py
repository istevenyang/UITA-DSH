"""API EndpointCatalog：加载并校验人工审核后的 api_map.json。

只负责安全的逻辑端点解析与业务输入投影，不读取凭据、不签名、不发送网络请求。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ApiCatalogError(ValueError):
    """api_map.json 结构、端点或调用参数不合法。"""


@dataclass(frozen=True)
class ApiInput:
    name: str
    location: str
    key: str
    required: bool = False


@dataclass(frozen=True)
class ApiExportSpec:
    """已审核导出端点的敏感下载契约。"""

    download_url_path: str
    async_flag_path: str
    async_expected: bool
    artifact_filename: str


@dataclass(frozen=True)
class ApiEndpoint:
    name: str
    aliases: tuple[str, ...]
    method: str
    transport: str
    action: str
    default_body: dict[str, Any]
    inputs: dict[str, ApiInput]
    idempotent: bool = False
    destructive: bool = False
    cleanup: str = ""
    success_rules: dict[str, Any] = field(default_factory=dict)
    export: ApiExportSpec | None = None

class ApiEndpointCatalog:
    """已审核 API endpoint 的 target 专属视图。"""

    def __init__(self, endpoints: dict[str, ApiEndpoint], source_path: str) -> None:
        self._endpoints = endpoints
        self.source_path = source_path
        self._aliases: dict[str, str] = {}
        for endpoint in endpoints.values():
            for alias in (endpoint.name, *endpoint.aliases):
                key = alias.strip().casefold()
                if not key:
                    raise ApiCatalogError(f"Endpoint {endpoint.name!r} contains an empty alias")
                previous = self._aliases.get(key)
                if previous and previous != endpoint.name:
                    raise ApiCatalogError(f"Duplicate endpoint alias {alias!r}: {previous!r}/{endpoint.name!r}")
                self._aliases[key] = endpoint.name

        # method+path 反向索引（2026-08-25 新增，供主 DSL get/post/put/
        # delete[] 动词式调用反查已审核端点，见计划文档
        # docs/plans/done/archive_202608.md §2.5）。
        # 仅 transport="http" 端点参与（有真实相对路径 action 可供索引）；
        # tc_action 端点（SaaS 控制面，方法恒 POST + Action 名而非路径）
        # 不参与本索引，范围明确收窄，不影响既有按名调用能力。
        self._method_path_index: dict[tuple[str, str], str] = {}
        for endpoint in endpoints.values():
            if endpoint.transport != "http":
                continue
            key = (endpoint.method, endpoint.action)
            if key in self._method_path_index:
                raise ApiCatalogError(
                    f"Duplicate (method, path) {key!r}: "
                    f"{self._method_path_index[key]!r}/{endpoint.name!r}"
                )
            self._method_path_index[key] = endpoint.name

    @classmethod
    def from_file(cls, path: str | Path) -> "ApiEndpointCatalog":
        source = Path(path)
        try:
            document = json.loads(source.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ApiCatalogError(f"Cannot read API catalog: {source}") from exc
        except json.JSONDecodeError as exc:
            raise ApiCatalogError(f"Invalid API catalog JSON: {source}:{exc.lineno}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("endpoints"), dict):
            raise ApiCatalogError("API catalog requires an object field 'endpoints'")
        meta = document.get("_meta", {})
        if not isinstance(meta, dict) or meta.get("review_status") != "approved":
            raise ApiCatalogError("API catalog must be manually approved before execution")
        endpoints = {
            name: cls._parse_endpoint(name, raw)
            for name, raw in document["endpoints"].items()
        }
        if not endpoints:
            raise ApiCatalogError("API catalog must contain at least one endpoint")
        return cls(endpoints, str(source))

    @staticmethod
    def _parse_endpoint(name: str, raw: Any) -> ApiEndpoint:
        if not isinstance(name, str) or not name.strip() or not isinstance(raw, dict):
            raise ApiCatalogError("Endpoint names must be non-empty strings and definitions must be objects")
        method = raw.get("method", "").upper()
        transport = raw.get("transport", "")
        action = raw.get("action", "")
        if transport not in ("tc_action", "http"):
            raise ApiCatalogError(f"Endpoint {name!r} transport must be 'tc_action' or 'http'")
        if transport == "tc_action":
            # SaaS 控制面：Action 名 + POST（云服务端 TC API 统一 POST）
            if method != "POST":
                raise ApiCatalogError(f"Endpoint {name!r} must declare method POST for tc_action")
            if not isinstance(action, str) or not action:
                raise ApiCatalogError(f"Endpoint {name!r} must declare non-empty action")
        else:
            # 私有化：相对 path（如 /capi/GatewayResource/DescribeResourceModules），host 由 configs 注入
            if method not in ("GET", "POST", "PUT", "DELETE", "UPLOAD"):
                raise ApiCatalogError(f"Endpoint {name!r} declares unsupported HTTP method {method!r}")
            if not isinstance(action, str) or not action.startswith("/"):
                raise ApiCatalogError(f"Endpoint {name!r} http transport requires a relative path in 'action'")
        aliases = raw.get("aliases", [])
        default_body = raw.get("default_body", {})
        inputs = raw.get("inputs", {})
        if not isinstance(aliases, list) or not all(isinstance(item, str) for item in aliases):
            raise ApiCatalogError(f"Endpoint {name!r} aliases must be a string array")
        if not isinstance(default_body, dict) or not isinstance(inputs, dict):
            raise ApiCatalogError(f"Endpoint {name!r} default_body and inputs must be objects")
        parsed_inputs: dict[str, ApiInput] = {}
        for input_name, input_raw in inputs.items():
            if not isinstance(input_name, str) or not isinstance(input_raw, dict):
                raise ApiCatalogError(f"Endpoint {name!r} has invalid input declaration")
            location = input_raw.get("in")
            key = input_raw.get("key")
            if location != "body" or not isinstance(key, str) or not key:
                raise ApiCatalogError(f"Endpoint {name!r} input {input_name!r} only supports non-empty body mapping")
            parsed_inputs[input_name] = ApiInput(
                name=input_name,
                location=location,
                key=key,
                required=bool(input_raw.get("required", False)),
            )
        export = ApiEndpointCatalog._parse_export_spec(name, raw.get("export"))
        return ApiEndpoint(
            name=name,
            aliases=tuple(aliases),
            method=method,
            transport=transport,
            action=action,
            default_body=dict(default_body),
            inputs=parsed_inputs,
            idempotent=bool(raw.get("idempotent", False)),
            destructive=bool(raw.get("destructive", False)),
            cleanup=str(raw.get("cleanup", "")),
            success_rules=dict(raw.get("success_rules", {})),
            export=export,
        )

    @staticmethod
    def _parse_export_spec(endpoint_name: str, raw: Any) -> ApiExportSpec | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise ApiCatalogError(f"Endpoint {endpoint_name!r} export metadata must be an object")
        download_url_path = raw.get("download_url_path")
        async_flag_path = raw.get("async_flag_path")
        artifact_filename = raw.get("artifact_filename")
        if not all(isinstance(value, str) and value for value in (download_url_path, async_flag_path, artifact_filename)):
            raise ApiCatalogError(f"Endpoint {endpoint_name!r} export metadata is incomplete")
        if not artifact_filename.endswith(".csv") or "/" in artifact_filename or "\\" in artifact_filename:
            raise ApiCatalogError(f"Endpoint {endpoint_name!r} export artifact filename must be a safe CSV basename")
        if not isinstance(raw.get("async_expected"), bool):
            raise ApiCatalogError(f"Endpoint {endpoint_name!r} export async_expected must be boolean")
        return ApiExportSpec(
            download_url_path=download_url_path,
            async_flag_path=async_flag_path,
            async_expected=raw["async_expected"],
            artifact_filename=artifact_filename,
        )

    def resolve(self, logical_name: str) -> ApiEndpoint:
        try:
            endpoint_name = self._aliases[logical_name.strip().casefold()]
        except KeyError as exc:
            raise ApiCatalogError(f"Unknown API endpoint {logical_name!r}") from exc
        return self._endpoints[endpoint_name]

    def resolve_by_method_and_path(self, method: str, path: str) -> ApiEndpoint:
        """按 (method, path) 反查已审核端点；未命中直接拒绝（安全边界不
        因调用风格改变而放宽，见计划文档
        docs/plans/done/archive_202608.md §2.5）。仅
        ``transport="http"`` 端点参与本索引（见 ``__init__`` 说明）。

        Raises:
            ApiCatalogError: 无匹配的已审核端点。
        """
        key = (method.upper(), path)
        try:
            endpoint_name = self._method_path_index[key]
        except KeyError as exc:
            raise ApiCatalogError(
                f"No approved endpoint matches {method.upper()} {path!r}"
            ) from exc
        return self._endpoints[endpoint_name]

    @staticmethod
    def build_body(endpoint: ApiEndpoint, arguments: dict[str, Any]) -> dict[str, Any]:
        unknown = set(arguments) - set(endpoint.inputs)
        if unknown:
            raise ApiCatalogError(f"Endpoint {endpoint.name!r} received unsupported inputs: {sorted(unknown)}")
        missing = [name for name, spec in endpoint.inputs.items() if spec.required and name not in arguments]
        if missing:
            raise ApiCatalogError(f"Endpoint {endpoint.name!r} is missing required inputs: {missing}")
        body = dict(endpoint.default_body)
        for name, value in arguments.items():
            body[endpoint.inputs[name].key] = value
        return body

    def all_names(self) -> list[str]:
        return list(self._endpoints)
