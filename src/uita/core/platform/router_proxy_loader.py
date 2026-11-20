"""
RouterProxyLoader — 加载 apps/<app_name>/router_proxy.json 并按元素 id 索引。

计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md §4.2
全局默认路由（__default__）：docs/plans/archive/03-resolver-rosetta-实施纪要.md

文件格式（key 为 rosetta_map.json 对应元素的 id 字段，非中文描述）：
{
    "__default__": {
        "default": "TEXT_MATCH,ROLE_MATCH"
    },
    "hxiw3m-hix9-zqndd90o": {
        "default": "TEXT_MATCH",
        "runtime_define": "TEXT_MATCH,VISUAL_FP"
    },
    "z7o98j-6kmd-1q9dk0sj": {
        "default": "VISUAL_FP"
    },
    "o06lw7-0uqm-0nvnayz0": {}
}

- "__default__" 是保留 key，表示全局默认路由（当元素无专属 id 路由时生效），
  只读 default 字段（不支持 runtime_define），见计划文档 §3。
- default/runtime_define 均可省略；值为逗号分隔字符串，token 内部统一转大写。
- runtime_define 非空时优先于 default（RouterProxyEntry.effective）。
- 文件不存在 / 内容为 {} / 某元素未出现在文件中，均视为"该元素无定制路由"。
- 非法 JSON、非 dict 的元素条目均容错为跳过（不抛异常，仅 logger.warning）。

本模块只做"字符串级"解析（token 拆分/大小写归一化），不引用 core/resolver/
里的 ResolveStrategy 枚举（避免 core/platform/ 依赖 core/resolver/，违反 R3 层级顺序：
dsl/ → engine/ → platform/ → resolver/ → ...），token → ResolveStrategy 的转换在
core/resolver/router.py::parse_proxy_tokens() 中完成。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from uita.core.rosetta.router_proxy_base import LocalJsonRouterProxyStore, RouterProxyStoreBase

logger = logging.getLogger(__name__)

# 全局默认路由的保留 key（不是 rosetta_map 元素 id，字面上不可能冲突）
ROUTER_PROXY_DEFAULT_KEY = "__default__"


def _parse_tokens(value) -> list[str]:
    """'TEXT_MATCH' → ['TEXT_MATCH']；'TEXT_MATCH,VISUAL_FP' → ['TEXT_MATCH','VISUAL_FP']；
    ''/None/缺失/非字符串 → []。"""
    if not value or not isinstance(value, str):
        return []
    return [tok.strip().upper() for tok in value.split(",") if tok.strip()]


@dataclass
class RouterProxyEntry:
    element_id: str
    default: list[str] = field(default_factory=list)
    runtime_define: list[str] = field(default_factory=list)

    @property
    def effective(self) -> list[str]:
        """R5：runtime_define 非空则优先，否则用 default。"""
        return self.runtime_define or self.default


class RouterProxyLoader:
    def __init__(self, map_path: str, store: Optional["RouterProxyStoreBase"] = None):
        self._map_path = map_path
        self._entries: dict[str, RouterProxyEntry] = {}
        self._loaded = False
        # 读取来源：DB 优先（DbFallbackRouterProxyStore），DB 不可用时回退本地 JSON。
        if store is None:
            try:
                from uita.core.rosetta.router_proxy_base import DbFallbackRouterProxyStore
                store = DbFallbackRouterProxyStore()
            except Exception:
                store = LocalJsonRouterProxyStore()
        self._store = store

    def load(self) -> None:
        """幂等加载；文件不存在/解析失败均降级为空（不抛异常，不阻断用例执行）。"""
        if self._loaded:
            return
        try:
            raw: dict = self._store.load_raw(self._map_path)
        except Exception as exc:  # noqa: BLE001 — router_proxy.json 是可选配置，读取异常不应阻断用例
            logger.warning("[router_proxy] 读取 %s 失败（忽略，视为无定制路由）: %s", self._map_path, exc)
            raw = {}

        entries: dict[str, RouterProxyEntry] = {}
        if isinstance(raw, dict):
            for element_id, fields in raw.items():
                if not isinstance(fields, dict):
                    logger.warning(
                        "[router_proxy] 元素 '%s' 的配置不是 dict，已忽略: %r", element_id, fields
                    )
                    continue
                entries[element_id] = RouterProxyEntry(
                    element_id=element_id,
                    default=_parse_tokens(fields.get("default")),
                    runtime_define=_parse_tokens(fields.get("runtime_define")),
                )
        else:
            logger.warning("[router_proxy] %s 根级不是 dict，已忽略整个文件", self._map_path)

        self._entries = entries
        self._loaded = True

    def find(self, element_id: str) -> Optional[RouterProxyEntry]:
        if not element_id:
            return None
        self.load()
        return self._entries.get(element_id)

    def default_route(self) -> Optional[RouterProxyEntry]:
        """返回全局默认路由条目（__default__ key）。

        不存在或 effective 为空时返回 None，调用方只需判断 is not None
        即可直接使用，无需再检查 .effective。

        计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md §4.1
        """
        self.load()
        entry = self._entries.get(ROUTER_PROXY_DEFAULT_KEY)
        if entry is None or not entry.effective:
            return None
        return entry

    def all_entries(self) -> dict[str, RouterProxyEntry]:
        self.load()
        return dict(self._entries)


def router_proxy_path_for(rosetta_map_path: str) -> str:
    """按约定推导 router_proxy.json 路径：与 rosetta_map.json 同目录。"""
    return os.path.join(os.path.dirname(rosetta_map_path), "router_proxy.json")
