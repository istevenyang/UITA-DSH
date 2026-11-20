"""--mode health-check：存量用例健康检查 — Dry-Run 验证，不启动引擎。

对全部 .tcs 用例做无副作用的健康评分：
  - 语法完整性（parse 是否通过）
  - 元素引用有效性（rosetta_map 中是否存在对应 key）
  - 定位完整性（元素有 selector/js 才算完整）
  - 健康评分：✅健康 / ⚠️有风险 / ❌已腐化

计划文档：docs/plans/archive/01-engine-lifecycle-实施纪要.md §用例腐化检测
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from uita.cli.modes.common import StartupError, _collect_tcs_files, _expand_cases_dir, _resolve_case_name_tokens

logger = logging.getLogger("uita.health")


@dataclass
class CaseHealth:
    """单条用例的健康评分。"""
    path: str
    app: str = ""
    case_name: str = ""
    score: int = 100           # 0-100，100=完全健康
    status: str = "✅"        # ✅ / ⚠️ / ❌
    parse_error: Optional[str] = None
    missing_elements: list[str] = field(default_factory=list)
    unlocatable_elements: list[str] = field(default_factory=list)
    total_elements: int = 0
    # window_hint 声明了但无法解析（非法值被运行时忽略）的元素；只报告，不影响评分
    invalid_window_hints: list[str] = field(default_factory=list)


# ── 元素提取 ────────────────────────────────────────────────────────
_ELEMENT_ACTIONS = {
    "click", "type", "hover", "press", "double_click", "right_click",
    "select", "check", "uncheck", "scroll_to", "focus", "drag",
    "drag_between", "upload_file",
}


def _extract_elements_from_ast(case_ast) -> list[str]:
    """从 CaseNode AST 中提取所有被引用的元素名。"""
    elements: list[str] = []

    for step in (case_ast.setup_steps + case_ast.steps + case_ast.teardown_steps):
        for node in step.body:
            node_type = type(node).__name__

            if node_type == "ActionNode":
                if node.action in _ELEMENT_ACTIONS and node.args:
                    elements.append(node.args[0])
                elif node.action in ("assert", "verify"):
                    # assert[x] / verify[x] 被解析为 ActionNode（不是 AssertNode）
                    if node.args:
                        elements.append(node.args[0])

            elif node_type == "AssertNode":
                if node.target:
                    # assert: [x] 格式 → target 即为元素名
                    elements.append(node.target)
                elif node.kwargs:
                    kw = node.kwargs
                    for k in ("js", "in", "of"):
                        val = kw.get(k, "")
                        if val and not val.startswith("$"):
                            elements.append(val)

            elif node_type == "WaitNode":
                if node.target:
                    elements.append(node.target)

            elif node_type == "SetNode":
                if node.source_args:
                    elements.append(node.source_args[0])

            elif node_type == "IfNode":
                for condition_node, branch_body in getattr(node, "branches", []):
                    for sub_node in branch_body:
                        sn_type = type(sub_node).__name__
                        if sn_type == "ActionNode" and sub_node.action in _ELEMENT_ACTIONS and sub_node.args:
                            elements.append(sub_node.args[0])
                for sub_node in getattr(node, "else_body", []):
                    sn_type = type(sub_node).__name__
                    if sn_type == "ActionNode" and sub_node.action in _ELEMENT_ACTIONS and sub_node.args:
                        elements.append(sub_node.args[0])

    # 去重 + 过滤空/纯数字/纯符号
    seen = set()
    result = []
    for e in elements:
        e = e.strip()
        if not e or e in seen:
            continue
        if re.match(r'^[\d\.]+$', e):  # 纯数字 wait[5s] 的 5s
            continue
        if e.startswith("$"):
            continue
        seen.add(e)
        result.append(e)
    return result


# ── rosetta_map 加载 ────────────────────────────────────────────────

# 已知字段名：用于判断一个 dict 是"元素"（含这些 key）还是"页面/app 容器"（不含）。
# 对齐 core/platform/element_map_loader.py::_looks_like_element 的判定思路。
_ELEMENT_FIELD_KEYS = {"mac", "win", "selector", "js", "usage"}


def _looks_like_element_dict(value: dict) -> bool:
    """value 自身的 key 命中已知定位/usage 字段 → 判定为"元素"而非"页面/app"容器。"""
    return bool(_ELEMENT_FIELD_KEYS & value.keys())


def _load_rosetta_map(app_name: str) -> dict[str, dict]:
    """加载 rosetta_map，返回 {element_name: {page, mac, win, selector, js, usage}} 扁平字典。

    rosetta_map.json 存在两种根级结构（可能混用，逐 root_key 独立判定，对齐
    element_map_loader.py 的自动识别逻辑，而非硬编码层级）：
      - v3 规范三层：{_meta, <app>: {<page>: {<element>: {mac,...}}}}（如 sample_web/sample_media）
      - 历史遗留两层：{_meta, <page>: {<element>: {mac,...}}}（如 chat_app，无 app 外层）
    此前实现硬编码假设两层，遇三层结构时会把 app 名当页面、页面名当元素名，
    导致三层结构 app 的全部元素校验失效（全量误报缺失）。
    """
    # apps/ 为业务资产（WORKSPACE_DIR），非框架包安装位置，pip 场景下二者不同。
    from uita.config import WORKSPACE_DIR

    rpath = os.path.join(WORKSPACE_DIR, "apps", app_name, "rosetta_map.json")
    if not os.path.isfile(rpath):
        if os.path.isdir(os.path.join(WORKSPACE_DIR, "apps", app_name)):
            logger.debug("[health-check] app=%s 无 rosetta_map.json，跳过元素校验", app_name)
            return {}
        return {}

    with open(rpath, encoding="utf-8") as f:
        data = json.load(f)

    flat: dict[str, dict] = {}

    def _register(elem_name: str, page_name: str, elem_val: dict) -> None:
        flat[elem_name] = {
            "page": page_name,
            "mac": elem_val.get("mac", ""),
            "win": elem_val.get("win", ""),
            # qt：QtProbe Qt 探针通道（L5 QT_PROBE），与 win/mac 并列，缺省会
            # 把仅有 qt 定位的元素误判为"定位信息缺失"。
            "qt": elem_val.get("qt", ""),
            "selector": elem_val.get("selector", ""),
            "js": elem_val.get("js", ""),
            "usage": elem_val.get("usage", ""),
            # window_hint 原始声明，供 health-check 校验合法性（解析见 core/platform/window_hint.py）
            "window_hint": elem_val.get("window_hint"),
        }

    for root_key, root_val in data.items():
        if root_key.startswith("_"):
            continue
        if not isinstance(root_val, dict):
            continue

        if _looks_like_element_dict(root_val):
            # 平铺结构：root_key 本身就是元素名（极少见，兜底兼容）
            _register(root_key, page_name="", elem_val=root_val)
            continue

        # root_val 不是元素本身，其子级要么是「元素」（两层：page→element），
        # 要么是「页面」（三层：app→page→element）——逐一检查子级判定层级。
        is_page_level = any(
            isinstance(v, dict) and _looks_like_element_dict(v)
            for v in root_val.values()
        )
        if is_page_level:
            # 两层：root_key = 页面名，子级 = 元素
            for elem_name, elem_val in root_val.items():
                if elem_name.startswith("_") or not isinstance(elem_val, dict):
                    continue
                _register(elem_name, page_name=root_key, elem_val=elem_val)
        else:
            # 三层：root_key = app 名，子级 = 页面 → 元素
            for page_name, page_val in root_val.items():
                if page_name.startswith("_") or not isinstance(page_val, dict):
                    continue
                for elem_name, elem_val in page_val.items():
                    if elem_name.startswith("_") or not isinstance(elem_val, dict):
                        continue
                    _register(elem_name, page_name=page_name, elem_val=elem_val)

    return flat


def _has_invalid_window_hint(el: dict) -> bool:
    """声明了 `window_hint` 但无法解析为合法窗口提示（运行时将被忽略）。"""
    from uita.core.platform.window_hint import parse_window_hint

    raw = el.get("window_hint")
    if raw is None or raw == "" or raw == {}:
        return False
    return parse_window_hint(raw) is None


def _is_locatable(el: dict) -> bool:
    """元素是否有可用定位信息（mac/win/qt/selector/js 任一非空）。

    `qt` 为 QtProbe Qt 探针通道路径（L5 QT_PROBE 策略消费），与 `win`/`mac`
    并列；仅有 `qt` 的元素（如轻页面族在伴生进程内、UIA 不可达的控件）同样
    是可定位元素，不能判为"定位信息缺失"。见 docs/plans/
    20261116203324-win-qt-probe-channel-plan.md。
    """
    return bool(
        el.get("mac", "").strip()
        or el.get("win", "").strip()
        or el.get("qt", "").strip()
        or el.get("selector", "").strip()
        or el.get("js", "").strip()
    )


# ── 健康检查主逻辑 ──────────────────────────────────────────────────

def _check_case(tcs_path: str) -> CaseHealth:
    """对单条 .tcs 文件执行健康检查。"""
    from uita.core.dsl.parser import parse_file

    h = CaseHealth(path=tcs_path)

    # ① 语法检查
    try:
        ast = parse_file(tcs_path)
        h.case_name = ast.name
        h.app = ast.app
    except Exception as exc:
        h.score = 0
        h.status = "❌"
        h.case_name = os.path.basename(tcs_path)
        h.parse_error = str(exc)
        return h

    # ② 提取元素引用
    elements = _extract_elements_from_ast(ast)
    h.total_elements = len(elements)

    if not elements:
        # 无用例元素引用（纯坐标操作或空步骤）
        h.score = 100
        h.status = "✅"
        return h

    # ③ 加载 rosetta_map
    if not h.app:
        h.score = 70
        h.status = "⚠️"
        return h
    rosetta = _load_rosetta_map(h.app)

    # ④ 交叉验证
    if not rosetta:
        h.score = 85
        h.status = "⚠️"
        h.parse_error = f"rosetta_map.json for app={h.app} not found or empty"
        return h

    for el_name in elements:
        if el_name not in rosetta:
            h.missing_elements.append(el_name)
        elif not _is_locatable(rosetta[el_name]):
            h.unlocatable_elements.append(el_name)
        if el_name in rosetta and _has_invalid_window_hint(rosetta[el_name]):
            h.invalid_window_hints.append(el_name)
    # ⑤ 评分
    if h.missing_elements and not h.unlocatable_elements:
        ratio = 1.0 - len(h.missing_elements) / len(elements)
        h.score = max(0, int(ratio * 80 + 20 * (1 - min(ratio, 0.5))))
        h.status = "❌" if ratio < 0.5 else "⚠️"
    elif h.unlocatable_elements:
        ratio = 1.0 - len(h.unlocatable_elements) / len(elements)
        h.score = max(30, int(ratio * 70 + 30))
        h.status = "❌" if ratio < 0.5 else "⚠️"
    else:
        h.score = 100
        h.status = "✅"

    return h


def _scan_tcs_files(root: str) -> list[str]:
    """递归扫描目录下所有 .tcs 文件，按文件名排序返回绝对路径列表。"""
    result: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if fname.endswith(".tcs"):
                result.append(os.path.join(dirpath, fname))
    return sorted(result)


def _handle_health_check(cli) -> int:
    """health-check 模式入口：扫描 .tcs 文件并输出健康报告。"""
    tcs_files: list[str] = []
    raw = list(getattr(cli, "cases_dir", None) or ["cases"])
    app = (getattr(cli, "app", "") or "").strip()

    if raw != ["cases"]:
        # 显式传入 --cases-dir/--case-dir：支持目录/单文件/裸用例名（--file 已废弃移除）
        tokens = _resolve_case_name_tokens(_expand_cases_dir(raw), app)
        tcs_files = _collect_tcs_files(tokens)
    elif app:
        from uita.config import ROOT_DIR
        cases_dir = os.path.join(ROOT_DIR, "cases", app)
        if os.path.isdir(cases_dir):
            tcs_files = _scan_tcs_files(cases_dir)
    else:
        raise StartupError("health-check 模式需要 --cases-dir/--case-dir 或 --app 参数")

    if not tcs_files:
        print("未找到 .tcs 文件")
        return 0

    # 逐条检查
    results: list[CaseHealth] = []
    for fpath in sorted(tcs_files):
        try:
            h = _check_case(fpath)
        except Exception as exc:
            h = CaseHealth(path=fpath, case_name=os.path.basename(fpath), score=0, status="❌", parse_error=str(exc))
        results.append(h)

    # 打印报告
    _print_report(results)

    return 0


def _print_report(results: list[CaseHealth]) -> None:
    """打印健康报告表格。"""
    total = len(results)
    healthy = sum(1 for r in results if r.status == "✅")
    warn = sum(1 for r in results if r.status == "⚠️")
    rot = sum(1 for r in results if r.status == "❌")

    print(f"\n{'='*80}")
    print(f"  用例健康报告")
    print(f"{'='*80}")
    print(f"  总计: {total}  ✅ 健康: {healthy}  ⚠️ 有风险: {warn}  ❌ 已腐化: {rot}")
    print(f"{'='*80}\n")

    if total == 0:
        print("  无 .tcs 文件")
        return

    # 表头
    print(f"  {'case':<40s} {'score':>5s} {'status':>4s}  issues")
    print(f"  {'─'*40} {'─'*5} {'─'*4}  {'─'*50}")
    for r in results:
        name = r.case_name or os.path.basename(r.path)
        issues: list[str] = []
        if r.parse_error:
            issues.append(f"parse: {r.parse_error[:60]}")
        for el in r.missing_elements[:3]:
            issues.append(f"缺元素: {el}")
        for el in r.unlocatable_elements[:3]:
            issues.append(f"无定位: {el}")
        more_issues = ""
        if len(r.missing_elements) > 3:
            more_issues += f" +{len(r.missing_elements) - 3}更多缺失"
        if len(r.unlocatable_elements) > 3:
            more_issues += f" +{len(r.unlocatable_elements) - 3}更多无定位"

        issue_str = ", ".join(issues) + (more_issues if more_issues else "")
        print(f"  {name:<40s} {r.score:>5d} {r.status:>4s}  {issue_str or '—'}")

    # 详情展开（仅 ⚠️ 和 ❌ 的 case）
    problematic = [r for r in results if r.status in ("⚠️", "❌")]
    if problematic:
        print(f"\n{'─'*80}")
        print(f"  腐化/风险详情")
        print(f"{'─'*80}")
        for r in problematic:
            name = r.case_name or os.path.basename(r.path)
            print(f"\n  [{r.status}] {name}  ({r.path})")
            if r.parse_error:
                print(f"         ❌ 语法错误: {r.parse_error}")
            if r.missing_elements:
                print(f"         ⚠️ rosetta_map 缺失元素 ({len(r.missing_elements)}):")
                for el in r.missing_elements:
                    print(f"            - {el}")
            if r.unlocatable_elements:
                print(f"         ⚠️ 定位信息缺失 ({len(r.unlocatable_elements)}):")
                for el in r.unlocatable_elements:
                    print(f"            - {el}")
            if r.invalid_window_hints:
                print(f"         ⚠️ window_hint 非法（运行时被忽略）({len(r.invalid_window_hints)}):")
                for el in r.invalid_window_hints:
                    print(f"            - {el}")
            if r.total_elements == 0 and not r.parse_error:
                print(f"         ⚠️ 无元素引用（可能是纯坐标操作或空 step）")
