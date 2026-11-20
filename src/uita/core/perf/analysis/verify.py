"""perf AI 分析：事后校验（把"漏了哪些数据"变成可执行的门禁信号）。

用户口径（2026-10-15）：**所有的数据都要有分析结论**；缺口要**显式化但不阻断**
（不因缺结论把 perf 判失败）。于是本模块做三件事：

1. **章节完整性**：报告必须含约定的章节 id（``reading``/``sources``/``overall``/
   ``scenarios``/``issues``/``actions``/``questions``/``appa``/``appb``）——缺章说明体例没走完；
2. **覆盖校验**：扫描 HTML 里的 ``data-fact-id="…"`` 标记，与 ``facts.json`` 的**必覆盖层**
   求差集 → 「结论覆盖 N/M（缺口 K）」；
3. **读数校验**：报告里出现的数字必须能在 facts 里找到对应读数（反向抽查：
   有 fact 却**一次都没出现**的关键读数记为"疑似未引用"，用于发现幻觉/漏项）。

然后把结果**注入报告**：头部 banner（读者第一眼看到覆盖情况）+ 末尾「附C 框架校验（自动追加）」。
注入失败（HTML 结构异常 / 无 ``</body>``）只打印，不改文件——**产物优先于装饰**。

一切判定都不抛异常：分析报告是"尽力而为的增量产物"，不能反过来阻断 perf 主流程。
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger("uita")

__all__ = [
    "VerifyResult",
    "build_gap_block",
    "inject_gap_block",
    "section_title",
    "verify_report",
]

#: 章节 id → 中文名（缺口提示里用中文，读者不需要认识 id）
SECTION_TITLES: dict[str, str] = {
    "reading": "读表约定",
    "sources": "一、数据源与可比性",
    "overall": "二、总体健康度",
    "scenarios": "三、分场景分析",
    "issues": "四、问题清单",
    "actions": "五、建议行动",
    "questions": "六、待澄清问题",
    "appa": "附A 采集口径",
    "appb": "附B 数据质量",
    "appc": "附C 框架校验",
}

_FACT_ID_RE = re.compile(r"""data-fact-id\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_ID_ATTR_RE = re.compile(r"""\bid\s*=\s*["']([A-Za-z0-9_\-]+)["']""")
_BODY_RE = re.compile(r"<body[^>]*>", re.IGNORECASE)
_BODY_END_RE = re.compile(r"</body\s*>", re.IGNORECASE)
_VERIFY_MARK = "data-framework-verify"

#: 数字校验的最小可比长度：短数字（0/1/2/8…）在 HTML 里到处都有，比对没有意义
_MIN_DIGIT_LEN = 4


@dataclass
class VerifyResult:
    """校验结果（可直接 json.dump）。"""

    html_path: str = ""
    status: str = "unreadable"          # ok | gaps | unreadable（读不到/空文件）
    sections_found: list[str] = field(default_factory=list)
    sections_missing: list[str] = field(default_factory=list)
    required_total: int = 0
    covered: int = 0
    missing_fact_ids: list[str] = field(default_factory=list)
    unknown_marked_ids: list[str] = field(default_factory=list)
    numeric_mismatches: list[dict[str, Any]] = field(default_factory=list)
    reference_total: int = 0
    reference_covered: int = 0
    missing_by_scope: dict[str, int] = field(default_factory=dict)
    missing_by_status: dict[str, int] = field(default_factory=dict)

    @property
    def gap_count(self) -> int:
        return len(self.missing_fact_ids)

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["gap_count"] = self.gap_count
        data["coverage_rate"] = (round(self.covered / self.required_total * 100.0, 1)
                                 if self.required_total else None)
        return data

    def summary_line(self) -> str:
        """单行摘要（CLI 打印 + banner 复用同一口径）。"""
        if self.status == "unreadable":
            return "分析报告不可读（未产出或内容为空）"
        return (f"结论覆盖 {self.covered}/{self.required_total}"
                f"（缺口 {self.gap_count}）"
                f"，章节缺失 {len(self.sections_missing)}"
                f"，疑似未引用读数 {len(self.numeric_mismatches)}"
                f"，参考层引用 {self.reference_covered}/{self.reference_total}")


def section_title(section_id: str) -> str:
    return SECTION_TITLES.get(section_id, section_id)


def _digits(text: Any) -> str:
    """归一化数字串：去逗号 / 空格（``1,305`` 与 ``1305`` 视为同一读数）。"""
    return re.sub(r"[,\s]", "", str(text or ""))


def _candidates(fact: dict[str, Any]) -> list[str]:
    """该 fact 在报告里"可被认出来"的若干数字写法（保守：宁少报不误报）。

    **只取 ``display``（即框架格式化后的读法，报告里逐字复制的那串）**：
    原始 ``numbers``（如 ``2.3333333``）在报告里从不出现——拿它当候选会把
    "表格里写 2.3 / 2 / 3"全判成不一致（首轮真机跑出 46 条此类误报）。
    数字位数太短（<4 位有效数字）无法与其它读数区分，直接放弃判定。
    """
    out: list[str] = []
    display = str(fact.get("display") or "")
    for chunk in re.split(r"[/、|]", display):
        text = _digits(chunk)
        if text and len(re.sub(r"\D", "", text)) >= _MIN_DIGIT_LEN and re.fullmatch(r"-?\d+(\.\d+)?", text):
            out.append(text)
    seen: list[str] = []
    for item in out:
        if item not in seen:
            seen.append(item)
    return seen


