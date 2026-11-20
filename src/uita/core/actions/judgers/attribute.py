"""
AttributeJudger — disabled=/checked=/value=/attr_<name>= 属性判定。

依赖 EngineBase.get_attribute()（全平台通用）。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from .base import JudgeResult, JudgerBase, JudgerSpec

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult

_BOOL_ATTRS = {"disabled", "checked"}


def _to_bool(raw: str) -> bool:
    return raw.strip().lower() not in ("false", "0", "", "no")


class AttributeJudger(JudgerBase):
    name = "attribute"

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        if resolve_result is None or not resolve_result.found:
            return JudgeResult(ok=False, reason=f"元素不存在，无法判定属性: '{spec.description}'")

        element = resolve_result.element
        for key, raw_expected in spec.kwargs.items():
            if key in _BOOL_ATTRS:
                attr_name = key
            elif key == "value":
                attr_name = "value"
            elif key.startswith("attr_"):
                attr_name = key[len("attr_"):]
            else:
                continue  # 不属于本判定器负责的 kwarg（由其他判定器处理）

            actual = await ctx.engine.get_attribute(element, attr_name)

            if key in _BOOL_ATTRS:
                expected_bool = _to_bool(raw_expected)
                actual_bool = actual is not None and _to_bool(actual)
                if actual_bool != expected_bool:
                    return JudgeResult(
                        ok=False,
                        reason=f"属性 {attr_name}={actual!r} 与期望 {expected_bool} 不符",
                        last_value=str(actual),
                    )
            else:
                if (actual or "") != raw_expected:
                    return JudgeResult(
                        ok=False,
                        reason=f"属性 {attr_name}={actual!r} 与期望 {raw_expected!r} 不符",
                        last_value=str(actual),
                    )
        return JudgeResult(ok=True)
