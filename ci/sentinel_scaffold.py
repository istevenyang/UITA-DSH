#!/usr/bin/env python3
"""sentinel 报告目录 + `summary.md` 台账骨架初始化器（步骤 1 首个动作）。

用途
----
把「建本轮报告目录 → 建 `.cache/` `/device/` `/tests/` → 落 `summary.md` 九步台账骨架
→ 落 `.cache/run-meta.json` → 落 `.cache/risk-ledger.json` 决策账本模板」收敛为**一次
`execute_command`**，省掉模型手写台账骨架的回合与格式漂移。

防跳步作用（本脚本存在的第二个理由）
------------------------------------
骨架预置 9 行 `step_id` 且状态为 `pending`；`00-guard-scripts.md` 的 S1 无头收尾闸会
反查「九个 step_id 均有终态、且无 `pending`/`running` 残留」。因此**任何被跳过的步骤都会
在机器闸上暴露**，不再依赖模型自己记得把 9 行补齐。

硬边界（不得突破）
------------------
1. 本脚本**只建目录与骨架文本**，不产出任何语义结论（不判风险、不判关联、不给 verdict）；
2. 不写 `risk-report.html`、不写 `utest-defects.json`（两者仍须模型直写/直出）；
3. 已存在的报告目录默认**拒绝覆盖**（除非显式 `--force`），避免踩掉既有轮次。

用法
----
    # 步骤 1 首个动作（source_ref 未知时先不传，步骤 2 规范化后回填）
    .venv/bin/python ci/sentinel_scaffold.py --story-id <需求单号或安全标识> \
        [--source-ref <ref>] [--code-diff-type mr|commit|ci|branch]

    # 步骤 2 规范化出 source_ref 后回填（幂等，可重复跑）
    .venv/bin/python ci/sentinel_scaffold.py --update --report-dir <报告目录> \
        --source-ref <ref> [--code-diff-type <t>]

退出码：0=成功；1=目录已存在/写盘失败（调用方记该步 `blocked`）；2=入参非法。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

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
STEP_BUDGETS = (4, 18, 6, 2, 3, 14, 8, 3, 8)

DIFF_TYPES = ("mr", "commit", "ci", "branch")
REPORT_ROOT_DEFAULT = "report/sentinel"

# 入参白名单：分支名 / 项目路径 / SHA / IID / 单号；严禁 URL、shell 元字符、路径穿越
SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/@!:#+\-]{1,240}$")
STORY_ID_RE = re.compile(r"^[A-Za-z0-9._\-]{1,120}$")
TIMESTAMP_RE = re.compile(r"^\d{8}-\d{6}$")


class ScaffoldError(RuntimeError):
    """入参非法或写盘失败（调用方记该步 blocked）。"""


def sanitize_story_id(raw: str) -> str:
    """非 `[A-Za-z0-9._-]` 字符统一替换为 `-`（口径同 `report-structure.md` 目录命名）。"""
    out = re.sub(r"[^A-Za-z0-9._\-]", "-", raw or "")
    out = re.sub(r"-{2,}", "-", out).strip("-._")
    return out[:120]


def default_story_id(source_ref: str, code_diff_type: str, locator: str) -> str:
    """MR → `mr-<iid>`；commit → `commit-<sha8>`；其余 → 清理后的 source_ref/locator。"""
    ref = source_ref or ""
    if code_diff_type == "mr":
        hit = re.search(r"!(\d+)$", ref)
        if hit:
            return f"mr-{hit.group(1)}"
    if code_diff_type == "commit":
        hit = re.search(r"([0-9a-fA-F]{7,40})", ref)
        if hit:
            return f"commit-{hit.group(1)[:8].lower()}"
    for candidate in (ref, locator):
        cleaned = sanitize_story_id(candidate)
        if cleaned:
            return cleaned
    raise ScaffoldError("无法推导 story_id：请显式传 --story-id")


def validate(source_ref: str, code_diff_type: str) -> None:
    if source_ref and ("://" in source_ref or not SAFE_REF_RE.match(source_ref)):
        raise ScaffoldError(f"source_ref 含非法字符或 URL：{source_ref!r}")
    if code_diff_type and code_diff_type not in DIFF_TYPES:
        raise ScaffoldError(f"code_diff_type 必须属于 {DIFF_TYPES}，收到 {code_diff_type!r}")


def summary_skeleton(meta: dict[str, str]) -> str:
    """九步台账骨架：9 行必填终态行 + guard 表头 + 固定字段（防跳步的机器可查载体）。"""
    rows = "\n".join(
        f"| {sid} | {idx} | pending | - | - | 0 |"
        for idx, sid in enumerate(STEP_IDS, start=1)
    )
    return f"""# sentinel 运行状态账本

