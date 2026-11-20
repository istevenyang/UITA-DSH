"""
TCS (TestCaseScript) Parser — converts token stream → CaseNode AST.

Supported syntax (Phase 2):
  name / app / os / priority / owner  (header)
  @setup / @teardown                   (per-step markers, only applies to the immediately following step)
  step: <name>                         (step definition)
      click[xxx]                       (action)
      type[xxx, text]                  (action)
      wait: [xxx, timeout=Ns]          (wait)
      wait: Ns                         (bare duration wait)
      assert: [xxx]                    (assert)
      verify: [xxx]                    (assert alias)
      set: var = get_text[element]     (variable assignment)
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

from uita.config import CASE_VARIABLES, refresh_case_variables

from .lexer import Token, TType, tokenize
from .nodes import (
    ActionNode, AssertNode, CaseNode, ConditionNode, IfNode, SetNode,
    StepBodyNode, StepNode, WaitNode,
)

# set: var_name = action[args]
_RE_SET_STMT = re.compile(r"^(\w+)\s*=\s*(\w+)\s*\[(.*)\]\s*$")
# set: var_name = action  (无参数)
_RE_SET_BARE = re.compile(r"^(\w+)\s*=\s*(\w+)\s*$")
# set: var_name = "raw string" 或 var_name = 'raw string'
_RE_SET_RAW = re.compile(r"""^(\w+)\s*=\s*(["'])(.*)\2\s*$""")

# Parse timeout string like "30s", "5s", "1.5s"  → float seconds
_RE_TIMEOUT = re.compile(r"(\d+(?:\.\d+)?)\s*s$", re.IGNORECASE)
# Parse duration span like "200ms" / "1.5s" → float seconds
_RE_TIMEOUT_SPAN = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|s)$", re.IGNORECASE)
# Parse wait target bracket: [element, timeout=Ns]  or  [element]
_RE_WAIT_BRACKET = re.compile(r"^\[(.+?)\]$")
# Duration-only wait: "1s", "30s"
_RE_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*s$", re.IGNORECASE)
# Action with brackets: click[xxx, yyy]（(.*) 而非 (.+)，兼容 clear_chat[] 等无参调用）
_RE_ACTION_CALL = re.compile(r"^(\w+)\s*\[(.*)\]\s*$")
# kwarg：key=value（key 须为 ASCII 标识符，避免误判中文位置参数里偶然出现的 "="）
_RE_KWARG = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")

# 参数整体作为单个字符串传递、不做逗号切分/全角逗号归一化的 action 集合
# （2026-07-28 新增，见
# docs/plans/archive/02-dsl-actions-实施纪要.md §3.2/§3.5）。
# 动机：这类 action 的唯一参数天然可能包含未被 () 包裹的顶层逗号（如 JS
# 代码里的数组/对象字面量），通用的 _split_top_level()（仅感知 () 深度，
# 不感知 []/{} 深度）会误判为多个位置参数；且这类内容里可能含有字面量全角
# 逗号（如内嵌的中文字符串常量），不应被 DSL 层无差别归一化改写。
# 新增其它"传整段代码/文本"的 action 时按需追加。
_RAW_SINGLE_ARG_ACTIONS = {"eval_js", "eval_js_on_target"}


def _split_top_level(raw: str) -> list[str]:
    """按顶层逗号切分字符串，忽略引号（' 或 "）内的逗号，以及圆括号内的逗号。

    供 action/assert 方括号内 kwarg 解析使用（P2 新增，见
    docs/plans/archive/02-dsl-actions-实施纪要.md §3.5）。

    圆括号深度计数（2026-07-27 新增，见
    docs/plans/archive/02-dsl-actions-实施纪要.md §2.1）：
    ``assert[equal(var_a, var_b)]`` 这类函数调用式写法本身在方括号内含有一个
    逗号（函数调用的两个参数分隔符），若不感知圆括号会被误判为顶层分隔符、
    拆成两个不完整 token。仅在圆括号深度为 0 时才把逗号视为顶层分隔符；不含
    圆括号的既有写法（对存量 421 个 .tcs 用例排查零命中方括号内含 ASCII 圆括号
    的写法）解析结果与改动前完全一致。
    """
    parts: list[str] = []
    buf: list[str] = []
    quote_char: Optional[str] = None
    paren_depth = 0
    for ch in raw:
        if quote_char:
            buf.append(ch)
            if ch == quote_char:
                quote_char = None
            continue
        if ch in ("'", '"'):
            quote_char = ch
            buf.append(ch)
            continue
        if ch == "(":
            paren_depth += 1
            buf.append(ch)
            continue
        if ch == ")":
            paren_depth = max(0, paren_depth - 1)
            buf.append(ch)
            continue
        if ch in (",", "\uff0c") and paren_depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    return [p.strip() for p in parts]


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _split_args_kwargs(raw_args: str) -> tuple[list[str], dict[str, str]]:
    """把方括号内逗号分隔的内容拆成 (纯位置参数, kwarg 字典)。

    形如 ``key=value`` 的项（key 须为 ASCII 标识符）识别为 kwarg 并剥离两端引号，
    其余按原样保留为位置参数（顺序不变）。无 "=" 的存量写法（如
    ``click[新建对话按钮]``/``type[输入框，你好]``）解析结果与改造前完全一致。
    """
    positional: list[str] = []
    kwargs: dict[str, str] = {}
    for part in _split_top_level(raw_args):
        if not part:
            continue
        m = _RE_KWARG.match(part)
        if m:
            kwargs[m.group(1)] = _strip_quotes(m.group(2))
        else:
            positional.append(_strip_quotes(part))
    return positional, kwargs


