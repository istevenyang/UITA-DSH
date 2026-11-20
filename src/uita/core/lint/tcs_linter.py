"""TCS 格式检查器 — 语法级 + 规范级检查（不做元素在 rosetta_map 中存在性等语义级检查）。

计划文档：docs/plans/archive/06-skill-agent-实施纪要.md §3.1/§3.2

依赖仅 `core.dsl.parser`（`parse_file`/`parse_source`）+ `core.dsl.nodes`
（`CaseNode`），不依赖 `core/engine`、`core/platform`、`core/resolver`、
`core/actions` 等——天然满足 R2（`core/` 零外部依赖）。"app 是否已注册"检查
（E006）不在本模块内做 `apps/` 目录扫描（避免 `core/lint/` 反向感知 `apps/`
目录结构），改为接受可选的 `known_apps` 集合，由调用方（`main.py`）复用
`_auto_discover()` 同款目录扫描逻辑构造后传入；未传入时该项检查自动跳过。

检查规则清单（Code 对照见计划文档 §3.2）：
    E001  无法解析（parse 异常，含已废弃写法 `js=`/`eval_js[$var]` 等）
    E002  name 字段为空
    E003  app 字段为空
    E004  os 字段为空
    E005  os 取值不合法（不在 macos/windows/linux/universal/all 集合内）
    E006  app 未注册（仅当传入 known_apps 时生效）
    W001  name 与文件名不一致
    W002  name 在本次批量扫描范围内重复
    W003  priority 声明了但不在 P0-P3 范围内
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from uita.core.dsl.nodes import CaseNode
from uita.core.dsl.parser import parse_file, parse_source

# os 合法取值：具体平台名 + 跨平台通用别名（"all" 与 "universal" 完全等价，
# 见宪法 R12、core/registry_common.py::_UNIVERSAL_OS_ALIASES）。
_VALID_OS = {"macos", "windows", "linux", "universal", "all"}

# priority 常见取值（tcs-syntax.md 约定，非宪法硬性规则，未命中仅 warning）。
_VALID_PRIORITY = {"p0", "p1", "p2", "p3"}


@dataclass
class LintIssue:
    """单条检查违规记录。"""

    code: str            # "E001".."E006" / "W001".."W003"
    severity: str         # "error" | "warning"
    message: str          # 中文描述
    line: int = 0          # 能定位到具体行时填（如 E001 用 ParseError.line_no），否则 0


@dataclass
class LintResult:
    """单个 .tcs 文件的检查结果。"""

    file: str
    ok: bool              # 无 severity=="error" 的 issue 即为 True（warning 不影响 ok）
    issues: list[LintIssue] = field(default_factory=list)


def _make_parse_error_issue(exc: Exception) -> LintIssue:
    message = str(exc)
    line_no = getattr(exc, "line_no", 0) or 0
    if "已废弃" in message:
        message = f"[已废弃语法] {message}"
    return LintIssue(code="E001", severity="error", message=f"解析失败: {message}", line=line_no)


def _check_name_matches_filename(case: CaseNode, file_label: str) -> list[LintIssue]:
    if not file_label or not case.name.strip():
        return []
    stem = os.path.splitext(os.path.basename(file_label))[0]
    if case.name.strip() != stem:
        return [LintIssue(
            code="W001", severity="warning",
            message=f"name({case.name!r}) 与文件名({stem!r}) 不一致（约定文件名应与 name 一致）",
        )]
    return []


def _check_case(case: CaseNode, *, known_apps: Optional[set[str]]) -> list[LintIssue]:
    issues: list[LintIssue] = []

    if not case.name.strip():
        issues.append(LintIssue(code="E002", severity="error", message="缺少必填字段 name（宪法 R12）"))
    if not case.app.strip():
        issues.append(LintIssue(code="E003", severity="error", message="缺少必填字段 app（宪法 R12）"))

    os_val = case.os.strip()
    if not os_val:
        issues.append(LintIssue(code="E004", severity="error", message="缺少必填字段 os（宪法 R12）"))
    elif os_val.lower() not in _VALID_OS:
        issues.append(LintIssue(
            code="E005", severity="error",
            message=(
                f"os 取值不合法: {case.os!r}，须为 macOS/Windows/Linux/all/universal "
                "之一（大小写不敏感，all 与 universal 完全等价，见宪法 R12）"
            ),
        ))

    if known_apps is not None and case.app.strip() and case.app.strip() not in known_apps:
        issues.append(LintIssue(
            code="E006", severity="error",
            message=f"app {case.app!r} 未在 apps/ 目录下注册（缺少 apps/{case.app}/register.py）",
        ))

    priority_val = case.priority.strip()
    if priority_val and priority_val.lower() not in _VALID_PRIORITY:
        issues.append(LintIssue(
            code="W003", severity="warning",
            message=f"priority 取值 {case.priority!r} 不在常见范围 P0/P1/P2/P3 内",
        ))

    return issues


def _lint_impl(
    *, path: Optional[str], source: Optional[str], file_label: str,
    known_apps: Optional[set[str]],
) -> tuple[LintResult, Optional[CaseNode]]:
    try:
        case = parse_file(path) if path is not None else parse_source(source or "")
    except Exception as exc:  # noqa: BLE001 — 任意解析异常均视为 E001，不向上抛出
        issue = _make_parse_error_issue(exc)
        return LintResult(file=file_label, ok=False, issues=[issue]), None

    issues = _check_case(case, known_apps=known_apps)
    issues.extend(_check_name_matches_filename(case, file_label))
    ok = not any(i.severity == "error" for i in issues)
    return LintResult(file=file_label, ok=ok, issues=issues), case


def lint_file(path: str, *, known_apps: Optional[set[str]] = None) -> LintResult:
    """检查单个 `.tcs` 文件路径。"""
    result, _case = _lint_impl(path=path, source=None, file_label=path, known_apps=known_apps)
    return result


def lint_source(
    source: str, *, file_label: str = "", known_apps: Optional[set[str]] = None,
) -> LintResult:
    """检查一段 `.tcs` 源码文本（不涉及真实文件路径，`file_label` 仅用于报告展示
    与 W001 文件名一致性检查——留空时跳过 W001）。"""
    result, _case = _lint_impl(path=None, source=source, file_label=file_label, known_apps=known_apps)
    return result


def _expand_to_tcs_files(paths: list[str]) -> list[str]:
    """paths 可混合文件与目录；目录递归展开为其下全部 *.tcs（对齐
    core/runner/batch_runner.py 现有的目录展开方式）。"""
    files: list[str] = []
    for p in paths:
        if os.path.isfile(p):
            if p.endswith(".tcs"):
                files.append(p)
        elif os.path.isdir(p):
            for root, _dirs, fnames in os.walk(p):
                for fname in fnames:
                    if fname.endswith(".tcs"):
                        files.append(os.path.join(root, fname))
    return sorted(files)


def lint_paths(paths: list[str], *, known_apps: Optional[set[str]] = None) -> list[LintResult]:
    """批量检查入口：`paths` 可混合文件与目录；目录递归展开为其下全部 `*.tcs`。

    W002（name 跨文件重复）依赖全量结果一次性计算，因此是唯一暴露的批量入口
    （`lint_file`/`lint_source` 均无法感知"批量范围内是否重复"）。
    """
    files = _expand_to_tcs_files(paths)

    results: list[LintResult] = []
    name_to_indices: dict[str, list[int]] = {}
    for idx, f in enumerate(files):
        result, case = _lint_impl(path=f, source=None, file_label=f, known_apps=known_apps)
        results.append(result)
        if case is not None and case.name.strip():
            name_to_indices.setdefault(case.name.strip(), []).append(idx)

    for name, idxs in name_to_indices.items():
        if len(idxs) <= 1:
            continue
        for idx in idxs:
            results[idx].issues.append(LintIssue(
                code="W002", severity="warning",
                message=f"name {name!r} 在本次批量扫描范围内重复（另有 {len(idxs) - 1} 个文件同名）",
            ))

    return results
