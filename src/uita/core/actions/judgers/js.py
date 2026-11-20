"""
JsJudger — js= 任意 JS 表达式判定（返回真值即通过）。

**Web only**：依赖 P1 引入的 PlaywrightEngine.evaluate()（非 EngineBase 契约）。

**变量引用支持**（2026-07-28 新增）：``js=`` 值支持整串 ``$name``/``${name}``
变量引用——``assert[js=$js_script]``/``if[js=$js_script]`` 会先从
``CaseContext`` 取出 ``js_script`` 的实际值再执行，而非把字面字符串
``"$js_script"`` 当 JS 执行（此前的缺陷：``$`` 是合法 JS 标识符字符，字面
执行会被浏览器当作引用一个不存在的全局变量，触发 ReferenceError，被下方
``except Exception`` 吞成判定失败，看似"断言失败"实为"变量引用从未生效"）。
复用 `core/actions/common/_js_var_ref.py::resolve_js_var_ref()`（与
`core/actions/common/action_eval_js.py::EvalJsAction` 共用同一份解析逻辑，
行为统一，见该模块的 "为何不复用 CaseContext.render()" 说明）：只有整串
恰好是 ``$name``/``${name}`` 时才当变量引用，内联 JS（即使含
``${...}``/``$(...)`` 语法）原样执行，不做子串替换。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

import re
import uita.config as settings

from uita.core.actions.common._js_var_ref import resolve_js_var_ref

from .base import JudgeResult, JudgerBase, JudgerSpec

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult


_CASE_VAR_RE = re.compile(r"\$\{\{([A-Za-z_]\w*)\}\}")


def _substitute_case_variables(expr: str) -> tuple[Optional[str], str]:
    """Expand unambiguous TCS double-braced tokens in rosetta-map JS."""
    missing: list[str] = []

    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name not in settings.CASE_VARIABLES:
            missing.append(name)
            return match.group(0)
        return str(settings.CASE_VARIABLES[name]).replace("\\", "\\\\")

    rendered = _CASE_VAR_RE.sub(replace, expr)
    if missing:
        return None, "undefined case variables: " + ", ".join(sorted(set(missing)))
    return rendered, ""


class JsJudger(JudgerBase):
    name = "js"

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        from uita.engine.playwright.engine import PlaywrightEngine

        if not isinstance(ctx.engine, PlaywrightEngine):
            return JudgeResult(ok=False, reason="仅 Web 引擎支持该判定（js=）")

        expr_raw = spec.kwargs.get("js", "")
        expr, var_err = resolve_js_var_ref(ctx, expr_raw)
        if expr is None:
            return JudgeResult(ok=False, reason=f"js 表达式变量引用错误: {var_err}")
        expr, case_var_err = _substitute_case_variables(expr)
        if expr is None:
            return JudgeResult(ok=False, reason=f"js case-variable error: {case_var_err}")

        try:
            result = await ctx.engine.evaluate(expr)
        except Exception as exc:  # noqa: BLE001 — JS 执行异常视为判定失败，不向上抛出
            return JudgeResult(ok=False, reason=f"js 表达式执行异常: {exc}")

        if not result:
            return JudgeResult(ok=False, reason=f"js 表达式 {expr!r} 返回假值: {result!r}", last_value=str(result))
        return JudgeResult(ok=True, last_value=str(result))
