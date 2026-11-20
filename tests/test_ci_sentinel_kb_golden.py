"""`ci/sentinel_kb_golden.py`（金标回放库）单测。

覆盖四条硬契约（对齐 `reference/kb-flywheel.md`「金标回放库」小节）：
  1. **字段契约**：条目字段集合与 标准规范 `GoldenQuery` 逐字段一致，category 只用既有 6 类；
  2. **确定性**：同输入同 qid（可跨轮对比、可增量合并），不做语义生成；
  3. **期望值可判相关**：`expect_uids` 与 `expect_terms` 至少一项非空；
  4. **合并幂等**：`merge` 保留既有条目、只追加新 qid。

全程用 `tmp_path` 自造工作区，不依赖网络 / 真实知识库 / 标准规范。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
CI_DIR = ROOT_DIR / "ci"
if str(CI_DIR) not in sys.path:
    sys.path.insert(0, str(CI_DIR))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, CI_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


golden = _load("sentinel_kb_golden", "sentinel_kb_golden.py")

PRODUCT = "demo"
PRODUCT_CODE = "demopc"

BUSINESS_ENDPOINTS = """# demo · 业务端点

## 业务端点

| 端点 | 用户可见入口 | 后端模块 | 可观察结果 |
|---|---|---|---|
| 导出相册 | 右键菜单 → 导出 | main/export | 目标目录出现文件 |
| 备份到云盘 | 设置 → 云盘备份 | main/cloud | 任务列表出现条目 |
"""

BUSINESS_FLOWS = """# demo · 业务流程

## 业务流程

| 流程 | 步骤链 | 分支 | 可观察输出 |
|---|---|---|---|
| 扫码登录 | 打开 → 扫码 → 轮询 → 登录态 | 二维码过期 | 首页出现用户头像 |
"""

TECH_ENDPOINTS = """# demo · 技术端点

## 技术端点

| 端点 | 类型 | 定义位置 | 入参→返回 |
|---|---|---|---|
| POST /api/export | HTTP | server/export.ts | {dir} → {taskId} |
"""

TECH_ARCH = """# demo · 技术架构

## 模块

| 模块 | 职责 | 依赖方向 |
|---|---|---|
| main | 主进程编排 | → renderer |
"""

TEST_DESIGN = """# demo · 测试设计

## 风险形态

| 风险形态 | 验证手段 | 断言契约 | mock 边界 |
|---|---|---|---|
| 导出中断后残留半成品 | 单测 | 目标文件数为 0 | fs 全 mock |
"""

WORLD_MODEL = {
    "_meta": {"app": PRODUCT, "version": 1},
    "pages": [
        {
            "id": "home",
            "name": "首页",
            "key_elements": ["此电脑", "设置"],
            "children": ["photos"],
        },
        {"id": "photos", "name": "图库", "key_elements": ["筛选"], "parent": "home"},
    ],
}

ROUTING = {
    "_meta": {"app": PRODUCT},
    "default": {"chain": ["axpath", "css", "vision"]},
    "overrides": {"_img": {"chain": ["css", "vision"]}},
}

DEFECT_CHAIN = """# demo · 缺陷链

