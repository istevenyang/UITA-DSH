"""
Archiver — 从 summary.json 生成 HTML 批次报告骨架。

输出：report_dir/report.html

2026-08-07 更新：非 passed（failed/error）的 case 追加渲染失败步骤名称/错误
信息/失败截图，复用 core/result/pages_report_render.py::_render_failed_steps()
（同包内部函数，已有完整实现，不重复实现，见
docs/plans/archive/04-report_portal-report-实施纪要.md）。
"""
from __future__ import annotations

import html as _html
import json
import os
from datetime import datetime


def _render_failed_steps(result_path: str) -> str:
    """读取单条用例的 result.json，渲染失败步骤摘要 HTML。"""
    if not result_path or not os.path.isfile(result_path):
        return '<div class="step"><div class="step-error">未找到用例结果文件</div></div>'
    try:
        with open(result_path, encoding="utf-8") as f:
            res = json.load(f)
    except Exception as exc:
        return f'<div class="step"><div class="step-error">读取结果文件失败: {_html.escape(str(exc))}</div></div>'

    parts: list[str] = []
    for step in res.get("steps", []):
        if step.get("status") in ("passed", "skipped"):
            continue
        sname = _html.escape(str(step.get("name") or step.get("step_name") or "未命名步骤"))
        serr = _html.escape(str(step.get("error") or step.get("message") or "执行失败"))
        parts.append(f'<div class="step"><div class="step-name">{sname}</div><div class="step-error">{serr}</div></div>')
    if not parts:
        err_msg = _html.escape(str(res.get("error") or res.get("message") or "用例未通过"))
        parts.append(f'<div class="step"><div class="step-error">{err_msg}</div></div>')
    return "".join(parts)


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>UITA 批次报告 — {run_time}</title>
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
  .total  {{ color: #3b82f6; }}
  table {{ width: 100%; border-collapse: collapse; background: white;
           border-radius: 8px; overflow: hidden;
           box-shadow: 0 1px 4px rgba(0,0,0,.1); }}
  th {{ background: #f9fafb; text-align: left; padding: 10px 16px;
        font-size: 13px; color: #6b7280; border-bottom: 1px solid #e5e7eb; }}
  td {{ padding: 10px 16px; font-size: 14px; border-bottom: 1px solid #f3f4f6; }}
  tr:last-child td {{ border-bottom: none; }}
  .badge {{ display: inline-block; padding: 2px 10px; border-radius: 999px;
            font-size: 12px; font-weight: 600; }}
  .badge-passed {{ background: #dcfce7; color: #166534; }}
  .badge-failed {{ background: #fee2e2; color: #991b1b; }}
  .badge-error  {{ background: #fef3c7; color: #92400e; }}
  .step {{ border-left: 3px solid #ef4444; margin: 4px 0; padding: 4px 0 4px 12px; }}
  .step .step-name {{ font-size: 13px; font-weight: 600; color: #374151; }}
  .step .step-error {{ font-size: 13px; color: #b91c1c; margin: 4px 0; white-space: pre-wrap; }}
  .step img {{ max-width: 100%; border: 1px solid #e5e7eb; border-radius: 4px; margin-top: 6px; }}
</style>
</head>
<body>
<h1>UITA 批次报告</h1>
<p style="color:#6b7280;font-size:13px">生成时间：{run_time} &nbsp;|&nbsp; 耗时：{duration_s:.1f}s</p>
<div class="summary">
  <div class="stat"><div class="num total">{total}</div><div>总计</div></div>
  <div class="stat"><div class="num passed">{passed}</div><div>通过</div></div>
  <div class="stat"><div class="num failed">{failed}</div><div>失败</div></div>
</div>
<table>
  <thead><tr><th>#</th><th>用例名</th><th>状态</th><th>耗时 (ms)</th></tr></thead>
  <tbody>
{rows}
  </tbody>
</table>
</body>
</html>
"""

_ROW_TEMPLATE = (
    '    <tr><td>{idx}</td><td>{name}</td>'
    '<td><span class="badge badge-{status}">{status}</span></td>'
    '<td>{duration_ms}</td></tr>'
)

_STEPS_ROW_TEMPLATE = '    <tr><td colspan="4">{steps_html}</td></tr>'


def generate_html_report(summary_path: str) -> str:
    """从 summary.json 生成report.html，返回 report.html 路径。

    每条 case 一行（#/用例名/状态/耗时）；非 passed 的 case 额外追加一行，
    展示失败步骤名称/错误信息/失败截图（_render_failed_steps 读取
    case.result_path 对应的 result.json，文件缺失/解析失败均有降级占位文案）。
    """
    with open(summary_path, encoding="utf-8") as f:
        data = json.load(f)

    row_parts: list[str] = []
    for i, c in enumerate(data.get("cases", [])):
        status = c.get("status", "unknown")
        row_parts.append(
            _ROW_TEMPLATE.format(
                idx=i + 1,
                name=c.get("name", ""),
                status=status,
                duration_ms=c.get("duration_ms", 0),
            )
        )
        if status != "passed":
            steps_html = _render_failed_steps(c.get("result_path", ""))
            row_parts.append(_STEPS_ROW_TEMPLATE.format(steps_html=steps_html))
    rows = "\n".join(row_parts)

    html = _HTML_TEMPLATE.format(
        run_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        duration_s=data.get("duration_ms", 0) / 1000,
        total=data.get("total", 0),
        passed=data.get("passed", 0),
        failed=data.get("failed", 0),
        rows=rows,
    )

    report_path = os.path.join(os.path.dirname(summary_path), "report.html")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html)
    return report_path
