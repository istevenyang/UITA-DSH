"""`ci/sentinel_test_design.py` 可执行单测设计契约测试。

仅使用临时目录和本地假 Runner；不访问真实业务仓、网络、模型、数据库或应用进程。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = ROOT_DIR / "ci" / "sentinel_test_design.py"
INTENT_REL = Path(".cache/test-intent.json")
EXECUTION_REL = Path(".cache/test-execution.json")
SUMMARY_REL = Path(".cache/test-design-summary.json")


def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True,
                          text=True, check=False)


def _git_repo(path: Path, content: str) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "UITA Test"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "uita@example.invalid"], check=True)
    (path / "src").mkdir()
    (path / "src" / "production.txt").write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "src/production.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "fixture"], check=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def _report(tmp_path: Path) -> Path:
    report = tmp_path / "report"
    (report / ".cache").mkdir(parents=True)
    (report / "tests").mkdir()
    refs = {
        "base-sha": _git_repo(tmp_path / "base-repo", "defective\n"),
        "head-sha": _git_repo(tmp_path / "head-repo", "correct\n"),
    }
    (report / ".cache" / "test-repo-refs.json").write_text(json.dumps(refs), encoding="utf-8")
    (report / ".cache" / "risk-ledger.json").write_text(json.dumps({
        "risks": [{"risk_key": "R1", "route": "unit", "title": "错误缓存必须可重试"}],
    }), encoding="utf-8")
    proc = _cli("init", "--report-dir", str(report))
    assert proc.returncode == 0, proc.stderr
    return report


def _case(kind: str = "adversarial") -> dict:
    case = {
        "test_id": "T1", "risk_key": "R1", "coverage_target": "错误缓存必须可重试",
        "test_kind": kind, "source_bug_id": "local-cache-fallback",
        "change_location": "src/cache.ts::getValue", "feature_behavior": "能力缺失不写入成功缓存",
        "ui_surface": "none", "given": "native 首次返回空", "when": "再次读取设备 ID",
        "then": "再次调用 native 而非返回缓存空值", "mock_surface": ["native adapter"],
        "assertions": ["native 调用次数为 2"], "test_file": "tests/cache.test.ts",
        "test_name": "retries when native is empty",
        "correct_oracle": {"code_ref": "head-sha", "description": "正确实现不缓存能力缺失"},
    }
    if kind == "adversarial":
        case["defective_oracle"] = {
            "code_ref": "base-sha", "description": "缺陷实现把空值写入幂等缓存",
        }
    return case


def _write_intent(report: Path, cases: list[dict] | None = None, unresolved: list[dict] | None = None,
                  source: str = "it('retries', () => { expect(calls).toBe(2) })\n") -> None:
    if cases is None:
        cases = [_case()]
    refs = json.loads((report / ".cache" / "test-repo-refs.json").read_text())
    for case in cases:
        for key in ("correct_oracle", "defective_oracle"):
            if key in case and case[key]["code_ref"] in refs:
                case[key]["code_ref"] = refs[case[key]["code_ref"]]
    (report / "tests" / "cache.test.ts").write_text(source, encoding="utf-8")
    (report / INTENT_REL).write_text(json.dumps({
        "schema_version": 1, "tests": cases, "unresolved": unresolved or [],
    }), encoding="utf-8")


def _freeze(report: Path) -> subprocess.CompletedProcess:
    proc = _cli("freeze", "--report-dir", str(report))
    assert proc.returncode == 0, proc.stderr
    return proc


def _runner(tmp_path: Path) -> Path:
    runner = tmp_path / "fake_runner.py"
    runner.write_text(
        "import pathlib, sys, time\n"
        "mode, output = sys.argv[1], pathlib.Path(sys.argv[2])\n"
        "if mode == 'missing':\n"
        "    raise SystemExit(1)\n"
        "facts = {'green': (1, 0, 0, 0), 'red': (1, 1, 0, 0), 'timeout': (1, 0, 0, 0),\n"
        "         'error': (1, 0, 1, 0), 'skipped': (1, 0, 0, 1), 'other': (1, 0, 0, 0),\n"
        "         'zero': (0, 0, 0, 0)}[mode]\n"
        "tests, failures, errors, skipped = facts\n"
        "body = '<failure>assertion</failure>' if failures else ('<error>boom</error>' if errors else ('<skipped/>' if skipped else ''))\n"
        "name = 'unrelated test' if mode == 'other' else 'retries when native is empty'\n"
        "output.parent.mkdir(parents=True, exist_ok=True)\n"
        "output.write_text(f'<testsuite tests=\"{tests}\" failures=\"{failures}\" errors=\"{errors}\" skipped=\"{skipped}\"><testcase name=\"{name}\">{body}</testcase></testsuite>' if tests else '<testsuite tests=\"0\"/>')\n"
        "if mode == 'timeout':\n"
        "    time.sleep(3)\n"
        "raise SystemExit(1 if mode in ('red', 'error') else 0)\n",
        encoding="utf-8",
    )
    return runner


def _run(report: Path, runner: Path, target: str, code_ref: str, mode: str,
         revision: int = 1, timeout: int = 120) -> subprocess.CompletedProcess:
    result = f".cache/test-runs/T1-r{revision}-{target}.xml"
    refs = json.loads((report / ".cache" / "test-repo-refs.json").read_text())
    actual_ref = refs.get(code_ref, code_ref)
    repo = report.parent / ("base-repo" if target == "defective" else "head-repo")
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "cache.test.ts").write_bytes((report / "tests" / "cache.test.ts").read_bytes())
    return _cli(
        "run", "--report-dir", str(report), "--test-id", "T1", "--target", target,
        "--code-ref", actual_ref, "--result-file", result, "--cwd", str(repo),
        "--executed-test-file", "tests/cache.test.ts", "--timeout-seconds", str(timeout),
        "--", sys.executable, str(runner), mode, str(report / result),
    )


def _execution(report: Path) -> dict:
    return json.loads((report / EXECUTION_REL).read_text(encoding="utf-8"))


def _summary(report: Path) -> dict:
    return json.loads((report / SUMMARY_REL).read_text(encoding="utf-8"))


def test_init_generates_targets_and_intent_without_result_fields(tmp_path):
    report = _report(tmp_path)
    targets = json.loads((report / ".cache/test-design-targets.json").read_text())
    intent = json.loads((report / INTENT_REL).read_text())
    assert targets["required_risks"][0]["risk_key"] == "R1"
    assert intent == {"schema_version": 1, "tests": [], "unresolved": []}
    assert "verification_status" not in (report / INTENT_REL).read_text()


def test_validate_rejects_agent_supplied_actual_result(tmp_path):
    report = _report(tmp_path)
    case = _case()
    case["actual_result"] = "green"
    _write_intent(report, [case])
    proc = _cli("validate", "--report-dir", str(report))
    assert proc.returncode == 1
    assert "未知字段" in proc.stderr and "actual_result" in proc.stderr
    validation = json.loads((report / ".cache/test-intent-validation.json").read_text())
    assert validation["valid"] is False and validation["rejected_test_ids"] == ["T1"]


def test_validate_requires_adversarial_oracle(tmp_path):
    report = _report(tmp_path)
    case = _case()
    del case["defective_oracle"]
    _write_intent(report, [case])
    proc = _cli("validate", "--report-dir", str(report))
    assert proc.returncode == 1 and "defective_oracle" in proc.stderr


def test_validate_rejects_path_escape_and_duplicate_id(tmp_path):
    report = _report(tmp_path)
    escaped = _case()
    escaped["test_file"] = "../outside.test.ts"
    _write_intent(report, [escaped])
    proc = _cli("validate", "--report-dir", str(report))
    assert proc.returncode == 1 and "路径越界" in proc.stderr

    duplicate = _case()
    _write_intent(report, [duplicate, dict(duplicate)])
    proc = _cli("validate", "--report-dir", str(report))
    assert proc.returncode == 1 and "重复" in proc.stderr


def test_freeze_generates_revision_and_is_idempotent(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    first = _execution(report)
    _freeze(report)
    second = _execution(report)
    current = second["tests"][0]
    assert current["current_revision"] == 1
    assert current["revisions"][0]["content_sha256"] == first["tests"][0]["revisions"][0]["content_sha256"]
    assert len(current["revisions"][0]["files"][0]["sha256"]) == 64


def test_static_validation_alone_cannot_verify_or_seal_coverage(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    proc = _cli("seal", "--report-dir", str(report))
    assert proc.returncode == 1
    summary = _summary(report)
    assert summary["tests"][0]["static_status"] == "pass"
    assert summary["tests"][0]["verification_status"] == "pending"
    assert summary["verified_coverage"] == [] and summary["uncovered_risks"] == ["R1"]


def test_adversarial_assertion_red_and_correct_green_is_verified(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    assert _run(report, runner, "defective", "base-sha", "red").returncode == 0
    assert _run(report, runner, "correct", "head-sha", "green").returncode == 0
    proc = _cli("seal", "--report-dir", str(report))
    assert proc.returncode == 0, proc.stderr
    summary = _summary(report)
    assert summary["tests"][0]["adversarial_status"] == "verified"
    assert summary["tests"][0]["verification_status"] == "verified"
    assert summary["verified_coverage"][0]["risk_key"] == "R1"


def test_defective_green_cannot_pass_adversarial_validation(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    _run(report, runner, "defective", "base-sha", "green")
    _run(report, runner, "correct", "head-sha", "green")
    assert _cli("seal", "--report-dir", str(report)).returncode == 1
    summary = _summary(report)
    assert summary["tests"][0]["adversarial_status"] == "failed"
    assert summary["verified_coverage"] == []


def test_junit_error_is_not_adversarial_assertion_red(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    _run(report, runner, "defective", "base-sha", "error")
    _run(report, runner, "correct", "head-sha", "green")
    assert _cli("seal", "--report-dir", str(report)).returncode == 1
    summary = _summary(report)
    assert summary["tests"][0]["adversarial_status"] == "inconclusive"
    assert summary["verified_coverage"] == []


def test_missing_runner_report_cannot_be_green(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    proc = _run(report, runner, "correct", "head-sha", "missing")
    assert proc.returncode == 0 and "environment_failure" in proc.stdout
    assert _cli("seal", "--report-dir", str(report)).returncode == 1
    assert _summary(report)["verified_coverage"] == []


def test_all_skipped_is_inconclusive_not_lock_verified(tmp_path):
    report = _report(tmp_path)
    _write_intent(report, [_case("lock")])
    _freeze(report)
    runner = _runner(tmp_path)
    _run(report, runner, "correct", "head-sha", "skipped")
    assert _cli("seal", "--report-dir", str(report)).returncode == 1
    test = _summary(report)["tests"][0]
    assert test["verification_status"] == "inconclusive"
    assert test["adversarial_status"] == "not_required"


def test_lock_green_does_not_hide_missing_adversarial(tmp_path):
    report = _report(tmp_path)
    _write_intent(report, [_case("lock")])
    _freeze(report)
    runner = _runner(tmp_path)
    _run(report, runner, "correct", "head-sha", "green")
    proc = _cli("seal", "--report-dir", str(report))
    assert proc.returncode == 1
    summary = _summary(report)
    assert summary["tests"][0]["verification_status"] == "verified"
    assert summary["missing_adversarial"] == ["R1"]


def test_unresolved_risk_preserves_gap_and_allows_seal(tmp_path):
    report = _report(tmp_path)
    _write_intent(report, [], [{
        "risk_key": "R1", "reason": "依赖真实硬件状态，无法构造隔离单测", "next_method": "device",
    }])
    _freeze(report)
    proc = _cli("seal", "--report-dir", str(report))
    assert proc.returncode == 0, proc.stderr
    summary = _summary(report)
    assert summary["verified_coverage"] == []
    assert summary["unresolved_risks"] == ["R1"]


def test_changed_file_creates_revision_and_invalidates_old_evidence(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    _run(report, runner, "defective", "base-sha", "red")
    _run(report, runner, "correct", "head-sha", "green")
    _write_intent(report, source="it('retries', () => { expect(calls).toBe(3) })\n")
    _freeze(report)
    proc = _cli("seal", "--report-dir", str(report))
    assert proc.returncode == 1
    execution = _execution(report)
    assert execution["tests"][0]["current_revision"] == 2
    assert len(execution["tests"][0]["revisions"]) == 2
    assert execution["tests"][0]["revisions"][1]["executions"] == []
    assert _summary(report)["counts"]["revisions"] == 2


def test_seal_rejects_unfrozen_intent_change(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    _write_intent(report, source="it('changed', () => { expect(true).toBe(false) })\n")
    proc = _cli("seal", "--report-dir", str(report))
    assert proc.returncode == 1 and "先 freeze" in proc.stderr


def test_run_rejects_wrong_code_ref_and_lock_defective_run(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    proc = _run(report, runner, "correct", "other-sha", "green")
    assert proc.returncode == 1 and "code_ref" in proc.stderr

    _write_intent(report, [_case("lock")])
    _freeze(report)
    proc = _run(report, runner, "defective", "base-sha", "red")
    assert proc.returncode == 1 and "lock" in proc.stderr


def test_result_path_must_be_isolated_junit_xml(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    proc = _cli(
        "run", "--report-dir", str(report), "--test-id", "T1", "--target", "correct",
        "--code-ref", json.loads((report / ".cache" / "test-repo-refs.json").read_text())["head-sha"],
        "--result-file", "tests/result.xml", "--cwd", str(report),
        "--executed-test-file", "tests/cache.test.ts",
        "--", sys.executable, str(runner), "green", str(report / "tests/result.xml"),
    )
    assert proc.returncode == 1 and ".cache/test-runs" in proc.stderr


@pytest.mark.parametrize("mode", ["zero", "skipped"])
def test_zero_or_skipped_junit_never_counts_as_adversarial_red(tmp_path, mode):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    _run(report, runner, "defective", "base-sha", mode)
    assert _cli("seal", "--report-dir", str(report)).returncode == 1
    assert _summary(report)["tests"][0]["adversarial_status"] == "inconclusive"


def test_freeze_does_not_overwrite_corrupted_execution_history(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    original = (report / EXECUTION_REL).read_text(encoding="utf-8")
    (report / EXECUTION_REL).write_text("{broken", encoding="utf-8")
    proc = _cli("freeze", "--report-dir", str(report))
    assert proc.returncode == 1
    assert (report / EXECUTION_REL).read_text(encoding="utf-8") == "{broken"
    assert original != "{broken"


def test_run_removes_stale_junit_before_observing_runner(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    result = report / ".cache/test-runs/T1-r1-correct.xml"
    result.parent.mkdir(parents=True)
    result.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0"/>')
    runner = _runner(tmp_path)
    proc = _run(report, runner, "correct", "head-sha", "missing")
    assert proc.returncode == 0 and "environment_failure" in proc.stdout
    execution = _execution(report)
    artifacts = execution["tests"][0]["revisions"][0]["executions"][0]["artifacts"]
    assert all(item["path"] != ".cache/test-runs/T1-r1-correct.xml" for item in artifacts)


def test_timeout_with_green_junit_is_inconclusive(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    _run(report, runner, "defective", "base-sha", "red")
    proc = _run(report, runner, "correct", "head-sha", "timeout", timeout=1)
    assert proc.returncode == 0 and "inconclusive" in proc.stdout
    assert _cli("seal", "--report-dir", str(report)).returncode == 1
    assert _summary(report)["verified_coverage"] == []


def test_unrelated_junit_testcase_cannot_verify_frozen_test(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    _run(report, runner, "defective", "base-sha", "red")
    _run(report, runner, "correct", "head-sha", "other")
    assert _cli("seal", "--report-dir", str(report)).returncode == 1
    assert _summary(report)["verified_coverage"] == []


def test_run_rejects_unfrozen_file_change_before_execution(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    (report / "tests" / "cache.test.ts").write_text("changed before run\n")
    proc = _run(report, _runner(tmp_path), "correct", "head-sha", "green")
    assert proc.returncode == 1 and "frozen revision" in proc.stderr
    assert _execution(report)["tests"][0]["revisions"][0]["executions"] == []


def test_adversarial_requires_distinct_commits_and_exact_coverage(tmp_path):
    report = _report(tmp_path)
    case = _case()
    case["defective_oracle"]["code_ref"] = "head-sha"
    _write_intent(report, [case])
    proc = _cli("validate", "--report-dir", str(report))
    assert proc.returncode == 1 and "同一 commit" in proc.stderr

    case = _case()
    case["coverage_target"] = "与风险无关的目标"
    _write_intent(report, [case])
    proc = _cli("validate", "--report-dir", str(report))
    assert proc.returncode == 1 and "coverage_target" in proc.stderr


def test_structurally_corrupted_execution_is_not_silently_rewritten(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    execution = _execution(report)
    execution["tests"].append(json.loads(json.dumps(execution["tests"][0])))
    corrupted = json.dumps(execution)
    (report / EXECUTION_REL).write_text(corrupted)
    proc = _cli("freeze", "--report-dir", str(report))
    assert proc.returncode == 1 and "重复" in proc.stderr
    assert (report / EXECUTION_REL).read_text() == corrupted


def test_failed_reseal_invalidates_previous_green_summary(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    _run(report, runner, "defective", "base-sha", "red")
    _run(report, runner, "correct", "head-sha", "green")
    assert _cli("seal", "--report-dir", str(report)).returncode == 0
    assert _summary(report)["ok"] is True
    (report / "tests" / "cache.test.ts").write_text("unfrozen change\n")
    assert _cli("seal", "--report-dir", str(report)).returncode == 1
    summary = _summary(report)
    assert summary["ok"] is False and "先 freeze" in summary["error"]


def test_untracked_production_file_invalidates_code_snapshot(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    (report.parent / "head-repo" / "src" / "untracked.ts").write_text("uncontrolled change\n")
    proc = _run(report, _runner(tmp_path), "correct", "head-sha", "green")
    assert proc.returncode == 1 and "未跟踪" in proc.stderr
    assert _execution(report)["tests"][0]["revisions"][0]["executions"] == []


# ── run-batch：一次进程多条执行，记录口径必须与单条 run 逐字段一致 ──────────────

# 易变字段：
# - occurred_at / execution_no / artifacts：时刻、序号与证据引用，天然不同；
# - command_sha256：两条路径的 JUnit 输出文件名不同 → 命令数组不同（产物命名差异）；
# - code_ref / commit / tree：两个夹具各自 `_git_repo` 生成仓库，commit 哈希必然不同。
# 其余字段（outcome / reason / returncode / junit / runner / target / timed_out /
# executed_files 哈希）即记录口径，必须逐字段相等。
_BATCH_VOLATILE = {
    "occurred_at", "execution_no", "artifacts", "command_sha256", "code_ref", "commit", "tree",
}


def _prepare_target_repo(report: Path, target: str) -> Path:
    repo = report.parent / ("base-repo" if target == "defective" else "head-repo")
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "cache.test.ts").write_bytes((report / "tests" / "cache.test.ts").read_bytes())
    return repo


def _batch_task(report: Path, target: str, mode: str, runner: Path, code_ref: str,
                index: int) -> dict:
    """构造一条 manifest 任务：result_file 显式给出，且 Runner 输出到同一路径。"""
    refs = json.loads((report / ".cache" / "test-repo-refs.json").read_text())
    repo = _prepare_target_repo(report, target)
    result_rel = f".cache/test-runs/T1-{target}-{index}.xml"
    return {
        "test_id": "T1", "target": target, "code_ref": refs.get(code_ref, code_ref),
        "cwd": str(repo), "executed_test_file": "tests/cache.test.ts",
        "result_file": result_rel,
        "command": [sys.executable, str(runner), mode, str(report / result_rel)],
    }


def _batch(report: Path, tasks: list[dict], manifest_name: str = "batch.json") -> subprocess.CompletedProcess:
    manifest = report.parent / manifest_name
    manifest.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    return _cli("run-batch", "--report-dir", str(report), "--manifest", str(manifest))


def _runs(report: Path) -> list[dict]:
    return _execution(report)["tests"][0]["revisions"][0]["executions"]


def _strip_volatile(record: dict) -> dict:
    return {key: value for key, value in record.items() if key not in _BATCH_VOLATILE}


def test_run_batch_records_each_task_like_single_run(tmp_path):
    batch_report = _report(tmp_path)
    _write_intent(batch_report)
    _freeze(batch_report)
    runner = _runner(tmp_path)
    proc = _batch(batch_report, [
        _batch_task(batch_report, "defective", "red", runner, "base-sha", 1),
        _batch_task(batch_report, "correct", "green", runner, "head-sha", 2),
    ])
    assert proc.returncode == 0, proc.stderr
    assert "TEST RUN BATCH OK tasks=2 recorded=2 contract_errors=0" in proc.stdout
    batch_runs = _runs(batch_report)
    assert [run["target"] for run in batch_runs] == ["defective", "correct"]
    assert [run["outcome"] for run in batch_runs] == ["assertion_red", "assertion_green"]
    assert [run["execution_no"] for run in batch_runs] == [1, 2]

    # 单条路径对照组：同一 manifest 的同一任务用 `run` 逐条执行，记录除易变字段外必须相等
    single_root = tmp_path / "single"
    single_root.mkdir()
    single_report = _report(single_root)
    _write_intent(single_report)
    _freeze(single_report)
    single_runner = _runner(single_root)
    _run(single_report, single_runner, "defective", "base-sha", "red")
    _run(single_report, single_runner, "correct", "head-sha", "green")
    assert [_strip_volatile(run) for run in _runs(single_report)] == \
        [_strip_volatile(run) for run in batch_runs]


def test_run_batch_records_are_usable_by_seal(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    assert _batch(report, [
        _batch_task(report, "defective", "red", runner, "base-sha", 1),
        _batch_task(report, "correct", "green", runner, "head-sha", 2),
    ]).returncode == 0
    proc = _cli("seal", "--report-dir", str(report))
    assert proc.returncode == 0, proc.stderr
    assert _summary(report)["ok"] is True


@pytest.mark.parametrize("manifest", [
    {},
    {"tasks": []},
    {"tasks": [{"test_id": "T9", "target": "defective"}]},
    "not-a-list",
])
def test_run_batch_rejects_invalid_manifest_without_writing(tmp_path, manifest):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    task = _batch_task(report, "defective", "red", runner, "base-sha", 1)
    extra = manifest.get("tasks") if isinstance(manifest, dict) else None
    payload = {"tasks": [task, *extra]} if extra else manifest
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = (report / EXECUTION_REL).read_text(encoding="utf-8")
    proc = _cli("run-batch", "--report-dir", str(report), "--manifest", str(path))
    assert proc.returncode == 1
    assert "TEST DESIGN ERROR" in proc.stderr
    assert (report / EXECUTION_REL).read_text(encoding="utf-8") == before


def test_run_batch_rejects_unknown_target_and_missing_command(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    task = _batch_task(report, "defective", "red", runner, "base-sha", 1)
    bad_target = dict(task, target="no-such-target")
    proc = _batch(report, [bad_target], "bad-target.json")
    assert proc.returncode == 1 and "target" in proc.stderr

    no_command = {key: value for key, value in task.items() if key != "command"}
    proc = _batch(report, [no_command], "no-command.json")
    assert proc.returncode == 1 and "command" in proc.stderr
    assert _runs(report) == []


def test_run_batch_continues_after_single_item_contract_error(tmp_path):
    report = _report(tmp_path)
    _write_intent(report)
    _freeze(report)
    runner = _runner(tmp_path)
    good = _batch_task(report, "correct", "green", runner, "head-sha", 2)
    bad = dict(_batch_task(report, "defective", "red", runner, "base-sha", 1))
    bad["code_ref"] = "0" * 40  # 与 defective_oracle 不一致 → 执行期契约错误
    proc = _batch(report, [bad, good])
    assert proc.returncode == 1
    assert "TEST RUN BATCH ITEM FAILED" in proc.stderr and "code_ref" in proc.stderr
    assert "TEST RUN BATCH PARTIAL tasks=2 recorded=1 contract_errors=1" in proc.stdout
    runs = _runs(report)
    assert len(runs) == 1 and runs[0]["target"] == "correct" and runs[0]["outcome"] == "assertion_green"
