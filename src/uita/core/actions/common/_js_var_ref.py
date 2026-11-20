"""共享辅助：整串 "$var"/"${var}" 变量引用解析（供 eval_js[]/assert[js=...]/
if[js=...] 三处 JS 相关能力复用）。

2026-07-28 提取（原逻辑仅存在于 action_eval_js.py，本次发现
assert[js=$var]/if[js=$var] 完全不支持变量替换——JsJudger 对 js= kwarg 值
不做任何替换，直接原样传给 engine.evaluate()，"$var" 会被浏览器当作引用一个
名为 "$var" 的全局变量执行，通常触发 ReferenceError，被吞成断言失败，而非
"取变量值后执行 JS"。提取为共享 helper 后 eval_js[]/assert[js=]/if[js=]
三处行为统一，避免各自实现一份、逻辑漂移）。

为何不复用 core/runner/case_context.py::CaseContext.render()（"${key}" 子串
扫描替换）：JS 表达式本身可能含模板字符串（``` `Hello ${name}` ```）或
jQuery 风格的 ``$(...)``，子串扫描替换会误伤这类内联 JS 语法。因此改为
**整串精确匹配**——只有当整个参数恰好是 ``$name`` 或 ``${name}`` 时才视为
UITA 变量引用去取值，其余情况（包括内部含 "${...}"/"$(...)" 的内联 JS）
原样返回，不做任何替换。
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext

# 仅整串匹配 "$name" 或 "${name}"，不做子串扫描（见模块 docstring）。
_RE_BARE_VAR_REF = re.compile(r"^\$\{?([A-Za-z_]\w*)\}?$")


def is_js_var_ref(raw: str) -> bool:
    """判断 raw 整串是否恰好是 ``$name``/``${name}`` 变量引用形状（不解析值，
    纯字符串形状判断）。2026-07-28 新增，供 `action_eval_js.py::_resolve_js_source()`
    区分"变量引用"分支与"rosetta_map 元素名查表"分支（两者互斥，变量引用优先，
    命中即返回，不再落到 rosetta_map 查表逻辑）。
    """
    return bool(_RE_BARE_VAR_REF.match(raw.strip()))


def resolve_js_var_ref(ctx: "ActionContext", raw: str) -> tuple[Optional[str], str]:
    """解析一段 JS 相关参数，识别是否为整串变量引用并取值。

    返回 ``(js_code, error_message)``：
      - 参数整串匹配 ``$name``/``${name}`` → 视为变量引用，从
        ``ctx.engine._case_context``（``CaseContext`` 实例）取值；变量未定义
        （或引擎无 ``_case_context``）时 ``js_code=None``，``error_message``
        给出明确原因（供调用方直接作为失败理由，不静默把无效引用当字面量
        执行导致下游产出费解的 JS 语法错误）。
      - 其余情况（含内联 JS，即使内部含有 ``${...}``/``$(...)`` 语法）→
        原样作为字面量返回，``error_message`` 为空串。
    """
    stripped = raw.strip()
    m = _RE_BARE_VAR_REF.match(stripped)
    if not m:
        return raw, ""

    var_name = m.group(1)
    case_ctx = getattr(ctx.engine, "_case_context", None)
    value = case_ctx.get(var_name) if case_ctx is not None else None
    if value is None:
        return None, f"变量 '{var_name}' 未定义（引用写法: {stripped!r}）"
    return value, ""