# assert[js=...]/verify[js=...]/if[js=...]/elif[js=...] 显式 kwarg 写法废弃
# （2026-07-28 新增，见宪法 R22b + 计划文档 §3.9）：既然 assert[<element_name>]/
# if[<element_name>] 已能通过 rosetta_map 的 js 表达式字段间接触发同一底层
# JsJudger 派发（core/actions/common/action_assert.py/core/runner/
# condition_eval.py 运行时内部注入 kwargs={"js": ...}，完全不经过本解析器），
# 用户不应再在 .tcs 源码里直接手写 js=<表达式> 硬编码大段 JS 代码。
# 仅拒绝**从 .tcs 源码文本解析出的** js= kwarg；运行时内部注入的 kwargs dict
# 从不经过本模块，不受影响。
_DEPRECATED_JS_KWARG_MSG = (
    "assert[js=...]/verify[js=...]/if[js=...]/elif[js=...] 写法已废弃：请改用 "
    "rosetta_map 的 js 表达式字段（元素只声明 {\"js\": \"<JS 表达式>\", "
    "\"usage\": \"...\"}，不声明任何定位字段）+ "
    "assert[<element_name>]/if[<element_name>]。见宪法 R22b。"
)


def _reject_deprecated_js_kwarg(kwargs: dict[str, str], line_no: int) -> None:
    if "js" in kwargs:
        raise ParseError(_DEPRECATED_JS_KWARG_MSG, line_no)


def _reject_deprecated_js_kwarg_in_expr(expr: str, line_no: int) -> None:
    _, kwargs = parse_condition_expr(expr)
    _reject_deprecated_js_kwarg(kwargs, line_no)


# eval_js[$var]/eval_js[${var}] 整串变量引用写法已废弃（2026-07-28，与
# assert[js=...]/if[js=...] 同一批收敛：变量引用应改为在 rosetta_map 声明
# js 表达式字段 + eval_js[<element_name>]，见宪法 R22b）。正则形状与
# core/actions/common/_js_var_ref.py::_RE_BARE_VAR_REF 同构——该模块属于
# core/actions/ 运行时层，core/dsl/ 依 R14 不得 import，故此处独立复制一份，
# 两处均为纯字符串形状匹配，无运行时耦合，不违反 R14。
_RE_EVAL_JS_BARE_VAR_REF = re.compile(r"^\$\{?([A-Za-z_]\w*)\}?$")

_DEPRECATED_EVAL_JS_VAR_REF_MSG = (
    "eval_js[$var]/eval_js[${var}] 整串变量引用写法已废弃：请改用 rosetta_map "
    "的 js 表达式字段（元素只声明 {\"js\": \"<JS 表达式>\", \"usage\": \"...\"}，"
    "不声明任何定位字段）+ eval_js[<element_name>]。见宪法 R22b。"
)


def _reject_deprecated_eval_js_var_ref(raw_arg: str, line_no: int) -> None:
    if _RE_EVAL_JS_BARE_VAR_REF.match(raw_arg.strip()):
        raise ParseError(_DEPRECATED_EVAL_JS_VAR_REF_MSG, line_no)


def parse_condition_expr(raw_expr: str) -> tuple[str, dict[str, str]]:
    """解析 if[]/elif[] 方括号内表达式（ConditionNode.raw_expr，非 empty[] 分支）
    为 (description, kwargs)，与 assert[]/click[] 同构。

    ``text=xxx`` 归一化为 ``contains=xxx``（复用 ContentJudger，不新增判定器，
    见 docs/plans/archive/02-dsl-actions-实施纪要.md §3.4/§3.7 风险 6）。

    此函数是纯字符串解析（不 import 任何运行时模块），供
    core/runner/condition_eval.py 复用，满足 core/dsl/ 零运行时依赖（R14）。
    """
    positional, kwargs = _split_args_kwargs(raw_expr)
    description = positional[0] if positional else ""
    if "text" in kwargs:
        kwargs = dict(kwargs)
        kwargs["contains"] = kwargs.pop("text")
    return description, kwargs


