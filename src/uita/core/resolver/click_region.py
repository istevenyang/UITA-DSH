"""
ClickRegion — 视觉指纹（L3.8）命中后的九宫格区域点击坐标计算。

计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md §3.2

纯函数工具，零外部依赖（符合 R3/R14：core/resolver/ 内部横向工具，不 import
Engine/Registry/Action/Resolver 等运行时模块）。

设计要点：区域点击坐标采用 bbox 内 1/4~3/4 的"四分点"而非字面边界点
（如 RIGHT 不是 (x+w, y+h/2) 而是 (x+w*0.75, y+h/2)），避免点击落在图标
边缘/描边像素上（抗锯齿容易导致点空或误点到相邻元素）。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional


class ClickRegion(Enum):
    CENTER = "center"
    TOP = "top"
    BOTTOM = "bottom"
    LEFT = "left"
    RIGHT = "right"
    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"


# 归一化到 [0, 1] 区间内的四分点系数，供 point_for_region() 统一计算
_REGION_RATIOS: dict[ClickRegion, tuple[float, float]] = {
    ClickRegion.CENTER: (0.50, 0.50),
    ClickRegion.TOP: (0.50, 0.25),
    ClickRegion.BOTTOM: (0.50, 0.75),
    ClickRegion.LEFT: (0.25, 0.50),
    ClickRegion.RIGHT: (0.75, 0.50),
    ClickRegion.TOP_LEFT: (0.25, 0.25),
    ClickRegion.TOP_RIGHT: (0.75, 0.25),
    ClickRegion.BOTTOM_LEFT: (0.25, 0.75),
    ClickRegion.BOTTOM_RIGHT: (0.75, 0.75),
}


def parse_region(value: str) -> ClickRegion:
    """字符串 → ClickRegion，大小写/连字符/空格不敏感（统一归一化为下划线小写）。

    例："TopRight" / "top-right" / "top right" / "top_right" 均解析为
    ClickRegion.TOP_RIGHT。

    Raises:
        ValueError: value 不是合法的区域取值。调用方（action 层）应捕获后
                    转为 ActionResult.fail，不静默忽略。
    """
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    try:
        return ClickRegion(normalized)
    except ValueError:
        valid = ", ".join(r.value for r in ClickRegion)
        raise ValueError(
            f"非法的 region 取值: {value!r}，合法取值为: {valid}"
        ) from None


def point_for_region(bbox: tuple[float, float, float, float], region: ClickRegion) -> tuple[float, float]:
    """bbox=(x, y, w, h) → 该区域的点击点坐标 (px, py)。

    Args:
        bbox: (x, y, w, h)，x/y 为左上角坐标，w/h 为宽高，坐标系与传入方一致
              （L3.8 场景下是"截图局部像素坐标"，见 l3_8_visual_fingerprint.py）。
        region: 目标区域。

    Returns:
        (px, py)：区域对应的四分点坐标，与 bbox 同一坐标系。
    """
    x, y, w, h = bbox
    rx, ry = _REGION_RATIOS[region]
    return (x + w * rx, y + h * ry)


def resolve_click_point(
    coords: tuple[float, float],
    bbox: Optional[tuple[float, float, float, float]],
    region_str: Optional[str],
) -> tuple[float, float]:
    """click/hover/type action 统一调用的坐标解析入口。

    Args:
        coords: 命中结果的默认中心点坐标（element.coords）。
        bbox:   命中结果的完整 bbox（element.bbox），仅 L3.8 命中时非 None。
        region_str: DSL kwarg 声明的 region 取值（如 "top_right"），可为 None/空串。

    Returns:
        最终使用的 (x, y) 坐标。

    Raises:
        ValueError: region_str 非空但不是合法取值（调用方应捕获后转为
                    ActionResult.fail，不静默忽略）。

    行为规则：
      - 未声明 region_str（None/空串）→ 直接返回 coords，零回归。
      - 声明了 region_str 但 bbox 为 None（如 L4/普通元素坐标句柄）→ 忽略
        region，回退 coords，不报错（由调用方决定是否打 debug 日志）。
      - 声明了 region_str 且 bbox 非 None → 按 region 计算区域坐标。
    """
    if not region_str:
        return coords
    region = parse_region(region_str)  # 非法值抛 ValueError，交给调用方处理
    if bbox is None:
        return coords
    return point_for_region(bbox, region)
