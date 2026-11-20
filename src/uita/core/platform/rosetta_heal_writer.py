"""
rosetta_heal_writer — 自愈回写本地 rosetta_map.json。

供 FallbackView.self_heal_writeback 和 HealBuffer.commit 调用，
在写 DB（{app}_rosetta_map_fallback 表）的同时把 selector/text/role/role_name
也回写到本地 JSON，保持 DB 与本地文件一致（离线可用）。

写入规则（与 DB 端一致，2026-07-24 三表迁移后统一命名，不再使用 post_xx 前缀；
2026-07-28 起 "js" 字段语义收敛为纯 JS 表达式，自愈回写改写 selector 字段，见
宪法 R22b）：
- selector: 直接写入 mac/win/linux/selector 对应字段（与主表/fallback 表字段名
  一一对应），不覆盖调用方未涉及的其他平台字段。
- text/role/role_name: 仅在当前为空时填充，不覆盖人工已填值。
- 回写后调用 _atomic_write 保证文件写入原子性。

路径解析：通过 AppRegistry 查找 app_name 对应的 rosetta_map_path；
若 AppRegistry 未注册则跳过（不阻断用例）。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# selector_field（原始平台字段名）→ rosetta_map.json 中的 JSON key，
# 与 core/db/models.py::FallbackRecord 字段名一一对应，不使用 post_ 前缀。
# 2026-07-28："js" 不再是自愈回写的合法 selector_field 取值（见宪法 R22b）：
# 自愈机制反查到的永远是真实 CSS selector，天然只应写 selector 字段；js 字段
# 专用存 JS 表达式，只能人工声明，运行时自愈不产生/不覆盖。
_FIELD_MAP = {
    "mac": "mac",
    "win": "win",
    "linux": "linux",
    "selector": "selector",
}


def writeback_to_json(
    app_name: str,
    element_id: str,
    element_name: str,
    *,
    selector: Optional[str] = None,
    selector_field: str = "mac",
    text: Optional[str] = None,
    role: Optional[str] = None,
    role_name: Optional[str] = None,
    label: Optional[str] = None,
) -> bool:
    """将自愈信息写入本地 rosetta_map.json。

    Args:
        app_name:       app 名，用于在 AppRegistry 中查 rosetta_map_path。
        element_id:     元素 id（rosetta_map 里的 id 字段），用于定位元素。
        element_name:   元素名称（用于日志）。
        selector:       自愈找到的新 selector。
        selector_field: 原始平台字段名（mac/win/linux/selector），直接写入对应字段。
        text/role/role_name/label: 仅在 JSON 中该字段为空时填充。label 为
            L2.5 LABEL_MATCH 用于匹配的 label 文本（2026-08-03 新增，见
            docs/plans/20260803150000-label-match-playwright-get-by-label.md §3.8）。

    Returns:
        True if any field was written.
    """
    map_path = _get_rosetta_map_path(app_name)
    if not map_path:
        logger.debug("[rosetta_heal_writer] app=%s 未注册 rosetta_map_path，跳过本地回写", app_name)
        return False
    if not os.path.exists(map_path):
        logger.debug("[rosetta_heal_writer] 文件不存在: %s，跳过", map_path)
        return False

    try:
        with open(map_path, encoding="utf-8") as f:
            raw: dict = json.load(f)
    except Exception as exc:
        logger.debug("[rosetta_heal_writer] 读取 JSON 失败（跳过）: %s", exc)
        return False

    # 遍历树形/平铺找到 element_id 对应的字段 dict
    target = _find_element_by_id(raw, element_id)
    if target is None:
        logger.debug(
            "[rosetta_heal_writer] element_id=%s (%s) 未在 %s 中找到，跳过",
            element_id, element_name, map_path,
        )
        return False

    changed = False
    # selector → mac/win/linux/js 对应字段（不使用 post_xx 前缀）
    if selector:
        field = _FIELD_MAP.get(selector_field, "mac")
        target[field] = selector
        changed = True

    # text/role/role_name 仅空时填充
    if text and not target.get("text"):
        target["text"] = text
        changed = True
    if role and not target.get("role"):
        target["role"] = role
        changed = True
    if role_name and not target.get("role_name"):
        target["role_name"] = role_name
        changed = True
    if label and not target.get("label"):
        target["label"] = label
        changed = True

    if not changed:
        return False

    try:
        _atomic_write(map_path, raw)
        logger.info(
            "[rosetta_heal_writer] 本地 JSON 回写成功: %s element=%r id=%s",
            map_path, element_name, element_id,
        )
    except Exception as exc:
        logger.debug("[rosetta_heal_writer] 写入 JSON 失败（非致命）: %s", exc)
        return False
    return True


def writeback_alias_to_json(
    app_name: str, element_id: str, element_name: str, alias: str,
) -> bool:
    """将别名追加到本地 rosetta_map.json 对应元素的 aliases 列表（去重）。

    与 RosettaMapView.add_alias 的 DB 写入配套，保持 DB 与本地 JSON 一致。

    Returns:
        True if alias was newly written.
    """
    map_path = _get_rosetta_map_path(app_name)
    if not map_path or not os.path.exists(map_path):
        logger.debug("[rosetta_heal_writer] app=%s 无 rosetta_map_path 或文件不存在，跳过别名回写", app_name)
        return False

    try:
        with open(map_path, encoding="utf-8") as f:
            raw: dict = json.load(f)
    except Exception as exc:
        logger.debug("[rosetta_heal_writer] 读取 JSON 失败（跳过）: %s", exc)
        return False

    target = _find_element_by_id(raw, element_id)
    if target is None:
        logger.debug(
            "[rosetta_heal_writer] element_id=%s (%s) 未在 %s 中找到，跳过别名回写",
            element_id, element_name, map_path,
        )
        return False

    aliases = target.get("aliases")
    if not isinstance(aliases, list):
        aliases = []
    if alias in aliases or alias == element_name:
        return False
    aliases.append(alias)
    target["aliases"] = aliases

    try:
        _atomic_write(map_path, raw)
        logger.info(
            "[rosetta_heal_writer] 别名回写成功: %s element=%r +alias=%r",
            map_path, element_name, alias,
        )
    except Exception as exc:
        logger.debug("[rosetta_heal_writer] 别名写入 JSON 失败（非致命）: %s", exc)
        return False
    return True


def _find_element_by_id(raw: dict, element_id: str) -> Optional[dict]:
    """递归在 raw 中找到 id==element_id 的元素字段 dict（就地修改用）。

    支持树形（app→page→element→{id:...}）和平铺（element→{id:...}）。
    """
    if not element_id:
        return None
    for key, value in raw.items():
        if key.startswith("_") or not isinstance(value, dict):
            continue
        # 平铺：value 本身是元素字段
        if value.get("id") == element_id:
            return value
        # 树形一层：value = {page: {element: fields}}
        for sub_key, sub_value in value.items():
            if not isinstance(sub_value, dict):
                continue
            # sub_value 可能是 page_dict 或直接是元素字段
            if sub_value.get("id") == element_id:
                return sub_value
            # 再深一层
            for el_name, fields in sub_value.items():
                if isinstance(fields, dict) and fields.get("id") == element_id:
                    return fields
    return None


def _get_rosetta_map_path(app_name: str) -> str:
    """通过 AppRegistry 查找 rosetta_map_path。"""
    try:
        from uita.core.platform.app_registry import AppRegistry
        import sys
        platform = "macos" if sys.platform == "darwin" else "windows"
        cfg = AppRegistry.get(app_name, platform)
        if cfg:
            return cfg.rosetta_map_path
        # 尝试不带平台匹配
        for key, val in AppRegistry._configs.items():
            if key[0] == app_name:
                return val.rosetta_map_path
    except Exception as exc:
        logger.debug("[rosetta_heal_writer] AppRegistry 查找失败: %s", exc)
    return ""


def _atomic_write(path: str, raw: dict) -> None:
    """原子写入（临时文件 + os.replace）。"""
    tmp = f"{path}.heal_tmp{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=4)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