def _parse_tag_list(val: str) -> list[str]:
    """解析 tag 头字段值，形如 ``[smoke, chat]`` → ``["smoke", "chat"]``。

    兼容全角逗号（，）与无中括号的裸逗号分隔写法；空值返回空列表。
    """
    v = val.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    v = v.replace("，", ",")
    return [t.strip() for t in v.split(",") if t.strip()]


_BOOL_TRUE = frozenset({"true", "yes", "1", "on"})
_BOOL_FALSE = frozenset({"false", "no", "0", "off"})


def _parse_span_s(text: str) -> Optional[float]:
    """``"200ms"`` / ``"1.5s"`` / ``"2"`` → 秒；非法返回 ``None``。"""
    raw = (text or "").strip()
    if not raw:
        return None
    match = _RE_TIMEOUT_SPAN.match(raw)
    if match:
        value = float(match.group(1))
        return value / 1000.0 if match.group(2).lower() == "ms" else value
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_bool_kwarg(text: str, *, name: str, line_no: int = 0) -> bool:
    """解析布尔 kwarg（大小写不敏感，白名单对齐 ``action_switch._parse_bool``）。

    非法值**报错**而不是回落默认值：``visual_only=ture`` 若被当成"没写"，
    就会静默走全链，与用户意图相反且难排查。
    """
    token = (text or "").strip().lower()
    if token in _BOOL_TRUE:
        return True
    if token in _BOOL_FALSE:
        return False
    raise ParseError(
        f"{name} 取值非法（{text!r}），合法值："
        f"true/false/yes/no/1/0/on/off",
        line_no=line_no,
    )


def _parse_wait_value(value: str, line_no: int = 0) -> WaitNode:
    """Parse the part after 'wait:'.

    Examples:
      "[新建对话按钮，timeout=30s]"        → WaitNode(target="新建对话按钮", timeout_s=30)
      "[输入框，timeout=5s]"               → WaitNode(target="输入框", timeout_s=5)
      "1s"                                 → WaitNode(target=None, timeout_s=1)
      "page_ready[timeout=300s]"           → WaitNode(target="page_ready", timeout_s=300)
      "app_ready[timeout=60s]"             → WaitNode(target="app_ready", timeout_s=60)
      "[悬浮球, state=gone, timeout=5s]"    → WaitNode(target="悬浮球", timeout_s=5, state="gone")
    """
    # Normalize Chinese comma inside brackets
    value = value.replace("，", ",").strip()

    m_dur = _RE_DURATION.match(value)
    if m_dur:
        return WaitNode(target=None, timeout_s=float(m_dur.group(1)), raw=value)

    m_br = _RE_WAIT_BRACKET.match(value)
    if m_br:
        inner = m_br.group(1).strip()
        parts = [p.strip() for p in inner.split(",")]
        target = parts[0]
        timeout_s = 10.0  # default
        state = "visible"  # default
        poll_s = 1.0
        visual_only = False
        for part in parts[1:]:
            m_to = re.match(r"timeout\s*=\s*(.+)", part, re.IGNORECASE)
            if m_to:
                timeout_s = _parse_span_s(m_to.group(1)) or timeout_s
                continue
            m_st = re.match(r"state\s*=\s*(.+)", part, re.IGNORECASE)
            if m_st:
                state = m_st.group(1).strip().lower()
                continue
            m_poll = re.match(r"poll\s*=\s*(.+)", part, re.IGNORECASE)
            if m_poll:
                parsed = _parse_span_s(m_poll.group(1))
                if parsed is None or parsed <= 0:
                    raise ParseError(
                        f"poll 取值非法（{m_poll.group(1)!r}），"
                        "需为正的时长（如 200ms / 0.5s）",
                        line_no=line_no,
                    )
                poll_s = max(0.05, parsed)
                continue
            m_vo = re.match(r"visual_only\s*=\s*(.*)$", part, re.IGNORECASE)
            if m_vo:
                visual_only = _parse_bool_kwarg(m_vo.group(1), name="visual_only", line_no=line_no)
        return WaitNode(
            target=target, timeout_s=timeout_s, state=state,
            poll_interval_s=poll_s, visual_only=visual_only, raw=value,
        )

    # Handle thick action bracket syntax: app_ready[timeout=300s]
    m_action = _RE_ACTION_CALL.match(value)
    if m_action:
        target = m_action.group(1).strip()
        raw_args = m_action.group(2).strip()
        timeout_s = 10.0
        state = "visible"
        poll_s = 1.0
        visual_only = False
        if raw_args:
            for part in [p.strip() for p in raw_args.split(",")]:
                m_to = re.match(r"timeout\s*=\s*(.+)", part, re.IGNORECASE)
                if m_to:
                    timeout_s = _parse_span_s(m_to.group(1)) or timeout_s
                    continue
                m_st = re.match(r"state\s*=\s*(.+)", part, re.IGNORECASE)
                if m_st:
                    state = m_st.group(1).strip().lower()
                    continue
                m_poll = re.match(r"poll\s*=\s*(.+)", part, re.IGNORECASE)
                if m_poll:
                    parsed = _parse_span_s(m_poll.group(1))
                    if parsed is None or parsed <= 0:
                        raise ParseError(
                            f"poll 取值非法（{m_poll.group(1)!r}），"
                            "需为正的时长（如 200ms / 0.5s）",
                            line_no=line_no,
                        )
                    poll_s = max(0.05, parsed)
                    continue
                m_vo = re.match(r"visual_only\s*=\s*(.*)$", part, re.IGNORECASE)
                if m_vo:
                    visual_only = _parse_bool_kwarg(
                        m_vo.group(1), name="visual_only", line_no=line_no,
                    )
        return WaitNode(
            target=target, timeout_s=timeout_s, state=state,
            poll_interval_s=poll_s, visual_only=visual_only, raw=value,
        )

    # Fallback: treat entire value as target
    return WaitNode(target=value, timeout_s=10.0, raw=value)


