"""perf AI 分析：兜底报告（模型未产出时的确定性渲染）。

定位：**保证"跑完必有报告"**。无头会话可能因卡死、超时、等人决策、模型报错而没有落下
任何产物；此时用事实包（``facts.json``）确定性渲染一份体例合规的 HTML——不推断、不编数，
每个必覆盖 fact 一行（``data-fact-id`` + 逐字取值 + 状态），结论列统一标注
「框架兜底：未由模型分析」。

纪律：

- **只用事实包**：读数逐字来自 ``facts.json`` 的 ``display``，与正常报告同一数值锚；
- **9 个章节 id 与正常报告一致**（``reading`` … ``appb``），``verify`` 同一套校验可过；
- **不冒充分析结论**：头部 banner 与各节正文都写明"本次为框架兜底、模型分析未产出"，
  并给出未产出原因（``cause``）；缺章节/缺覆盖由「附C 框架校验」如实标注；
- 不抛异常给调用方（写入失败只记日志），失败即由编排层按"无报告"收尾。
"""
from __future__ import annotations

import html
import json
import logging
import os
from typing import Any, Iterable

logger = logging.getLogger("uita")

__all__ = ["build_fallback_html", "write_fallback_report"]

#: 章节顺序（与正常报告一致；``appc`` 由校验层注入，不在这里写）
_SECTIONS = (
    ("reading", "读表约定（先看这里）"),
    ("sources", "一、数据源与可比性"),
    ("overall", "二、总体健康度"),
    ("scenarios", "三、分场景分析"),
    ("issues", "四、问题清单"),
    ("actions", "五、建议行动"),
    ("questions", "六、待澄清问题"),
    ("appa", "附A 采集口径"),
    ("appb", "附B 数据质量"),
)

_FALLBACK_MARK = "data-fallback-report"

_CSS = """
body{font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
margin:0 auto;max-width:1180px;padding:24px 28px 64px;color:#1f2328;line-height:1.6}
h1{font-size:24px;margin:0 0 6px}h2{font-size:19px;margin:32px 0 12px;padding-bottom:6px;
border-bottom:1px solid #d8dee4}h3{font-size:15px;margin:20px 0 8px;color:#30363d}
table{border-collapse:collapse;width:100%;margin:10px 0 18px;font-size:13px}
th,td{border:1px solid #d8dee4;padding:6px 9px;text-align:left;vertical-align:top}
th{background:#f6f8fa;font-weight:600}td.num{font-variant-numeric:tabular-nums}
code{background:#f6f8fa;padding:1px 4px;border-radius:4px;font-size:12px}
.fallback-banner{margin:16px 0;padding:12px 16px;border:1px solid #a1260d;
border-left:6px solid #a1260d;border-radius:6px;background:#fff5f2;font-size:14px}
.muted{color:#57606a;font-size:12px}
"""


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _facts_of(facts_doc: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in (facts_doc.get("facts") or []) if isinstance(f, dict)]


def _cell(fact: dict[str, Any]) -> str:
    """取值格子：``data-fact-id`` 与逐字读数同格（校验按标记邻域比对读数）。"""
    return (f'<td class="num" data-fact-id="{_escape(fact.get("fact_id"))}">'
            f'{_escape(fact.get("display"))}</td>')


def _table(facts: Iterable[dict[str, Any]], *, with_tier: bool = False) -> str:
    head = ["含义"]
    if with_tier:
        head += ["档位", "轮次"]
    head += ["取值", "单位", "状态", "备注"]
    rows = []
    for fact in facts:
        row = [f'<td>{_escape(fact.get("zh"))}</td>']
        if with_tier:
            row += [f'<td>{_escape(fact.get("tier"))}</td>',
                    f'<td>{_escape(fact.get("round"))}</td>']
        row += [_cell(fact), f'<td>{_escape(fact.get("unit"))}</td>',
                f'<td>{_escape(fact.get("data_status"))}</td>',
                f'<td>{_escape(fact.get("gap"))}</td>']
        rows.append("<tr>" + "".join(row) + "</tr>")
    if not rows:
        return '<p class="muted">（本批次该层无数据）</p>'
    header = "".join(f"<th>{title}</th>" for title in head)
    return (f"<table><thead><tr>{header}</tr></thead><tbody>"
            + "".join(rows) + "</tbody></table>")


