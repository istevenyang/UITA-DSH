#!/usr/bin/env python3
"""sentinel 过程证据台账：逐步 append → 归并 `evidence-manifest.json`。

为什么逐步
----------
`evidence-manifest.json` 原本由步骤 9 一次性手写整份 `evidence[]`：预算一紧它最先被
牺牲；而 报告解析器把它当**可选**（`_read_evidence_manifest` 缺失即返回 None →
`evidence.status=missing`，CLI 恒 `return 0`），于是「这轮没证据」既不影响结论也不留痕。
本脚本把证据前移到每一步：该步产物落盘时**同批** append 一行到
`<报告目录>/.cache/evidence-ledger.jsonl`（append-only），步骤 9 只做归并与自检，
不再手写 `evidence[]`、不再手算哈希。

硬边界（不得突破）
------------------
1. 只做格式、哈希、归并、校验；**不做**风险/采纳/`verdict`/忽略等语义判定；
2. 历史行永不改写：同 `evidence_key` 重复 append 直接拒绝（宪法 R17）；
3. `artifact_refs[]` 的 `sha256` / `size_bytes` / `media_type` 由脚本实算，模型只给 `path`；
4. schema 常量与校验口径取自 `uita/core/lifecycle/device_exec/evidence.py`，不另立一套。

用法
----
    # 每步一次（与该步产物落盘同批；模型只给语义字段与工件路径）
    .venv/bin/python ci/sentinel_evidence.py append --report-dir <报告目录> \
        --step-id design_tests --evidence-key design:T3 --category behavior \
        --verification-method unit_test --strength definitive --polarity supporting \
        --fact-summary "T3 对抗用例断言红→修复后绿" --risk-key R3 --test-key T3 \
        --artifact tests/x.test.ts

    # 多条 / 长文本：JSON 走 stdin（对象或数组）
    .venv/bin/python ci/sentinel_evidence.py append --report-dir <报告目录> --json

    # 步骤 9：归并（附 device 片段）→ 自检 → 落 evidence-manifest.json
    .venv/bin/python ci/sentinel_evidence.py merge --report-dir <报告目录> \
        --run-key '<source_ref>#<yyyyMMdd-HHmmss>' --code-revision <40 位 SHA> \
        --knowledge-version profile-v6 --model <模型标识>

    # 门禁：本步至少一条；无 --step 时要求 1–9 全有 + manifest 合法
    .venv/bin/python ci/sentinel_evidence.py check --report-dir <报告目录> --step design_tests
    .venv/bin/python ci/sentinel_evidence.py check --report-dir <报告目录>

    # 排障
    .venv/bin/python ci/sentinel_evidence.py show --report-dir <报告目录>

证据缺失不阻断流程
------------------
`check` 默认 **advisory**：证据缺口只输出 `EVIDENCE CHECK WARN` 并退出 0，调用方把缺口记进
`summary.md` 的 `evidence_gaps` 后继续上报；要硬门才显式加 `--strict`。`merge` 在台账为空时
输出 `EVIDENCE MERGE SKIP`（不产 manifest，退出 0）——过程证据缺失只影响可审计性，不影响结论。
真正会失败（退出 1）的只有**契约性错误**：台账条目非法、schema/枚举不合规、工件哈希失配
（这种产物交给 报告解析器 会被整单拒绝，必须当场修）。

退出码：0=成功或 advisory 缺口；1=契约性错误（须当场修）；2=入参非法。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

SCHEMA_VERSION = 1
MAX_ARTIFACTS_PER_ITEM = 32
MAX_FACT_SUMMARY = 4000
MAX_MANIFEST_BYTES = 512 * 1024
CATEGORIES = {"context", "decision", "change", "impact", "execution", "runtime", "behavior", "conclusion"}
STRENGTHS = {"definitive", "partial", "inconclusive", "absent"}
POLARITIES = {"supporting", "counter", "neutral"}
MEDIA_TYPES = {
    "application/json",
    "text/markdown",
    "text/plain",
    "text/html",
    "image/png",
    "image/jpeg",
}
_EXT_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".json": "application/json",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".html": "text/html",
}


def media_type_for(path: str | Path) -> str:
    return _EXT_MEDIA_TYPES.get(Path(path).suffix.lower(), "text/plain")

STEP_IDS = (
    "decompose_requirement",
    "extract_change",
    "discover_risks",
    "review_defects",
    "route_risks",
    "design_tests",
    "execute_on_device",
    "self_heal_writeback",
    "report",
)
# 无 `--step` 时的必查步：1–9 全要（report 证据在导入前 append，见 reference/09）
REQUIRED_STEPS = STEP_IDS

VERIFICATION_METHODS = {"file", "command", "mcp", "unit_test", "device", "guard", "manual"}
ITEM_KEYS = (
    "evidence_key",
    "step_id",
    "category",
    "verification_method",
    "linked_risk_key",
    "linked_test_key",
    "strength",
    "polarity",
    "fact_summary",
    "artifact_refs",
    "occurred_at",
)
ARTIFACT_KEYS = {"path", "sha256", "media_type", "size_bytes"}
CONTEXT_KEYS = (
    "code_revision",
    "diff_hash",
    "rules_version",
    "knowledge_version",
    "model",
    "prompt_hash",
    "agent_definition_hash",
    "skill_definition_hash",
)
PRODUCER_KEYS = ("agent_name", "skill_name", "skill_version")
CONTEXT_ALLOWED = set(CONTEXT_KEYS) | {"requirement_ids"}

EVIDENCE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9:._-]{0,159}$")
RISK_KEY_RE = re.compile(r"^R[1-9]\d*$")
TEST_KEY_RE = re.compile(r"^T[1-9]\d*$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SENSITIVE_RE = re.compile(
    r"(?i)\b(?:token|password|passwd|cookie|authorization|secret|signature)\s*[:=]",
)

LEDGER_REL = Path(".cache") / "evidence-ledger.jsonl"
RUN_META_REL = Path(".cache") / "run-meta.json"
DIFF_RAW_REL = Path(".cache") / "diff.raw.txt"
MANIFEST_FILENAME = "evidence-manifest.json"
DEVICE_GLOB = "device/**/device-evidence.json"
AGENT_DEFINITION_REL = Path(".dsh/agents/sentinel.md")
SKILL_DEFINITION_REL = Path(".dsh/skills/sentinel/SKILL.md")
RULES_META_TEMPLATE = ".dsh/knowledge/products/{product}/utest-rules/rules-meta.json"
MAX_ITEMS = 1000
MAX_TEXT_128 = 128


class LedgerError(RuntimeError):
    """校验失败（调用方记该步 `blocked`）。"""


class UsageError(RuntimeError):
    """入参非法。"""


# ── 基础工具 ──────────────────────────────────────────────────────────────


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_text(value: Any, field: str, max_length: int = MAX_TEXT_128) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise LedgerError(f"{field} 必须是长度不超过 {max_length} 的非空字符串")
    lowered = value.lower()
    if SENSITIVE_RE.search(value) or "http://" in lowered or "https://" in lowered:
        raise LedgerError(f"{field} 不得包含敏感赋值或原始 URL")
    return value


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _normalize_occurred_at(value: Any) -> str:
    if value is None or value == "":
        return _iso_now()
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise LedgerError(f"occurred_at 必须是 ISO 8601 时间: {value!r}") from exc
    if parsed.tzinfo is None:
        raise LedgerError(f"occurred_at 必须带时区: {value!r}")
    return parsed.isoformat(timespec="seconds")


def _resolve_artifact(report_dir: Path, rel: Any) -> tuple[str, Path]:
    if (
        not isinstance(rel, str)
        or not rel
        or len(rel) > 512
        or "\\" in rel
        or rel.startswith(("/", "~"))
        or "://" in rel
        or re.match(r"^[A-Za-z]:", rel)
    ):
        raise LedgerError(f"artifact 路径必须是报告目录内相对路径: {rel!r}")
    relative = Path(rel)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise LedgerError(f"artifact 路径不允许路径穿越: {rel!r}")
    root = report_dir.resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise LedgerError(f"artifact 路径越出报告目录: {rel!r}") from exc
    if not target.is_file():
        raise LedgerError(f"artifact 文件不存在: {rel}")
    return rel, target


def _artifact_entry(report_dir: Path, rel: Any) -> dict[str, Any]:
    text, target = _resolve_artifact(report_dir, rel)
    raw = target.read_bytes()
    return {
        "path": text,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "media_type": media_type_for(target),
        "size_bytes": len(raw),
    }


def _read_json(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _run_meta(report_dir: Path) -> dict:
    return _read_json(report_dir / RUN_META_REL) or {}


# ── 台账读写 ──────────────────────────────────────────────────────────────


def _ledger_path(report_dir: Path) -> Path:
    return report_dir / LEDGER_REL


def _read_ledger(report_dir: Path) -> list[dict]:
    path = _ledger_path(report_dir)
    if not path.is_file():
        return []
    items: list[dict] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            item = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"{LEDGER_REL}:{lineno} 不是合法 JSON: {exc}") from exc
        if not isinstance(item, dict):
            raise LedgerError(f"{LEDGER_REL}:{lineno} 必须是 JSON 对象")
        items.append(item)
    return items


def _normalize_item(report_dir: Path, spec: dict) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise LedgerError("证据条目必须是 JSON 对象")
    key = spec.get("evidence_key")
    if not isinstance(key, str) or not EVIDENCE_KEY_RE.fullmatch(key):
        raise LedgerError(f"evidence_key 非法（^[A-Za-z][A-Za-z0-9:._-]{{0,159}}$）: {key!r}")
    step_id = spec.get("step_id")
    if step_id not in STEP_IDS:
        raise LedgerError(f"step_id 不属于固定九步: {step_id!r}")
    category = spec.get("category")
    if category not in CATEGORIES:
        raise LedgerError(f"category 非法: {category!r}")
    method = spec.get("verification_method")
    if method not in VERIFICATION_METHODS:
        raise LedgerError(f"verification_method 非法: {method!r}")
    strength = spec.get("strength")
    if strength not in STRENGTHS:
        raise LedgerError(f"strength 非法: {strength!r}")
    polarity = spec.get("polarity")
    if polarity not in POLARITIES:
        raise LedgerError(f"polarity 非法: {polarity!r}")
    risk_key = spec.get("linked_risk_key") or None
    if risk_key is not None and not (isinstance(risk_key, str) and RISK_KEY_RE.fullmatch(risk_key)):
        raise LedgerError(f"linked_risk_key 必须是 R<n> 或 null: {risk_key!r}")
    test_key = spec.get("linked_test_key") or None
    if test_key is not None and not (isinstance(test_key, str) and TEST_KEY_RE.fullmatch(test_key)):
        raise LedgerError(f"linked_test_key 必须是 T<n> 或 null: {test_key!r}")
    fact_summary = _validate_text(
        spec.get("fact_summary"), "fact_summary", max_length=MAX_FACT_SUMMARY,
    )

    raw_refs: Iterable[Any] = spec.get("artifacts") or spec.get("artifact_refs") or []
    if isinstance(raw_refs, (str, dict)):
        raw_refs = [raw_refs]
    rels: list[str] = []
    for entry in raw_refs:
        rel = entry.get("path") if isinstance(entry, dict) else entry
        if isinstance(rel, str) and rel not in rels:
            rels.append(rel)
    if len(rels) > MAX_ARTIFACTS_PER_ITEM:
        raise LedgerError(f"artifact_refs 超过 {MAX_ARTIFACTS_PER_ITEM} 项")
    artifacts = [_artifact_entry(report_dir, rel) for rel in rels]

    return {
        "evidence_key": key,
        "step_id": step_id,
        "category": category,
        "verification_method": method,
        "linked_risk_key": risk_key,
        "linked_test_key": test_key,
        "strength": strength,
        "polarity": polarity,
        "fact_summary": fact_summary,
        "artifact_refs": artifacts,
        "occurred_at": _normalize_occurred_at(spec.get("occurred_at")),
    }


def _specs_from_args(args: argparse.Namespace) -> list[dict]:
    if args.json:
        try:
            payload = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            raise UsageError(f"--json 读到的内容不是合法 JSON: {exc}") from exc
        if isinstance(payload, dict):
            return [payload]
        if isinstance(payload, list) and payload:
            return payload
        raise UsageError("--json 须为证据对象或非空数组")
    spec = {
        "evidence_key": args.evidence_key,
        "step_id": args.step_id,
        "category": args.category,
        "verification_method": args.verification_method,
        "linked_risk_key": args.risk_key,
        "linked_test_key": args.test_key,
        "strength": args.strength,
        "polarity": args.polarity,
        "fact_summary": args.fact_summary,
        "artifacts": args.artifact or [],
        "occurred_at": args.occurred_at,
    }
    missing = [
        flag
        for flag, value in (
            ("--evidence-key", spec["evidence_key"]),
            ("--step-id", spec["step_id"]),
            ("--category", spec["category"]),
            ("--verification-method", spec["verification_method"]),
            ("--strength", spec["strength"]),
            ("--polarity", spec["polarity"]),
            ("--fact-summary", spec["fact_summary"]),
        )
        if not value
    ]
    if missing:
        raise UsageError(f"缺必填参数: {', '.join(missing)}（或用 --json 走 stdin）")
    return [spec]


def cmd_append(args: argparse.Namespace) -> int:
    report_dir = Path(args.report_dir).resolve()
    if not report_dir.is_dir():
        raise UsageError(f"--report-dir 不是目录: {report_dir}")
    specs = _specs_from_args(args)
    existing = {item.get("evidence_key") for item in _read_ledger(report_dir)}
    normalized: list[dict] = []
    for spec in specs:
        item = _normalize_item(report_dir, spec)
        key = item["evidence_key"]
        if key in existing:
            raise LedgerError(
                f"evidence_key 已存在且历史证据不可改写: {key}"
                "（追加新事实请换用新 evidence_key）",
            )
        existing.add(key)
        normalized.append(item)

    path = _ledger_path(report_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for item in normalized:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    total = len(_read_ledger(report_dir))
    keys = ",".join(item["evidence_key"] for item in normalized)
    print(f"EVIDENCE APPEND OK keys={keys} ledger={LEDGER_REL.as_posix()} total={total}")
    return 0


# ── device 片段归并 ───────────────────────────────────────────────────────


def _device_segments(report_dir: Path, run_key: str) -> tuple[list[dict], list[str]]:
    """读 `device/**/device-evidence.json` 片段，artifact 路径重写为相对报告目录。"""
    items: list[dict] = []
    skipped: list[str] = []
    for path in sorted(report_dir.glob(DEVICE_GLOB)):
        payload = _read_json(path)
        if payload is None:
            raise LedgerError(f"{path.relative_to(report_dir)} 不是合法 JSON 对象")
        if set(payload) != {"schema_version", "run_key", "source_ref", "evidence"}:
            raise LedgerError(
                f"{path.relative_to(report_dir)} 顶层字段必须恰为 "
                "schema_version/run_key/source_ref/evidence",
            )
        if payload["schema_version"] != SCHEMA_VERSION:
            raise LedgerError(f"{path.relative_to(report_dir)} schema_version 必须为 {SCHEMA_VERSION}")
        segment_run_key = str(payload.get("run_key") or "").strip()
        if run_key and segment_run_key and segment_run_key != run_key:
            skipped.append(str(path.relative_to(report_dir)))
            continue
        prefix = path.parent.relative_to(report_dir).as_posix()
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            raise LedgerError(f"{path.relative_to(report_dir)}.evidence 必须是数组")
        for item in evidence:
            if not isinstance(item, dict) or set(item) != set(ITEM_KEYS):
                raise LedgerError(f"{path.relative_to(report_dir)} 的 evidence 条目字段不合规")
            rewritten = dict(item)
            refs: list[dict] = []
            for artifact in item.get("artifact_refs") or []:
                rel = str((artifact or {}).get("path") or "")
                if not rel:
                    raise LedgerError(f"{path.relative_to(report_dir)} 存在缺 path 的 artifact_refs")
                full = rel if rel.startswith(f"{prefix}/") else f"{prefix}/{rel}"
                refs.append(_artifact_entry(report_dir, full))
            rewritten["artifact_refs"] = refs
            rewritten["occurred_at"] = _normalize_occurred_at(item.get("occurred_at"))
            items.append(rewritten)
    return items, skipped


def _collect_items(report_dir: Path, run_key: str) -> tuple[list[dict], list[str]]:
    ledger_items = _read_ledger(report_dir)
    device_items, skipped = _device_segments(report_dir, run_key)
    seen: set[str] = set()
    for item in ledger_items:
        if set(item) != set(ITEM_KEYS):
            raise LedgerError(
                "台账存在字段不合规的条目"
                f"（多={sorted(set(item) - set(ITEM_KEYS))} 少={sorted(set(ITEM_KEYS) - set(item))}）；"
                "台账只允许由 `append` 写入",
            )
        key = item.get("evidence_key")
        if not isinstance(key, str) or not EVIDENCE_KEY_RE.fullmatch(key):
            raise LedgerError(f"台账存在非法 evidence_key: {key!r}")
        if key in seen:
            raise LedgerError(f"台账存在重复 evidence_key: {key}")
        seen.add(key)
    for item in device_items:
        key = item["evidence_key"]
        if key in seen:
            raise LedgerError(f"device 片段 evidence_key 与台账冲突: {key}")
        seen.add(key)
    return [*ledger_items, *device_items], skipped


# ── context 自动拼装 ──────────────────────────────────────────────────────


def _skill_frontmatter(skill_path: Path) -> dict:
    if not skill_path.is_file():
        raise LedgerError(f"缺 skill 定义文件: {skill_path}")
    text = skill_path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not match:
        raise LedgerError(f"{skill_path} 缺 YAML frontmatter")
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 依赖已声明
        raise LedgerError("需要 pyyaml 解析 skill frontmatter（requirements.txt 已声明）") from exc
    try:
        payload = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise LedgerError(f"{skill_path} frontmatter 不是合法 YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise LedgerError(f"{skill_path} frontmatter 必须是对象")
    return payload


def _prompt_hash(frontmatter: dict) -> str:
    prompt = frontmatter.get("prompt_template")
    if not isinstance(prompt, dict):
        raise LedgerError("SKILL.md 缺 prompt_template 块（prompt_hash 无法计算）")
    payload = {
        key: prompt.get(key)
        for key in ("system_template", "user_template", "separator", "variables")
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _rules_version(report_dir: Path, args: argparse.Namespace) -> str:
    if args.rules_meta:
        path = Path(args.rules_meta)
    else:
        product = args.product_code or str(_run_meta(report_dir).get("product_code") or "")
        if not product:
            raise UsageError("缺 --product-code（或 .cache/run-meta.json 无 product_code），无法定位 rules-meta.json")
        path = REPO_ROOT / RULES_META_TEMPLATE.format(product=product)
    payload = _read_json(path)
    if payload is None:
        raise LedgerError(f"读不到 rules-meta.json: {path}")
    version = payload.get("rules_version")
    if not isinstance(version, str) or not version.strip():
        raise LedgerError(f"{path} 缺 rules_version")
    return version.strip()


def _build_context(report_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    meta = _run_meta(report_dir)
    frontmatter = _skill_frontmatter(REPO_ROOT / SKILL_DEFINITION_REL)

    code_revision = (args.code_revision or "").strip()
    if code_revision == "auto":
        code_revision = str(meta.get("code_revision") or "").strip()
    if not COMMIT_RE.fullmatch(code_revision):
        raise UsageError(
            "--code-revision 必须是完整 40 位小写 commit SHA"
            "（branch 轮次请取分支 HEAD，不得用分支名）",
        )

    knowledge_version = (args.knowledge_version or "").strip()
    if knowledge_version == "auto":
        profile = str(meta.get("profile_version") or "").strip()
        knowledge_version = f"profile-{profile}" if profile else ""
    if not knowledge_version:
        raise UsageError("缺 --knowledge-version（形如 profile-v6；或用 auto 读 run-meta 的 profile_version）")

    model = (args.model or os.environ.get("MODEL") or "").strip()
    if not model:
        raise UsageError("缺 --model（或环境变量 MODEL），禁止猜测模型标识")

    diff_path = Path(args.diff_file) if args.diff_file else report_dir / DIFF_RAW_REL
    if not diff_path.is_file():
        raise LedgerError(f"缺 diff 原文（diff_hash 无法计算）: {diff_path}")
    agent_path = REPO_ROOT / AGENT_DEFINITION_REL
    if not agent_path.is_file():
        raise LedgerError(f"缺 agent 定义文件: {agent_path}")
    skill_path = REPO_ROOT / SKILL_DEFINITION_REL

    skill_version = (args.skill_version or str(frontmatter.get("version") or "")).strip()
    if not skill_version:
        raise UsageError("缺 --skill-version（默认取 SKILL.md frontmatter 的 version）")

    context: dict[str, Any] = {
        "code_revision": _validate_text(code_revision, "context.code_revision"),
        "diff_hash": _sha256_file(diff_path),
        "rules_version": _validate_text(_rules_version(report_dir, args), "context.rules_version"),
        "knowledge_version": _validate_text(knowledge_version, "context.knowledge_version"),
        "model": _validate_text(model, "context.model"),
        "prompt_hash": _prompt_hash(frontmatter),
        "agent_definition_hash": _sha256_file(agent_path),
        "skill_definition_hash": _sha256_file(skill_path),
    }
    requirement_ids = list(args.requirement_id or [])
    if requirement_ids:
        if len(requirement_ids) > 32 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 64 or "://" in item
            for item in requirement_ids
        ):
            raise UsageError("--requirement-id 必须是不含 URL 的短字符串，最多 32 条")
        context["requirement_ids"] = requirement_ids
    return context


# ── manifest 自检（与 报告解析器 `_read_evidence_manifest` 同口径）────────────


def validate_manifest(payload: Any, report_dir: Path) -> list[str]:
    problems: list[str] = []
    if not isinstance(payload, dict):
        return ["manifest 必须是 JSON 对象"]
    expected_top = {"schema_version", "run_key", "source_ref", "producer", "context", "evidence"}
    if set(payload) != expected_top:
        return [
            "manifest 顶层字段必须恰为 schema_version/run_key/source_ref/producer/context/evidence"
            f"（多={sorted(set(payload) - expected_top)} 少={sorted(expected_top - set(payload))}）",
        ]
    if payload["schema_version"] != SCHEMA_VERSION:
        problems.append(f"schema_version 必须为 {SCHEMA_VERSION}")
    for key in ("run_key", "source_ref"):
        try:
            _validate_text(payload[key], key)
        except LedgerError as exc:
            problems.append(str(exc))

    producer = payload["producer"]
    if not isinstance(producer, dict) or set(producer) != set(PRODUCER_KEYS):
        problems.append("producer 字段必须恰为 agent_name/skill_name/skill_version")
    else:
        for key in PRODUCER_KEYS:
            try:
                _validate_text(producer[key], f"producer.{key}")
            except LedgerError as exc:
                problems.append(str(exc))

    context = payload["context"]
    if not isinstance(context, dict):
        problems.append("context 必须是对象")
    else:
        missing = set(CONTEXT_KEYS) - set(context)
        unknown = set(context) - CONTEXT_ALLOWED
        if missing or unknown:
            problems.append(f"context 字段不合规: 缺少={sorted(missing)} 未知={sorted(unknown)}")
        for key in CONTEXT_KEYS:
            if key not in context:
                continue
            try:
                _validate_text(context[key], f"context.{key}")
            except LedgerError as exc:
                problems.append(str(exc))

    items = payload["evidence"]
    if not isinstance(items, list) or not items:
        return [*problems, "evidence 必须是非空数组"]
    if len(items) > MAX_ITEMS:
        problems.append(f"evidence 条数 {len(items)} 超过上限 {MAX_ITEMS}")

    seen: set[str] = set()
    for index, item in enumerate(items):
        label = f"evidence[{index}]"
        if not isinstance(item, dict) or set(item) != set(ITEM_KEYS):
            problems.append(f"{label} 字段集合不合规")
            continue
        key = item["evidence_key"]
        if not isinstance(key, str) or not EVIDENCE_KEY_RE.fullmatch(key):
            problems.append(f"{label}.evidence_key 非法: {key!r}")
        elif key in seen:
            problems.append(f"{label}.evidence_key 重复: {key}")
        else:
            seen.add(key)
        if item["step_id"] not in STEP_IDS:
            problems.append(f"{label}.step_id 非法: {item['step_id']!r}")
        if item["category"] not in CATEGORIES:
            problems.append(f"{label}.category 非法: {item['category']!r}")
        if item["verification_method"] not in VERIFICATION_METHODS:
            problems.append(f"{label}.verification_method 非法: {item['verification_method']!r}")
        if item["strength"] not in STRENGTHS:
            problems.append(f"{label}.strength 非法: {item['strength']!r}")
        if item["polarity"] not in POLARITIES:
            problems.append(f"{label}.polarity 非法: {item['polarity']!r}")
        risk_key = item["linked_risk_key"]
        if risk_key is not None and not (isinstance(risk_key, str) and RISK_KEY_RE.fullmatch(risk_key)):
            problems.append(f"{label}.linked_risk_key 必须是 R<n> 或 null")
        test_key = item["linked_test_key"]
        if test_key is not None and not (isinstance(test_key, str) and TEST_KEY_RE.fullmatch(test_key)):
            problems.append(f"{label}.linked_test_key 必须是 T<n> 或 null")
        try:
            _validate_text(item["fact_summary"], f"{label}.fact_summary", MAX_FACT_SUMMARY)
        except LedgerError as exc:
            problems.append(str(exc))
        try:
            _normalize_occurred_at(item["occurred_at"])
            datetime.fromisoformat(str(item["occurred_at"]))
        except (LedgerError, ValueError):
            problems.append(f"{label}.occurred_at 必须是带时区 ISO 8601")
        refs = item["artifact_refs"]
        if not isinstance(refs, list) or len(refs) > MAX_ARTIFACTS_PER_ITEM:
            problems.append(f"{label}.artifact_refs 必须是受限数组")
            continue
        for ref_index, ref in enumerate(refs):
            ref_label = f"{label}.artifact_refs[{ref_index}]"
            if not isinstance(ref, dict) or set(ref) != ARTIFACT_KEYS:
                problems.append(f"{ref_label} 字段必须恰为 path/sha256/media_type/size_bytes")
                continue
            try:
                _, target = _resolve_artifact(report_dir, ref["path"])
            except LedgerError as exc:
                problems.append(f"{ref_label}: {exc}")
                continue
            raw = target.read_bytes()
            if not isinstance(ref["sha256"], str) or not SHA256_RE.fullmatch(ref["sha256"]):
                problems.append(f"{ref_label}.sha256 必须是 64 位小写十六进制")
            elif hashlib.sha256(raw).hexdigest() != ref["sha256"]:
                problems.append(f"{ref_label}.sha256 与实际文件不一致")
            if ref["media_type"] not in MEDIA_TYPES:
                problems.append(f"{ref_label}.media_type 不在允许列表")
            if not isinstance(ref["size_bytes"], int) or isinstance(ref["size_bytes"], bool):
                problems.append(f"{ref_label}.size_bytes 必须是非负整数")
            elif ref["size_bytes"] != len(raw):
                problems.append(f"{ref_label}.size_bytes 与实际不符")

    size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if size > MAX_MANIFEST_BYTES:
        problems.append(f"manifest 序列化后 {size} 字节，超过 {MAX_MANIFEST_BYTES} 上限")
    return problems


# ── 子命令 ────────────────────────────────────────────────────────────────


def _refresh_items(report_dir: Path, items: Sequence[dict], strict: bool) -> list[str]:
    """重算 artifact 实算值；返回哈希漂移的 evidence_key 列表。"""
    drifted: list[str] = []
    for item in items:
        changed = False
        for ref in item.get("artifact_refs") or []:
            fresh = _artifact_entry(report_dir, ref.get("path"))
            if (
                fresh["sha256"] != ref.get("sha256")
                or fresh["size_bytes"] != ref.get("size_bytes")
                or fresh["media_type"] != ref.get("media_type")
            ):
                changed = True
            ref.clear()
            ref.update(fresh)
        if changed:
            drifted.append(item["evidence_key"])
    if drifted and strict:
        raise LedgerError(
            "--strict-hash 下禁止哈希漂移，请改用新 evidence_key 追加事实: " + ", ".join(drifted),
        )
    return drifted


def cmd_merge(args: argparse.Namespace) -> int:
    report_dir = Path(args.report_dir).resolve()
    if not report_dir.is_dir():
        raise UsageError(f"--report-dir 不是目录: {report_dir}")
    run_key = (args.run_key or "").strip()
    if not run_key:
        raise UsageError("缺 --run-key（格式 <source_ref>#<yyyyMMdd-HHmmss>，须与 utest-defects.json 同源）")
    _validate_text(run_key, "run_key")
    source_ref = (args.source_ref or str(_run_meta(report_dir).get("source_ref") or "")).strip()
    if not source_ref:
        raise UsageError("缺 --source-ref（或 .cache/run-meta.json 的 source_ref）")
    _validate_text(source_ref, "source_ref")

    items, skipped_device = _collect_items(report_dir, run_key)
    if not items:
        # 证据缺失**不阻断**流程：不产出 manifest（报告解析器 视其为可选），但必须留 WARN 供留痕。
        print(
            "EVIDENCE MERGE SKIP no_evidence "
            f"ledger={LEDGER_REL.as_posix()} skipped_device={len(skipped_device)}",
        )
        print(
            "  WARN: 本轮无任何过程证据 → 不产出 evidence-manifest.json（不阻断上报）；"
            "请在 summary.md 的 evidence_gaps 记缺失步骤与原因",
            file=sys.stderr,
        )
        return 0
    for item in items:
        item["evidence_key"] = _validate_text(item["evidence_key"], "evidence_key")
        for key in ("step_id", "category", "verification_method", "strength", "polarity"):
            if not isinstance(item.get(key), str):
                raise LedgerError(f"证据 {item['evidence_key']} 的 {key} 非法")
    drifted = _refresh_items(report_dir, items, strict=args.strict_hash)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "run_key": run_key,
        "source_ref": source_ref,
        "producer": {
            "agent_name": _validate_text(args.agent_name, "producer.agent_name"),
            "skill_name": _validate_text(args.skill_name, "producer.skill_name"),
            "skill_version": _validate_text(
                (args.skill_version or str(_skill_frontmatter(REPO_ROOT / SKILL_DEFINITION_REL).get("version") or "")),
                "producer.skill_version",
            ),
        },
        "context": _build_context(report_dir, args),
        "evidence": [{key: item[key] for key in ITEM_KEYS} for item in items],
    }
    problems = validate_manifest(payload, report_dir)
    if problems:
        raise LedgerError("manifest 自检失败:\n  - " + "\n  - ".join(problems))

    out_name = args.out or MANIFEST_FILENAME
    if Path(out_name).name != out_name:
        raise UsageError(f"--out 只允许文件名（不得带目录）: {out_name!r}")
    target = report_dir / out_name
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    steps = sorted({item["step_id"] for item in items}, key=STEP_IDS.index)
    print(
        "EVIDENCE MERGE OK "
        f"items={len(items)} steps={len(steps)}/9 manifest={target.relative_to(report_dir).as_posix()} "
        f"refreshed={len(drifted)}{':' + ','.join(drifted) if drifted else ''} "
        f"skipped_device={len(skipped_device)}",
    )
    print(f"  steps={','.join(steps)}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    report_dir = Path(args.report_dir).resolve()
    if not report_dir.is_dir():
        raise UsageError(f"--report-dir 不是目录: {report_dir}")
    run_key = (args.run_key or "").strip()
    if args.step and args.step not in STEP_IDS:
        raise UsageError(f"--step 不属于固定九步: {args.step!r}")

    if run_key:
        items, _ = _collect_items(report_dir, run_key)
    else:  # 未给 run_key 时只按台账 + 任意 device 片段统计（步骤内自查用）
        items = list(_read_ledger(report_dir))
        device_items, _ = _device_segments(report_dir, "")
        items.extend(device_items)

    counts: dict[str, int] = {}
    for item in items:
        step = item.get("step_id")
        counts[step] = counts.get(step, 0) + 1

    if args.step:
        if not counts.get(args.step):
            return _report_gap(
                args,
                problems=[f"步骤 {args.step} 无过程证据（台账 {LEDGER_REL.as_posix()} 无该 step_id）"],
                summary=f"step={args.step} items=0",
            )
        print(f"EVIDENCE CHECK OK step={args.step} items={counts[args.step]}")
        return 0

    missing = [step for step in REQUIRED_STEPS if not counts.get(step)]
    problems: list[str] = []
    if missing:
        problems.append("缺步骤证据: " + ", ".join(missing))
    manifest_path = report_dir / (args.out or MANIFEST_FILENAME)
    if not manifest_path.is_file():
        problems.append(f"缺 {manifest_path.name}（先跑 merge；无证据时不产 manifest 属允许）")
    else:
        payload = _read_json(manifest_path)
        if payload is None:
            problems.append(f"{manifest_path.name} 不是合法 JSON 对象")
        else:
            problems.extend(validate_manifest(payload, report_dir))
    total = sum(counts.values())
    if problems:
        return _report_gap(
            args,
            problems=problems,
            summary=f"steps={len(STEP_IDS) - len(missing)}/{len(STEP_IDS)} items={total}",
        )
    print(f"EVIDENCE CHECK OK steps={len(REQUIRED_STEPS)}/{len(STEP_IDS)} items={total} manifest={manifest_path.name}")
    return 0


def _report_gap(args: argparse.Namespace, *, problems: list[str], summary: str) -> int:
    """证据缺口：默认 advisory（不阻断流程），`--strict` 才判失败。"""
    detail = "\n  - ".join(problems)
    if getattr(args, "strict", False):
        raise LedgerError("证据门禁未过（--strict）:\n  - " + detail)
    print(f"EVIDENCE CHECK WARN {summary} strict=off")
    print(f"  WARN: 证据缺口不阻断流程，但须在 summary.md 的 evidence_gaps 逐条留痕并继续上报:\n  - {detail}",
          file=sys.stderr)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    report_dir = Path(args.report_dir).resolve()
    if not report_dir.is_dir():
        raise UsageError(f"--report-dir 不是目录: {report_dir}")
    items, skipped = _collect_items(report_dir, (args.run_key or "").strip())
    counts: dict[str, int] = {}
    for item in items:
        counts[item["step_id"]] = counts.get(item["step_id"], 0) + 1
    print(f"EVIDENCE SHOW ledger={LEDGER_REL.as_posix()} items={len(items)}")
    for step in STEP_IDS:
        keys = [item["evidence_key"] for item in items if item["step_id"] == step]
        flag = "OK " if keys else "MISS"
        print(f"  {flag} {step:<22} {len(keys):>3} {','.join(keys[:6])}")
    if skipped:
        print(f"  skipped_device={skipped}")
    manifest_path = report_dir / MANIFEST_FILENAME
    print(f"  manifest={'present' if manifest_path.is_file() else 'missing'}")
    return 0


# ── 入口 ──────────────────────────────────────────────────────────────────


def _add_merge_context_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--code-revision", default="", help="完整 40 位 commit SHA；auto=读 run-meta")
    parser.add_argument("--knowledge-version", default="", help="形如 profile-v6；auto=读 run-meta 的 profile_version")
    parser.add_argument("--model", default="", help="本轮实际模型标识；缺省读环境变量 MODEL")
    parser.add_argument("--skill-version", default="", help="缺省取 SKILL.md frontmatter version")
    parser.add_argument("--product-code", default="", help="定位 .dsh/knowledge/products/<code>/utest-rules/")
    parser.add_argument("--rules-meta", default="", help="直接指定 rules-meta.json 路径（覆盖 product-code 推导）")
    parser.add_argument("--diff-file", default="", help="diff 原文路径（缺省 .cache/diff.raw.txt）")
    parser.add_argument("--requirement-id", action="append", default=None, help="关联需求单标识（可重复，≤32）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="sentinel 过程证据台账（逐步 append → 步骤 9 归并 manifest）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    append = sub.add_parser("append", help="追加一条/多条过程证据到 .cache/evidence-ledger.jsonl")
    append.add_argument("--report-dir", required=True)
    append.add_argument("--json", action="store_true", help="从 stdin 读证据对象或数组")
    append.add_argument("--step-id", default="")
    append.add_argument("--evidence-key", default="")
    append.add_argument("--category", default="")
    append.add_argument("--verification-method", default="")
    append.add_argument("--strength", default="")
    append.add_argument("--polarity", default="")
    append.add_argument("--fact-summary", default="")
    append.add_argument("--risk-key", default="")
    append.add_argument("--test-key", default="")
    append.add_argument("--artifact", action="append", default=None, help="报告目录内相对路径（可重复）")
    append.add_argument("--occurred-at", default="", help="带时区 ISO 8601；缺省取当前时间")
    append.set_defaults(func=cmd_append)

    merge = sub.add_parser("merge", help="归并台账 + device 片段 → evidence-manifest.json")
    merge.add_argument("--report-dir", required=True)
    merge.add_argument("--run-key", default="")
    merge.add_argument("--source-ref", default="")
    merge.add_argument("--agent-name", default="sentinel")
    merge.add_argument("--skill-name", default="sentinel")
    merge.add_argument("--strict-hash", action="store_true", help="artifact 哈希漂移即失败")
    merge.add_argument("--out", default=MANIFEST_FILENAME)
    _add_merge_context_args(merge)
    merge.set_defaults(func=cmd_merge)

    check = sub.add_parser("check", help="证据自查（advisory）：本步至少一条 / 九步齐全 + manifest 合法")
    check.add_argument("--report-dir", required=True)
    check.add_argument("--step", default="", help="只校验该步")
    check.add_argument("--run-key", default="", help="给了则按该轮次过滤 device 片段")
    check.add_argument("--out", default=MANIFEST_FILENAME)
    check.add_argument(
        "--strict", action="store_true",
        help="把证据缺口判为失败（默认 advisory：缺失不阻断流程，只 WARN 留痕）",
    )
    check.set_defaults(func=cmd_check)

    show = sub.add_parser("show", help="打印台账摘要（各步条数 + 键）")
    show.add_argument("--report-dir", required=True)
    show.add_argument("--run-key", default="")
    show.set_defaults(func=cmd_show)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except UsageError as exc:
        print(f"EVIDENCE USAGE: {exc}", file=sys.stderr)
        return 2
    except LedgerError as exc:
        print(f"EVIDENCE FAIL: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"EVIDENCE FAIL: 写盘/读盘失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
