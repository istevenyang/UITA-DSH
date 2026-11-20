"""
RouterPolicy — L1.5 语义候选界面核对全部失败后的降级策略接口。

当前为 stub 实现：fallback() 始终返回 None，调用方（Resolver._l1_5_semantic_match）
继续走下一个 ActionRouter 策略（P3 起为 TEXT_MATCH/ROLE_MATCH，取决于引擎类型）。

── 与 core/resolver/router.py::ActionRouter 的职责边界（P3 核实结论，见
   docs/plans/archive/03-resolver-rosetta-实施纪要.md §5）──

两者不是同一套机制的新旧版本，职责范围不同，均保留：

  - ActionRouter（core/resolver/router.py）：宏观策略路由——在 MAP_EXACT /
    TEXT_MATCH / ROLE_MATCH 这几个"解析层级"之间决定尝试顺序。
  - RouterPolicy（本文件）：微观候选选择——只在 L1.5 语义匹配已经召回了
    "多个同义/近似候选 FlatEntry，但界面核对（count_by_id）全部失败"这一
    细分场景下，决定要不要换一个候选 selector 再试一次；即使 ActionRouter
    未来接入 Pinned Strategy（backlog #3），也不会替代这一步"候选内部怎么选"
    的职责，二者是不同粒度的决策，因此保留、不删除、不合并。

未来接入真正的策略记录（backlog #3 StrategyStore）时，可在此处的 fallback()
内部消费 strategy_store 推荐的候选 selector，调用方（Resolver）无需修改。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from uita.core.engine.base import EngineBase
    from uita.core.platform.element_map_loader import FlatEntry


class RouterPolicy:
    """L1.5 候选选择降级策略接口（微观，见模块 docstring 的职责边界说明）。

    stub 阶段：fallback() 直接返回 None，等同"没有更好的候选可换"，调用方
    （Resolver._l1_5_semantic_match）视为 MAP_EXACT 整体失败，交还给
    ActionRouter 决策的下一个策略（P3 起为 TEXT_MATCH/ROLE_MATCH）。
    """

    def __init__(self, strategy_store=None):
        self._strategy_store = strategy_store  # 预留（backlog #3），当前为 None

    async def fallback(
        self,
        query: str,
        candidates: list["FlatEntry"],
        engine: "EngineBase",
        page_context: str = "",
    ) -> Optional["FlatEntry"]:
        """候选元素界面核对全部失败后的降级尝试。

        Returns:
            成功找到的 FlatEntry，或 None（调用方视为 MAP_EXACT 失败，交还给
            ActionRouter 决策的下一个策略）。

        stub 实现：直接返回 None。backlog #3 落地时可在此处消费
        strategy_store 推荐的候选，方法签名不需要变化。
        """
        return None