| bug | 关联符号 |
|---|---|
| 1 导出的文件残留 | 2099035012：`apps/main/export.ts::cleanupOnAbort` |
| 2 裸文件名的行 | 2099035013：`export.ts::cleanupOnAbort` |
"""


def _make_workspace(tmp_path: Path, *, with_dimensions: bool = True) -> Path:
    product = tmp_path / ".dsh" / "knowledge" / "products" / PRODUCT
    (product / "orchestrator").mkdir(parents=True, exist_ok=True)
    (product / "utest-rules").mkdir(parents=True, exist_ok=True)
    (product / "defects").mkdir(parents=True, exist_ok=True)
    (product / "utest-rules" / "rules-meta.json").write_text(
        json.dumps({"rules_version": "v3", "product_code": PRODUCT_CODE}), encoding="utf-8",
    )
    (product / "orchestrator" / "world_model.json").write_text(
        json.dumps(WORLD_MODEL, ensure_ascii=False), encoding="utf-8",
    )
    (product / "orchestrator" / "routing_strategies.json").write_text(
        json.dumps(ROUTING, ensure_ascii=False), encoding="utf-8",
    )
    (product / "defects" / f"{PRODUCT}-defect-chain.md").write_text(DEFECT_CHAIN, encoding="utf-8")
    if with_dimensions:
        (product / "business").mkdir(parents=True, exist_ok=True)
        (product / "architecture").mkdir(parents=True, exist_ok=True)
        (product / "business" / "business-endpoints.md").write_text(BUSINESS_ENDPOINTS, encoding="utf-8")
        (product / "business" / "business-flows.md").write_text(BUSINESS_FLOWS, encoding="utf-8")
        (product / "architecture" / "technical-endpoints.md").write_text(TECH_ENDPOINTS, encoding="utf-8")
        (product / "architecture" / "technical-architecture.md").write_text(TECH_ARCH, encoding="utf-8")
        (product / "utest-rules" / "07-test-design.md").write_text(TEST_DESIGN, encoding="utf-8")
    return tmp_path


# ── 结构解析 ────────────────────────────────────────────────────────────────

def test_markdown_tables_skips_separator_and_headings():
    tables = golden.markdown_tables(BUSINESS_ENDPOINTS)
    assert len(tables) == 1
    section, rows = tables[0]
    assert section == "业务端点"
    names = [row[0] for row in rows]
    assert "导出相册" in names
    assert all(set(cell) - set("-: ") for cell in rows[0])


def test_dimension_facts_dedupes_by_name():
    text = "| 端点 |\n|---|\n| 导出相册 |\n| 导出相册 |\n"
    facts = golden.dimension_facts(text)
    assert [name for name, _ in facts] == ["导出相册"]


# ── 各来源 ──────────────────────────────────────────────────────────────────

def test_golden_from_dimension_shape():
    rows = golden.golden_from_dimension(
        PRODUCT, PRODUCT_CODE, "business_endpoint", BUSINESS_ENDPOINTS, cap=6,
    )
    assert len(rows) == 2
    row = rows[0]
    assert set(row) == {
        "qid", "query", "category", "expect_uids", "expect_terms",
        "product_code", "provenance", "note",
    }
    assert row["category"] == golden.CATEGORY_CROSS_SUBGRAPH
    assert "dim=business_endpoint" in row["note"]
    assert row["expect_uids"] == [
        "uita:doc:.dsh/knowledge/products/demo/business/business-endpoints.md"
    ]
    assert row["product_code"] == PRODUCT_CODE
    assert row["query"].startswith("业务端点「")


def test_golden_from_dimension_cap_and_skip_cells():
    text = "| 端点 | 说明 |\n|---|---|\n| 端点 | 表头重复 |\n| — | 占位 |\n| A端点 | 真事实 |\n| B端点 | 真事实 |\n"
    rows = golden.golden_from_dimension(PRODUCT, PRODUCT_CODE, "business_endpoint", text, cap=1)
    assert len(rows) == 1
    assert any(term in rows[0]["query"] for term in ("A端点", "B端点"))
    rows_all = golden.golden_from_dimension(PRODUCT, PRODUCT_CODE, "business_endpoint", text, cap=10)
    assert len(rows_all) == 2


def test_golden_from_world_model_uses_terms_only():
    rows = golden.golden_from_world_model(PRODUCT, PRODUCT_CODE, WORLD_MODEL, cap=6)
    by_category = {row["category"] for row in rows}
    assert by_category <= {golden.CATEGORY_PAGE_ELEMENTS, golden.CATEGORY_PAGE_NAVIGATION}
    for row in rows:
        assert row["expect_uids"] == []
        assert row["expect_terms"]
    assert any("首页" in row["query"] for row in rows)


def test_golden_from_world_model_tolerates_bad_payload():
    assert golden.golden_from_world_model(PRODUCT, None, None, cap=6) == []
    assert golden.golden_from_world_model(PRODUCT, None, {"pages": "oops"}, cap=6) == []


def test_golden_from_routing_default_and_overrides():
    rows = golden.golden_from_routing(PRODUCT, PRODUCT_CODE, ROUTING, cap=6)
    queries = " ".join(row["query"] for row in rows)
    assert "默认策略链" in queries
    assert "「_img」" in queries
    assert all(row["category"] == golden.CATEGORY_ROUTING_STRATEGY for row in rows)


def test_golden_from_actions_adds_file_uid_only_for_directories():
    payload = {
        "actions": {
            "click": {"source": "apps/sample_web/action_click.py:12"},
            "type": {"source": "action_type.py:3"},
        }
    }
    rows = golden.golden_from_actions("sample_web", "sample_web", payload, cap=6)
    by_name = {row["query"].split()[1]: row for row in rows}
    assert "code:file:apps/sample_web/action_click.py" in by_name["click"]["expect_uids"]
    assert all(
        not uid.startswith("code:file:") for uid in by_name["type"]["expect_uids"]
    )


def test_golden_from_defect_chain_requires_directory_for_symbol_uid():
    rows = golden.golden_from_defect_chain(
        PRODUCT, PRODUCT_CODE, DEFECT_CHAIN, "defects/demo-defect-chain.md", cap=10,
    )
    by_issue = {row["query"].split()[1]: row for row in rows}
    assert "code:symbol:apps/main/export.ts::cleanupOnAbort" in by_issue["2099035012"]["expect_uids"]
    assert all(
        not uid.startswith("code:symbol:")
        for uid in by_issue["2099035013"]["expect_uids"]
    )


# ── 装配 / 合并 ─────────────────────────────────────────────────────────────

def test_build_for_product_reads_five_dimensions(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    queries, coverage = golden.build_for_product(workspace, PRODUCT, per_category=6)
    assert set(coverage) == set(golden.DIMENSIONS)
    assert all(item["exists"] for item in coverage.values())
    notes = " ".join(row["note"] for row in queries)
    for dimension in golden.DIMENSIONS:
        assert f"dim={dimension}" in notes
    for row in queries:
        assert row["category"] in golden.CATEGORIES
        assert row["expect_uids"] or row["expect_terms"]
        assert row["product_code"] == PRODUCT_CODE


def test_build_for_product_missing_dimension_is_reported_not_raised(tmp_path: Path):
    workspace = _make_workspace(tmp_path, with_dimensions=False)
    queries, coverage = golden.build_for_product(workspace, PRODUCT, per_category=6)
    assert coverage["business_endpoint"]["exists"] is False
    assert coverage["business_endpoint"]["golden_count"] == 0
    assert queries  # world_model 等来源仍然产出


def test_build_is_deterministic(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    first, _ = golden.build_for_product(workspace, PRODUCT, per_category=6)
    second, _ = golden.build_for_product(workspace, PRODUCT, per_category=6)
    assert [row["qid"] for row in first] == [row["qid"] for row in second]
    assert golden.stable_qid(PRODUCT, "defect_chain", "a", "b") == golden.stable_qid(
        PRODUCT, "defect_chain", "a", "b",
    )


def test_merge_queries_keeps_base_and_is_idempotent():
    base = [{"qid": "keep-1", "query": "手工条目"}]
    extra = [{"qid": "new-1", "query": "机器条目"}, {"qid": "keep-1", "query": "重复"}]
    merged, added = golden.merge_queries(base, extra)
    assert added == 1
    qids = [row["qid"] for row in merged]
    assert "keep-1" in qids and "new-1" in qids
    assert next(row for row in merged if row["qid"] == "keep-1")["query"] == "手工条目"
    again, added_again = golden.merge_queries(merged, extra)
    assert added_again == 0
    assert [row["qid"] for row in again] == qids


def test_build_payload_meta_contract(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    queries, coverage = golden.build_for_product(workspace, PRODUCT, per_category=6)
    payload = golden.build_payload(queries, generated_from=str(workspace), coverage=coverage)
    meta = payload["_meta"]
    assert meta["version"] == golden.SCHEMA
    assert meta["count"] == len(payload["queries"])
    assert meta["categories"] == sorted({row["category"] for row in queries})
    assert set(meta["dimensions"]) == set(golden.DIMENSIONS)


# ── 自检 ────────────────────────────────────────────────────────────────────

def _payload(tmp_path: Path) -> dict:
    workspace = _make_workspace(tmp_path)
    queries, coverage = golden.build_for_product(workspace, PRODUCT, per_category=6)
    return golden.build_payload(queries, generated_from=str(workspace), coverage=coverage)


def test_check_payload_ok(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    report = golden.check_payload(_payload(tmp_path), workspace)
    assert report["ok"] is True
    assert report["problems"] == []
    assert report["dimension_gaps"] == []
    assert report["counts"]["total"] > 0


def test_check_payload_flags_duplicate_and_illegal_category(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    payload = _payload(tmp_path)
    payload["queries"].append(dict(payload["queries"][0]))
    payload["queries"][1]["category"] = "new_invented_category"
    payload["_meta"]["count"] = len(payload["queries"])
    report = golden.check_payload(payload, workspace)
    assert report["ok"] is False
    assert any("qid 重复" in item for item in report["problems"])
    assert any("category 非法" in item for item in report["problems"])


def test_check_payload_flags_empty_expect_and_bad_uid(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    payload = _payload(tmp_path)
    payload["queries"][0]["expect_uids"] = ["bogus:type:key"]
    payload["queries"][0]["expect_terms"] = []
    report = golden.check_payload(payload, workspace)
    assert report["ok"] is False
    assert any("命名空间非法" in item for item in report["problems"])


def test_check_payload_flags_count_mismatch(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    payload = _payload(tmp_path)
    payload["_meta"]["count"] = 999
    report = golden.check_payload(payload, workspace)
    assert any("_meta.count" in item for item in report["problems"])


def test_check_payload_warns_missing_provenance(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    payload = _payload(tmp_path)
    payload["queries"][0]["provenance"] = ".dsh/knowledge/products/demo/nope.md#x"
    report = golden.check_payload(payload, workspace)
    assert report["ok"] is True
    assert any("provenance 文件不存在" in item for item in report["warnings"])


def test_check_payload_rejects_empty_queries(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    try:
        golden.check_payload({"_meta": {}, "queries": []}, workspace)
    except golden.InputError as exc:
        assert "queries" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("空 queries 必须抛 InputError")


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_build_then_check(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    out = tmp_path / "golden.json"
    assert golden.main(["--mode", "build", "--workspace-root", str(workspace), "--out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["_meta"]["count"] == len(payload["queries"])
    assert golden.main(["--mode", "check", "--workspace-root", str(workspace), "--out", str(out)]) == 0
    assert (tmp_path / "golden.check.json").is_file()


def test_cli_check_returns_one_on_problem(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    out = tmp_path / "golden.json"
    payload = {
        "_meta": {"count": 1, "categories": ["bogus"]},
        "queries": [
            {
                "qid": "x", "query": "q", "category": "bogus",
                "expect_uids": [], "expect_terms": [], "product_code": None,
                "provenance": "", "note": "",
            }
        ],
    }
    out.write_text(json.dumps(payload), encoding="utf-8")
    assert golden.main(["--mode", "check", "--workspace-root", str(workspace), "--out", str(out)]) == 1


def test_cli_merge_requires_base(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    out = tmp_path / "golden.json"
    assert golden.main(
        ["--mode", "merge", "--workspace-root", str(workspace), "--out", str(out)],
    ) == 2


def test_cli_merge_appends(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    out = tmp_path / "golden.json"
    assert golden.main(["--mode", "build", "--workspace-root", str(workspace), "--out", str(out)]) == 0
    before = json.loads(out.read_text(encoding="utf-8"))
    assert golden.main(
        ["--mode", "merge", "--workspace-root", str(workspace), "--out", str(out)],
    ) == 0
    after = json.loads(out.read_text(encoding="utf-8"))
    assert after["_meta"]["merged"]["added"] == 0
    assert after["_meta"]["merged"]["kept"] == len(before["queries"])


def test_cli_rejects_unknown_product(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    assert golden.main(
        ["--mode", "build", "--workspace-root", str(workspace),
         "--product-code", "ghost", "--out", str(tmp_path / "g.json")],
    ) == 2


def test_cli_rejects_bad_per_category(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    assert golden.main(
        ["--mode", "build", "--workspace-root", str(workspace),
         "--per-category", "0", "--out", str(tmp_path / "g.json")],
    ) == 2
