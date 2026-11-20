#!/usr/bin/env python3
"""sentinel 知识库本地双质量飞轮：平级召回 / 缺口审计 / 死知识台账 / 图谱关系维护。

为什么需要这个脚本
------------------
知识图谱标准规范 侧知识库有「召回日志 → 采纳 → 缺口派生 → 金标评分」的观测口径，但三件事
没有对外写入口（已核代码事实）：

* **无 `kb_gap` 表**：缺口是 `kb_recall_log` 零命中行的只读派生指标；
* **无死知识字段 / 下线 API**：`kb_entity` / `kb_edge` 没有 `status` / `deprecated`；
* **无图谱写接口**：实体与边只由 ingest 抽取器产出。

所以「补知识 / 标记死知识 / 维护关系」必须在 **UITA 知识源侧**做，并保持三条硬纪律：

1. **机器只建议、不删除**：死知识机器只能写 `status=candidate` + `marked_by=agent_suggested`；
   `confirmed` 只接受业务侧输入（`--source business`），且机器永无删除动作。
2. **关系受白名单约束**：`edge_type` 必须落在 知识图谱标准规范 `DEFAULT_EDGE_WHITELIST`（20 个），
   uid 必须带合法命名空间；落库后是否真被抽取，如实记 `unverified`，不假设成功。
3. **知识写入仍只走 `ci/sentinel_kb_writeback.py`**（`kb:auto` 区块协议）；本脚本只做
   审计、校验与台账，**不写知识正文**。

子命令
------
* `audit`     ：读召回证据（`sentinel-kb-recall/v1`）→ 缺口清单 + 决策校验 + 五维覆盖矩阵；
* `dead`      ：读 / 写死知识台账（`sentinel-kb-dead/v1`），按 `--source` 限制权限；
* `relations` ：解析知识正文关系表 → 白名单 / 命名空间 / 端点存在性校验。

退出码：`0` 通过；`1` 守卫拦截（决策非法 / 死知识越权 / 关系非法）；`2` 参数或输入错误。
只用标准库（宪法 R13）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA_AUDIT = "sentinel-kb-audit/v1"
SCHEMA_DEAD = "sentinel-kb-dead/v1"
SCHEMA_RELATIONS = "sentinel-kb-relations/v1"
SCHEMA_RECALL = "sentinel-kb-recall/v1"
SCHEMA_LOCAL_RECALL = "sentinel-kb-local-recall/v1"

KNOWLEDGE_PREFIX = ".dsh/knowledge/products"

#: 知识库五维（与 `ci/sentinel_kb_golden.py::DIMENSIONS` 同源）。
DIMENSIONS: dict[str, dict[str, str]] = {
    "business_endpoint": {"file": "business/business-endpoints.md", "title": "业务端点"},
    "business_flow": {"file": "business/business-flows.md", "title": "业务流程"},
    "technical_endpoint": {"file": "architecture/technical-endpoints.md", "title": "技术端点"},
    "technical_arch": {"file": "architecture/technical-architecture.md", "title": "技术架构"},
    "test_design": {"file": "utest-rules/07-test-design.md", "title": "测试设计"},
}

#: 知识准度决策枚举（模型产出，脚本只校验与记账）。
DECISIONS: tuple[str, ...] = ("keep", "update", "add", "conflict", "deprecate_candidate")

#: 知识图谱标准规范 `app/services/kb/graph.py::DEFAULT_EDGE_WHITELIST`（20 个，逐字一致）。
EDGE_WHITELIST: tuple[str, ...] = (
    "part_of", "contains", "documents", "defined_in", "implements", "realized_by",
    "triggers", "resolves_by", "covered_by", "verified_by", "guarded_by",
    "regression_of", "caused_defect", "fixed_by", "reported_in", "related_to",
    "governed_by", "uses", "routes_to", "bound_to",
)

#: 合法实体 UID 命名空间。
UID_NAMESPACES: tuple[str, ...] = ("uita", "code", "issue", "pr", "git", "tcs", "utest")

#: 关系表表头别名（首行单元格小写匹配）。
_HEADER_SRC = frozenset({"src", "source", "起点", "主体", "源"})
_HEADER_DST = frozenset({"dst", "target", "终点", "客体", "目标"})
_HEADER_REL = frozenset({"relation", "edge", "关系", "边", "关系类型"})
_HEADER_EVIDENCE = frozenset({"evidence", "证据", "依据"})

_SKIP_ROW = frozenset({"", "—", "-", "todo", "tbd", "n/a", "na"})


class InputError(Exception):
    """参数或输入错误（退出码 2）。"""


class GuardViolation(Exception):
    """守卫拦截（退出码 1）。"""


# ── 基础工具 ────────────────────────────────────────────────────────────────

def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def read_json(path: Path) -> Any:
    text = read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def products_root(workspace_root: Path) -> Path:
    return workspace_root / KNOWLEDGE_PREFIX


def product_dir(workspace_root: Path, product: str) -> Path:
    return products_root(workspace_root) / product


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── Markdown 结构解析 ───────────────────────────────────────────────────────

def markdown_tables(text: str) -> list[tuple[str, list[list[str]]]]:
    """返回 `[(章节标题, 表格行列表)]`（分隔行跳过，单元格已去空白）。"""
    tables: list[tuple[str, list[list[str]]]] = []
    heading = ""
    rows: list[list[str]] = []
    in_table = False

    def flush() -> None:
        nonlocal rows
        if rows:
            tables.append((heading, rows))
        rows = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            in_table = False
            heading = stripped.lstrip("#").strip()
            continue
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if all(cell and set(cell) <= set("-: ") for cell in cells):
                continue
            rows.append(cells)
            in_table = True
            continue
        if in_table:
            flush()
            in_table = False
    flush()
    return tables


def _is_relation_header(row: Sequence[str]) -> bool:
    """表头同时含 src / relation / dst 三列别名 ⇒ 关系表（不计入五维事实行）。"""
    cells = {cell.replace("`", "").strip().lower() for cell in row}
    return bool(cells & _HEADER_SRC) and bool(cells & _HEADER_REL) and bool(cells & _HEADER_DST)


def fact_rows(text: str) -> list[tuple[str, list[str], str]]:
    """`[(事实名, 整行单元格, 章节标题)]`（第一列作事实名；跳过表头、占位行与关系表）。"""
    out: list[tuple[str, list[str], str]] = []
    for section, rows in markdown_tables(text):
        if not rows:
            continue
        if _is_relation_header(rows[0]):
            continue
        for row in rows[1:]:  # 首行是表头，不是事实
            name = (row[0] if row else "").replace("`", "").strip()
            if name.lower() in _SKIP_ROW or len(name) < 2:
                continue
            out.append((name, row, section))
    return out


# ── 召回证据 / 决策校验 ─────────────────────────────────────────────────────

def _uid_namespace_ok(uid: str) -> bool:
    parts = uid.split(":", 2)
    return len(parts) == 3 and all(part.strip() for part in parts) and parts[0] in UID_NAMESPACES


def audit_recall_evidence(
    evidence: Any, *, knowledge_dir: Path,
) -> dict[str, Any]:
    """校验召回证据 + 产出缺口 / 决策台账；只做 schema 校验与统计（零语义判定）。"""
    if not isinstance(evidence, dict):
        raise InputError("召回证据 JSON 顶层必须是对象")
    schema = evidence.get("schema")
    if schema is not None and schema != SCHEMA_RECALL:
        raise InputError(f"召回证据 schema 非法：{schema}（期望 {SCHEMA_RECALL}）")
    items = evidence.get("items")
    if not isinstance(items, list) or not items:
        raise InputError("召回证据缺非空 items 数组")

    problems: list[str] = []
    gaps: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    decisions: Counter[str] = Counter()
    recall_ids: list[int] = []

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"items[{index}] 不是对象")
            continue
        recall_id = item.get("recall_id")
        if not isinstance(recall_id, int) or recall_id < 1:
            problems.append(f"items[{index}] 的 recall_id 必须是 ≥1 的整数")
        else:
            recall_ids.append(recall_id)
        query = str(item.get("query") or "").strip()
        if not query:
            problems.append(f"items[{index}] 缺 query")
        step = item.get("step")
        if step not in (3, 6, 8):
            problems.append(f"items[{index}] 的 step 必须是 3 / 6 / 8（实际 {step!r}）")
        dimension = item.get("dimension")
        if dimension is not None and dimension not in DIMENSIONS:
            problems.append(f"items[{index}] 的 dimension 非法：{dimension}")
        hit_count = item.get("hit_count")
        if not isinstance(hit_count, int) or hit_count < 0:
            problems.append(f"items[{index}] 的 hit_count 必须是 ≥0 的整数")
            hit_count = 0
        hits = item.get("hits") or []
        if not isinstance(hits, list):
            problems.append(f"items[{index}] 的 hits 必须是数组")
            hits = []
        adopted = item.get("adopted_uids") or []
        if not isinstance(adopted, list):
            problems.append(f"items[{index}] 的 adopted_uids 必须是数组")
            adopted = []
        for uid in adopted:
            if not _uid_namespace_ok(str(uid)):
                problems.append(f"items[{index}] 的 adopted_uid 命名空间非法：{uid}")
        decision = item.get("decision")
        if decision not in DECISIONS:
            problems.append(
                f"items[{index}] 的 decision 非法：{decision!r}（允许：{'/'.join(DECISIONS)}）",
            )
            decision = None
        else:
            decisions[str(decision)] += 1
        evidence_note = str(item.get("decision_evidence") or "").strip()
        if decision in ("update", "conflict", "deprecate_candidate") and not evidence_note:
            problems.append(f"items[{index}] 的 decision={decision} 缺 decision_evidence")
        target = str(item.get("target") or "").strip()
        if decision in ("update", "conflict", "deprecate_candidate") and not target:
            problems.append(f"items[{index}] 的 decision={decision} 缺 target（知识位置）")
        if decision == "add" and dimension is None:
            problems.append(f"items[{index}] 的 decision=add 缺 dimension（补哪一维）")

        if hit_count == 0:
            gaps.append(
                {
                    "kind": "no_hit",
                    "recall_id": recall_id,
                    "query": query,
                    "step": step,
                    "dimension": dimension,
                    "reason": str(item.get("note") or "召回零命中"),
                }
            )
        elif decision in ("add", "deprecate_candidate") and not adopted:
            gaps.append(
                {
                    "kind": "not_useful",
                    "recall_id": recall_id,
                    "query": query,
                    "step": step,
                    "dimension": dimension,
                    "reason": str(item.get("note") or "命中但不可用（知识在库里但不顶用）"),
                }
            )
        if decision in ("add", "update", "conflict", "deprecate_candidate"):
            actions.append(
                {
                    "decision": decision,
                    "dimension": dimension,
                    "target": target or None,
                    "recall_id": recall_id,
                    "conflict": decision == "conflict",
                }
            )

    duplicated = sorted({item for item in recall_ids if recall_ids.count(item) > 1})
    if duplicated:
        problems.append(f"recall_id 重复（同一召回被多条证据引用）：{duplicated}")

    coverage: dict[str, Any] = {}
    for dimension, spec in DIMENSIONS.items():
        path = knowledge_dir / spec["file"]
        text = read_text(path)
        facts = fact_rows(text) if text else []
        coverage[dimension] = {
            "file": f"{KNOWLEDGE_PREFIX}/{knowledge_dir.name}/{spec['file']}",
            "exists": text is not None,
            "fact_rows": len(facts),
            "filled": bool(facts),
        }
        if text is None:
            gaps.append(
                {
                    "kind": "missing_dimension",
                    "dimension": dimension,
                    "reason": f"知识文件缺失：{spec['file']}",
                }
            )
        elif not facts:
            gaps.append(
                {
                    "kind": "empty_dimension",
                    "dimension": dimension,
                    "reason": f"知识文件无有效事实行：{spec['file']}",
                }
            )

    return {
        "schema": SCHEMA_AUDIT,
        "generated_at": now_iso(),
        "product": knowledge_dir.name,
        "run_key": evidence.get("run_key"),
        "coverage": coverage,
        "gaps": gaps,
        "gap_counts": dict(Counter(item["kind"] for item in gaps)),
        "decisions": dict(decisions),
        "machine_actions": actions,
        "problems": problems,
        "ok": not problems,
    }


# ── 死知识台账 ─────────────────────────────────────────────────────────────

def dead_ledger_path(workspace_root: Path, product: str) -> Path:
    return product_dir(workspace_root, product) / "kb-flywheel" / "dead-knowledge.json"


def load_dead_ledger(path: Path, *, product: str) -> dict[str, Any]:
    payload = read_json(path) if path.is_file() else None
    if payload is None:
        return {
            "schema": SCHEMA_DEAD,
            "product": product,
            "updated_at": now_iso(),
            "items": [],
        }
    if not isinstance(payload, dict):
        raise InputError(f"死知识台账顶层必须是对象：{path}")
    if payload.get("schema") != SCHEMA_DEAD:
        raise InputError(f"死知识台账 schema 非法：{payload.get('schema')!r}")
    if not isinstance(payload.get("items", []), list):
        raise InputError("死知识台账 items 必须是数组")
    return payload


def record_dead(
    ledger: dict[str, Any], record: Any, *, source: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """登记一条死知识；机器（`source=agent`）只能写候选，业务可写确认。"""
    if not isinstance(record, dict):
        raise InputError("--record 必须是 JSON 对象")
    target = str(record.get("target") or "").strip()
    reason = str(record.get("reason") or "").strip()
    if not target or not reason:
        raise InputError("死知识记录缺 target / reason")
    status = str(record.get("status") or "candidate").strip()
    if status not in ("candidate", "confirmed"):
        raise InputError(f"死知识 status 非法：{status}（candidate / confirmed）")
    if source == "agent":
        if status == "confirmed":
            raise GuardViolation("机器侧不得把死知识标为 confirmed（需业务确认）")
        marked_by = "agent_suggested"
    else:
        marked_by = str(record.get("marked_by") or "business").strip() or "business"

    items = ledger.setdefault("items", [])
    existing = next((item for item in items if item.get("target") == target), None)
    entry = {
        "target": target,
        "reason": reason,
        "marked_by": marked_by,
        "status": status,
        "marked_at": str(record.get("marked_at") or now_iso()),
        "evidence": str(record.get("evidence") or ""),
    }
    if existing is None:
        items.append(entry)
        action = "recorded"
    else:
        if existing.get("status") == "confirmed" and status == "candidate":
            raise GuardViolation(
                f"已由业务确认的死知识不得被机器降级为 candidate：{target}",
            )
        before = dict(existing)
        existing.update(entry)
        if existing.get("status") == status and before.get("reason") == reason and before.get("marked_by") == marked_by:
            action = "unchanged"
        else:
            action = "updated"
    ledger["updated_at"] = now_iso()
    return ledger, {"action": action, "entry": entry, "source": source}


# ── 图谱关系维护 ───────────────────────────────────────────────────────────

def relation_rows(text: str) -> list[dict[str, str]]:
    """从知识正文的关系表抽行（表头须同时含 src / relation / dst 三列别名）。"""
    rows: list[dict[str, str]] = []
    for section, table in markdown_tables(text):
        if not table:
            continue
        header = [cell.replace("`", "").strip().lower() for cell in table[0]]
        idx_src = next((i for i, cell in enumerate(header) if cell in _HEADER_SRC), None)
        idx_dst = next((i for i, cell in enumerate(header) if cell in _HEADER_DST), None)
        idx_rel = next((i for i, cell in enumerate(header) if cell in _HEADER_REL), None)
        if idx_src is None or idx_dst is None or idx_rel is None:
            continue
        idx_ev = next((i for i, cell in enumerate(header) if cell in _HEADER_EVIDENCE), None)
        for row in table[1:]:
            def cell(index: int | None) -> str:
                if index is None or index >= len(row):
                    return ""
                return row[index].replace("`", "").strip()

            src, dst, relation = cell(idx_src), cell(idx_dst), cell(idx_rel)
            if not src or not dst or not relation:
                continue
            if src.lower() in _SKIP_ROW or dst.lower() in _SKIP_ROW:
                continue
            rows.append(
                {
                    "src": src,
                    "dst": dst,
                    "relation": relation,
                    "evidence": cell(idx_ev),
                    "section": section,
                }
            )
    return rows


def _code_endpoint_status(uid: str, workspace_root: Path) -> tuple[str, str]:
    """`code:` 端点的存在性：返回 (`ok|unverified`, 原因)。"""
    key = uid.split(":", 2)[2]
    if "::" in key:
        path_part, symbol = key.split("::", 1)
    else:
        path_part, symbol = key, ""
    target = workspace_root / path_part
    if not target.is_file():
        return "unverified", f"文件不存在：{path_part}"
    if not symbol:
        return "ok", ""
    text = read_text(target) or ""
    if symbol not in text:
        return "unverified", f"符号未在文件中出现：{path_part}::{symbol}"
    return "ok", ""


def audit_relations(
    workspace_root: Path, product: str, *, files: Sequence[str] | None = None,
) -> dict[str, Any]:
    base = product_dir(workspace_root, product)
    scan: list[str] = list(files) if files else [
        spec["file"] for spec in DIMENSIONS.values()
    ] + ["business/business-rules.md", "business/feature-matrix.md", "architecture/README.md"]
    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    scanned: list[str] = []
    seen: set[tuple[str, str, str]] = set()

    for file_rel in scan:
        text = read_text(base / file_rel)
        if text is None:
            continue
        scanned.append(f"{KNOWLEDGE_PREFIX}/{product}/{file_rel}")
        for row in relation_rows(text):
            entry = {
                "src": row["src"],
                "dst": row["dst"],
                "relation": row["relation"],
                "evidence": row["evidence"],
                "source": f"{KNOWLEDGE_PREFIX}/{product}/{file_rel}#{row['section']}",
            }
            if row["relation"] not in EDGE_WHITELIST:
                entry["problem"] = f"relation 不在 知识图谱标准白名单：{row['relation']}"
                invalid.append(entry)
                continue
            bad_ns = [uid for uid in (row["src"], row["dst"]) if not _uid_namespace_ok(uid)]
            if bad_ns:
                entry["problem"] = f"uid 命名空间非法（允许 {'/'.join(UID_NAMESPACES)}）：{bad_ns}"
                invalid.append(entry)
                continue
            if row["src"] == row["dst"]:
                entry["problem"] = "自环关系（src == dst）"
                invalid.append(entry)
                continue
            key = (row["src"], row["dst"], row["relation"])
            if key in seen:
                entry["problem"] = "重复关系"
                invalid.append(entry)
                continue
            seen.add(key)
            reasons: list[str] = []
            for uid in (row["src"], row["dst"]):
                if uid.startswith("code:"):
                    status, reason = _code_endpoint_status(uid, workspace_root)
                    if status != "ok":
                        reasons.append(reason)
            if reasons:
                entry["problem"] = "；".join(reasons)
                unverified.append(entry)
            else:
                valid.append(entry)

    return {
        "schema": SCHEMA_RELATIONS,
        "generated_at": now_iso(),
        "product": product,
        "files_scanned": scanned,
        "edge_whitelist_size": len(EDGE_WHITELIST),
        "valid": valid,
        "invalid": invalid,
        "unverified": unverified,
        "totals": {
            "valid": len(valid),
            "invalid": len(invalid),
            "unverified": len(unverified),
        },
        "ok": not invalid,
    }


def recall_knowledge(
    workspace_root: Path,
    product: str,
    query: str,
    *,
    dimension: str | None = None,
    top_k: int = 8,
    hop_depth: int = 1,
) -> dict[str, Any]:
    """本地知识图谱平级召回：扫描 `.dsh/knowledge/products/<product>/` 五维与规则文档，
    按词面 + 章节/表格事实打分，并沿关系表做 1~2 跳图扩展。
    """
    base = _ensure_product(workspace_root, product)
    clean_query = (query or "").strip()
    if not clean_query:
        raise InputError("query 不能为空")
    if dimension and dimension not in DIMENSIONS:
        raise InputError(f"未知 dimension：{dimension}（允许 {tuple(DIMENSIONS)}）")

    raw_tokens = [t.lower() for t in re.split(r"[\s,，。；;、/|:：()（）\[\]【】\"'`]+", clean_query) if t.strip()]
    # 追加 2-gram 中文子串提升召回率
    tokens: list[str] = list(dict.fromkeys(raw_tokens))
    for tok in raw_tokens:
        if len(tok) >= 4 and any("\u4e00" <= ch <= "\u9fff" for ch in tok):
            for idx in range(len(tok) - 1):
                bi = tok[idx : idx + 2]
                if bi not in tokens:
                    tokens.append(bi)

    # 收集关系图用于 hop 扩展
    rel_audit = audit_relations(workspace_root, product)
    edges = rel_audit.get("valid", []) + rel_audit.get("unverified", [])

    candidates: list[dict[str, Any]] = []
    scan_targets: list[tuple[str, str]] = []
    for dim_key, spec in DIMENSIONS.items():
        if dimension and dim_key != dimension:
            continue
        scan_targets.append((dim_key, spec["file"]))
    if not dimension:
        scan_targets.extend([
            ("pitfalls", "utest-rules/04-known-pitfalls.md"),
            ("defect_chain", f"defects/{product}-defect-chain.md"),
            ("history_digest", "utest-rules/06-run-history-digest.md"),
        ])

    for dim_key, file_rel in scan_targets:
        fpath = base / file_rel
        text = read_text(fpath)
        if not text:
            continue
        doc_rel_path = f"{KNOWLEDGE_PREFIX}/{product}/{file_rel}"
        doc_uid_str = f"uita:doc:{doc_rel_path}"

        # 按二级/三级章节切分切片
        sections: list[tuple[str, str]] = []
        cur_head = "概览"
        cur_lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("#"):
                if cur_lines:
                    sections.append((cur_head, "\n".join(cur_lines).strip()))
                    cur_lines = []
                cur_head = line.lstrip("#").strip() or cur_head
            else:
                cur_lines.append(line)
        if cur_lines:
            sections.append((cur_head, "\n".join(cur_lines).strip()))

        for head, body in sections:
            haystack = f"{head}\n{body}".lower()
            matched = [tok for tok in tokens if tok in haystack]
            if not matched and clean_query.lower() not in haystack:
                continue
            score = sum(3.0 if len(tok) >= 3 else 1.0 for tok in matched)
            if clean_query.lower() in haystack:
                score += 5.0
            # 提取关联邻居边
            neighbors: list[dict[str, str]] = []
            if hop_depth >= 1:
                for edge in edges:
                    if (
                        edge["src"] == doc_uid_str
                        or edge["dst"] == doc_uid_str
                        or any(tok in edge["src"].lower() or tok in edge["dst"].lower() for tok in matched)
                    ):
                        neighbors.append({
                            "src": edge["src"],
                            "relation": edge["relation"],
                            "dst": edge["dst"],
                            "evidence": edge.get("evidence", ""),
                        })
            snippet = body[:400].strip()
            candidates.append({
                "uid": f"{doc_uid_str}#{head}",
                "doc_uid": doc_uid_str,
                "dimension": dim_key,
                "file": doc_rel_path,
                "heading": head,
                "score": round(score, 2),
                "matched_terms": matched[:10],
                "snippet": snippet,
                "neighbors": neighbors[:8],
            })

    candidates.sort(key=lambda item: (-item["score"], item["file"], item["heading"]))
    hits = candidates[: max(1, int(top_k))]
    return {
        "schema": SCHEMA_LOCAL_RECALL,
        "generated_at": now_iso(),
        "product": product,
        "query": clean_query,
        "dimension": dimension or "all",
        "hit_count": len(hits),
        "hits": hits,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────

def _resolve_root(value: str | None) -> Path:
    return Path(value) if value else Path(__file__).resolve().parents[1]


def _ensure_product(workspace_root: Path, product: str) -> Path:
    base = product_dir(workspace_root, product)
    if not base.is_dir():
        available = (
            sorted(item.name for item in products_root(workspace_root).iterdir() if item.is_dir())
            if products_root(workspace_root).is_dir()
            else []
        )
        raise InputError(
            f"产品知识目录不存在：{base}\n"
            f"  这里要传**知识目录名**（如 `sample_web`）。\n"
            f"  现有目录：{available}",
        )
    return base


def _cmd_audit(args: argparse.Namespace) -> int:
    workspace_root = _resolve_root(args.workspace_root)
    evidence_path = Path(args.recall_evidence)
    evidence = read_json(evidence_path)
    if evidence is None:
        print(f"[kb-flywheel] 召回证据不存在或不是合法 JSON：{evidence_path}", file=sys.stderr)
        return 2
    try:
        base = _ensure_product(workspace_root, args.product_code)
        report = audit_recall_evidence(evidence, knowledge_dir=base)
    except (InputError, GuardViolation) as exc:
        print(f"[kb-flywheel] {exc}", file=sys.stderr)
        return 2 if isinstance(exc, InputError) else 1
    out = Path(args.out) if args.out else evidence_path.with_suffix(".audit.json")
    write_json(out, report)
    print(
        f"[kb-flywheel] audit {'OK' if report['ok'] else 'FAIL'} "
        f"gaps={len(report['gaps'])} decisions={report['decisions']} -> {out}",
    )
    return 0 if report["ok"] else 1


def _cmd_dead(args: argparse.Namespace) -> int:
    workspace_root = _resolve_root(args.workspace_root)
    try:
        _ensure_product(workspace_root, args.product_code)
    except InputError as exc:
        print(f"[kb-flywheel] {exc}", file=sys.stderr)
        return 2
    ledger_path = Path(args.ledger) if args.ledger else dead_ledger_path(
        workspace_root, args.product_code,
    )
    try:
        ledger = load_dead_ledger(ledger_path, product=args.product_code)
    except InputError as exc:
        print(f"[kb-flywheel] {exc}", file=sys.stderr)
        return 2
    if not args.record:
        write_json(ledger_path, ledger)
        confirmed = sum(1 for item in ledger["items"] if item.get("status") == "confirmed")
        print(
            f"[kb-flywheel] dead ledger items={len(ledger['items'])} confirmed={confirmed} "
            f"-> {ledger_path}",
        )
        return 0
    try:
        record = json.loads(args.record)
    except json.JSONDecodeError as exc:
        print(f"[kb-flywheel] --record 不是合法 JSON：{exc}", file=sys.stderr)
        return 2
    try:
        ledger, result = record_dead(ledger, record, source=args.source)
    except InputError as exc:
        print(f"[kb-flywheel] {exc}", file=sys.stderr)
        return 2
    except GuardViolation as exc:
        print(f"[kb-flywheel] STOP {exc}", file=sys.stderr)
        return 1
    write_json(ledger_path, ledger)
    print(
        f"[kb-flywheel] dead {result['action']} target={result['entry']['target']} "
        f"status={result['entry']['status']} source={args.source} -> {ledger_path}",
    )
    return 0


def _cmd_relations(args: argparse.Namespace) -> int:
    workspace_root = _resolve_root(args.workspace_root)
    try:
        _ensure_product(workspace_root, args.product_code)
    except InputError as exc:
        print(f"[kb-flywheel] {exc}", file=sys.stderr)
        return 2
    files = list(args.file) if args.file else None
    report = audit_relations(workspace_root, args.product_code, files=files)
    out = Path(args.out) if args.out else (
        product_dir(workspace_root, args.product_code) / "kb-flywheel" / "relations.json"
    )
    write_json(out, report)
    print(
        f"[kb-flywheel] relations {'OK' if report['ok'] else 'FAIL'} "
        f"valid={report['totals']['valid']} invalid={report['totals']['invalid']} "
        f"unverified={report['totals']['unverified']} -> {out}",
    )
    return 0 if report["ok"] else 1


def _cmd_recall(args: argparse.Namespace) -> int:
    workspace_root = _resolve_root(args.workspace_root)
    try:
        report = recall_knowledge(
            workspace_root,
            args.product_code,
            args.query,
            dimension=args.dimension or None,
            top_k=args.top_k,
            hop_depth=args.hop_depth,
        )
    except InputError as exc:
        print(f"[kb-flywheel] {exc}", file=sys.stderr)
        return 2
    if args.out:
        write_json(Path(args.out), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="sentinel 知识库本地双飞轮：平级召回 / 缺口审计 / 死知识台账 / 关系校验",
    )
    parser.add_argument("--workspace-root", default=None, help="UITA 工作区根（缺省按脚本位置推断）")
    parser.add_argument("--product-code", required=True, help="知识目录名（如 sample_web）")
    sub = parser.add_subparsers(dest="command", required=True)

    recall = sub.add_parser("recall", help="本地知识库平级召回（词面 + 五维章节 + 关系图谱扩展）")
    recall.add_argument("--query", required=True, help="检索问题或关键词")
    recall.add_argument("--dimension", default="", help="可选限定五维之一（business_endpoint 等）")
    recall.add_argument("--top-k", type=int, default=8, help="返回条数上限（默认 8）")
    recall.add_argument("--hop-depth", type=int, default=1, help="关系图谱扩展跳数（0/1/2，默认 1）")
    recall.add_argument("--out", default=None, help="可选：将召回结果写入 JSON 文件")
    recall.set_defaults(func=_cmd_recall)

    audit = sub.add_parser("audit", help="缺口审计 + 决策校验")
    audit.add_argument("--recall-evidence", required=True, help="召回证据 JSON（sentinel-kb-recall/v1）")
    audit.add_argument("--out", default=None, help="审计报告输出（缺省=<证据>.audit.json）")
    audit.set_defaults(func=_cmd_audit)

    dead = sub.add_parser("dead", help="死知识台账（机器只能写候选）")
    dead.add_argument("--ledger", default=None, help="台账路径（缺省=<product>/kb-flywheel/dead-knowledge.json）")
    dead.add_argument("--record", default=None, help="待登记条目 JSON（target/reason/status/evidence）")
    dead.add_argument("--source", choices=("agent", "business"), default="agent", help="登记来源")
    dead.set_defaults(func=_cmd_dead)

    relations = sub.add_parser("relations", help="关系表白名单 / 命名空间 / 端点存在性校验")
    relations.add_argument("--out", default=None, help="报告输出路径")
    relations.add_argument("--file", action="append", default=None, help="只扫指定文件（可重复）")
    relations.set_defaults(func=_cmd_relations)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    workspace_root = _resolve_root(args.workspace_root)
    if not workspace_root.is_dir():
        print(f"[kb-flywheel] 工作区不存在：{workspace_root}", file=sys.stderr)
        return 2
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
