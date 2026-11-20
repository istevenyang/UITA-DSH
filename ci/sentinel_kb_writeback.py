#!/usr/bin/env python3
"""受控知识正文改写（sentinel W3 机器区块协议）。

为什么需要这个脚本
------------------
sentinel 自愈回写原本只能改 `utest-rules/04-known-pitfalls.md` 的**人工表格**；
一旦允许模型改知识正文，"AI 污染人工知识" 就成为最高风险项。故本脚本把改写
限制在一个**可机读、可审计、可回滚**的机器区块协议内：

    <!-- kb:auto id=<stable-key> run=<run_key> at=<ISO8601> -->
    （机器生成内容）
    <!-- /kb:auto -->

硬规则（与 `reference/08-self-heal-writeback.md`「知识正文改写」小节逐条对应）
--------------------------------------------------------------------------
1. 只允许写 `kb:auto` 区块**内部**；区块外任何字节差异 ⇒ `out_of_scope_changes[]` + stop。
2. 同 `id` 重写 = 原位替换（幂等）；内容（归一化后）未变 ⇒ 整个文件一个字节都不动。
3. 同 `id` 新结论与旧结论冲突 ⇒ 由调用方显式声明 `"conflict": true`，两条**都保留**
   并把新块落成 `<id>@conflict:<ts>`，记入 `conflicts[]`，**不静默覆盖**。
4. 整文件由机器创建**仅限白名单文件**：缺陷链、运行历史摘要，以及知识库五维
   （`business/business-endpoints.md`、`business/business-flows.md`、
   `architecture/technical-endpoints.md`、`architecture/technical-architecture.md`、
   `utest-rules/07-test-design.md`）——文件头由本脚本固定生成，调用方塞不进区块外内容。
5. 白名单外的目标文件一律拒绝（退出码 2）；既有 `utest-rules/04-known-pitfalls.md` 与五维文件
   只允许区块内改写。
6. 每次改写落 `<report-dir>/kb-writeback.json`（前后 sha256 + 区块台账 + 冲突 + 越界记录），
   原文件副本落 `<report-dir>/kb-backup/<相对路径镜像>` —— 两者即回滚依据。

退出码
------
* `0` = 成功（含"全部内容未变"与 `--dry-run` 通过）
* `1` = 守卫拦截（越界 / 协议损坏 / 命中密钥特征）——**stop**，不得继续上报
* `2` = 参数或输入错误（白名单外文件、JSON 坏、缺字段）

只用标准库，无第三方依赖（宪法 R13）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = "sentinel-kb-writeback/v1"

#: 机器可**整文件创建**的白名单（`<product>` 为占位符）。
#: 五维知识文件（业务端点 / 业务流程 / 技术端点 / 技术架构 / 测试设计）同样允许整文件创建：
#: 文件头由本脚本固定模板生成，调用方塞不进区块外内容。
CREATABLE_TEMPLATES = (
    ".dsh/knowledge/products/{product}/defects/{product}-defect-chain.md",
    ".dsh/knowledge/products/{product}/utest-rules/06-run-history-digest.md",
    ".dsh/knowledge/products/{product}/business/business-endpoints.md",
    ".dsh/knowledge/products/{product}/business/business-flows.md",
    ".dsh/knowledge/products/{product}/architecture/technical-endpoints.md",
    ".dsh/knowledge/products/{product}/architecture/technical-architecture.md",
    ".dsh/knowledge/products/{product}/utest-rules/07-test-design.md",
)

#: 允许**区块内**改写的既有文件（不得整文件覆盖）。
BLOCK_EDITABLE_TEMPLATES = (
    ".dsh/knowledge/products/{product}/utest-rules/04-known-pitfalls.md",
    ".dsh/knowledge/products/{product}/business/business-endpoints.md",
    ".dsh/knowledge/products/{product}/business/business-flows.md",
    ".dsh/knowledge/products/{product}/architecture/technical-endpoints.md",
    ".dsh/knowledge/products/{product}/architecture/technical-architecture.md",
    ".dsh/knowledge/products/{product}/utest-rules/07-test-design.md",
)

#: 各可创建文件的固定标题（`build_new_file_text` 用；按文件名匹配，未列出的走兜底标题）。
FILE_TITLES: dict[str, str] = {
    "{product}-defect-chain.md": "# {product} · 缺陷链（机器维护）",
    "06-run-history-digest.md": "# {product} · 运行历史摘要（机器维护）",
    "business-endpoints.md": "# {product} · 业务端点（业务维护 + 机器区块）",
    "business-flows.md": "# {product} · 业务流程（业务维护 + 机器区块）",
    "technical-endpoints.md": "# {product} · 技术端点（机器区块）",
    "technical-architecture.md": "# {product} · 技术架构（机器区块）",
    "07-test-design.md": "# {product} · 测试设计（风险→手段→断言契约）",
}

BLOCK_RE = re.compile(
    r"<!--\s*kb:auto\s+(?P<attrs>[^>]*?)-->(?P<body>.*?)<!--\s*/kb:auto\s*-->",
    re.DOTALL,
)
OPEN_MARKER_RE = re.compile(r"<!--\s*kb:auto\s")
CLOSE_MARKER_RE = re.compile(r"<!--\s*/kb:auto\s*-->")
ATTR_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)=(\S+)")

#: 命中即 stop 的密钥特征（知识正文会进图、进页面，必须挡住）。
SECRET_PATTERNS = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(
        r"(?i)(password|passwd|api[_-]?key|secret|access[_-]?token)\s*[:=]\s*[A-Za-z0-9+/=_\-]{16,}",
    ),
)


class GuardViolation(Exception):
    """守卫拦截（退出码 1）。"""

    def __init__(self, message: str, *, out_of_scope: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.out_of_scope = list(out_of_scope)


class InputError(Exception):
    """参数或输入错误（退出码 2）。"""


# ── 工具 ─────────────────────────────────────────────────────────────────────

def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _outside_blocks(text: str) -> str:
    """挖掉全部 `kb:auto` 区块（含标记行）后剩下的文本 —— 越界检测的判据。"""
    return BLOCK_RE.sub("", text)


def _normalize(body: str) -> str:
    return body.strip()


def _template_whitelist(product: str) -> tuple[set[str], set[str]]:
    creatable = {item.format(product=product) for item in CREATABLE_TEMPLATES}
    editable = {item.format(product=product) for item in BLOCK_EDITABLE_TEMPLATES}
    return creatable, editable


def _rel_posix(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def rules_meta_path(root: Path, product: str) -> Path:
    return (
        root / ".dsh" / "knowledge" / "products" / product
        / "utest-rules" / "rules-meta.json"
    )


def read_rules_version(root: Path, product: str) -> Any:
    """只**读** `rules-meta.json` 的 `rules_version`（本脚本严禁改写该文件）。

    **原样返回**（实测取值为 `"v29"` / `"v1"` 这类字符串，不是整数；强制转 int 会把
    权威值读成「未知」）。读不到 / 坏 JSON ⇒ `None`（由报告如实标注"未知"，不猜不造）。
    """
    path = rules_meta_path(root, product)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    value = payload.get("rules_version") if isinstance(payload, dict) else None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return value


def validate_product(root: Path, product: str) -> None:
    """护栏：`--product-code` 必须是**知识目录名**（如 `sample_web`），不是外部平台编码。

    传错会**静默新建一个错误目录**。故这里硬拦：
    ① 产品目录必须已存在；② 若 `rules-meta.json` 里写了 `product_code`，必须与入参一致。
    """
    base = root / ".dsh" / "knowledge" / "products"
    target = base / product
    if not target.is_dir():
        available = sorted(item.name for item in base.iterdir() if item.is_dir()) if base.is_dir() else []
        raise InputError(
            f"知识目录不存在：{target}\n"
            f"  注意：此处要传**知识目录名**（如 `sample_web`），"
            f"不是外部平台产品编码。\n"
            f"  现有目录：{available}",
        )
    meta = target / "utest-rules" / "rules-meta.json"
    if meta.is_file():
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        declared = payload.get("product_code") if isinstance(payload, dict) else None
        if isinstance(declared, str) and declared.strip() and declared.strip() != product:
            raise InputError(
                f"product_code 不一致：入参 {product} vs rules-meta.json 的 {declared.strip()}",
            )


@dataclass
class Block:
    """一个待写入的机器区块。"""

    file: str
    id: str
    content: str
    conflict: bool = False


@dataclass
class BlockResult:
    id: str
    status: str  # inserted | replaced | unchanged | conflict
    before_sha256: str | None = None
    after_sha256: str | None = None
    conflict_with: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "conflict_with": self.conflict_with,
        }


@dataclass
class FileResult:
    path: str
    action: str  # created | updated | unchanged
    before_sha256: str | None
    after_sha256: str | None
    backup_path: str | None
    blocks: list[BlockResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "action": self.action,
            "before_sha256": self.before_sha256,
            "after_sha256": self.after_sha256,
            "backup_path": self.backup_path,
            "blocks": [block.as_dict() for block in self.blocks],
        }


@dataclass
class ParsedBlock:
    id: str
    attrs: dict[str, str]
    body: str
    span: tuple[int, int]


# ── 区块解析 / 渲染 ──────────────────────────────────────────────────────────

def parse_blocks(text: str) -> list[ParsedBlock]:
    """解析全部机器区块；协议损坏（标记不配对 / 重复 id / 缺 id）即抛 GuardViolation。"""
    parsed: list[ParsedBlock] = []
    for match in BLOCK_RE.finditer(text):
        attrs = dict(ATTR_RE.findall(match.group("attrs")))
        block_id = attrs.get("id")
        if not block_id:
            raise GuardViolation("kb:auto 区块缺少 id 属性（协议损坏）")
        parsed.append(
            ParsedBlock(
                id=block_id,
                attrs=attrs,
                body=match.group("body"),
                span=(match.start(), match.end()),
            ),
        )
    ids = [item.id for item in parsed]
    duplicates = sorted({item for item in ids if ids.count(item) > 1})
    if duplicates:
        raise GuardViolation(f"kb:auto 区块 id 重复：{', '.join(duplicates)}（协议损坏）")

    # 标记配对：正则只认完整块，故这里补查「孤立标记」。
    if len(OPEN_MARKER_RE.findall(text)) != len(parsed):
        raise GuardViolation("存在未闭合的 kb:auto 开始标记（协议损坏）")
    if len(CLOSE_MARKER_RE.findall(text)) != len(parsed):
        raise GuardViolation("存在无对应开始标记的 <!-- /kb:auto -->（协议损坏）")
    return parsed


def render_block(block_id: str, body: str, *, run_key: str, at: str) -> str:
    return (
        f"<!-- kb:auto id={block_id} run={run_key} at={at} -->\n"
        f"{body.strip()}\n"
        f"<!-- /kb:auto -->"
    )


def _append_block(text: str, rendered: str) -> tuple[str, str]:
    """在文本末尾追加区块，返回 (新文本, 本次追加引入的**区块外**字节)。

    区块外字节 = 分隔符 + 区块后的换行；返回值用于越界复核（不得吞掉任何字符，
    也不得靠 `rstrip` 掩盖差异）。
    """
    if not text.strip():
        # 空文件：整篇就是区块，区块外只剩结尾换行。
        return f"{rendered}\n", "\n"
    if text.endswith("\n\n"):
        separator = ""
    elif text.endswith("\n"):
        separator = "\n"
    else:
        separator = "\n\n"
    return f"{text}{separator}{rendered}\n", f"{separator}\n"


def _check_block_content(block: Block) -> None:
    stripped = block.content.strip()
    if not stripped:
        raise InputError(f"区块 {block.id} 内容为空")
    if OPEN_MARKER_RE.search(block.content) or CLOSE_MARKER_RE.search(block.content):
        raise GuardViolation(
            f"区块 {block.id} 内容里含 kb:auto 标记（会造成嵌套 / 协议损坏）",
            out_of_scope=[f"{block.file}#{block.id}:nested-marker"],
        )
    for pattern in SECRET_PATTERNS:
        if pattern.search(block.content):
            raise GuardViolation(
                f"区块 {block.id} 内容命中密钥特征（{pattern.pattern[:24]}…），已 stop",
                out_of_scope=[f"{block.file}#{block.id}:secret_pattern"],
            )


# ── 应用（纯函数，便于单测） ─────────────────────────────────────────────────

def apply_blocks_to_text(
    old_text: str,
    blocks: Sequence[Block],
    *,
    run_key: str,
    at: str,
    existing: Sequence[ParsedBlock] | None = None,
) -> tuple[str, list[BlockResult], list[dict[str, Any]]]:
    """把区块写进 `old_text`，返回 (新文本, 区块台账, 冲突记录)。

    越界不变量：区块外文本必须**只有**我们主动追加的分隔符发生变化，即
    ``_outside_blocks(new) == _outside_blocks(old) + 追加分隔符``；不成立即
    GuardViolation（构造逻辑已保证，这里是防守式复核，防未来改动引入越界）。
    """
    parsed = list(existing) if existing is not None else parse_blocks(old_text)
    by_id = {item.id: item for item in parsed}
    # 既有冲突块（`<id>@conflict:<ts>`）索引：同结论重跑必须幂等，不得一次一个时间戳。
    conflict_seen: dict[str, list[ParsedBlock]] = {}
    for item in parsed:
        head, _, tail = item.id.partition("@conflict:")
        if tail:
            conflict_seen.setdefault(head, []).append(item)

    chunks: list[str] = []
    cursor = 0
    results: list[BlockResult] = []
    conflicts: list[dict[str, Any]] = []
    # 已解析区块按原文顺序处理：先把「需要原位替换」的区块写出去，再统一附加新块。
    replaced: dict[str, Block] = {}
    appended: list[Block] = []
    for block in blocks:
        _check_block_content(block)
        current = by_id.get(block.id)
        if current is None:
            appended.append(block)
            continue
        if _normalize(current.body) == _normalize(block.content):
            digest = sha256_text(_normalize(current.body))
            results.append(
                BlockResult(
                    id=block.id, status="unchanged",
                    before_sha256=digest, after_sha256=digest,
                ),
            )
            continue
        if block.conflict:
            kept = next(
                (
                    item
                    for item in conflict_seen.get(block.id, [])
                    if _normalize(item.body) == _normalize(block.content)
                ),
                None,
            )
            if kept is not None:
                digest = sha256_text(_normalize(kept.body))
                results.append(
                    BlockResult(
                        id=kept.id, status="unchanged",
                        before_sha256=digest, after_sha256=digest,
                        conflict_with=block.id,
                    ),
                )
                continue
            conflicts.append(
                {
                    "id": block.id,
                    "resolved_as": f"{block.id}@conflict:{at}",
                    "reason": "explicit",
                },
            )
            appended.append(
                Block(id=block.id, content=block.content, conflict=True, file=block.file),
            )
            continue
        replaced[block.id] = block

    for item in parsed:
        chunks.append(old_text[cursor:item.span[0]])
        block = replaced.get(item.id)
        if block is None:
            chunks.append(old_text[item.span[0]:item.span[1]])
        else:
            rendered = render_block(item.id, block.content, run_key=run_key, at=at)
            chunks.append(rendered)
            results.append(
                BlockResult(
                    id=item.id,
                    status="replaced",
                    before_sha256=sha256_text(_normalize(item.body)),
                    after_sha256=sha256_text(_normalize(block.content)),
                ),
            )
        cursor = item.span[1]
    chunks.append(old_text[cursor:])
    new_text = "".join(chunks)

    # 追加区块：只允许在**文件末尾**追加，且追加引入的区块外字节必须可枚举
    # （分隔符 + 结尾换行），越界复核据此逐字节比对。
    outside_additions: list[str] = []
    for block in appended:
        if block.conflict:
            new_id = f"{block.id}@conflict:{at}"
            status = "conflict"
            conflict_with = block.id
        else:
            new_id = block.id
            status = "inserted"
            conflict_with = None
        rendered = render_block(new_id, block.content, run_key=run_key, at=at)
        new_text, addition = _append_block(new_text, rendered)
        outside_additions.append(addition)
        results.append(
            BlockResult(
                id=new_id,
                status=status,
                before_sha256=None,
                after_sha256=sha256_text(block.content.strip()),
                conflict_with=conflict_with,
            ),
        )

    expected_outside = _outside_blocks(old_text) + "".join(outside_additions)
    if _outside_blocks(new_text) != expected_outside:
        raise GuardViolation(
            "区块外内容发生变化（越界）",
            out_of_scope=["outside_kb_auto_region_modified"],
        )
    return new_text, results, conflicts


def build_new_file_text(product: str, blocks: Sequence[Block], *, run_key: str, at: str) -> str:
    """白名单新文件的**固定文件头** + 机器区块（调用方无法塞区块外内容）。"""
    name = blocks[0].file.rsplit("/", 1)[-1]
    template = FILE_TITLES.get(name)
    if template is None:
        # `{product}-defect-chain.md` 这类模板要用产品名展开后再比对文件名。
        template = next(
            (
                value
                for key, value in FILE_TITLES.items()
                if key.format(product=product) == name
            ),
            "# {product} · 知识正文（机器维护）",
        )
    title = template.format(product=product)
    header = (
        f"{title}\n\n"
        f"> 本文件由 sentinel 步骤 8 的机器区块协议维护：机器**只允许改写 `kb:auto` "
        f"区块内部**，区块外内容由业务维护、机器不得改动。\n"
        f"> 生成轮次：`{run_key}`，生成时间：{at}\n"
    )
    body = "\n\n".join(
        render_block(block.id, block.content, run_key=run_key, at=at) for block in blocks
    )
    return f"{header}\n{body}\n"


# ── 输入装配 ─────────────────────────────────────────────────────────────────

def load_input(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InputError(f"输入文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"输入 JSON 解析失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise InputError("输入 JSON 顶层必须是对象")
    return payload


def build_blocks(payload: dict[str, Any], *, root: Path, product: str) -> list[Block]:
    raw = payload.get("blocks")
    if not isinstance(raw, list) or not raw:
        raise InputError("输入 JSON 缺少非空 blocks 数组")
    creatable, editable = _template_whitelist(product)
    allowed = creatable | editable
    blocks: list[Block] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InputError(f"blocks[{index}] 必须是对象")
        file_ref = str(item.get("file") or "").strip()
        block_id = str(item.get("id") or "").strip()
        content = item.get("content")
        if not file_ref or not block_id or not isinstance(content, str):
            raise InputError(f"blocks[{index}] 缺少 file / id / content")
        resolved = (root / file_ref).resolve()
        try:
            rel = resolved.relative_to(root.resolve()).as_posix()
        except ValueError as exc:
            raise InputError(f"blocks[{index}].file 越出工作区：{file_ref}") from exc
        if rel not in allowed:
            raise InputError(
                f"blocks[{index}].file 不在机器可写白名单：{rel}\n  允许：{sorted(allowed)}\n"
                f"  提示：路径里的 `<product>` 是**知识目录名**（如 `sample_web`）。",
            )
        blocks.append(
            Block(
                file=rel,
                id=block_id,
                content=content,
                conflict=bool(item.get("conflict")),
            ),
        )
    return blocks


# ── 主流程 ───────────────────────────────────────────────────────────────────

def run(
    *,
    workspace_root: Path,
    report_dir: Path,
    run_key: str,
    product_code: str,
    blocks: Sequence[Block],
    rules_version: Any = None,
    summary: str = "",
    dry_run: bool = False,
    now: str | None = None,
) -> tuple[dict[str, Any], int]:
    at = now or datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    validate_product(workspace_root, product_code)
    creatable, _ = _template_whitelist(product_code)
    by_file: dict[str, list[Block]] = {}
    for block in blocks:
        by_file.setdefault(block.file, []).append(block)

    # `rules_version` 以 `rules-meta.json`（**只读**，本脚本严禁改写）为准；
    # 输入里的值只作对照记录，防「报告写的版本 ≠ 知识库实际版本」。
    declared = rules_version
    observed = read_rules_version(workspace_root, product_code)
    effective = observed if observed is not None else declared

    report: dict[str, Any] = {
        "schema": SCHEMA,
        "run_key": run_key,
        "generated_at": at,
        "product_code": product_code,
        "rules_version": effective,
        "rules_version_source": (
            "rules-meta.json" if observed is not None else ("input" if declared is not None else "unknown")
        ),
        "rules_version_declared": declared,
        "summary": summary,
        "policy": {"protocol": "kb:auto", "conflict_policy": "keep-both", "dry_run": dry_run},
        "files": [],
        "conflicts": [],
        "out_of_scope_changes": [],
        "totals": {
            "files_total": len(by_file),
            "files_changed": 0,
            "blocks_written": 0,
            "blocks_unchanged": 0,
            "conflicts": 0,
        },
        "status": "ok",
    }

    # 两阶段：**先全量计划（零写入）再提交** —— 任一路径的守卫拦截都不得留下半成品
    # （否则「越界即 stop」会变成「越界且已改了前一个文件」）。
    plans: list[tuple[str, bool, str, str, list[BlockResult], list[dict[str, Any]]]] = []
    try:
        for rel, file_blocks in by_file.items():
            target = workspace_root / rel
            existed = target.exists()
            old_text = target.read_text(encoding="utf-8") if existed else ""
            seen: set[str] = set()
            for block in file_blocks:
                if block.id in seen:
                    raise InputError(f"{rel} 的输入区块 id 重复：{block.id}")
                seen.add(block.id)
            if existed:
                new_text, results, conflicts = apply_blocks_to_text(
                    old_text, file_blocks, run_key=run_key, at=at,
                )
            else:
                if rel not in creatable:
                    raise InputError(f"文件不存在且不属于可整文件创建的白名单：{rel}")
                for block in file_blocks:  # 新建路径同样过协议/密钥检查（防借"新文件"越狱）
                    _check_block_content(block)
                new_text = build_new_file_text(
                    product_code, file_blocks, run_key=run_key, at=at,
                )
                results = [
                    BlockResult(
                        id=block.id,
                        status="inserted",
                        after_sha256=sha256_text(block.content.strip()),
                    )
                    for block in file_blocks
                ]
                conflicts = []
            plans.append((rel, existed, old_text, new_text, results, conflicts))
    except GuardViolation as exc:
        report["status"] = "stopped"
        report["reason"] = str(exc)
        report["out_of_scope_changes"].extend(exc.out_of_scope)
        _write_report(report, report_dir, dry_run=dry_run)
        return report, 1

    for rel, existed, old_text, new_text, results, conflicts in plans:
        changed = new_text != old_text
        backup_rel: str | None = None
        if changed and not dry_run:
            if existed:
                backup = report_dir / "kb-backup" / rel
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(workspace_root / rel, backup)
                backup_rel = _rel_posix(backup, report_dir)
            target = workspace_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_text, encoding="utf-8")

        action = "unchanged"
        if changed:
            action = "updated" if existed else "created"
        report["files"].append(
            FileResult(
                path=rel,
                action=action,
                before_sha256=sha256_text(old_text) if existed else None,
                after_sha256=sha256_text(new_text),
                backup_path=backup_rel,
                blocks=results,
            ).as_dict(),
        )
        report["conflicts"].extend(conflicts)
        if changed:
            report["totals"]["files_changed"] += 1
        for result in results:
            if result.status == "unchanged":
                report["totals"]["blocks_unchanged"] += 1
            else:
                report["totals"]["blocks_written"] += 1

    report["totals"]["conflicts"] = len(report["conflicts"])
    if report["totals"]["files_changed"] == 0:
        report["status"] = "no_change"
    _write_report(report, report_dir, dry_run=dry_run)
    return report, 0


def _write_report(report: dict[str, Any], report_dir: Path, *, dry_run: bool) -> None:
    if dry_run:
        return
    report_dir.mkdir(parents=True, exist_ok=True)
    target = report_dir / "kb-writeback.json"
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


# ── DSH Tool 便捷写入包装（复用同一套 kb:auto 守卫） ───────────────────────────

def read_json(path: Path) -> dict[str, Any] | None:
    """安全读取 JSON 对象文件，失败返回 None。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _run_single_block(
    workspace_root: Path,
    product: str,
    rel_file: str,
    block_id: str,
    content: str,
    *,
    summary: str = "",
) -> dict[str, Any]:
    report_dir = workspace_root / "report" / "kb_writeback"
    run_key = datetime.now().strftime("dsh-%Y%m%d%H%M%S")
    block = Block(file=rel_file, id=block_id, content=content)
    report, _ = run(
        workspace_root=workspace_root,
        report_dir=report_dir,
        run_key=run_key,
        product_code=product,
        blocks=[block],
        summary=summary,
    )
    return report