def _parse_assert_value(value: str, line_no: int = 0) -> AssertNode:
    """Parse 'assert:xxx' or 'verify:xxx' token value.

    支持 kwarg（P2 新增）：``assert:[元素, contains="xxx", count_min=1]``
    → target=第一个位置参数，其余 key=value 项进入 kwargs。

    ``js=`` kwarg 显式写法已废弃（见 `_DEPRECATED_JS_KWARG_MSG`），解析出该
    kwarg 时直接 ParseError。
    """
    kw, _, rest = value.partition(":")
    rest = rest.strip()
    if rest.startswith("[") and rest.endswith("]"):
        rest = rest[1:-1].strip()
    positional, kwargs = _split_args_kwargs(rest)
    _reject_deprecated_js_kwarg(kwargs, line_no)
    target = positional[0] if positional else ""
    return AssertNode(target=target, keyword=kw.lower(), kwargs=kwargs, raw=value)


def _parse_action_token(value: str, line_no: int = 0) -> "StepBodyNode":
    """Parse an ACTION token value.

    assert[xxx] / verify[xxx]  → AssertNode
    wait[xxx, timeout=Ns]      → WaitNode
    wait[Ns]                   → WaitNode（bare duration）
    其余                        → ActionNode
    """
    # 先在未做全角逗号归一化的原始文本上探测一次，取出 action 名——
    # 命中 _RAW_SINGLE_ARG_ACTIONS 时直接用原始括号内容作为单个参数返回，
    # 不归一化、不切分（见 _RAW_SINGLE_ARG_ACTIONS 定义处说明与
    # docs/plans/archive/02-dsl-actions-实施纪要.md §3.5）。
    # 该探测不影响其它 action：匹配成功与否只取决于 "="/"["/"]" 等结构字符，
    # 不受逗号是否归一化影响，未命中该集合时走下方原有逻辑，结果完全一致。
    m_probe = _RE_ACTION_CALL.match(value.strip())
    if m_probe and m_probe.group(1).lower() in _RAW_SINGLE_ARG_ACTIONS:
        action_name = m_probe.group(1).lower()
        raw_args = m_probe.group(2).strip()
        if action_name == "eval_js":
            _reject_deprecated_eval_js_var_ref(raw_args, line_no)
        return ActionNode(action=action_name, args=[raw_args], kwargs={}, raw=value)

    # 保留原始括号内容：_split_top_level() 已负责把引号外的中英文逗号视为
    # 分隔符；若先全局归一化，会错误改写引号内用于 UI 精确匹配的中文逗号。
    m = _RE_ACTION_CALL.match(value.strip())

    if m:
        action_name = m.group(1).lower()
        raw_args = m.group(2).strip()

        # assert[xxx] / verify[xxx] → AssertNode（支持 P2 kwarg：assert[元素, contains="xxx"]）
        # 注意：此处使用原始 value（未做全角逗号归一化）重新提取 raw_args，
        # 避免 contains="..." 等引号内的全角逗号被误转为英文逗号。
        # _split_top_level 已能在引号外将全角逗号视为分隔符，无需预处理。
        if action_name in ("assert", "verify"):
            m_orig = _RE_ACTION_CALL.match(value.strip())
            orig_raw_args = m_orig.group(2).strip() if m_orig else raw_args
            positional, akwargs = _split_args_kwargs(orig_raw_args.strip("[]"))
            _reject_deprecated_js_kwarg(akwargs, line_no)
            target = positional[0] if positional else ""
            return AssertNode(target=target, keyword=action_name, kwargs=akwargs, raw=value)

        # wait[xxx, timeout=Ns] 或 wait[Ns] → WaitNode
        if action_name == "wait":
            # wait[1s] → 裸时长；wait[element, timeout=Ns] → 元素等待
            m_dur = _RE_DURATION.match(raw_args.strip())
            if m_dur:
                return WaitNode(target=None, timeout_s=float(m_dur.group(1)), raw=value)
            return _parse_wait_value(f"[{raw_args}]", line_no)

        # 通用 action[pos1, pos2, key=value]（P2 新增 kwarg 支持，P3 复用同一解析）
        args, akwargs = _split_args_kwargs(raw_args)
        return ActionNode(action=action_name, args=args, kwargs=akwargs, raw=value)

    # 裸关键词（无括号）
    kw = value.strip().lower()
    if kw in ("assert", "verify"):
        return AssertNode(target="", keyword=kw, raw=value)
    if kw == "wait":
        return WaitNode(target=None, timeout_s=0, raw=value)
    return ActionNode(action=kw, args=[], raw=value)


