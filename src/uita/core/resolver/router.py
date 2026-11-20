"""
ActionRouter — 策略路由器（支持 L1~L9 整数层级与 App 适配级 / 元素自定义级降级链）。

统一整数层级（L1 ~ L9）：
  - L1 MAP_EXACT    : 元素库精确选择器匹配（selector / mac / win）
  - L2 SEMANTIC     : 语义近似匹配（别名/包含/bigram）+ 界面唯一性核对
  - L3 TEXT_MATCH   : 界面可见文本匹配
  - L4 LABEL_MATCH  : 关联 <label> 文本匹配
  - L5 ROLE_MATCH   : ARIA / AX 角色与名称匹配
  - L6 LLM_SELECTOR : 基于可访问性摘要的 LLM 选择器推断
  - L7 VISUAL_FP    : OpenCV 局部特征 / 模板视觉指纹匹配
  - L8 LLM_VISUAL   : 多模态视觉大模型坐标定位（别名 VISUAL）
  - L9 QT_PROBE     : Qt 进程内对象树探针通道（按需由自定义降级链开启）

自定义降级链三级优先级（`route_v2`）：
  1. **元素自定义级（Element-level）**：
     - `apps/<app>/router_proxy.json` 中对应元素 `id` 的 `runtime_define`（最高）或 `default`；
     - 或 `apps/<app>/rosetta_map.json` 中该元素条目的 `"strategy_chain"` 字段；
     - 命中时以自定义策略序列打头，标准全链去重补齐剩余步骤，`skip_self_heal=True`。
  2. **App 适配级（App-level）**：
     - `apps/<app>/router_proxy.json` 中的 `"__default__": {"default": "..."}`；
     - 或 `apps/<app>/configs.py` 中 `AppPlatformConfig.extra["default_strategy_chain"]`；
     - 或 `apps/<app>/rosetta_map.json` 中 `"_meta": {"default_strategy_chain": "..."}`；
     - 命中时以应用级策略序列打头，标准全链去重补齐剩余步骤，`skip_self_heal=False`（允许元素自愈学习）。
  3. **框架标准全链（Standard Full Chain）**：
     - `L1(MAP_EXACT) → L2(SEMANTIC) → L3(TEXT_MATCH) → L4(LABEL_MATCH) → L5(ROLE_MATCH) → L6(LLM_SELECTOR) → L7(VISUAL_FP) → L8(LLM_VISUAL)`，并按 `has_selector` / `is_native` / `has_pattern` 自动裁剪不适用步骤。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:
    from uita.core.platform.element_map_loader import FlatEntry
    from uita.core.platform.router_proxy_loader import RouterProxyEntry

logger = logging.getLogger(__name__)


class ResolveStrategy(Enum):
    MAP_EXACT = "map_exact"          # L1（精确选择器匹配）
    SEMANTIC = "semantic"            # L2（语义近似匹配）
    TEXT_MATCH = "text_match"        # L3（可见文本匹配）
    LABEL_MATCH = "label_match"      # L4（关联 <label> 文本匹配）
    ROLE_MATCH = "role_match"        # L5（ARIA/AX 角色匹配）
    LLM_SELECTOR = "llm_selector"    # L6（LLM 选择器推断）
    VISUAL_FP = "visual_fp"          # L7（OpenCV 视觉指纹匹配）
    LLM_VISUAL = "llm_visual"        # L8（LLM 视觉坐标定位）
    VISUAL = "llm_visual"            # L8 别名（ResolveStrategy.VISUAL is ResolveStrategy.LLM_VISUAL）
    QT_PROBE = "qt_probe"            # L9（Qt 探针通道，由自定义降级链显式启用）


#: 支持在 router_proxy.json / strategy_chain 中使用 L1~L9 整数层级简写或完整枚举名
_LEVEL_TOKEN_ALIASES: dict[str, ResolveStrategy] = {
    "L1": ResolveStrategy.MAP_EXACT,
    "L1_MAP_EXACT": ResolveStrategy.MAP_EXACT,
    "L2": ResolveStrategy.SEMANTIC,
    "L2_SEMANTIC": ResolveStrategy.SEMANTIC,
    "L3": ResolveStrategy.TEXT_MATCH,
    "L3_TEXT_MATCH": ResolveStrategy.TEXT_MATCH,
    "L4": ResolveStrategy.LABEL_MATCH,
    "L4_LABEL_MATCH": ResolveStrategy.LABEL_MATCH,
    "L5": ResolveStrategy.ROLE_MATCH,
    "L5_ROLE_MATCH": ResolveStrategy.ROLE_MATCH,
    "L6": ResolveStrategy.LLM_SELECTOR,
    "L6_LLM_SELECTOR": ResolveStrategy.LLM_SELECTOR,
    "L7": ResolveStrategy.VISUAL_FP,
    "L7_VISUAL_FP": ResolveStrategy.VISUAL_FP,
    "L8": ResolveStrategy.LLM_VISUAL,
    "L8_LLM_VISUAL": ResolveStrategy.LLM_VISUAL,
    "L9": ResolveStrategy.QT_PROBE,
    "L9_QT_PROBE": ResolveStrategy.QT_PROBE,
}

# 标准全链顺序（L1 → L8；不含 L9 QT_PROBE，QT_PROBE 由自定义降级链按需开启）
FULL_STRATEGY_ORDER: tuple[ResolveStrategy, ...] = (
    ResolveStrategy.MAP_EXACT,
    ResolveStrategy.SEMANTIC,
    ResolveStrategy.TEXT_MATCH,
    ResolveStrategy.LABEL_MATCH,
    ResolveStrategy.ROLE_MATCH,
    ResolveStrategy.LLM_SELECTOR,
    ResolveStrategy.VISUAL_FP,
    ResolveStrategy.LLM_VISUAL,
)


@dataclass
class RouteDecision:
    strategies: list[ResolveStrategy]
    reason: str = ""
    pinned: bool = False
    skip_self_heal: bool = False


_ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "button": ("按钮", "button", "btn", "点击", "确认", "取消", "关闭", "发送"),
    "textbox": ("输入框", "input", "搜索框", "文本框", "textbox"),
    "checkbox": ("复选框", "勾选", "checkbox"),
    "link": ("链接", "link"),
    "tab": ("标签页", "tab", "选项卡"),
    "progressbar": ("进度条", "progress", "加载中"),
}


def infer_role_from_description(description: str) -> Optional[str]:
    """按关键词推断 ARIA/AX role，找不到返回 None（供 L5 ROLE_MATCH 策略兜底使用）。"""
    if not description:
        return None
    desc_lower = description.lower()
    for role, keywords in _ROLE_KEYWORDS.items():
        if any(kw in desc_lower for kw in keywords):
            return role
    return None


_DECLARED_KWARG_PRIORITY = ("selector", "role", "label", "text")


def extract_declared_strategy(
    kwargs: dict, allowed: tuple[str, ...] = _DECLARED_KWARG_PRIORITY,
) -> Optional[dict]:
    """从 action kwargs 中提取声明式首策略（click[selector=]/[role=]/[label=]/[text=]）。"""
    present = [k for k in _DECLARED_KWARG_PRIORITY if k in kwargs and k in allowed]
    if not present:
        return None
    if len(present) > 1:
        logger.warning(
            "[declared_strategy] 同时声明了多个互斥 kwarg %s，按优先级取 '%s'，忽略其余",
            present, present[0],
        )
    key = present[0]
    if key == "role":
        return {"role": kwargs["role"], "name": kwargs.get("name", "")}
    return {key: kwargs[key]}


def declared_strategy_label(declared: dict) -> str:
    """把 declared dict 转成人类可读的日志/错误信息标签。"""
    if "selector" in declared:
        return f"selector={declared['selector']}"
    if "role" in declared:
        name = declared.get("name", "")
        return f"role={declared['role']}" + (f",name={name}" if name else "")
    if "label" in declared:
        return f"label={declared['label']}"
    if "text" in declared:
        return f"text={declared['text']}"
    return "declared"


class ActionRouter:
    """策略路由器。"""

    def __init__(self, strategy_store: Optional[object] = None):
        self._strategy_store = strategy_store

    def route(
        self,
        description: str,
        flat_entry: Optional["FlatEntry"],
        is_native: bool = False,
        has_pattern: bool = False,
        has_local_fingerprint: bool = False,
        fingerprint_age_days: float = 999.0,
        llm_available: bool = False,
    ) -> RouteDecision:
        """推导基础策略序列（兼容旧调用路径）。"""
        if self._strategy_store is not None:
            pinned = getattr(self._strategy_store, "get_pinned", lambda _: None)(description)
            if pinned is not None:
                return RouteDecision(
                    [pinned], reason="策略记录命中", pinned=True, skip_self_heal=True,
                )

        has_selector = bool(flat_entry and flat_entry.selector)

        strategies: list[ResolveStrategy] = [ResolveStrategy.MAP_EXACT]
        reasons: list[str] = [
            "元素库 selector 命中" if has_selector else "无 selector，语义匹配兜底"
        ]

        if is_native:
            reasons.append("原生引擎跳过文字匹配（AX/UIA 树遍历成本高）")
        else:
            strategies.append(ResolveStrategy.TEXT_MATCH)
            reasons.append("Web 引擎追加文字匹配降级")

        strategies.append(ResolveStrategy.ROLE_MATCH)
        reasons.append("角色匹配兜底")

        has_fresh_fp = has_local_fingerprint or (has_pattern and fingerprint_age_days < 30)
        if llm_available:
            strategies.append(ResolveStrategy.LLM_SELECTOR)
            reasons.append("LLM 推断 selector 兜底")
        if has_fresh_fp:
            strategies.append(ResolveStrategy.VISUAL_FP)
            reasons.append("视觉指纹兜底")
        if llm_available:
            strategies.append(ResolveStrategy.LLM_VISUAL)
            reasons.append("LLM 视觉定位兜底")

        return RouteDecision(strategies, reason=" | ".join(reasons))

    def route_v2(
        self,
        description: str,
        flat_entry: Optional["FlatEntry"],
        router_proxy_entry: Optional["RouterProxyEntry"] = None,
        default_proxy_entry: Optional["RouterProxyEntry"] = None,
        is_native: bool = False,
        app_default_chain: Optional[Sequence[str]] = None,
    ) -> RouteDecision:
        """融合 rosetta_map 信号 + 元素自定义级 / App 适配级配置的降级链决策。

        决策优先级：
          1. 元素自定义级（`router_proxy_entry.effective` 或 `flat_entry.strategy_chain` 非空）
             → 该配置打头，标准全链去重补齐剩余步骤，`skip_self_heal=True`。
          2. App 适配级（`default_proxy_entry` 或 `app_default_chain` 非空）
             → 该配置打头，标准全链去重补齐剩余步骤，`skip_self_heal=False`。
          3. 均不满足 → 标准全链，`skip_self_heal=False`。
        """
        has_selector = bool(flat_entry and flat_entry.selector)
        has_pattern = bool(flat_entry and flat_entry.patterns)

        proxy_tokens = router_proxy_entry.effective if router_proxy_entry else []
        elem_chain_tokens = list(getattr(flat_entry, "strategy_chain", None) or []) if flat_entry else []
        app_chain_tokens = list(app_default_chain or [])

        if proxy_tokens:
            # 优先级 1a：router_proxy.json 元素专属路由
            configured = parse_proxy_tokens(proxy_tokens)
            reason = f"router_proxy 定制命中: {[s.value for s in configured]}"
            skip_self_heal = True
        elif elem_chain_tokens:
            # 优先级 1b：rosetta_map.json 元素内联 strategy_chain
            configured = parse_proxy_tokens(elem_chain_tokens)
            reason = f"element strategy_chain 定制命中: {[s.value for s in configured]}"
            skip_self_heal = True
        elif default_proxy_entry is not None:
            # 优先级 2a：router_proxy.json 全局默认路由（__default__）
            configured = parse_proxy_tokens(default_proxy_entry.effective)
            reason = f"router_proxy __default__ 命中: {[s.value for s in configured]}"
            skip_self_heal = False
        elif app_chain_tokens:
            # 优先级 2b：App 适配级 default_strategy_chain（configs.py extra 或 rosetta_map _meta）
            configured = parse_proxy_tokens(app_chain_tokens)
            reason = f"app default_strategy_chain 命中: {[s.value for s in configured]}"
            skip_self_heal = False
        else:
            # 优先级 3：标准全链
            configured = []
            reason = "无定制配置，标准全链"
            skip_self_heal = False

        chain = build_effective_chain(configured, has_selector, is_native, has_pattern)
        return RouteDecision(strategies=chain, reason=reason, skip_self_heal=skip_self_heal)


def parse_proxy_tokens(tokens: Sequence[str]) -> list[ResolveStrategy]:
    """字符串 token 列表 → ResolveStrategy 列表（支持策略名如 TEXT_MATCH 或层级名如 L3 / L3_TEXT_MATCH）。"""
    result: list[ResolveStrategy] = []
    for raw_tok in tokens:
        tok = str(raw_tok or "").strip().upper()
        if not tok:
            continue
        if tok in _LEVEL_TOKEN_ALIASES:
            result.append(_LEVEL_TOKEN_ALIASES[tok])
            continue
        try:
            result.append(ResolveStrategy[tok])
        except KeyError:
            logger.warning("[router_proxy] 未知策略 token '%s'，已忽略", raw_tok)
    return result


def build_effective_chain(
    configured: list[ResolveStrategy],
    has_selector: bool,
    is_native: bool = False,
    has_pattern: bool = False,
) -> list[ResolveStrategy]:
    """configured 打头 + 标准全链去重补齐剩余步骤；再按信号裁剪不适用的步骤。"""
    unique_configured = list(dict.fromkeys(configured))
    remaining = [s for s in FULL_STRATEGY_ORDER if s not in unique_configured]
    chain = unique_configured + remaining
    if not has_selector:
        chain = [s for s in chain if s is not ResolveStrategy.MAP_EXACT]
    if is_native:
        chain = [s for s in chain if s is not ResolveStrategy.TEXT_MATCH]
    if not has_selector and has_pattern and not unique_configured:
        chain = [s for s in chain if s in (ResolveStrategy.VISUAL_FP, ResolveStrategy.LLM_VISUAL)]
    return chain
