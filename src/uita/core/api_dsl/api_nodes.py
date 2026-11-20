"""独立 API TCS DSL AST。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ApiCallNode:
    name: str
    arguments: dict[str, Any]
    line_no: int
    raw: str
    result_var: str = ""


@dataclass(frozen=True)
class ApiValueNode:
    variable: str
    value: Any
    line_no: int
    raw: str


@dataclass(frozen=True)
class ApiExtractNode:
    variable: str
    source_variable: str
    path: str
    line_no: int
    raw: str


@dataclass(frozen=True)
class ApiTextNode:
    variable: str
    value_expression: str
    line_no: int
    raw: str


@dataclass(frozen=True)
class ApiTimestampNode:
    variable: str
    offset_seconds: int
    line_no: int
    raw: str


@dataclass(frozen=True)
class ApiJsonParseNode:
    variable: str
    value_expression: str
    line_no: int
    raw: str


@dataclass(frozen=True)
class ApiDownloadNode:
    variable: str
    export_result_variable: str
    line_no: int
    raw: str


@dataclass(frozen=True)
class ApiCsvCompareNode:
    variable: str
    download_result_variable: str
    source_rows_expression: str
    unique_key: str
    field_mapping: dict[str, str]
    line_no: int
    raw: str


@dataclass(frozen=True)
class ApiAssertNode:
    variable: str
    path: str
    operator: str
    expected: Any
    line_no: int
    raw: str


@dataclass(frozen=True)
class ApiExpectErrorNode:
    call: ApiCallNode
    error_code: str
    line_no: int
    raw: str


ApiStatement = (
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
)


@dataclass
class ApiStepNode:
    name: str
    statements: list[ApiStatement] = field(default_factory=list)
    is_setup: bool = False
    is_teardown: bool = False
    line_no: int = 0


@dataclass
class ApiCaseNode:
    name: str = ""
    app: str = ""
    os: str = ""
    case_type: str = ""
    priority: str = ""
    owner: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    setup_steps: list[ApiStepNode] = field(default_factory=list)
    steps: list[ApiStepNode] = field(default_factory=list)
    teardown_steps: list[ApiStepNode] = field(default_factory=list)
