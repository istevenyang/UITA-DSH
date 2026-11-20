"""perf AI 分析：编排层（facts → 内置 DSH 无头直写 HTML → 事后校验）。

三段式：

  ① **事实层**（``facts.py``，确定性）：遍历批次产物 → ``facts.json`` / ``facts.md`` /
     ``facts-raw.md``，作为 agent 的数值锚与覆盖校验基准；
  ② **分析层**（``agent.py``，内置 DSH）：把 Skill ``perf-analysis`` 全文 + 本次任务段
     粘进 prompt，让 agent **直写** ``perf_analysis_report.html``（分段落盘）；
  ③ **校验层**（``verify.py``，确定性）：章节完整性 + ``data-fact-id`` 覆盖率 + 读数出现性，
     结果注入报告头部 banner 与末尾「附C 框架校验」。

纪律：

  - **绝不抛异常、绝不改变 perf 退出码**（分析是增量产物，失败只降级告警）；
  - **开关默认关**（``--perf-ai-analysis`` / ``UITA_PERF_AI_ANALYSIS=1``），不开关时本模块不产生
    任何文件、不起任何进程；
  - 失败原因分类可读（``timeout`` / ``no-report`` / ``facts-error`` …）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from uita import config
from uita.core.perf.analysis import agent as agent_mod
from uita.core.perf.analysis import fallback as fallback_mod
from uita.core.perf.analysis.facts import collect_facts, write_facts
from uita.core.perf.analysis.verify import (
    VerifyResult,
    build_gap_block,
    inject_gap_block,
    section_title,
    verify_report,
    write_verify,
)

logger = logging.getLogger("uita")

__all__ = [
    "AnalysisOutcome",
    "analysis_dir",
    "analysis_enabled",
    "build_prompt",
    "load_headless_text",
    "load_skill_text",
    "report_path_for",
    "run_analysis",
]

#: 分析产物子目录名（落在批次目录下）
ANALYSIS_SUBDIR = "perf_ai_analysis"
#: 分析报告文件名
REPORT_NAME = "perf_analysis_report.html"
#: Skill 相对路径（prompt 内嵌正文）
SKILL_REL = os.path.join(".dsh", "skills", "perf-analysis", "SKILL.md")
#: Skill 的无头执行契约（H4 分段落盘 / H6 回合预算 / H7 终局令牌）
HEADLESS_REL = os.path.join(".dsh", "skills", "perf-analysis", "reference",
                            "03-headless-contract.md")
STYLE_REL = os.path.join(".dsh", "skills", "perf-analysis", "reference",
                         "02-report-structure.md")
#: 报告"可用"的最低门槛（防半截文件被当成成品上传）
_MIN_REPORT_BYTES = 4096


def analysis_enabled(flag: bool = False) -> bool:
    """开关：命令行 ``--perf-ai-analysis`` 或 env ``UITA_PERF_AI_ANALYSIS``（二者取或）。"""
    return bool(flag) or bool(config.PERF_AI_ANALYSIS_ENABLED)


def analysis_dir(run_dir: str) -> str:
    return os.path.join(run_dir, ANALYSIS_SUBDIR)


def report_path_for(run_dir: str) -> str:
    return os.path.join(run_dir, REPORT_NAME)


@dataclass
class AnalysisOutcome:
    """一次分析的最终结果（可 json.dump；CLI 只消费 status/reason/report_path/verify）。"""

    status: str = "failed"              # ok | degraded | failed | skipped
    reason: str = ""
    report_path: str = ""
    run_dir: str = ""
    facts_paths: dict[str, str] = field(default_factory=dict)
    verify_path: str = ""
    verify: Optional[VerifyResult] = None
    agent_summary: str = ""
    elapsed_s: float = 0.0
    sections_missing: list[str] = field(default_factory=list)
    #: 报告由框架兜底渲染（模型未产出）——报告仍可用，但必须明确不是模型结论
    fallback: bool = False
    fallback_cause: str = ""

    @property
    def usable(self) -> bool:
        """能否作为入口报告（只有"报告可用"才允许顶替 perf_report.html）。"""
        return bool(self.report_path) and self.status in ("ok", "degraded")

    def summary_line(self) -> str:
        if self.status == "skipped":
            return "未开启（--perf-ai-analysis / UITA_PERF_AI_ANALYSIS）"
        parts = [f"{self.status}"]
        if self.fallback:
            parts.append(f"框架兜底报告（模型未产出：{self.fallback_cause or 'unknown'}）")
        if self.verify is not None:
            parts.append(self.verify.summary_line())
        elif self.reason:
            parts.append(self.reason)
        if self.agent_summary:
            parts.append(self.agent_summary)
        return "，".join(parts)

    def cli_line(self) -> str:
        """流水线可见的一行（前缀固定，便于 grep / 上报）。"""
        detail = self.summary_line()
        if self.report_path:
            detail += f"，报告 {self.report_path}"
        return f"PERF_AI_ANALYSIS: {detail}"


def load_skill_text(workdir: str = "") -> str:
    """读取 Skill 正文（prompt 内嵌用）；缺失返回空串（调用方按降级处理）。"""
    return _read_text(os.path.join(workdir or agent_mod.default_workdir(), SKILL_REL),
                      label="Skill")


def load_headless_text(workdir: str = "") -> str:
    """读取无头执行契约（H4/H6/H7）正文；缺失返回空串（prompt 里会显式标注缺失）。"""
    return _read_text(os.path.join(workdir or agent_mod.default_workdir(), HEADLESS_REL),
                      label="无头契约")


def _read_text(path: str, *, label: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        logger.warning("[perf-ai] 读取%s失败 %s：%s", label, path, exc)
        return ""


def _budget_lines(budget_s: int, stall_s: int, loop_repeat: int, grace_s: int = 0) -> list[str]:
    """prompt 里的「预算与看门狗」段：把框架真实闸门告诉 agent（它看不到代码）。"""
    budget = max(60, int(budget_s or config.PERF_AI_TIMEOUT_S))
    stall = int(stall_s or config.PERF_AI_STALL_S)
    loops = int(loop_repeat or config.PERF_AI_LOOP_REPEAT)
    grace = int(grace_s or config.PERF_AI_ARTIFACT_GRACE_S or max(300, budget // 2))
    skeleton_at = max(60, int(budget * 0.35))
    sections_at = max(120, int(budget * 0.80))
    lines = ["## 预算与看门狗（框架按秒强杀，违反即整轮作废）", ""]
    lines.append(f"- **总预算 {budget}s**：到点无条件终止，未完成的章节一律作废（不会有人工续跑）；")
    if stall:
        lines.append(f"- **静默 {stall}s 无任何输出 = 判定卡死并终止**：长命令不要静默等待，"
                     "拆成小步并保持输出；")
    if loops:
        lines.append(f"- **同一工具 + 同一参数重复 {loops} 次 = 判定循环调用并终止**："
                     "读同一文件不要反复重读，已读到的内容直接用；")
    lines += [
        f"- **第 {skeleton_at}s 前必须完成首次写盘**（先落含 9 个章节 id 的骨架）——"
        f"超过 {grace}s 报告仍一字节未落盘，框架按「无产物」终止；",
        f"- **第 {sections_at}s 前必须让 9 个章节 id 全部存在**（内容允许先写「缺口句 / 待澄清」），"
        "之后再用剩余预算逐章细化；宁可写缺口，不可缺章；",
        "- 工具名以当前会话实际可用工具为准：本 Skill 写的 `read_file` / `write_to_file` / "
        "`replace_in_file` 分别对应 `Read` / `Write` / `Edit`；不匹配时用等价工具，不要空转试探。",
        "",
    ]
    return lines


def build_prompt(
    *, skill_text: str, facts: dict[str, Any], facts_paths: dict[str, str],
    report_path: str, run_dir: str, model: str = "", notes: Iterable[str] = (),
    timeout_s: int = 0, stall_s: int = 0, loop_repeat: int = 0, headless_text: str = "",
    artifact_grace_s: int = 0, style_text: str = "",
) -> str:
    """组装 prompt：Skill + 报告结构 + 无头纪律 + 本次任务；路径一律绝对路径。"""
    tiers = "、".join(facts.get("tiers") or []) or "—"
    sections = "、".join(f"{sid}（{section_title(sid)}）" for sid in config.PERF_AI_REQUIRED_SECTIONS)
    coverage = facts.get("coverage") or {}
    compare = facts.get("compare") or {}
    compare_text = (f"`{compare.get('source')}`（{len(compare.get('metrics') or [])} 项可比指标）"
                    if compare.get("metrics") else f"无（{compare.get('reason') or '未启用'}）")
    lines = [
        skill_text.strip() if skill_text else "（Skill 正文缺失：按本任务段要求直接执行）",
        "",
        "---",
        "",
        "## 报告结构与简洁写法（优先遵守）",
        "",
        style_text.strip() if style_text else "（报告结构参考缺失：仍须首屏结论先行、明细折叠、档位有序且不重复）",
        "",
        "---",
        "",
        "## 无头执行契约（硬约束，框架会强杀违反者）",
        "",
        headless_text.strip() if headless_text else "（无头契约缺失：仍须遵守「先骨架、后逐章、必交卷」）",
        "",
        "---",
        "",
        "# 本次任务（由 uita perf 模式自动下发）",
        "",
        f"- 应用 / 平台：`{facts.get('app')}` / `{facts.get('platform')}`",
        f"- 档位 / 模式：{tiers}（{facts.get('mode')}）",
        f"- 批次目录：`{run_dir}`",
        f"- **数值锚（必读，报告读数只能来自这里）**：`{facts_paths.get('md')}`；"
        f"机器版 `{facts_paths.get('json')}`；参考层（逐轮原始读数/元信息/对比）`{facts_paths.get('raw_md')}`",
        f"- 原始产物（深挖用）：`{os.path.join(run_dir, 'perf_summary.json')}`、"
        f"`{os.path.join(run_dir, 'perf_metrics.csv')}`、`{run_dir}/tier-*/`",
        f"- **报告输出（直写这个绝对路径）**：`{report_path}`",
        f"- 必须包含的章节 id（框架会校验，缺章即降级）：{sections}",
        f"- 对比批次：{compare_text}",
        f"- 事实规模：共 {coverage.get('total')} 条，**必覆盖 {coverage.get('required')} 条**"
        f"（缺失会被计入「结论覆盖 N/M（缺口 K）」并逐条列在报告末尾）",
        f"- 模型：`{model or config.PERF_AI_MODEL}`；会话可回放（session id 见同目录 `session_id.txt`）",
    ]
    note_lines = [f"{index}. {text}" for index, text in enumerate(notes or [], start=1)]
    if note_lines:
        lines += [
            "",
            "## 批次产物备注（框架扫描发现，必须在报告里如实反映）",
            *note_lines,
        ]
    lines += [""] + _budget_lines(timeout_s, stall_s, loop_repeat, artifact_grace_s) + [
        "## 硬性要求（违反即视为不合格）",
        "1. **首屏先讲一句话结论、最重要的问题和下一步**；读表口径、逐轮与全部事实默认折叠；",
        "2. **每个必覆盖读数格带 `data-fact-id=\"<fact_id>\"`**，与 `facts.md` 逐字一致；",
        "3. 跨档矩阵只写一次；档位按事实包顺序写完整节，不要后补早期档位造成 3.4 先于 3.1；",
        "4. 体例遵循已内嵌的报告结构契约；",
        "5. 缺失数据不等于应用故障；单轮成功不等于稳定门槛；相关异常不等于同一根因；",
        "6. 不得引入 facts 之外的读数；无法判断的读数写进「六、待澄清问题」；",
        "7. **分段落盘**：先 Write 骨架（含上述章节 id 锚点），再逐章补写；",
        "8. 只读纪律：不得修改 `perf_summary.json` / `perf_metrics.csv` / facts 任何文件，"
        "不得改批次目录之外的任何文件，不得执行 git 写操作；",
        "9. **零反问、零等待**：信息不足就在报告里写「待澄清/缺口」，不要停下来提问；"
        "本会话已禁用一切需要人决策的工具（提问 / 结构化提问 / 计划模式 / 给人发消息），"
        "调用会被立即拒绝——别浪费回合照它；",
        f"10. 完成后输出终局令牌：`PERF_ANALYSIS_FINAL: status=<ok|partial> sections=<已写章节 id 逗号分隔> "
        f"fact_ids=<写入的 fact_id 数量> report={report_path}`。",
        "",
    ]
    return "\n".join(lines)


def _usable_report(path: str) -> bool:
    try:
        return os.path.getsize(path) >= _MIN_REPORT_BYTES
    except OSError:
        return False


def _write_prompt(out_dir: str, prompt: str) -> str:
    path = os.path.join(out_dir, "prompt.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(prompt)
    return path


def _write_json(path: str, payload: dict[str, Any]) -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _retryable(run: Optional[Any]) -> bool:
    """该次失败值不值得换新会话重试（环境类失败与鉴权失败重试无意义，只会重复烧额度）。"""
    if run is None:
        return False
    if str(getattr(run, "agent_error", "") or "") == "agent-auth":
        return False
    error = str(getattr(run, "error", "") or "")
    return not error.startswith(("missing-", "spawn-failed"))


def _retry_note(index: int, run: Optional[Any], budget_s: int) -> str:
    """重试时追加的任务段：把上一次的死因讲清楚，并强调"不许等人决策、先落骨架"。

    真机形态：无头会话若走"向用户确认 / 计划模式"这类需要人决策的路径，没人应答就是永久
    卡死。第一次失败后用**新会话**重试，并在提示里显式封死这条路。
    """
    reason = ""
    if run is not None:
        reason = str(getattr(run, "error", "") or "")
        if not reason and getattr(run, "timed_out", False):
            reason = "timeout"
    return "\n".join([
        "",
        "---",
        "",
        f"## 重试说明（第 {index} 次尝试，上一次已被框架强制终止）",
        "",
        f"- 上一次未产出可用报告，原因：`{reason or 'unknown'}`。",
        "- **本次禁止任何需要人决策的动作**：不得提问、不得请求确认、不得进入计划模式、"
        "不得等待输入；信息不足一律写进「六、待澄清问题」继续往下写。",
        f"- 本次预算 {budget_s}s：**第一件事就是写出骨架**（首屏摘要占位 + 9 个章节 id），"
        "按顺序逐章补写；长表折叠，早期档位不补写到文件末尾。",
        "- 上一次可能已写出部分内容：先 Read 现有报告文件，能续写就续写，不要推倒重来。",
        "- 最后一行仍须输出 `PERF_ANALYSIS_FINAL:` 令牌。",
        "",
    ])


def run_analysis(
    tiers: Iterable[tuple[str, Any]], *, app_name: str, run_dir: str, platform: str = "",
    workdir: str = "", model: str = "", timeout_s: int = 0, heartbeat_s: int = 0,
    compare_run: str = "auto", report_root: str = "", session_id: str = "",
    on_log: Optional[Callable[[str], None]] = None, runner: Optional[Callable[..., Any]] = None,
    skip_agent: bool = False, notes: Iterable[str] = (),
    stall_s: int = 0, loop_repeat: int = 0, artifact_grace_s: int = 0,
) -> AnalysisOutcome:
    """跑一次 perf AI 分析（**永不抛异常**）。

    Args:
        tiers: ``[(档位名, PerfRunResult)]``。
        runner: 注入点（默认真调内置 DSH 无头会话）；单测传入假 runner，避免真实进程。
        skip_agent: 只产事实包与校验骨架（``--perf-ai-backend none`` 的等价物 / 离线排查）。
        stall_s / loop_repeat / artifact_grace_s: 卡死闸门覆盖值（0 = 取 ``config``；
            ``artifact_grace_s`` 最终为 0 时自动取 ``max(300, 总预算的一半)``）。
    """
    log = on_log or (lambda text: logger.info("%s", text))
    started = time.monotonic()
    tier_list = list(tiers)
    outcome = AnalysisOutcome(run_dir=run_dir)
    out_dir = analysis_dir(run_dir)
    report_path = report_path_for(run_dir)
    outcome.report_path = report_path

    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        outcome.status, outcome.reason = "failed", f"mkdir-failed({exc})"
        log(f"[perf-ai] {outcome.cli_line()}")
        return outcome

    # 没有档位 = 没有可分析的数据：直接失败，**绝不无脑起一次无头会话**
    # （空跑一次 agent 既烧额度，又会让"报告已产出"变成假象）。
    if not tier_list:
        outcome.status, outcome.reason = "failed", "no-tiers"
        log("[perf-ai] 未发现任何档位数据，跳过分析")
        log(f"[perf-ai] {outcome.cli_line()}")
        return outcome

    # ── ① 事实层（确定性） ─────────────────────────────
    try:
        facts = collect_facts(
            tier_list, app_name=app_name, run_dir=run_dir,
            platform=platform or str(getattr(_first_result(tier_list), "platform", "")),
            compare_run=compare_run, report_root=report_root or os.path.dirname(run_dir),
        )
        outcome.facts_paths = write_facts(facts, out_dir)
        _write_json(os.path.join(out_dir, "facts_index.json"), {
            "schema_version": facts.get("schema_version"),
            "app": facts.get("app"), "platform": facts.get("platform"),
            "tiers": facts.get("tiers"), "coverage": facts.get("coverage"),
            "compare": facts.get("compare"), "generated_at": facts.get("generated_at"),
        })
    except Exception as exc:  # noqa: BLE001 - 事实层失败不允许影响 perf 主流程
        outcome.status, outcome.reason = "failed", f"facts-error({exc})"
        log(f"[perf-ai] 事实层失败：{exc}")
        log(f"[perf-ai] {outcome.cli_line()}")
        return outcome

    scenario_count = len(facts.get("scenarios") or [])
    log(f"[perf-ai] 事实包就绪：{facts.get('coverage', {}).get('required')} 条必覆盖 / "
        f"{scenario_count} 个场景；档位 {facts.get('tiers')}")

    if skip_agent:
        outcome.status, outcome.reason = "skipped", "skip-agent（未调模型）"
        outcome.elapsed_s = time.monotonic() - started
        log(f"[perf-ai] {outcome.cli_line()}")
        return outcome

    # ── ② 分析层（内置 DSH 直写 HTML） ───────────
    budget_s = timeout_s or config.PERF_AI_TIMEOUT_S
    stall_s = stall_s or config.PERF_AI_STALL_S
    loop_repeat = loop_repeat or config.PERF_AI_LOOP_REPEAT
    artifact_grace_s = (artifact_grace_s or config.PERF_AI_ARTIFACT_GRACE_S
                        or max(300, int(budget_s or 0) // 2))
    skill_text = load_skill_text(workdir)
    style_text = _read_text(os.path.join(workdir or agent_mod.default_workdir(), STYLE_REL),
                            label="报告结构")
    prompt = build_prompt(
        skill_text=skill_text, style_text=style_text, facts=facts, facts_paths=outcome.facts_paths,
        report_path=report_path, run_dir=run_dir,
        model=model or config.PERF_AI_MODEL, notes=list(notes or []),
        timeout_s=budget_s, stall_s=stall_s, loop_repeat=loop_repeat,
        artifact_grace_s=artifact_grace_s, headless_text=load_headless_text(workdir),
    )
    prompt_path = _write_prompt(out_dir, prompt)
    session_id = session_id or agent_mod.session_id_for(app=app_name, run_id=os.path.basename(run_dir))
    session_file = os.path.join(out_dir, "session_id.txt")
    with open(session_file, "w", encoding="utf-8") as fh:
        fh.write(session_id)
    log(f"[perf-ai] prompt 已落盘 {prompt_path}（{len(prompt)} 字符）；session={session_id}")
    log(f"[perf-ai] 看门狗：总超时 {budget_s}s／静默 {stall_s or '关'}s／"
        f"循环 {loop_repeat or '关'}次／产物宽限 {artifact_grace_s}s")

    call = runner or agent_mod.run_agent
    attempts = 1 + max(0, int(config.PERF_AI_RETRY_ATTEMPTS))
    retry_budget_s = max(300, int(budget_s or 0) // 2)
    run = None
    for index in range(1, attempts + 1):
        attempt_session = session_id if index == 1 else f"{session_id}-r{index}"
        attempt_prompt = prompt if index == 1 else prompt + _retry_note(index, run, retry_budget_s)
        attempt_budget = budget_s if index == 1 else retry_budget_s
        try:
            run = call(
                attempt_prompt, workdir=workdir or agent_mod.default_workdir(),
                model=model or config.PERF_AI_MODEL, session_id=attempt_session,
                timeout_s=attempt_budget,
                heartbeat_s=heartbeat_s or config.PERF_AI_HEARTBEAT_S,
                out_dir=out_dir, on_log=log,
                stall_timeout_s=stall_s, loop_repeat=loop_repeat,
                artifact_path=report_path, artifact_grace_s=artifact_grace_s,
            )
        except Exception as exc:  # noqa: BLE001 - 注入点异常同样只降级
            run = None
            outcome.reason = f"agent-call-failed({exc})"
            log(f"[perf-ai] 无头调用异常：{exc}")
        if run is not None:
            outcome.agent_summary = run.summary()
            _write_json(os.path.join(out_dir, "agent_run.json" if index == 1
                                     else f"agent_run-r{index}.json"), {
                "attempt": index, "session_id": attempt_session, "timeout_s": attempt_budget,
                "command": run.command, "returncode": run.returncode, "elapsed_s": run.elapsed_s,
                "timed_out": run.timed_out, "truncated": run.truncated, "usage": run.usage,
                "error": run.error,
                # 带内失败（rc=0 但事件里 is_error）：真因详情只落这里 + 日志，便于事后定位
                "agent_error": getattr(run, "agent_error", ""),
                "error_detail": getattr(run, "error_detail", ""),
                # 凭据来源与尝试次数（>1 说明鉴权失败后换过凭据；只记来源，不记 Key 本体）
                "credentials_source": getattr(run, "credentials_source", ""),
                "attempts": getattr(run, "attempts", 0),
                # 卡死闸门的现场（事后区分"卡住"与"写得慢"的唯一证据）
                "stalled": getattr(run, "stalled", False),
                "max_silence_s": round(float(getattr(run, "max_silence_s", 0.0) or 0.0), 1),
                "loop_signature": getattr(run, "loop_signature", ""),
                "loop_count": getattr(run, "loop_count", 0),
                "artifact_seen": getattr(run, "artifact_seen", False),
                "watchdogs": {
                    "timeout_s": attempt_budget, "stall_s": stall_s, "loop_repeat": loop_repeat,
                    "artifact_grace_s": artifact_grace_s,
                },
                "stdout": run.stdout_path, "stderr": run.stderr_path,
            })
            if run.error and not outcome.reason:
                outcome.reason = run.error
        if _usable_report(report_path):
            break
        if index < attempts and _retryable(run):
            log(f"[perf-ai] 第 {index} 次尝试未产出可用报告（{outcome.reason or 'no-report'}）"
                f"→ 换新会话重试（{index + 1}/{attempts}，预算 {retry_budget_s}s）")
            outcome.reason = ""      # 上一次的原因只属于上一次；重试结果重新判定
            continue
        break

    # ── ②b 兜底报告：模型无论如何没产出，也要有一份合规报告（不猜数、只回放读数） ──
    fallback_written = False
    if not _usable_report(report_path) and config.PERF_AI_FALLBACK_ENABLED:
        cause = outcome.reason or (
            "timeout" if (run is not None and getattr(run, "timed_out", False)) else "no-report"
        )
        try:
            fallback_mod.write_fallback_report(
                report_path, facts, cause=cause, model=model or config.PERF_AI_MODEL,
                session_id=session_id, generated_at=_timestamp(),
            )
            outcome.fallback, outcome.fallback_cause = True, cause
            fallback_written = True
            log(f"[perf-ai] 模型未产出可用报告（{cause}）→ 已用事实包渲染兜底报告：{report_path}")
        except Exception as exc:  # noqa: BLE001 - 兜底渲染失败仍按无报告收尾
            log(f"[perf-ai] 兜底报告渲染失败：{exc}")

    # ── ③ 校验层（确定性；报告在就校验 + 注入缺口） ────
    # 兜底报告由框架按事实包逐条渲染、"完整性"由构造保证，故不再受"最小体积"门槛约束
    if not _usable_report(report_path) and not fallback_written:
        outcome.status = "failed"
        if run is not None and getattr(run, "stalled", False):
            outcome.reason = outcome.reason or "stalled"
        else:
            outcome.reason = outcome.reason or (
                "timeout" if (run is not None and run.timed_out) else "no-report"
            )
        outcome.elapsed_s = time.monotonic() - started
        log(f"[perf-ai] 未产出可用报告（{outcome.reason}）：{report_path}")
        log(f"[perf-ai] {outcome.cli_line()}")
        return outcome

    try:
        verify = verify_report(report_path, facts, required_sections=config.PERF_AI_REQUIRED_SECTIONS)
        verify_path = os.path.join(out_dir, "verify.json")
        write_verify(verify_path, verify)
        outcome.verify, outcome.verify_path = verify, verify_path
        outcome.sections_missing = list(verify.sections_missing)
        banner, section = build_gap_block(verify, facts)
        inject_gap_block(report_path, banner, section)
        outcome.status = "ok" if (verify.status == "ok" and not verify.gap_count
                                  and not verify.sections_missing) else "degraded"
        outcome.reason = outcome.reason or ("gap-visible" if outcome.status == "degraded" else "")
        if outcome.status == "ok":
            # 报告已通过校验 → 进程侧带内告警不再是降级理由（如实留在 agent_run.json 与日志里）
            outcome.reason = ""
    except Exception as exc:  # noqa: BLE001 - 校验失败不影响"报告可用"这一事实
        outcome.status = "degraded"
        outcome.reason = f"verify-error({exc})"
        log(f"[perf-ai] 校验失败（报告仍可用）：{exc}")

    if outcome.fallback:
        # 兜底报告可用（可上传），但结论不是模型给的分析，状态必须是 degraded 且原因如实
        outcome.status = "degraded"
        outcome.reason = f"fallback-report({outcome.fallback_cause or 'unknown'})"

    outcome.elapsed_s = time.monotonic() - started
    log(f"[perf-ai] {outcome.cli_line()}")
    return outcome


def _first_result(tiers: Iterable[tuple[str, Any]]) -> Any:
    for _name, result in tiers:
        return result
    return None
