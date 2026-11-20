"""API-only ValuePath：受限字段、下标、列表字段投影和等值筛选。

不支持脚本、函数、通配符、递归下降或动态字段名。筛选返回保序列表，调用方必须使用
``single`` 断言后显式 ``.0`` 取值，禁止隐式取首项。
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any


class ApiValuePathError(ValueError):
    pass


_RE_FIELD = re.compile(r"^\w+$")
_RE_INDEX = re.compile(r"^-?\d+$")
_RE_FILTER = re.compile(r"^\[(\w+)=(.+)\]$")
_RE_VARIABLE = re.compile(r"^\$\{(.+)\}$", re.DOTALL)


def split_api_value_path(path: str) -> list[str]:
    if not path or len(path) > 512:
        raise ApiValuePathError("API ValuePath must be non-empty and no longer than 512 characters")
    segments: list[str] = []
    buffer: list[str] = []
    depth = 0
    for char in path:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth < 0:
                raise ApiValuePathError("API ValuePath contains an unbalanced closing bracket")
        if char == "." and depth == 0:
            segment = "".join(buffer).strip()
            if not segment:
                raise ApiValuePathError("API ValuePath contains an empty segment")
            segments.append(segment)
            buffer = []
            continue
        buffer.append(char)
    if depth:
        raise ApiValuePathError("API ValuePath contains an unclosed filter")
    final = "".join(buffer).strip()
    if not final:
        raise ApiValuePathError("API ValuePath contains an empty segment")
    segments.append(final)
    return segments


def validate_api_value_path(path: str) -> None:
    for segment in split_api_value_path(path):
        if _RE_FIELD.fullmatch(segment) or _RE_INDEX.fullmatch(segment):
            continue
        matched = _RE_FILTER.fullmatch(segment)
        if not matched:
            raise ApiValuePathError(f"unsupported API ValuePath segment {segment!r}")
        _, raw_expected = matched.groups()
        _parse_expected(raw_expected)


def resolve_api_value_path(
    value: Any,
    path: str,
    variable_resolver: Callable[[str], Any],
) -> Any:
    validate_api_value_path(path)
    current = value
    for segment in split_api_value_path(path):
        filter_match = _RE_FILTER.fullmatch(segment)
        if filter_match:
            if not isinstance(current, list):
                return None
            field, raw_expected = filter_match.groups()
            expected = _parse_expected(raw_expected, variable_resolver)
            current = [item for item in current if isinstance(item, dict) and item.get(field) == expected]
            continue
        if isinstance(current, dict):
            current = current.get(segment)
            continue
        if isinstance(current, list):
            if _RE_INDEX.fullmatch(segment):
                index = int(segment)
                current = current[index] if -len(current) <= index < len(current) else None
            elif _RE_FIELD.fullmatch(segment):
                current = [item.get(segment) for item in current if isinstance(item, dict) and segment in item]
            else:
                return None
            continue
        return None
    return current


def _parse_expected(raw: str, variable_resolver: Callable[[str], Any] | None = None) -> Any:
    expected = raw.strip()
    variable = _RE_VARIABLE.fullmatch(expected)
    if variable:
        if variable_resolver is None:
            return expected
        return variable_resolver(variable.group(1).strip())
    try:
        return json.loads(expected)
    except json.JSONDecodeError as exc:
        raise ApiValuePathError("filter expected value must be JSON scalar or ${variable}") from exc