class ParseError(Exception):
    def __init__(self, msg: str, line_no: int = 0):
        super().__init__(f"Line {line_no}: {msg}")
        self.line_no = line_no


# if/elif/else 缩进块规则（2026-07-25 新增，见
# docs/plans/archive/02-dsl-actions-实施纪要.md §3.1）：
# 缩进单位固定 4 空格；仅在 if/elif/else 块内部生效，缩进语义只在遇到
# if/elif 关键字后才启用，不影响块外任何存量内容的解析（见 lexer.py 顶部说明）。
_INDENT_UNIT = 4
_MAX_IF_NESTING = 3

# not[expr] / ![expr] / empty[expr] / not empty[expr] / !empty[expr] 修饰词解析
# （呼应 §3.2/§3.3，两种 not 写法归一化为同一内部表示，不区分书写形式）。
_RE_NOT_EMPTY = re.compile(r"^(?:not|!)\s+empty\s*\[(.*)\]\s*$", re.IGNORECASE)
_RE_BANG_EMPTY = re.compile(r"^!empty\s*\[(.*)\]\s*$", re.IGNORECASE)
_RE_EMPTY = re.compile(r"^empty\s*\[(.*)\]\s*$", re.IGNORECASE)
_RE_NOT = re.compile(r"^not\s*\[(.*)\]\s*$", re.IGNORECASE)
_RE_BANG = re.compile(r"^!\[(.*)\]\s*$", re.IGNORECASE)
_RE_PLAIN_BRACKET = re.compile(r"^\[(.*)\]\s*$")
# empty[]/not empty[]/!empty[] 仅允许作用于 ${var} 变量引用（见计划文档 §3.3
# 必要性评估结论：empty[] 有独立存在价值，但必须限定作用域，非变量引用场景
# 直接在 parser 阶段报错，不静默允许产生歧义）。
_RE_VAR_REF_ONLY = re.compile(r"^\$\{[^}]+\}$")


def _parse_condition(raw_value: str, line_no: int, raw_line: str = "") -> ConditionNode:
    """解析 if[...]/elif[...] token.value（已剥离 if/elif 关键字前缀）为 ConditionNode。

    支持写法（见计划文档 §3.2/§3.3）：
      [expr]              → ConditionNode(raw_expr=expr)
      not[expr] / ![expr] → ConditionNode(raw_expr=expr, negate=True)
      empty[${var}]         → ConditionNode(raw_expr="${var}", is_empty_check=True)
      not empty[${var}] / !empty[${var}]
                           → ConditionNode(raw_expr="${var}", negate=True, is_empty_check=True)

    empty[]/not empty[]/!empty[] 仅支持 ${var} 变量引用，其余写法（元素描述/
    text=/js=/selector= 等 kwarg 组合）直接 ParseError（应改用 if not[...] 表达
    "不存在/不包含"语义）。
    """
    v = raw_value.strip()

    m = _RE_NOT_EMPTY.match(v) or _RE_BANG_EMPTY.match(v)
    if m:
        expr = m.group(1).strip()
        _require_var_ref(expr, line_no)
        return ConditionNode(raw_expr=expr, negate=True, is_empty_check=True, raw=raw_line)

    m = _RE_EMPTY.match(v)
    if m:
        expr = m.group(1).strip()
        _require_var_ref(expr, line_no)
        return ConditionNode(raw_expr=expr, negate=False, is_empty_check=True, raw=raw_line)

    m = _RE_NOT.match(v) or _RE_BANG.match(v)
    if m:
        expr = m.group(1).strip()
        _reject_deprecated_js_kwarg_in_expr(expr, line_no)
        return ConditionNode(raw_expr=expr, negate=True, is_empty_check=False, raw=raw_line)

    m = _RE_PLAIN_BRACKET.match(v)
    if m:
        expr = m.group(1).strip()
        _reject_deprecated_js_kwarg_in_expr(expr, line_no)
        return ConditionNode(raw_expr=expr, negate=False, is_empty_check=False, raw=raw_line)

    raise ParseError(
        f"if/elif 条件格式错误，须为 [expr] / not[expr] / ![expr] / empty[${{var}}] / "
        f"not empty[${{var}}] / !empty[${{var}}] 形式，实际: {raw_value!r}",
        line_no,
    )


