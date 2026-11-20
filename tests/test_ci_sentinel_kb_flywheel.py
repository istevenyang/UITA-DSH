"""`ci/sentinel_kb_flywheel.py`（知识库双质量飞轮）单测。

覆盖三条硬契约（对齐 `reference/kb-flywheel.md`）：
  1. **缺口可分辨**：零召回（`no_hit`）与「命中但不可用」（`not_useful`）、维度缺失与空维度必须分开记；
  2. **机器不越权**：死知识机器只能写 `candidate`，`confirmed` 只接受业务侧输入，机器永无删除；
  3. **关系受白名单约束**：`edge_type` 必须落在 标准规范 20 白名单、uid 必须带合法命名空间，
     端点存在性查不到时记 `unverified`（不假设成功）。

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


flywheel = _load("sentinel_kb_flywheel", "sentinel_kb_flywheel.py")

PRODUCT = "demo"

BUSINESS_ENDPOINTS = """# demo · 业务端点

## 业务端点

| 端点 | 入口 | 后端模块 |
|---|---|---|
| 导出相册 | 右键 → 导出 | main/export |

## 关系

| src | relation | dst | evidence |
|---|---|---|---|
| uita:doc:.dsh/knowledge/products/demo/business/business-endpoints.md | routes_to | code:file:apps/main/export.ts | 导出入口 |
"""

TEST_DESIGN = """# demo · 测试设计

## 风险形态

