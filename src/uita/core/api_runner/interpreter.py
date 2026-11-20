"""独立 API TCS Interpreter。"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

from uita.core.api_runner.context import ApiCaseContext, ApiResultView
from uita.core.api_runner.assert_ops import ApiAssertionError, assert_endpoint_success, compare_values
from uita.core.api_dsl.api_nodes import (
    ApiAssertNode,
    ApiCallNode,
    ApiCaseNode,
    ApiCsvCompareNode,
    ApiDownloadNode,
    ApiExpectErrorNode,
    ApiExtractNode,
    ApiJsonParseNode,
    ApiStepNode,
    ApiTextNode,
    ApiTimestampNode,
    ApiValueNode,
)
from uita.core.api_runner.export_compare import ApiExportError, compare_csv_export
from uita.core.engine.api_config import ApiRuntimeConfig
from uita.core.engine.api_endpoint_catalog import ApiCatalogError, ApiEndpointCatalog
from uita.core.engine.api_base import ApiEngineBase
from uita.core.result.collector import CaseResult, ResultCollector


class ApiExecutionError(RuntimeError):
    """API Case 的业务执行或断言失败。"""


@dataclass
class ApiInterpreter:
    case: ApiCaseNode
    catalog: ApiEndpointCatalog
    engine: ApiEngineBase
    runtime_config: ApiRuntimeConfig
    result_dir: str
    run_id: str
    case_file: str = ""

    def __post_init__(self) -> None:
        self.context = ApiCaseContext(self.run_id)
        self.collector = ResultCollector(
            self.case.name, case_file=self.case_file, result_dir=self.result_dir,
        )
        self._used_artifact_names: set[str] = set()

    def _dedupe_artifact_filename(self, filename: str) -> str:
        """同用例多次下载同一端点时工件文件名去重（stem.csv → stem_2.csv …）。"""
        if filename not in self._used_artifact_names:
            self._used_artifact_names.add(filename)
            return filename
        stem, dot, suffix = filename.rpartition(".")
        base = stem if dot else filename
        ext = f".{suffix}" if dot else ""
        index = 2
        while f"{base}_{index}{ext}" in self._used_artifact_names:
            index += 1
        candidate = f"{base}_{index}{ext}"
        self._used_artifact_names.add(candidate)
        return candidate

    async def run(self) -> CaseResult:
        try:
            main_allowed = await self._run_steps(self.case.setup_steps, stop_on_failure=True)
            if main_allowed:
                await self._run_steps(self.case.steps, stop_on_failure=True)
            await self._run_steps(self.case.teardown_steps, stop_on_failure=False)
            result = self.collector.finalize()
            self.collector.write_json()
            return result
        finally:
            # 预签名下载地址仅为当前 Case 的下载节点保留，结束后立即清除。
            self.context.clear_private()

    async def _run_steps(self, steps: list[ApiStepNode], stop_on_failure: bool) -> bool:
        for step in steps:
            started = time.monotonic()
            try:
                for statement in step.statements:
                    await self._execute_statement(statement)
            except Exception as exc:
                self.collector.record_step(
                    step.name,
                    "failed",
                    duration_ms=(time.monotonic() - started) * 1000,
                    message=self._safe_error(exc),
                )
                if stop_on_failure:
                    return False
            else:
                self.collector.record_step(
                    step.name,
                    "passed",
                    duration_ms=(time.monotonic() - started) * 1000,
                    message="API step completed",
                )
        return True

    async def _execute_statement(
        self,
        statement: (
            ApiCallNode
            | ApiValueNode
            | ApiExtractNode
            | ApiTextNode
            | ApiTimestampNode
            | ApiJsonParseNode
            | ApiDownloadNode
            | ApiCsvCompareNode
            | ApiAssertNode
            | ApiExpectErrorNode
        ),
    ) -> None:
        if isinstance(statement, ApiCallNode):
            result, raw_body, export_spec = await self._execute_call(statement)
            if statement.result_var:
                self.context.set_api_result(statement.result_var, result, raw_body, export_spec)
            return
        if isinstance(statement, ApiDownloadNode):
            await self._execute_download(statement)
            return
        if isinstance(statement, ApiCsvCompareNode):
            self._execute_csv_compare(statement)
            return
        if isinstance(statement, ApiValueNode):
            self.context.set(statement.variable, self.context.render(statement.value))
            return
        if isinstance(statement, ApiExtractNode):
            value = self.context.get(f"{statement.source_variable}[{statement.path}]")
            if value is None:
                raise ApiExecutionError("API extract path did not resolve")
            self.context.set(statement.variable, value)
            return
        if isinstance(statement, ApiTextNode):
            value = self.context.get(statement.value_expression)
            # bool 按 JSON 文本（true/false）渲染：过滤值面向服务端 JSON 语义，Python str(True) 不命中。
            if isinstance(value, bool):
                self.context.set(statement.variable, "true" if value else "false")
            else:
                self.context.set(statement.variable, str(value))
            return
        if isinstance(statement, ApiTimestampNode):
            self.context.set(statement.variable, int(time.time()) + statement.offset_seconds)
            return
        if isinstance(statement, ApiJsonParseNode):
            value = self.context.get(statement.value_expression)
            if not isinstance(value, str):
                raise ApiExecutionError("解析JSON source must be a JSON string")
            try:
                self.context.set(statement.variable, json.loads(value))
            except json.JSONDecodeError as exc:
                raise ApiExecutionError("解析JSON source is not valid JSON") from exc
            return
        if isinstance(statement, ApiAssertNode):
            self._execute_assert(statement)
            return
        if isinstance(statement, ApiExpectErrorNode):
            try:
                await self._execute_call(statement.call)
            except Exception as exc:
                if statement.error_code not in str(exc):
                    raise ApiExecutionError("API error code did not match expectation") from exc
                return
            raise ApiExecutionError("API call succeeded but an error was expected")
        raise ApiExecutionError("Unsupported API AST statement")

    async def _execute_call(self, call: ApiCallNode) -> tuple[ApiResultView, Any, Any]:
        endpoint = self.catalog.resolve(call.name)
        arguments = self.context.render(call.arguments)
        max_rows = arguments.pop("最大条数", None)
        body = self.catalog.build_body(endpoint, arguments)
        if max_rows is not None:
            body = await self._cap_terminal_compliance_export_body(endpoint.action, body, max_rows)
        response = await self.engine.request(
            endpoint.method,
            endpoint.action,
            body=json.dumps(body, ensure_ascii=False),
            timeout=int(self.runtime_config.timeout_s),
        )
        result = ApiResultView.from_response(response, self.runtime_config.redacted_fields)
        try:
            assert_endpoint_success(endpoint.success_rules, result)
        except ApiAssertionError as exc:
            raise ApiExecutionError(str(exc)) from exc
        raw_body = response.json if response.json is not None else response.body
        if endpoint.export is not None:
            self._assert_export_contract(raw_body, endpoint.export)
        return result, raw_body, endpoint.export

    async def _cap_terminal_compliance_export_body(
        self,
        action: str,
        body: dict[str, Any],
        max_rows: Any,
    ) -> dict[str, Any]:
        """超过上限时丢弃最旧数据，收窄导出开始时间。"""
        if action != "DescribeLogDownloadURLTerminalCompliance":
            raise ApiExecutionError("最大条数 only supports terminal compliance exports")
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or not 1 <= max_rows <= 8000:
            raise ApiExecutionError("最大条数 must be an integer between 1 and 8000")
        start_time = body.get("StartTime")
        end_time = body.get("EndTime")
        if (
            isinstance(start_time, bool)
            or isinstance(end_time, bool)
            or not isinstance(start_time, int)
            or not isinstance(end_time, int)
            or start_time > end_time
        ):
            raise ApiExecutionError("最大条数 requires valid StartTime and EndTime")
        if await self._terminal_compliance_total(body, start_time) <= max_rows:
            return body

        low, high = start_time + 1, end_time
        capped_start: int | None = None
        while low <= high:
            midpoint = (low + high) // 2
            total = await self._terminal_compliance_total(body, midpoint)
            if total <= max_rows:
                capped_start = midpoint
                high = midpoint - 1
            else:
                low = midpoint + 1
        if capped_start is None:
            raise ApiExecutionError("terminal compliance export cannot be capped within the requested time window")
        capped = dict(body)
        capped["StartTime"] = capped_start
        return capped

    async def _terminal_compliance_total(self, export_body: dict[str, Any], start_time: int) -> int:
        allowed = (
            "LogId",
            "EndTime",
            "DomainInstanceId",
            "Filters",
            "OsType",
            "Department",
            "EndpointGroup",
            "Sort",
            "UserName",
        )
        query_body = {key: export_body[key] for key in allowed if key in export_body}
        query_body.update({"StartTime": start_time, "PageNumber": 0, "PageSize": 1})
        response = await self.engine.request(
            "POST",
            "DescribeLogDetailTerminalCompliance",
            body=json.dumps(query_body, ensure_ascii=False),
            timeout=int(self.runtime_config.timeout_s),
        )
        result = ApiResultView.from_response(response, self.runtime_config.redacted_fields)
        try:
            assert_endpoint_success({"error_absent": True}, result)
        except ApiAssertionError as exc:
            raise ApiExecutionError("terminal compliance export preflight failed") from exc
        payload = response.json if isinstance(response.json, dict) else {}
        total = payload.get("Response", {}).get("Data", {}).get("Total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise ApiExecutionError("terminal compliance export preflight returned an invalid total")
        return total

    @staticmethod
    def _assert_export_contract(raw_body: Any, export_spec) -> None:
        from uita.core.api_runner.value_path import resolve_api_value_path

        # 日志中心家族响应无 IsAsync 字段：缺失视为同步（false）。
        raw_flag = resolve_api_value_path(raw_body, export_spec.async_flag_path, lambda _: None)
        is_async = bool(raw_flag) if raw_flag is not None else False
        if is_async != export_spec.async_expected:
            raise ApiExecutionError("Export response async contract did not match the approved endpoint metadata")
        download_url = resolve_api_value_path(raw_body, export_spec.download_url_path, lambda _: None)
        if not isinstance(download_url, str) or not download_url:
            raise ApiExecutionError("Export response did not contain an approved download URL")

    async def _execute_download(self, statement: ApiDownloadNode) -> None:
        download_url, export_spec = self.context.get_export_download_url(statement.export_result_variable)
        artifact_dir = os.path.join(self.result_dir, "artifacts")
        artifact = await self.engine.download_export(
            download_url,
            artifact_dir,
            self._dedupe_artifact_filename(export_spec.artifact_filename),
            self.runtime_config.download_host_allowlist,
            self.runtime_config.max_download_bytes,
            int(self.runtime_config.download_timeout_s),
        )
        # 读取 CSV 数据行数与表头，供筛选用例做"导出行数==查询行数"等断言。
        from uita.core.api_runner.export_compare import read_csv_rows

        try:
            headers, csv_rows = read_csv_rows(artifact.path)
            row_count = len(csv_rows)
        except Exception:
            headers, row_count = [], -1  # 非 CSV（如 zip）不强行解析，rows=-1 占位
        self.context.set_download_artifact(
            statement.variable,
            {
                "artifact": artifact.relative_path,
                "bytes": artifact.byte_size,
                "content_type": artifact.content_type,
                "sha256": artifact.sha256,
                "rows": row_count,
                "headers": headers,
            },
            artifact.path,
        )
        self.collector.add_artifact(statement.variable, artifact.relative_path)

    def _execute_csv_compare(self, statement: ApiCsvCompareNode) -> None:
        source_rows = self.context.get(statement.source_rows_expression)
        artifact_path = self.context.get_download_artifact_path(statement.download_result_variable)
        try:
            result = compare_csv_export(source_rows, artifact_path, statement.unique_key, statement.field_mapping)
        except ApiExportError as exc:
            raise ApiExecutionError(f"CSV export comparison failed: {exc}") from exc
        self.context.set(statement.variable, result)

    @staticmethod
    def _assert_endpoint_success(success_rules: dict[str, Any], result: ApiResultView) -> None:
        """向后兼容包装：原样委托 assert_ops.assert_endpoint_success()（提取
        自本类，见 core/api_runner/assert_ops.py），保留原方法名/签名/异常
        类型不变，供既有外部引用（如有）零改动兼容。"""
        try:
            assert_endpoint_success(success_rules, result)
        except ApiAssertionError as exc:
            raise ApiExecutionError(str(exc)) from exc

    def _execute_assert(self, assertion: ApiAssertNode) -> None:
        actual = self.context.get(f"{assertion.variable}[{assertion.path}]")
        operator = assertion.operator
        expected = None
        if operator not in ("exists", "single"):
            expected = self.context.render(assertion.expected)
        passed = compare_values(operator, actual, expected)
        if not passed:
            raise ApiExecutionError(f"API assertion failed: {assertion.variable}[{assertion.path}] {operator}")

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        # 仅输出类型和受控 message，避免 SDK 异常或请求文本夹带凭据。
        message = str(exc).replace("\n", " ")[:300]
        return f"{type(exc).__name__}: {message}"
