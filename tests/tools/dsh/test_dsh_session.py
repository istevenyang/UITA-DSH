"""DSH 会话：状态机 / 事件流 / 预算 / 审批闸 / 人工插话。"""
from __future__ import annotations

import asyncio

import pytest

from uita.tools.dsh.policy import Mode
from uita.tools.dsh.session import (DshSession, InvalidTransition, SessionState,
                                    SessionStopped)


def test_session_created_event_and_ids(make_session):
    session = make_session()
    assert session.state is SessionState.IDLE
    assert session.session_id.startswith("dsh-")
    assert session.events[0]["kind"] == "session_created"
    assert session.events[0]["seq"] == 1


def test_events_seq_monotonic_and_incremental(make_session):
    session = make_session()
    session.append_event("a", value=1)
    session.append_event("b", value=2)
    assert [e["seq"] for e in session.events] == [1, 2, 3]
    assert [e["kind"] for e in session.events_since(1)] == ["a", "b"]
    assert session.events_since(3) == []


def test_invalid_transition_raises(make_session):
    session = make_session()
    with pytest.raises(InvalidTransition):
        session.transition(SessionState.PASSED)


def test_valid_transition_chain(make_session):
    session = make_session()
    session.transition(SessionState.PREPARING)
    session.transition(SessionState.RUNNING)
    session.transition(SessionState.PASSED, "done")
    assert session.is_terminal()
    assert session.events[-1]["kind"] == "state"


def test_pause_and_resume_only_from_running(make_session):
    session = make_session()
    assert session.pause() is False
    session.transition(SessionState.PREPARING)
    session.transition(SessionState.RUNNING)
    assert session.pause() is True
    assert session.state is SessionState.PAUSED
    assert session.resume() is True
    assert session.state is SessionState.RUNNING


def test_budget_steps_exhausted(make_session):
    session = make_session(max_steps=2)
    ok, reason = session.can_continue()
    assert ok
    session.bump_step(2)
    ok, reason = session.can_continue()
    assert not ok and "步数预算" in reason
    assert session.budget_left()["steps_left"] == 0


def test_budget_timeout_exhausted(make_session):
    clock = {"now": 1000.0}
    session = make_session(timeout_s=10, now=lambda: clock["now"])
    clock["now"] += 11
    ok, reason = session.can_continue()
    assert not ok and "时长预算" in reason


def test_record_verdict_counts_and_unknown_fallback(make_session):
    session = make_session()
    for verdict in ("true", "false", "unknown", "bogus"):
        session.record_verdict(verdict)
    assert session.assertions == {"true": 1, "false": 1, "unknown": 2}
    assert session.unknown_count() == 2


def test_hints_enqueue_and_drain(make_session):
    session = make_session()
    session.add_hint("   ")
    assert session.drain_hints() == []
    session.add_hint("换个按钮")
    session.add_hint("先关弹窗")
    assert session.drain_hints() == ["换个按钮", "先关弹窗"]
    assert session.drain_hints() == []


async def test_request_approval_resolved_true(make_session):
    session = make_session(approval_timeout_s=2)
    task = asyncio.ensure_future(
        session.request_approval(step=1, tool="act_click", args={"target": "设置"})
    )
    for _ in range(50):
        await asyncio.sleep(0.01)
        if session.pending_approval:
            break
    assert session.pending_approval["tool"] == "act_click"
    assert session.resolve_approval(True, "看起来没问题") is True
    decision = await task
    assert decision.approved is True
    assert session.pending_approval is None


async def test_request_approval_timeout(make_session):
    session = make_session(approval_timeout_s=0.01)
    decision = await session.request_approval(step=1, tool="act_click", args={})
    assert decision.approved is False
    assert "审批超时" in decision.reason
    assert any(e["kind"] == "approval_timeout" for e in session.events)


async def test_request_approval_auto_approve(make_session):
    session = make_session(auto_approve=True)
    decision = await session.request_approval(step=1, tool="env_install", args={})
    assert decision.approved is True
    assert session.pending_approval is None


def test_resolve_approval_without_pending_returns_false(make_session):
    session = make_session()
    assert session.resolve_approval(True) is False


