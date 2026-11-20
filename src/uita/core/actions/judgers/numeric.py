"""有限数值比较：lt/le/gt/ge 与左闭右开的 between，多个约束按 AND。"""
from __future__ import annotations

import operator
import re
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Optional

from .base import JudgeResult, JudgerBase, JudgerSpec

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult


_COMPARATORS = {"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge}
_NUMBER_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")


def _strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].strip()
    return text


def _render(raw: str, case_context: object, label: str) -> str:
    if raw is None:
        raise ValueError(f"{label} 为空值: {raw!r}")
    if isinstance(raw, bool):
        raise ValueError(f"{label} 不接受 bool: {raw!r}")
    if not isinstance(raw, str):
        raise ValueError(f"{label} 必须为数值文本: {raw!r}")
    text = _strip_quotes(raw)
    if "${" in text:
        render = getattr(case_context, "render", None)
        if not callable(render):
            raise ValueError(f"{label} 变量未定义（无 CaseContext）: {raw!r}")
        try:
            text = render(text)
        except Exception as exc:
            raise ValueError(f"{label} 变量渲染失败: {raw!r}: {exc}") from exc
        if not isinstance(text, str):
            raise ValueError(f"{label} 变量未返回数值文本: {raw!r} → {text!r}")
        if "${" in text:
            raise ValueError(f"{label} 变量未定义或引用非法: {raw!r} → {text!r}")
        text = _strip_quotes(text)
    if not text:
        raise ValueError(f"{label} 为空值: {raw!r}")
    return text


def _number(text: str, label: str) -> Decimal:
    text = _strip_quotes(text)
    if not text:
        raise ValueError(f"{label} 为空值: {text!r}")
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{label} 不是合法数值: {text!r}") from exc
    if not value.is_finite():
        raise ValueError(f"{label} 必须为有限数值（拒绝 NaN/Infinity）: {text!r}")
    if not _NUMBER_RE.fullmatch(text):
        raise ValueError(f"{label} 不是合法数值文本: {text!r}")
    return value


class NumericJudger(JudgerBase):
    name = "numeric"

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        case_context = getattr(ctx.engine, "_case_context", None)
        original = f"target={spec.description!r}, kwargs={spec.kwargs!r}"
        try:
            target = _number(_render(spec.description, case_context, "目标"), "目标")
            constraints: list[tuple[str, tuple[Decimal, ...]]] = []
            for key, raw in spec.kwargs.items():
                if key in _COMPARATORS:
                    boundary = _number(_render(raw, case_context, f"{key} 边界"), f"{key} 边界")
                    constraints.append((key, (boundary,)))
                elif key == "between":
                    text = _render(raw, case_context, "between 区间")
                    parts = text.split(",")
                    if len(parts) != 2:
                        raise ValueError(f"between 期望两个边界（lower,upper）: {text!r}")
                    lower = _number(parts[0], "between 下界")
                    upper = _number(parts[1], "between 上界")
                    if lower >= upper:
                        raise ValueError(f"between 区间非法，下界必须小于上界: {text!r}")
                    constraints.append((key, (lower, upper)))
            if not constraints:
                raise ValueError("未声明数值约束 lt/le/gt/ge/between")
        except ValueError as exc:
            return JudgeResult(ok=False, reason=f"数值判定失败: {exc}; {original}", last_value=original)

        failed: list[str] = []
        for key, boundaries in constraints:
            if key == "between":
                lower, upper = boundaries
                if not lower <= target < upper:
                    failed.append(f"between=[{lower},{upper})")
            elif not _COMPARATORS[key](target, boundaries[0]):
                failed.append(f"{key}={boundaries[0]}")
        last_value = f"value={target}; {original}"
        if failed:
            return JudgeResult(
                ok=False,
                reason=f"数值判定失败: value={target} 未满足 {' AND '.join(failed)}; {original}",
                last_value=last_value,
            )
        return JudgeResult(ok=True, last_value=last_value)
