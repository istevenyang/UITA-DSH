"""
AXPath 解析器 — 将 rosetta_map 中的 AXPath 字符串转换为 atomacos 查询。

AXPath 格式（UITA 自定义，非 XPath）：
  AXButton[title=快速检索]           # 按 AXTitle 匹配
  AXStaticText[value=搜索结果]     # 按 AXValue 匹配
  AXButton[description=最小化按钮]   # 按 AXDescription 匹配
  AXButton[index=4]                  # 按同类型第 N 个（1-based）
  AXButton                           # 同类型第 1 个（无属性过滤）
  AX_OFFSET[anchor=AXStaticText[value=摄像头保护],dx=100,dy=-50]
                                     # 锚点偏移定位（AX不可见元素）

对应 atomacos 查询：
  app.windows()[0].findFirstR(AXRole="AXButton", AXTitle="快速检索")
  app.windows()[0].findFirstR(AXRole="AXStaticText", AXValue="搜索结果")
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class AXPathSpec:
    role: str               # e.g. "AXButton"
    attr: Optional[str]     # e.g. "title" / "value" / "description" / "index" / None
    value: Optional[str]    # e.g. "快速检索" / "4"
    contains: bool = False  # True 时做前缀/包含匹配（attr 值以 * 结尾）
    index: Optional[int] = None  # 双属性时的序号（1-based），先按 attr/value 过滤再取第 N 个


# 支持三种格式：
#   AXButton[title=快速检索]                  单属性匹配
#   AXStaticText[value=已发现可清理文件]       单属性匹配（value 走前缀匹配）
#   AXStaticText[value=更多, index=1]          双属性匹配（先按 value 过滤，再取第 N 个）
#   AXImage[description=arrow, index=2]        双属性匹配（先按 description 过滤，再取第 N 个）
_RE_AXPATH = re.compile(
    r"^(AX\w+)(?:\[(\w+)=(.+?)(?:,\s*index=(\d+))?\])?$"
)

# 属性名 → atomacos AX 属性名映射
_ATTR_MAP = {
    "title":       "AXTitle",
    "value":       "AXValue",
    "description": "AXDescription",
    "help":        "AXHelp",
    "identifier":  "AXIdentifier",
    "label":       "AXLabel",
    "subrole":     "AXSubrole",
}


class AXPathError(ValueError):
    pass


def _split_candidates(value: str) -> list[str]:
    """按 `|` 拆分多语言候选值，去除两端空白，过滤空字符串。

    单一候选（无 `|`）时返回单元素列表，行为与改动前完全一致。
    用于应对同一元素在不同系统语言态下 AX 属性值不同的场景（如
    `AXButton[title=开始扫描|StartScan]`），见宪法 R22c /
    docs/plans/20260731120000-axpath-multilang-candidate-values-and-
    constitution-update.md。
    """
    if "|" not in value:
        return [value]
    parts = [p.strip() for p in value.split("|")]
    return [p for p in parts if p]


def parse_axpath(axpath: str) -> AXPathSpec:
    """解析 AXPath 字符串为 AXPathSpec。"""
    m = _RE_AXPATH.match(axpath.strip())
    if not m:
        raise AXPathError(f"无效的 AXPath 格式: {axpath!r}")
    role = m.group(1)
    attr = m.group(2)
    value = m.group(3)
    index_str = m.group(4)
    index = int(index_str) if index_str else None
    return AXPathSpec(role=role, attr=attr, value=value, index=index)


def query_element(window, spec: AXPathSpec):
    """
    在 window 中按 AXPathSpec 查找元素。
    返回 atomacos element 或 None。

    匹配策略：
      - index  → 按同类型第 N 个（1-based）
      - title / description / help → findAllR 精确匹配（atomacos 可靠）
      - value  → 遍历所有同 role 元素，用 startswith 前缀匹配
                 （因为 AXValue 常含动态数字，精确匹配不可靠）
      - None   → 取第 1 个
    """
    try:
        if spec.attr is None:
            results = window.findAllR(AXRole=spec.role)
            return results[0] if results else None

        if spec.attr == "index":
            idx = int(spec.value) - 1
            results = window.findAllR(AXRole=spec.role)
            return results[idx] if idx < len(results) else None

        # value 属性：遍历 + 匹配（精确优先，前缀兜底，应对动态数字后缀）
        # 注意：必须精确匹配优先，否则"摄像头保护"会前缀匹配到
        # "摄像头保护：软件调用摄像头时进行提示"等左侧描述文字，导致锚点偏移
        # 定位到错误坐标（真机事故：privacy_onekey_toggle 用例四开关全点空）。
        # 末尾带 * 时走 contains（parser 已处理 spec.contains=True）。
        # 支持 `|` 分隔多语言候选值（R22c）：所有候选先各跑一轮精确，全部候选
        # 精确都未命中才对每个候选各跑一轮前缀，不按候选顺序交叉判定精确/前缀，
        # 避免优先级打架。
        if spec.attr == "value":
            candidates = _split_candidates(spec.value)
            matches = []
            for elem in window.findAllR(AXRole=spec.role):
                try:
                    v = str(getattr(elem, "AXValue", "") or "")
                    if spec.contains:
                        if any(c in v for c in candidates):
                            matches.append(elem)
                    elif v in candidates:
                        matches.append(elem)
                except Exception:
                    pass
            # 精确无果时前缀兜底（应对"发现 929.00 GB"等动态数字后缀）
            if not matches and not spec.contains:
                for elem in window.findAllR(AXRole=spec.role):
                    try:
                        v = str(getattr(elem, "AXValue", "") or "")
                        if any(v.startswith(c) for c in candidates):
                            matches.append(elem)
                    except Exception:
                        pass
            if spec.index is not None:
                idx = spec.index - 1
                return matches[idx] if idx < len(matches) else None
            return matches[0] if matches else None

        # title/description/help/identifier/label/subrole 等通用属性：支持
        # `|` 分隔多语言候选值（R22c），按声明顺序依次尝试，首个命中即用。
        ax_attr = _ATTR_MAP.get(spec.attr, f"AX{spec.attr.capitalize()}")
        candidates = _split_candidates(spec.value)
        for candidate in candidates:
            results = window.findAllR(AXRole=spec.role, **{ax_attr: candidate})
            if results:
                if spec.index is not None:
                    idx = spec.index - 1
                    return results[idx] if idx < len(results) else None
                return results[0]
        return None

    except Exception:
        return None


def is_axpath(selector: str) -> bool:
    """判断选择器是否为 AXPath 格式（AX 开头）。"""
    return bool(selector.strip().startswith("AX"))


# ── scoped AXPath（窗口作用域，2026-07-29 新增）─────────────────────
#
# 格式: AXWindow[subrole=AXUnknown]/AXButton[title=快速检索]
#   scope 段定位候选窗口（主窗口 + 卫星进程窗口中匹配者），
#   target 段在每个 scope 窗口内查询，首个命中返回。
# 用途：卫星进程弹窗（如 SampleApp 轻页面 popover 属 AEMonitor 进程）与主窗口
#   存在同名元素时消歧（popover"快速检索" vs 主窗口"快速检索"）。


def is_scoped_axpath(selector: str) -> bool:
    """判断是否为 scoped AXPath（含顶层 '/' 分段符）。"""
    return split_scoped_axpath(selector) is not None


def split_scoped_axpath(selector: str) -> Optional[tuple[str, str]]:
    """按顶层 '/' 拆分 scope/target 两段；括号内的 '/' 不算。非 scoped 返回 None。"""
    depth = 0
    for i, ch in enumerate(selector):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        elif ch == "/" and depth == 0:
            scope, target = selector[:i], selector[i + 1:]
            if scope.strip() and target.strip():
                return scope.strip(), target.strip()
            return None
    return None


def window_matches(win, spec: AXPathSpec) -> bool:
    """判断窗口是否匹配 scope spec（role + 单属性精确匹配，异常视为不匹配）。

    支持 `|` 分隔多语言候选值（R22c），如 scope 段
    `AXWindow[title=偏好设置|Preferences]`。
    """
    try:
        if getattr(win, "AXRole", None) != spec.role:
            return False
        if spec.attr is None or spec.attr == "index":
            return True
        ax_attr = _ATTR_MAP.get(spec.attr, f"AX{spec.attr.capitalize()}")
        actual = getattr(win, ax_attr, None)
        candidates = _split_candidates(spec.value)
        return any(str(actual or "") == c for c in candidates)
    except Exception:
        return False


def query_element_in_windows(windows, selector: str):
    """在窗口序列中按（可能 scoped 的）AXPath 查询元素。

    - scoped：先按 scope 段过滤窗口，再在匹配窗口内逐个查 target 段；
      scope 带 index 时取第 N 个匹配窗口（1-based）。
    - 非 scoped：逐窗口 query_element，首个命中返回。
    全部未命中返回 None。
    """
    pair = split_scoped_axpath(selector)
    if pair is None:
        spec = parse_axpath(selector)
        for win in windows:
            result = query_element(win, spec)
            if result is not None:
                return result
        return None

    scope_str, target_str = pair
    scope_spec = parse_axpath(scope_str)
    target_spec = parse_axpath(target_str)
    matched = [w for w in windows if window_matches(w, scope_spec)]
    if scope_spec.attr == "index":
        idx = int(scope_spec.value) - 1
        matched = matched[idx:idx + 1]
    elif scope_spec.index is not None:
        idx = scope_spec.index - 1
        matched = matched[idx:idx + 1]
    for win in matched:
        result = query_element(win, target_spec)
        if result is not None:
            return result
    return None


# ── AX_OFFSET 偏移定位 ─────────────────────────────────────────────

# 格式: AX_OFFSET[anchor=AXStaticText[value=摄像头保护],dx=100,dy=-50]
_RE_OFFSET = re.compile(
    r"^AX_OFFSET\[anchor=(AX\w+(?:\[\w+=.+?\])?),dx=(-?\d+),dy=(-?\d+)\]$"
)


@dataclass
class VirtualElement:
    """虚拟元素：仅有坐标信息，用于 AX 不可见元素的偏移定位。"""
    AXPosition: object  # 需要有 .x .y 属性
    AXSize: object      # 需要有 .width .height 属性
    AXRole: str = "AXVirtual"
    AXTitle: str = ""
    AXValue: str = ""
    AXActionNames: list = None

    def __post_init__(self):
        if self.AXActionNames is None:
            self.AXActionNames = []

    def Press(self):
        raise RuntimeError("VirtualElement 不支持 Press，需走 CGEvent 坐标点击")


class _Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _Size:
    def __init__(self, w, h):
        self.width = w
        self.height = h


def is_ax_offset(selector: str) -> bool:
    """判断选择器是否为 AX_OFFSET 偏移格式。"""
    return selector.strip().startswith("AX_OFFSET[")


def parse_ax_offset(selector: str) -> tuple:
    """解析 AX_OFFSET 字符串，返回 (anchor_axpath, dx, dy)。"""
    m = _RE_OFFSET.match(selector.strip())
    if not m:
        raise AXPathError(f"无效的 AX_OFFSET 格式: {selector!r}")
    return m.group(1), int(m.group(2)), int(m.group(3))


def query_element_with_offset(window, selector: str) -> Optional[VirtualElement]:
    """
    AX_OFFSET 偏移定位：
    1. 解析锚点 AXPath + 偏移量
    2. 查找锚点元素获取坐标
    3. 计算偏移后的虚拟元素（中心点坐标）
    """
    anchor_path, dx, dy = parse_ax_offset(selector)
    spec = parse_axpath(anchor_path)
    anchor = query_element(window, spec)
    if anchor is None:
        return None
    pos = getattr(anchor, "AXPosition", None)
    if pos is None:
        return None
    # 偏移后的中心点坐标，虚拟元素尺寸设为 44x22（标准开关大小）
    target_x = pos.x + dx
    target_y = pos.y + dy
    virtual_w, virtual_h = 44, 22
    # AXPosition 是左上角坐标，我们需要让 center = (target_x, target_y)
    vpos = _Point(target_x - virtual_w / 2, target_y - virtual_h / 2)
    vsz = _Size(virtual_w, virtual_h)
    return VirtualElement(AXPosition=vpos, AXSize=vsz)
