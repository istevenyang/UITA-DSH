#!/usr/bin/env python3
"""sentinel 步骤 6 可执行单测设计契约。

Agent 只提交 .cache/test-intent.json；本脚本独占 revision、真实执行记录、
verification_status 和覆盖统计。真实执行统一使用参数数组（shell=False），
结果只接受本次运行新生成的 JUnit XML，避免把文本声明当作执行事实。

执行入口两条，记录口径完全一致：`run`（单条，`--test-id` + `--` 命令数组）与
`run-batch`（一次进程按 manifest 循环多条，只为省掉逐条的进程启动开销）。批量
清单不合法 → 整批拒绝且零写入；某条执行期违约 → 只记该条失败并继续其余条目。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

SCHEMA_VERSION = 1
INTENT_REL = Path(".cache/test-intent.json")
TARGETS_REL = Path(".cache/test-design-targets.json")
VALIDATION_REL = Path(".cache/test-intent-validation.json")
EXECUTION_REL = Path(".cache/test-execution.json")
SUMMARY_REL = Path(".cache/test-design-summary.json")
RUNS_REL = Path(".cache/test-runs")
TEST_ID_RE = re.compile(r"^T[1-9]\d*$")
RISK_KEY_RE = re.compile(r"^R[1-9]\d*$")
CODE_REF_RE = re.compile(r"^[0-9a-fA-F]{40}$")
RESULT_FIELDS = {
    "actual_result", "verification_status", "risk_eliminated", "execution_status",
    "adversarial_status", "static_status", "content_sha256", "revision",
}


class ContractError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _pairs_no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"JSON 字段重复：{key}")
        result[key] = value
    return result


def _read_json(path: Path) -> dict:
    if not path.is_file():
        raise ContractError(f"文件不存在：{path}")
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ContractError(f"JSON 超过 2 MiB：{path.name}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs_no_duplicates)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"JSON 无法解析：{path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"JSON 顶层必须是对象：{path.name}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _invalidate_summary(report_dir: Path, reason: str) -> None:
    if (report_dir / SUMMARY_REL).is_file():
        _write_json(report_dir / SUMMARY_REL, {
            "schema_version": SCHEMA_VERSION, "ok": False, "error": reason, "sealed_at": _now(),
        })


def _safe_path(report_dir: Path, value: object, field: str) -> tuple[str, Path]:
    if not isinstance(value, str) or not value.strip() or "\\" in value or "\x00" in value:
        raise ContractError(f"{field} 必须是报告目录内的 POSIX 相对路径")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts:
        raise ContractError(f"{field} 路径越界：{value!r}")
    normalized = pure.as_posix()
    path = report_dir / normalized
    resolved = path.resolve()
    try:
        resolved.relative_to(report_dir.resolve())
    except ValueError as exc:
        raise ContractError(f"{field} 路径越界：{value!r}") from exc
    return normalized, path


def _artifact(report_dir: Path, path: Path, normalized: str) -> dict:
    if not path.is_file():
        raise ContractError(f"Artifact 不存在：{normalized}")
    data = path.read_bytes()
    return {"path": normalized, "sha256": _sha256_bytes(data), "size_bytes": len(data)}


def _keys(value: object, required: set[str], optional: set[str], where: str) -> dict:
    if not isinstance(value, dict):
        raise ContractError(f"{where} 必须是对象")
    unknown = set(value) - required - optional
    missing = required - set(value)
    if unknown:
        raise ContractError(f"{where} 含未知字段：{sorted(unknown)}")
    if missing:
        raise ContractError(f"{where} 缺字段：{sorted(missing)}")
    return value


def _text(value: object, where: str, limit: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ContractError(f"{where} 必须为 1..{limit} 字符的非空字符串")
    if "://" in value or any(token in value.lower() for token in ("token=", "password=", "cookie:")):
        raise ContractError(f"{where} 不得包含 URL 或凭据形态")
    return value.strip()


def _text_list(value: object, where: str, *, nonempty: bool) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise ContractError(f"{where} 必须为{'非空' if nonempty else ''}字符串数组")
    return [_text(item, f"{where}[]", 1000) for item in value]


def _code_ref(value: object, where: str) -> str:
    if not isinstance(value, str) or not CODE_REF_RE.fullmatch(value):
        raise ContractError(f"{where} 必须为安全代码/变异标识，收到 {value!r}")
    return value


def _oracle(value: object, where: str) -> dict:
    value = _keys(value, {"code_ref", "description"}, set(), where)
    return {
        "code_ref": _code_ref(value["code_ref"], f"{where}.code_ref"),
        "description": _text(value["description"], f"{where}.description"),
    }


def _load_targets(report_dir: Path) -> list[dict]:
    targets = _read_json(report_dir / TARGETS_REL)
    _keys(targets, {"schema_version", "required_risks"}, set(), "targets")
    if targets["schema_version"] != SCHEMA_VERSION or not isinstance(targets["required_risks"], list):
        raise ContractError("targets schema_version/required_risks 非法")
    required_pairs = set()
    for index, item in enumerate(targets["required_risks"]):
        item = _keys(item, {"risk_key", "route", "coverage_target"}, set(), f"targets[{index}]")
        if not RISK_KEY_RE.fullmatch(item["risk_key"]) or item["route"] not in ("unit", "unit+device"):
            raise ContractError(f"targets[{index}] 风险或路由非法")
        _text(item["coverage_target"], f"targets[{index}].coverage_target")
        pair = (item["risk_key"], item["coverage_target"])
        if pair in required_pairs:
            raise ContractError(f"targets 覆盖目标重复：{pair}")
        required_pairs.add(pair)
    return targets["required_risks"]


def _validate_intent(report_dir: Path) -> tuple[dict, list[str], list[str]]:
    errors: list[str] = []
    rejected: list[str] = []
    _invalidate_summary(report_dir, "intent_or_validation_changed")
    try:
        targets = _load_targets(report_dir)
        required = {item["risk_key"] for item in targets}
        required_pairs = {(item["risk_key"], item["coverage_target"]) for item in targets}
        intent = _read_json(report_dir / INTENT_REL)
        _keys(intent, {"schema_version", "tests", "unresolved"}, set(), "intent")
        if intent["schema_version"] != SCHEMA_VERSION:
            raise ContractError("intent.schema_version 必须为 1")
        if not isinstance(intent["tests"], list) or not isinstance(intent["unresolved"], list):
            raise ContractError("intent.tests/unresolved 必须为数组")
    except ContractError as exc:
        report = {"schema_version": SCHEMA_VERSION, "valid": False, "errors": [str(exc)],
                  "rejected_test_ids": [], "candidate_count": 0, "validated_at": _now()}
        _write_json(report_dir / VALIDATION_REL, report)
        return intent if "intent" in locals() else {}, [str(exc)], []

    seen_ids: set[str] = set()
    seen_names: set[tuple[str, str]] = set()
    for index, raw in enumerate(intent["tests"]):
        test_id = raw.get("test_id", f"tests[{index}]") if isinstance(raw, dict) else f"tests[{index}]"
        try:
            test = _keys(raw, {
                "test_id", "risk_key", "coverage_target", "test_kind", "source_bug_id",
                "change_location", "feature_behavior", "ui_surface", "given", "when", "then",
                "mock_surface", "assertions", "test_file", "test_name", "correct_oracle",
            }, {"defective_oracle"}, f"tests[{index}]")
            if not TEST_ID_RE.fullmatch(test["test_id"]):
                raise ContractError(f"{test_id} 必须形如 T<n>")
            test_id = test["test_id"]
            if test_id in seen_ids:
                raise ContractError(f"test_id 重复：{test_id}")
            seen_ids.add(test_id)
            if not RISK_KEY_RE.fullmatch(test["risk_key"]) or test["risk_key"] not in required:
                raise ContractError(f"{test_id}.risk_key 不在待设计风险清单中")
            for field in ("coverage_target", "source_bug_id", "change_location", "feature_behavior",
                          "ui_surface", "given", "when", "then", "test_name"):
                _text(test[field], f"{test_id}.{field}")
            if (test["risk_key"], test["coverage_target"]) not in required_pairs:
                raise ContractError(f"{test_id}.coverage_target 与风险目标清单不一致")
            if test["test_kind"] not in ("adversarial", "lock"):
                raise ContractError(f"{test_id}.test_kind 只允许 adversarial|lock")
            _text_list(test["mock_surface"], f"{test_id}.mock_surface", nonempty=False)
            _text_list(test["assertions"], f"{test_id}.assertions", nonempty=True)
            normalized, file_path = _safe_path(report_dir, test["test_file"], f"{test_id}.test_file")
            if not normalized.startswith("tests/") or not file_path.is_file():
                raise ContractError(f"{test_id}.test_file 必须是已存在的 tests/ 内文件")
            pair = (normalized, test["test_name"])
            if pair in seen_names:
                raise ContractError(f"测试文件和测试名重复：{pair}")
            seen_names.add(pair)
            _oracle(test["correct_oracle"], f"{test_id}.correct_oracle")
            if test["test_kind"] == "adversarial":
                if "defective_oracle" not in test:
                    raise ContractError(f"{test_id} 对抗测试缺 defective_oracle")
                _oracle(test["defective_oracle"], f"{test_id}.defective_oracle")
                if test["defective_oracle"]["code_ref"] == test["correct_oracle"]["code_ref"]:
                    raise ContractError(f"{test_id} defective/correct 不得使用同一 commit")
            elif test.get("defective_oracle") is not None:
                raise ContractError(f"{test_id} lock 测试不得声明 defective_oracle")
        except ContractError as exc:
            errors.append(str(exc))
            rejected.append(str(test_id))

    unresolved_seen: set[str] = set()
    for index, raw in enumerate(intent["unresolved"]):
        try:
            item = _keys(raw, {"risk_key", "reason", "next_method"}, set(), f"unresolved[{index}]")
            risk_key = item["risk_key"]
            if risk_key not in required or risk_key in unresolved_seen:
                raise ContractError(f"unresolved[{index}].risk_key 非法或重复")
            unresolved_seen.add(risk_key)
            _text(item["reason"], f"unresolved[{index}].reason")
            if item["next_method"] not in ("device", "functional", "manual", "test_advice"):
                raise ContractError(f"unresolved[{index}].next_method 非法")
        except ContractError as exc:
            errors.append(str(exc))

    report = {
        "schema_version": SCHEMA_VERSION,
        "valid": not errors,
        "errors": errors,
        "rejected_test_ids": rejected,
        "candidate_count": len(intent["tests"]),
        "validated_at": _now(),
    }
    _write_json(report_dir / VALIDATION_REL, report)
    return intent, errors, rejected


def _require_valid_intent(report_dir: Path) -> dict:
    intent, errors, _ = _validate_intent(report_dir)
    if errors:
        raise ContractError("测试意图校验失败：" + "；".join(errors[:5]))
    return intent


def _file_facts(report_dir: Path, test: dict) -> list[dict]:
    normalized, path = _safe_path(report_dir, test["test_file"], "test_file")
    return [_artifact(report_dir, path, normalized)]


def _revision_fingerprint(test: dict, files: list[dict]) -> str:
    return _sha256_bytes(_canonical({"intent": test, "files": files}))


def _load_execution(report_dir: Path) -> dict:
    value = _read_json(report_dir / EXECUTION_REL)
    _keys(value, {"schema_version", "tests", "updated_at"}, set(), "execution")
    if value["schema_version"] != SCHEMA_VERSION or not isinstance(value["tests"], list):
        raise ContractError("execution schema_version/tests 非法")
    seen_tests = set()
    for test in value["tests"]:
        _keys(test, {"test_id", "active", "current_revision", "revisions"}, set(), "execution.test")
        test_id = test["test_id"]
        if not isinstance(test_id, str) or not TEST_ID_RE.fullmatch(test_id) or test_id in seen_tests:
            raise ContractError(f"execution test_id 非法或重复：{test_id!r}")
        seen_tests.add(test_id)
        revisions = test["revisions"]
        if not isinstance(test["active"], bool) or not isinstance(revisions, list) or not revisions:
            raise ContractError(f"{test_id} revisions/active 非法")
        if test["current_revision"] != len(revisions):
            raise ContractError(f"{test_id} current_revision 损坏")
        for position, revision in enumerate(revisions, 1):
            _keys(revision, {"revision", "intent", "files", "content_sha256", "executions", "frozen_at"},
                  set(), f"{test_id}.revision")
            if revision["revision"] != position or not isinstance(revision["intent"], dict):
                raise ContractError(f"{test_id} revision 编号/意图损坏")
            if not isinstance(revision["content_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", revision["content_sha256"]):
                raise ContractError(f"{test_id} revision 哈希损坏")
            if not isinstance(revision["files"], list) or len(revision["files"]) != 1 or not isinstance(revision["executions"], list):
                raise ContractError(f"{test_id} files/executions 损坏")
            file_fact = revision["files"][0]
            if not isinstance(file_fact, dict) or not re.fullmatch(r"[0-9a-f]{64}", file_fact.get("sha256", "")):
                raise ContractError(f"{test_id} revision 文件哈希损坏")
            for no, run in enumerate(revision["executions"], 1):
                _keys(run, {
                    "execution_no", "target", "code_ref", "commit", "tree", "runner", "command_sha256",
                    "returncode", "timed_out", "outcome", "reason", "junit", "artifacts", "executed_files", "occurred_at",
                }, set(), f"{test_id}.execution")
                if run["execution_no"] != no or run["target"] not in ("defective", "correct"):
                    raise ContractError(f"{test_id} execution 编号/target 损坏")
                if run["outcome"] not in ("assertion_red", "assertion_green", "environment_failure", "inconclusive"):
                    raise ContractError(f"{test_id} execution outcome 损坏")
                if not isinstance(run["timed_out"], bool) or not isinstance(run["artifacts"], list) or not isinstance(run["executed_files"], list):
                    raise ContractError(f"{test_id} execution 证据字段损坏")
    return value


def _latest_run(revision: dict, target: str) -> dict | None:
    runs = [run for run in revision["executions"] if run["target"] == target]
    return max(runs, key=lambda run: run["execution_no"]) if runs else None


def _project_test(test: dict) -> dict:
    revision = test["revisions"][test["current_revision"] - 1]
    intent = revision["intent"]
    defective = _latest_run(revision, "defective")
    correct = _latest_run(revision, "correct")
    latest = max(revision["executions"], key=lambda run: run["execution_no"], default=None)
    execution_status = latest["outcome"] if latest else "not_run"
    if intent["test_kind"] == "lock":
        adversarial_status = "not_required"
        if correct and correct["outcome"] == "assertion_green":
            verification = "verified"
        elif correct and correct["outcome"] in ("environment_failure", "inconclusive"):
            verification = "inconclusive"
        else:
            verification = "unverified"
    else:
        if not defective:
            adversarial_status = "pending"
        elif defective["outcome"] in ("environment_failure", "inconclusive"):
            adversarial_status = "inconclusive"
        elif defective["outcome"] != "assertion_red":
            adversarial_status = "failed"
        elif correct and correct["outcome"] == "assertion_green":
            adversarial_status = "verified"
        elif correct and correct["outcome"] in ("environment_failure", "inconclusive", "assertion_red"):
            adversarial_status = "inconclusive"
        else:
            adversarial_status = "pending"
        if adversarial_status == "verified":
            verification = "verified"
        elif adversarial_status == "inconclusive":
            verification = "inconclusive"
        elif adversarial_status == "failed":
            verification = "unverified"
        else:
            verification = "pending"
    return {
        "test_id": test["test_id"], "active": test["active"], "revision": revision["revision"],
        "risk_key": intent["risk_key"], "coverage_target": intent["coverage_target"],
        "test_kind": intent["test_kind"], "static_status": "pass",
        "execution_status": execution_status, "adversarial_status": adversarial_status,
        "verification_status": verification,
    }


def _freeze(report_dir: Path) -> dict:
    intent = _require_valid_intent(report_dir)
    execution_path = report_dir / EXECUTION_REL
    if execution_path.is_file():
        execution = _load_execution(report_dir)
    else:
        execution = {"schema_version": SCHEMA_VERSION, "tests": [], "updated_at": _now()}
    previous = {item["test_id"]: item for item in execution["tests"]}
    active_ids = {test["test_id"] for test in intent["tests"]}
    result_tests = []
    for test in intent["tests"]:
        files = _file_facts(report_dir, test)
        fingerprint = _revision_fingerprint(test, files)
        old = previous.get(test["test_id"])
        if old:
            revisions = old["revisions"]
            current = revisions[old["current_revision"] - 1]
            if current["content_sha256"] == fingerprint:
                revisions = old["revisions"]
                revision_no = old["current_revision"]
            else:
                revision_no = old["current_revision"] + 1
                revisions = old["revisions"] + [{
                    "revision": revision_no, "intent": test, "files": files,
                    "content_sha256": fingerprint, "executions": [], "frozen_at": _now(),
                }]
        else:
            revision_no = 1
            revisions = [{
                "revision": 1, "intent": test, "files": files,
                "content_sha256": fingerprint, "executions": [], "frozen_at": _now(),
            }]
        result_tests.append({"test_id": test["test_id"], "active": True,
                             "current_revision": revision_no, "revisions": revisions})
    for test_id, old in previous.items():
        if test_id not in active_ids:
            old = dict(old)
            old["active"] = False
            result_tests.append(old)
    execution = {"schema_version": SCHEMA_VERSION, "tests": result_tests, "updated_at": _now()}
    _write_json(report_dir / EXECUTION_REL, execution)
    return execution


def _find_current_test(execution: dict, test_id: str) -> tuple[dict, dict]:
    for test in execution["tests"]:
        if test["test_id"] == test_id and test["active"]:
            return test, test["revisions"][test["current_revision"] - 1]
    raise ContractError(f"活动测试不存在：{test_id}")


def _classify_junit(result_path: Path, returncode: int | None, test_name: str,
                    timed_out: bool) -> tuple[str, str, dict]:
    if timed_out:
        return "inconclusive", "Runner 超时；任何已写 JUnit 均不作为红绿证据", {}
    if not result_path.is_file():
        return ("environment_failure" if returncode else "inconclusive",
                "Runner 未生成本次 JUnit XML", {})
    try:
        root = ET.parse(result_path).getroot()
        matches = [case for case in root.iter("testcase") if case.attrib.get("name") == test_name]
    except ET.ParseError as exc:
        return "inconclusive", f"JUnit XML 无法解析：{exc}", {}
    if len(matches) != 1:
        return "inconclusive", f"JUnit 必须唯一匹配冻结 test_name，实际匹配 {len(matches)}", {}
    case = matches[0]
    facts = {
        "tests": 1,
        "failures": len(case.findall("failure")),
        "errors": len(case.findall("error")),
        "skipped": len(case.findall("skipped")),
    }
    if facts["errors"]:
        return "inconclusive", "目标 testcase 含 error，不按断言红处理", facts
    if facts["skipped"]:
        return "inconclusive", "目标 testcase 被 skipped", facts
    if facts["failures"]:
        return "assertion_red", "目标 testcase 含 assertion failure", facts
    if returncode:
        return "inconclusive", f"Runner 退出码 {returncode} 且目标无失败明细", facts
    return "assertion_green", "目标 testcase 已执行且通过", facts


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", "-C", str(cwd), *args], stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
                               check=False, shell=False)
    if completed.returncode:
        raise ContractError(f"Git 代码身份校验失败：{completed.stderr.decode(errors='replace').strip()}")
    return completed.stdout.decode().strip()


def _git_facts(cwd: Path, code_ref: str, executed_rel: str) -> dict:
    head = _git(cwd, "rev-parse", "HEAD")
    if head.lower() != code_ref.lower():
        raise ContractError(f"code_ref 不是 cwd 实际 HEAD：期望 {code_ref}，实际 {head}")
    tree = _git(cwd, "rev-parse", "HEAD^{tree}")
    prefix = _git(cwd, "rev-parse", "--show-prefix")
    repo_test_path = prefix + executed_rel
    status_lines = filter(None, _git(cwd, "status", "--porcelain=v1", "--untracked-files=all").splitlines())
    changed = set()
    for line in status_lines:
        changed.add(line[3:].split(" -> ")[-1])
    if changed - {repo_test_path}:
        raise ContractError(f"代码树含候选测试外的未提交/未跟踪变更：{sorted(changed - {repo_test_path})}")
    return {"commit": head, "tree": tree}


def _executed_path(cwd: Path, value: str) -> tuple[str, Path]:
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts or "\\" in value:
        raise ContractError("executed_test_file 必须是 cwd 内 POSIX 相对路径")
    normalized = pure.as_posix()
    path = (cwd / normalized).resolve()
    try:
        path.relative_to(cwd)
    except ValueError as exc:
        raise ContractError("executed_test_file 路径越界") from exc
    if not path.is_file():
        raise ContractError(f"executed_test_file 不存在：{normalized}")
    return normalized, path


def _run_test(report_dir: Path, args: argparse.Namespace) -> dict:
    _invalidate_summary(report_dir, "execution_changed")
    execution = _load_execution(report_dir)
    test, revision = _find_current_test(execution, args.test_id)
    intent = revision["intent"]
    if _revision_fingerprint(intent, _file_facts(report_dir, intent)) != revision["content_sha256"]:
        raise ContractError("执行前测试意图/文件与 frozen revision 不一致")
    if args.target == "defective" and intent["test_kind"] != "adversarial":
        raise ContractError("lock 测试不接受 defective 执行")
    oracle = intent[f"{args.target}_oracle"]
    expected_ref = oracle["code_ref"]
    if args.code_ref != expected_ref:
        raise ContractError(f"code_ref 与 {args.target}_oracle 不一致：期望 {expected_ref}")
    result_rel, result_path = _safe_path(report_dir, args.result_file, "result_file")
    if not result_rel.startswith(RUNS_REL.as_posix() + "/") or result_path.suffix != ".xml":
        raise ContractError("result_file 必须位于 .cache/test-runs/ 且为 .xml")
    cwd = Path(args.cwd).resolve()
    if not cwd.is_dir():
        raise ContractError("cwd 不是已存在目录")
    executed_rel, executed_path = _executed_path(cwd, args.executed_test_file)
    executed_before = _artifact(cwd, executed_path, executed_rel)
    if executed_before["sha256"] != revision["files"][0]["sha256"]:
        raise ContractError("实际执行测试文件哈希与 frozen revision 不一致")
    code_facts = _git_facts(cwd, args.code_ref, executed_rel)
    if not args.command:
        raise ContractError("必须提供 Runner 命令（-- 后为参数数组）")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    if result_path.exists():
        result_path.unlink()
    stdout_path = result_path.with_suffix(".stdout.log")
    stderr_path = result_path.with_suffix(".stderr.log")
    returncode: int | None = None
    timed_out = False
    try:
        completed = subprocess.run(
            args.command, cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=args.timeout_seconds, check=False, shell=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.output or b""
        stderr = exc.stderr or b""
        stderr += f"\nRunner 超时：{args.timeout_seconds}s".encode()
    stdout_path.write_bytes(stdout)
    stderr_path.write_bytes(stderr)
    outcome, reason, junit = _classify_junit(result_path, returncode, intent["test_name"], timed_out)
    executed_after = _artifact(cwd, executed_path, executed_rel)
    content_stable = _revision_fingerprint(intent, _file_facts(report_dir, intent)) == revision["content_sha256"]
    content_stable = content_stable and executed_after["sha256"] == executed_before["sha256"]
    try:
        content_stable = content_stable and _git(cwd, "rev-parse", "HEAD") == code_facts["commit"]
    except ContractError:
        content_stable = False
    if not content_stable:
        outcome = "inconclusive"
        reason = "执行期间测试文件或代码 HEAD 变化"
    numbers = [run["execution_no"] for run in revision["executions"]]
    execution_no = max(numbers, default=0) + 1
    artifacts = []
    for path, normalized in ((result_path, result_rel),
                             (stdout_path, stdout_path.relative_to(report_dir).as_posix()),
                             (stderr_path, stderr_path.relative_to(report_dir).as_posix())):
        if path.is_file():
            artifacts.append(_artifact(report_dir, path, normalized))
    record = {
        "execution_no": execution_no, "target": args.target, "code_ref": args.code_ref,
        "commit": code_facts["commit"], "tree": code_facts["tree"],
        "runner": Path(args.command[0]).name, "command_sha256": _sha256_bytes(_canonical(args.command)),
        "returncode": returncode, "timed_out": timed_out, "outcome": outcome, "reason": reason, "junit": junit,
        "artifacts": artifacts, "executed_files": [executed_after], "occurred_at": _now(),
    }
    revision["executions"].append(record)
    execution["updated_at"] = _now()
    _write_json(report_dir / EXECUTION_REL, execution)
    return record


BATCH_REQUIRED_KEYS = {"test_id", "target", "code_ref", "cwd", "executed_test_file", "command"}
BATCH_OPTIONAL_KEYS = {"result_file", "timeout_seconds"}


def _load_batch_manifest(report_dir: Path, manifest_path: Path) -> list[dict]:
    """校验并归一化 run-batch 清单：清单不合法 → 整批拒绝（零写入）；单条执行期失败 → 只记该条。"""
    if not manifest_path.is_file():
        raise ContractError(f"manifest 不存在：{manifest_path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(f"manifest 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list) or not payload["tasks"]:
        raise ContractError("manifest 必须形如 {\"tasks\": [...]} 且非空")
    execution = _load_execution(report_dir)
    normalized: list[dict] = []
    for index, task in enumerate(payload["tasks"], 1):
        where = f"tasks[{index}]"
        if not isinstance(task, dict):
            raise ContractError(f"{where} 必须是对象")
        _keys(task, BATCH_REQUIRED_KEYS, BATCH_OPTIONAL_KEYS, where)
        test_id = task["test_id"]
        if not isinstance(test_id, str) or not TEST_ID_RE.fullmatch(test_id):
            raise ContractError(f"{where}.test_id 非法：{test_id!r}")
        # 提前确认活动测试存在：清单错误必须在任何执行/写入之前暴露
        _find_current_test(execution, test_id)
        if task["target"] not in ("defective", "correct"):
            raise ContractError(f"{where}.target 只允许 defective/correct")
        command = task["command"]
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ContractError(f"{where}.command 必须是非空字符串数组")
        timeout = task.get("timeout_seconds", 120)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 3600:
            raise ContractError(f"{where}.timeout_seconds 必须位于 1..3600")
        result_file = task.get(
            "result_file",
            (RUNS_REL / f"{test_id}-{task['target']}-{index}.xml").as_posix(),
        )
        normalized.append({
            "test_id": test_id,
            "target": task["target"],
            "code_ref": _code_ref(task["code_ref"], f"{where}.code_ref"),
            "cwd": task["cwd"],
            "executed_test_file": task["executed_test_file"],
            "command": list(command),
            "timeout_seconds": timeout,
            "result_file": result_file,
        })
    return normalized


def _batch_namespace(task: dict) -> argparse.Namespace:
    """与单条 `run` 完全同构的参数对象：批量只换外层循环，执行与记录路径复用 `_run_test`。"""
    return argparse.Namespace(
        test_id=task["test_id"], target=task["target"], code_ref=task["code_ref"],
        result_file=task["result_file"], cwd=task["cwd"],
        executed_test_file=task["executed_test_file"], command=task["command"],
        timeout_seconds=task["timeout_seconds"],
    )


def _run_batch(report_dir: Path, manifest_path: Path) -> dict:
    tasks = _load_batch_manifest(report_dir, manifest_path)
    records: list[dict] = []
    failures: list[dict] = []
    for task in tasks:
        try:
            record = _run_test(report_dir, _batch_namespace(task))
        except ContractError as exc:
            failures.append({"test_id": task["test_id"], "target": task["target"], "reason": str(exc)})
            print(f"TEST RUN BATCH ITEM FAILED test={task['test_id']} target={task['target']} "
                  f"reason={exc}", file=sys.stderr)
            continue
        records.append({
            "test_id": task["test_id"], "target": record["target"],
            "execution_no": record["execution_no"], "outcome": record["outcome"],
            "reason": record["reason"],
        })
        print(f"TEST RUN RECORDED test={task['test_id']} target={task['target']} "
              f"outcome={record['outcome']} reason={record['reason']}")
    return {"records": records, "failures": failures}


def _seal(report_dir: Path) -> dict:
    _invalidate_summary(report_dir, "seal_in_progress")
    execution = _load_execution(report_dir)
    intent = _require_valid_intent(report_dir)
    current_intents = {test["test_id"]: test for test in intent["tests"]}
    for test in execution["tests"]:
        if not test["active"]:
            continue
        revision = test["revisions"][test["current_revision"] - 1]
        current = current_intents.get(test["test_id"])
        if current is None or _revision_fingerprint(current, _file_facts(report_dir, current)) != revision["content_sha256"]:
            raise ContractError(f"{test['test_id']} 意图或文件已变化，必须先 freeze 新 revision")
        for run in revision["executions"]:
            if run["commit"].lower() != run["code_ref"].lower() or len(run["executed_files"]) != 1:
                raise ContractError(f"{test['test_id']} 执行代码/文件身份损坏")
            if run["executed_files"][0].get("sha256") != revision["files"][0]["sha256"]:
                raise ContractError(f"{test['test_id']} 执行文件与 revision 不一致")
    projections = [_project_test(test) for test in execution["tests"] if test["active"]]
    targets = _load_targets(report_dir)
    required = {item["risk_key"] for item in targets}
    required_pairs = {(item["risk_key"], item["coverage_target"]) for item in targets}
    verified_coverage = sorted({
        (item["risk_key"], item["coverage_target"]) for item in projections
        if item["verification_status"] == "verified"
    })
    verified_pairs = set(verified_coverage)
    covered = {risk for risk in required if all(pair in verified_pairs for pair in required_pairs if pair[0] == risk)}
    adversarial_verified = {(item["risk_key"], item["coverage_target"]) for item in projections
                            if item["test_kind"] == "adversarial" and item["adversarial_status"] == "verified"}
    unresolved = {item["risk_key"] for item in intent["unresolved"]}
    uncovered_pairs = sorted(pair for pair in required_pairs - verified_pairs if pair[0] not in unresolved)
    uncovered = sorted(required - covered - unresolved)
    missing_adversarial = sorted({pair[0] for pair in required_pairs - adversarial_verified
                                  if pair[0] not in unresolved})
    validation = _read_json(report_dir / VALIDATION_REL)
    counts = {
        "active": len(projections),
        "adversarial": sum(item["test_kind"] == "adversarial" for item in projections),
        "lock": sum(item["test_kind"] == "lock" for item in projections),
        "verified": sum(item["verification_status"] == "verified" for item in projections),
        "unverified": sum(item["verification_status"] == "unverified" for item in projections),
        "inconclusive": sum(item["verification_status"] == "inconclusive" for item in projections),
        "pending": sum(item["verification_status"] == "pending" for item in projections),
        "rejected_candidates": validation["candidate_count"] - len(projections),
        "revisions": sum(len(test["revisions"]) for test in execution["tests"]),
    }
    summary = {
        "schema_version": SCHEMA_VERSION,
        "ok": not uncovered and not missing_adversarial,
        "counts": counts,
        "tests": projections,
        "verified_coverage": [{"risk_key": risk, "coverage_target": target}
                              for risk, target in verified_coverage],
        "uncovered_risks": uncovered,
        "uncovered_coverage": [{"risk_key": risk, "coverage_target": target}
                               for risk, target in uncovered_pairs],
        "missing_adversarial": missing_adversarial,
        "unresolved_risks": sorted(unresolved),
        "source_digests": {path.as_posix(): _sha256_bytes((report_dir / path).read_bytes())
                           for path in (TARGETS_REL, INTENT_REL, EXECUTION_REL)},
        "sealed_at": _now(),
    }
    _write_json(report_dir / SUMMARY_REL, summary)
    return summary


def _init(report_dir: Path) -> None:
    ledger = _read_json(report_dir / ".cache/risk-ledger.json")
    risks = ledger.get("risks")
    if not isinstance(risks, list):
        raise ContractError("risk-ledger.json 缺 risks[]")
    required = []
    for risk in risks:
        if not isinstance(risk, dict) or risk.get("route") not in ("unit", "unit+device"):
            continue
        risk_key = risk.get("risk_key")
        if not isinstance(risk_key, str) or not RISK_KEY_RE.fullmatch(risk_key):
            raise ContractError(f"单测路由风险 risk_key 非法：{risk_key!r}")
        coverage = risk.get("coverage_target") or risk.get("title") or "未命名覆盖目标"
        required.append({"risk_key": risk_key, "route": risk["route"], "coverage_target": coverage})
    required.sort(key=lambda item: int(item["risk_key"][1:]))
    target_doc = {"schema_version": SCHEMA_VERSION, "required_risks": required}
    target_path = report_dir / TARGETS_REL
    if target_path.is_file() and _read_json(target_path) != target_doc:
        raise ContractError("待设计风险清单已变化；不得用 init 覆盖既有测试契约")
    _write_json(target_path, target_doc)
    intent_path = report_dir / INTENT_REL
    if not intent_path.exists():
        _write_json(intent_path, {"schema_version": SCHEMA_VERSION, "tests": [], "unresolved": []})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("init", "validate", "freeze", "seal"):
        child = sub.add_parser(name)
        child.add_argument("--report-dir", required=True)
    run = sub.add_parser("run")
    run.add_argument("--report-dir", required=True)
    run.add_argument("--test-id", required=True)
    run.add_argument("--target", choices=("defective", "correct"), required=True)
    run.add_argument("--code-ref", required=True)
    run.add_argument("--result-file", required=True)
    run.add_argument("--cwd", required=True)
    run.add_argument("--executed-test-file", required=True)
    run.add_argument("--timeout-seconds", type=int, default=120)
    run.add_argument("command", nargs=argparse.REMAINDER)
    batch = sub.add_parser("run-batch", help="一次进程执行多条 run（记录结构与 run 逐字段一致）")
    batch.add_argument("--report-dir", required=True)
    batch.add_argument("--manifest", required=True, help='{"tasks": [{test_id,target,code_ref,cwd,executed_test_file,command[]}]}')
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report_dir = Path(args.report_dir).resolve()
        if not report_dir.is_dir():
            raise ContractError("report_dir 不是已存在目录")
        if args.action == "init":
            _init(report_dir)
            print(f"TEST DESIGN INIT OK targets={TARGETS_REL.as_posix()} intent={INTENT_REL.as_posix()}")
        elif args.action == "validate":
            _, errors, rejected = _validate_intent(report_dir)
            if errors:
                print("TEST DESIGN VALIDATE FAILED " + "；".join(errors[:5]), file=sys.stderr)
                return 1
            print("TEST DESIGN VALIDATE OK")
        elif args.action == "freeze":
            execution = _freeze(report_dir)
            active = sum(test["active"] for test in execution["tests"])
            print(f"TEST DESIGN FREEZE OK active={active}")
        elif args.action == "run":
            if not 1 <= args.timeout_seconds <= 3600:
                raise ContractError("timeout_seconds 必须位于 1..3600")
            if args.command and args.command[0] == "--":
                args.command = args.command[1:]
            record = _run_test(report_dir, args)
            print(f"TEST RUN RECORDED test={args.test_id} outcome={record['outcome']} reason={record['reason']}")
        elif args.action == "run-batch":
            result = _run_batch(report_dir, Path(args.manifest).expanduser())
            failed = len(result["failures"])
            print(f"TEST RUN BATCH {'OK' if not failed else 'PARTIAL'} "
                  f"tasks={len(result['records']) + failed} recorded={len(result['records'])} "
                  f"contract_errors={failed}")
            if failed:
                return 1
        else:
            summary = _seal(report_dir)
            print(f"TEST DESIGN SEAL {'OK' if summary['ok'] else 'INCOMPLETE'} "
                  f"verified={summary['counts']['verified']} uncovered={len(summary['uncovered_risks'])} "
                  f"missing_adversarial={len(summary['missing_adversarial'])}")
            return 0 if summary["ok"] else 1
        return 0
    except ContractError as exc:
        if args.action == "seal" and "report_dir" in locals():
            _write_json(report_dir / SUMMARY_REL, {
                "schema_version": SCHEMA_VERSION, "ok": False, "error": str(exc), "sealed_at": _now(),
            })
        print(f"TEST DESIGN ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
