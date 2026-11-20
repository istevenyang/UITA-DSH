"""DSH 主循环：plan → gate → invoke → observe → verify → budget。

单步语义（每步一条事件，seq 单调递增）：

1. ``plan``：模型给出 ``{thought, tool, args}``；
2. ``gate``：按会话模式与工具名单放行（action/write 在 supervised 下需人工批准）；
3. ``invoke``：执行工具（失败不中断，作为观察结果回给模型）；
4. ``observe``：动作类工具成功后自动补一次快照，模型看得到执行结果；
5. ``verify``：``assert_*`` 三态入账；``false`` 触发一次重定位（heal）；
6. ``budget``：步数/时长耗尽即 ``aborted``，并写清耗尽原因。

终态：模型调 ``task_finish`` → passed / failed；人工中止 → aborted；宿主异常 →
error。任何 ``unknown`` 断言都会阻止结论变成 passed（防假绿）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from .planner import DshPlanner, PlannerError
from .policy import PolicyGate
from .registry import (TIER_ACTION, TIER_WRITE, DshTool, all_tools,
                       load_builtin_tools)
from .session import DshSession, SessionState, SessionStopped
from .textcase import STEP_PASSED, STEP_UNKNOWN

logger = logging.getLogger("uita.dsh")

FINISH_TOOL = "task_finish"
OBSERVE_TOOL = "observe_snapshot"
ASSERT_PREFIX = "assert_"
_MAX_TEXT = 400


def _announce_current_step(session: DshSession) -> None:
    """文本用例模式下，把"当前待执行步骤"作为提示注入下一步上下文。"""
    case = getattr(session, "case", None)
    if case is None:
        return
    current = case.next_pending()
    if current is None:
        return
    hint = f"当前第 {current.no} 步：{current.action}"
    if current.expected:
        hint += f"（预期：{current.expected}）"
    hints = getattr(session, "_hints", None)
    if hints is not None and hint not in hints:
        hints.append(hint)


def _shorten(value: Any, limit: int = 24) -> Any:
    """截断事件里的长文本/长列表，避免 events.jsonl 与页面被撑爆。"""
    if isinstance(value, str):
        return value if len(value) <= _MAX_TEXT else value[:_MAX_TEXT] + "…"
    if isinstance(value, dict):
        return {key: _shorten(item, limit) for key, item in list(value.items())[:limit]}
    if isinstance(value, (list, tuple)):
        items = [_shorten(item, limit) for item in list(value)[:limit]]
        if len(value) > limit:
            items.append(f"…(+{len(value) - limit})")
        return items
    return value


def _finish(session: DshSession, state: SessionState, summary: str,
            **extra: Any) -> dict[str, Any]:
    """收敛到终态并落一次快照（已终态则只补结论）。"""
    if state in {SessionState.ABORTED, SessionState.ERROR, SessionState.FAILED}:
        session.close_open_steps(STEP_UNKNOWN, reason=f"会话收尾（{state.value}）：{summary}")
    conclusion = session.set_conclusion(
        state, summary,
        case_progress=session.case_progress(), **extra,
    )
    if not session.is_terminal():
        try:
            session.transition(state, summary)
        except Exception as exc:  # noqa: BLE001 - 迁移失败只记日志，结论已在事件流里
            logger.warning("dsh: 终态迁移失败 %s: %s", state.value, exc)
    store = getattr(session, "store", None)
    if store is not None:
        try:
            store.write_json("session.json", session.snapshot())
        except Exception as exc:  # noqa: BLE001
            logger.warning("dsh: 会话快照落盘失败: %s", exc)
    return conclusion


async def _auto_observe(ctx: Any, registry: dict[str, DshTool],
                        session: DshSession) -> None:
    tool = registry.get(OBSERVE_TOOL)
    if tool is None:
        return
    result = await tool.invoke(ctx, {})
    session.append_event(
        "observation", ok=result.ok, error=result.error,
        data=_shorten(result.data),
        evidence=[ref.to_dict() for ref in result.evidence],
    )


def _finish_from_plan(session: DshSession, data: dict[str, Any]) -> dict[str, Any]:
    status = str(data.get("status") or "failed")
    summary = str(data.get("summary") or "")

    if status == "passed":
        unknown = session.unknown_count()
        if unknown:
            return _finish(
                session, SessionState.FAILED,
                f"断言存在 {unknown} 条 unknown，不能判过；模型结论：{summary}",
                model_status=status,
            )
        unresolved = session.unresolved_false_assertions()
        if unresolved:
            return _finish(
                session, SessionState.FAILED,
                f"存在 {len(unresolved)} 条未解决的 false，不能判过；模型结论：{summary}",
                model_status=status,
            )
        case = getattr(session, "case", None)
        if case is not None:
            auto_settle = getattr(session, "auto_settle_open_steps", None)
            if callable(auto_settle):
                auto_settle(summary)
            open_steps = session.unresolved_steps()
            if open_steps:
                return _finish(
                    session, SessionState.FAILED,
                    "存在未上报结论的用例步骤："
                    + ", ".join(str(item["no"]) for item in open_steps)
                    + f"；模型结论：{summary}",
                    model_status=status,
                )
            not_passed = [
                {"no": step.no, "status": step.status, "action": step.action}
                for step in case.steps if step.status != STEP_PASSED
            ]
            if not_passed:
                return _finish(
                    session, SessionState.FAILED,
                    f"有 {len(not_passed)} 步非 passed，不能判过；模型结论：{summary}",
                    model_status=status, not_passed=not_passed,
                )
            if int((getattr(session, "assertions", None) or {}).get("true", 0)) < 1:
                return _finish(
                    session, SessionState.FAILED,
                    f"用例未执行任何真实 assert_* == true 断言，不能判过；模型结论：{summary}",
                    model_status=status,
                )
    state = SessionState.PASSED if status == "passed" else SessionState.FAILED
    return _finish(session, state, summary or status, model_status=status)


async def run_goal(session: DshSession, ctx: Any, planner: Optional[Any] = None,
                   *, gate: Optional[PolicyGate] = None,
                   tools: Optional[dict[str, DshTool]] = None,
                   auto_observe: Optional[bool] = None) -> dict[str, Any]:
    """跑完一次会话目标，返回会话结论 dict。"""
    load_builtin_tools()
    registry = dict(tools) if tools is not None else all_tools()
    planner = planner or DshPlanner()
    gate = gate or PolicyGate(session.mode, allowlist=set(registry))
    if auto_observe is None:
        from uita.config import UITA_DSH_OBSERVE_AFTER_ACTION

        auto_observe = UITA_DSH_OBSERVE_AFTER_ACTION
    specs = [tool.schema() for _, tool in sorted(registry.items())]

    if session.state is SessionState.IDLE:
        session.transition(SessionState.PREPARING, "harness start")
    if session.state is SessionState.PREPARING:
        session.transition(SessionState.RUNNING, "loop start")

    if session.case is not None:
        raw = session.case.raw
        blockers = [marker for marker in ("TODO-ASSERT", "TODO-VISUAL", "SKIP-")
                    if marker in raw]
        if blockers:
            return _finish(session, SessionState.FAILED,
                           "用例尚未评审，禁止规划及执行：" + ", ".join(blockers))
        if not session.case.parsed and raw.lstrip().startswith(("{", "[")):
            return _finish(session, SessionState.FAILED,
                           "结构化 JSON 不能作为单步目标执行，请先用 case_import 转换并评审")

    try:
        while not session.is_terminal():
            ok, reason = session.can_continue()
            if not ok:
                return _finish(session, SessionState.ABORTED, reason)

            await session.wait_if_paused()
            _announce_current_step(session)

            try:
                plan = await planner.plan(
                    session=session, observation=ctx.last_observation,
                    tools=specs, tool_names=sorted(registry),
                )
            except PlannerError as exc:
                return _finish(session, SessionState.ERROR, f"规划失败: {exc}")
            except Exception as exc:  # noqa: BLE001 - 宿主/网络异常
                return _finish(session, SessionState.ERROR,
                               f"规划异常: {type(exc).__name__}: {exc}")

            ok, reason = session.can_continue()
            if not ok:
                return _finish(session, SessionState.ABORTED, reason)

            session.append_event("plan", **_shorten(plan.to_dict()))
            tool = registry.get(plan.tool)
            if tool is None:
                session.append_event(
                    "gate_denied", tool=plan.tool,
                    reason=f"未知工具（可用：{sorted(registry)}）",
                )
                session.bump_step()
                continue

            decision = gate.check(tool)
            if not decision.allowed:
                if decision.needs_approval:
                    approval = await session.request_approval(
                        step=session.steps_used + 1, tool=tool.name, args=plan.args,
                    )
                    if not approval.approved:
                        return _finish(
                            session, SessionState.ABORTED,
                            f"人工未批准 {tool.name}: {approval.reason}",
                        )
                else:
                    session.append_event("gate_denied", tool=tool.name,
                                         reason=decision.reason)
                    session.bump_step()
                    continue

            session.bump_step()
            current = session.case.next_pending() if session.case is not None else None
            result = await tool.invoke(ctx, plan.args)
            result_event = session.append_event(
                "tool_result", tool=tool.name, args=_shorten(plan.args),
                step_no=current.no if current is not None else None,
                ok=result.ok, error=_shorten(result.error),
                data=_shorten(result.data), took_ms=result.took_ms,
                evidence=[ref.to_dict() for ref in result.evidence],
            )

            if tool.name == FINISH_TOOL and result.ok:
                return _finish_from_plan(session, result.data)

            if tool.name.startswith(ASSERT_PREFIX):
                verdict = str(result.data.get("verdict") or "unknown") if result.ok else "unknown"
                session.record_verdict(verdict, tool=tool.name, args=plan.args,
                                       step_no=current.no if current is not None else None,
                                       tool_result_seq=result_event["seq"])
                if verdict in {"false", "unknown"} and \
                        session.heal_rounds < session.max_heal_rounds:
                    session.heal_rounds += 1
                    session.add_hint(
                        f"上一步断言 {verdict}：请重新 locate 目标元素后再试，"
                        "若仍无法判定就如实报告 unknown 并结束。"
                    )

            if result.ok and auto_observe and tool.name != OBSERVE_TOOL and \
                    tool.tier in (TIER_ACTION, TIER_WRITE):
                await _auto_observe(ctx, registry, session)

        return dict(session.conclusion)
    except SessionStopped as exc:
        return _finish(session, SessionState.ABORTED, str(exc))
    except asyncio.CancelledError:
        _finish(session, SessionState.ABORTED, "任务被取消")
        raise
    except Exception as exc:  # noqa: BLE001 - 宿主异常必须落成 error 终态
        logger.warning("dsh: harness 异常: %s", exc, exc_info=True)
        return _finish(session, SessionState.ERROR,
                       f"{type(exc).__name__}: {exc}")