def append_pitfall(
    workspace_root: Path,
    product: str,
    *,
    title: str,
    symptom: str,
    root_cause: str,
    rule: str,
    evidence: str,
    dedup_key: str | None = None,
) -> dict[str, Any]:
    key = (dedup_key or title or "pitfall").strip().replace(" ", "_")
    block_id = f"pitfall:{key}"
    rel = f".dsh/knowledge/products/{product}/utest-rules/04-known-pitfalls.md"
    content = (
        f"### {title}\n"
        f"- **现象**：{symptom}\n"
        f"- **根因**：{root_cause}\n"
        f"- **防呆规则**：{rule}\n"
        f"- **证据**：{evidence}"
    )
    return _run_single_block(workspace_root, product, rel, block_id, content, summary=f"pitfall:{title}")


def append_defect_chain(
    workspace_root: Path,
    product: str,
    *,
    chain_id: str,
    title: str,
    trigger: str,
    propagation: str,
    fix_anchor: str,
    regression_case: str,
    evidence: str,
) -> dict[str, Any]:
    cid = (chain_id or title or "chain").strip().replace(" ", "_")
    block_id = f"defect-chain:{cid}"
    rel = f".dsh/knowledge/products/{product}/defects/{product}-defect-chain.md"
    content = (
        f"### [{cid}] {title}\n"
        f"- **触发条件**：{trigger}\n"
        f"- **传播路径**：{propagation}\n"
        f"- **修复锚点**：{fix_anchor}\n"
        f"- **回归用例**：{regression_case}\n"
        f"- **证据**：{evidence}"
    )
    return _run_single_block(workspace_root, product, rel, block_id, content, summary=f"defect-chain:{cid}")


