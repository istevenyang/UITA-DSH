"""
VarExprJudger — assert[]/verify[]（及共用同一派发逻辑的 if[]/elif[]）裸括号
变量比较/判空函数调用表达式判定（2026-07-27 新增）。

裸写法（无任何 kwarg）优先级：is_path_like() → is_var_expr()（本模块） →
默认 ExistenceJudger（当作"元素描述"去 resolve UI 元素）。三者字符集互斥，
无优先级歧义，见 core/actions/judgers/__init__.py::select_judgers()。

支持的函数调用写法（裸变量名，不加 ${}；未在 CaseContext 中声明的 token
原样作字面值参与比较/包含判定，以支持 equal(status_code, 200) 这类"一侧写
字面常量"的写法）：
    equal(var_a, var_b)       — 相等（数值可比较时按 float 比较，否则按字符串）
    not_equal(var_a, var_b)   — 不等（equal 取反）
    include(var_a, var_b)     — 双向包含：var_a 包含 var_b 或 var_b 包含 var_a
                                 均判定为 True
    empty(var)                — 判空，复用 core/api_utils.py::is_empty()
    not_empty(var)             — 判空取反

见 docs/plans/archive/02-dsl-actions-实施纪要.md。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

from uita.core.api_utils import is_empty

from .base import JudgeResult, JudgerBase, JudgerSpec

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult
    from uita.core.runner.case_context import CaseContext

_RE_ONE_ARG = re.compile(r"^(empty|not_empty)\((.+)\)$")
_RE_TWO_ARG = re.compile(r"^(equal|not_equal|include)\((.+)\)$")


def is_var_expr(text: str) -> bool:
    """裸括号内容是否命中变量比较/判空函数调用表达式。"""
    t = text.strip()
    if not t:
        return False
    return bool(_RE_ONE_ARG.match(t) or _RE_TWO_ARG.match(t))


def _split_two_args(inner: str) -> tuple[str, str]:
    """按顶层逗号切分两参数（引号内逗号受保护；不支持嵌套函数调用，
    见计划文档 §一"明确不做"）。参数数量不为 2 时抛 ValueError，由调用方
    转为判定失败而非向上抛异常。
    """
    parts: list[str] = []
    buf: list[str] = []
    quote_char: Optional[str] = None
    for ch in inner:
        if quote_char:
            buf.append(ch)
            if ch == quote_char:
                quote_char = None
            continue
        if ch in ("'", '"'):
            quote_char = ch
            buf.append(ch)
            continue
        if ch == ",":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    parts.append("".join(buf))
    parts = [p.strip() for p in parts]
    if len(parts) != 2:
        raise ValueError(f"期望 2 个参数，实际 {len(parts)} 个: {inner!r}")
    return parts[0], parts[1]


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _resolve_token(token: str, case_ctx: Optional["CaseContext"]) -> str:
    """裸变量名/字面值 → CaseContext 取值；未命中（未声明该变量）→ token
    原样作字面值（支持一侧写字面常量，如 equal(status_code, 200)）。
    """
    token = _strip_quotes(token.strip())
    if case_ctx is not None:
        val = case_ctx.get(token)
        if val is not None:
            return val
    return token


def _smart_eq(a: str, b: str) -> bool:
    """数值可比较时按 float 比较（"200"==200.0"），否则按字符串精确比较。"""
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def evaluate_var_expr(text: str, case_ctx: Optional["CaseContext"]) -> tuple[bool, str]:
    """返回 (是否为真, 说明文本)。供 VarExprJudger 与 if[]/elif[] 复用。"""
    t = text.strip()

    m = _RE_ONE_ARG.match(t)
    if m:
        fn, var = m.group(1), _strip_quotes(m.group(2).strip())
        # 判空必须能识别"变量未声明 = 空"，直接读 case_ctx.get() 原始返回值，
        # 不走 _resolve_token() 的"未命中回退为字面值"逻辑（那是给比较/包含
        # 场景用的，让"一侧写字面常量"成立；两者对"未声明"的语义故意不同：
        # 比较/包含场景变量名常代表待比较的字面值本身，判空场景变量名就是
        # 判空的直接目标，未声明=空是唯一合理语义）。
        raw_value = case_ctx.get(var) if case_ctx is not None else None
        is_empty_result = is_empty(raw_value)
        result = is_empty_result if fn == "empty" else not is_empty_result
        return result, f"{fn}({var}) → {result}"

    m = _RE_TWO_ARG.match(t)
    if m:
        fn = m.group(1)
        try:
            a_raw, b_raw = _split_two_args(m.group(2))
        except ValueError as exc:
            return False, str(exc)
        a, b = _resolve_token(a_raw, case_ctx), _resolve_token(b_raw, case_ctx)
        if fn == "equal":
            result = _smart_eq(a, b)
        elif fn == "not_equal":
            result = not _smart_eq(a, b)
        else:  # include：双向包含，var_a 包含 var_b 或 var_b 包含 var_a
            result = (str(b) in str(a)) or (str(a) in str(b))
        return result, f"{fn}({a_raw}={a!r}, {b_raw}={b!r}) → {result}"

    return False, f"无法解析的变量表达式: {text!r}"


class VarExprJudger(JudgerBase):
    name = "var_expr"

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        case_ctx = getattr(ctx.engine, "_case_context", None)
        ok, desc = evaluate_var_expr(spec.description, case_ctx)
        if ok:
            return JudgeResult(ok=True, last_value=desc)
        return JudgeResult(ok=False, reason=f"变量表达式判定失败: {desc}", last_value=desc)
