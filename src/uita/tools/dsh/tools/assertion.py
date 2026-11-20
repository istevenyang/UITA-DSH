"""断言类工具：三态契约（true / false / unknown）。

**unknown 不折算通过**：判定不了就如实说"判定不了"，由 harness 计入
``unknown_count``，会话结论不得因此变成 passed（见计划 §5.3）。

每条断言都带回判定层级与证据，禁止"模型自己说成立"。
"""
from __future__ import annotations

import re
from typing import Any

from ..registry import (TIER_READ, DshTool, ToolResult, register_dsh_tool)

VERDICT_TRUE = "true"
VERDICT_FALSE = "false"
VERDICT_UNKNOWN = "unknown"


def _unknown(reason: str, **data: Any) -> ToolResult:
    return ToolResult(ok=True, data={
        "verdict": VERDICT_UNKNOWN, "detail": reason, **data,
    })


def _verdict(value: bool, judger: str, **data: Any) -> ToolResult:
    return ToolResult(ok=True, data={
        "verdict": VERDICT_TRUE if value else VERDICT_FALSE,
        "judger": judger,
        **data,
    })


async def _locate(ctx: Any, target: str, skip_l4: bool) -> tuple[Any, str]:
    engine = ctx.require_engine()
    resolver = ctx.get_resolver(engine)
    result = await resolver.resolve(target, skip_l4=skip_l4)
    level = getattr(getattr(result, "level", None), "value", "NOT_FOUND")
    return result, level


@register_dsh_tool
class AssertExistTool(DshTool):
    name = "assert_exist"
    tier = TIER_READ
    description = "断言元素存在（走 Resolver 全链，含视觉/OCR 兜底）。返回 true/false/unknown。"
    params = {
        "target": {"type": "string", "description": "元素语义名"},
        "skip_l4": {"type": "boolean", "description": "跳过 LLM 视觉层"},
    }
    required = ("target",)

    async def run(self, ctx: Any, target: str, skip_l4: bool = False) -> ToolResult:
        if ctx.engine is None:
            return _unknown("引擎未准备", target=target)
        try:
            result, level = await _locate(ctx, target, skip_l4)
        except Exception as exc:  # noqa: BLE001 - 定位异常=判定不了
            return _unknown(f"{type(exc).__name__}: {exc}", target=target)
        return _verdict(bool(getattr(result, "found", False)),
                        "resolver:exists", target=target, level=level)


@register_dsh_tool
class AssertAbsentTool(DshTool):
    name = "assert_absent"
    tier = TIER_READ
    description = "断言元素不存在（用于验证弹窗/提示已消失）。返回 true/false/unknown。"
    params = {
        "target": {"type": "string", "description": "元素语义名"},
        "skip_l4": {"type": "boolean", "description": "跳过 LLM 视觉层"},
    }
    required = ("target",)

    async def run(self, ctx: Any, target: str, skip_l4: bool = False) -> ToolResult:
        if ctx.engine is None:
            return _unknown("引擎未准备", target=target)
        try:
            result, level = await _locate(ctx, target, skip_l4)
        except Exception as exc:  # noqa: BLE001
            return _unknown(f"{type(exc).__name__}: {exc}", target=target)
        return _verdict(not bool(getattr(result, "found", False)),
                        "resolver:absent", target=target, level=level)


