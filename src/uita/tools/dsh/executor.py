"""IR 执行器（DSH v2）：按步骤 IR 执行，**不构造 Planner、不调用大模型**。

- 定位/动作/断言全部复用既有 Dsh Tool（与 ReAct 路径同一套能力面，语义不漂移）；
- 步骤结论写回会话步骤表（复用 ``record_case_step``），报告/门禁/页面无需改动；
- 失败即收敛（fail-fast）：后续步骤依赖前一步状态，继续跑只会制造假绿；
- 计划文档：``docs/plans/20261117200000-dsh-v2-ir-flow-plan.md`` §6。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from .harness import _finish  # 同一包内复用终态收敛逻辑（含快照落盘）
from .policy import PolicyGate
from .registry import all_tools, bound_timeout, get_tool, load_builtin_tools
from .session import DshSession, SessionState, SessionStopped
from .textcase import STEP_FAILED, STEP_PASSED, STEP_UNKNOWN

logger = logging.getLogger("uita.dsh")

_DEFAULT_WAIT_TIMEOUT = 15.0
_POLL_INTERVAL = 0.4


class _StepResult:
    __slots__ = ("status", "detail", "evidence")

    def __init__(self, status: str, detail: str = "",
                 evidence: Optional[list[dict[str, Any]]] = None) -> None:
        self.status = status
        self.detail = detail
        self.evidence = list(evidence or [])


def _tool_args(step: Any) -> dict[str, Any]:
    """把 IR target 翻译成工具入参（元素名优先，坐标必须带 source）。"""
    target = step.target
    if target.kind == "coord":
        return {"x": target.coord[0], "y": target.coord[1],
                "source": target.coord_source}
    return {"target": target.name}


async def _invoke(session: DshSession, ctx: Any, gate: PolicyGate,
                  name: str, args: dict[str, Any]) -> Any:
    """按策略闸调用工具；需审批而无人批准时抛 ``_Denied``。"""
    tool = get_tool(name)
    if tool is None:
        return None
    decision = gate.check(tool)
    if not decision.allowed:
        if not decision.needs_approval:
            return _Denied(f"策略拒绝 {name}: {decision.reason}")
        approval = await session.request_approval(
            step=session.steps_used, tool=name, args=args,
        )
        if not approval.approved:
            return _Denied(f"人工未批准 {name}: {approval.reason}")
    result = await tool.invoke(ctx, args, timeout_s=bound_timeout(tool, session))
    session.append_event(
        "tool_result", tool=name, args=_short(args), ok=result.ok,
        error=_short(result.error), data=_short(result.data), took_ms=result.took_ms,
        evidence=[ref.to_dict() for ref in result.evidence],
    )
    return result


class _Denied:
    """策略拒绝占位（与 ToolResult 区分，避免被当成工具返回）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        self.ok = False
        self.data: dict[str, Any] = {}
        self.error = reason
        self.took_ms = 0.0
        self.evidence: list[Any] = []


def _short(value: Any, limit: int = 24) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit * 4 else value[: limit * 4] + "…"
    if isinstance(value, dict):
        return {k: _short(v, limit) for k, v in list(value.items())[: limit]}
    if isinstance(value, list):
        return [_short(item, limit) for item in value[:limit]]
    return value


async def _ensure_ready(session: DshSession, ctx: Any, gate: PolicyGate,
                        ir: Any) -> Optional[_StepResult]:
    """按 IR 前置条件准备环境：需要时安装 / 注入探针 / 启动应用。"""
    if ctx.prepared:
        return None
    pre = ir.preconditions or {}
    if pre.get("install"):
        result = await _invoke(session, ctx, gate, "env_install", {"app": ir.meta.app})
        if isinstance(result, _Denied) or result is None or not result.ok:
            return _StepResult(STEP_FAILED, f"环境安装失败: {getattr(result, 'error', '工具缺失')}")
    if pre.get("probe") in {"auto", "force"}:
        await _invoke(session, ctx, gate, "env_probe_provision", {"app": ir.meta.app})
    result = await _invoke(session, ctx, gate, "app_prepare",
                           {"app": ir.meta.app, "case_name": ir.meta.name})
    if isinstance(result, _Denied):
        return _StepResult(STEP_FAILED, result.reason)
    if result is None or not result.ok:
        return _StepResult(STEP_FAILED,
                           f"应用启动失败: {getattr(result, 'error', '工具缺失')}")
    return None