def _require_var_ref(expr: str, line_no: int) -> None:
    if not _RE_VAR_REF_ONLY.match(expr.strip()):
        raise ParseError(
            f"empty[]/not empty[]/!empty[] 仅支持 ${{var}} 变量引用，不支持元素描述/kwarg "
            f"组合（实际: {expr!r}）。如需判断元素不存在/内容不包含，请用 if not[...] 表达",
            line_no,
        )


class Parser:
    def __init__(self, tokens: list[Token]):
        self._tokens = [t for t in tokens if t.ttype not in (TType.BLANK, TType.COMMENT)]
        self._pos = 0

    # ---- helpers ----

    def _peek(self) -> Optional[Token]:
        if self._pos < len(self._tokens):
            return self._tokens[self._pos]
        return None

    def _consume(self) -> Token:
        t = self._tokens[self._pos]
        self._pos += 1
        return t

    def _expect(self, ttype: TType) -> Token:
        t = self._peek()
        if t is None or t.ttype != ttype:
            got = repr(t) if t else "EOF"
            raise ParseError(f"Expected {ttype}, got {got}", t.line_no if t else 0)
        return self._consume()

    @staticmethod
    def _indent_level(t: Token) -> int:
        """校验并返回 token 的缩进层级（4 空格 = 1 层）。

        仅在 if/elif/else 块解析路径调用，不影响块外 token（见模块顶部说明）。
        """
        if t.has_tab_indent:
            raise ParseError("if/elif/else 块内缩进只支持空格，不支持 tab", t.line_no)
        if t.indent % _INDENT_UNIT != 0:
            raise ParseError(
                f"if/elif/else 块内缩进必须是 {_INDENT_UNIT} 空格的倍数，实际 {t.indent} 空格",
                t.line_no,
            )
        return t.indent // _INDENT_UNIT

    # ---- grammar ----

    def parse(self) -> CaseNode:
        case = CaseNode()
        self._parse_header(case)
        self._parse_body(case)
        return case

    def _parse_header(self, case: CaseNode) -> None:
        while self._peek() and self._peek().ttype == TType.META_KEY:
            t = self._consume()
            key, _, val = t.value.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key == "name":
                case.name = val
            elif key == "app":
                case.app = val
            elif key == "os":
                case.os = val
            elif key == "priority":
                case.priority = val
            elif key == "owner":
                case.owner = val
            elif key == "zyid":
                case.zyid = val
            elif key == "description":
                case.description = val
            elif key == "tag":
                case.tags = _parse_tag_list(val)
            elif key == "retryable":
                case.retryable = val.lower() in ("true", "1")
            elif key == "fail_fast":
                case.fail_fast = val.lower() in ("true", "1")
            elif key == "browser_mode":
                case.browser_mode = val

    def _parse_body(self, case: CaseNode) -> None:
        # @setup / @teardown 只对紧随其后的下一个 step 生效，消费该 step 后自动重置
        pending_section: Optional[str] = None  # None | "setup" | "teardown"

        while self._peek():
            t = self._peek()

            if t.ttype == TType.SECTION:
                self._consume()
                pending_section = t.value  # "setup" | "teardown"
                continue

            if t.ttype == TType.STEP:
                step = self._parse_step()
                if pending_section == "setup":
                    step.is_setup = True
                    case.setup_steps.append(step)
                elif pending_section == "teardown":
                    step.is_teardown = True
                    case.teardown_steps.append(step)
                else:
                    case.steps.append(step)
                pending_section = None  # 消费完一个 step 后重置，@section 仅作用于下一个 step
                continue

            # Unknown token at body level → skip with warning
            self._consume()

    def _parse_step(self) -> StepNode:
        t = self._consume()  # STEP token
        step = StepNode(name=t.value)
        # Consume body until next STEP or SECTION or EOF
        while self._peek() and self._peek().ttype not in (TType.STEP, TType.SECTION):
            body_node = self._parse_body_node(nesting=1)
            if body_node is not None:
                step.body.append(body_node)
        return step

    def _parse_body_node(self, nesting: int = 1) -> Optional[StepBodyNode]:
        t = self._peek()
        if t is None:
            return None
        if t.ttype == TType.IF:
            return self._parse_if_stmt(nesting)
        if t.ttype in (TType.ELIF, TType.ELSE):
            # 孤立的 elif/else（未紧随匹配的 if，或缩进层级与 if 链不一致）
            raise ParseError(
                f"{'elif' if t.ttype == TType.ELIF else 'else'} 缺少匹配的 if（或缩进层级不一致）",
                t.line_no,
            )

        t = self._consume()
        if t.ttype == TType.ACTION:
            return _parse_action_token(t.value, t.line_no)
        if t.ttype == TType.WAIT:
            node = _parse_wait_value(t.value, getattr(t, "line", 0) or 0)
            node.raw = t.raw
            return node
        if t.ttype == TType.ASSERT:
            node = _parse_assert_value(t.value, t.line_no)
            node.raw = t.raw
            return node
        if t.ttype == TType.SET:
            return _parse_set_value(t.value, t.raw, t.line_no)
        # Skip unknowns silently
        return None

    def _parse_if_stmt(self, nesting: int = 1) -> IfNode:
        """解析 if[cond] ... (elif[cond] ...)* (else ...)? 控制流块。

        见 docs/plans/archive/02-dsl-actions-实施纪要.md §3.1/§3.6。
        """
        if_tok = self._consume()  # IF token
        base_level = self._indent_level(if_tok)
        cond = _parse_condition(if_tok.value, if_tok.line_no, if_tok.raw)
        body = self._parse_indented_block(base_level, nesting)
        branches: list[tuple[ConditionNode, list[StepBodyNode]]] = [(cond, body)]
        else_body: list[StepBodyNode] = []

        while True:
            t = self._peek()
            if t is None or t.ttype not in (TType.ELIF, TType.ELSE):
                break
            if self._indent_level(t) != base_level:
                break  # 缩进层级不一致，不属于本 if 链，交还上层处理

            if t.ttype == TType.ELIF:
                self._consume()
                cond = _parse_condition(t.value, t.line_no, t.raw)
                body = self._parse_indented_block(base_level, nesting)
                branches.append((cond, body))
                continue

            # ELSE
            if t.error:
                raise ParseError(t.error, t.line_no)
            self._consume()
            else_body = self._parse_indented_block(base_level, nesting)
            break

        return IfNode(branches=branches, else_body=else_body, raw=if_tok.raw)

    def _parse_indented_block(self, base_level: int, nesting: int) -> list[StepBodyNode]:
        """解析比 base_level 深一层（body_level = base_level + 1）的缩进块。

        遇到以下情况即停止（不消费该 token，交还调用方）：
          - EOF / STEP / SECTION token
          - 缩进层级 <= base_level（含同级 elif/else，交还 _parse_if_stmt 处理）
        缩进跳跃超过一层（> body_level）直接 ParseError（见计划文档 §3.1）。
        """
        body_level = base_level + 1
        body: list[StepBodyNode] = []
        while True:
            t = self._peek()
            if t is None or t.ttype in (TType.STEP, TType.SECTION):
                break
            lvl = self._indent_level(t)
            if lvl <= base_level:
                break
            if lvl > body_level:
                raise ParseError(
                    f"缩进跳跃过多（预期层级 {body_level}，实际 {lvl}）", t.line_no,
                )
            if t.ttype == TType.IF:
                if nesting >= _MAX_IF_NESTING:
                    raise ParseError(
                        f"if/elif/else 嵌套层数超过限制（最多 {_MAX_IF_NESTING} 层）", t.line_no,
                    )
                body.append(self._parse_if_stmt(nesting + 1))
                continue
            node = self._parse_body_node(nesting)
            if node is not None:
                body.append(node)
        return body


