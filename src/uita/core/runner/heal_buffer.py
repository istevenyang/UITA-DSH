"""HealBuffer — 用例级自愈回写缓冲。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class HealRecord:
    """单次自愈命中的暂存记录。"""
    element_id: str
    strategy_name: str
    latency_ms: float
    description: str = ""
    selector: Optional[str] = None
    selector_field: str = "mac"
    heal_text: str = ""
    heal_role: str = ""
    heal_role_name: str = ""
    heal_label: str = ""
    fallback_route: str = ""
    llm_prompt: str = ""


class HealBuffer:
    """用例级自愈缓冲 — 生命周期绑定一个 .tcs 文件执行周期。"""

    def __init__(self) -> None:
        self._records: list[HealRecord] = []
        self._reset_ids: set[str] = set()

    def record(self, rec: HealRecord) -> None:
        self._records.append(rec)
        logger.debug(
            "[HealBuffer] 暂存自愈记录: element_id=%s strategy=%s",
            rec.element_id, rec.strategy_name,
        )

    def record_reset(self, element_id: str) -> None:
        if not element_id:
            return
        self._reset_ids.add(element_id)

    def discard(self) -> None:
        self._records.clear()
        self._reset_ids.clear()

    def commit(
        self,
        candidate_store: Optional[object] = None,
        rosetta_map_view: Optional[object] = None,
        fallback_view: Optional[object] = None,
        router_proxy_view: Optional[object] = None,
    ) -> int:
        self._reset_ids.clear()
        if not self._records:
            return 0
        committed = 0
        for rec in self._records:
            try:
                if candidate_store is not None:
                    candidate_store.record_hit(
                        rec.element_id, rec.strategy_name,
                        description=rec.description,
                    )
                committed += 1
            except Exception as exc:
                logger.warning("[HealBuffer] 提交失败（静默）: element_id=%s exc=%s", rec.element_id, exc)
        self._records.clear()
        return committed

    def __len__(self) -> int:
        return len(self._records)
