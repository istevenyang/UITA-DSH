"""
ElementMapLoader — loads rosetta_map.json and flattens it to per-platform FlatEntry dicts.

支持两种根级结构，可在同一文件内混用（按根 key 逐个自动识别）：

1. 树形结构（app → page → element → fields）：
{
    "_meta": {"version": "3.0"},
    "sample_web": {
        "首页": {
            "快速检索按钮": {"mac": "AXButton[title=快速检索]", "usage": "..."}
        }
    }
}

2. 平铺结构（element → fields，根 key 直接是元素名）：
{
    "_meta": {"version": "3.0"},
    "对话输入框": {"selector": ".chat-input", "usage": "...", "view": "对话页"}
}

字段规范（每个元素）：
- mac / win / selector / js：语义互斥，严禁混用（详见宪法 R22b）
  - selector：CSS/JS **选择器**（web 定位用），跨平台，优先由 Playwright 引擎操作。
    严禁存储 JS 表达式。
  - mac / win：原生可访问性选择器（AXButton[...] / UITA_...），按 os 展平后用
    native 引擎操作（MacAppKitEngine / WinUIAEngine）。仅当 selector 缺失时使用
  - js：**JS 表达式**（IIFE / document.* / 箭头函数），供
    `assert[<element_name>]`/`eval_js[<element_name>]` 执行判定/取值用。严禁存储
    selector。与 selector/mac/win 语义互斥（同时声明视为配置错误，运行时优先取
    js，见 `_register()::_warn_mixed_declaration()`）。不参与 `_resolve_selector()`
    选择器解析（2026-07-28 起移出优先级链，见 R22b）。
- usage：必填（用途说明）
- id：元素唯一标识，缺失时读取路径容错为空串（写入路径由 rosetta_writer.generate_id() 补全）
- views：元素所属页面列表，兼容旧版单值 view 字段

FlatEntry: {
    "element_name": str,
    "page": str,
    "selector": str,   # 解析优先级：platform_key（mac/win）→ selector（CSS）
    "js_expr": str,    # 原始 "js" 字段内容（JS 表达式，与 selector 互斥）
    "usage": str,
    "id": str,
    "views": list[str],
}
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from uita.core.platform.window_hint import WindowHint, parse_window_hint

if TYPE_CHECKING:
    from uita.core.rosetta.base import RosettaStoreBase

logger = logging.getLogger(__name__)

# 已知字段名：用于判断一个 dict 是"元素"（含这些 key）还是"页面/app 容器"（不含）
_FIELD_KEYS = {
    "mac", "win", "linux", "js", "selector", "usage", "id",
    "view", "views", "desc", "text", "role", "role_name", "label",
    "pattern", "pattern_mac", "pattern_win", "pattern_linux",
    "post_mac", "post_win", "post_selector", "click_rect",
    # qt：QtProbe Qt 探针 XPath（进程内对象树定位，可选字段）。与 win（UIA
    # QPath）并存、互不覆盖，供 L5 QT_PROBE 策略消费，见
    # docs/plans/20261116203324-win-qt-probe-channel-plan.md。
    "qt",
    # window_hint：元素所属窗口（Windows 多窗口应用），规范化见
    # core/platform/window_hint.py，仅 WinUIA 引擎消费。
    "window_hint",
    # strategy_chain：元素自定义降级链（如 "MAP_EXACT,VISUAL_FP" 或 ["L1", "L7"]）
    "strategy_chain",
}

# platform_key（已展平的 "mac"/"win"/"linux"）→ 对应的平台专属 pattern 字段名
_PATTERN_FIELD_MAP = {
    "mac": "pattern_mac",
    "win": "pattern_win",
    "linux": "pattern_linux",
}

# Platform key aliases
_PLATFORM_KEY_MAP = {
    "macos": "mac",
    "mac": "mac",
    "windows": "win",
    "win": "win",
    "linux": "linux",
}


@dataclass
class FlatEntry:
    element_name: str
    page: str
    selector: str
    # js_expr：原始 "js" 字段内容（JS 表达式，与 selector 语义互斥，见模块顶部
    # docstring 与宪法 R22b）。供 assert[<element_name>]/eval_js[<element_name>]
    # 查表使用（core/resolver/resolver.py::get_js_expr()/has_locator_entry()）。
    js_expr: str = ""
    usage: str = ""
    id: str = ""
    views: list = field(default_factory=list)
    aliases: list = field(default_factory=list)  # 同义词列表，用于 L2 语义匹配
    role: str = ""       # ARIA/AX role（如 "button"），供 L5 ROLE_MATCH 精确匹配
    role_name: str = ""  # role 对应的可访问名称（如 "确认"）
    # label：关联 <label> 文本（如 "邮箱地址"），供 L4 LABEL_MATCH 精确匹配。
    # 缺失时该策略走"用元素名兜底尝试"分支，存量 rosetta_map 零改动、零回归。
    label: str = ""
    # 视觉指纹（z85 压缩 base64，见 core/rosetta/pattern_codec.py）。
    # 支持平台专属字段（pattern_mac/pattern_win/pattern_linux）优先，
    # 缺失时回退通用 pattern；且单个字段可为多张指纹数组。
    # patterns：解析后最终生效的指纹列表（已做平台优先+回退），供 L7 多模板匹配。
    patterns: list = field(default_factory=list)
    # pattern：向后兼容旧调用点的单值视图，取 patterns[0]（无则空串）。
    pattern: str = ""
    # click_rect：模板指纹图内的框选点击子区域 (x, y, w, h)，局部像素坐标（相对模板图
    # 自身左上角），与 pattern 同级的静态声明字段。默认 None。
    click_rect: Optional[tuple] = None
    # qt_path：rosetta_map 可选 `qt` 字段（QtProbe Qt 探针 XPath，如
    # `MainWindowBg//ToolTipButton[@btnKey='themeSwitch']`）。空串表示该元素
    # 未声明 qt 通道，L9 QT_PROBE 策略对该元素自动跳过（能力裁剪）。
    qt_path: str = ""
    # window_hint：元素所属窗口声明（已规范化的 WindowHint，未声明/非法为 None）。
    # 仅 Windows 多窗口应用的 WinUIA 引擎消费；其余引擎与未声明元素行为不变。
    window_hint: Optional["WindowHint"] = None
    # strategy_chain：元素级自定义降级链 token 列表（如 ["MAP_EXACT", "VISUAL_FP"] 或 ["L1", "L7"]）
    strategy_chain: list[str] = field(default_factory=list)


def _looks_like_element(value: dict) -> bool:
    """value 自身的 key 命中已知字段名 → 判定为"元素"而非"页面/app"容器。"""
    return bool(_FIELD_KEYS & value.keys())


def _resolve_selector(elem_fields: dict, platform_key: str) -> str:
    """选择器解析优先级：platform_key（win/mac）→ selector（CSS）。

    2026-07-13 调整：platform_key 优先级提到 selector 之前，确保原生引擎（WinUIA /
    MacAppKit）优先收到各自平台的选择器而非 CSS。selector 仅在 platform_key 缺失时
    作为 web 引擎兜底（元素如仅有 CSS selector 而无 win/mac 字段时仍可被 Playwright
    使用；原生引擎收到 CSS 会 L1 失败后降级到 L2/L3/L4）。

    2026-07-28 起 `js` 字段彻底移出该优先级链（见宪法 R22b）：`js` 字段语义收敛为
    纯 JS 表达式（供 assert[]/eval_js[] 执行），不再参与 selector 解析——原始内容
    改由 `_register()` 单独读取存入 `FlatEntry.js_expr`。

    Args:
        elem_fields: rosetta_map 中该元素的扁平 dict
        platform_key: "mac" / "win"（已展平）

    Returns:
        选择器字符串（空串表示该元素在该平台无可用选择器，调用方应跳过）
    """
    return (
        elem_fields.get(platform_key)
        or elem_fields.get("selector")
        or ""
    )


def _resolve_views(elem_fields: dict) -> list:
    """views 优先取新版列表字段，否则回退旧版单值 view。"""
    views = elem_fields.get("views")
    if isinstance(views, list) and views:
        return list(views)
    view = elem_fields.get("view")
    if view:
        return [view]
    return []


def _parse_click_rect(raw_value) -> Optional[tuple]:
    """解析 rosetta_map 中的 click_rect 字段（[x, y, w, h] → tuple）。

    非法值（非 4 元素 / 非数字 / 宽高 <= 0）静默忽略返回 None，不阻断整个
    文件加载——只有这一条元素的框选点击能力失效，等价于未声明该字段。
    """
    if not raw_value:
        return None
    try:
        if len(raw_value) != 4:
            return None
        x, y, w, h = (float(v) for v in raw_value)
        if w <= 0 or h <= 0:
            return None
        return (x, y, w, h)
    except (TypeError, ValueError):
        return None


def _normalize_patterns(raw) -> list:
    """将 pattern 字段原始值规范化为字符串列表。

    - 单个非空字符串 → 单元素列表（现状 100% 场景，零回归）
    - 字符串数组 → 过滤空字符串后的列表（新增：多张局部指纹）
    - 其余（None/空串/非法类型）→ 空列表
    """
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, list):
        return [str(p) for p in raw if p]
    return []


def _resolve_patterns(elem_fields: dict, platform_key: str) -> list:
    """视觉指纹解析优先级：平台专属字段（pattern_mac/pattern_win/pattern_linux）
    命中优先，未命中（字段缺失或为空）则回退通用 pattern（类似 universal 兜底）。

    Args:
        elem_fields: rosetta_map 中该元素的扁平 dict
        platform_key: "mac" / "win" / "linux"（已展平）

    Returns:
        规范化后的指纹字符串列表，可能为空列表（表示该元素在该平台无视觉指纹）
    """
    platform_field = _PATTERN_FIELD_MAP.get(platform_key)
    if platform_field:
        platform_patterns = _normalize_patterns(elem_fields.get(platform_field))
        if platform_patterns:
            return platform_patterns
    return _normalize_patterns(elem_fields.get("pattern"))


def _normalize_strategy_tokens(raw) -> list[str]:
    """将 strategy_chain / default_strategy_chain 规范化为大写 token 列表。"""
    if isinstance(raw, str):
        return [tok.strip().upper() for tok in raw.split(",") if tok.strip()]
    if isinstance(raw, (list, tuple)):
        out: list[str] = []
        for item in raw:
            if isinstance(item, str):
                out.extend(tok.strip().upper() for tok in item.split(",") if tok.strip())
        return out
    return []


def _resolve_page(elem_fields: dict, fallback_page: str) -> str:
    """平铺结构下的 page：优先 views[0] / view，否则用 fallback（树形下的页面名）。"""
    views = _resolve_views(elem_fields)
    if views:
        return views[0]
    return fallback_page


class ElementMapLoader:
    def __init__(self, map_path: str, platform: str, store: Optional["RosettaStoreBase"] = None):
        self._map_path = map_path
        self._platform_key = _PLATFORM_KEY_MAP.get(platform.lower(), platform.lower())
        self._entries: dict[str, FlatEntry] = {}  # element_name → FlatEntry
        self._default_strategy_chain: list[str] = []
        self._loaded = False
        # 读取来源：DB 优先（DbFallbackRosettaStore），DB 不可用时自动回退本地 JSON。
        # 外部可传入自定义 store（如测试时传入 LocalJsonRosettaStore）。
        if store is None:
            try:
                from uita.core.rosetta.base import DbFallbackRosettaStore
                store = DbFallbackRosettaStore()
            except Exception:
                from uita.core.rosetta.base import LocalJsonRosettaStore
                store = LocalJsonRosettaStore()
        self._store = store

    def load(self) -> None:
        if self._loaded:
            return
        if not os.path.exists(self._map_path):
            raise FileNotFoundError(f"rosetta_map not found: {self._map_path}")
        raw: dict = self._store.load_raw(self._map_path)
        meta = raw.get("_meta") if isinstance(raw, dict) else None
        if isinstance(meta, dict):
            self._default_strategy_chain = _normalize_strategy_tokens(
                meta.get("default_strategy_chain")
            )
        self._entries = self._flatten(raw, self._platform_key)
        # 混合结构 warning（2026-07-06）：rosetta_map 同时出现"仅有 selector/js
        # 的元素"和"仅有 mac/win 的元素"时输出软提示，不阻断加载。该结构是反模式：
        # 单引擎（Playwright 或 MacAppKit）无法同时覆盖两种元素，应补全每元素
        # 的字段（同时有 selector + mac/win）或拆分成两个 app。
        self._warn_mixed_structure(raw)
        self._loaded = True

    def _warn_mixed_structure(self, raw: dict) -> None:
        """检测 rosetta_map 中混合结构（仅 selector/js vs 仅 mac/win）。"""
        has_web_only = False   # 至少一个元素仅有 selector/js（无 mac/win）
        has_native_only = False  # 至少一个元素仅有 mac/win（无 selector/js）

        def _check_elem(elem_fields: dict) -> None:
            nonlocal has_web_only, has_native_only
            if not isinstance(elem_fields, dict):
                return
            has_web = bool(elem_fields.get("selector"))
            has_native = bool(elem_fields.get("mac") or elem_fields.get("win"))
            if has_web and not has_native:
                has_web_only = True
            elif has_native and not has_web:
                has_native_only = True

        for root_key, root_value in raw.items():
            if root_key.startswith("_") or not isinstance(root_value, dict):
                continue
            if _looks_like_element(root_value):
                _check_elem(root_value)
                continue
            for page_data in root_value.values():
                if not isinstance(page_data, dict):
                    continue
                for elem_fields in page_data.values():
                    _check_elem(elem_fields if isinstance(elem_fields, dict) else {})

        if has_web_only and has_native_only:
            logger.warning(
                "[rosetta_map] %s 存在混合结构：部分元素仅有 selector（适合 "
                "Playwright），部分仅有 mac/win（适合原生引擎）。单引擎无法同时"
                "覆盖两种元素——建议补全每元素的字段（同时有 selector + mac/win）"
                "或拆分成两个 app。详见 docs/plans/archive/03-resolver-rosetta-实施纪要.md §四",
                self._map_path,
            )

    @staticmethod
    def _flatten(raw: dict, platform_key: str) -> dict[str, FlatEntry]:
        entries: dict[str, FlatEntry] = {}

        def _register(elem_name: str, fallback_page: str, elem_fields: dict) -> None:
            selector = _resolve_selector(elem_fields, platform_key)
            js_expr = elem_fields.get("js", "") or ""
            resolved_patterns = _resolve_patterns(elem_fields, platform_key)
            # qt：Qt 探针通道的独立定位字段（与 selector/mac/win 并存，不参与
            # `_resolve_selector` 优先级链，仅供 L5 QT_PROBE 消费）。
            qt_path = elem_fields.get("qt", "") or ""
            if not isinstance(qt_path, str):
                logger.warning(
                    "[rosetta_map] 元素 %r 的 qt 字段不是字符串（%r），已忽略",
                    elem_name, type(qt_path),
                )
                qt_path = ""
            if not selector and not js_expr and not resolved_patterns and not qt_path:
                return  # 四者都没有才跳过（占位/无效元素，零回归）
            if js_expr and selector:
                logger.warning(
                    "[rosetta_map] 元素 %r 同时声明了 js 表达式字段与定位字段"
                    "（selector/mac/win），违反字段互斥约束（详见宪法 R22b）。运行时将"
                    "优先使用 js 表达式，定位字段被忽略——请修正 rosetta_map，仅保留一种。",
                    elem_name,
                )
            raw_aliases = elem_fields.get("aliases", [])
            aliases = raw_aliases if isinstance(raw_aliases, list) else []
            entries[elem_name] = FlatEntry(
                element_name=elem_name,
                page=_resolve_page(elem_fields, fallback_page),
                selector=selector,
                js_expr=js_expr,
                usage=elem_fields.get("usage", ""),
                id=elem_fields.get("id", ""),
                views=_resolve_views(elem_fields),
                aliases=aliases,
                role=elem_fields.get("role", ""),
                role_name=elem_fields.get("role_name", ""),
                label=elem_fields.get("label", ""),
                patterns=resolved_patterns,
                pattern=resolved_patterns[0] if resolved_patterns else "",
                click_rect=_parse_click_rect(elem_fields.get("click_rect")),
                qt_path=qt_path,
                window_hint=parse_window_hint(elem_fields.get("window_hint")),
                strategy_chain=_normalize_strategy_tokens(elem_fields.get("strategy_chain")),
            )

        for root_key, root_value in raw.items():
            if root_key.startswith("_"):
                continue
            if not isinstance(root_value, dict):
                continue

            if _looks_like_element(root_value):
                # 平铺结构：root_key 即为元素名
                _register(root_key, fallback_page="", elem_fields=root_value)
                continue

            # 树形结构：root_key = app 名，root_value = {页面: {元素: 字段}}
            for page_name, page_data in root_value.items():
                if not isinstance(page_data, dict):
                    continue
                for elem_name, elem_fields in page_data.items():
                    if isinstance(elem_fields, dict):
                        _register(elem_name, fallback_page=page_name, elem_fields=elem_fields)

        return entries

    def find(self, element_name: str) -> Optional[FlatEntry]:
        self.load()
        return self._entries.get(element_name)

    def all_entries(self) -> dict[str, FlatEntry]:
        self.load()
        return dict(self._entries)

    def default_strategy_chain(self) -> list[str]:
        """返回 _meta.default_strategy_chain 声明的 App 级默认降级链 token 列表。"""
        self.load()
        return list(self._default_strategy_chain)