_FIVE_DIM_FILE_MAP = {
    "business-endpoints": "business/business-endpoints.md",
    "business-flows": "business/business-flows.md",
    "technical-endpoints": "architecture/technical-endpoints.md",
    "technical-architecture": "architecture/technical-architecture.md",
    "test-design": "utest-rules/07-test-design.md",
}


def append_five_dim(
    workspace_root: Path,
    product: str,
    *,
    dimension: str,
    section: str,
    item_id: str,
    name: str,
    summary: str,
    source: str,
    evidence: str,
) -> dict[str, Any]:
    subpath = _FIVE_DIM_FILE_MAP.get(dimension.strip())
    if not subpath:
        raise InputError(f"未知的五维知识维度: {dimension!r}，可选: {sorted(_FIVE_DIM_FILE_MAP)}")
    iid = (item_id or name or "item").strip().replace(" ", "_")
    block_id = f"five-dim:{dimension}:{iid}"
    rel = f".dsh/knowledge/products/{product}/{subpath}"
    content = (
        f"### [{iid}] {name} ({section})\n"
        f"- **摘要**：{summary}\n"
        f"- **来源**：{source}\n"
        f"- **证据**：{evidence}"
    )
    return _run_single_block(workspace_root, product, rel, block_id, content, summary=f"five-dim:{iid}")


