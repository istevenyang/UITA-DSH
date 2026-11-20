"""tools/rosetta_diff/knowledge_writer.py — 把 RosettaMapDiff 追加写入知识库正式文档。

对齐 R22 人工审核门槛："diff 报告 + 人工确认后自动写入正式知识库文档"：
- 本模块**只做追加（append-only）**，从不覆盖/删除已有内容，避免破坏历史探查记录。
- 调用方（CLI `--mode probe-diff --apply` / Skill）必须先展示 diff 报告，
  用户显式确认（重新以 `--apply` 调用即视为确认）后才应调用这里的写入函数。
- 目标文件不存在时自动创建（覆盖范围声明：不强制预先建好知识库目录结构）。
"""
from __future__ import annotations

import datetime as _dt
import os

from uita.tools.rosetta_diff.differ import RosettaMapDiff


def probe_log_path(root_dir: str, app: str) -> str:
    return os.path.join(root_dir, ".dsh", "knowledge", "products", app, "element", "probe-log.md")


def known_issues_path(root_dir: str, app: str) -> str:
    return os.path.join(root_dir, ".dsh", "knowledge", "products", app, "element", "known-issues.md")


def _today() -> str:
    return _dt.date.today().strftime("%Y-%m-%d")


def build_probe_log_entry(diff: RosettaMapDiff, trigger: str = "probe-diff") -> str:
    """生成追加到 probe-log.md 的一段探查同步记录（Markdown 二级标题）。"""
    lines = [f"## {_today()} — probe-diff 自动同步（{trigger}）", ""]
    lines.append(f"- **新增元素**：{len(diff.added)}")
    lines.append(f"- **消失元素**：{len(diff.removed)}")
    lines.append(f"- **变更元素**：{len(diff.changed)}")
    lines.append(f"- **selector 冲突**：{len(diff.selector_conflicts)}")

    if diff.added:
        lines.append("")
        lines.append("**新增元素明细**：")
        for ref in diff.added:
            lines.append(f"- `{ref.page}` / {ref.name}")

    if diff.changed:
        lines.append("")
        lines.append("**变更元素明细**：")
        for chg in diff.changed:
            fields = "、".join(chg.field_changes.keys())
            lines.append(f"- `{chg.page}` / {chg.name}（字段：{fields}）")

    lines.append("")
    return "\n".join(lines)


def build_known_issues_entry(diff: RosettaMapDiff) -> str | None:
    """生成追加到 known-issues.md 的风险项记录（消失元素 / selector 冲突）。

    无风险项时返回 None（不追加空章节）。
    """
    if not diff.removed and not diff.selector_conflicts:
        return None

    lines = [f"## {_today()} — probe-diff 风险项（待人工复核）", ""]

    if diff.removed:
        lines.append("**消失元素**（探查中未再命中，可能页面改版/临时状态，需人工确认是否失效）：")
        for ref in diff.removed:
            lines.append(f"- `{ref.page}` / {ref.name}")
        lines.append("")

    if diff.selector_conflicts:
        lines.append("**selector 冲突**（新增元素的定位字段与已存在元素撞值，需人工澄清）：")
        for c in diff.selector_conflicts:
            lines.append(
                f"- `{c.page}` / {c.name} 的 `{c.field}`=`{c.value}` "
                f"与 `{c.owner_page}` / {c.owner_name} 冲突"
            )
        lines.append("")

    return "\n".join(lines)


def _append_markdown(path: str, entry: str, default_title: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()
        sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
        with open(path, "w", encoding="utf-8") as f:
            f.write(existing + sep + entry + "\n")
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(default_title + "\n\n" + entry + "\n")


def write_diff_to_knowledge(diff: RosettaMapDiff, root_dir: str, app: str, trigger: str = "probe-diff") -> dict:
    """把 diff 追加写入知识库（probe-log.md 必写，known-issues.md 仅有风险项时写）。

    仅在调用方已完成人工确认（`--apply`）后调用。返回 {"probe_log": path, "known_issues": path|None}。
    """
    plog_path = probe_log_path(root_dir, app)
    _append_markdown(plog_path, build_probe_log_entry(diff, trigger=trigger), f"# {app} — AX 树探查日志")

    ki_entry = build_known_issues_entry(diff)
    ki_path = None
    if ki_entry:
        ki_path = known_issues_path(root_dir, app)
        _append_markdown(ki_path, ki_entry, f"# {app} — 已知无 ID 控件与定位脆弱元素")

    return {"probe_log": plog_path, "known_issues": ki_path}
