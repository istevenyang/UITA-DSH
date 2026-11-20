#!/usr/bin/env python3
"""sentinel 金标回放库：从产品事实**确定性**生成兼容的金标语料。

为什么需要这个脚本
------------------
评测规范 侧已有金标回放机制（`kb_eval_golden` 语料 + `kb-eval` 离线回放 +
`GoldenQuery` 口径），但语料只覆盖「页面 / 导航 / 定位策略 / 动作实现 / 缺陷链 / 跨子图」。
知识库五维（业务端点 / 业务流程 / 技术端点 / 技术架构 / 测试设计）没有金标，于是：

* 这五维**缺知识也测不出来**（没有期望值就没有 Recall/MRR 可算）；
* 知识「还在不在、准不准」无法用一轮评分回答。

本脚本把「知识正文里已经写明的事实」翻译成金标条目（问句 + 期望 uid/字面量），
产物可被 评测规范 直接消费：

    <评测规范>/app/cli.py kb-eval --golden <本脚本产物> --no-save

契约（与 评测规范 `app/services/kb/eval.py::GoldenQuery` 逐字段一致）
--------------------------------------------------------------------
* 条目字段集合固定 8 个：`qid / query / category / expect_uids / expect_terms /
  product_code / provenance / note`；`expect_uids` 与 `expect_terms` 至少一项非空。
* `category` **只用 评测规范 既有 6 类**（不新增类别，避免 UI / 统计口径漂移）：
  `page_elements` / `page_navigation` / `routing_strategy` / `action_source` /
  `defect_chain` / `cross_subgraph`。五维归属写进 `note`（`dim=<维度>`）。
* 文件形态 `{_meta:{version, generated_at, generated_from, count, categories,
  dimensions}, queries:[...]}`。

期望值口径（保守，不猜）
------------------------
* Markdown 知识：`expect_uids = ("uita:doc:<仓库相对路径>",)`（`extract_markdown`
  的 doc uid 就是该路径），`expect_terms` 取表内第一列名称（`eval.is_relevant`
  第 4 条按字面量判相关）。
* JSON 知识（world_model / routing_strategies / action-semantics）：只给
  `expect_terms`（页面名 / 元素名 / 通道名等正文中必然出现的字面量），**不猜**实体 uid。
* 缺陷链：行内同时出现缺陷单号与 `<路径>::<符号>` 时，路径带目录才给
  `code:symbol:` uid（裸文件名无法定位实现端点，只作字面量）。

模式
----
* `build`  ：从零生成（覆盖 `--out`）。
* `merge`  ：保留 `--base` 既有条目，只追加 `qid` 不存在的新条目（幂等）。
* `check`  ：对 `--base`（缺省 `--out`）做离线自检 + 五维覆盖统计（**离线金标回放
  自检**；真回放由 评测规范 `kb-eval` 或 CI 定时任务执行，不在本脚本内）。

退出码：`0` 成功；`1` 自检发现阻断问题（`check` 专用）；`2` 参数 / 输入错误。
只用标准库（宪法 R13）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

SCHEMA = "sentinel-kb-golden/v1"
KNOWLEDGE_PREFIX = ".dsh/knowledge/products"

CATEGORY_PAGE_ELEMENTS = "page_elements"
CATEGORY_PAGE_NAVIGATION = "page_navigation"
CATEGORY_ROUTING_STRATEGY = "routing_strategy"
CATEGORY_ACTION_SOURCE = "action_source"
CATEGORY_DEFECT_CHAIN = "defect_chain"
CATEGORY_CROSS_SUBGRAPH = "cross_subgraph"

#: 评测规范 既有 6 类（**不得新增**）。
CATEGORIES: tuple[str, ...] = (
    CATEGORY_PAGE_ELEMENTS,
    CATEGORY_PAGE_NAVIGATION,
    CATEGORY_ROUTING_STRATEGY,
    CATEGORY_ACTION_SOURCE,
    CATEGORY_DEFECT_CHAIN,
    CATEGORY_CROSS_SUBGRAPH,
)

#: 知识库五维 → (知识文件相对产品目录, 归属 category, 展示名, 问句模板)。
DIMENSIONS: dict[str, dict[str, str]] = {
    "business_endpoint": {
        "file": "business/business-endpoints.md",
        "category": CATEGORY_CROSS_SUBGRAPH,
        "title": "业务端点",
        "query": "业务端点「{name}」的用户入口、后端模块与可观察结果是什么",
    },
    "business_flow": {
        "file": "business/business-flows.md",
        "category": CATEGORY_PAGE_NAVIGATION,
        "title": "业务流程",
        "query": "业务流程「{name}」的步骤链、分支与失败路径是什么",
    },
    "technical_endpoint": {
        "file": "architecture/technical-endpoints.md",
        "category": CATEGORY_ACTION_SOURCE,
        "title": "技术端点",
        "query": "技术端点「{name}」的定义位置、出入参与错误码是什么",
    },
    "technical_arch": {
        "file": "architecture/technical-architecture.md",
        "category": CATEGORY_CROSS_SUBGRAPH,
        "title": "技术架构",
        "query": "模块「{name}」的职责、依赖方向与进程模型是什么",
    },
    "test_design": {
        "file": "utest-rules/07-test-design.md",
        "category": CATEGORY_DEFECT_CHAIN,
        "title": "测试设计",
        "query": "风险形态「{name}」用哪种手段验证、断言契约与 mock 边界是什么",
    },
}

#: 表头 / 占位单元格（不是事实名，不能当期望字面量）。
_SKIP_CELLS = frozenset(
    {
        "端点", "名称", "模块", "流程", "风险形态", "风险", "src", "source", "项目",
        "接口", "文件", "步骤", "—", "-", "n/a", "na", "todo", "tbd", "说明",
    }
)

_QID_SAFE_RE = re.compile(r"[^a-z0-9_]+")
#: 缺陷管理平台单号（要求 ≥9 位数字以避开日期 / 行号误命中）。
_ISSUE_RE = re.compile(r"\b(\d{9,20})\b")
_SYMBOL_RE = re.compile(r"([A-Za-z0-9_./\-]+\.(?:ts|js|mjs|cjs|tsx|jsx|py|go|rs|java|kt))::([A-Za-z_$][\w$]*)")
_SYMBOL_CELL_RE = re.compile(r"`?([A-Za-z0-9_./\-]+\.(?:ts|js|mjs|cjs|tsx|jsx|py|go|rs|java|kt))::([A-Za-z_$][\w$]*)`?")

MAX_QUERY_CHARS = 500


class InputError(Exception):
    """参数或输入错误（退出码 2）。"""


# ── 基础工具 ────────────────────────────────────────────────────────────────

def repo_rel(*parts: str) -> str:
    return "/".join(part.strip("/").replace("\\", "/") for part in parts if part)


def doc_rel(product_short: str, file_rel: str) -> str:
    return repo_rel(KNOWLEDGE_PREFIX, product_short, file_rel)


def doc_uid(product_short: str, file_rel: str) -> str:
    """`uita:doc:<仓库相对路径>`（口径同 评测规范 `uid.doc_uid`）。"""
    return f"uita:doc:{doc_rel(product_short, file_rel)}"


def symbol_uid(path: str, symbol: str) -> str:
    """`code:symbol:<路径>::<符号>`（口径同 评测规范 `uid.symbol_uid`）。"""
    return f"code:symbol:{path.replace(chr(92), '/')}::{symbol}"


def file_uid(path: str) -> str:
    return f"code:file:{path.replace(chr(92), '/')}"


def stable_qid(product_short: str, category: str, *seeds: str) -> str:
    """稳定 qid：同输入必同 id（跨轮可比、可增量合并）。"""
    digest = hashlib.sha1("|".join(seeds).encode("utf-8")).hexdigest()[:8]
    return f"{product_short}-{category}-{digest}"


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


def normalize_name(name: str) -> str:
    cleaned = name.replace("**", "").replace("`", "").strip()
    return re.sub(r"\s+", " ", cleaned)


def _is_fact_cell(cell: str) -> bool:
    lowered = cell.strip().lower()
    if not lowered or lowered in _SKIP_CELLS:
        return False
    if set(lowered) <= set("-: "):
        return False
    if lowered.startswith("（") or lowered.startswith("("):
        return False
    return len(normalize_name(cell)) >= 2


# ── Markdown 结构解析（确定性，零语义判定） ──────────────────────────────────

def markdown_tables(text: str) -> list[tuple[str, list[list[str]]]]:
    """返回 `[(章节标题, 表格行列表)]`；行内单元格已去空白，分隔行已跳过。"""
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
            if all(set(cell) <= set("-: ") and cell for cell in cells):
                continue  # 分隔行
            rows.append(cells)
            in_table = True
            continue
        if in_table:
            flush()
            in_table = False
    flush()

    return [(section, rows) for section, rows in tables if rows]


def dimension_facts(text: str) -> list[tuple[str, str]]:
    """五维知识文件 → `[(事实名, 章节标题)]`（第一列事实名，跳过表头，去重保序）。"""
    facts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for section, rows in markdown_tables(text):
        for row in rows[1:]:  # 首行是表头，不是事实
            name = normalize_name(row[0]) if row else ""
            if not name or name in seen or not _is_fact_cell(row[0]):
                continue
            seen.add(name)
            facts.append((name, section))
    return facts


# ── 各来源生成 ──────────────────────────────────────────────────────────────

def golden_from_dimension(
    product_short: str,
    product_code: str | None,
    dimension: str,
    text: str,
    *,
    cap: int,
) -> list[dict[str, Any]]:
    spec = DIMENSIONS[dimension]
    rel = spec["file"]
    rows: list[dict[str, Any]] = []
    for name, section in dimension_facts(text):
        uid = doc_uid(product_short, rel)
        query = spec["query"].format(name=name)
        if len(query) > MAX_QUERY_CHARS:
            continue
        rows.append(
            {
                "qid": stable_qid(product_short, spec["category"], uid, name),
                "query": query,
                "category": spec["category"],
                "expect_uids": [uid],
                "expect_terms": [name],
                "product_code": product_code,
                "provenance": f"{doc_rel(product_short, rel)}#{section or dimension}",
                "note": f"dim={dimension}（{spec['title']}）；知识正文第一列事实名",
            }
        )
    return sorted(rows, key=lambda item: item["qid"])[:cap]


def golden_from_world_model(
    product_short: str, product_code: str | None, payload: Any, *, cap: int,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rel = "orchestrator/world_model.json"
    pages = payload.get("pages")
    if not isinstance(pages, list):
        return []
    names = {
        str(page.get("id")): str(page.get("name") or page.get("id"))
        for page in pages
        if isinstance(page, dict) and page.get("id")
    }
    rows_by_category: dict[str, list[dict[str, Any]]] = {
        CATEGORY_PAGE_ELEMENTS: [],
        CATEGORY_PAGE_NAVIGATION: [],
    }
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_id = str(page.get("id") or "")
        page_name = names.get(page_id)
        if not page_name:
            continue
        keys = [normalize_name(str(item)) for item in page.get("key_elements") or ()]
        keys = [item for item in keys if _is_fact_cell(item)]
        if keys:
            rows_by_category[CATEGORY_PAGE_ELEMENTS].append(
                {
                    "qid": stable_qid(product_short, CATEGORY_PAGE_ELEMENTS, page_id, *keys[:3]),
                    "query": f"「{page_name}」页面的关键元素有哪些",
                    "category": CATEGORY_PAGE_ELEMENTS,
                    "expect_uids": [],
                    "expect_terms": [page_name, *keys[:5]],
                    "product_code": product_code,
                    "provenance": f"{doc_rel(product_short, rel)}#pages/{page_id}",
                    "note": "world_model.key_elements",
                }
            )
        targets: list[str] = []
        for child in page.get("children") or ():
            if str(child) in names:
                targets.append(str(child))
        parent = page.get("parent")
        if isinstance(parent, str) and parent in names:
            targets.append(parent)
        for target in targets:
            rows_by_category[CATEGORY_PAGE_NAVIGATION].append(
                {
                    "qid": stable_qid(
                        product_short, CATEGORY_PAGE_NAVIGATION, page_id, target,
                    ),
                    "query": f"从「{page_name}」可以进入「{names[target]}」吗，路径关系是什么",
                    "category": CATEGORY_PAGE_NAVIGATION,
                    "expect_uids": [],
                    "expect_terms": [page_name, names[target]],
                    "product_code": product_code,
                    "provenance": f"{doc_rel(product_short, rel)}#pages/{page_id}",
                    "note": "world_model.children/parent",
                }
            )
    out: list[dict[str, Any]] = []
    for category, rows in rows_by_category.items():
        out.extend(sorted(rows, key=lambda item: item["qid"])[:cap])
    return out


def golden_from_routing(
    product_short: str, product_code: str | None, payload: Any, *, cap: int,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    rel = "orchestrator/routing_strategies.json"
    rows: list[dict[str, Any]] = []
    default = payload.get("default")
    if isinstance(default, dict):
        chain = [str(item) for item in default.get("chain") or () if str(item).strip()]
        if chain:
            rows.append(
                {
                    "qid": stable_qid(product_short, CATEGORY_ROUTING_STRATEGY, "default", *chain[:3]),
                    "query": "元素定位的默认策略链与降级顺序是什么",
                    "category": CATEGORY_ROUTING_STRATEGY,
                    "expect_uids": [],
                    "expect_terms": chain[:5],
                    "product_code": product_code,
                    "provenance": f"{doc_rel(product_short, rel)}#default",
                    "note": "routing_strategies.default.chain",
                }
            )
    overrides = payload.get("overrides")
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            name = normalize_name(str(key))
            if not _is_fact_cell(name) or not isinstance(value, dict):
                continue
            chain = [str(item) for item in value.get("chain") or () if str(item).strip()]
            if not chain:
                continue
            rows.append(
                {
                    "qid": stable_qid(product_short, CATEGORY_ROUTING_STRATEGY, name, *chain[:3]),
                    "query": f"元素「{name}」的定位策略与降级链是什么",
                    "category": CATEGORY_ROUTING_STRATEGY,
                    "expect_uids": [],
                    "expect_terms": [name, *chain[:4]],
                    "product_code": product_code,
                    "provenance": f"{doc_rel(product_short, rel)}#overrides",
                    "note": "routing_strategies.overrides[].chain",
                }
            )
    return sorted(rows, key=lambda item: item["qid"])[:cap]


def golden_from_actions(
    product_short: str, product_code: str | None, payload: Any, *, cap: int,
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    actions = payload.get("actions")
    if not isinstance(actions, dict):
        return []
    rel = "capability/action-semantics.json"
    rows: list[dict[str, Any]] = []
    for name, spec in actions.items():
        action_name = normalize_name(str(name))
        if not _is_fact_cell(action_name):
            continue
        expect_uids: list[str] = [f"uita:action:{action_name}"]
        source = spec.get("source") if isinstance(spec, dict) else None
        if isinstance(source, str):
            match = re.match(r"^\s*([A-Za-z0-9_./\-]+\.(?:py|ts|js|go|rs|java|kt))", source)
            if match and "/" in match.group(1):
                expect_uids.append(file_uid(match.group(1)))
        rows.append(
            {
                "qid": stable_qid(product_short, CATEGORY_ACTION_SOURCE, action_name),
                "query": f"动作 {action_name} 的实现端点在哪个文件",
                "category": CATEGORY_ACTION_SOURCE,
                "expect_uids": expect_uids,
                "expect_terms": [action_name],
                "product_code": product_code,
                "provenance": f"{doc_rel(product_short, rel)}#actions/{action_name}",
                "note": "action-semantics.actions[].source",
            }
        )
    return sorted(rows, key=lambda item: item["qid"])[:cap]


def defect_symbol_facts(text: str) -> list[tuple[str, str, str]]:
    """缺陷正文 → `[(issue_id, path, symbol)]`（仅同现行；去重保序）。"""
    facts: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in text.splitlines():
        if "::" not in line:
            continue
        issue_ids = _ISSUE_RE.findall(line)
        if not issue_ids:
            continue
        for path, symbol in _SYMBOL_RE.findall(line)[:2]:
            if len(symbol) <= 2 or (path, symbol) in seen:
                continue
            seen.add((path, symbol))
            facts.append((issue_ids[0], path, symbol))
    return facts


def golden_from_defect_chain(
    product_short: str, product_code: str | None, text: str, file_rel: str, *, cap: int,
) -> list[dict[str, Any]]:
    rel = file_rel
    uid = doc_uid(product_short, rel)
    rows: list[dict[str, Any]] = []
    for issue_id, path, symbol in defect_symbol_facts(text):
        expect_uids = [uid]
        if "/" in path:
            expect_uids.append(symbol_uid(path, symbol))
        rows.append(
            {
                "qid": stable_qid(product_short, CATEGORY_DEFECT_CHAIN, issue_id, path, symbol),
                "query": f"缺陷 {issue_id} 的根因符号 {symbol} 落在哪个实现端点与测试契约上",
                "category": CATEGORY_DEFECT_CHAIN,
                "expect_uids": expect_uids,
                "expect_terms": [symbol],
                "product_code": product_code,
                "provenance": f"{doc_rel(product_short, rel)}",
                "note": "defect-chain: 缺陷单号 + 路径::符号 同现",
            }
        )
    return sorted(rows, key=lambda item: item["qid"])[:cap]


# ── 装配 ────────────────────────────────────────────────────────────────────

def products_root(workspace_root: Path) -> Path:
    return workspace_root / KNOWLEDGE_PREFIX


def available_products(workspace_root: Path) -> list[str]:
    root = products_root(workspace_root)
    if not root.is_dir():
        return []
    return sorted(item.name for item in root.iterdir() if item.is_dir())


def build_for_product(
    workspace_root: Path, product_short: str, *, per_category: int = 6,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    product_dir = products_root(workspace_root) / product_short
    product_code = product_short
    meta_path = product_dir / "utest-rules" / "rules-meta.json"
    meta = read_json(meta_path)
    if isinstance(meta, dict):
        declared = meta.get("product_code")
        if isinstance(declared, str) and declared.strip():
            product_code = declared.strip()

    queries: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {}

    for dimension, spec in DIMENSIONS.items():
        text = read_text(product_dir / spec["file"])
        rows = (
            golden_from_dimension(
                product_short, product_code, dimension, text, cap=per_category,
            )
            if text
            else []
        )
        coverage[dimension] = {
            "file": doc_rel(product_short, spec["file"]),
            "exists": text is not None,
            "golden_count": len(rows),
        }
        queries.extend(rows)

    queries.extend(
        golden_from_world_model(
            product_short,
            product_code,
            read_json(product_dir / "orchestrator" / "world_model.json"),
            cap=per_category,
        )
    )
    queries.extend(
        golden_from_routing(
            product_short,
            product_code,
            read_json(product_dir / "orchestrator" / "routing_strategies.json"),
            cap=per_category,
        )
    )
    queries.extend(
        golden_from_actions(
            product_short,
            product_code,
            read_json(product_dir / "capability" / "action-semantics.json"),
            cap=per_category,
        )
    )
    for file_rel in ("defects/%s-defect-chain.md" % product_short, "utest-rules/04-known-pitfalls.md"):
        text = read_text(product_dir / file_rel)
        if not text:
            continue
        queries.extend(
            golden_from_defect_chain(
                product_short, product_code, text, file_rel, cap=per_category,
            )
        )
    return dedupe(queries), coverage


def dedupe(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 `qid` 去重并按 (category, qid) 排序；容忍人工条目缺字段（缺键按空串排序）。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in sorted(
        rows, key=lambda item: (str(item.get("category") or ""), str(item.get("qid") or "")),
    ):
        qid = str(row.get("qid") or "")
        if qid in seen:
            continue
        seen.add(qid)
        out.append(row)
    return out


def build_payload(
    queries: Sequence[dict[str, Any]],
    *,
    generated_from: str,
    coverage: dict[str, Any] | None = None,
    now: str | None = None,
) -> dict[str, Any]:
    at = now or datetime.now().isoformat(timespec="seconds")
    return {
        "_meta": {
            "version": SCHEMA,
            "generated_at": at,
            "generated_from": generated_from,
            "count": len(queries),
            "categories": sorted({str(item["category"]) for item in queries}),
            "dimensions": coverage or {},
        },
        "queries": list(queries),
    }


def merge_queries(
    base: Sequence[dict[str, Any]], extra: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """保留既有条目（人工改动不丢），只追加 `qid` 不存在的新条目。"""
    by_qid = {str(item.get("qid")): item for item in base}
    added = 0
    for item in extra:
        qid = str(item.get("qid"))
        if qid in by_qid:
            continue
        by_qid[qid] = item
        added += 1
    return dedupe(list(by_qid.values())), added


# ── 自检（离线金标回放自检） ────────────────────────────────────────────────

def check_payload(payload: Any, workspace_root: Path) -> dict[str, Any]:
    """结构 + 血缘自检；返回 `{"ok":bool,"problems":[],"warnings":[],"coverage":{}}`。"""
    problems: list[str] = []
    warnings: list[str] = []
    if not isinstance(payload, dict):
        raise InputError("金标 JSON 顶层必须是对象")
    queries = payload.get("queries")
    if not isinstance(queries, list) or not queries:
        raise InputError("金标 JSON 缺非空 queries 数组")

    required = {
        "qid", "query", "category", "expect_uids", "expect_terms", "product_code",
        "provenance", "note",
    }
    qids: set[str] = set()
    per_category: dict[str, int] = {}
    per_dimension: dict[str, int] = {}
    for index, item in enumerate(queries):
        if not isinstance(item, dict):
            problems.append(f"queries[{index}] 不是对象")
            continue
        keys = set(item)
        missing = sorted(required - keys)
        extra = sorted(keys - required)
        if missing:
            problems.append(f"queries[{index}] 缺字段：{', '.join(missing)}")
        if extra:
            warnings.append(f"queries[{index}] 多出字段（评测规范 会忽略）：{', '.join(extra)}")
        qid = str(item.get("qid") or "")
        if not qid:
            problems.append(f"queries[{index}] 缺 qid")
        elif qid in qids:
            problems.append(f"qid 重复：{qid}")
        else:
            qids.add(qid)
        category = str(item.get("category") or "")
        if category not in CATEGORIES:
            problems.append(f"{qid or index} 的 category 非法（只允许既有 6 类）：{category}")
        per_category[category] = per_category.get(category, 0) + 1
        if not str(item.get("query") or "").strip():
            problems.append(f"{qid or index} 缺 query")
        elif len(str(item["query"])) > MAX_QUERY_CHARS:
            problems.append(f"{qid or index} 的 query 超长（>{MAX_QUERY_CHARS}）")
        uids = item.get("expect_uids") or []
        terms = item.get("expect_terms") or []
        if not uids and not terms:
            problems.append(f"{qid or index} 无任何期望值（无法判相关）")
        for uid in uids:
            if not str(uid).startswith(("uita:", "code:", "issue:", "pr:", "git:", "tcs:", "utest:")):
                problems.append(f"{qid or index} 的 expect_uid 命名空间非法：{uid}")
        note = str(item.get("note") or "")
        match = re.search(r"dim=([a-z_]+)", note)
        if match:
            per_dimension[match.group(1)] = per_dimension.get(match.group(1), 0) + 1
        elif category in (CATEGORY_PAGE_ELEMENTS, CATEGORY_PAGE_NAVIGATION, CATEGORY_ROUTING_STRATEGY, CATEGORY_ACTION_SOURCE):
            per_dimension["product_fact"] = per_dimension.get("product_fact", 0) + 1
        provenance = str(item.get("provenance") or "")
        if provenance:
            path_part = provenance.split("#", 1)[0]
            if path_part and not (workspace_root / path_part).exists():
                warnings.append(f"{qid or index} 的 provenance 文件不存在：{path_part}")

    meta = payload.get("_meta")
    count = len(queries)
    if isinstance(meta, dict):
        declared = meta.get("count")
        if declared is not None and declared != count:
            problems.append(f"_meta.count={declared} 与实际条目数 {count} 不一致")
        categories = meta.get("categories")
        if isinstance(categories, list):
            illegal = [str(item) for item in categories if str(item) not in CATEGORIES]
            if illegal:
                problems.append(f"_meta.categories 含非法类别：{', '.join(illegal)}")
    else:
        warnings.append("缺 _meta（评测规范 可读但丢失血缘信息）")

    dimension_gaps = [
        dimension
        for dimension in DIMENSIONS
        if per_dimension.get(dimension, 0) == 0
    ]
    return {
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "counts": {
            "total": count,
            "by_category": per_category,
            "by_dimension": per_dimension,
        },
        "dimension_gaps": dimension_gaps,
    }


# ── CLI ─────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="sentinel 金标回放库：确定性生成 / 增量合并 / 离线自检",
    )
    parser.add_argument("--mode", choices=("build", "merge", "check"), default="build")
    parser.add_argument("--workspace-root", default=None, help="UITA 工作区根（缺省按脚本位置推断）")
    parser.add_argument(
        "--product-code", action="append", default=None,
        help="知识目录名（可重复；缺省=全部产品）",
    )
    parser.add_argument("--out", required=True, help="金标 JSON 输出路径（check 模式下为待检文件）")
    parser.add_argument("--base", default=None, help="merge 模式的既有金标（缺省=--out 自身）")
    parser.add_argument("--per-category", type=int, default=6, help="每类上限（默认 6）")
    parser.add_argument("--report", default=None, help="check 报告 JSON 输出路径（缺省=<out>.check.json）")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    workspace_root = Path(args.workspace_root) if args.workspace_root else Path(__file__).resolve().parents[1]
    out_path = Path(args.out)
    if args.per_category < 1:
        print("[kb-golden] --per-category 必须 ≥1", file=sys.stderr)
        return 2
    if not workspace_root.is_dir():
        print(f"[kb-golden] 工作区不存在：{workspace_root}", file=sys.stderr)
        return 2

    if args.mode == "check":
        target = out_path
        payload = read_json(target) if target.is_file() else None
        if payload is None:
            print(f"[kb-golden] 待检金标不存在或不是合法 JSON：{target}", file=sys.stderr)
            return 2
        try:
            report = check_payload(payload, workspace_root)
        except InputError as exc:
            print(f"[kb-golden] {exc}", file=sys.stderr)
            return 2
        report["checked_file"] = str(target)
        report_path = Path(args.report) if args.report else target.with_suffix(".check.json")
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        status = "OK" if report["ok"] else "FAIL"
        print(
            f"[kb-golden] check {status} count={report['counts']['total']} "
            f"problems={len(report['problems'])} warnings={len(report['warnings'])} "
            f"dimension_gaps={','.join(report['dimension_gaps']) or 'none'} -> {report_path}",
        )
        return 0 if report["ok"] else 1

    products = args.product_code or available_products(workspace_root)
    if not products:
        print(f"[kb-golden] 未找到产品知识目录：{products_root(workspace_root)}", file=sys.stderr)
        return 2

    queries: list[dict[str, Any]] = []
    coverage: dict[str, Any] = {}
    for product in products:
        if not (products_root(workspace_root) / product).is_dir():
            print(f"[kb-golden] 产品目录不存在：{product}", file=sys.stderr)
            return 2
        rows, product_coverage = build_for_product(
            workspace_root, product, per_category=args.per_category,
        )
        queries.extend(rows)
        coverage[product] = product_coverage

    if args.mode == "merge":
        base_path = Path(args.base) if args.base else out_path
        base_payload = read_json(base_path) if base_path.is_file() else None
        if base_payload is None:
            print(f"[kb-golden] merge 的 base 不存在或非法：{base_path}", file=sys.stderr)
            return 2
        base_queries = base_payload.get("queries") if isinstance(base_payload, dict) else None
        if not isinstance(base_queries, list):
            print("[kb-golden] merge 的 base 缺 queries 数组", file=sys.stderr)
            return 2
        merged, added = merge_queries(base_queries, queries)
        payload = build_payload(merged, generated_from=str(workspace_root), coverage=coverage)
        payload["_meta"]["merged"] = {"base": str(base_path), "added": added, "kept": len(base_queries)}
    else:
        payload = build_payload(dedupe(queries), generated_from=str(workspace_root), coverage=coverage)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"[kb-golden] {args.mode} ok count={payload['_meta']['count']} "
        f"categories={','.join(payload['_meta']['categories'])} -> {out_path}",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