<!-- 本骨架由 `ci/sentinel_scaffold.py` 生成：主体段落只在步骤 2/6/9 统一更新；
     每步 gate 台账行以单条 `>>` append（append-only，不重读全文、不额外占回合）。 -->

## 基本事实

| 项 | 值 |
|---|---|
| source_ref | {meta["source_ref"] or "(待步骤 2 回填：`--update --source-ref`)"} |
| code_diff_type | {meta["code_diff_type"] or "(待填)"} |
| story_id | {meta["story_id"]} |
| report_dir | {meta["report_dir"]} |
| started_at | {meta["created_at"]} |
| headless | (待判定：UITA_HEADLESS=1 / 输入含 CI 无头声明字面量 / 反问类工具不可用 / 上下文无真实用户消息) |
| commit_policy | (待填：交互=需用户显式确认 / 无头=`no_commit(headless)`) |
| vision_unavailable | (待填 true/false) |
| tool_fallback | [] |
| credential_source | (待步骤 9 回填) |
| notify_status | (待步骤 9 回填) |
| shots_skipped_budget | 0 |
| burn_list | [] |

## 步骤状态

<!-- ⛔ 收尾前必须把 9 行状态全部改为终态（done / blocked / skipped / failed）并填证据路径与 gate。
     残留 pending/running 会让 S1 无头收尾闸判 fail（= 本轮视为未完成）。 -->

| step_id | # | 状态 | 证据路径 | gate | 回合数 |
|---|---|---|---|---|---|
{rows}

## budget

- total: 66（业务 9 步）；per-step: {" / ".join(f"{i}={b}" for i, b in enumerate(STEP_BUDGETS, start=1))}
- used: 0
- agent_overhead_turns: 0
- headless_hard_cap: 90
- budget_exhausted: false
- downgrades: []
- guard_conflict: []
- out_of_scope_changes: []
- unverified: []
- no_call_record: []
- ignored_unreachable: 0（逐条见 `utest-defects.json` 的 `summary.ignored_unreachable`）
- final_token: (待收尾逐字写入 `SENTINEL_FINAL: ...` 单行)

## guard

| step | 机器闸（命令摘要） | 结果 | 回合数 | unverified | no_call_record | 备注 |
|---|---|---|---|---|---|---|

## 历史缺陷检索漏斗（步骤 2 落盘，计数由 `ci/sentinel_ledger.py funnel` 计算）

(待填)

## diff 分片（步骤 2 落盘）

