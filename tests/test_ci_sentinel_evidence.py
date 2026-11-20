"""`ci/sentinel_evidence.py` 过程证据台账契约测试。

被测算子：**逐步 append → 步骤 9 merge**。
测试只做本地临时目录 + 子进程，不触网、不写库、不依赖真实报告轮次。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = ROOT_DIR / "ci" / "sentinel_evidence.py"

SHA = "0123456789abcdef0123456789abcdef01234567"
SOURCE_REF = "org_proj/sample-web!999"
RUN_KEY = f"{SOURCE_REF}#20261027-230000"
LEDGER_REL = Path(".cache") / "evidence-ledger.jsonl"
ITEM_KEYS = {
    "evidence_key", "step_id", "category", "verification_method", "linked_risk_key",
    "linked_test_key", "strength", "polarity", "fact_summary", "artifact_refs", "occurred_at",
}


def _run(*args: str, stdin: str | None = None, drop_model: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    if drop_model:
        env.pop("MODEL", None)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _report_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "mr-999+20261027-230000"
    (run_dir / ".cache").mkdir(parents=True)
    (run_dir / "tests").mkdir()
    (run_dir / "device" / "R1").mkdir(parents=True)
    (run_dir / ".cache" / "run-meta.json").write_text(
        json.dumps({"source_ref": SOURCE_REF, "product_code": "sample_web"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (run_dir / ".cache" / "diff.raw.txt").write_text("diff --git a/x.ts b/x.ts\n+// 改动\n", encoding="utf-8")
    (run_dir / "rules-meta.json").write_text(json.dumps({"rules_version": "v29"}), encoding="utf-8")
    (run_dir / "01-req.md").write_text("# 需求口径\n", encoding="utf-8")
    (run_dir / "03-risk.md").write_text("# 风险\n", encoding="utf-8")
    (run_dir / "tests" / "t.test.ts").write_text("it('x', () => {})\n", encoding="utf-8")
    (run_dir / "utest-defects.json").write_text('{"summary":{}}\n', encoding="utf-8")
    (run_dir / "risk-report.html").write_text("<!doctype html><html></html>\n", encoding="utf-8")
    return run_dir


def _append(
    run_dir: Path,
    key: str,
    step: str,
    *,
    category: str = "decision",
    method: str = "file",
    strength: str = "definitive",
    polarity: str = "supporting",
    fact: str = "本步事实摘要",
    risk: str | None = None,
    test: str | None = None,
    artifacts: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    args = [
        "append", "--report-dir", str(run_dir),
        "--step-id", step, "--evidence-key", key,
        "--category", category, "--verification-method", method,
        "--strength", strength, "--polarity", polarity,
        "--fact-summary", fact,
    ]
    if risk:
        args += ["--risk-key", risk]
    if test:
        args += ["--test-key", test]
    for rel in artifacts:
        args += ["--artifact", rel]
    return _run(*args)


def _merge(run_dir: Path, *extra: str) -> subprocess.CompletedProcess:
    return _run(
        "merge", "--report-dir", str(run_dir),
        "--run-key", RUN_KEY, "--source-ref", SOURCE_REF,
        "--code-revision", SHA, "--knowledge-version", "profile-v1",
        "--model", "test-model", "--skill-version", "9.9.9",
        "--rules-meta", str(run_dir / "rules-meta.json"),
        *extra,
    )


def _ledger_lines(run_dir: Path) -> list[dict]:
    path = run_dir / LEDGER_REL
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _all_steps(run_dir: Path) -> None:
    for index, step in enumerate(
        (
            "decompose_requirement", "extract_change", "discover_risks", "review_defects",
            "route_risks", "design_tests", "execute_on_device", "self_heal_writeback", "report",
        ),
        start=1,
    ):
        proc = _append(run_dir, f"step{index}:{step}", step, fact=f"第 {index} 步事实")
        assert proc.returncode == 0, proc.stderr


# ── append ────────────────────────────────────────────────────────────────


def test_append_computes_artifact_facts(tmp_path):
    run_dir = _report_dir(tmp_path)
    proc = _append(run_dir, "change:diff", "extract_change", category="change",
                   method="command", artifacts=("01-req.md",))
    assert proc.returncode == 0, proc.stderr
    assert "EVIDENCE APPEND OK" in proc.stdout

    lines = _ledger_lines(run_dir)
    assert len(lines) == 1
    assert set(lines[0]) == ITEM_KEYS
    artifact = lines[0]["artifact_refs"][0]
    raw = (run_dir / "01-req.md").read_bytes()
    assert artifact["path"] == "01-req.md"
    assert artifact["sha256"] == hashlib.sha256(raw).hexdigest()
    assert artifact["size_bytes"] == len(raw)
    assert artifact["media_type"] == "text/markdown"


def test_append_rejects_duplicate_key_without_touching_history(tmp_path):
    run_dir = _report_dir(tmp_path)
    assert _append(run_dir, "risk:R1", "discover_risks", risk="R1").returncode == 0
    dup = _append(run_dir, "risk:R1", "discover_risks", risk="R1", fact="改写后的事实")
    assert dup.returncode == 1
    assert "历史证据不可改写" in dup.stderr
    assert len(_ledger_lines(run_dir)) == 1


def test_append_rejects_non_english_enums(tmp_path):
    run_dir = _report_dir(tmp_path)
    proc = _append(run_dir, "bad:category", "discover_risks", category="决策")
    assert proc.returncode == 1
    assert "category 非法" in proc.stderr

    proc = _append(run_dir, "bad:step", "unknown_step")
    assert proc.returncode == 1
    assert "step_id" in proc.stderr


def test_append_rejects_path_escape_and_missing_artifact(tmp_path):
    run_dir = _report_dir(tmp_path)
    escaped = _append(run_dir, "x:escape", "extract_change", artifacts=("../outside.md",))
    assert escaped.returncode == 1
    absolute = _append(run_dir, "x:absolute", "extract_change", artifacts=(str(run_dir / "01-req.md"),))
    assert absolute.returncode == 1
    missing = _append(run_dir, "x:missing", "extract_change", artifacts=("nope.md",))
    assert missing.returncode == 1
    assert "不存在" in missing.stderr
    assert _ledger_lines(run_dir) == []


def test_append_requires_evidence_key_and_fact_summary(tmp_path):
    run_dir = _report_dir(tmp_path)
    proc = _run("append", "--report-dir", str(run_dir), "--step-id", "extract_change",
                "--evidence-key", "x:1", "--category", "change",
                "--verification-method", "file", "--strength", "definitive",
                "--polarity", "supporting")
    assert proc.returncode == 2
    assert "--fact-summary" in proc.stderr


def test_append_json_batch_from_stdin_keeps_order(tmp_path):
    run_dir = _report_dir(tmp_path)
    batch = [
        {
            "evidence_key": "risk:R1", "step_id": "discover_risks", "category": "impact",
            "verification_method": "file", "strength": "partial", "polarity": "supporting",
            "fact_summary": "R1 代码证据", "linked_risk_key": "R1", "linked_test_key": None,
            "artifacts": ["03-risk.md"],
        },
        {
            "evidence_key": "risk:R2", "step_id": "discover_risks", "category": "impact",
            "verification_method": "file", "strength": "partial", "polarity": "neutral",
            "fact_summary": "R2 代码证据", "linked_risk_key": "R2", "linked_test_key": None,
            "artifacts": [],
        },
    ]
    proc = _run("append", "--report-dir", str(run_dir), "--json",
                stdin=json.dumps(batch, ensure_ascii=False))
    assert proc.returncode == 0, proc.stderr
    keys = [line["evidence_key"] for line in _ledger_lines(run_dir)]
    assert keys == ["risk:R1", "risk:R2"]


# ── merge ─────────────────────────────────────────────────────────────────


def test_merge_builds_manifest_with_exact_top_level_keys(tmp_path):
    run_dir = _report_dir(tmp_path)
    assert _append(run_dir, "ctx:req", "decompose_requirement", category="context",
                   method="file", artifacts=("01-req.md",)).returncode == 0
    assert _append(run_dir, "change:diff", "extract_change", category="change",
                   method="command").returncode == 0
    proc = _merge(run_dir)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads((run_dir / "evidence-manifest.json").read_text(encoding="utf-8"))
    assert set(payload) == {"schema_version", "run_key", "source_ref", "producer", "context", "evidence"}
    assert payload["schema_version"] == 1
    assert payload["run_key"] == RUN_KEY
    assert payload["source_ref"] == SOURCE_REF
    assert [item["evidence_key"] for item in payload["evidence"]] == ["ctx:req", "change:diff"]
    assert set(payload["producer"]) == {"agent_name", "skill_name", "skill_version"}


def test_merge_includes_device_segment_and_rewrites_paths(tmp_path):
    run_dir = _report_dir(tmp_path)
    shot = run_dir / "device" / "R1" / "device" / "R1-s1-before.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    segment = {
        "schema_version": 1,
        "run_key": RUN_KEY,
        "source_ref": SOURCE_REF,
        "evidence": [
            {
                "evidence_key": "device:R1",
                "step_id": "execute_on_device",
                "category": "execution",
                "verification_method": "device",
                "linked_risk_key": "R1",
                "linked_test_key": None,
                "strength": "definitive",
                "polarity": "supporting",
                "fact_summary": "真机执行 2 步 pass",
                "artifact_refs": [
                    {"path": "device/R1-s1-before.png", "sha256": "0" * 64,
                     "media_type": "image/png", "size_bytes": 0},
                ],
                "occurred_at": "2026-10-27T23:00:00+08:00",
            },
        ],
    }
    (run_dir / "device" / "R1" / "device-evidence.json").write_text(
        json.dumps(segment, ensure_ascii=False), encoding="utf-8",
    )
    proc = _merge(run_dir)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads((run_dir / "evidence-manifest.json").read_text(encoding="utf-8"))
    item = payload["evidence"][0]
    assert item["evidence_key"] == "device:R1"
    ref = item["artifact_refs"][0]
    assert ref["path"] == "device/R1/device/R1-s1-before.png"
    assert ref["sha256"] == hashlib.sha256(shot.read_bytes()).hexdigest()
    assert ref["size_bytes"] == shot.stat().st_size


def test_merge_skips_device_segment_from_other_run(tmp_path):
    run_dir = _report_dir(tmp_path)
    assert _append(run_dir, "ctx:req", "decompose_requirement", category="context").returncode == 0
    segment = {
        "schema_version": 1,
        "run_key": "org_proj/sample-web!1#20261001-000000",
        "source_ref": "org_proj/sample-web!1",
        "evidence": [
            {
                "evidence_key": "device:R9", "step_id": "execute_on_device",
                "category": "execution", "verification_method": "device",
                "linked_risk_key": "R9", "linked_test_key": None,
                "strength": "definitive", "polarity": "supporting",
                "fact_summary": "他轮真机证据", "artifact_refs": [],
                "occurred_at": "2026-10-01T00:00:00+08:00",
            },
        ],
    }
    (run_dir / "device" / "R9").mkdir(parents=True)
    (run_dir / "device" / "R9" / "device-evidence.json").write_text(
        json.dumps(segment, ensure_ascii=False), encoding="utf-8",
    )
    proc = _merge(run_dir)
    assert proc.returncode == 0, proc.stderr
    assert "skipped_device=1" in proc.stdout
    payload = json.loads((run_dir / "evidence-manifest.json").read_text(encoding="utf-8"))
    assert [item["evidence_key"] for item in payload["evidence"]] == ["ctx:req"]


def test_merge_computes_context_hashes_from_repo_files(tmp_path):
    run_dir = _report_dir(tmp_path)
    assert _append(run_dir, "ctx:req", "decompose_requirement", category="context").returncode == 0
    proc = _merge(run_dir)
    assert proc.returncode == 0, proc.stderr
    context = json.loads((run_dir / "evidence-manifest.json").read_text(encoding="utf-8"))["context"]
    assert set(context) == {
        "code_revision", "diff_hash", "rules_version", "knowledge_version", "model",
        "prompt_hash", "agent_definition_hash", "skill_definition_hash",
    }
    agent = ROOT_DIR / ".dsh" / "agents" / "sentinel.md"
    skill = ROOT_DIR / ".dsh" / "skills" / "sentinel" / "SKILL.md"
    assert context["agent_definition_hash"] == hashlib.sha256(agent.read_bytes()).hexdigest()
    assert context["skill_definition_hash"] == hashlib.sha256(skill.read_bytes()).hexdigest()
    assert context["diff_hash"] == hashlib.sha256((run_dir / ".cache" / "diff.raw.txt").read_bytes()).hexdigest()
    assert context["rules_version"] == "v29"
    assert context["code_revision"] == SHA
    assert len(context["prompt_hash"]) == 64


def test_merge_requires_model_identity(tmp_path):
    run_dir = _report_dir(tmp_path)
    assert _append(run_dir, "ctx:req", "decompose_requirement", category="context").returncode == 0
    proc = _run(
        "merge", "--report-dir", str(run_dir),
        "--run-key", RUN_KEY, "--source-ref", SOURCE_REF,
        "--code-revision", SHA, "--knowledge-version", "profile-v1",
        "--rules-meta", str(run_dir / "rules-meta.json"),
    )
    assert proc.returncode == 2
    assert "--model" in proc.stderr
    assert not (run_dir / "evidence-manifest.json").exists()


def test_merge_refreshes_changed_artifact_and_strict_hash_fails(tmp_path):
    run_dir = _report_dir(tmp_path)
    assert _append(run_dir, "ctx:req", "decompose_requirement", category="context",
                   artifacts=("01-req.md",)).returncode == 0
    assert _merge(run_dir).returncode == 0
    before = json.loads((run_dir / "evidence-manifest.json").read_text(encoding="utf-8"))
    first_hash = before["evidence"][0]["artifact_refs"][0]["sha256"]

    (run_dir / "01-req.md").write_text("# 需求口径（修订）\n", encoding="utf-8")
    strict = _merge(run_dir, "--strict-hash")
    assert strict.returncode == 1
    assert "哈希漂移" in strict.stderr

    refreshed = _merge(run_dir)
    assert refreshed.returncode == 0, refreshed.stderr
    assert "refreshed=1" in refreshed.stdout
    after = json.loads((run_dir / "evidence-manifest.json").read_text(encoding="utf-8"))
    new_hash = after["evidence"][0]["artifact_refs"][0]["sha256"]
    assert new_hash != first_hash
    assert new_hash == hashlib.sha256((run_dir / "01-req.md").read_bytes()).hexdigest()


def test_merge_skips_manifest_when_no_evidence_without_blocking(tmp_path):
    """证据缺失不阻断：不产 manifest（视为可选），只 WARN 留痕。"""
    run_dir = _report_dir(tmp_path)
    proc = _merge(run_dir)
    assert proc.returncode == 0, proc.stderr
    assert "EVIDENCE MERGE SKIP no_evidence" in proc.stdout
    assert "evidence_gaps" in proc.stderr
    assert not (run_dir / "evidence-manifest.json").exists()


# ── check / show ──────────────────────────────────────────────────────────


def test_check_step_reports_gap_as_advisory_by_default(tmp_path):
    run_dir = _report_dir(tmp_path)
    assert _append(run_dir, "change:diff", "extract_change", category="change").returncode == 0

    ok = _run("check", "--report-dir", str(run_dir), "--step", "extract_change")
    assert ok.returncode == 0
    assert "EVIDENCE CHECK OK" in ok.stdout

    gap = _run("check", "--report-dir", str(run_dir), "--step", "design_tests")
    assert gap.returncode == 0, "缺证据默认不得阻断流程"
    assert "EVIDENCE CHECK WARN" in gap.stdout
    assert "design_tests" in gap.stderr

    strict = _run("check", "--report-dir", str(run_dir), "--step", "design_tests", "--strict")
    assert strict.returncode == 1
    assert "design_tests" in strict.stderr


def test_check_all_reports_gaps_as_advisory_and_ok_when_complete(tmp_path):
    run_dir = _report_dir(tmp_path)
    assert _append(run_dir, "change:diff", "extract_change", category="change").returncode == 0
    partial = _run("check", "--report-dir", str(run_dir))
    assert partial.returncode == 0, "缺步骤证据默认不得阻断流程"
    assert "EVIDENCE CHECK WARN" in partial.stdout
    assert "缺步骤证据" in partial.stderr and "design_tests" in partial.stderr

    strict = _run("check", "--report-dir", str(run_dir), "--strict")
    assert strict.returncode == 1

    _all_steps(run_dir)
    assert _merge(run_dir).returncode == 0
    full = _run("check", "--report-dir", str(run_dir), "--run-key", RUN_KEY)
    assert full.returncode == 0, full.stderr
    assert "EVIDENCE CHECK OK" in full.stdout


def test_show_reports_per_step_counts(tmp_path):
    run_dir = _report_dir(tmp_path)
    assert _append(run_dir, "risk:R1", "discover_risks", risk="R1").returncode == 0
    assert _append(run_dir, "risk:R2", "discover_risks", risk="R2").returncode == 0
    proc = _run("show", "--report-dir", str(run_dir))
    assert proc.returncode == 0
    assert "items=2" in proc.stdout
    assert "discover_risks" in proc.stdout
    assert "manifest=missing" in proc.stdout
