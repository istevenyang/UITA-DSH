#!/usr/bin/env python3
"""ci/diff-skeleton.py — 统一 diff 骨架生成器（sentinel 步骤 2 专用）。

定位（T3）：把 `reference/02-diff-extraction.md` §2「一次 execute_command 跑脚本
生成骨架」固化为文件，避免每轮由模型现写一份不同的临时脚本——**纯格式化、零语义
判断**，不产出任何风险结论、不做任何采纳决策。

它只做一件确定性的事：读统一 diff 文本 → 输出覆盖 **100% 文件** 的骨架表。

字段严格对齐 `02-diff-extraction.md` §2：
  文件路径 / 增删行数 / hunk 数 / 变更性质（新增·删除·修改·重命名）/ `文件::方法或符号`

符号提取仅用 hunk 头 `@@ ... @@ <context>` 与常见语言的声明行正则；提取不到一律写
`符号待定`（**不猜**），由模型在深读阶段补全。

用法：
    .venv/bin/python ci/diff-skeleton.py <diff.raw.txt> [--json <out.json>] [--md <out.md>]
                                [--change-id <MR!123 或 COMMIT<sha8>>]
    # 不给 --md 时骨架表打到 stdout

退出码：0 成功；1 输入不可读或解析出 0 个文件（视为异常，绝不静默当"无变更"）
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
from typing import Dict, List, Optional

# ── diff 结构正则（统一 diff 格式，git / MCP 返回同构）────────────────────────
RE_DIFF_HEAD = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")
RE_NEW_FILE = re.compile(r"^new file mode ")
RE_DEL_FILE = re.compile(r"^deleted file mode ")
RE_RENAME_FROM = re.compile(r"^rename from (?P<p>.+)$")
RE_RENAME_TO = re.compile(r"^rename to (?P<p>.+)$")
RE_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?P<ctx>.*)$")
RE_OLD_PATH = re.compile(r"^--- (?:a/)?(?P<p>.+)$")
RE_NEW_PATH = re.compile(r"^\+\+\+ (?:b/)?(?P<p>.+)$")

# ── 符号声明正则（多语言，仅用于「看得见就取、看不见就留白」）──────────────
SYMBOL_PATTERNS = [
    # TS / JS
    re.compile(r"\b(?:export\s+)?(?:async\s+)?function\s+(?P<n>[A-Za-z_$][\w$]*)"),
    re.compile(r"\b(?:export\s+)?(?:abstract\s+)?class\s+(?P<n>[A-Za-z_$][\w$]*)"),
    re.compile(r"\b(?:export\s+)?const\s+(?P<n>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\("),
    re.compile(r"^\s*(?:public|private|protected|static|readonly|async|\s)*"
               r"(?P<n>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*[:{]"),
    # Python
    re.compile(r"^\s*(?:async\s+)?def\s+(?P<n>[A-Za-z_]\w*)"),
    re.compile(r"^\s*class\s+(?P<n>[A-Za-z_]\w*)"),
    # Go / Java / C-like
    re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(?P<n>[A-Za-z_]\w*)"),
    re.compile(r"^\s*(?:public|private|protected)\s+[\w<>\[\],\s]+\s+(?P<n>\w+)\s*\("),
    # Vue SFC 顶层块
    re.compile(r"^\s*<(?P<n>template|script|style)\b"),
]


def extract_symbol(text: str) -> Optional[str]:
    """从一行上下文中提取符号名；取不到返回 None（**不猜测**）。"""
    if not text or not text.strip():
        return None
    for pat in SYMBOL_PATTERNS:
        m = pat.search(text)
        if m:
            name = m.group("n")
            if name and name not in {"if", "for", "while", "switch", "catch", "return"}:
                return name
    return None


class FileEntry:
    __slots__ = ("path", "old_path", "added", "deleted", "hunks", "nature", "symbols")

    def __init__(self, path: str) -> None:
        self.path = path
        self.old_path: Optional[str] = None
        self.added = 0
        self.deleted = 0
        self.hunks = 0
        self.nature = "修改"
        self.symbols: List[str] = []

    def symbol_field(self) -> str:
        if not self.symbols:
            return "符号待定"
        uniq: List[str] = []
        for s in self.symbols:
            if s not in uniq:
                uniq.append(s)
        shown = uniq[:5]
        suffix = f" 等{len(uniq)}个" if len(uniq) > 5 else ""
        return "、".join(f"{self.path}::{s}" for s in shown) + suffix

    def to_dict(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "old_path": self.old_path,
            "added": self.added,
            "deleted": self.deleted,
            "hunks": self.hunks,
            "nature": self.nature,
            "symbols": self.symbols,
            "evidence_depth": "pending",  # 由模型深读后改 full / summary_only
        }


def parse_diff(text: str) -> List[FileEntry]:
    entries: List[FileEntry] = []
    cur: Optional[FileEntry] = None
    pending_rename_from: Optional[str] = None

    for line in text.splitlines():
        m = RE_DIFF_HEAD.match(line)
        if m:
            cur = FileEntry(m.group("b"))
            if m.group("a") != m.group("b"):
                cur.old_path = m.group("a")
            entries.append(cur)
            pending_rename_from = None
            continue
        if cur is None:
            # 某些来源不带 `diff --git` 头，用 +++ 行兜底建条目
            mn = RE_NEW_PATH.match(line)
            if mn and mn.group("p") != "/dev/null":
                cur = FileEntry(mn.group("p"))
                entries.append(cur)
            continue

        if RE_NEW_FILE.match(line):
            cur.nature = "新增"
            continue
        if RE_DEL_FILE.match(line):
            cur.nature = "删除"
            continue
        mrf = RE_RENAME_FROM.match(line)
        if mrf:
            pending_rename_from = mrf.group("p")
            continue
        mrt = RE_RENAME_TO.match(line)
        if mrt:
            cur.nature = "重命名"
            cur.old_path = pending_rename_from
            continue

        mh = RE_HUNK.match(line)
        if mh:
            cur.hunks += 1
            # ① 优先取 hunk 头自带的函数上下文（`@@ ... @@ func foo()`）。
            #    注意：MCP / 部分 git 配置返回的 hunk 头**不带**该上下文
            #    （实测 `@@ -1213,7 +1213,7 @@` 后为空），故还有 ②③ 两路兜底。
            sym = extract_symbol(mh.group("ctx"))
            if sym:
                cur.symbols.append(sym)
            continue

        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            cur.added += 1
            sym = extract_symbol(line[1:])
            if sym:
                cur.symbols.append(sym)
        elif line.startswith("-"):
            cur.deleted += 1
            # ② 被删除行同样可能含符号声明（如删掉整个函数）
            sym = extract_symbol(line[1:])
            if sym:
                cur.symbols.append(sym)
        elif line.startswith(" "):
            # ③ 上下文行兜底：hunk 头无函数名时，改动往往紧邻其所在函数/方法的
            #    声明行或调用点。只取声明形态（由 SYMBOL_PATTERNS 判定），
            #    取不到就留白 —— 仍然不猜。
            sym = extract_symbol(line[1:])
            if sym:
                cur.symbols.append(sym)

    return entries


def render_md(entries: List[FileEntry], change_id: Optional[str], zero_context: bool) -> str:
    total_a = sum(e.added for e in entries)
    total_d = sum(e.deleted for e in entries)
    total_h = sum(e.hunks for e in entries)
    pending = sum(1 for e in entries if not e.symbols)
    head = f"### 变更分节：{change_id}\n\n" if change_id else ""
    note = (
        f"{head}> 骨架由 `ci/diff-skeleton.py` 生成，**覆盖 100% 变更文件**"
        f"（{len(entries)} 文件 / +{total_a} −{total_d} / {total_h} hunk；"
        f"符号待定 {pending}）。`符号待定` 表示脚本未能从 diff 文本中提取到符号，"
        f"须在深读阶段补全，**不代表无风险**。\n"
    )
    if zero_context:
        note += (
            "\n> ⚠️ **本 diff 上下文行严重不足**（近乎只有 `+`/`-`，且 hunk 头无函数名）——"
            "这是外部远端 diff 接口返回形态，非解析失败。"
            "此形态下**脚本无法可靠定位 `文件::符号`**，全部记 `符号待定`；"
            "深读阶段必须结合**仓库源码**（按 hunk 行号回查所在函数）补全定位，"
            "**不得**因骨架未给符号就省略该文件的风险分析。\n"
        )
    lines = [
        note,
        "| # | 文件路径 | +/- | hunk | 变更性质 | 文件::方法/符号 |",
        "|---|---|---|---|---|---|",
    ]
    for i, e in enumerate(entries, 1):
        path = e.path if not e.old_path else f"{e.path}<br><sub>（原 {e.old_path}）</sub>"
        lines.append(
            f"| {i} | `{path}` | +{e.added} −{e.deleted} | {e.hunks} "
            f"| {e.nature} | {e.symbol_field()} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 diff 骨架（覆盖 100% 文件）")
    ap.add_argument("diff_file", help="统一 diff 文本路径（如 .cache/diff.raw.txt）")
    ap.add_argument("--json", dest="json_out", help="骨架 JSON 输出路径")
    ap.add_argument("--md", dest="md_out", help="骨架 Markdown 输出路径")
    ap.add_argument("--change-id", dest="change_id", help="所属变更标识（MR!123 / COMMITxxxxxxx）")
    args = ap.parse_args()

    p = pathlib.Path(args.diff_file)
    if not p.is_file():
        print(f"[diff-skeleton] 错误：diff 文件不存在: {p}", file=sys.stderr)
        return 1
    text = p.read_text(encoding="utf-8", errors="replace")
    entries = parse_diff(text)
    if not entries:
        print(
            f"[diff-skeleton] 错误：未从 {p} 解析出任何变更文件"
            f"（文件大小 {p.stat().st_size}B）。**不得**据此判定「无变更/无风险」，"
            f"请检查 diff 来源是否为空或格式异常。",
            file=sys.stderr,
        )
        return 1

    # 判定「上下文贫乏 diff」：有 hunk，但上下文行远少于常规 3 行/hunk 的形态
    # （典型：远端 commit diff 接口只回 +/- 行，hunk 头也不带函数名）。
    # 该形态下符号提取基本必然失败，必须在产物里显式说明，避免被误读为
    # "解析出错"或"这些文件没有符号所以没风险"。
    # 用比例而非「完全为零」判据：实测 MCP 返回可能夹带极少量上下文行
    # （本轮 6 hunk 仅 2 行），硬判零会漏报。
    has_hunk = sum(e.hunks for e in entries)
    ctx_lines = sum(1 for ln in text.splitlines() if ln.startswith(" ") and ln.strip())
    zero_context = bool(has_hunk) and ctx_lines < has_hunk  # 期望 ≥3/hunk，不足 1/hunk 即判贫乏

    md = render_md(entries, args.change_id, zero_context)
    if args.md_out:
        pathlib.Path(args.md_out).write_text(md, encoding="utf-8")
    else:
        print(md)

    if args.json_out:
        payload = {
            "change_id": args.change_id,
            "file_count": len(entries),
            "added": sum(e.added for e in entries),
            "deleted": sum(e.deleted for e in entries),
            "hunks": sum(e.hunks for e in entries),
            "symbol_pending": sum(1 for e in entries if not e.symbols),
            "zero_context_diff": zero_context,
            "files": [e.to_dict() for e in entries],
        }
        pathlib.Path(args.json_out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    print(
        f"[diff-skeleton] ✓ {len(entries)} 文件 / "
        f"+{sum(e.added for e in entries)} −{sum(e.deleted for e in entries)} / "
        f"{sum(e.hunks for e in entries)} hunk / "
        f"符号待定 {sum(1 for e in entries if not e.symbols)} 个"
        + ("（来源 diff 无上下文行，符号须按行号回查源码补全）" if zero_context else ""),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
