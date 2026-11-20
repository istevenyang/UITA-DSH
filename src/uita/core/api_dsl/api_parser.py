"""独立 API TCS Parser。

不导入 UI DSL、Engine、App 或运行时 Action。语法仅覆盖 SaaS MVP：中文逻辑接口
调用、JSON 参数、基础响应路径断言、setup/teardown 与 expect_error。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from uita.core.api_runner.value_path import ApiValuePathError, validate_api_value_path

from .api_lexer import ApiLexError, lex
from .api_nodes import (
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


class ApiParseError(ValueError):
    def __init__(self, message: str, line_no: int) -> None:
        super().__init__(f"Line {line_no}: {message}")
        self.line_no = line_no


_RE_HEADER = re.compile(r"^([A-Za-z_]\w*)\s*:\s*(.*)$")
_RE_STEP = re.compile(r"^step\s*:\s*(.+)$", re.IGNORECASE)
_RE_SET = re.compile(r"^(\w+)\s*=\s*(.+)$", re.DOTALL)
_RE_EXTRACT = re.compile(r"^(\w+)\[(.+)\]$", re.DOTALL)
_RE_CALL = re.compile(r"^([\w-]+)\s*\((.*)\)\s*$", re.DOTALL)
_RE_ASSERT = re.compile(
    r"^(\w+)\[(.+)\]\s*(==|!=|<=|>=|<|>|contains|exists|single)(?:\s+(.+))?$",
    re.DOTALL,
)
_RE_EXPECT_ERROR = re.compile(r"^expect_error\s+(.+)\s+code\s*=\s*(.+)$", re.DOTALL)
_RE_VARIABLE = re.compile(r"^\$\{(.+)\}$", re.DOTALL)
_FORBIDDEN_ARGUMENTS = {"headers", "host", "base_url", "timeout", "retry", "auth", "signature", "encrypt"}


def _parse_tag_list(val: str) -> list[str]:
    """解析 tag 头字段值，与 UI DSL 一致：`[smoke, chat]` 或裸逗号分隔。"""
    v = val.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    v = v.replace("，", ",")
    return [t.strip() for t in v.split(",") if t.strip()]


def _balanced(text: str) -> bool:
    stack: list[str] = []
    quote = ""
    escaped = False
    pairs = {")": "(", "]": "[", "}": "{"}
    for char in text:
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
        elif char in "([{":
            stack.append(char)
        elif char in ")]}":
            if not stack or stack.pop() != pairs[char]:
                return False
    return not quote and not stack


def _split_top_level(text: str, line_no: int) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    stack: list[str] = []
    quote = ""
    escaped = False
    pairs = {")": "(", "]": "[", "}": "{"}
    for char in text:
        if quote:
            buffer.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ("'", '"'):
            quote = char
            buffer.append(char)
            continue
        if char in "([{":
            stack.append(char)
            buffer.append(char)
            continue
        if char in ")]}":
            if not stack or stack.pop() != pairs[char]:
                raise ApiParseError("unbalanced JSON/function delimiter", line_no)
            buffer.append(char)
            continue
        if char == "," and not stack:
            parts.append("".join(buffer).strip())
            buffer = []
            continue
        buffer.append(char)
    if quote or stack:
        raise ApiParseError("unclosed quote or delimiter", line_no)
    final = "".join(buffer).strip()
    if final:
        parts.append(final)
    return parts


def _parse_value(raw: str, line_no: int) -> Any:
    value = raw.strip()
    if _RE_VARIABLE.match(value):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ApiParseError("argument value must be JSON or ${variable}", line_no) from exc


def _parse_call(raw: str, line_no: int) -> ApiCallNode:
    matched = _RE_CALL.match(raw.strip())
    if not matched:
        raise ApiParseError("expected logical_api_name(key=value, ...)", line_no)
    name, raw_args = matched.groups()
    arguments: dict[str, Any] = {}
    for item in _split_top_level(raw_args, line_no):
        if "=" not in item:
            raise ApiParseError("API calls only support named arguments", line_no)
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"\w+", key):
            raise ApiParseError(f"invalid argument name {key!r}", line_no)
        if key.casefold() in _FORBIDDEN_ARGUMENTS:
            raise ApiParseError(f"forbidden API TCS argument {key!r}", line_no)
        if key in arguments:
            raise ApiParseError(f"duplicate argument {key!r}", line_no)
        arguments[key] = _parse_value(raw_value, line_no)
    return ApiCallNode(name=name, arguments=arguments, line_no=line_no, raw=raw)


def _require_exact_variable(value: Any, argument_name: str, line_no: int) -> str:
    if not isinstance(value, str):
        raise ApiParseError(f"{argument_name} must be a complete ${{variable}} reference", line_no)
    matched = _RE_VARIABLE.match(value)
    if not matched:
        raise ApiParseError(f"{argument_name} must be a complete ${{variable}} reference", line_no)
    return matched.group(1).strip()


def _parse_export_builtin(call: ApiCallNode, result_var: str, line_no: int):
    if call.name == "转为文本":
        if set(call.arguments) != {"值"}:
            raise ApiParseError("转为文本 only accepts 值=${variable}", line_no)
        expression = _require_exact_variable(call.arguments["值"], "值", line_no)
        return ApiTextNode(variable=result_var, value_expression=expression, line_no=line_no, raw=call.raw)
    if call.name == "当前时间戳":
        if not set(call.arguments) <= {"偏移秒"}:
            raise ApiParseError("当前时间戳 only accepts optional 偏移秒=<integer>", line_no)
        offset = call.arguments.get("偏移秒", 0)
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise ApiParseError("当前时间戳 偏移秒 must be an integer", line_no)
        return ApiTimestampNode(variable=result_var, offset_seconds=offset, line_no=line_no, raw=call.raw)
    if call.name == "解析JSON":
        if set(call.arguments) != {"值"}:
            raise ApiParseError("解析JSON only accepts 值=${variable}", line_no)
        expression = _require_exact_variable(call.arguments["值"], "值", line_no)
        return ApiJsonParseNode(variable=result_var, value_expression=expression, line_no=line_no, raw=call.raw)
    if call.name == "下载导出文件":
        if set(call.arguments) != {"导出结果"}:
            raise ApiParseError("下载导出文件 only accepts 导出结果=${export_result}", line_no)
        expression = _require_exact_variable(call.arguments["导出结果"], "导出结果", line_no)
        if "[" in expression:
            raise ApiParseError("导出结果 must reference an export result variable without a path", line_no)
        return ApiDownloadNode(variable=result_var, export_result_variable=expression, line_no=line_no, raw=call.raw)
    if call.name == "校验CSV导出":
        required = {"下载结果", "查询数据", "唯一键", "字段映射"}
        if set(call.arguments) != required:
            raise ApiParseError("校验CSV导出 requires 下载结果、查询数据、唯一键、字段映射", line_no)
        download_variable = _require_exact_variable(call.arguments["下载结果"], "下载结果", line_no)
        if "[" in download_variable:
            raise ApiParseError("下载结果 must reference a download result variable without a path", line_no)
        source_rows_expression = _require_exact_variable(call.arguments["查询数据"], "查询数据", line_no)
        unique_key = call.arguments["唯一键"]
        mapping = call.arguments["字段映射"]
        if not isinstance(unique_key, str) or not unique_key:
            raise ApiParseError("唯一键 must be a non-empty string", line_no)
        if not isinstance(mapping, dict) or not mapping or not all(isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()):
            raise ApiParseError("字段映射 must be a non-empty JSON object of string mappings", line_no)
        return ApiCsvCompareNode(
            variable=result_var,
            download_result_variable=download_variable,
            source_rows_expression=source_rows_expression,
            unique_key=unique_key,
            field_mapping=mapping,
            line_no=line_no,
            raw=call.raw,
        )
    return None


def _parse_assert(raw: str, line_no: int) -> ApiAssertNode:
    matched = _RE_ASSERT.match(raw.strip())
    if not matched:
        raise ApiParseError("invalid API assertion", line_no)
    variable, path, operator, raw_expected = matched.groups()
    try:
        validate_api_value_path(path)
    except ApiValuePathError as exc:
        raise ApiParseError(str(exc), line_no) from exc
    if operator in {"exists", "single"}:
        if raw_expected:
            raise ApiParseError(f"assertion {operator!r} does not accept an expected value", line_no)
        expected = None
    else:
        if raw_expected is None:
            raise ApiParseError(f"assertion {operator!r} requires an expected value", line_no)
        expected = _parse_value(raw_expected, line_no)
    return ApiAssertNode(variable=variable, path=path, operator=operator, expected=expected, line_no=line_no, raw=raw)


def _collect_statement(lines: list[tuple[int, int, str]], index: int) -> tuple[str, int]:
    line_no, indent, text = lines[index]
    statement = text
    index += 1
    while not _balanced(statement):
        if index >= len(lines):
            raise ApiParseError("unclosed multi-line statement", line_no)
        next_no, next_indent, next_text = lines[index]
        if next_indent < indent:
            raise ApiParseError("multi-line statement continuation cannot dedent", next_no)
        statement += "\n" + next_text
        index += 1
    return statement, index


def parse_api_source(source: str) -> ApiCaseNode:
    try:
        token_lines = lex(source)
    except ApiLexError as exc:
        raise ApiParseError(str(exc), 0) from exc
    lines = [(line.number, line.indent, line.text) for line in token_lines]
    case = ApiCaseNode()
    pending_section = ""
    current_step: ApiStepNode | None = None
    index = 0
    while index < len(lines):
        line_no, indent, text = lines[index]
        if text in {"@setup", "@teardown"}:
            if indent:
                raise ApiParseError("section markers must not be indented", line_no)
            pending_section = text[1:]
            current_step = None
            index += 1
            continue
        step_match = _RE_STEP.match(text)
        if step_match:
            if indent:
                raise ApiParseError("step must not be indented", line_no)
            current_step = ApiStepNode(name=step_match.group(1).strip(), line_no=line_no)
            if pending_section == "setup":
                current_step.is_setup = True
                case.setup_steps.append(current_step)
            elif pending_section == "teardown":
                current_step.is_teardown = True
                case.teardown_steps.append(current_step)
            else:
                case.steps.append(current_step)
            pending_section = ""
            index += 1
            continue
        header_match = _RE_HEADER.match(text)
        if current_step is None and header_match:
            key, value = header_match.groups()
            key = key.lower()
            if key == "name":
                case.name = value
            elif key == "app":
                case.app = value
            elif key == "os":
                case.os = value
            elif key == "type":
                case.case_type = value.lower()
            elif key == "priority":
                case.priority = value
            elif key == "owner":
                case.owner = value
            elif key == "description":
                case.description = value
            elif key == "tag":
                case.tags = _parse_tag_list(value)
            index += 1
            continue
        if current_step is None:
            raise ApiParseError("expected a header, section marker, or step", line_no)
        if indent < 4:
            raise ApiParseError("API step statements must use at least four spaces indentation", line_no)
        statement, index = _collect_statement(lines, index)
        if statement.startswith("set:"):
            matched = _RE_SET.match(statement[4:].strip())
            if not matched:
                raise ApiParseError("invalid set statement", line_no)
            variable, raw_value = matched.groups()
            extract_match = _RE_EXTRACT.match(raw_value.strip())
            if extract_match:
                source_variable, path = extract_match.groups()
                try:
                    validate_api_value_path(path)
                except ApiValuePathError as exc:
                    raise ApiParseError(str(exc), line_no) from exc
                current_step.statements.append(
                    ApiExtractNode(
                        variable=variable,
                        source_variable=source_variable,
                        path=path,
                        line_no=line_no,
                        raw=statement,
                    )
                )
                continue
            if _RE_CALL.match(raw_value.strip()):
                call = _parse_call(raw_value, line_no)
                export_builtin = _parse_export_builtin(call, variable, line_no)
                if export_builtin is not None:
                    current_step.statements.append(export_builtin)
                    continue
                current_step.statements.append(
                    ApiCallNode(
                        name=call.name,
                        arguments=call.arguments,
                        line_no=line_no,
                        raw=statement,
                        result_var=variable,
                    )
                )
                continue
            current_step.statements.append(
                ApiValueNode(
                    variable=variable,
                    value=_parse_value(raw_value, line_no),
                    line_no=line_no,
                    raw=statement,
                )
            )
            continue
        if statement.startswith("assert:"):
            current_step.statements.append(_parse_assert(statement[7:].strip(), line_no))
            continue
        expected_match = _RE_EXPECT_ERROR.match(statement)
        if expected_match:
            raw_call, raw_code = expected_match.groups()
            code = _parse_value(raw_code, line_no)
            if not isinstance(code, str):
                raise ApiParseError("expect_error code must be a string", line_no)
            current_step.statements.append(ApiExpectErrorNode(_parse_call(raw_call, line_no), code, line_no, statement))
            continue
        raise ApiParseError("unsupported API step statement", line_no)
    if case.case_type != "api":
        raise ApiParseError("API TCS requires header 'type: api'", 1)
    if not case.name or not case.app or not case.os:
        raise ApiParseError("API TCS requires name/app/os headers", 1)
    if not case.steps and not case.setup_steps:
        raise ApiParseError("API TCS requires at least one setup or main step", 1)
    return case


def parse_api_file(path: str | Path) -> ApiCaseNode:
    source = Path(path).read_text(encoding="utf-8")
    return parse_api_source(source)
