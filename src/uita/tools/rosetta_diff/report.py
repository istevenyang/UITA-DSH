"""tools/rosetta_diff/report.py — 将 RosettaMapDiff 渲染成 markdown / json 报告。

仅做纯展示格式化，不做任何文件写入（写入动作在 knowledge_writer.py，
且必须经 `--apply` 显式确认，对齐 R22 人工审核门槛）。
"""
from __future__ import annotations

from uita.tools.rosetta_diff.differ import RosettaMapDiff


def render_markdown(diff: RosettaMapDiff) -> str:
    lines: list = []
    app_label = diff.app or "(未指定 app)"
    lines.append(f"# rosetta_map 探查 diff 报告 — {app_label}")
    lines.append("")
    lines.append(f"- 新增元素: {len(diff.added)}")
    lines.append(f"- 消失元素: {len(diff.removed)}")
    lines.append(f"- 变更元素: {len(diff.changed)}")
    lines.append(f"- selector 冲突: {len(diff.selector_conflicts)}")
    lines.append("")

    if diff.added:
        lines.append("## 新增元素")
        for ref in diff.added:
            lines.append(f"- `{ref.page}` / **{ref.name}**")
        lines.append("")

    if diff.removed:
        lines.append("## 消失元素（可能页面改版或探查未覆盖）")
        for ref in diff.removed:
            lines.append(f"- `{ref.page}` / **{ref.name}**")
        lines.append("")

    if diff.changed:
        lines.append("## 变更元素")
        for chg in diff.changed:
            lines.append(f"- `{chg.page}` / **{chg.name}**")
            for f, (ov, nv) in chg.field_changes.items():
                lines.append(f"  - `{f}`: `{ov}` → `{nv}`")
        lines.append("")

    if diff.selector_conflicts:
        lines.append("## ⚠️ selector 冲突（需人工确认，不建议自动写入）")
        for c in diff.selector_conflicts:
            lines.append(
                f"- `{c.page}` / **{c.name}** 的 `{c.field}`=`{c.value}` "
                f"已被 `{c.owner_page}` / **{c.owner_name}** 占用"
            )
        lines.append("")

    if not diff.has_changes:
        lines.append("无变更。")

    return "\n".join(lines)


def render_json(diff: RosettaMapDiff) -> dict:
    return {
        "app": diff.app,
        "added": [{"page": r.page, "name": r.name, "entry": r.entry} for r in diff.added],
        "removed": [{"page": r.page, "name": r.name, "entry": r.entry} for r in diff.removed],
        "changed": [
            {
                "page": c.page,
                "name": c.name,
                "field_changes": {f: {"old": ov, "new": nv} for f, (ov, nv) in c.field_changes.items()},
            }
            for c in diff.changed
        ],
        "selector_conflicts": [
            {
                "page": c.page,
                "name": c.name,
                "field": c.field,
                "value": c.value,
                "owner_page": c.owner_page,
                "owner_name": c.owner_name,
            }
            for c in diff.selector_conflicts
        ],
        "has_changes": diff.has_changes,
    }