(待填)
"""


LEDGER_TEMPLATE = {
    "_schema": "sentinel 决策账本（模型填决策，ci/sentinel_ledger.py risk 只做算术）",
    "_fields": {
        "risks[].risk_key": "R<n>（编号一经落盘不得重排/复用/改写语义）",
        "risks[].risk_level": "high|medium|low|undetermined",
        "risks[].risk_priority": "P0|P1|P2（未消除条目必填）",
        "risks[].dimension_codes": "英文逗号分隔：business,function,logic,data,state,architecture",
        "risks[].route": "unit|device|unit+device|accept|ignore",
        "risks[].status": "eliminated|uneliminated|ignored",
        "risks[].covered_method": "status=eliminated 时必填：unit|lock|device|baseline|accepted",
        "risks[].reachability": "ui_reachable|device_reachable|unreachable",
        "risks[].device_verdict": "route 含 device 时填 pass|fail|inconclusive",
        "risks[].item_type": "risk|defect（defect 才进 defects[]）",
        "risks[].defect_location": "item_type=defect 时必填，格式 路径.文件名.扩展名::行号",
        "risks[].coverage_target": "item_type=defect 时必填",
        "risks[].unreachable_type": "status=ignored 时必填：ui_unreachable|ui_no_entry|ui_and_device_unreachable|external_uncontrolled",
        "risks[].judgement_evidence": "status=ignored 时必填（四步法一段式证据）",
        "risks[].ignore_reason": "status=ignored 时必填",
        "risks[].escalation_trigger": "status=ignored 时必填",
        "noise_count": "噪音剔除条数（独立量纲，不进风险池）",
        "knowledge_gaps": "可选（W4）：本轮知识库召回为空/未采纳的缺口，[{query, reason, recall_id}]，步骤 3 写入；"
                          "`ci/sentinel_ledger.py risk` 只读 risks[]，本字段不参与算术",
    },
    "source_ref": "",
    "code_diff_type": "",
    "noise_count": 0,
    "risks": [],
}


def write_summary(path: Path, meta: dict[str, str]) -> None:
    path.write_text(summary_skeleton(meta), encoding="utf-8")


def update_existing(report_dir: Path, source_ref: str, code_diff_type: str) -> dict[str, str]:
    """回填 source_ref / code_diff_type（幂等单行替换，不改骨架其余内容）。"""
    meta_path = report_dir / ".cache" / "run-meta.json"
    if not meta_path.is_file():
        raise ScaffoldError(f"缺 {meta_path}：该目录不是本脚本创建的轮次目录")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if source_ref:
        validate(source_ref, "")
        meta["source_ref"] = source_ref
    if code_diff_type:
        validate("", code_diff_type)
        meta["code_diff_type"] = code_diff_type
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary_path = report_dir / "summary.md"
    if summary_path.is_file():
        text = summary_path.read_text(encoding="utf-8")
        text = re.sub(
            r"^\| source_ref \| .*$", f"| source_ref | {meta['source_ref'] or '(待回填)'} |",
            text, count=1, flags=re.M,
        )
        text = re.sub(
            r"^\| code_diff_type \| .*$", f"| code_diff_type | {meta['code_diff_type'] or '(待填)'} |",
            text, count=1, flags=re.M,
        )
        summary_path.write_text(text, encoding="utf-8")
    return meta


STEP_ROW_RE = re.compile(
    r"^\|\s*(" + "|".join(STEP_IDS) + r")\s*(?=\|).*$", re.M,
)
TERMINAL_STATES = ("done", "blocked", "skipped", "failed")
FINAL_TOKEN_PREFIX = "SENTINEL_FINAL:"
# 骨架里有一句提示文案也含该前缀（「逐字写入 `SENTINEL_FINAL: ...`」），
# 故「是否已有令牌」必须按行首 + status= 判定，不能用子串判断。
FINAL_TOKEN_RE = re.compile(r"^SENTINEL_FINAL:\s*status=", re.M)


def _row_state(line: str) -> str:
    cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return cells[2].lower() if len(cells) >= 3 else ""


def latest_report_dir(report_root: Path) -> Path:
    """报告根目录下按目录名（内含时间戳）取最新一轮；无则报错。"""
    candidates = sorted((p for p in report_root.iterdir() if p.is_dir()), key=lambda p: p.name)
    if not candidates:
        raise ScaffoldError(f"{report_root} 下没有任何轮次目录")
    return candidates[-1]


def finalize_report(report_dir: Path, status: str = "blocked", reason: str = "interrupted") -> dict:
    """把残留 pending/running 的步骤行补成终态并确保终局令牌存在（幂等，只补不改）。

    解决的故障（2026-11-04 实测）：外部墙钟超时把 agent 硬杀后，台账停在 `4/9`、
    `used: 0`、无 `SENTINEL_FINAL`，诊断器只能把整轮判成
    「无令牌黑盒」，无法区分「预算耗尽」与「进程被杀」。由 CI 在 agent 退出后无条件调用
    本命令，即可把半成品转成可判定的 `partial/blocked` 结果。

    只做三件事，绝不改已终态行、不重排、不删行：
    1. `pending`/`running` → `skipped`，gate 列记 `interrupted(finalize)`；
    2. 缺令牌则追加一行 `SENTINEL_FINAL: ...`；
    3. `final_token:` 行回填同一令牌文本。
    """
    summary = report_dir / "summary.md"
    if not summary.is_file():
        raise ScaffoldError(f"缺 {summary}：该目录不是有效轮次")
    text = summary.read_text(encoding="utf-8")

    skipped: list[str] = []
    done_rows: list[str] = []

    def _rewrite(match: re.Match[str]) -> str:
        line = match.group(0)
        state = _row_state(line)
        if state in TERMINAL_STATES:
            if state == "done":
                done_rows.append(match.group(1))
            return line
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        while len(cells) < 6:
            cells.append("-")
        cells[2] = "skipped"
        cells[4] = "interrupted(finalize)"
        cells[5] = "0"
        skipped.append(match.group(1))
        return "| " + " | ".join(cells) + " |"

    text = STEP_ROW_RE.sub(_rewrite, text)
    steps_done = len(done_rows)

    if not skipped and FINAL_TOKEN_RE.search(text):
        summary.write_text(text, encoding="utf-8")
        return {"changed": False, "steps_done": steps_done, "skipped": [], "token": ""}

    token_line = ""
    for line in text.splitlines():
        if line.strip().startswith(FINAL_TOKEN_PREFIX):
            token_line = line.strip()
            break
    if not token_line:
        token_line = (
            f"{FINAL_TOKEN_PREFIX} status={status} steps_done={steps_done}/9 "
            f"report_dir={report_dir.as_posix()} reason={reason}"
        )
        text = text.rstrip("\n") + "\n\n" + token_line + "\n"
    text = re.sub(r"^- final_token: .*$", f"- final_token: {token_line}", text, count=1, flags=re.M)
    summary.write_text(text, encoding="utf-8")
    return {"changed": True, "steps_done": steps_done, "skipped": skipped, "token": token_line}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="sentinel 报告目录 + summary.md 台账骨架初始化器",
    )
    parser.add_argument("--update", action="store_true", help="回填既有轮次的 source_ref/code_diff_type（幂等）")
    parser.add_argument("--finalize", action="store_true",
                        help="把残留 pending/running 补成终态并确保终局令牌存在（CI 在 agent 退出后调用，幂等）")
    parser.add_argument("--latest", action="store_true",
                        help="配合 --finalize：取 --report-root 下最新一轮（不传 --report-dir 时）")
    parser.add_argument("--status", default="blocked", choices=("blocked", "partial", "failed"),
                        help="--finalize 时写入令牌的 status 取值")
    parser.add_argument("--reason", default="外部超时或进程中断（auto-finalize）",
                        help="--finalize 时写入令牌的 reason")
    parser.add_argument("--report-dir", default="", help="报告目录（--update/--finalize 时必填，或用 --latest）")
    parser.add_argument("--report-root", default=REPORT_ROOT_DEFAULT, help="报告根目录")
    parser.add_argument("--story-id", default="", help="报告目录的 <story_id> 段")
    parser.add_argument("--source-ref", default="", help="规范化后的 source_ref（步骤 2 得出后回填）")
    parser.add_argument("--code-diff-type", default="", help="mr | commit | ci | branch")
    parser.add_argument("--locator", default="", help="change_locator（推导 story_id 的兜底输入）")
    parser.add_argument("--timestamp", default="", help="覆盖时间戳（YYYYMMDD-HHmmss）")
    parser.add_argument("--force", action="store_true", help="目录已存在时允许复用（默认拒绝）")
    args = parser.parse_args(argv)

    try:
        if args.finalize:
            if args.report_dir:
                report_dir = Path(args.report_dir).resolve()
            elif args.latest:
                report_dir = latest_report_dir(Path(args.report_root).resolve()).resolve()
            else:
                raise ScaffoldError("--finalize 必须给 --report-dir 或 --latest")
            result = finalize_report(report_dir, status=args.status, reason=args.reason)
            if not result["changed"]:
                print(f"SCAFFOLD FINALIZE SKIP（9 行已全终态且有令牌）steps_done={result['steps_done']}/9 "
                      f"dir={report_dir}")
                return 0
            print(f"SCAFFOLD FINALIZE OK steps_done={result['steps_done']}/9 "
                  f"skipped={result['skipped'] or '-'} dir={report_dir}")
            print(result["token"])
            return 0

        if args.update:
            if not args.report_dir:
                raise ScaffoldError("--update 必须同时给 --report-dir")
            report_dir = Path(args.report_dir).resolve()
            meta = update_existing(report_dir, args.source_ref, args.code_diff_type)
            print(f"SCAFFOLD UPDATE OK source_ref={meta.get('source_ref') or '-'} dir={report_dir}")
            return 0

        validate(args.source_ref, args.code_diff_type)
        timestamp = args.timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
        if not TIMESTAMP_RE.match(timestamp):
            raise ScaffoldError(f"--timestamp 必须为 YYYYMMDD-HHmmss，收到 {timestamp!r}")
        story_id = args.story_id.strip()
        if story_id and not STORY_ID_RE.match(story_id):
            raise ScaffoldError(f"--story-id 含非法字符：{story_id!r}")
        if not story_id:
            story_id = default_story_id(args.source_ref, args.code_diff_type, args.locator)

        report_root = Path(args.report_root)
        report_dir = report_root / f"{story_id}+{timestamp}"
        if report_dir.exists() and not args.force:
            raise ScaffoldError(f"报告目录已存在：{report_dir}（如需复用请显式 --force）")

        for sub in (".cache", "device", "tests"):
            (report_dir / sub).mkdir(parents=True, exist_ok=True)

        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        meta = {
            "source_ref": args.source_ref,
            "code_diff_type": args.code_diff_type,
            "story_id": story_id,
            "report_dir": str(report_dir),
            "report_root": str(report_root),
            "created_at": created_at,
            "steps": list(STEP_IDS),
        }
        (report_dir / ".cache" / "run-meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        ledger_template = dict(LEDGER_TEMPLATE)
        ledger_template["source_ref"] = args.source_ref
        ledger_template["code_diff_type"] = args.code_diff_type
        (report_dir / ".cache" / "risk-ledger.json").write_text(
            json.dumps(ledger_template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        write_summary(report_dir / "summary.md", meta)
    except (ScaffoldError, OSError, json.JSONDecodeError) as exc:
        print(f"SCAFFOLD FAIL: {exc}", file=sys.stderr)
        return 1

    print(f"SCAFFOLD OK dir={report_dir}")
    print(f"  run_meta={report_dir / '.cache' / 'run-meta.json'}")
    print(f"  ledger_template={report_dir / '.cache' / 'risk-ledger.json'}")
    print("  summary.md 已预置 9 行 pending：收尾前必须全部改为终态，否则 S1 无头收尾闸判 fail")
    return 0


if __name__ == "__main__":
    sys.exit(main())