def _parse_set_value(value: str, raw: str = "", line_no: int = 0) -> SetNode:
    """解析 set: 语句。

    支持格式：
      var = get_text[element]
      var = get_text[element, attr]
      var = get_attribute[element, href]
      var = "raw string value"
      var = 'raw string value'

    全角逗号归一化时序（2026-07-28，见
    docs/plans/archive/02-dsl-actions-实施纪要.md §3.5）：
    raw string 赋值（_RE_SET_RAW）与命中 _RAW_SINGLE_ARG_ACTIONS 的
    action 调用（如 eval_js），均先在**未做**"，"→"," 归一化的原始文本上
    探测匹配并直接使用原始内容——raw string 语义上不该被 DSL 层做任何隐式
    转换，_RAW_SINGLE_ARG_ACTIONS 同 _parse_action_token() 的理由（避免
    悄悄改写内嵌 JS 代码里的全角逗号字面内容）。其余写法维持原有归一化
    逻辑，解析结果与改动前完全一致。
    """
    stripped_raw = value.strip()

    # 1) raw string 赋值：始终优先在原始文本上匹配，不归一化。
    m_raw_string = _RE_SET_RAW.match(stripped_raw)
    if m_raw_string:
        return SetNode(
            var_name=m_raw_string.group(1).strip(), source_action="raw",
            source_args=[m_raw_string.group(3)], raw=raw,
        )

    # 2) action 调用：先在原始文本上探测 source_action，命中
    #    _RAW_SINGLE_ARG_ACTIONS 时使用原始（未归一化、未切分）内容。
    m_stmt_probe = _RE_SET_STMT.match(stripped_raw)
    if m_stmt_probe and m_stmt_probe.group(2).strip().lower() in _RAW_SINGLE_ARG_ACTIONS:
        source_action = m_stmt_probe.group(2).strip().lower()
        source_arg = m_stmt_probe.group(3).strip()
        if source_action == "eval_js":
            _reject_deprecated_eval_js_var_ref(source_arg, line_no)
        return SetNode(
            var_name=m_stmt_probe.group(1).strip(),
            source_action=source_action,
            source_args=[source_arg],
            raw=raw,
        )

    # 3) 其余写法：维持原有"先归一化、再匹配/切分"逻辑。
    value = value.replace("，", ",")
    m = _RE_SET_STMT.match(value.strip())
    if m:
        var_name = m.group(1).strip()
        source_action = m.group(2).strip().lower()
        args, kwargs = _split_args_kwargs(m.group(3))
        return SetNode(
            var_name=var_name, source_action=source_action,
            source_args=args, raw=raw, source_kwargs=kwargs,
        )
    m = _RE_SET_BARE.match(value.strip())
    if m:
        return SetNode(var_name=m.group(1).strip(), source_action=m.group(2).strip().lower(), source_args=[], raw=raw)
    # fallback
    return SetNode(var_name=value.split("=")[0].strip(), source_action="", source_args=[], raw=raw)


