"""文本用例 → 结构化步骤。

输入是人工写的自然语言用例（Markdown/测试管理系统粘贴文本都行），输出是可执行、
可回报状态的步骤表。解析策略（按优先级）：

1. 标题：`用例名称/标题/名称：xxx`；否则取首个非空且不含编号的短行；
2. 前置条件：`前置条件/前置/预置条件` 段；
3. 步骤：`1. xxx` / `1、xxx` / `步骤1：xxx` / `- xxx` 行；
4. 预期：步骤块内 `预期/期望/断言/验证：xxx`，或独立的 `预期结果` 列表按序号配对；
5. 一条步骤都识别不到时，退化为单步（整段文本即目标），并标记 ``parsed=False``。

解析结果只用于编排与结果展示，不改变"断言必须由工具执行验证"的纪律：
模型仍须调 ``assert_*``，`report_step` 只是把结论落到步骤表上。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_PASSED = "passed"
STEP_FAILED = "failed"
STEP_UNKNOWN = "unknown"
STEP_SKIPPED = "skipped"

STEP_STATUSES = (STEP_PENDING, STEP_RUNNING, STEP_PASSED, STEP_FAILED,
                 STEP_UNKNOWN, STEP_SKIPPED)
STEP_TERMINAL = (STEP_PASSED, STEP_FAILED, STEP_UNKNOWN, STEP_SKIPPED)

_NAME_RE = re.compile(r"^\s*(?:用例名称|用例标题|标题|名称)\s*[:：]\s*(.+)$")
_PRECOND_RE = re.compile(r"^\s*(?:前置条件|前置|预置条件|环境准备)\s*[:：]?\s*(.*)$")
_STEPS_HEAD_RE = re.compile(r"^\s*(?:测试步骤|操作步骤|执行步骤|步骤)\s*[:：]?\s*$")
_EXPECT_HEAD_RE = re.compile(r"^\s*(?:预期结果|期望结果|预期|期望)\s*[:：]?\s*$")
_NUMBERED_RE = re.compile(
    r"^\s*(?:第\s*(\d{1,2})\s*步\s*[:：.、]?|(?:步骤\s*)?[（(]?(\d{1,2})[)）]?\s*[.、)）:：])\s*(.+)$"
)
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.+)$")
_INLINE_EXPECT_RE = re.compile(
    r"^(.*?)(?:[（(]?\s*(?:预期|期望|断言|验证)(?:结果)?\s*[:：]\s*(.+?)[)）]?)\s*$"
)
_EXPECT_LINE_RE = re.compile(
    r"^\s*(?:预期|期望|断言|验证)(?:结果)?\s*[:：]\s*(.*)$"
)
_COMMENT_LINE_RE = re.compile(r"^\s*(?:#+\s*(.*)$|//.*$)")
_NL_INLINE_EXPECT_RE = re.compile(
    r"^(.+?)(?:[，,；;]\s*(?:并|并且|同时)?|(?:并|并且)\s*)"
    r"(?:预期|期望|断言|验证|确认|检查|确保|核对|看到|应显示|应该显示|应出现|应该出现|需显示|需要显示)"
    r"\s*[:：]?\s*(.+)$"
)
_ACTION_CLAUSE_RE = re.compile(
    r"^\s*(?:先|首先|然后|接着|随后|再|之后|最后|并且|并|进到.+?后|进入.+?后)*\s*"
    r"(?:帮我|请)?\s*"
    r"(?:打开|启动|进入|进到|点击|点开|点一下|点选|点|双击|右键|输入|填写|键入|选择|切换|"
    r"勾选|取消|滑动|滚动|拖拽|按|等待|返回|回到|关闭|退出|跳转|访问|浏览|展开|收起)"
)
_ASSERT_CLAUSE_RE = re.compile(
    r"^\s*(?:先|首先|然后|接着|随后|再|之后|最后|并且|并)*\s*"
    r"(?:断言|验证|检查|确认|确保|核对|判断|看看|看下|看是否|看到|应显示|应该显示|应出现|应该出现|需显示|需要显示)"
)
_ASSERT_INTENT_RE = re.compile(
    r"(?:^\s*(?:先|首先|然后|接着|随后|再|之后|最后)*\s*"
    r"(?:断言|验证|检查|确认|确保|核对|判断|看看|看下|看是否|看到|应显示|应该显示|应出现|应该出现)"
    r"|(?:是否存在|是否显示|是否出现|是否开启|是否关闭|是否正常|在不在|有没有|已开启|已关闭))"
)


@dataclass
class CaseStep:
    """单个用例步骤（含执行期状态）。"""

    no: int
    action: str
    expected: str = ""
    status: str = STEP_PENDING
    detail: str = ""
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def is_terminal(self) -> bool:
        return self.status in STEP_TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "no": self.no,
            "action": self.action,
            "expected": self.expected,
            "status": self.status,
            "detail": self.detail,
            "evidence": list(self.evidence),
        }


@dataclass
class TextCase:
    """一份可执行的文本用例。"""

    name: str = ""
    preconditions: list[str] = field(default_factory=list)
    steps: list[CaseStep] = field(default_factory=list)
    raw: str = ""
    parsed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "preconditions": list(self.preconditions),
            "parsed": self.parsed,
            "raw": self.raw,
            "steps": [step.to_dict() for step in self.steps],
        }

    def step(self, no: int) -> CaseStep | None:
        for item in self.steps:
            if item.no == int(no):
                return item
        return None

    def next_pending(self) -> CaseStep | None:
        for item in self.steps:
            if not item.is_terminal():
                return item
        return None

    def all_passed(self) -> bool:
        return bool(self.steps) and all(
            item.status == STEP_PASSED for item in self.steps
        )

    def summary(self) -> dict[str, int]:
        counts = {status: 0 for status in STEP_STATUSES}
        for item in self.steps:
            counts[item.status] = counts.get(item.status, 0) + 1
        counts["total"] = len(self.steps)
        return counts


def step_requires_assertion(step: CaseStep) -> bool:
    """判断某个用例步骤是否必须有真实 ``assert_* == true`` 证据。

    - 显式带 ``expected`` 的步骤：必须有断言；
    - 动作描述本身含「确认/验证/检查/断言/看看/看到/是否存在...」语义：必须有断言；
    - 纯操作/导航步骤（如「打开首页」「点击快速检索入口」）：只需操作工具成功执行即可过该步，
      但整条用例结束前仍须至少有一条真实 ``assert_* == true``。
    """
    if (step.expected or "").strip():
        return True
    return bool(_ASSERT_INTENT_RE.search(step.action or ""))


def _split_inline_expected(text: str, *, allow_natural: bool = False) -> tuple[str, str]:
    m = _INLINE_EXPECT_RE.match(text)
    if m:
        action = m.group(1).strip(" -—,，；;。")
        expected = m.group(2).strip(" ）)。")
        return action, expected
    if allow_natural:
        m_nl = _NL_INLINE_EXPECT_RE.match(text)
        if m_nl:
            cand_action = m_nl.group(1).strip(" -—,，；;。")
            cand_expect = m_nl.group(2).strip(" ）)。")
            if cand_action and cand_expect and not _ASSERT_CLAUSE_RE.match(cand_action):
                return cand_action, cand_expect
    return text.strip(" ；;。"), ""


def _split_natural_clauses(plain_candidates: list[str]) -> list[str]:
    """将无编号的自然语言描述拆分为顺序步骤子句。"""
    expanded: list[str] = []
    for line in plain_candidates:
        # 1) 箭头链：打开首页 -> 点击快速检索 -> 确认扫描进度
        if re.search(r"\s*(?:->|→|=>|➡️)\s*", line):
            parts = [p.strip() for p in re.split(r"\s*(?:->|→|=>|➡️)\s*", line) if p.strip()]
            if len(parts) >= 2:
                expanded.extend(parts)
                continue
        # 2) 分号/句号多句：先打开首页；然后点击快速检索；最后确认结果。
        semi_parts = [p.strip() for p in re.split(r"[；;。]+", line) if p.strip()]
        if len(semi_parts) >= 2:
            expanded.extend(semi_parts)
            continue
        expanded.append(line.strip(" 。；;"))

    # 3) 对每个句子：若含逗号分隔的多步动作（>=2 个独立动作子句），继续按逗号拆步
    final_clauses: list[str] = []
    for item in expanded:
        comma_parts = [p.strip(" 。；;") for p in re.split(r"[，,]+", item) if p.strip(" 。；;")]
        if len(comma_parts) >= 2:
            action_count = sum(
                1 for p in comma_parts
                if _ACTION_CLAUSE_RE.match(p) and not _ASSERT_CLAUSE_RE.match(p)
            )
            if action_count >= 2:
                final_clauses.extend(comma_parts)
                continue
        final_clauses.append(item)
    return final_clauses


def parse_text_case(text: str, *, default_name: str = "") -> TextCase:
    """把文本用例解析为 :class:`TextCase`（识别不到步骤则退化为单步）。"""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in raw.split("\n")]
    non_empty = [line.strip() for line in lines if line.strip()]

    name = ""
    explicit_name = False
    heading_name = ""
    preconditions: list[str] = []
    numbered: list[tuple[int, str]] = []
    bullets: list[tuple[int, str]] = []
    plain_candidates: list[str] = []
    expect_by_no: dict[int, str] = {}
    inline_expect_by_no: dict[int, str] = {}

    section = ""  # preconditions / steps / expects / ""
    for line in lines:
        stripped = line.strip()
        if not stripped or (stripped.startswith("<!--") and stripped.endswith("-->")):
            continue
        m_comment = _COMMENT_LINE_RE.match(stripped)
        if m_comment:
            if not heading_name and m_comment.group(1):
                heading_name = m_comment.group(1).strip()
            continue

        m = _NAME_RE.match(stripped)
        if m and not name:
            name = m.group(1).strip()
            explicit_name = True
            continue
        m = _PRECOND_RE.match(stripped)
        if m:
            section = "preconditions"
            tail = m.group(1).strip()
            if tail:
                preconditions.append(tail)
            continue
        if _STEPS_HEAD_RE.match(stripped):
            section = "steps"
            continue
        if _EXPECT_HEAD_RE.match(stripped):
            if numbered and section == "steps" and re.fullmatch(r"(?:预期|期望)\s*[:：]", stripped):
                expect_by_no[numbered[-1][0]] = ""
            else:
                section = "expects"
            continue

        m = _NUMBERED_RE.match(stripped)
        if m:
            no = int(m.group(1) or m.group(2))
            body_text = m.group(3)
            if section == "expects" and numbered:
                expect_by_no[no] = body_text.strip()
                continue
            action, inline = _split_inline_expected(body_text, allow_natural=True)
            numbered.append((no, action))
            if inline:
                inline_expect_by_no[no] = inline
            section = "steps"
            continue

        m = _EXPECT_LINE_RE.match(stripped)
        if m:
            target_idx = (numbered[-1][0] if numbered else (len(plain_candidates) or 1))
            expect_by_no.setdefault(target_idx, m.group(1).strip())
            if numbered:
                expect_by_no[numbered[-1][0]] = m.group(1).strip()
            elif plain_candidates:
                expect_by_no[len(plain_candidates)] = m.group(1).strip()
            continue

        m = _BULLET_RE.match(stripped)
        if m:
            if section == "preconditions":
                preconditions.append(m.group(1).strip())
            elif section == "expects":
                target_no = numbered[-1][0] if numbered else 1
                expect_by_no.setdefault(target_no, m.group(1).strip())
            else:
                action, inline = _split_inline_expected(m.group(1).strip(), allow_natural=True)
                idx = len(bullets) + 1
                bullets.append((idx, action))
                if inline:
                    inline_expect_by_no[idx] = inline
            continue

        if section == "preconditions":
            preconditions.append(stripped)
        elif section == "steps" and numbered:
            # 步骤的续行（说明文字）拼回上一步
            no, action = numbered[-1]
            numbered[-1] = (no, f"{action} {stripped}".strip())
        else:
            plain_candidates.append(stripped)

    # 支持纯 .txt 自然语言描述（多行、箭头链、分号/句号/逗号多动作子句，或含内联预期）
    raw_stripped = raw.lstrip()
    is_json_like = raw_stripped.startswith(("{", "["))
    if not numbered and not bullets and not is_json_like and plain_candidates:
        clauses = _split_natural_clauses(plain_candidates)
        if len(clauses) >= 2 or (
            len(clauses) == 1 and _INLINE_EXPECT_RE.match(clauses[0])
        ):
            for idx, clause_text in enumerate(clauses, start=1):
                action, inline = _split_inline_expected(clause_text, allow_natural=True)
                bullets.append((idx, action))
                if inline:
                    inline_expect_by_no[idx] = inline

    entries = numbered or bullets
    if not explicit_name and default_name.strip():
        name = default_name.strip()[:80]
    if not name and heading_name:
        name = heading_name[:80]
    if not name:
        for candidate in non_empty:
            if not _NUMBERED_RE.match(candidate) and not _COMMENT_LINE_RE.match(candidate) and len(candidate) <= 80:
                name = candidate
                break
        if not name:
            name = (non_empty[0] if non_empty else "文本用例")[:80]

    if not entries:
        body = "\n".join(non_empty).strip()
        return TextCase(
            name=name or "文本用例",
            preconditions=preconditions,
            steps=[CaseStep(no=1, action=body or "执行文本用例")],
            raw=raw,
            parsed=False,
        )

    steps: list[CaseStep] = []
    for index, (no, action) in enumerate(entries, start=1):
        resolved_no = no if numbered else index
        expected = inline_expect_by_no.get(resolved_no) or expect_by_no.get(
            resolved_no, "",
        )
        steps.append(CaseStep(no=resolved_no, action=action.strip(), expected=expected))

    steps.sort(key=lambda item: item.no)
    return TextCase(name=name or "文本用例", preconditions=preconditions,
                    steps=steps, raw=raw, parsed=True)


def case_from_dict(data: dict[str, Any]) -> TextCase | None:
    """从快照/JSON 复原（供服务端重启与会话回读）。"""
    if not isinstance(data, dict) or not data.get("steps"):
        return None
    steps = []
    for item in data.get("steps") or []:
        if not isinstance(item, dict):
            continue
        steps.append(CaseStep(
            no=int(item.get("no") or len(steps) + 1),
            action=str(item.get("action") or ""),
            expected=str(item.get("expected") or ""),
            status=str(item.get("status") or STEP_PENDING),
            detail=str(item.get("detail") or ""),
            evidence=list(item.get("evidence") or []),
        ))
    return TextCase(
        name=str(data.get("name") or ""),
        preconditions=list(data.get("preconditions") or []),
        steps=steps,
        raw=str(data.get("raw") or ""),
        parsed=bool(data.get("parsed", True)),
    )
