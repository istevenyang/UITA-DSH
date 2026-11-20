"""core/lint/html_report.py — TCS lint 结果 → HTML 报告（纯函数，无 IO）。

风格参考 `core/result/archiver.py::generate_html_report()`（统计卡片 + 表格 +
badge 配色），但数据形状不同：本模块按文件分组，每文件下展开该文件的全部
`LintIssue`，而非按 case 展开单行。

不做任何文件 IO——落盘由调用方（`main.py::_handle_lint()`）负责，写入固定路径
`report/tcs_check/<YYYYMMDDHHMMSS>.html`，便于单测直接断言返回字符串内容
（对齐 R19：`core/` 单测不依赖真实文件系统副作用）。

计划文档：docs/plans/archive/06-skill-agent-实施纪要.md §3.3
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from uita.core.lint.tcs_linter import LintIssue, LintResult

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>UITA TCS 格式检查报告 — {run_time}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; padding: 24px; background: #f5f5f5; color: #333; }}
  h1 {{ font-size: 20px; margin-bottom: 8px; }}
  .summary {{ display: flex; gap: 24px; margin-bottom: 24px; }}
  .stat {{ background: white; border-radius: 8px; padding: 16px 24px; text-align: center;
           box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
  .stat .num {{ font-size: 32px; font-weight: bold; }}
  .passed {{ color: #22c55e; }}
  .failed {{ color: #ef4444; }}
  .warning {{ color: #f59e0b; }}
  .total  {{ color: #3b82f6; }}
  .file-block {{ background: white; border-radius: 8px; margin-bottom: 12px; padding: 12px 16px;
                 box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
  .file-header {{ display: flex; align-items: center; gap: 10px; font-size: 14px; }}
  .file-path {{ font-family: Consolas, Menlo, monospace; word-break: break-all; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 999px;
            font-size: 12px; font-weight: 600; }}
  .badge-passed {{ background: #dcfce7; color: #166534; }}
  .badge-failed {{ background: #fee2e2; color: #991b1b; }}
  .badge-error  {{ background: #fee2e2; color: #991b1b; }}
  .badge-warning {{ background: #fef3c7; color: #92400e; }}
  table.issues {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
  table.issues td {{ padding: 6px 8px; font-size: 13px; border-bottom: 1px solid #f3f4f6; vertical-align: top; }}
  .no-issue {{ color: #9ca3af; font-size: 13px; margin: 6px 0 0; }}
  code {{ background: #f3f4f6; padding: 1px 5px; border-radius: 4px; }}
</style>
</head>
<body>
<h1>UITA TCS 格式检查报告</h1>
<p style="color:#6b7280;font-size:13px">生成时间：{run_time}</p>
<div class="summary">
  <div class="stat"><div class="num total">{total}</div><div>扫描文件</div></div>
  <div class="stat"><div class="num passed">{passed}</div><div>通过</div></div>
  <div class="stat"><div class="num failed">{failed}</div><div>存在 Error</div></div>
  <div class="stat"><div class="num warning">{warning_only}</div><div>仅 Warning</div></div>
</div>
{blocks}
</body>
</html>
"""

_FILE_BLOCK_TEMPLATE = """\
<div class="file-block">
  <div class="file-header">
    <span class="badge badge-{status}">{status_label}</span>
    <span class="file-path">{file}</span>
  </div>
  {issues_html}
</div>\
"""

_ISSUE_ROW_TEMPLATE = (
    '<tr><td><span class="badge badge-{severity}">{code}</span></td>'
    "<td>{message}</td><td>{line}</td></tr>"
)


def _render_issue_row(issue: LintIssue) -> str:
    return _ISSUE_ROW_TEMPLATE.format(
        severity=issue.severity,
        code=escape(issue.code),
        message=escape(issue.message),
        line=(f"行 {issue.line}" if issue.line else ""),
    )


def _render_file_block(result: LintResult) -> str:
    status = "passed" if result.ok else "failed"
    status_label = "PASS" if result.ok else "FAIL"
    if result.issues:
        rows = "\n    ".join(_render_issue_row(i) for i in result.issues)
        issues_html = f'<table class="issues"><tbody>\n    {rows}\n  </tbody></table>'
    else:
        issues_html = '<p class="no-issue">无违规项</p>'
    return _FILE_BLOCK_TEMPLATE.format(
        file=escape(result.file), status=status, status_label=status_label,
        issues_html=issues_html,
    )


def render_html_report(results: list[LintResult]) -> str:
    """纯函数：`results` → 完整 HTML 字符串，不做任何文件 IO。"""
    total = len(results)
    passed = sum(1 for r in results if r.ok)
    failed = total - passed
    warning_only = sum(1 for r in results if r.ok and r.issues)

    blocks = "\n".join(_render_file_block(r) for r in results)
    if not blocks:
        blocks = '<p style="color:#6b7280">未扫描到任何 .tcs 文件</p>'

    return _HTML_TEMPLATE.format(
        run_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total=total, passed=passed, failed=failed, warning_only=warning_only,
        blocks=blocks,
    )