_VAR_PATTERN = re.compile(r"\$\{\{(\w+)\}\}")
_logger = logging.getLogger(__name__)


def _resolve_variables(text: str) -> str:
    """TCS 文本中的 ${{VAR}} 占位符替换为 CASE_VARIABLES 值。

    在 parse_source 入口、lexer 之前做文本级 re.sub，使 ${{VAR}} 可出现在
    任何位置（selector / type 文本 / url 等）。未匹配的占位符保留原样 + warning。
    """
    def _replacer(m: "re.Match") -> str:
        key = m.group(1)
        if key in CASE_VARIABLES:
            # TCS lexer 不执行 JSON 反转义，变量值必须按字面值替换；否则 Windows
            # 路径中的每个分隔符都会变成真实的重复反斜杠。
            return str(CASE_VARIABLES[key])
        _logger.warning("用例变量 '%s' 未在 CASE_VARIABLES 中定义，保留原样", m.group(0))
        return m.group(0)
    return _VAR_PATTERN.sub(_replacer, text)


def parse_source(source: str) -> CaseNode:
    """Parse TCS source text → CaseNode AST."""
    refresh_case_variables()
    source = _resolve_variables(source)
    tokens = tokenize(source)
    return Parser(tokens).parse()


def parse_file(path: str) -> CaseNode:
    """Parse a TCS file → CaseNode AST."""
    with open(path, encoding="utf-8") as f:
        source = f.read()
    return parse_source(source)


def infer_app_from_path(case_file: str) -> str:
    """从 .tcs 文件路径推断 app 名称：取路径中 "cases" 目录后紧跟的一级目录名
    （项目约定 cases/<app_name>/... 结构）。找不到符合结构的路径则返回空字符串。

    用于 case.app 头字段缺失时的兜底推断，优先级低于
    case.app 声明值、高于最终硬编码兜底 "sample_web"。

    Examples:
        cases/sample_web/smoke/x.tcs          → "sample_web"
        /abs/path/UITA/cases/sample_web/x.tcs → "sample_web"
        cases/x.tcs（无子目录）                 → ""（不误判文件名为 app 名）
        /tmp/custom_dir/x.tcs                 → ""（路径中无 "cases" 目录，无法推断）
    """
    if not case_file:
        return ""
    parts = os.path.normpath(case_file).split(os.sep)
    for i, part in enumerate(parts):
        if part.lower() == "cases" and i + 1 < len(parts):
            candidate = parts[i + 1]
            if not candidate.endswith(".tcs"):  # 排除 cases/ 下直接放文件（无子目录）的误判
                return candidate
    return ""