def append_run_digest(
    workspace_root: Path,
    product: str,
    report_data: dict[str, Any],
) -> dict[str, Any]:
    run_id = str(report_data.get("run_key") or report_data.get("run_id") or "latest").strip()
    block_id = f"run-digest:{run_id}"
    rel = f".dsh/knowledge/products/{product}/utest-rules/06-run-history-digest.md"
    status = report_data.get("status", "unknown")
    content = f"- **轮次 `{run_id}`**：status=`{status}`"
    return _run_single_block(workspace_root, product, rel, block_id, content, summary=f"run-digest:{run_id}")


# ── CLI ─────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="sentinel W3：按 kb:auto 机器区块协议受控改写知识正文",
    )
    parser.add_argument("--workspace-root", required=True, help="UITA 工作区根（白名单相对此根）")
    parser.add_argument("--report-dir", required=True, help="本轮报告目录（备份与 kb-writeback.json 落此）")
    parser.add_argument("--run-key", required=True, help="执行轮次键（写入区块属性）")
    parser.add_argument("--product-code", required=True, help="产品编码（决定白名单路径）")
    parser.add_argument("--input", required=True, help="改写指令 JSON（blocks/summary/rules_version）")
    parser.add_argument("--dry-run", action="store_true", help="只检查不落盘（不写文件、不写报告）")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    root = Path(args.workspace_root)
    report_dir = Path(args.report_dir)
    if not root.exists():
        print(f"[kb-writeback] 工作区不存在：{root}", file=sys.stderr)
        return 2
    try:
        # 产品目录护栏**先于**白名单解析：传错产品名时给出「知识目录名 ≠ 外部编码」
        # 的明确指引，而不是让调用方去猜白名单里那串路径哪里不对。
        validate_product(root, args.product_code)
        payload = load_input(Path(args.input))
        blocks = build_blocks(payload, root=root, product=args.product_code)
    except InputError as exc:
        print(f"[kb-writeback] 输入错误：{exc}", file=sys.stderr)
        return 2

    try:
        report, code = run(
            workspace_root=root,
            report_dir=report_dir,
            run_key=args.run_key,
            product_code=args.product_code,
            blocks=blocks,
            rules_version=payload.get("rules_version"),
            summary=str(payload.get("summary") or ""),
            dry_run=bool(args.dry_run),
        )
    except InputError as exc:
        print(f"[kb-writeback] 输入错误：{exc}", file=sys.stderr)
        return 2
    totals = report["totals"]
    print(
        f"[kb-writeback] status={report['status']} files={totals['files_total']} "
        f"changed={totals['files_changed']} blocks_written={totals['blocks_written']} "
        f"unchanged={totals['blocks_unchanged']} conflicts={totals['conflicts']}"
        + ("（dry-run，未落盘）" if args.dry_run else ""),
    )
    for item in report["files"]:
        print(f"  {item['action']:<9} {item['path']}")
    for item in report["conflicts"]:
        print(f"  冲突保留：{item['id']} → {item['resolved_as']}")
    if report["out_of_scope_changes"]:
        for item in report["out_of_scope_changes"]:
            print(f"  越界：{item}", file=sys.stderr)
        print(f"[kb-writeback] STOP：{report.get('reason')}", file=sys.stderr)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
