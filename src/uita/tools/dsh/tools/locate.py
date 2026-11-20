"""定位类工具：只读调用既有 Resolver 链，返回命中层级与几何证据。

不新建定位算法、不改 rosetta_map：层级（L1 … L5/NOT_FOUND）原样透出，供
模型判断"这次命中可靠不可靠"，也让后续人工复核有据可查。
"""
from __future__ import annotations

from typing import Any

from ..registry import (TIER_READ, DshTool, ToolResult, register_dsh_tool)


def element_geometry(element: Any) -> dict[str, Any]:
    """把 ElementRef 的可选几何信息收敛成 dict（缺就不写，不臆造）。"""
    geometry: dict[str, Any] = {}
    if element is None:
        return geometry
    coords = getattr(element, "coords", None)
    if coords:
        geometry["coords"] = list(coords)
    bbox = getattr(element, "bbox", None)
    if bbox:
        geometry["bbox"] = list(bbox)
    text = getattr(element, "text", "")
    if text:
        geometry["text"] = str(text)[:200]
    return geometry


@register_dsh_tool
class LocateTool(DshTool):
    name = "locate"
    tier = TIER_READ
    description = (
        "按元素语义名在 rosetta_map / 语义 / 文本 / 标签 / 角色 / LLM / 视觉指纹链上定位，"
        "返回命中层级与坐标。未命中返回失败（不要臆造坐标代替定位）。"
    )
    params = {
        "target": {"type": "string", "description": "元素语义名（rosetta_map 中的名字或可读描述）"},
        "skip_l4": {"type": "boolean", "description": "跳过 LLM 视觉层（省时省额度）"},
    }
    required = ("target",)

    async def run(self, ctx: Any, target: str,
                  skip_l4: bool = False) -> ToolResult:
        engine = ctx.require_engine()
        resolver = ctx.get_resolver(engine)
        result = await resolver.resolve(target, skip_l4=skip_l4)
        level = getattr(getattr(result, "level", None), "value", "NOT_FOUND")
        data: dict[str, Any] = {"target": target, "level": level,
                                "found": bool(getattr(result, "found", False))}
        if not data["found"]:
            return ToolResult(ok=False, data=data,
                              error=f"未定位到元素「{target}」（level={level}）")
        element = result.element
        data.update(element_geometry(element))
        rect_fn = getattr(engine, "get_element_rect", None)
        if callable(rect_fn):
            try:
                rect = await rect_fn(element)
                if rect:
                    data["rect"] = list(rect)
            except Exception:  # noqa: BLE001 - 几何信息属可选增强
                pass
        if level != "L1_MAP_EXACT":
            data["fallback"] = True
        return ToolResult(ok=True, data=data)