def _windows_for_markers(content: str, *, span: int = 240) -> dict[str, str]:
    """``{fact_id: 标记前后 span 字符窗口}``——用于校验"标记旁边写的数"是否与 facts 一致。

    **两侧都取**：报告常见写法是 ``<td data-fact-id="...">9,902 / 9,718</td>``（读数在后），
    也有人写成 ``<td>9,902 <span data-fact-id="..."></span></td>``（读数在前）——
    只取一侧会把后者误判成不一致。窗口内包含标签与分隔符，比对前统一去掉逗号与空白。
    """
    out: dict[str, str] = {}
    for match in _FACT_ID_RE.finditer(content):
        fid = match.group(1).strip()
        if fid and fid not in out:
            out[fid] = _digits(content[max(0, match.start() - span): match.end() + span])
    return out


def verify_report(
    html_path: str, facts_doc: dict[str, Any], *,
    required_sections: Iterable[str] = (),
) -> VerifyResult:
    """校验分析报告：章节 + 覆盖 + 读数（**永不抛异常**，读不到即 ``unreadable``）。"""
    result = VerifyResult(html_path=html_path)
    try:
        with open(html_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        logger.warning("[perf-ai] 分析报告读取失败 %s：%s", html_path, exc)
        return result
    if not content.strip():
        logger.warning("[perf-ai] 分析报告为空：%s", html_path)
        return result

    result.status = "ok"
    marked = {m.strip() for m in _FACT_ID_RE.findall(content) if m.strip()}
    ids = set(_ID_ATTR_RE.findall(content))
    required_ids = [s for s in (required_sections or ())]
    result.sections_found = [s for s in required_ids if s in ids]
    result.sections_missing = [s for s in required_ids if s not in ids]

    facts = [f for f in (facts_doc.get("facts") or []) if isinstance(f, dict)]
    required = [f for f in facts if f.get("required")]
    reference = [f for f in facts if not f.get("required")]
    result.required_total = len(required)
    result.reference_total = len(reference)
    result.covered = sum(1 for f in required if f.get("fact_id") in marked)
    result.reference_covered = sum(1 for f in reference if f.get("fact_id") in marked)

    known_ids = {f.get("fact_id") for f in facts}
    result.unknown_marked_ids = sorted(m for m in marked if m not in known_ids)

    flat = _digits(content)
    marked_windows = _windows_for_markers(content)
    for fact in required:
        fid = str(fact.get("fact_id") or "")
        candidates = _candidates(fact)
        if fact.get("fact_id") in marked:
            # 已标记 → 进一步看标记**附近**是否出现了对应读数（防"标了 id 却写别的数"）
            window = marked_windows.get(fid, "")
            if candidates and window and not any(c in window for c in candidates):
                result.numeric_mismatches.append({
                    "fact_id": fid, "zh": fact.get("zh"), "display": fact.get("display"),
                    "reason": "标记附近的读数与 facts 不一致（疑似改写/串行）",
                })
            continue
        result.missing_fact_ids.append(fid)
        scope = str(fact.get("scope") or "other")
        status = str(fact.get("data_status") or "ok")
        result.missing_by_scope[scope] = result.missing_by_scope.get(scope, 0) + 1
        result.missing_by_status[status] = result.missing_by_status.get(status, 0) + 1
        if candidates and not any(c in flat for c in candidates):
            result.numeric_mismatches.append({
                "fact_id": fid, "zh": fact.get("zh"), "display": fact.get("display"),
                "reason": "报告中未见该读数（疑似漏项或读数被改写）",
            })
    if result.sections_missing or result.gap_count or result.numeric_mismatches:
        result.status = "gaps"
    logger.info("[perf-ai] 校验完成：%s", result.summary_line())
    return result


def _escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def build_gap_block(result: VerifyResult, facts_doc: dict[str, Any],
                    *, limit: int = 120) -> tuple[str, str]:
    """生成（头部 banner, 末尾附C 章节）两段 HTML；缺口逐条列出（超量时截断并注明）。"""
    tone = "#0b7a3b" if result.status == "ok" and not result.gap_count else "#a15c00"
    if result.status == "unreadable":
        tone = "#a1260d"

    head = [
        f'<div {_VERIFY_MARK}="1" style="margin:16px 0;padding:12px 16px;border:1px solid {tone};'
        f'border-left:6px solid {tone};border-radius:6px;background:#fffaf2;font-size:14px;">',
        f'<strong>框架校验（{_escape(facts_doc.get("app"))} / '
        f'{_escape(facts_doc.get("platform"))}）</strong>：{_escape(result.summary_line())}',
    ]
    if result.sections_missing:
        head.append("<br>缺失章节：" + "、".join(
            _escape(section_title(s)) for s in result.sections_missing))
    if result.gap_count:
        head.append(f"<br>未给结论的数据见文末「附C 框架校验」"
                    f"（共 {result.gap_count} 项，报告仍有效，仅为可见性提示）。")
    head.append("</div>")

    rows = []
    for fid in result.missing_fact_ids[:limit]:
        fact = next((f for f in (facts_doc.get("facts") or []) if f.get("fact_id") == fid), {})
        rows.append(
            "<tr><td><code>" + _escape(fid) + "</code></td>"
            f"<td>{_escape(fact.get('zh'))}</td>"
            f"<td>{_escape(fact.get('display'))}</td>"
            f"<td>{_escape(fact.get('data_status'))}</td>"
            f"<td>{_escape(fact.get('gap'))}</td></tr>"
        )
    more = ""
    if result.gap_count > limit:
        more = f"<p>（仅列前 {limit} 项，完整清单见 <code>perf_analysis_verify.json</code>）</p>"

    numeric_rows = "".join(
        "<tr><td><code>" + _escape(item.get("fact_id")) + "</code></td>"
        f"<td>{_escape(item.get('zh'))}</td><td>{_escape(item.get('display'))}</td>"
        f"<td>{_escape(item.get('reason'))}</td></tr>"
        for item in result.numeric_mismatches[:limit]
    )
    section = [
        '<section id="appc"><details>',
        "<summary>附C 框架校验（自动追加，非分析结论）</summary>",
        f"<p>{_escape(result.summary_line())}</p>",
        "<p>本节由 <code>uita.core.perf.analysis.verify</code> 在分析报告产出后自动追加："
        "分析结论由 AI 给出，本节只回答「每条数据是否都被分析到」。</p>",
    ]
    if result.sections_missing:
        section.append("<h3>章节缺失</h3><ul>" + "".join(
            f"<li>{_escape(section_title(s))}（<code>{_escape(s)}</code>）</li>"
            for s in result.sections_missing) + "</ul>")
    if result.missing_by_scope:
        section.append("<h3>缺口分布</h3><ul>" + "".join(
            f"<li>{_escape(scope)}：{count} 项</li>"
            for scope, count in sorted(result.missing_by_scope.items(), key=lambda kv: -kv[1])
        ) + "</ul>")
    if rows:
        section.append(
            "<h3>未给出结论的数据（需补分析）</h3>"
            "<table><thead><tr><th>fact_id</th><th>含义</th><th>取值</th><th>状态</th><th>备注</th></tr>"
            "</thead><tbody>" + "".join(rows) + "</tbody></table>" + more
        )
    if numeric_rows:
        section.append(
            "<h3>疑似未引用 / 读数不一致（提示）</h3>"
            "<table><thead><tr><th>fact_id</th><th>含义</th><th>facts 读数</th><th>原因</th></tr>"
            "</thead><tbody>" + numeric_rows + "</tbody></table>"
            "<p>提示级：报告可能用别的写法呈现了该读数，人工扫一眼即可。</p>"
        )
    if not rows and not numeric_rows and not result.sections_missing:
        section.append("<p>全部必覆盖数据均已在报告中出现，未见读数不一致。</p>")
    section.append("</details></section>")
    return "\n".join(head), "\n".join(section)


def _strip_existing_block(content: str) -> str:
    """删掉上一次注入的 banner 与附C 章节（重复校验时**刷新**而不是跳过）。"""
    content = re.sub(
        rf'<div {re.escape(_VERIFY_MARK)}="1".*?</div>\s*', "", content, count=1, flags=re.S,
    )
    return re.sub(r'<section id="appc">.*?</section>\s*', "", content, count=1, flags=re.S)


def inject_gap_block(html_path: str, banner: str, section: str) -> bool:
    """把 banner 插到 ``<body>`` 之后、附C 插到 ``</body>`` 之前；失败返回 ``False``（不改文件）。

    **可重复调用**：已存在框架校验块时先移除旧的再写新的（口径变化/重新校验后结果要刷新，
    留旧块会让读者看到过期覆盖率）。
    """
    try:
        with open(html_path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        logger.warning("[perf-ai] 校验注入读取失败 %s：%s", html_path, exc)
        return False
    content = _strip_existing_block(content)
    body = _BODY_RE.search(content)
    if not body:
        logger.warning("[perf-ai] 报告无 <body>，跳过校验注入：%s", html_path)
        return False
    updated = content[:body.end()] + "\n" + banner + content[body.end():]
    end = _BODY_END_RE.search(updated)
    if end:
        updated = updated[:end.start()] + section + "\n" + updated[end.start():]
    else:
        updated = updated + "\n" + section
    try:
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(updated)
    except OSError as exc:
        logger.warning("[perf-ai] 校验注入写回失败 %s：%s", html_path, exc)
        return False
    logger.info("[perf-ai] 框架校验块已写入报告：%s", html_path)
    return True


def write_verify(path: str, result: VerifyResult) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result.to_dict(), fh, ensure_ascii=False, indent=2)
    return path
