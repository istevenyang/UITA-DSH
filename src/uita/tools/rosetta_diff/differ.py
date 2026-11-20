"""tools/rosetta_diff/differ.py — rosetta_map v3.0 语义 diff 引擎。

比较两份 v3.0 格式的 rosetta_map dict（例如"探查草稿" vs "现有正式配置"），
按 `app -> 页面 -> 元素` 粒度识别：
- added: 探查/新版本中新增的元素
- removed: 旧版本存在但新版本已消失的元素（可能页面改版/元素失效）
- changed: 同名元素定位字段（mac/win/linux/selector/js）或 usage 发生变化
- selector_conflicts: 新增元素的定位字段值与旧版本中其他已存在元素冲突
  （同一 selector/js/mac/win 被两个不同元素占用，通常意味着探查有误或
  rosetta_map 需要人工澄清，对齐 R22b 互斥约束的延伸检查）

不依赖 core/engine/apps 等运行时模块（R2 精神：工具层保持零侵入），
仅做纯数据结构比较，可被 CLI 层（cli/modes/probe_diff_mode.py）或
Skill（repair-rosetta / spec-to-test）复用。
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

# 参与"变更"判定的字段：定位字段 + usage 说明。
# pattern_mac/pattern_win/aliases/id 等指纹类/元信息字段体积大且易噪，不参与 diff。
DIFF_FIELDS: tuple = ("mac", "win", "linux", "selector", "js", "usage")

# 定位字段：用于 selector 冲突检测（同值被两个不同元素占用）。
LOCATOR_FIELDS: tuple = ("mac", "win", "linux", "selector", "js")

ElementKey = tuple  # (page, name)


@dataclass
class ElementRef:
    page: str
    name: str
    entry: dict = dc_field(default_factory=dict)


@dataclass
class ElementChange:
    page: str
    name: str
    field_changes: dict = dc_field(default_factory=dict)  # field -> (old, new)


@dataclass
class ConflictRef:
    page: str
    name: str
    field: str
    value: str
    owner_page: str
    owner_name: str


@dataclass
class RosettaMapDiff:
    app: str = ""
    added: list = dc_field(default_factory=list)
    removed: list = dc_field(default_factory=list)
    changed: list = dc_field(default_factory=list)
    selector_conflicts: list = dc_field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed or self.selector_conflicts)


def extract_app_section(rosetta_map: dict, app: str) -> dict:
    """从完整 rosetta_map JSON（含 `_meta`）中取出指定 app 的 `页面->元素` 子树。

    容错：app 不存在时返回空 dict，不抛异常（探查草稿可能只含目标 app 一份数据，
    也可能整份就是该 app 的子树本身——后者时直接把非 `_meta` 顶层当页面用）。
    """
    if not isinstance(rosetta_map, dict):
        return {}
    if app in rosetta_map and isinstance(rosetta_map[app], dict):
        return rosetta_map[app]
    # 兼容：传入的已经是"某个 app 的子树"（顶层直接是页面名）
    return {k: v for k, v in rosetta_map.items() if k != "_meta" and isinstance(v, dict)}


def _flatten(app_section: dict) -> dict:
    """把 `页面 -> 元素 -> entry` 展开成 `(page, name) -> entry`。"""
    flat: dict = {}
    for page_key, page_val in (app_section or {}).items():
        if not isinstance(page_val, dict) or page_key.startswith("_"):
            continue
        for elem_name, elem_val in page_val.items():
            if not isinstance(elem_val, dict) or elem_name.startswith("_"):
                continue
            flat[(page_key, elem_name)] = elem_val
    return flat


def _find_locator_owner(flat: dict, value: str, exclude_key) -> tuple | None:
    if not value:
        return None
    for key, entry in flat.items():
        if key == exclude_key:
            continue
        for f in LOCATOR_FIELDS:
            if entry.get(f) == value:
                return key
    return None


def diff_rosetta_maps(old_map: dict, new_map: dict, app: str = "") -> RosettaMapDiff:
    """对比 old（现有正式配置）与 new（探查草稿/新版本）中指定 app 的元素定义。

    `old_map`/`new_map` 均为完整 rosetta_map JSON dict（含或不含 `_meta`）。
    """
    old_flat = _flatten(extract_app_section(old_map, app))
    new_flat = _flatten(extract_app_section(new_map, app))

    diff = RosettaMapDiff(app=app)

    old_keys = set(old_flat)
    new_keys = set(new_flat)

    for key in sorted(new_keys - old_keys):
        page, name = key
        diff.added.append(ElementRef(page=page, name=name, entry=new_flat[key]))

    for key in sorted(old_keys - new_keys):
        page, name = key
        diff.removed.append(ElementRef(page=page, name=name, entry=old_flat[key]))

    for key in sorted(old_keys & new_keys):
        page, name = key
        old_entry, new_entry = old_flat[key], new_flat[key]
        field_changes: dict = {}
        for f in DIFF_FIELDS:
            ov, nv = old_entry.get(f, ""), new_entry.get(f, "")
            if ov != nv:
                field_changes[f] = (ov, nv)
        if field_changes:
            diff.changed.append(ElementChange(page=page, name=name, field_changes=field_changes))

    # selector/js/mac/win 冲突检测：新增元素的定位值是否已被旧版本中的
    # "另一个"元素占用（同名元素在 changed 分支已处理，这里只看 added）。
    for ref in diff.added:
        for f in LOCATOR_FIELDS:
            value = ref.entry.get(f)
            if not value:
                continue
            owner = _find_locator_owner(old_flat, value, exclude_key=(ref.page, ref.name))
            if owner:
                diff.selector_conflicts.append(
                    ConflictRef(
                        page=ref.page, name=ref.name, field=f, value=value,
                        owner_page=owner[0], owner_name=owner[1],
                    )
                )
                break  # 一个元素只记首个命中字段的冲突，避免报告冗余

    return diff
