"""
semantic_matcher — L1.5 语义候选查找。

三层规则（优先级从高到低，命中即停止）：
  1. 别名精确匹配（aliases）
  2. 包含匹配（子串）：query ⊆ key 或 key ⊆ query
  3. 字符 bigram Jaccard 相似度 ≥ threshold

多候选时用 page_context 与 views 字段缩窄：
  取 views 与 page_context 有包含关系的候选；缩窄后为空则保留全量。

候选顺序：按规则优先级（别名 > 包含 > bigram），同规则内按最短 key / 最高 Jaccard 排序。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from uita.core.platform.element_map_loader import FlatEntry


# ── 工具函数 ─────────────────────────────────────────────────────────────

def _bigrams(s: str) -> set[str]:
    s = s.strip()
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else {s}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _narrow_by_context(
    candidates: list["FlatEntry"], page_context: str
) -> list["FlatEntry"]:
    """用 page_context 与 views 字段缩窄候选列表。

    缩窄规则：views 中任意元素与 page_context 有包含关系（互为子串）。
    缩窄后为空时保留原列表，避免过滤掉全部候选。
    """
    if not page_context or len(candidates) <= 1:
        return candidates
    narrowed = [
        c for c in candidates
        if any(page_context in v or v in page_context for v in c.views)
    ]
    return narrowed if narrowed else candidates


# ── 主接口 ───────────────────────────────────────────────────────────────

def find_candidates(
    query: str,
    entries: dict[str, "FlatEntry"],
    page_context: str = "",
    threshold: float = 0.5,
) -> list["FlatEntry"]:
    """返回语义匹配候选列表（按置信度降序排列），空列表表示无候选。

    Args:
        query:        用户输入的元素名（来自 TCS）
        entries:      rosetta_map 展平后的全量 FlatEntry 字典（key = element_name）
        page_context: 当前页面/功能区域，用于多候选缩窄；空字符串表示不缩窄
        threshold:    bigram Jaccard 相似度阈值（默认 0.5）
    """
    q = query.strip()
    if not q:
        return []

    # ── 规则 1：别名精确匹配 ──────────────────────────────────────────────
    q_lower = q.lower()
    alias_hits = [
        e for e in entries.values()
        if any(a.lower() == q_lower for a in e.aliases)
    ]
    if alias_hits:
        candidates = alias_hits
        return _narrow_by_context(candidates, page_context)

    # ── 规则 2：包含匹配（子串）──────────────────────────────────────────
    contain_hits: list[tuple[int, "FlatEntry"]] = []
    for key, entry in entries.items():
        if q in key or key in q:
            contain_hits.append((len(key), entry))
    if contain_hits:
        contain_hits.sort(key=lambda x: x[0])          # 最短 key 排首位
        candidates = [e for _, e in contain_hits]
        return _narrow_by_context(candidates, page_context)

    # ── 规则 3：字符 bigram Jaccard ───────────────────────────────────────
    q_bi = _bigrams(q)
    scored: list[tuple[float, "FlatEntry"]] = []
    for key, entry in entries.items():
        score = _jaccard(q_bi, _bigrams(key))
        if score >= threshold:
            scored.append((score, entry))
    if scored:
        scored.sort(key=lambda x: -x[0])               # 最高分排首位
        candidates = [e for _, e in scored]
        return _narrow_by_context(candidates, page_context)

    return []
