"""--mode lint：TCS/Rosetta 只读检查，生成 HTML/JSON 报告，退出码始终 0。"""
from __future__ import annotations

import os

from uita.cli.modes.common import _expand_cases_dir


def _scan_known_apps() -> set[str]:
    """扫描 apps/ 目录下含 register.py 的子目录名，供 lint 模式 E006 使用。

    与 `uita.__main__::_auto_discover()` 同款纯目录扫描逻辑（无 import 副作用，
    只看文件是否存在）——修改需同步（见计划文档 §五 风险评估）。

    注意：必须用 `WORKSPACE_DIR`（业务资产根目录）而非 `ROOT_DIR`/`PKG_DIR`
    （框架包安装位置）。pip 安装场景下二者不同——`ROOT_DIR` 指向
    site-packages/uita/，其下没有 apps/，若误用会导致 E006 对所有 app 误报
    "未注册"（2026-08-14 端到端打包验证发现的缺陷，`_auto_discover()`/
    `app_discovery.py::discover_app_aliases()` 早已改用 `WORKSPACE_DIR`，
    本函数此前遗漏同步）。
    """
    from uita.config import WORKSPACE_DIR

    known: set[str] = set()
    apps_root = os.path.join(WORKSPACE_DIR, "apps")
    if os.path.isdir(apps_root):
        for app_name in os.listdir(apps_root):
            reg = os.path.join(apps_root, app_name, "register.py")
            if os.path.isfile(reg):
                known.add(app_name)
    return known


def _handle_lint(cli) -> int:
    """批量/单文件检查 TCS 或 Rosetta，生成 HTML 与机器可读 JSON。

    退出码始终 0：检查结果通过 stdout 摘要、HTML 与 JSON 呈现，
    不影响调用方（CI）的退出码判断；异常仅在 linter 自身崩溃时才会向上抛出，
    走 main.py 现有 `_entrypoint()` 兜底（exit 1），与"发现了违规"语义不同。

    计划文档：docs/plans/archive/06-skill-agent-实施纪要.md
    """
    import json
    from dataclasses import asdict
    from datetime import datetime

    from uita.core.lint.html_report import render_html_report
    from uita.config import REPORT_DIR, WORKSPACE_DIR

    target = getattr(cli, "lint_target", "tcs")
    paths = _expand_cases_dir(cli.cases_dir)
    if target == "rosetta":
        from uita.core.lint.rosetta_linter import lint_paths

        if paths == ["cases"]:
            app = getattr(cli, "app", "")
            apps_root = os.path.join(WORKSPACE_DIR, "apps")
            if app:
                if app not in _scan_known_apps():
                    from uita.core.lint.tcs_linter import LintIssue, LintResult

                    results = [LintResult(file=app, ok=False, issues=[LintIssue(
                        code="E103", severity="error", message=f"app {app!r} 未注册",
                    )])]
                else:
                    results = lint_paths([os.path.join(apps_root, app, "rosetta_map.json")])
            else:
                results = lint_paths([apps_root])
        else:
            results = lint_paths(paths)
    else:
        from uita.core.lint.tcs_linter import lint_paths

        results = lint_paths(paths, known_apps=_scan_known_apps())

    total = len(results)
    passed = sum(1 for r in results if r.ok)
    failed = total - passed
    error_count = sum(1 for r in results for i in r.issues if i.severity == "error")
    warning_count = sum(1 for r in results for i in r.issues if i.severity == "warning")

    print(f"\n[lint] 扫描 {total} 个文件：{passed} 通过，{failed} 存在 error")
    for r in results:
        icon = "\u2705 PASS" if r.ok else "\u274c FAIL"
        print(f"  {icon}  {r.file}")
        for issue in r.issues:
            tag = "ERROR" if issue.severity == "error" else "WARN "
            loc = f"（行 {issue.line}）" if issue.line else ""
            print(f"      [{tag}][{issue.code}] {issue.message}{loc}")

    html = render_html_report(results)
    if target == "rosetta":
        html = html.replace("UITA TCS 格式检查报告", "UITA Rosetta 静态检查报告", 2)
        html = html.replace("未扫描到任何 .tcs 文件", "未扫描到任何正式 rosetta_map.json 文件")
    report_dir = os.path.join(REPORT_DIR, f"{target}_check")
    os.makedirs(report_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f" if target == "rosetta" else "%Y%m%d%H%M%S")
    report_path = os.path.join(report_dir, f"{stamp}.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    json_path = os.path.join(report_dir, f"{stamp}.json")
    payload = {
        "schema_version": "1.0", "lint_target": target,
        "summary": {"total": total, "passed": passed, "failed": failed,
                    "errors": error_count, "warnings": warning_count},
        "results": [asdict(result) for result in results],
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")

    print(f"\n总计: {total} files, {error_count} errors, {warning_count} warnings")
    print(f"Report \u2192 {report_path}")
    print(f"JSON \u2192 {json_path}")
    return 0