async def _wait_for(ctx: Any, gate: PolicyGate, session: DshSession,
                    step: Any) -> _StepResult:
    """``wait`` 步骤：轮询 locate 直到命中或超时（不截图，零视觉）。"""
    timeout = float(step.args.get("timeout") or _DEFAULT_WAIT_TIMEOUT)
    deadline = time.monotonic() + max(0.0, timeout)
    last = ""
    while time.monotonic() < deadline:
        if session.stop_requested():
            return _StepResult(STEP_FAILED, "会话被中止")
        expired = session.clock_expired_reason()
        if expired:
            return _StepResult(STEP_FAILED, expired)
        result = await _invoke(session, ctx, gate, "locate", _tool_args(step))
        if isinstance(result, _Denied):
            return _StepResult(STEP_FAILED, result.reason)
        if result is not None and result.ok:
            data = result.data or {}
            return _StepResult(
                STEP_PASSED,
                f"等待命中（level={data.get('level')}）",
                [ref.to_dict() for ref in result.evidence],
            )
        last = getattr(result, "error", "") or last
        if session.stop_requested():
            return _StepResult(STEP_FAILED, "会话被中止")
        expired = session.clock_expired_reason()
        if expired:
            return _StepResult(STEP_FAILED, expired)
        await asyncio.sleep(min(_POLL_INTERVAL, max(0.0, deadline - time.monotonic())))
    return _StepResult(STEP_FAILED, f"等待超时 {timeout}s：{last or '未命中'}")


_ASSERT_TOOLS = {
    "exist": "assert_exist",
    "absent": "assert_absent",
    "text": "assert_text",
    "value": "assert_value",
    "js": "assert_js",
}


async def _run_expects(session: DshSession, ctx: Any, gate: PolicyGate,
                       step: Any) -> _StepResult:
    """执行该步的全部断言；false 优先、其次 unknown、全 true 才 passed。"""
    verdicts: list[str] = []
    details: list[str] = []
    evidence: list[dict[str, Any]] = []
    for exp in step.expect:
        tool_name = _ASSERT_TOOLS.get(exp.kind)
        if tool_name is None:
            verdicts.append(STEP_UNKNOWN)
            details.append(f"{exp.kind}: 未知断言类型")
            continue
        args: dict[str, Any] = {"target": exp.element}
        if exp.kind in {"text", "js"}:
            args["expected"] = exp.value
        elif exp.kind == "value":
            args["attr"] = exp.attr or "value"
            args["expected"] = exp.value
        result = await _invoke(session, ctx, gate, tool_name, args)
        if isinstance(result, _Denied):
            verdicts.append(STEP_UNKNOWN)
            details.append(f"{exp.kind}: {result.reason}")
            continue
        data = getattr(result, "data", {}) or {}
        verdict = str(data.get("verdict") or ("unknown" if result is None or not result.ok else "true"))
        verdicts.append(verdict)
        session.record_verdict(verdict)
        evidence.extend(ref.to_dict() for ref in getattr(result, "evidence", []))
        details.append(f"{exp.kind}:{exp.element}={verdict}")

    if "false" in verdicts:
        return _StepResult(STEP_FAILED, "断言 false → " + "; ".join(details), evidence)
    if "unknown" in verdicts:
        return _StepResult(STEP_UNKNOWN, "断言 unknown → " + "; ".join(details), evidence)
    return _StepResult(STEP_PASSED, "; ".join(details) or "断言通过", evidence)


_ACTION_TOOLS = {
    "click": "act_click",
    "type": "act_type",
    "press_key": "act_press_key",
    "scroll": "act_scroll",
    "hover": "act_hover",
}


async def _run_step(session: DshSession, ctx: Any, gate: PolicyGate,
                    ir: Any, step: Any) -> _StepResult:
    """执行单个 IR 步骤（动作 → 断言）。"""
    action = step.action
    evidence: list[dict[str, Any]] = []

    if action == "wait":
        return await _wait_for(ctx, gate, session, step)

    if action == "assert":
        return await _run_expects(session, ctx, gate, step)

    if action == "open":
        name = step.target.name
        if ctx.prepared:
            result = await _invoke(session, ctx, gate, "app_switch",
                                   {"app": name or ir.meta.app})
        else:
            result = await _invoke(session, ctx, gate, "app_prepare",
                                   {"app": ir.meta.app, "case_name": ir.meta.name})
        if isinstance(result, _Denied):
            return _StepResult(STEP_FAILED, result.reason)
        if result is None or not result.ok:
            return _StepResult(STEP_FAILED,
                               f"打开应用失败: {getattr(result, 'error', '工具缺失')}")
        evidence = [ref.to_dict() for ref in getattr(result, "evidence", [])]
    else:
        tool_name = _ACTION_TOOLS.get(action)
        if tool_name is None:
            return _StepResult(STEP_FAILED, f"不支持的 action: {action!r}")
        args = _tool_args(step)
        if action == "type":
            args["text"] = str(step.args.get("text") or "")
            if "submit" in step.args:
                args["submit"] = bool(step.args["submit"])
        elif action == "press_key":
            args = {"key": str(step.args.get("key") or step.target.name or "")}
        elif action == "scroll":
            args = {"direction": str(step.args.get("direction") or "down")}
            if step.args.get("amount"):
                args["amount"] = int(step.args["amount"])
            if step.target.name:
                args["target"] = step.target.name
        result = await _invoke(session, ctx, gate, tool_name, args)
        if isinstance(result, _Denied):
            return _StepResult(STEP_FAILED, result.reason)
        if result is None or not result.ok:
            return _StepResult(STEP_FAILED,
                               f"{action} 失败: {getattr(result, 'error', '工具缺失')}")
        evidence = [ref.to_dict() for ref in getattr(result, "evidence", [])]

    if step.expect:
        expect_result = await _run_expects(session, ctx, gate, step)
        evidence.extend(expect_result.evidence)
        if expect_result.status != STEP_PASSED:
            return _StepResult(expect_result.status, expect_result.detail, evidence)
        return _StepResult(STEP_PASSED, f"{action} 完成；{expect_result.detail}", evidence)
    return _StepResult(STEP_PASSED, f"{action} 完成", evidence)


