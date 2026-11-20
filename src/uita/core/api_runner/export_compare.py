"""API-only CSV 导出解析与严格 JSON/CSV 比对。"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from uita.core.api_runner.value_path import ApiValuePathError, resolve_api_value_path


class ApiExportError(ValueError):
    """受控导出文件解析或比对失败。"""


def read_csv_rows(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    source = Path(path)
    if source.suffix.lower() != ".csv":
        raise ApiExportError("Export artifact must be a CSV file")
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            with source.open("r", encoding=encoding, newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
                    raise ApiExportError("CSV must contain unique headers")
                return list(reader.fieldnames), [dict(row) for row in reader]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise ApiExportError("CSV must be UTF-8 encoded") from last_error


def compare_csv_export(
    source_rows: Any,
    csv_path: str | Path,
    unique_key: str,
    field_mapping: dict[str, str],
) -> dict[str, Any]:
    source_rows = _normalize_source_rows(source_rows)
    if not source_rows or not all(isinstance(row, dict) for row in source_rows):
        raise ApiExportError("Export comparison source rows must be a non-empty object list")
    if not isinstance(unique_key, str) or not unique_key:
        raise ApiExportError("Export comparison requires a unique key")
    if not isinstance(field_mapping, dict) or not field_mapping:
        raise ApiExportError("Export comparison requires a non-empty field mapping")
    if unique_key not in field_mapping:
        raise ApiExportError("Field mapping must include the unique key")
    if not all(isinstance(source_path, str) and isinstance(csv_header, str) for source_path, csv_header in field_mapping.items()):
        raise ApiExportError("Field mapping must map source paths to CSV header strings")

    headers, csv_rows = read_csv_rows(csv_path)
    required_headers = set(field_mapping.values())
    if not required_headers.issubset(headers):
        raise ApiExportError("CSV is missing required export headers")
    if len(csv_rows) != len(source_rows):
        return _result(False, len(source_rows), len(csv_rows), "row_count_mismatch")

    expected = _index_source_rows(source_rows, unique_key)
    actual = _index_csv_rows(csv_rows, field_mapping[unique_key])
    if set(expected) != set(actual):
        return _result(False, len(source_rows), len(csv_rows), "unique_key_set_mismatch")

    for key, source_row in expected.items():
        csv_row = actual[key]
        for source_path, csv_header in field_mapping.items():
            try:
                source_value = resolve_api_value_path(source_row, source_path, lambda _: None)
            except ApiValuePathError as exc:
                raise ApiExportError("Invalid source field mapping path") from exc
            if _normalize(source_value) != _normalize(csv_row.get(csv_header, "")):
                return _result(False, len(source_rows), len(csv_rows), "field_value_mismatch")
    return _result(True, len(source_rows), len(csv_rows), "matched")


def _normalize_source_rows(rows: Any) -> list[Any]:
    """日志中心明细接口的行是 JSON 字符串，比对前统一解析为对象。"""
    if not isinstance(rows, list):
        return []
    normalized: list[Any] = []
    for row in rows:
        if isinstance(row, str):
            try:
                row = json.loads(row)
            except json.JSONDecodeError as exc:
                raise ApiExportError("Export comparison source row is not valid JSON") from exc
        normalized.append(row)
    return normalized


def _index_source_rows(rows: list[dict[str, Any]], unique_key: str) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        value = resolve_api_value_path(row, unique_key, lambda _: None)
        key = _normalize(value)
        if not key or key in indexed:
            raise ApiExportError(
                f"Source export rows must contain distinct non-empty unique keys "
                f"(unique_key={unique_key!r}, resolved={key!r})")
        indexed[key] = row
    return indexed


def _index_csv_rows(rows: list[dict[str, str]], unique_header: str) -> dict[str, dict[str, str]]:
    indexed: dict[str, dict[str, str]] = {}
    for row in rows:
        key = _normalize(row.get(unique_header, ""))
        if not key or key in indexed:
            raise ApiExportError("CSV export rows must contain distinct non-empty unique keys")
        indexed[key] = row
    return indexed


def _normalize(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _result(matched: bool, expected_rows: int, actual_rows: int, reason: str) -> dict[str, Any]:
    return {
        "matched": matched,
        "expected_rows": expected_rows,
        "actual_rows": actual_rows,
        "reason": reason,
    }
