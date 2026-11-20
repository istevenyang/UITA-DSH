#!/usr/bin/env python3
"""sentinel 机械计数/推导器（风险账本 + 历史缺陷降噪漏斗）。

用途
----
把**纯算术**从模型手里拿走，只留语义判定给模型：

- `risk`：读本轮决策账本 `.cache/risk-ledger.json`（模型在步骤 3/5 落盘的逐条风险决策），
  推导并校验 —— 风险漏斗六阶段计数、`uneliminated_risk_count`（含等级/优先级分布）、
  `device_required[]`、`summary.risk_ledger`、`summary.eliminated_counts`、
  `summary.ignored_unreachable`、`defects[]` 待入池编号；可选对 `07-device-verification.md`
  的 `### R<n>` 记录集合做一一对应校验（多出=硬凑、缺失=漏跑）。
- `funnel`：读本轮候选终态账本 `.cache/defect-funnel.json`（模型在步骤 2 落盘的三来源候选 + 每
  条的终态），推导历史缺陷降噪漏斗全部计数并校验 11 项守恒式。

硬边界（不得突破）
------------------
1. **只做确定性统计与格式化，不产出任何语义结论**：不判风险、不定关联等级、不给 verdict、
   不做采纳/忽略决策——这些仍由模型判定后写进输入账本；
2. 输入必须已落盘且字段齐全；缺字段即 exit 2（写 `unknown + 原因`），**禁止**填 0 或拟合；
3. 输出仍须过 `00-guard-scripts.md` 的 S2/S3/S5e/S5f/S6 反向校验——本脚本算错会被守卫抓住；
4. 本脚本**不是守卫**：S1–S6 仍只能现场 heredoc 内联执行，严禁并入本文件。

用法
----
    .venv/bin/python ci/sentinel_ledger.py risk --ledger <报告目录>/.cache/risk-ledger.json \
        [--out <报告目录>/.cache/ledger-out.json] [--device-records <报告目录>/07-device-verification.md]
    .venv/bin/python ci/sentinel_ledger.py funnel --funnel <报告目录>/.cache/defect-funnel.json \
        [--md <报告目录>/.cache/defect-funnel.md]

退出码：0=OK；1=算错/不自洽（对应步骤记 `blocked`）；2=字段缺失或入参非法（记 `unknown + 原因`）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ── 风险账本口径常量（R17 标准契约）──
RISK_LEVELS = ("high", "medium", "low", "undetermined")
RISK_PRIORITIES = ("P0", "P1", "P2")
COVERED_METHOD_KEYS = ("unit", "device", "lock", "baseline", "accepted")
STATUSES = ("eliminated", "uneliminated", "ignored")
ROUTES = ("unit", "device", "unit+device", "accept", "ignore")
REACHABILITY = ("ui_reachable", "device_reachable", "unreachable")
UNREACHABLE_TYPES = (
    "ui_unreachable", "ui_no_entry", "ui_and_device_unreachable", "external_uncontrolled",
)
RISK_KEY_RE = re.compile(r"^R[1-9]\d*$")
DIMENSION_CODES = ("business", "function", "logic", "data", "state", "architecture")
# 导入器 `_LOCATION_RE` 允许 `/` 与 `-`；本脚本额外禁止 `/`——S3 对 `defects[]` 的
# `defect_location` 做了 `/`→`.` 归一，两侧不一致会造成同一缺陷被去重成两条（真实缺口）。
LOCATION_RE = re.compile(r"^[\w][\w.\- ]*(::\d+)$")

# ── 历史缺陷降噪漏斗口径常量（与 S2/G3 守恒式同源）──
TERMINALS = (
    "filtered_content_empty", "filtered_content_unrelated", "filtered_status",
    "filtered_stale", "filtered_module", "filtered_unmappable", "filtered_unfalsifiable",
    "excluded_weak", "candidate_overflow", "adopted_strong", "adopted_medium",
    "review_pending",
)
# 正交观察计数（不参与守恒式）
ORTHOGONAL_FLAGS = ("metadata_missing", "stale_relaxed", "severity_priority_below_threshold",
                    "story_module_mismatch", "multi_source")
RETRIEVAL_SOURCES = ("keyword_a", "story_related_b", "history_story_c")
FILTER_WARN_RANGE = (0.15, 0.35)


class LedgerError(RuntimeError):
    """输入字段缺失/非法 → exit 2（写 `unknown + 原因`，不得拟合）。"""


class LedgerInconsistent(RuntimeError):
    """算出来不自洽（守恒式破坏）→ exit 1（该步 blocked）。"""


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise LedgerError(message)


def _int(value: object, field: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= 0,
             f"{field} 必须为非负整数，收到 {value!r}")
    return int(value)


# ──────────────────────────────────────────────────────────────────────────── #
# risk 子命令
# ──────────────────────────────────────────────────────────────────────────── #
def _device_in_route(route: str) -> bool:
    return "device" in route.split("+")


def _validate_risk(raw: object, index: int) -> dict:
    _require(isinstance(raw, dict), f"risks[{index}] 必须是对象")
    risk = dict(raw)
    key = risk.get("risk_key")
    _require(isinstance(key, str) and RISK_KEY_RE.match(key),
             f"risks[{index}].risk_key 必须形如 R<n>，收到 {key!r}")
    level = risk.get("risk_level")
    _require(level in RISK_LEVELS, f"{key}.risk_level 必须属于 {RISK_LEVELS}，收到 {level!r}")
    status = risk.get("status")
    _require(status in STATUSES, f"{key}.status 必须属于 {STATUSES}，收到 {status!r}")
    route = risk.get("route")
    _require(route in ROUTES, f"{key}.route 必须属于 {ROUTES}，收到 {route!r}")
    item_type = risk.get("item_type") or "risk"
    _require(item_type in ("risk", "defect"), f"{key}.item_type 必须为 risk|defect")
    _require(isinstance(risk.get("title"), str) and risk["title"].strip(),
             f"{key}.title 必填（报告与 JSON 都要用）")
    codes = risk.get("dimension_codes")
    _require(isinstance(codes, str) and codes.strip(),
             f"{key}.dimension_codes 必填（英文逗号分隔一至多维）")
    parts = [part.strip() for part in codes.split(",") if part.strip()]
    _require(parts and all(part in DIMENSION_CODES for part in parts),
             f"{key}.dimension_codes 只允许 {DIMENSION_CODES} 中的值，收到 {codes!r}")
    _require(parts == sorted(parts, key=DIMENSION_CODES.index),
             f"{key}.dimension_codes 必须按 {DIMENSION_CODES} 固定顺序书写，收到 {codes!r}")

    method = risk.get("covered_method")
    if status == "eliminated":
        _require(method in COVERED_METHOD_KEYS,
                 f"{key}.covered_method 在 status=eliminated 时必填，且属于 {COVERED_METHOD_KEYS}")
        _require((method == "accepted") == (route == "accept"),
                 f"{key}: route=accept 与 covered_method=accepted 必须同时成立")
        # 只做单向约束：route 是步骤 5 的路由决策（先于单测执行），故 route=unit+device
        # 而单测已消除（covered_method=unit）是合法情形——真机此时不要求执行。
        if method == "device":
            _require(_device_in_route(route),
                     f"{key}: covered_method=device 要求 route 含 device，收到 {route!r}")
    elif method is not None:
        raise LedgerError(f"{key}.covered_method 只允许出现在 status=eliminated 条目上")

    if status == "ignored":
        _require(route == "ignore", f"{key}: status=ignored 时 route 必须为 ignore")
        _require(risk.get("unreachable_type") in UNREACHABLE_TYPES,
                 f"{key}.unreachable_type 必须属于 {UNREACHABLE_TYPES}")
        for field in ("judgement_evidence", "ignore_reason", "escalation_trigger"):
            _require(isinstance(risk.get(field), str) and risk[field].strip(),
                     f"{key}.{field} 必填（无忽略原因/升级触发条件的条目不得忽略）")
    else:
        _require(risk.get("reachability") in REACHABILITY,
                 f"{key}.reachability 必须属于 {REACHABILITY}")

    if item_type == "defect":
        loc = risk.get("defect_location")
        _require(isinstance(loc, str) and LOCATION_RE.match(loc),
                 f"{key}.defect_location 必须形如 路径.文件名.扩展名::行号 且不含 `/`，收到 {loc!r}")
        _require(isinstance(risk.get("coverage_target"), str) and risk["coverage_target"].strip(),
                 f"{key}.coverage_target 必填")

    if status == "uneliminated":
        _require(risk.get("risk_priority") in RISK_PRIORITIES,
                 f"{key}.risk_priority 必填（未消除条目逐条必填 P0/P1/P2）")
    elif risk.get("risk_priority") not in (None, "") and risk["risk_priority"] not in RISK_PRIORITIES:
        raise LedgerError(f"{key}.risk_priority 必须属于 {RISK_PRIORITIES}")

    verdict = risk.get("device_verdict")
    if _device_in_route(route):
        _require(verdict in ("pass", "fail", "inconclusive", None),
                 f"{key}.device_verdict 必须属于 pass|fail|inconclusive（未执行为 null）")
        if verdict == "pass":
            _require(status == "eliminated" and method == "device",
                     f"{key}: 真机 verdict=pass 即计真机消除，status 必须为 eliminated 且 covered_method=device")
        if verdict == "fail":
            _require(status == "uneliminated" and item_type == "defect",
                     f"{key}: 真机 verdict=fail 必须计入 defects[]（status=uneliminated + item_type=defect）")
    elif verdict not in (None, ""):
        raise LedgerError(f"{key}.device_verdict 只允许出现在 route 含 device 的条目上")

    risk["item_type"] = item_type
    risk["route"] = route
    return risk


def build_risk_view(ledger: dict, device_records: list[str] | None = None) -> dict:
    _require(isinstance(ledger, dict), "决策账本必须是 JSON 对象")
    raw_risks = ledger.get("risks")
    _require(isinstance(raw_risks, list) and raw_risks, "决策账本 risks 必须为非空数组")
    risks = [_validate_risk(item, i) for i, item in enumerate(raw_risks)]
    keys = [r["risk_key"] for r in risks]
    _require(len(set(keys)) == len(keys), f"risk_key 重复：{[k for k in keys if keys.count(k) > 1]}")

    defect_keys: set[tuple[str, str]] = set()
    for r in risks:
        if r["item_type"] == "defect":
            dedup = (r["defect_location"], r["coverage_target"])
            _require(dedup not in defect_keys, f"缺陷键重复（defect_location + coverage_target）：{dedup}")
            defect_keys.add(dedup)

    eliminated = [r for r in risks if r["status"] == "eliminated"]
    pending = [r for r in risks if r["status"] == "uneliminated"]
    ignored = [r for r in risks if r["status"] == "ignored"]

    def _method_count(method: str) -> int:
        return len([r for r in eliminated if r["covered_method"] == method])

    accepted_count = _method_count("accepted")
    ignored_count = len(ignored)
    covered_count = len(eliminated) + ignored_count
    pending_defect_count = len([r for r in pending if r["item_type"] == "defect"])
    noise_count = _int(ledger.get("noise_count", 0), "noise_count")

    covered_methods = {
        "unit": _method_count("unit"),
        "device": _method_count("device"),
        "lock": _method_count("lock"),
        "baseline": _method_count("baseline"),
        "accepted": accepted_count + ignored_count,
    }
    unit_covered = covered_methods["unit"] + covered_methods["lock"] + covered_methods["baseline"]
    device_covered = covered_methods["device"]
    total_count = len(pending) + covered_count

    conservation = unit_covered + device_covered + accepted_count + ignored_count + len(pending)
    if conservation != total_count:
        raise LedgerInconsistent(
            f"风险漏斗守恒式破坏：总数 {total_count} != 单测覆盖 {unit_covered} + 真机覆盖 "
            f"{device_covered} + 接受类 {accepted_count} + 不可达忽略 {ignored_count} + 待人工确认 {len(pending)}",
        )

    device_required = [
        r["risk_key"] for r in risks
        if _device_in_route(r["route"]) and r["status"] != "ignored"
        and not (r["status"] == "eliminated" and r["covered_method"] != "device")
    ]
    if device_records is not None:
        missing = sorted(set(device_required) - set(device_records), key=_rkey)
        extra = sorted(set(device_records) - set(device_required), key=_rkey)
        if missing or extra:
            raise LedgerInconsistent(
                f"真机清单与 07 记录不一致：漏跑={missing or '无'}，硬凑={extra or '无'}"
                "（device_required 只列路由到真机且单测未消除的编号）",
            )

    level_distribution = {level: 0 for level in RISK_LEVELS}
    priority_distribution = {priority: 0 for priority in RISK_PRIORITIES}
    for r in pending:
        level_distribution[r["risk_level"]] += 1
        priority_distribution[r["risk_priority"]] += 1

    ledger_block = {
        "schema_version": 1,
        "total_count": total_count,
        "pending_count": len(pending),
        "pending_defect_count": pending_defect_count,
        "covered_count": covered_count,
        "accepted_count": accepted_count,
        "noise_count": noise_count,
        "covered_methods": covered_methods,
        "pending_level_distribution": level_distribution,
        "pending_priority_distribution": priority_distribution,
    }
    return {
        "risk_ledger": ledger_block,
        "uneliminated_risk_count": len(pending),
        "funnel": {
            "risk_total_count": total_count,
            "noise_count": noise_count,
            "unit_covered_count": unit_covered,
            "device_covered_count": device_covered,
            "accepted_count": accepted_count,
            "ignored_unreachable_count": ignored_count,
            "pending_count": len(pending),
        },
        "eliminated_counts": {
            "caliber": "忽略即已消除（机器侧以旁路计数承载）",
            "run_level": {
                "lock": covered_methods["lock"],
                "ignored_unreachable": ignored_count,
                "total": covered_count,
            },
            "dimension_ledger_unit_eliminated": None,
        },
        "ignored_unreachable": {
            "count": ignored_count,
            "items": [
                {
                    "risk_key": r["risk_key"],
                    "risk_level": r["risk_level"],
                    "dimension_codes": r.get("dimension_codes", ""),
                    "unreachable_type": r.get("unreachable_type"),
                    "judgement_evidence": r.get("judgement_evidence"),
                    "ignore_reason": r.get("ignore_reason"),
                    "escalation_trigger": r.get("escalation_trigger"),
                }
                for r in ignored
            ],
        },
        "device_required": device_required,
        "pending_defect_keys": [
            {"risk_key": r["risk_key"], "item_type": "defect",
             "defect_location": r["defect_location"], "coverage_target": r["coverage_target"]}
            for r in pending if r["item_type"] == "defect"
        ],
    }


def _rkey(key: str) -> int:
    return int(key[1:]) if RISK_KEY_RE.match(key) else 0


def _load_json(path: Path, field: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise LedgerError(f"缺文件：{path}（{field}）") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError(f"{path} 不是合法 JSON：{exc}") from exc
    _require(isinstance(payload, dict), f"{path} 顶层必须是 JSON 对象")
    return payload


def _device_records_from(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LedgerError(f"读不到 {path}：{exc}") from exc
    hits = re.findall(r"^###\s*(R[1-9]\d*)", text, flags=re.M)
    return sorted(set(hits), key=_rkey)


def run_risk(args: argparse.Namespace) -> int:
    ledger = _load_json(Path(args.ledger), "决策账本")
    records = None
    if args.device_records:
        records = _device_records_from(Path(args.device_records))
    view = build_risk_view(ledger, records)
    ledger_block = view["risk_ledger"]
    funnel = view["funnel"]

    print("LEDGER OK")
    print(f"  source_ref={ledger.get('source_ref') or '-'}  噪音剔除={funnel['noise_count']}（独立量纲）")
    print(
        f"  funnel: 风险总数 {funnel['risk_total_count']} = 单测覆盖 {funnel['unit_covered_count']}"
        f" + UITA真机 {funnel['device_covered_count']} + 接受类 {funnel['accepted_count']}"
        f"（另有不可达忽略 {funnel['ignored_unreachable_count']}） + 待人工确认 {funnel['pending_count']}"
    )
    print(
        f"  pending_count={ledger_block['pending_count']} pending_defect_count={ledger_block['pending_defect_count']}"
        f" covered_count={ledger_block['covered_count']} accepted_count={ledger_block['accepted_count']}"
    )
    print(
        "  pending_level_distribution: "
        + " / ".join(f"{k} {v}" for k, v in ledger_block["pending_level_distribution"].items())
        + " | pending_priority_distribution: "
        + " / ".join(f"{k} {v}" for k, v in ledger_block["pending_priority_distribution"].items())
    )
    print(f"  device_required({len(view['device_required'])}): {view['device_required']}")
    if view["ignored_unreachable"]["count"]:
        print("  ignored_unreachable: "
              + "; ".join(f"{i['risk_key']}({i['unreachable_type']})"
                          for i in view["ignored_unreachable"]["items"]))
    for name in ("risk_ledger", "eliminated_counts", "ignored_unreachable", "device_required"):
        print(f"--- {name} ---")
        print(json.dumps(view[name], ensure_ascii=False, indent=2))
    print("→ 上述块逐字写入 `utest-defects.json`（`risk_ledger` 进 `summary.risk_ledger`；"
          "`device_required` 交步骤 7；`eliminated_counts` 的 "
          "`dimension_ledger_unit_eliminated` 由模型按六维台账补）")
    if args.out:
        Path(args.out).write_text(
            json.dumps(view, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(f"ledger_out={args.out}")
    return 0


# ──────────────────────────────────────────────────────────────────────────── #
# funnel 子命令
# ──────────────────────────────────────────────────────────────────────────── #
def build_funnel_view(payload: dict) -> dict:
    candidates = payload.get("candidates")
    _require(isinstance(candidates, list), "defect-funnel.candidates 必须是数组")
    channels = payload.get("channels") or {}
    _require(isinstance(channels, dict), "defect-funnel.channels 必须是对象")
    for field in ("keyword_channel_raw", "keyword_channel_dedup",
                  "story_related_raw", "story_related_dedup",
                  "history_story_raw", "history_story_qualified",
                  "history_story_defects_raw", "history_story_defects_dedup"):
        if field in channels:
            _int(channels[field], f"channels.{field}")

    terminals = {name: 0 for name in TERMINALS}
    flags = {name: 0 for name in ORTHOGONAL_FLAGS}
    seen_ids: set[str] = set()
    for index, item in enumerate(candidates):
        _require(isinstance(item, dict), f"candidates[{index}] 必须是对象")
        bug_id = str(item.get("bug_id") or "").strip()
        _require(bool(bug_id), f"candidates[{index}].bug_id 必填")
        _require(bug_id not in seen_ids, f"candidates[{index}].bug_id 重复：{bug_id}（先按 bug_id 去重再落盘）")
        seen_ids.add(bug_id)
        terminal = item.get("terminal")
        _require(terminal in TERMINALS,
                 f"candidates[{index}].terminal 必须属于 11 项硬过滤/采纳终态 + review_pending，收到 {terminal!r}")
        terminals[terminal] += 1
        sources = item.get("retrieval_sources") or []
        _require(isinstance(sources, list) and set(sources) <= set(RETRIEVAL_SOURCES),
                 f"candidates[{index}].retrieval_sources 只允许 {RETRIEVAL_SOURCES}")
        multi = len(set(sources)) >= 2
        _require(not item.get("multi_source") or multi,
                 f"candidates[{index}]: 仅 1 个来源不得标 multi_source")
        for name in ORTHOGONAL_FLAGS:
            if name == "multi_source":
                flags[name] += int(multi)
            elif item.get(name):
                flags[name] += 1

    dedup = len(candidates)
    total_split = sum(terminals.values())
    if total_split != dedup:
        raise LedgerInconsistent(
            f"漏斗守恒式破坏：dedup_candidates({dedup}) != 12 项终态计数之和({total_split})",
        )

    filtered_out = sum(count for name, count in terminals.items() if name.startswith("filtered_"))
    mappable = dedup - filtered_out
    adopted = terminals["adopted_strong"] + terminals["adopted_medium"]
    adopted_ratio = (adopted / mappable) if mappable else None
    raw_candidates = sum(
        _int(channels.get(k, 0), f"channels.{k}")
        for k in ("keyword_channel_raw", "story_related_raw", "history_story_defects_raw")
    ) or dedup

    story_related = payload.get("story_related", "not_applicable")
    history_story = payload.get("history_story", "not_applicable")
    _require(story_related in ("executed", "not_applicable"), "story_related 必须为 executed|not_applicable")
    _require(history_story in ("executed", "not_applicable"), "history_story 必须为 executed|not_applicable")
    if payload.get("issue_url_verified") and story_related != "executed":
        raise LedgerInconsistent(
            "有可信需求单链接而来源 B（需求关联通道）未执行：检索不完整，该步须记 blocked",
        )
    if payload.get("symbols_located") and history_story != "executed":
        raise LedgerInconsistent(
            "diff 已定位符号/模块而来源 C（历史需求关联通道）未执行：检索不完整，该步须记 blocked",
        )
    if story_related == "not_applicable" and not payload.get("story_related_reason"):
        raise LedgerError("story_related=not_applicable 时必须写 story_related_reason")
    if history_story == "not_applicable" and not payload.get("history_story_reason"):
        raise LedgerError("history_story=not_applicable 时必须写 history_story_reason")

    recall_truncated = _int(payload.get("recall_truncated", 0), "recall_truncated")
    view = {
        "retrieval_mode": payload.get("retrieval_mode") or "unknown",
        "retrieval_channels": payload.get("retrieval_channels") or [],
        "queries": payload.get("queries") or [],
        "stale_cutoff_at": payload.get("stale_cutoff_at") or "unknown",
        "stale_cutoff_at_related": payload.get("stale_cutoff_at_related") or "unknown",
        "stale_cutoff_at_story": payload.get("stale_cutoff_at_story") or "unknown",
        "story_related": story_related,
        "history_story": history_story,
        "story_related_reason": payload.get("story_related_reason") or "",
        "history_story_reason": payload.get("history_story_reason") or "",
        "raw_candidates": raw_candidates,
        "dedup_candidates": dedup,
        "terminals": terminals,
        "mappable": mappable,
        "adopted_strong": terminals["adopted_strong"],
        "adopted_medium": terminals["adopted_medium"],
        "adopted_ratio": adopted_ratio,
        "filter_warning": bool(adopted_ratio is not None and not (
            FILTER_WARN_RANGE[0] <= adopted_ratio <= FILTER_WARN_RANGE[1])),
        "recall_truncated": recall_truncated,
        "recall_warning": recall_truncated > 0,
        "orthogonal": flags,
        "channels": {k: v for k, v in channels.items()
                     if k.startswith(("keyword_channel_", "story_related_", "history_story_"))},
    }
    return view


def render_funnel_markdown(view: dict) -> str:
    lines = [
        "### 三来源召回与降噪漏斗（计数由 `ci/sentinel_ledger.py funnel` 计算，勿手改）",
        "",
        f"- retrieval_mode：`{view['retrieval_mode']}`；channels：{view['retrieval_channels'] or '—'}",
        f"- 查询词：{view['queries'] or '—'}",
        f"- 陈旧度截止：A `{view['stale_cutoff_at']}` / B `{view['stale_cutoff_at_related']}`"
        f" / 历史需求 `{view['stale_cutoff_at_story']}`",
        f"- 来源 B 状态：`{view['story_related']}`（{view['story_related_reason'] or '—'}）"
        f"；来源 C 状态：`{view['history_story']}`（{view['history_story_reason'] or '—'}）",
        "",
        "| 阶段 | 计数 |",
        "|---|---|",
        f"| raw_candidates | {view['raw_candidates']} |",
        f"| dedup_candidates | {view['dedup_candidates']} |",
    ]
    lines += [f"| {name} | {count} |" for name, count in view["terminals"].items()]
    lines += [
        f"| mappable | {view['mappable']} |",
        f"| adopted_ratio | "
        f"{'n/a' if view['adopted_ratio'] is None else format(view['adopted_ratio'], '.2%')} |",
        "",
        "守恒式：`dedup_candidates == "
        + " + ".join(view["terminals"])
        + f"` → {view['dedup_candidates']} == {sum(view['terminals'].values())} ✓",
        "",
        "正交观察计数：" + "；".join(f"`{k}` {v}" for k, v in view["orthogonal"].items())
        + f"；`recall_truncated` {view['recall_truncated']}"
        + ("（**检索不完整**，须在报告中提示并优先扩召回）" if view["recall_warning"] else ""),
        "",
        "渠道计数：" + ("；".join(f"`{k}` {v}" for k, v in view["channels"].items()) or "—"),
    ]
    return "\n".join(lines) + "\n"


def run_funnel(args: argparse.Namespace) -> int:
    payload = _load_json(Path(args.funnel), "历史缺陷候选终态账本")
    view = build_funnel_view(payload)
    markdown = render_funnel_markdown(view)
    print("DEFECT FUNNEL OK")
    print(markdown)
    if view["filter_warning"]:
        print("⚠️ filter_warning：adopted_ratio 落在 15%~35% 参考区间之外（仅提示人工复核，不自动改判）")
    if args.md:
        Path(args.md).write_text(markdown, encoding="utf-8")
        print(f"defect_funnel_md={args.md}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sentinel 机械计数/推导器")
    sub = parser.add_subparsers(dest="command", required=True)

    risk = sub.add_parser("risk", help="风险账本：漏斗/计数/risk_ledger/device_required 推导")
    risk.add_argument("--ledger", required=True, help=".cache/risk-ledger.json（模型落盘的决策账本）")
    risk.add_argument("--out", default="", help="可选：把推导结果落盘到 .cache/ledger-out.json")
    risk.add_argument("--device-records", default="",
                      help="可选：07-device-verification.md，用于校验 ### R<n> 记录集合与 device_required 一一对应")

    funnel = sub.add_parser("funnel", help="历史缺陷降噪漏斗：计数 + 守恒校验 + 快照块")
    funnel.add_argument("--funnel", required=True, help=".cache/defect-funnel.json（模型落盘的候选终态账本）")
    funnel.add_argument("--md", default="", help="可选：把漏斗块落盘为 .cache/defect-funnel.md")

    args = parser.parse_args(argv)
    try:
        if args.command == "risk":
            return run_risk(args)
        return run_funnel(args)
    except LedgerInconsistent as exc:
        print(f"LEDGER FAIL: {exc}", file=sys.stderr)
        return 1
    except LedgerError as exc:
        print(f"LEDGER FIELD MISSING: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"LEDGER FAIL(IO): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