def test_record_usage_ignores_garbage(make_session):
    session = make_session()
    session.record_usage({
        "prompt_tokens": "nope",
        "completion_tokens": None,
        "prompt_tokens_details": "not-a-dict",
        "total_tokens": "12",
    })
    session.record_usage({"input_tokens": -4, "output_tokens": 3})
    assert session.usage["input_tokens"] == 0
    assert session.usage["output_tokens"] == 3
    assert session.usage["cached_tokens"] == 0
    assert session.usage["total_tokens"] == 15


def test_ensure_terminal_from_idle(make_session):
    session = make_session()
    conclusion = session.ensure_terminal(SessionState.ERROR, "planner 构造失败")
    assert session.is_terminal()
    assert session.state is SessionState.ERROR
    assert conclusion["state"] == "error"
    assert "planner" in conclusion["summary"]
    again = session.ensure_terminal(SessionState.FAILED, "不要覆盖")
    assert again["summary"] == conclusion["summary"]


async def test_wait_if_paused_honors_clock(make_session):
    clock = {"now": 1000.0}
    session = make_session(timeout_s=10, now=lambda: clock["now"])
    session.transition(SessionState.PREPARING)
    session.transition(SessionState.RUNNING)
    session.pause()
    clock["now"] += 11
    with pytest.raises(SessionStopped, match="时长预算"):
        await session.wait_if_paused(interval=0.001)


async def test_resolve_approval_from_other_thread(make_session):
    import threading

    session = make_session(approval_timeout_s=2)
    task = asyncio.ensure_future(
        session.request_approval(step=1, tool="act_click", args={"target": "设置"})
    )
    for _ in range(50):
        await asyncio.sleep(0.01)
        if session.pending_approval:
            break
    holder: dict = {}

    def _resolve() -> None:
        holder["ok"] = session.resolve_approval(True, "跨线程批准")

    thread = threading.Thread(target=_resolve)
    thread.start()
    thread.join(timeout=2)
    decision = await task
    assert holder["ok"] is True
    assert decision.approved is True
    assert decision.reason == "跨线程批准"


async def test_wait_if_paused_raises_on_stop(make_session):
    session = make_session()
    session.transition(SessionState.PREPARING)
    session.transition(SessionState.RUNNING)
    session.pause()
    session.request_stop()
    with pytest.raises(SessionStopped):
        await session.wait_if_paused(interval=0.001)


def test_stop_requested_sets_flag(make_session):
    session = make_session()
    session.request_stop()
    assert session.stop_requested()
    ok, reason = session.can_continue()
    assert not ok and "人工中止" in reason


def test_conclusion_and_snapshot(make_session):
    session = make_session(mode=Mode.AUTO, max_steps=7)
    session.set_conclusion(SessionState.FAILED, "卡在登录")
    snapshot = session.snapshot()
    assert snapshot["conclusion"]["state"] == "failed"
    assert snapshot["conclusion"]["summary"] == "卡在登录"
    assert snapshot["mode"] == "auto"
    assert snapshot["max_steps"] == 7
    assert snapshot["pending_approval"] is None
    assert snapshot["dir"].endswith(session.session_id)


def test_snapshot_written_on_disk(make_session):
    session = make_session()
    session.store.write_json("session.json", session.snapshot())
    events = session.store.read_events()
    assert events and events[0]["kind"] == "session_created"


# ─────────────────────────────────────────────
# 文本用例步骤
# ─────────────────────────────────────────────

def _case():
    from uita.tools.dsh.textcase import parse_text_case

    return parse_text_case("1. 打开首页\n2. 点击设置\n3. 断言已开启")


def test_session_with_case_uses_name_as_goal(make_session):
    from uita.tools.dsh.textcase import parse_text_case

    case = parse_text_case("用例名称：隐私开关\n1. 打开首页")
    session = make_session(goal="", case=case)
    assert session.goal == "隐私开关"
    assert session.snapshot()["case"]["name"] == "隐私开关"
    assert session.events[0]["case"]["steps"][0]["action"] == "打开首页"


def test_record_case_step_and_progress(make_session):
    session = make_session(goal="", case=_case())
    session.record_case_step(1, "running")
    session.record_case_step(1, "passed", "看到首页")
    event = session.record_case_step(2, "unknown", "设置入口不可见")
    assert event["kind"] == "case_step" and event["no"] == 2
    progress = session.case_progress()
    assert progress == {"pending": 1, "running": 0, "passed": 1, "failed": 0,
                        "unknown": 1, "skipped": 0, "total": 3}
    assert session.unresolved_steps() == [
        {"no": 3, "action": "断言已开启", "status": "pending"}]


