"""
TCS (TestCaseScript) Lexer — tokenizes a .tcs source file.

Token types:
  META_KEY   : name / app / os / priority / owner / zyid / description / tag / retryable / fail_fast / browser_mode  (header key)
  SECTION    : @setup / @teardown
  STEP       : step:
  ACTION     : 括号形式 "name[...]" 为形状匹配（任意标识符+方括号，2026-07-27
               起不再要求 name 在固定白名单内，具体是否已注册交由 dispatch
               时的 get_action() 判断）；裸词形式（无括号，如 click/wait）
               仍要求 name 在 _ACTION_KEYWORDS 白名单内。
  WAIT       : wait:
  ASSERT     : assert: / verify:
  SET        : set:
  IF         : if[cond] / if not[cond] / if ![cond] / if empty[cond] / if not empty[cond]
  ELIF       : elif[...]（同 IF 修饰语法）
  ELSE       : else（不带条件；else[...] 视为语法错误，见 Token.error）
  COMMENT    : # ...
  BLANK      : empty line
  UNKNOWN    : anything else

Full/half-width colon compatibility: both ： and : are accepted.

缩进（2026-07-25 新增，见
docs/plans/archive/02-dsl-actions-实施纪要.md）：
  - Lexer 层只**记录**每行前导空白信息（`Token.indent` = 前导空格字符数，
    `Token.has_tab_indent` = 前导空白是否含 tab），**不做任何合法性校验、
    不抛异常**——"缩进决定归属"的强制规则（4 空格倍数、不支持 tab、缩进跳跃）
    只在 parser 层解析 if/elif/else 块时按需触发校验（`parser._indent_level()`），
    避免影响存量 `.tcs` 文件中与 if/elif/else 无关内容的解析（历史上这些内容的
    缩进纯属排版，从不参与语法层级判断）。见计划文档 §3.7 风险 3。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto


class TType(Enum):
    META_KEY = auto()
    SECTION = auto()
    STEP = auto()
    ACTION = auto()
    WAIT = auto()
    ASSERT = auto()
    SET = auto()
    IF = auto()
    ELIF = auto()
    ELSE = auto()
    COMMENT = auto()
    BLANK = auto()
    UNKNOWN = auto()


@dataclass
class Token:
    ttype: TType
    value: str          # stripped logical value (without leading keyword)
    raw: str            # original source line
    line_no: int
    indent: int = 0             # 前导空白字符数（原始计数，未做倍数校验）
    has_tab_indent: bool = False  # 前导空白是否包含 tab
    error: str = ""       # 词法阶段已探测到的语法错误（如 else[cond]），parser 消费时应直接报错


# Normalize full-width colon → half-width
_FULL_WIDTH_COLON = "："


def _normalize(line: str) -> str:
    return line.replace(_FULL_WIDTH_COLON, ":")


def _leading_whitespace_info(raw_line: str) -> tuple[int, bool]:
    """返回 (前导空白字符数, 是否含 tab)，不做任何合法性判断/异常。"""
    stripped_len = len(raw_line) - len(raw_line.lstrip())
    leading = raw_line[:stripped_len]
    return len(leading), ("\t" in leading)


# Header meta keys
# 注意：description/tag 缺失曾导致真实 bug——_parse_header() 是"遇到第一个非
# META_KEY token 就终止"的循环，若 description/tag 排在 app/os/priority/owner
# 之前（几乎所有真实 .tcs 都是这个顺序），后续字段会被静默丢弃（详见
# docs/plans/archive/02-dsl-actions-实施纪要.md）。
_META_KEYS = {"name", "app", "os", "priority", "owner", "zyid", "description", "tag", "retryable", "fail_fast", "browser_mode"}

# 裸词（无括号）action 关键字白名单。
#
# 2026-07-27 起，仅用于裸词形式（如 "soft_reset"、"wait"）的识别——括号形式
# （"name[...]"）已改为形状匹配，不再依赖本白名单（见下方 _RE_ACTION_BRACKET
# 说明）。裸词形式天然歧义（任意单词都可能是裸 action 也可能是普通文本），
# 维持保守的显式枚举；新增 action 事实上均采用 "name[...]" 括号写法（即使
# 无参也写 "name[]"），极少需要为新 action 扩展本列表。
#
# 2026-07-27 清理：移除 4 个孤立占位关键字：send/wait_reply/clear_chat/
# del_chat/switch_model（历史背景见
# docs/plans/archive/02-dsl-actions-实施纪要.md）。
# 这些关键字此前既无对应 @register_action 实现，也无任何存量 .tcs 引用
# （已核实 cases/ 全量语料库零命中），纯粹的死代码。
_ACTION_KEYWORDS = {
    "click", "visual_click", "type", "press_key", "hover", "scroll",
    "swipe", "drag", "drag_between", "focus", "clear", "submit",
    "assert", "verify", "wait", "soft_reset",
    # 跨应用切换（M10，docs/plans/archive/02-dsl-actions-实施纪要.md）
    "switch", "switch_back",
    "open",
    "upload_file", "file_picker", "select_file", "assert_audit_log", "dlp_assert_audit_log",
    # sample_web app-scoped actions
    "jump", "quit_app",
    # 全局：系统文件选择对话框路径跳转
    "choose_file",
    # 全局：文件系统操作 / 唤起访达资源管理器（见
    # docs/plans/archive/02-dsl-actions-实施纪要.md）
    "file_op", "open_path",
    # 全局：关闭当前窗口快捷键
    "close_window",
    # 全局：本地文件存在性检查
    "exist",
}

# Patterns
_RE_SECTION = re.compile(r"^@(setup|teardown)\s*$", re.IGNORECASE)
_RE_STEP = re.compile(r"^step\s*:\s*(.*)$", re.IGNORECASE)
_RE_WAIT = re.compile(r"^wait\s*:\s*(.*)$", re.IGNORECASE)
_RE_ASSERT = re.compile(r"^(assert|verify)\s*:\s*(.*)$", re.IGNORECASE)
_RE_SET = re.compile(r"^set\s*:\s*(.+)$", re.IGNORECASE)
_RE_META = re.compile(r"^(\w+)\s*:\s*(.*)$")
# if/elif 后必须紧跟空格或 "["（避免 "ifxxx" 之类的裸词误判），修饰词
# not[]/![]/empty[]/not empty[]/!empty[] 与外层方括号的具体拆分交给 parser 层
# （core/dsl/parser.py::_parse_condition），lexer 只负责整体捕获剩余文本。
_RE_IF = re.compile(r"^if(?=[\s\[])\s*(.+)$", re.IGNORECASE)
_RE_ELIF = re.compile(r"^elif(?=[\s\[])\s*(.+)$", re.IGNORECASE)
_RE_ELSE_BRACKET = re.compile(r"^else\s*\[.*\]\s*$", re.IGNORECASE)
_RE_ELSE_BARE = re.compile(r"^else\s*$", re.IGNORECASE)
# 注意：括号内用 (.*) 而非 (.+) —— 无参 thick action（如 close_window[]/
# soft_reset[]）空括号是合法调用，(.+) 会导致空括号整行判定为 UNKNOWN 被静默
# 跳过（真实 bug，历史 app 专属 Action 中已验证：无参厚封装若用 (.+) 会从未
# 被执行过）。
#
# 2026-07-27 起改为"形状匹配"而非关键字白名单匹配：任意合法标识符紧跟
# "[...]"（无冒号）即判定为 ACTION token，具体名字是否为真正已注册的 action
# 交由 core/actions/registry.py::get_action() 在 dispatch 时判断（未注册 →
# 显式抛 ActionNotFoundError，而不是词法层静默吞掉整行）。
#
# 动机：历史上曾因新增 action 忘记同步本白名单，导致 exist[]/close_window[]/
# choose_file[] 三个已注册、单测能 import 到的 action 在真实 .tcs 执行链路
# 中完全不可用且零报错（词法层直接判定 UNKNOWN，parser 静默跳过整行）——
# 详见 tests/core/dsl/test_action_keyword_registry_sync.py。改为形状匹配后，
# 新增任何 "<name>[...]" 形式的全局/app-scoped action **无需再改动本文件**，
# 该类回归从架构上被消除（不再依赖人工记忆同步）。
#
# 不会与其它 token 类型冲突：SECTION/STEP/WAIT/ASSERT/SET/IF/ELIF/ELSE 均在
# 本正则之前被检查（见 tokenize() 内 elif 链顺序），且均要求冒号或专属前缀
# （if/elif/else），与"标识符+方括号、无冒号"的形状互斥；META_KEY 同样要求
# 冒号，`key: value` 形状与本正则不重叠。已用全量存量 440 个 .tcs 语料库验证
# 零行为变化（原 UNKNOWN 判定的两行均为纯文本描述，不含 "标识符[...]" 形状）。
_RE_ACTION_BRACKET = re.compile(r"^([A-Za-z_]\w*)\s*\[(.*)\]\s*$")
_RE_ACTION_BARE = re.compile(
    r"^(" + "|".join(_ACTION_KEYWORDS) + r")\s*$",
    re.IGNORECASE,
)


def tokenize(source: str) -> list[Token]:
    tokens: list[Token] = []
    for lineno, raw_line in enumerate(source.splitlines(), start=1):
        stripped = raw_line.strip()
        norm = _normalize(stripped)

        if not norm:
            tokens.append(Token(TType.BLANK, "", raw_line, lineno))
            continue

        indent, has_tab = _leading_whitespace_info(raw_line)

        if norm.startswith("#"):
            tokens.append(Token(TType.COMMENT, norm[1:].strip(), raw_line, lineno, indent=indent, has_tab_indent=has_tab))
            continue

        m = _RE_SECTION.match(norm)
        if m:
            tokens.append(Token(TType.SECTION, norm.lstrip("@").lower(), raw_line, lineno, indent=indent, has_tab_indent=has_tab))
            continue

        m = _RE_STEP.match(norm)
        if m:
            tokens.append(Token(TType.STEP, m.group(1).strip(), raw_line, lineno, indent=indent, has_tab_indent=has_tab))
            continue

        m = _RE_WAIT.match(norm)
        if m:
            tokens.append(Token(TType.WAIT, m.group(1).strip(), raw_line, lineno, indent=indent, has_tab_indent=has_tab))
            continue

        m = _RE_ASSERT.match(norm)
        if m:
            kw = m.group(1).lower()
            tokens.append(Token(TType.ASSERT, f"{kw}:{m.group(2).strip()}", raw_line, lineno, indent=indent, has_tab_indent=has_tab))
            continue

        m = _RE_SET.match(norm)
        if m:
            tokens.append(Token(TType.SET, m.group(1).strip(), raw_line, lineno, indent=indent, has_tab_indent=has_tab))
            continue

        m = _RE_IF.match(norm)
        if m:
            tokens.append(Token(TType.IF, m.group(1).strip(), raw_line, lineno, indent=indent, has_tab_indent=has_tab))
            continue

        m = _RE_ELIF.match(norm)
        if m:
            tokens.append(Token(TType.ELIF, m.group(1).strip(), raw_line, lineno, indent=indent, has_tab_indent=has_tab))
            continue

        if _RE_ELSE_BRACKET.match(norm):
            tokens.append(Token(
                TType.ELSE, "", raw_line, lineno, indent=indent, has_tab_indent=has_tab,
                error="else 不接受条件参数（else 后不能带方括号，写作裸 else）",
            ))
            continue

        if _RE_ELSE_BARE.match(norm):
            tokens.append(Token(TType.ELSE, "", raw_line, lineno, indent=indent, has_tab_indent=has_tab))
            continue

        m = _RE_ACTION_BRACKET.match(norm) or _RE_ACTION_BARE.match(norm)
        if m:
            tokens.append(Token(TType.ACTION, norm, raw_line, lineno, indent=indent, has_tab_indent=has_tab))
            continue

        m = _RE_META.match(norm)
        if m and m.group(1).lower() in _META_KEYS:
            tokens.append(Token(
                TType.META_KEY, f"{m.group(1).lower()}:{m.group(2).strip()}", raw_line, lineno,
                indent=indent, has_tab_indent=has_tab,
            ))
            continue

        tokens.append(Token(TType.UNKNOWN, norm, raw_line, lineno, indent=indent, has_tab_indent=has_tab))

    return tokens
