"""操作类工具：元素优先（R11）。

坐标操作只在"元素名不可用/定位失败"时使用，且必须显式声明 ``source``
（来源说明），否则直接拒绝——避免模型用裸坐标绕过元素识别机制。
"""
from __future__ import annotations

from typing import Any

from ..registry import (TIER_ACTION, DshTool, ToolResult, register_dsh_tool)

_DIRECTIONS = ("up", "down", "left", "right")


def _element_ref(coords: tuple[float, float]) -> Any:
    from uita.core.engine.base import ElementRef

    return ElementRef(element_id=f"coords:{coords[0]},{coords[1]}", coords=coords)


async def _resolve_element(ctx: Any, engine: Any, target: str) -> tuple[Any, str]:
    resolver = ctx.get_resolver(engine)
    result = await resolver.resolve(target)
    if not getattr(result, "found", False):
        level = getattr(getattr(result, "level", None), "value", "NOT_FOUND")
        raise LookupError(f"未定位到元素「{target}」（level={level}）")
    level = getattr(getattr(result, "level", None), "value", "")
    return result.element, level


def _coords_from(x: Any, y: Any) -> tuple[float, float] | None:
    if x is None or y is None:
        return None
    return (float(x), float(y))


@register_dsh_tool
class ActClickTool(DshTool):
    name = "act_click"
    tier = TIER_ACTION
    description = (
        "点击元素（优先用元素语义名走 Resolver 定位）。仅当元素名不可用时才用 x/y 坐标，"
        "此时必须填 source 说明来源（如「视觉指纹命中」「截图目测」）。"
    )
    params = {
        "target": {"type": "string", "description": "元素语义名"},
        "x": {"type": "number", "description": "屏幕逻辑坐标 X（兜底，需配 source）"},
        "y": {"type": "number", "description": "屏幕逻辑坐标 Y（兜底，需配 source）"},
        "source": {"type": "string", "description": "坐标来源说明（使用 x/y 时必填）"},
    }

    async def run(self, ctx: Any, target: str = "", x: float | None = None,
                  y: float | None = None, source: str = "") -> ToolResult:
        engine = ctx.require_engine()
        coords = _coords_from(x, y)
        if coords is not None:
            if not str(source).strip():
                return ToolResult.fail("使用坐标点击必须提供 source 说明来源（R11 护栏）")
            await engine.click(_element_ref(coords))
            data = {"mode": "coords", "coords": list(coords), "source": source}
            ctx.set_observation(data)
            return ToolResult(ok=True, data=data)
        if not str(target).strip():
            return ToolResult.fail("必须提供 target 元素名或 x/y 坐标")
        element, level = await _resolve_element(ctx, engine, target)
        await engine.click(element)
        data = {"mode": "element", "target": target, "level": level}
        ctx.set_observation(data)
        return ToolResult(ok=True, data=data)


@register_dsh_tool
class ActTypeTool(DshTool):
    name = "act_type"
    tier = TIER_ACTION
    description = "向元素输入文本（默认先清空）；submit=true 时追加回车。"
    params = {
        "target": {"type": "string", "description": "元素语义名"},
        "text": {"type": "string", "description": "要输入的文本"},
        "clear": {"type": "boolean", "description": "输入前是否清空（默认 true）"},
        "submit": {"type": "boolean", "description": "输入后是否按回车"},
    }
    required = ("target", "text")

    async def run(self, ctx: Any, target: str, text: str,
                  clear: bool = True, submit: bool = False) -> ToolResult:
        engine = ctx.require_engine()
        element, level = await _resolve_element(ctx, engine, target)
        await engine.type_text(element, text, clear=clear)
        if submit:
            await engine.press_key("Enter")
        data = {"target": target, "level": level, "chars": len(text), "submit": submit}
        ctx.set_observation(data)
        return ToolResult(ok=True, data=data)


@register_dsh_tool
class ActPressKeyTool(DshTool):
    name = "act_press_key"
    tier = TIER_ACTION
    description = "按单键（如 Enter / Tab / Escape / Cmd+W）。"
    params = {"key": {"type": "string", "description": "键名"}}
    required = ("key",)

    async def run(self, ctx: Any, key: str) -> ToolResult:
        engine = ctx.require_engine()
        await engine.press_key(key)
        return ToolResult(ok=True, data={"key": key})


@register_dsh_tool
class ActHoverTool(DshTool):
    name = "act_hover"
    tier = TIER_ACTION
    description = "悬停在元素上（用于展开菜单/提示）。"
    params = {"target": {"type": "string", "description": "元素语义名"}}
    required = ("target",)

    async def run(self, ctx: Any, target: str) -> ToolResult:
        engine = ctx.require_engine()
        element, level = await _resolve_element(ctx, engine, target)
        await engine.hover(element)
        return ToolResult(ok=True, data={"target": target, "level": level})


@register_dsh_tool
class ActScrollTool(DshTool):
    name = "act_scroll"
    tier = TIER_ACTION
    description = "滚动窗口或指定元素（direction 取 up/down/left/right）。"
    params = {
        "direction": {"type": "string", "enum": list(_DIRECTIONS)},
        "amount": {"type": "integer", "description": "滚动量（像素/格，默认 300）"},
        "target": {"type": "string", "description": "元素语义名（缺省滚动当前窗口）"},
    }
    required = ("direction",)

    async def run(self, ctx: Any, direction: str, amount: int = 300,
                  target: str = "") -> ToolResult:
        if direction not in _DIRECTIONS:
            return ToolResult.fail(f"非法 direction={direction!r}")
        engine = ctx.require_engine()
        element = None
        level = ""
        if str(target).strip():
            element, level = await _resolve_element(ctx, engine, target)
        await engine.scroll(element, direction, int(amount))
        return ToolResult(ok=True, data={
            "direction": direction, "amount": int(amount),
            "target": target, "level": level,
        })