def test_record_case_step_rejects_bad_input(make_session):
    session = make_session(goal="", case=_case())
    with pytest.raises(ValueError):
        session.record_case_step(9, "passed")
    with pytest.raises(ValueError):
        session.record_case_step(1, "bogus")
    session.record_case_step(1, "passed")
    with pytest.raises(ValueError):
        session.record_case_step(1, "failed")


def test_record_case_step_without_case_raises(make_session):
    session = make_session()
    with pytest.raises(ValueError):
        session.record_case_step(1, "passed")
    assert session.case_progress() is None
    assert session.unresolved_steps() == []
    assert session.close_open_steps() == []


def test_close_open_steps_marks_unknown(make_session):
    session = make_session(goal="", case=_case())
    session.record_case_step(1, "passed", "ok")
    changed = session.close_open_steps("unknown", reason="预算耗尽")
    assert changed == [2, 3]
    assert session.case.step(2).status == "unknown"
    assert session.case.step(2).detail == "预算耗尽"
    assert session.close_open_steps("unknown") == []


def _step_assertion(session, verdict, *, args=None, step_no=1,
                    tool="assert_value", ok=True):
    args = dict(args or {"target": "开关", "attr": "value", "expected": "on"})
    source = session.append_event(
        "tool_result", tool=tool, args=args, step_no=step_no,
        ok=ok, data={"verdict": verdict}, evidence=[],
    )
    session.record_verdict(verdict, tool=tool, args=args,
                           step_no=step_no, tool_result_seq=source["seq"])
    return source


@pytest.mark.parametrize("next_args,remaining", [
    ({"target": "开关", "attr": "value", "expected": "on"}, 0),
    ({"target": "其它", "attr": "value", "expected": "on"}, 1),
    ({"target": "开关", "attr": "enabled", "expected": "on"}, 1),
    ({"target": "开关", "attr": "value", "expected": "off"}, 1),
])
def test_false_requires_same_step_target_property_and_expected(
        make_session, next_args, remaining):
    session = make_session(case=_case())
    _step_assertion(session, "false")
    _step_assertion(session, "true", args=next_args)
    assert len(session.unresolved_false_assertions()) == remaining
    if remaining:
        with pytest.raises(ValueError, match="false"):
            session.step_assertion_evidence(1)
    else:
        assert session.step_assertion_evidence(1)


def test_invalid_source_cannot_clear_previous_false(make_session):
    session = make_session(case=_case())
    _step_assertion(session, "false")
    _step_assertion(session, "true", ok=False)
    assert len(session.unresolved_false_assertions()) == 1


@pytest.mark.parametrize("tool,ok", [("assert_value", False), ("act_click", True)])
def test_only_successful_assert_tool_result_authorizes_step(make_session, tool, ok):
    session = make_session(case=_case())
    _step_assertion(session, "true", tool=tool, ok=ok)
    with pytest.raises(ValueError):
        session.step_assertion_evidence(1)
    assert session.case.step(1).status == "pending"


def test_plain_true_counter_is_not_assertion_evidence(make_session):
    session = make_session(case=_case())
    session.record_verdict("true", step_no=1)
    with pytest.raises(ValueError):
        session.step_assertion_evidence(1)


def test_unknown_history_is_not_erased_by_true_retry(make_session):
    session = make_session(case=_case())
    _step_assertion(session, "unknown")
    _step_assertion(session, "true")
    with pytest.raises(ValueError, match="unknown"):
        session.step_assertion_evidence(1)
    assert session.unknown_count() == 1


def test_assertion_source_cannot_be_consumed_by_another_step(make_session):
    session = make_session(case=_case())
    source = _step_assertion(session, "true")
    evidence = session.step_assertion_evidence(1)
    session.record_case_step(1, "passed", evidence=evidence)
    session.record_verdict("true", tool=source["tool"], args=source["args"],
                           step_no=2, tool_result_seq=source["seq"])
    with pytest.raises(ValueError):
        session.step_assertion_evidence(2)
    assert session.case.step(2).status == "pending"