| 风险形态 | 验证手段 | 断言契约 |
|---|---|---|
| 导出中断残留 | 单测 | 文件数为 0 |
"""


def _make_workspace(tmp_path: Path, *, with_dimensions: bool = True) -> Path:
    product = tmp_path / ".dsh" / "knowledge" / "products" / PRODUCT
    (product / "utest-rules").mkdir(parents=True, exist_ok=True)
    if with_dimensions:
        (product / "business").mkdir(parents=True, exist_ok=True)
        (product / "architecture").mkdir(parents=True, exist_ok=True)
        (product / "business" / "business-endpoints.md").write_text(
            BUSINESS_ENDPOINTS, encoding="utf-8",
        )
        (product / "utest-rules" / "07-test-design.md").write_text(TEST_DESIGN, encoding="utf-8")
    (tmp_path / "apps" / "main").mkdir(parents=True)
    (tmp_path / "apps" / "main" / "export.ts").write_text(
        "export function cleanupOnAbort() {}\n", encoding="utf-8",
    )
    return tmp_path


def _evidence(items: list[dict]) -> dict:
    return {"schema": flywheel.SCHEMA_RECALL, "run_key": "demo!1#20261023-120000", "items": items}


def _item(**overrides) -> dict:
    base = {
        "recall_id": 7,
        "query": "导出的清理逻辑在哪",
        "step": 3,
        "dimension": "business_endpoint",
        "hit_count": 1,
        "hits": ["code:symbol:apps/main/export.ts::cleanupOnAbort"],
        "adopted_uids": ["code:symbol:apps/main/export.ts::cleanupOnAbort"],
        "decision": "keep",
        "decision_evidence": "与本轮 diff 一致",
        "target": "",
        "note": "",
    }
    base.update(overrides)
    return base


# ── 缺口审计 ────────────────────────────────────────────────────────────────

def test_audit_reports_missing_and_empty_dimensions(tmp_path: Path):
    workspace = _make_workspace(tmp_path, with_dimensions=False)
    report = flywheel.audit_recall_evidence(
        _evidence([_item()]), knowledge_dir=workspace / ".dsh" / "knowledge" / "products" / PRODUCT,
    )
    kinds = [gap["kind"] for gap in report["gaps"]]
    assert kinds.count("missing_dimension") == len(flywheel.DIMENSIONS)
    assert report["ok"] is True


def test_audit_covers_filled_dimensions(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    report = flywheel.audit_recall_evidence(
        _evidence([_item()]), knowledge_dir=workspace / ".dsh" / "knowledge" / "products" / PRODUCT,
    )
    assert report["coverage"]["business_endpoint"]["filled"] is True
    assert report["coverage"]["business_endpoint"]["fact_rows"] == 1
    assert report["coverage"]["business_flow"]["exists"] is False
    gaps = {(gap["kind"], gap.get("dimension")) for gap in report["gaps"]}
    assert ("missing_dimension", "business_flow") in gaps


def test_audit_empty_dimension_is_not_missing(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    path = workspace / ".dsh" / "knowledge" / "products" / PRODUCT / "utest-rules" / "07-test-design.md"
    path.write_text("# demo · 测试设计\n\n（待补）\n", encoding="utf-8")
    report = flywheel.audit_recall_evidence(
        _evidence([_item()]), knowledge_dir=workspace / ".dsh" / "knowledge" / "products" / PRODUCT,
    )
    kinds = {(gap["kind"], gap.get("dimension")) for gap in report["gaps"]}
    assert ("empty_dimension", "test_design") in kinds
    assert ("missing_dimension", "test_design") not in kinds


def test_audit_distinguishes_no_hit_and_not_useful(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    items = [
        _item(recall_id=1, hit_count=0, hits=[], adopted_uids=[], decision="add"),
        _item(recall_id=2, hit_count=3, adopted_uids=[], decision="add"),
    ]
    report = flywheel.audit_recall_evidence(
        _evidence(items), knowledge_dir=workspace / ".dsh" / "knowledge" / "products" / PRODUCT,
    )
    kinds = [gap["kind"] for gap in report["gaps"]]
    assert "no_hit" in kinds
    assert "not_useful" in kinds
    assert report["gap_counts"]["no_hit"] == 1
    assert report["gap_counts"]["not_useful"] == 1


def test_audit_tallies_decisions_and_machine_actions(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    items = [
        _item(recall_id=1, decision="keep"),
        _item(recall_id=2, decision="update", target="business/business-endpoints.md#导出相册"),
        _item(
            recall_id=3, decision="conflict",
            target="business/business-endpoints.md#导出相册",
        ),
        _item(recall_id=4, decision="deprecate_candidate", target="utest-rules/07-test-design.md#旧断言"),
    ]
    report = flywheel.audit_recall_evidence(
        _evidence(items), knowledge_dir=workspace / ".dsh" / "knowledge" / "products" / PRODUCT,
    )
    assert report["decisions"] == {
        "keep": 1, "update": 1, "conflict": 1, "deprecate_candidate": 1,
    }
    assert [item["decision"] for item in report["machine_actions"]] == [
        "update", "conflict", "deprecate_candidate",
    ]
    assert report["machine_actions"][1]["conflict"] is True


def test_audit_flags_illegal_decision_and_missing_evidence(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    items = [
        _item(recall_id=1, decision="maybe"),
        _item(recall_id=2, decision="update", decision_evidence="", target="x.md#y"),
        _item(recall_id=3, decision="add", dimension=None),
        _item(recall_id=4, step=5),
    ]
    report = flywheel.audit_recall_evidence(
        _evidence(items), knowledge_dir=workspace / ".dsh" / "knowledge" / "products" / PRODUCT,
    )
    assert report["ok"] is False
    joined = " | ".join(report["problems"])
    assert "decision 非法" in joined
    assert "缺 decision_evidence" in joined
    assert "缺 dimension" in joined
    assert "step 必须是 3 / 6 / 8" in joined


def test_audit_flags_duplicate_recall_id_and_bad_uid(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    items = [
        _item(recall_id=5, adopted_uids=["bogus:x:y"]),
        _item(recall_id=5),
    ]
    report = flywheel.audit_recall_evidence(
        _evidence(items), knowledge_dir=workspace / ".dsh" / "knowledge" / "products" / PRODUCT,
    )
    joined = " | ".join(report["problems"])
    assert "recall_id 重复" in joined
    assert "命名空间非法" in joined


def test_audit_rejects_bad_schema_and_empty_items(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    for payload in (
        {"schema": "other/v1", "items": [_item()]},
        {"schema": flywheel.SCHEMA_RECALL, "items": []},
        ["not-a-dict"],
    ):
        try:
            flywheel.audit_recall_evidence(
                payload,
                knowledge_dir=workspace / ".dsh" / "knowledge" / "products" / PRODUCT,
            )
        except flywheel.InputError:
            continue
        raise AssertionError(f"必须拒绝非法输入：{payload!r}")


# ── 死知识台账 ─────────────────────────────────────────────────────────────

def test_dead_ledger_new_and_idempotent(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    path = flywheel.dead_ledger_path(workspace, PRODUCT)
    ledger = flywheel.load_dead_ledger(path, product=PRODUCT)
    record = {"target": "utest-rules/07-test-design.md#旧断言", "reason": "已重构", "status": "candidate"}
    ledger, result = flywheel.record_dead(ledger, record, source="agent")
    assert result["action"] == "recorded"
    assert result["entry"]["marked_by"] == "agent_suggested"
    ledger, result = flywheel.record_dead(ledger, record, source="agent")
    assert result["action"] == "unchanged"
    assert len(ledger["items"]) == 1


def test_dead_agent_cannot_confirm(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    ledger = flywheel.load_dead_ledger(flywheel.dead_ledger_path(workspace, PRODUCT), product=PRODUCT)
    try:
        flywheel.record_dead(
            ledger, {"target": "a#b", "reason": "r", "status": "confirmed"}, source="agent",
        )
    except flywheel.GuardViolation as exc:
        assert "confirmed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("机器侧写 confirmed 必须被拒")


def test_dead_business_can_confirm_and_agent_cannot_downgrade(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    ledger = flywheel.load_dead_ledger(flywheel.dead_ledger_path(workspace, PRODUCT), product=PRODUCT)
    ledger, _ = flywheel.record_dead(
        ledger,
        {"target": "a#b", "reason": "业务确认失效", "status": "confirmed", "marked_by": "business"},
        source="business",
    )
    assert ledger["items"][0]["status"] == "confirmed"
    try:
        flywheel.record_dead(ledger, {"target": "a#b", "reason": "机器想降级"}, source="agent")
    except flywheel.GuardViolation as exc:
        assert "不得被机器降级" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("已确认的死知识不得被机器降级")


def test_dead_record_validation(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    ledger = flywheel.load_dead_ledger(flywheel.dead_ledger_path(workspace, PRODUCT), product=PRODUCT)
    for record in ({"target": "a#b"}, {"reason": "r"}, {"target": "a#b", "reason": "r", "status": "deleted"}):
        try:
            flywheel.record_dead(ledger, record, source="agent")
        except flywheel.InputError:
            continue
        raise AssertionError(f"必须拒绝非法记录：{record!r}")
    assert ledger["items"] == []


def test_dead_ledger_rejects_wrong_schema(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    path = flywheel.dead_ledger_path(workspace, PRODUCT)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "nope", "items": []}), encoding="utf-8")
    try:
        flywheel.load_dead_ledger(path, product=PRODUCT)
    except flywheel.InputError as exc:
        assert "schema" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("schema 非法必须被拒")


# ── 关系维护 ───────────────────────────────────────────────────────────────

def test_relations_valid_row(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    report = flywheel.audit_relations(workspace, PRODUCT)
    assert report["ok"] is True
    assert report["totals"]["valid"] == 1
    assert report["valid"][0]["relation"] == "routes_to"
    assert report["files_scanned"]


def test_relations_reject_unknown_edge_type(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    path = workspace / ".dsh" / "knowledge" / "products" / PRODUCT / "business" / "business-endpoints.md"
    text = path.read_text(encoding="utf-8").replace("| routes_to |", "| invented_edge |")
    path.write_text(text, encoding="utf-8")
    report = flywheel.audit_relations(workspace, PRODUCT)
    assert report["ok"] is False
    assert report["totals"]["invalid"] == 1
    assert "白名单" in report["invalid"][0]["problem"]


def test_relations_reject_bad_namespace_and_self_loop(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    path = workspace / ".dsh" / "knowledge" / "products" / PRODUCT / "business" / "business-endpoints.md"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "| uita:doc:.dsh/knowledge/products/demo/business/business-endpoints.md | routes_to |",
        "| bogus:x:y | routes_to |",
    )
    path.write_text(text, encoding="utf-8")
    report = flywheel.audit_relations(workspace, PRODUCT)
    assert report["ok"] is False
    assert any("命名空间非法" in item["problem"] for item in report["invalid"])

    text = text.replace("| bogus:x:y | routes_to | code:file:apps/main/export.ts |", "| uita:doc:a | routes_to | uita:doc:a |")
    path.write_text(text, encoding="utf-8")
    report = flywheel.audit_relations(workspace, PRODUCT)
    assert any("自环" in item["problem"] for item in report["invalid"])


def test_relations_mark_missing_code_endpoint_unverified(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    path = workspace / ".dsh" / "knowledge" / "products" / PRODUCT / "business" / "business-endpoints.md"
    text = path.read_text(encoding="utf-8").replace(
        "code:file:apps/main/export.ts", "code:file:apps/main/missing.ts",
    )
    path.write_text(text, encoding="utf-8")
    report = flywheel.audit_relations(workspace, PRODUCT)
    assert report["ok"] is True
    assert report["totals"]["unverified"] == 1
    assert "文件不存在" in report["unverified"][0]["problem"]


def test_relations_mark_absent_symbol_unverified(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    path = workspace / ".dsh" / "knowledge" / "products" / PRODUCT / "business" / "business-endpoints.md"
    text = path.read_text(encoding="utf-8").replace(
        "code:file:apps/main/export.ts", "code:symbol:apps/main/export.ts::notThere",
    )
    path.write_text(text, encoding="utf-8")
    report = flywheel.audit_relations(workspace, PRODUCT)
    assert report["totals"]["unverified"] == 1
    assert "符号未在文件中出现" in report["unverified"][0]["problem"]


def test_relations_duplicate_row_is_invalid(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    path = workspace / ".dsh" / "knowledge" / "products" / PRODUCT / "business" / "business-endpoints.md"
    text = path.read_text(encoding="utf-8")
    row = "| uita:doc:a | routes_to | code:file:apps/main/export.ts | e |\n"
    path.write_text(text + row + row, encoding="utf-8")
    report = flywheel.audit_relations(workspace, PRODUCT)
    assert any("重复关系" in item["problem"] for item in report["invalid"])


def test_relation_rows_ignores_tables_without_header(tmp_path: Path):
    text = "## 表\n\n| 端点 | 说明 |\n|---|---|\n| A | B |\n"
    assert flywheel.relation_rows(text) == []


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_audit_writes_report(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    evidence_path = tmp_path / "recall.json"
    evidence_path.write_text(json.dumps(_evidence([_item()]), ensure_ascii=False), encoding="utf-8")
    code = flywheel.main(
        ["--workspace-root", str(workspace), "--product-code", PRODUCT,
         "audit", "--recall-evidence", str(evidence_path)],
    )
    assert code == 0
    report = json.loads((tmp_path / "recall.audit.json").read_text(encoding="utf-8"))
    assert report["product"] == PRODUCT


def test_cli_audit_returns_one_on_invalid_decision(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    evidence_path = tmp_path / "recall.json"
    evidence_path.write_text(
        json.dumps(_evidence([_item(decision="guess")]), ensure_ascii=False), encoding="utf-8",
    )
    code = flywheel.main(
        ["--workspace-root", str(workspace), "--product-code", PRODUCT,
         "audit", "--recall-evidence", str(evidence_path)],
    )
    assert code == 1


def test_cli_dead_agent_confirm_returns_one(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    record = json.dumps({"target": "a#b", "reason": "r", "status": "confirmed"})
    code = flywheel.main(
        ["--workspace-root", str(workspace), "--product-code", PRODUCT,
         "dead", "--record", record, "--source", "agent"],
    )
    assert code == 1


def test_cli_dead_business_writes_ledger(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    record = json.dumps({"target": "a#b", "reason": "业务确认失效", "status": "confirmed"})
    code = flywheel.main(
        ["--workspace-root", str(workspace), "--product-code", PRODUCT,
         "dead", "--record", record, "--source", "business"],
    )
    assert code == 0
    ledger = json.loads(
        flywheel.dead_ledger_path(workspace, PRODUCT).read_text(encoding="utf-8"),
    )
    assert ledger["items"][0]["status"] == "confirmed"


def test_cli_relations_returns_one_on_invalid(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    path = workspace / ".dsh" / "knowledge" / "products" / PRODUCT / "business" / "business-endpoints.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace("| routes_to |", "| invented |"), encoding="utf-8",
    )
    code = flywheel.main(
        ["--workspace-root", str(workspace), "--product-code", PRODUCT, "relations"],
    )
    assert code == 1


def test_cli_rejects_unknown_product(tmp_path: Path):
    workspace = _make_workspace(tmp_path)
    code = flywheel.main(
        ["--workspace-root", str(workspace), "--product-code", "ghost", "relations"],
    )
    assert code == 2


def test_local_recall_and_sample_web_knowledge_structure(tmp_path: Path):
    out_file = tmp_path / "recall.json"
    code = flywheel.main(
        [
            "--workspace-root",
            str(ROOT_DIR),
            "--product-code",
            "sample_web",
            "recall",
            "--query",
            "首页 表单 提交",
            "--out",
            str(out_file),
        ]
    )
    assert code == 0
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["schema"] == "sentinel-kb-local-recall/v1"
    assert payload["product"] == "sample_web"
    assert payload["hit_count"] >= 1
    assert any("business-endpoints.md" in h["file"] or "business-flows.md" in h["file"] for h in payload["hits"])

