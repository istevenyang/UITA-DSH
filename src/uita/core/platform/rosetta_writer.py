"""
rosetta_writer — 向 rosetta_map.json 写入/更新元素的工具函数。

设计原则（宪法 R30 关联计划：docs/plans/archive/03-resolver-rosetta-实施纪要.md）：
1. 已存在元素：仅当缺失 id 字段时补全 id，其余已有字段值一律不修改。
2. 新增元素：写入前校验 usage 必填、mac/win/selector/js 至少一个存在；缺失 id 则自动生成。
3. 追加时遵循该 root_key 现有的结构类型（树形按 page 追加；平铺/全新 root_key 直接根级追加）。
4. 不改变文件中其他任何既有元素/字段。
"""
from __future__ import annotations

import json
import os
import secrets
from typing import Optional

from uita.core.platform.element_map_loader import _FIELD_KEYS, _looks_like_element

# base36：小写字母 + 数字
_CHARSET = "0123456789abcdefghijklmnopqrstuvwxyz"

# 2026-07-28：新增 "selector"（web CSS/JS 选择器，与 "js"（纯 JS 表达式）语义
# 互斥，见宪法 R22b）。"js" 字段仍保留在候选集中——元素只声明 js 表达式（无任何
# 定位字段）也是合法的新增场景（供 assert[<element_name>]/eval_js[<element_name>]
# 使用），不应被此校验拒绝。
_SELECTOR_FIELDS = ("mac", "win", "selector", "js")


class RosettaValidationError(Exception):
    """新增元素字段校验失败（缺 usage 或缺 mac/win/selector/js）。"""


def generate_id() -> str:
    """生成 xxxxxx-xxxx-xxxxxxxx 格式 ID（base36 小写随机字符，6-4-8 分组）。"""
    def rand(n: int) -> str:
        return "".join(secrets.choice(_CHARSET) for _ in range(n))
    return f"{rand(6)}-{rand(4)}-{rand(8)}"


def _validate_new_fields(fields: dict) -> None:
    if not fields.get("usage"):
        raise RosettaValidationError("新增元素缺少必填字段 'usage'")
    if not any(fields.get(k) for k in _SELECTOR_FIELDS):
        raise RosettaValidationError(
            f"新增元素必须至少提供一个选择器字段: {_SELECTOR_FIELDS}"
        )


def _is_tree_root(root_value: dict) -> bool:
    """root_value 本身不像元素（不含已知字段 key）→ 判定为树形（app→page→element）。"""
    return not _looks_like_element(root_value)


def _find_existing(raw: dict, root_key: str, element_name: str) -> Optional[dict]:
    """在 raw[root_key] 中查找 element_name 对应的字段 dict（树形或平铺均适用）。"""
    root_value = raw.get(root_key)
    if not isinstance(root_value, dict):
        return None

    if _looks_like_element(root_value):
        # root_key 本身就是元素名的情况不在本函数处理范围（由调用方按 element_name==root_key 判断）
        return None

    # 树形：遍历所有页面
    for page_data in root_value.values():
        if isinstance(page_data, dict) and element_name in page_data:
            candidate = page_data[element_name]
            if isinstance(candidate, dict):
                return candidate
    return None


def upsert_element(
    map_path: str,
    root_key: str,
    element_name: str,
    fields: dict,
    page: Optional[str] = None,
) -> dict:
    """
    向 rosetta_map.json 写入/更新一个元素。

    Args:
        map_path:     rosetta_map.json 文件路径
        root_key:     根级 key。树形结构下为 app 名；平铺结构下可传 element_name 本身
                      （此时元素直接挂在根级，不经过 app/page 包装）
        element_name: 元素名（树形/平铺场景下的元素 key）
        fields:       元素字段 dict（如 {"mac": "...", "usage": "..."}）
        page:         树形结构下的页面名。root_key 现有内容为树形时必填；
                      root_key 不存在或为平铺时忽略（直接根级追加）

    Returns:
        更新后的完整 raw dict（已写回文件）

    Raises:
        RosettaValidationError: 新增元素校验失败（缺 usage 或缺 mac/win/selector/js）
    """
    if os.path.exists(map_path):
        with open(map_path, encoding="utf-8") as f:
            raw: dict = json.load(f)
    else:
        raw = {"_meta": {"version": "3.0"}}

    # ---- 平铺场景：root_key 直接就是元素名（不经过 app 包装）----
    if root_key == element_name:
        existing = raw.get(element_name)
        if isinstance(existing, dict) and _looks_like_element(existing):
            if not existing.get("id"):
                existing["id"] = generate_id()
            # 其余已有字段不做任何修改
        else:
            _validate_new_fields(fields)
            new_fields = dict(fields)
            new_fields.setdefault("id", generate_id())
            raw[element_name] = new_fields
        _write_back(map_path, raw)
        return raw

    # ---- 查找是否已存在（树形结构内）----
    existing = _find_existing(raw, root_key, element_name)
    if existing is not None:
        if not existing.get("id"):
            existing["id"] = generate_id()
        # 其余已有字段不做任何修改
        _write_back(map_path, raw)
        return raw

    # ---- 新增元素 ----
    _validate_new_fields(fields)
    new_fields = dict(fields)
    new_fields.setdefault("id", generate_id())

    root_value = raw.get(root_key)
    if root_value is None:
        # root_key 全新：默认按平铺方式挂载 {element_name: fields}
        raw[root_key] = {element_name: new_fields}
    elif isinstance(root_value, dict) and _is_tree_root(root_value):
        # 树形结构：追加到指定 page
        if not page:
            raise RosettaValidationError(
                f"root_key='{root_key}' 现有内容为树形结构，新增元素必须指定 page"
            )
        page_data = root_value.setdefault(page, {})
        page_data[element_name] = new_fields
    else:
        # root_key 现有内容为平铺元素本身（不应作为容器使用）
        raise RosettaValidationError(
            f"root_key='{root_key}' 已被用作平铺元素，无法作为容器追加子元素"
        )

    _write_back(map_path, raw)
    return raw


def _write_back(map_path: str, raw: dict) -> None:
    with open(map_path, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=4)
        f.write("\n")