def _group_by_scenario(facts: Iterable[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        key = str(fact.get("scenario") or fact.get("case") or "全局 / 非场景")
        groups.setdefault(key, []).append(fact)
    return list(groups.items())


def _calibers(facts_doc: dict[str, Any], limit: int = 0) -> str:
    calibers = [c for c in (facts_doc.get("calibers") or []) if isinstance(c, dict)]
    if limit:
        calibers = calibers[:limit]
    if not calibers:
        return '<p class="muted">（本批次未登记口径词典）</p>'
    rows = "".join(
        f"<tr><td><code>{_escape(item.get('id'))}</code></td>"
        f"<td>{_escape(item.get('zh'))}</td><td>{_escape(item.get('text'))}</td></tr>"
        for item in calibers
    )
    return ("<table><thead><tr><th>口径 id</th><th>名称</th><th>说明</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>")


def _banner(cause: str, facts_doc: dict[str, Any]) -> str:
    return (
        f'<div {_FALLBACK_MARK}="1" class="fallback-banner">'
        "<strong>框架兜底报告：本次模型分析未产出</strong><br>"
        f"未产出原因：<code>{_escape(cause)}</code>；"
        f"应用 / 平台：{_escape(facts_doc.get('app'))} / {_escape(facts_doc.get('platform'))}；"
        f"档位：{_escape('、'.join(facts_doc.get('tiers') or []) or '—')}。<br>"
        "本报告由事实包确定性渲染：每条数据都有逐字取值与状态，但**没有模型给出的分析结论**"
        "（问题分级 / 建议 / 归因均为空缺，需重跑分析或人工补齐）。"
        "</div>"
    )


def build_fallback_html(
    facts_doc: dict[str, Any], *, cause: str = "no-report", model: str = "",
    session_id: str = "", generated_at: str = "",
) -> str:
    """用事实包渲染一份体例合规的兜底报告（纯函数，不写文件）。"""
    facts = _facts_of(facts_doc)
    required = [f for f in facts if f.get("required")]
    coverage = facts_doc.get("coverage") or {}
    by_scope: dict[str, list[dict[str, Any]]] = {}
    for fact in required:
        scope = str(fact.get("scope") or "other")
        by_scope.setdefault(scope, []).append(fact)

    bad = [f for f in required if str(f.get("data_status") or "ok") != "ok"]
    sources = by_scope.get("meta", []) + by_scope.get("host", [])
    overall = by_scope.get("health", []) + by_scope.get("compare", [])
    placed = {f.get("fact_id") for f in sources} | {f.get("fact_id") for f in overall}
    scenario_facts = [f for f in required if f.get("fact_id") not in placed]

    blocks: dict[str, str] = {}
    blocks["reading"] = (
        "<p>本报告为<strong>框架兜底产物</strong>：读数逐字取自事实包 "
        "<code>perf_ai_analysis/facts.json</code>，未经模型二次加工。"
        "表格约定：<code>均值 / 最小值 / 最大值</code> 为跨轮聚合；<code>—</code> 表示该档该指标"
        "无有效数据（不是 0），原因见「附B 数据质量」与「附C 框架校验」。</p>"
        + _calibers(facts_doc, limit=6)
    )
    blocks["sources"] = (
        f"<p>批次目录：<code>{_escape(facts_doc.get('run_dir'))}</code>；模式："
        f"{_escape(facts_doc.get('mode'))}；生成时间：{_escape(facts_doc.get('generated_at'))}；"
        f"兜底渲染时间：{_escape(generated_at)}；模型：<code>{_escape(model or '未提供')}</code>；"
        f"会话：<code>{_escape(session_id or '未提供')}</code>。</p>"
        f'<p class="muted">模型分析未产出原因：<code>{_escape(cause)}</code>。'
        "因此本报告<b>不含</b>跨档趋势判定、问题分级与建议。</p>"
        + _table(sources)
    )
    blocks["overall"] = (
        '<p class="muted">本节为读数汇总，未做健康度判定（模型分析未产出）。</p>'
        + _table(overall)
    )
    scenario_html = ['<p class="muted">本节按场景列出逐条读数，未做根因分析（模型分析未产出）。</p>']
    for name, group in _group_by_scenario(scenario_facts):
        scenario_html.append(f"<h3>{_escape(name)}</h3>{_table(group, with_tier=True)}")
    blocks["scenarios"] = "".join(scenario_html)
    blocks["issues"] = (
        '<p class="muted">本节无内容：问题分级需要模型分析，本次未产出。'
        "下列为数据状态异常的读数（可作为人工排查线索）：</p>"
        + _table(bad)
    )
    blocks["actions"] = (
        "<ol><li>重跑分析：<code>uita --mode perf --app "
        f"{_escape(facts_doc.get('app'))} --perf-ai-run-dir "
        f"{_escape(facts_doc.get('run_dir'))} --perf-ai-timeout-s 3600</code>（产物已落盘，不必重跑压测）；</li>"
        "<li>或按本报告读数人工补齐结论（每条读数都已带 <code>data-fact-id</code>，可直接对照事实包）；</li>"
        "<li>若反复兜底，先看 <code>perf_ai_analysis/agent_run.json</code> 的 "
        "<code>error</code> / <code>stalled</code> / <code>loop_signature</code> 定位是环境还是提示词问题。</li></ol>"
    )
    blocks["questions"] = (
        "<p>需要人工确认的读数（数据状态非 ok）：</p>"
        + _table(bad)
        + "<p>另需确认：本次为何没有模型分析结论（卡死 / 超时 / 鉴权 / 模型报错），"
        "对应证据见 <code>perf_ai_analysis/agent_run.json</code> 与同目录 RAW 输出。</p>"
    )
    blocks["appa"] = _calibers(facts_doc)
    blocks["appb"] = (
        f"<p>事实规模：共 {_escape(coverage.get('total'))} 条，必覆盖 "
        f"{_escape(coverage.get('required'))} 条；按状态："
        f"<code>{_escape(json.dumps(coverage.get('by_status') or {}, ensure_ascii=False))}</code>。</p>"
        f"<p>状态非 ok 的必覆盖读数共 {len(bad)} 条，逐条列在「六、待澄清问题」；"
        "缺失原因以事实包 <code>gap</code> 字段为准，本报告不推测。</p>"
        "<p>已知口径限制：兜底报告不含跨批对比结论、不含周期性趋势判断、不含优先级排序。</p>"
    )

    body = [
        "<!DOCTYPE html>",
        '<html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>性能分析报告（框架兜底） - {_escape(facts_doc.get('app'))}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>性能分析报告（框架兜底）</h1>",
        f'<p class="muted">{_escape(facts_doc.get("app"))} / '
        f'{_escape(facts_doc.get("platform"))} / 档位 '
        f'{_escape("、".join(facts_doc.get("tiers") or []) or "—")}</p>',
        _banner(cause, facts_doc),
    ]
    for section_id, title in _SECTIONS:
        body.append(f'<section id="{section_id}"><h2>{_escape(title)}</h2>')
        body.append(blocks.get(section_id, ""))
        body.append("</section>")
    body += [
        f"<hr><p class=\"muted\">框架兜底报告 · 生成于 {_escape(generated_at)} · "
        f"模型 {_escape(model or '未提供')} · 未产出原因 {_escape(cause)}</p>",
        "</body></html>",
    ]
    return "\n".join(body)


def write_fallback_report(path: str, facts_doc: dict[str, Any], **kwargs: Any) -> str:
    """渲染并写入兜底报告（返回路径；写失败只记日志并抛给调用方判定）。"""
    content = build_fallback_html(facts_doc, **kwargs)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    logger.info("[perf-ai] 兜底报告已写入：%s（%d 字符）", path, len(content))
    return path
