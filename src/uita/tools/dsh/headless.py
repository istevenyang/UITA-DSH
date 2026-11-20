"""DSH 内置无头执行器（Headless Harness Runner）。

提供两类无头能力：

1. ``run_headless(...)``：面向 CLI（``dsh -p`` / ``dsh run`` / ``uita --mode dsh-run``），
   内置默认 ``Mode.AUTO`` 免审批、四道看门狗（总超时 / 静默 / 死循环 / 产物宽限期）、
   保活心跳、``stream-json | json | text`` 三种输出格式、Token 用量统计、
   退出时自动回收 ``AppLifecycleStack`` 引擎并落盘 ``plan.ir.json``。
2. ``make_perf_ai_runner(...)``：与 ``core/perf/analysis/pipeline.py::run_analysis`` 的
   ``runner`` 注入点同构，使 ``uita --mode perf --perf-ai-analysis`` 可直接由内置 DSH
   无头完成，零外部 Node.js / 外部 CLI 依赖（且保持 R1：``core/`` 不 import ``tools/``）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .context import ToolContext
from .harness import run_goal
from .ir import save_ir
from .planner import (DshPlanner, HttpChatClient, resolve_api_key,
                      resolve_llm_url, resolve_model)
from .policy import Mode
from .registry import all_tools, load_builtin_tools
from .session import DshSession
from .textcase import TextCase, parse_text_case

logger = logging.getLogger("uita.dsh")

OUTPUT_FORMATS = ("text", "json", "stream-json")


@dataclass
class DshHeadlessResult:
    """一次无头 DSH 会话的结构化结果（与 ``AgentRun`` 字段兼容）。"""

    command: list[str] = field(default_factory=list)
    returncode: Optional[int] = None
    elapsed_s: float = 0.0
    timed_out: bool = False
    truncated: bool = False
    session_id: str = ""
    stdout_path: str = ""
    stderr_path: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    result_text: str = ""
    error: str = ""
    agent_error: str = ""
    error_detail: str = ""
    credentials_source: str = "dsh-env"
    attempts: int = 1
    stalled: bool = False
    max_silence_s: float = 0.0
    loop_signature: str = ""
    loop_count: int = 0
    artifact_seen: bool = False
    state: str = ""
    session_dir: str = ""
    ir_path: str = ""
    tcs_path: str = ""
    promoted_ir_path: str = ""
    conclusion: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "returncode": self.returncode,
            "elapsed_s": round(self.elapsed_s, 3),
            "timed_out": self.timed_out,
            "truncated": self.truncated,
            "session_id": self.session_id,
            "stdout_path": self.stdout_path,
            "stderr_path": self.stderr_path,
            "usage": dict(self.usage),
            "result_text": self.result_text,
            "error": self.error,
            "agent_error": self.agent_error,
            "error_detail": self.error_detail,
            "credentials_source": self.credentials_source,
            "attempts": self.attempts,
            "stalled": self.stalled,
            "max_silence_s": round(self.max_silence_s, 3),
            "loop_signature": self.loop_signature,
            "loop_count": self.loop_count,
            "artifact_seen": self.artifact_seen,
            "state": self.state,
            "session_dir": self.session_dir,
            "ir_path": self.ir_path,
            "tcs_path": self.tcs_path,
            "promoted_ir_path": self.promoted_ir_path,
        }

    def summary(self) -> str:
        status = f"state={self.state or 'unknown'} rc={self.returncode}"
        if self.error:
            status += f" error={self.error}"
        tok = self.usage.get("total_tokens", 0)
        return f"{status} elapsed={self.elapsed_s:.1f}s tokens={tok}"


def format_stream_event(session_id: str, event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """把 DSH 内部事件映射为无头 ``stream-json`` 标准事件对象。"""
    kind = event.get("kind")
    if kind == "plan":
        return {
            "type": "assistant",
            "session_id": session_id,
            "seq": event.get("seq"),
            "thought": event.get("thought", ""),
            "tool": event.get("tool", ""),
            "args": event.get("args", {}),
            "expect": event.get("expect", ""),
        }
    if kind == "tool_result":
        return {
            "type": "tool_result",
            "session_id": session_id,
            "seq": event.get("seq"),
            "tool": event.get("tool", ""),
            "ok": event.get("ok", False),
            "error": event.get("error", ""),
            "data": event.get("data", {}),
            "took_ms": event.get("took_ms", 0),
        }
    if kind == "case_step":
        return {
            "type": "case_step",
            "session_id": session_id,
            "no": event.get("no"),
            "status": event.get("status"),
            "action": event.get("action"),
            "detail": event.get("detail", ""),
        }
    if kind == "gate_denied":
        return {
            "type": "gate_denied",
            "session_id": session_id,
            "tool": event.get("tool", ""),
            "reason": event.get("reason", ""),
        }
    return None


def build_result_envelope(session: DshSession,
                          conclusion: dict[str, Any],
                          elapsed_s: float) -> dict[str, Any]:
    """构造无头输出末尾的 ``type="result"`` 标准对象。"""
    state = str(conclusion.get("state") or session.state.value)
    summary = str(conclusion.get("summary") or "")
    is_error = state != "passed"
    return {
        "type": "result",
        "subtype": "success" if not is_error else state,
        "is_error": is_error,
        "state": state,
        "result": summary,
        "session_id": session.session_id,
        "steps_used": session.steps_used,
        "assertions": dict(session.assertions),
        "usage": dict(session.usage),
        "elapsed_s": round(elapsed_s, 3),
        "dir": session.store.session_dir if session.store else "",
    }


def _classify_watchdog_failure(summary: str) -> tuple[str, bool, bool]:
    """从中止摘要中提取 ``(error_code, timed_out, stalled)``。"""
    text = str(summary or "")
    if text.startswith("agent-loop:") or "工具循环空转" in text:
        return "agent-loop", False, False
    if text.startswith("stalled:") or "静默超时" in text:
        return "stalled", False, True
    if text.startswith("no-artifact:") or "产物宽限期超时" in text:
        return "no-artifact", False, False
    if "时长预算耗尽" in text or "timeout" in text.lower():
        return "timeout", True, False
    return "", False, False


def _inject_skill_or_agent_hint(session: DshSession, ctx: ToolContext, *,
                                skill: str = "", agent: str = "") -> None:
    if not skill and not agent:
        return
    from .tools.skill import load_skill_or_agent_bundle

    try:
        bundle = load_skill_or_agent_bundle(ctx, skill=skill, agent=agent)
        header = f"已加载 {bundle['kind']}={bundle['name']}（{bundle['path']}）：\n"
        session.add_hint(header + str(bundle.get("content") or "")[:6000])
    except Exception as exc:  # noqa: BLE001 - 提示注入失败不阻断启动，但留痕
        session.add_hint(f"加载 {skill or agent} 失败: {exc}")


async def run_headless_async(
    *,
    goal: str = "",
    case: Optional[TextCase] = None,
    app_name: str = "",
    workdir: str = "",
    mode: Mode = Mode.AUTO,
    auto_approve: bool = True,
    session_id: str = "",
    root: str = "",
    max_steps: int = 0,
    timeout_s: int = 0,
    stall_s: float = 0.0,
    loop_repeat: int = 0,
    heartbeat_s: float = 0.0,
    artifact_path: str = "",
    artifact_grace_s: float = 0.0,
    model: str = "",
    llm_url: str = "",
    api_key: str = "",
    skill: str = "",
    agent: str = "",
    output_format: str = "text",
    ir_save: str = "",
    tcs_out: str = "",
    promote: bool = False,
    keep_app: bool = False,
    planner: Optional[Any] = None,
    tools: Optional[dict[str, Any]] = None,
    stream_sink: Optional[Callable[[str], None]] = None,
) -> DshHeadlessResult:
    """执行一次完整的无头 DSH 会话并返回结构化结果。"""
    from uita import config

    fmt = output_format if output_format in OUTPUT_FORMATS else "text"
    sink = stream_sink or (lambda line: print(line, flush=True))

    eff_steps = int(max_steps or config.UITA_DSH_MAX_STEPS)
    eff_timeout = int(timeout_s or config.UITA_DSH_TIMEOUT_S)
    eff_loop = int(loop_repeat if loop_repeat is not None else config.UITA_DSH_LOOP_REPEAT)
    eff_stall = float(stall_s if stall_s is not None else config.UITA_DSH_STALL_S)
    eff_hb = float(heartbeat_s if heartbeat_s is not None else config.UITA_DSH_HEARTBEAT_S)

    stream_records: list[dict[str, Any]] = []
    last_emit = [time.monotonic()]

    def _on_event(event: dict[str, Any]) -> None:
        mapped = format_stream_event(session.session_id, event)
        if mapped is not None:
            stream_records.append(mapped)
            if fmt == "stream-json":
                sink(json.dumps(mapped, ensure_ascii=False))
                last_emit[0] = time.monotonic()
            elif fmt == "text":
                kind = mapped.get("type")
                if kind == "assistant":
                    sink(f"[DSH #{session.steps_used + 1}] 计划: {mapped.get('tool')} "
                         f"{json.dumps(mapped.get('args') or {}, ensure_ascii=False)}")
                    last_emit[0] = time.monotonic()
                elif kind == "tool_result":
                    ok_str = "OK" if mapped.get("ok") else f"FAIL({mapped.get('error')})"
                    sink(f"[DSH #{session.steps_used}] 结果: {mapped.get('tool')} -> {ok_str} "
                         f"({mapped.get('took_ms', 0)}ms)")
                    last_emit[0] = time.monotonic()

    session = DshSession(
        goal=goal,
        case=case,
        app_name=app_name,
        mode=mode,
        max_steps=eff_steps,
        timeout_s=eff_timeout,
        approval_timeout_s=float(config.UITA_DSH_APPROVAL_TIMEOUT_S),
        auto_approve=auto_approve,
        session_id=session_id,
        root=root,
        max_loop_repeat=max(0, eff_loop),
        stall_timeout_s=max(0.0, eff_stall),
        artifact_path=artifact_path,
        artifact_grace_s=max(0.0, float(artifact_grace_s or 0.0)),
        on_event=_on_event,
    )
    ctx = ToolContext(session=session, app_name=app_name, workdir=workdir)
    _inject_skill_or_agent_hint(session, ctx, skill=skill, agent=agent)

    if planner is None:
        client = HttpChatClient(url=llm_url, api_key=api_key, model=model)
        planner = DshPlanner(client=client, model=model)

    stop_hb = threading.Event()

    def _heartbeat_worker() -> None:
        if eff_hb <= 0:
            return
        while not stop_hb.wait(timeout=min(eff_hb, 1.0)):
            if session.is_terminal():
                break
            idle = time.monotonic() - last_emit[0]
            if idle >= eff_hb:
                elapsed = time.time() - session.created_at
                hb_payload = {
                    "type": "heartbeat",
                    "session_id": session.session_id,
                    "steps_used": session.steps_used,
                    "max_steps": session.max_steps,
                    "elapsed_s": round(elapsed, 1),
                }
                if fmt == "stream-json":
                    sink(json.dumps(hb_payload, ensure_ascii=False))
                elif fmt == "text":
                    sink(f"[DSH] 运行中（{elapsed:.0f}s，已执行 {session.steps_used}/{session.max_steps} 步）")
                last_emit[0] = time.monotonic()

    hb_thread: Optional[threading.Thread] = None
    if eff_hb > 0:
        hb_thread = threading.Thread(
            target=_heartbeat_worker, name=f"dsh-hb-{session.session_id}", daemon=True,
        )
        hb_thread.start()

    started = time.monotonic()
    ir_saved_path = ""
    tcs_saved_path = ""
    promoted_path = ""
    try:
        conclusion = await run_goal(session, ctx, planner, tools=tools)
        ir_obj = session.to_ir(persist=True)
        if ir_obj is not None and session.store is not None:
            ir_saved_path = os.path.join(session.store.session_dir, "plan.ir.json")
        if ir_save and ir_obj is not None:
            try:
                ir_saved_path = save_ir(ir_obj, ir_save)
            except Exception as exc:  # noqa: BLE001
                logger.warning("dsh: IR 另存失败 %s: %s", ir_save, exc)
        run_passed = str(conclusion.get("state") or session.state.value) == "passed"
        if (tcs_out or promote) and run_passed and ir_obj is not None:
            from .compile import compile_ir_file, promote_ir, resolve_tcs_output_path

            if tcs_out:
                try:
                    resolved_tcs = resolve_tcs_output_path(
                        tcs_out, ir_obj, preferred_name=ir_obj.meta.name,
                    )
                    src_ir = ir_saved_path or os.path.join(session.store.ensure(), "plan.ir.json")
                    if not os.path.isfile(src_ir):
                        save_ir(ir_obj, src_ir)
                    _, tcs_saved_path = compile_ir_file(src_ir, resolved_tcs)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("dsh: TCS 编译落盘失败 %s: %s", tcs_out, exc)
            if promote:
                try:
                    promoted_path = promote_ir(ir_obj)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("dsh: IR 提升入库失败: %s", exc)
    finally:
        stop_hb.set()
        if hb_thread is not None:
            hb_thread.join(timeout=1.0)
        if not keep_app:
            await ctx.cleanup_engine()

    elapsed_s = time.monotonic() - started
    envelope = build_result_envelope(session, conclusion, elapsed_s)
    stream_records.append(envelope)

    if fmt == "stream-json":
        sink(json.dumps(envelope, ensure_ascii=False))
    elif fmt == "json":
        sink(json.dumps(stream_records, ensure_ascii=False, indent=2))
    else:
        sink(f"\n[DSH] 结论: {conclusion.get('state')} — {conclusion.get('summary', '')}")
        if session.case is not None:
            for step in session.case.steps:
                sink(f"  [{step.status}] {step.no}. {step.action}"
                     + (f" — {step.detail}" if step.detail else ""))
        if session.usage.get("total_tokens"):
            sink(f"[DSH] Token 用量: in={session.usage['input_tokens']} "
                 f"out={session.usage['output_tokens']} total={session.usage['total_tokens']}")
        if tcs_saved_path:
            sink(f"[DSH] TCS 已自动编译落盘: {tcs_saved_path}")
        elif tcs_out and str(conclusion.get('state')) != "passed":
            sink(f"[DSH] 会话未通过（{conclusion.get('state')}），跳过 TCS 编译导出")
        if promoted_path:
            sink(f"[DSH] IR 已提升入库: {promoted_path}")
        sink(f"[DSH] 证据目录: {session.store.session_dir if session.store else '-'}")

    state = str(conclusion.get("state") or session.state.value)
    summary = str(conclusion.get("summary") or "")
    err_code, timed_out, stalled = _classify_watchdog_failure(summary)
    if state == "error" and not err_code:
        err_code = "agent-error"

    return DshHeadlessResult(
        command=["dsh", "-p", goal[:80]],
        returncode=0 if state == "passed" else 1,
        elapsed_s=elapsed_s,
        timed_out=timed_out,
        session_id=session.session_id,
        usage=dict(session.usage),
        result_text=summary,
        error=err_code,
        agent_error="agent-error(dsh)" if state == "error" else "",
        error_detail=summary[:400] if state != "passed" else "",
        stalled=stalled,
        loop_signature=session._loop_signature if err_code == "agent-loop" else "",
        loop_count=session._loop_count if err_code == "agent-loop" else 0,
        artifact_seen=session.artifact_ready() if artifact_path else False,
        state=state,
        session_dir=session.store.session_dir if session.store else "",
        ir_path=ir_saved_path,
        tcs_path=tcs_saved_path,
        promoted_ir_path=promoted_path,
        conclusion=dict(conclusion),
    )


def run_headless(**kwargs: Any) -> DshHeadlessResult:
    """同步包装：在独立事件循环中执行一次无头会话。"""
    return asyncio.run(run_headless_async(**kwargs))


def is_dsh_configured() -> bool:
    """检查当前环境是否已配置可用的 DSH 模型端点与密钥。"""
    return bool(resolve_llm_url() and resolve_api_key())


def make_perf_ai_runner(*, llm_url: str = "", api_key: str = "",
                        planner: Optional[Any] = None) -> Callable[..., Any]:
    """构造与 ``core/perf/analysis/pipeline.py::run_analysis(runner=...)`` 兼容的无头执行器。"""

    def _runner(
        prompt: str,
        *,
        out_dir: str,
        model: str = "",
        timeout_s: int = 0,
        heartbeat_s: int = 0,
        stall_s: int = 0,
        loop_repeat: int = 0,
        artifact_path: str = "",
        artifact_grace_s: float = 0.0,
        max_turns: int = 0,
        session_id: str = "",
        workdir: str = "",
        on_log: Optional[Callable[[str], None]] = None,
        **_extra: Any,
    ) -> DshHeadlessResult:
        log = on_log or (lambda msg: None)
        os.makedirs(out_dir, exist_ok=True)
        stdout_path = os.path.join(out_dir, f"dsh-{session_id or 'auto'}-out.jsonl")
        stderr_path = os.path.join(out_dir, f"dsh-{session_id or 'auto'}-err.log")
        lines: list[str] = []

        def _sink(line: str) -> None:
            lines.append(line)

        log(f"[perf-ai] 启动内置 DSH 无头分析引擎（session={session_id or 'auto'}）")
        result = run_headless(
            goal=prompt,
            workdir=workdir or os.path.dirname(os.path.abspath(out_dir)),
            mode=Mode.AUTO,
            auto_approve=True,
            session_id=session_id,
            root=out_dir,
            max_steps=int(max_turns or 60),
            timeout_s=int(timeout_s or 1800),
            stall_s=float(stall_s or 600),
            loop_repeat=int(loop_repeat or 6),
            heartbeat_s=float(heartbeat_s or 240),
            artifact_path=artifact_path,
            artifact_grace_s=float(artifact_grace_s or 0.0),
            model=model,
            llm_url=llm_url,
            api_key=api_key,
            output_format="stream-json",
            planner=planner,
            stream_sink=_sink,
        )
        try:
            with open(stdout_path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + ("\n" if lines else ""))
        except OSError:
            pass
        result.stdout_path = stdout_path
        result.stderr_path = stderr_path
        log(f"[perf-ai] 内置 DSH 无头分析结束：{result.summary()}")
        return result

    return _runner


def doctor_check() -> dict[str, Any]:
    """自检 DSH 无头运行环境就绪状态（端点、凭据掩码、模型列表、注册工具数、工作区）。"""
    from uita import config
    from . import settings_store

    load_builtin_tools()
    tools = all_tools()
    url = resolve_llm_url()
    key = resolve_api_key()
    model = resolve_model()
    fallbacks = list(getattr(config, "UITA_DSH_MODEL_FALLBACKS", ()) or ())

    issues: list[str] = []
    if not url:
        issues.append("未配置模型端点（请设置 DSH_API_URL）")
    if not key:
        issues.append("未配置模型密钥（请设置 DSH_API_KEY）")

    return {
        "ok": len(issues) == 0,
        "llm_url": url,
        "api_key_set": bool(key),
        "api_key_mask": settings_store.MASK if key else "",
        "model": model,
        "fallbacks": fallbacks,
        "tools_count": len(tools),
        "tools": sorted(tools),
        "workspace_dir": getattr(config, "WORKSPACE_DIR", ""),
        "report_dir": getattr(config, "UITA_DSH_OUT_ROOT", ""),
        "settings_file": settings_store.settings_path(),
        "issues": issues,
    }