async def run_ir(session: DshSession, ctx: Any, ir: Any, *,
                 tools: Optional[dict[str, Any]] = None,
                 gate: Optional[PolicyGate] = None,
                 fail_fast: bool = True) -> dict[str, Any]:
    """按 IR 执行整份用例，返回会话结论（与 ReAct 路径同构）。"""
    load_builtin_tools()
    registry = dict(tools) if tools is not None else all_tools()
    gate = gate or PolicyGate(session.mode, allowlist=set(registry))

    session.ir = ir
    if session.case is None:
        session.case = ir.to_text_case()
    if session.state is SessionState.IDLE:
        session.transition(SessionState.PREPARING, "ir replay start")
    if session.state is SessionState.PREPARING:
        session.transition(SessionState.RUNNING, "ir replay loop")

    session.append_event(
        "ir_loaded", name=ir.meta.name, app=ir.meta.app, os=ir.meta.os,
        revision=ir.meta.revision, steps=len(ir.steps),
    )

    try:
        ready = await _ensure_ready(session, ctx, gate, ir)
        if ready is not None:
            return _finish(session, SessionState.FAILED, ready.detail)

        for step in ir.steps:
            ok, reason = session.can_continue()
            if not ok:
                return _finish(session, SessionState.ABORTED, reason)
            await session.wait_if_paused()
            session.bump_step()
            session.append_event("ir_step", no=step.no, action=step.action,
                                 target=step.target.name or step.target.anchor_id)

            result = await _run_step(session, ctx, gate, ir, step)
            session.record_case_step(
                step.no, result.status,
                detail=result.detail or step.note,
                evidence=result.evidence,
            )
            if result.status == STEP_PASSED:
                continue

            if result.status == STEP_FAILED and \
                    session.heal_rounds < session.max_heal_rounds and step.expect:
                session.heal_rounds += 1
                session.append_event("ir_heal", no=step.no,
                                     reason=result.detail[:200])
                retry = await _run_expects(session, ctx, gate, step)
                session.record_case_step(
                    step.no, retry.status,
                    detail=f"heal 重试：{retry.detail}", evidence=retry.evidence,
                )
                if retry.status == STEP_PASSED:
                    continue
                result = retry

            if fail_fast:
                return _finish(
                    session, SessionState.FAILED,
                    f"第 {step.no} 步 {result.status}：{result.detail}",
                    failed_step=step.no,
                )

        unresolved = session.unresolved_steps()
        if unresolved:
            return _finish(
                session, SessionState.FAILED,
                "存在未上报结论的步骤：" + ", ".join(str(item["no"]) for item in unresolved),
            )
        if session.unknown_count():
            return _finish(
                session, SessionState.FAILED,
                f"断言存在 {session.unknown_count()} 条 unknown，不能判过",
            )
        if not session.case.all_passed():
            return _finish(session, SessionState.FAILED, "存在非 passed 步骤")
        return _finish(session, SessionState.PASSED,
                       f"IR 执行通过（{len(ir.steps)} 步，revision {ir.meta.revision}）")
    except SessionStopped as exc:
        return _finish(session, SessionState.ABORTED, str(exc))
    except asyncio.CancelledError:
        _finish(session, SessionState.ABORTED, "任务被取消")
        raise
    except Exception as exc:  # noqa: BLE001 - 宿主异常必须落成 error 终态
        logger.warning("dsh: IR 执行异常: %s", exc, exc_info=True)
        return _finish(session, SessionState.ERROR, f"{type(exc).__name__}: {exc}")
