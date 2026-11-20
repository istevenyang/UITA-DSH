"""只读 Rosetta v3 静态检查，复用 LintIssue/LintResult，无运行时或外部依赖。

E101 JSON/IO；E102 原始重复键；E103 v3 schema；E104 usage；
E105 正式名/aliases 冲突；E106 ID；E107 selector/js 互斥；
E108 Windows Name 正则语法；E109 纯树索引/坐标；E110 volatile 基线。
W101 长文案单候选维护提示，不放宽精确预期。

text_baseline.json 契约：schema_version="1.0"，elements 按正式名索引；
每个条目仅包含 candidates、versions、states 三个非空唯一字符串数组。
volatile 为布尔值；true 要求同目录基线有效且覆盖定位中的精确候选。
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Any

from uita.core.lint.tcs_linter import LintIssue, LintResult

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_WINDOWS_NAME = re.compile(r"(?:@|/)Name\s*=\s*(['\"])(.*?)\1")
_MAC_TEXT = re.compile(r"(?:title|value|description)\s*=\s*([^\]]+)")
_NUMBER = r"[-+]?\d+(?:\.\d+)?"
_COORDS = re.compile(
    rf"(?:\(?\s*{_NUMBER}\s*,\s*{_NUMBER}\s*\)?|"
    rf"\[\s*{_NUMBER}\s*,\s*{_NUMBER}\s*\]|"
    rf"(?:coords?|point|click_at|type_at)\s*[:=(\[]\s*{_NUMBER}\s*,\s*{_NUMBER}\s*[)\]]?|"
    rf"x\s*=\s*{_NUMBER}\s*[,; ]+\s*y\s*=\s*{_NUMBER})",
    re.IGNORECASE,
)
_TREE_INDEX = re.compile(
    r"(?:(?:tree|index|path)\s*[:=]\s*)?"
    r"(?:\d+(?:[./]\d+)+|(?:/\d+)+|(?:\[\d+\])+|\d+)",
    re.IGNORECASE,
)


class _JSONObject(dict):
    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        super().__init__(pairs)
        self.pairs = pairs


def _error(issues: list[LintIssue], code: str, message: str) -> None:
    issues.append(LintIssue(code=code, severity="error", message=message))


def _duplicates(value: Any, path: str, issues: list[LintIssue]) -> None:
    if isinstance(value, _JSONObject):
        seen: set[str] = set()
        for key, child in value.pairs:
            location = f"{path}[{key!r}]"
            if key in seen:
                _error(issues, "E102", f"原始 JSON 重复键: {location}")
            seen.add(key)
            _duplicates(child, location, issues)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _duplicates(child, f"{path}[{index}]", issues)


def _reject_constant(value: str) -> None:
    raise ValueError(f"非标准 JSON 数值: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"非有限 JSON 数值: {value}")
    return number


def _load_json(source: str, label: str, issues: list[LintIssue]) -> Any:
    try:
        data = json.loads(
            source, object_pairs_hook=_JSONObject,
            parse_constant=_reject_constant, parse_float=_finite_float,
        )
    except (ValueError, TypeError, RecursionError) as exc:
        issues.append(LintIssue(
            code="E101", severity="error", message=f"{label} JSON 解析失败: {exc}",
            line=getattr(exc, "lineno", 0),
        ))
        return None
    try:
        _duplicates(data, label, issues)
    except RecursionError:
        _error(issues, "E101", f"{label} JSON 嵌套过深")
        return None
    return data


def _string_list(value: Any) -> bool:
    return (
        isinstance(value, list) and bool(value)
        and all(isinstance(item, str) and bool(item.strip()) for item in value)
        and len(set(value)) == len(value)
    )


def _baseline(source: str | None, issues: list[LintIssue]) -> dict[str, Any] | None:
    if source is None:
        _error(issues, "E110", "volatile 元素缺少同目录 text_baseline.json")
        return None
    start = len(issues)
    data = _load_json(source, "text_baseline.json", issues)
    valid = (
        isinstance(data, dict) and set(data) == {"schema_version", "elements"}
        and data.get("schema_version") == "1.0"
        and isinstance(data.get("elements"), dict)
    )
    if valid:
        for name, entry in data["elements"].items():
            if (
                not name.strip() or not isinstance(entry, dict)
                or set(entry) != {"candidates", "versions", "states"}
                or not all(_string_list(entry[key]) for key in ("candidates", "versions", "states"))
            ):
                valid = False
                break
    if not valid or len(issues) != start:
        _error(issues, "E110", "text_baseline.json schema 无效；须为 1.0，包含正式名及非空 candidates/versions/states")
        return None
    return data["elements"]


def _text_candidates(element: dict[str, Any]) -> list[list[str]]:
    groups: list[list[str]] = []
    win = element.get("win")
    if isinstance(win, str) and win:
        matches = list(_WINDOWS_NAME.finditer(win))
        groups.extend(match.group(2).split("|") for match in matches)
        if not matches and not any(char in win for char in "@/=[]"):
            groups.append(win.split("|"))
    mac = element.get("mac")
    if isinstance(mac, str):
        groups.extend(match.group(1).split("|") for match in _MAC_TEXT.finditer(mac))
    return groups


def _check_windows(win: str, location: str, issues: list[LintIssue]) -> None:
    syntax = _QUOTED.sub("''", win)
    if re.search(r"@Name\s*~=", syntax):
        _error(issues, "E108", f"{location}.win 不支持 @Name~=；Windows 正则应使用 /Name~=")
    if _COORDS.fullmatch(win.strip()) or _TREE_INDEX.fullmatch(win.strip()):
        _error(issues, "E109", f"{location}.win 禁止纯树索引或纯坐标定位")
    elif re.fullmatch(r"(?:@?Index\s*=\s*(?:''|\d+)\s*)+", syntax, re.IGNORECASE):
        _error(issues, "E109", f"{location}.win 仅声明 Index，无稳定语义属性")


def _elements(data: Any, issues: list[LintIssue]) -> list[tuple[str, str, str, dict[str, Any]]]:
    if not isinstance(data, dict):
        if not any(issue.code == "E101" for issue in issues):
            _error(issues, "E103", "Rosetta 顶层须为 JSON object")
        return []
    meta = data.get("_meta")
    if not isinstance(meta, dict) or meta.get("version") != "3.0":
        _error(issues, "E103", "Rosetta _meta.version 须为字符串 '3.0'")
    apps = [name for name in data if name != "_meta"]
    if not apps:
        _error(issues, "E103", "Rosetta 缺少 app -> page -> element 结构")
    records = []
    for app in apps:
        pages = data[app]
        if not app.strip() or not isinstance(pages, dict) or not pages:
            _error(issues, "E103", f"app {app!r} 须为非空页面 object")
            continue
        for page, elements in pages.items():
            if not page.strip() or not isinstance(elements, dict) or not elements:
                _error(issues, "E103", f"{app}/{page} 须为非空元素 object")
                continue
            for name, element in elements.items():
                if not name.strip() or not isinstance(element, dict):
                    _error(issues, "E103", f"{app}/{page}/{name} 须为非空正式名及元素 object")
                    continue
                records.append((app, page, name, element))
    return records


def _check_records(
    records: list[tuple[str, str, str, dict[str, Any]]],
    issues: list[LintIssue], baseline_source: str | None,
) -> None:
    names: dict[tuple[str, str], str] = {}
    ids: dict[str, str] = {}
    baseline = None
    if any(element.get("volatile") is True for _, _, _, element in records):
        baseline = _baseline(baseline_source, issues)
    for app, page, name, element in records:
        location = f"{app}/{page}/{name}"
        usage = element.get("usage")
        if not isinstance(usage, str) or not usage.strip():
            _error(issues, "E104", f"{location} 缺少非空字符串 usage")
        if "selector" in element and "js" in element:
            _error(issues, "E107", f"{location} selector 与 js 互斥")
        for key in ("mac", "win", "selector", "js"):
            if key in element and not isinstance(element[key], str):
                _error(issues, "E103", f"{location}.{key} 须为字符串")
        aliases = element.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(alias, str) or not alias.strip() for alias in aliases):
            _error(issues, "E105", f"{location}.aliases 须为非空字符串数组")
            aliases = []
        for token in [name, *aliases]:
            key = (app, token)
            if key in names:
                _error(issues, "E105", f"{location} 正式名/别名 {token!r} 与 {names[key]} 冲突")
            else:
                names[key] = location
        if "id" in element:
            element_id = element["id"]
            if not isinstance(element_id, str) or not element_id.strip():
                _error(issues, "E106", f"{location}.id 须为非空字符串")
            elif element_id in ids:
                _error(issues, "E106", f"{location} 重复 id {element_id!r}，已用于 {ids[element_id]}")
            else:
                ids[element_id] = location
        win = element.get("win")
        if isinstance(win, str):
            _check_windows(win, location, issues)
        groups = _text_candidates(element)
        if any(len(group) == 1 and len(group[0]) >= 12 and re.search(r"[\u3400-\u9fff]", group[0]) for group in groups):
            issues.append(LintIssue(
                code="W101", severity="warning",
                message=f"{location} 长文案仅单一候选；确认状态/版本维护，精确预期可保留单文案",
            ))
        volatile = element.get("volatile", False)
        if "volatile" in element and not isinstance(volatile, bool):
            _error(issues, "E110", f"{location}.volatile 须为布尔值")
        if volatile is True and baseline is not None:
            entry = baseline.get(name)
            if entry is None:
                _error(issues, "E110", f"{location} 缺少正式名对应的 text_baseline 候选")
            elif any(candidate not in entry["candidates"] for group in groups for candidate in group):
                _error(issues, "E110", f"{location} 定位精确候选未全部纳入 text_baseline（保留尾空格）")


def _result(file_label: str, issues: list[LintIssue]) -> LintResult:
    return LintResult(file=file_label, ok=not any(issue.severity == "error" for issue in issues), issues=issues)


def lint_source(source: str, *, file_label: str = "", baseline_source: str | None = None) -> LintResult:
    """检查原始 JSON；基线由调用方注入，不执行 IO 或修改候选。"""
    issues: list[LintIssue] = []
    data = _load_json(source, file_label or "rosetta_map.json", issues)
    records = _elements(data, issues)
    _check_records(records, issues, baseline_source)
    return _result(file_label, issues)


def lint_file(path: str) -> LintResult:
    """检查文件，只为 volatile=true 读取同目录 text_baseline.json。"""
    label = os.fspath(path)
    issues: list[LintIssue] = []
    try:
        with open(label, encoding="utf-8") as stream:
            source = stream.read()
    except (OSError, UnicodeError) as exc:
        _error(issues, "E101", f"无法读取 {label}: {exc}")
        return _result(label, issues)
    data = _load_json(source, label, issues)
    records = _elements(data, issues)
    baseline_source = None
    if any(element.get("volatile") is True for _, _, _, element in records):
        baseline_path = os.path.join(os.path.dirname(label), "text_baseline.json")
        try:
            with open(baseline_path, encoding="utf-8") as stream:
                baseline_source = stream.read()
        except (OSError, UnicodeError) as exc:
            _error(issues, "E110", f"无法读取同目录基线 {baseline_path}: {exc}")
    _check_records(records, issues, baseline_source)
    return _result(label, issues)


def lint_paths(paths: list[str]) -> list[LintResult]:
    """目录只递归选择正式 rosetta_map.json；显式文件允许任意文件名。"""
    files: set[str] = set()
    for path in paths:
        label = os.fspath(path)
        if os.path.isdir(label):
            for root, dirs, names in os.walk(label):
                dirs[:] = sorted(name for name in dirs if not name.startswith("."))
                if "rosetta_map.json" in names:
                    files.add(os.path.abspath(os.path.join(root, "rosetta_map.json")))
        else:
            files.add(os.path.abspath(label))
    return [lint_file(path) for path in sorted(files)]