@register_dsh_tool
class AssertTextTool(DshTool):
    name = "assert_text"
    tier = TIER_READ
    description = "断言元素可见文本满足条件（match=contains/equals/regex）。返回 true/false/unknown。"
    params = {
        "target": {"type": "string", "description": "元素语义名"},
        "expected": {"type": "string", "description": "期望文本/正则"},
        "match": {"type": "string", "enum": ["contains", "equals", "regex"]},
    }
    required = ("target", "expected")

    async def run(self, ctx: Any, target: str, expected: str,
                  match: str = "contains") -> ToolResult:
        if ctx.engine is None:
            return _unknown("引擎未准备", target=target)
        try:
            result, level = await _locate(ctx, target, False)
        except Exception as exc:  # noqa: BLE001
            return _unknown(f"{type(exc).__name__}: {exc}", target=target)
        if not getattr(result, "found", False):
            return _verdict(False, "resolver:text", target=target, level=level,
                            reason="元素未找到，无法读取文本")
        try:
            actual = await ctx.engine.get_text(result.element)
        except Exception as exc:  # noqa: BLE001
            return _unknown(f"读取文本失败: {type(exc).__name__}: {exc}", target=target)
        actual_text = str(actual or "")
        if match == "equals":
            passed = actual_text == expected
        elif match == "regex":
            try:
                passed = re.search(expected, actual_text) is not None
            except re.error as exc:
                return _unknown(f"正则非法: {exc}", target=target)
        else:
            passed = expected in actual_text
        return _verdict(passed, "engine:text", target=target, level=level,
                        expected=expected, actual=actual_text[:500], match=match)


@register_dsh_tool
class AssertValueTool(DshTool):
    name = "assert_value"
    tier = TIER_READ
    description = "断言元素属性值匹配（attr 如 value/checked/enabled）。返回 true/false/unknown。"
    params = {
        "target": {"type": "string", "description": "元素语义名"},
        "attr": {"type": "string", "description": "属性名"},
        "expected": {"type": "string", "description": "期望值"},
    }
    required = ("target", "attr", "expected")

    async def run(self, ctx: Any, target: str, attr: str,
                  expected: str) -> ToolResult:
        if ctx.engine is None:
            return _unknown("引擎未准备", target=target)
        try:
            result, level = await _locate(ctx, target, False)
        except Exception as exc:  # noqa: BLE001
            return _unknown(f"{type(exc).__name__}: {exc}", target=target)
        if not getattr(result, "found", False):
            return _verdict(False, "resolver:value", target=target, level=level,
                            reason="元素未找到，无法读取属性")
        try:
            actual = await ctx.engine.get_attribute(result.element, attr)
        except Exception as exc:  # noqa: BLE001
            return _unknown(f"读取属性失败: {type(exc).__name__}: {exc}", target=target)
        if actual is None:
            return _unknown(f"属性 {attr} 不存在", target=target)
        return _verdict(str(actual) == expected, "engine:attribute",
                        target=target, level=level, attr=attr,
                        expected=expected, actual=str(actual)[:200])


@register_dsh_tool
class AssertJsTool(DshTool):
    name = "assert_js"
    tier = TIER_READ
    description = (
        "执行 rosetta_map 中该元素的 js 表达式并断言取值（仅 web/Playwright 引擎支持）；"
        "引擎不支持时返回 unknown，不假装通过。"
    )
    params = {
        "target": {"type": "string", "description": "元素语义名（其 js 字段承载表达式）"},
        "expected": {"type": "string", "description": "期望的字符串化取值"},
    }
    required = ("target", "expected")

    async def run(self, ctx: Any, target: str, expected: str) -> ToolResult:
        if ctx.engine is None:
            return _unknown("引擎未准备", target=target)
        try:
            resolver = ctx.get_resolver(ctx.engine)
            expression = resolver.get_js_expr(target)
        except Exception as exc:  # noqa: BLE001
            return _unknown(f"{type(exc).__name__}: {exc}", target=target)
        if not expression:
            return _unknown(f"元素「{target}」未声明 js 表达式", target=target)
        eval_fn = getattr(ctx.engine, "eval_js", None) or getattr(
            ctx.engine, "evaluate_js", None,
        )
        if not callable(eval_fn):
            return _unknown("当前引擎不支持 js 表达式求值", target=target)
        try:
            actual = await eval_fn(expression)
        except Exception as exc:  # noqa: BLE001
            return _unknown(f"js 求值失败: {type(exc).__name__}: {exc}", target=target)
        actual_text = str(actual)
        return _verdict(actual_text == expected, "engine:js", target=target,
                        expression=expression[:300], expected=expected,
                        actual=actual_text[:200])
