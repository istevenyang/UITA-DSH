"""DSH 主循环：六阶段、审批闸、heal、预算、unknown 防假绿。"""
from __future__ import annotations

import pytest

from dsh_helpers import FakePlanner
from uita.tools.dsh.harness import run_goal
from uita.tools.dsh.policy import Mode
from uita.tools.dsh.registry import (TIER_ACTION, TIER_READ, DshTool,
                                     ToolResult)
from uita.tools.dsh.session import ApprovalDecision, SessionState


class _Observe(DshTool):
    name = "observe_snapshot"
    tier = TIER_READ
    params = {}

    def __init__(self) -> None:
        self.count = 0

    async def run(self, ctx) -> ToolResult:
        self.count += 1
        data = {"window_title": f"win-{self.count}"}
        ctx.set_observation(data)
        return ToolResult(ok=True, data=data)


class _Act(DshTool):
    name = "act_click"
    tier = TIER_ACTION
    params = {"target": {"type": "string"}}
    required = ("target",)

    def __init__(self) -> None:
        self.targets: list[str] = []

    async def run(self, ctx, target: str) -> ToolResult:
        self.targets.append(target)
        return ToolResult(ok=True, data={"clicked": target})


class _Assert(DshTool):
    name = "assert_probe"
    tier = TIER_READ
    params = {"verdict": {"type": "string"}}

    async def run(self, ctx, verdict: str = "true") -> ToolResult:
        return ToolResult(ok=True, data={"verdict": verdict})


class _Finish(DshTool):
    name = "task_finish"
    tier = TIER_READ
    params = {"status": {"type": "string"}, "summary": {"type": "string"}}

    async def run(self, ctx, status: str, summary: str) -> ToolResult:
        return ToolResult(ok=True, data={"status": status, "summary": summary})


class _Boom(DshTool):
    name = "boom"
    tier = TIER_READ
    params = {}

    async def run(self, ctx) -> ToolResult:
        raise RuntimeError("宿主炸了")


def _registry(monkeypatch):
    tools = {}
    for cls in (_Observe, _Act, _Assert, _Finish, _Boom):
        instance = cls()
        tools[instance.name] = instance
    monkeypatch.setattr("uita.tools.dsh.harness.load_builtin_tools", lambda: None)
    return tools


def _finish_plan(status: str = "passed", summary: str = "完成") -> dict:
    return {"tool": "task_finish", "args": {"status": status, "summary": summary}}


async def test_happy_path_passed(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.AUTO)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "observe_snapshot", "args": {}},
        {"tool": "assert_probe", "args": {"verdict": "true"}},
        _finish_plan(),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == "passed"
    assert session.state is SessionState.PASSED
    kinds = [event["kind"] for event in session.events]
    assert kinds.count("plan") == 3
    assert kinds.count("tool_result") == 3
    assert "conclusion" in kinds
    assert session.assertions["true"] == 1


async def test_assert_false_triggers_single_heal(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.AUTO, max_heal_rounds=1)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "assert_probe", "args": {"verdict": "false"}},
        _finish_plan("passed", "我认为过了"),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert session.heal_rounds == 1
    assert any(event["kind"] == "hint" for event in session.events)
    assert conclusion["state"] == "failed"


async def test_unknown_assertion_blocks_passed(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.AUTO)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "assert_probe", "args": {"verdict": "unknown"}},
        _finish_plan("passed", "应该过"),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == "failed"
    assert conclusion["unknown_count"] == 1
    assert "unknown" in conclusion["summary"]


async def test_false_assertion_cannot_be_overridden_by_model(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.AUTO)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "assert_probe", "args": {"verdict": "false"}},
        _finish_plan("passed", "模型声称通过"),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == "failed"


async def test_report_passed_requires_real_step_assertion(make_session, monkeypatch):
    from uita.tools.dsh.textcase import parse_text_case
    from uita.tools.dsh.tools.meta import ReportStepTool

    tools = _registry(monkeypatch)
    tools["report_step"] = ReportStepTool()
    session = make_session(mode=Mode.AUTO, case=parse_text_case("1. 检查首页"))
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "report_step", "args": {"step": 1, "status": "passed"}},
        _finish_plan(),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == "failed"
    assert session.case.step(1).status == "unknown"


async def test_unreviewed_case_never_invokes_actions(make_session, monkeypatch):
    from uita.tools.dsh.textcase import parse_text_case

    tools = _registry(monkeypatch)
    case = parse_text_case("<!-- SKIP-L4 -->\n用例名称：卸载\n1. 卸载应用")
    session = make_session(mode=Mode.AUTO, case=case)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "act_click", "args": {"target": "卸载"}},
        _finish_plan(),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == "failed"
    assert tools["act_click"].targets == []
    assert not any(event["kind"] == "plan" for event in session.events)


async def test_finish_failed_maps_to_failed(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.AUTO)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([_finish_plan("failed", "按钮找不到")])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == "failed"
    assert conclusion["model_status"] == "failed"


async def test_max_steps_exhausted_aborts(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.AUTO, max_steps=1)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "observe_snapshot", "args": {}},
        _finish_plan(),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == "aborted"
    assert "步数预算" in conclusion["summary"]


async def test_readonly_denies_action_then_finishes(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.READONLY)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "act_click", "args": {"target": "设置"}},
        _finish_plan("failed", "只读模式不能点"),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert tools["act_click"].targets == []
    assert any(event["kind"] == "gate_denied" for event in session.events)
    assert conclusion["state"] == "failed"


async def test_supervised_rejection_aborts(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.SUPERVISED)

    async def _reject(*, step, tool, args):
        return ApprovalDecision(False, "人工拒绝")

    session.request_approval = _reject  # type: ignore[assignment]
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([{"tool": "act_click", "args": {"target": "设置"}}])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == "aborted"
    assert "人工未批准" in conclusion["summary"]
    assert tools["act_click"].targets == []


async def test_supervised_approval_executes_action(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.SUPERVISED)

    async def _approve(*, step, tool, args):
        return ApprovalDecision(True, "ok")

    session.request_approval = _approve  # type: ignore[assignment]
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "act_click", "args": {"target": "设置"}},
        _finish_plan(),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert tools["act_click"].targets == ["设置"]
    assert conclusion["state"] == "passed"


async def test_unknown_tool_is_denied_and_loop_continues(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.AUTO)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "hack_the_planet", "args": {}},
        _finish_plan(),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    denied = [e for e in session.events if e["kind"] == "gate_denied"]
    assert denied and "未知工具" in denied[0]["reason"]
    assert conclusion["state"] == "passed"


async def test_tool_exception_recorded_and_loop_continues(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.AUTO)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([{"tool": "boom", "args": {}}, _finish_plan()])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    failures = [e for e in session.events
                if e["kind"] == "tool_result" and not e["ok"]]
    assert failures and "RuntimeError" in failures[0]["error"]
    assert conclusion["state"] == "passed"


async def test_auto_observe_after_action(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.AUTO)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "act_click", "args": {"target": "设置"}},
        _finish_plan(),
    ])
    await run_goal(session, ctx, planner, tools=tools, auto_observe=True)
    observations = [e for e in session.events if e["kind"] == "observation"]
    assert observations and tools["observe_snapshot"].count >= 1


async def test_planner_error_ends_with_error(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.AUTO)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    conclusion = await run_goal(session, ctx, FakePlanner([]), tools=tools,
                                auto_observe=False)
    assert conclusion["state"] == "error"
    assert "规划失败" in conclusion["summary"]


async def test_stop_requested_before_loop(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.AUTO)
    session.request_stop()
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    conclusion = await run_goal(session, ctx, FakePlanner([_finish_plan()]),
                                tools=tools, auto_observe=False)
    assert conclusion["state"] == "aborted"


async def test_session_snapshot_persisted(make_session, monkeypatch):
    tools = _registry(monkeypatch)
    session = make_session(mode=Mode.AUTO)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    await run_goal(session, ctx, FakePlanner([_finish_plan()]), tools=tools,
                   auto_observe=False)
    import os

    assert os.path.isfile(os.path.join(session.store.session_dir, "session.json"))


# ─────────────────────────────────────────────
# 文本用例门禁
# ─────────────────────────────────────────────

def _case():
    from uita.tools.dsh.textcase import parse_text_case

    return parse_text_case("1. 打开首页\n2. 点击设置")


def _case_tools(monkeypatch):
    from uita.tools.dsh.tools.meta import ReportStepTool

    tools = _registry(monkeypatch)
    report = ReportStepTool()
    tools[report.name] = report
    return tools


async def test_case_passed_requires_all_steps_reported(make_session, monkeypatch):
    tools = _case_tools(monkeypatch)
    session = make_session(goal="", case=_case(), mode=Mode.AUTO)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "assert_probe", "args": {"verdict": "true"}},
        {"tool": "report_step", "args": {"step": 1, "status": "passed", "detail": "ok"}},
        _finish_plan("passed", "我看完了"),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == "failed"
    assert "未上报结论" in conclusion["summary"]
    assert session.case.step(2).status == "unknown"


async def test_case_all_steps_passed_yields_passed(make_session, monkeypatch):
    tools = _case_tools(monkeypatch)
    session = make_session(goal="", case=_case(), mode=Mode.AUTO)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "assert_probe", "args": {"verdict": "true"}},
        {"tool": "report_step", "args": {"step": 1, "status": "passed", "detail": "首页可见"}},
        {"tool": "assert_probe", "args": {"verdict": "true"}},
        {"tool": "report_step", "args": {"step": 2, "status": "passed", "detail": "设置页可见"}},
        _finish_plan("passed", "两步都过了"),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == "passed"
    assert conclusion["case_progress"]["passed"] == 2


async def test_case_step_failed_blocks_passed(make_session, monkeypatch):
    tools = _case_tools(monkeypatch)
    session = make_session(goal="", case=_case(), mode=Mode.AUTO)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "report_step", "args": {"step": 1, "status": "failed", "detail": "首页没出来"}},
        {"tool": "report_step", "args": {"step": 2, "status": "skipped", "detail": "前序失败"}},
        _finish_plan("passed", "我说过了"),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == "failed"
    assert "非 passed" in conclusion["summary"]


async def test_case_steps_closed_on_budget_abort(make_session, monkeypatch):
    tools = _case_tools(monkeypatch)
    session = make_session(goal="", case=_case(), mode=Mode.AUTO, max_steps=1)
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "observe_snapshot", "args": {}},
        _finish_plan(),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == "aborted"
    assert session.case.step(1).status == "unknown"
    assert session.case.step(1).detail.startswith("会话收尾")


@pytest.mark.parametrize("verdict", ["true", "false", "unknown"])
async def test_real_assertion_tools_drive_step_conclusion(make_session, verdict):
    from dsh_helpers import FakeEngine, FakeResolver, make_ctx
    from uita.tools.dsh.textcase import parse_text_case
    from uita.tools.dsh.tools.assertion import AssertTextTool, AssertValueTool
    from uita.tools.dsh.tools.meta import ReportStepTool, TaskFinishTool

    session = make_session(mode=Mode.READONLY, case=parse_text_case("1. 检查设置标题"))
    engine = FakeEngine(text_value="设置", attr_value=None)
    ctx = make_ctx(engine, session, resolver=FakeResolver())
    assertion = AssertValueTool() if verdict == "unknown" else AssertTextTool()
    args = ({"target": "标题", "attr": "missing", "expected": "1"}
            if verdict == "unknown" else
            {"target": "标题", "expected": "设置" if verdict == "true" else "错误值"})
    status = {"true": "passed", "false": "failed", "unknown": "unknown"}[verdict]
    tools = {tool.name: tool for tool in (assertion, ReportStepTool(), TaskFinishTool())}
    planner = FakePlanner([
        {"tool": assertion.name, "args": args},
        {"tool": "report_step", "args": {"step": 1, "status": status}},
        _finish_plan(),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == ("passed" if verdict == "true" else "failed")
    assert session.case.step(1).status == status
    assert session.assertions[verdict] == 1
    if verdict == "true":
        evidence = session.case.step(1).evidence
        assert evidence[0]["kind"] == "assertion"
        assert evidence[0]["tool_result_seq"] < evidence[0]["seq"]


@pytest.mark.parametrize("change_expected", [False, True])
async def test_false_retry_requires_same_assertion_condition(make_session, change_expected):
    from dsh_helpers import FakeEngine, FakeResolver, make_ctx
    from uita.tools.dsh.textcase import parse_text_case
    from uita.tools.dsh.tools.assertion import AssertTextTool
    from uita.tools.dsh.tools.meta import ReportStepTool, TaskFinishTool

    session = make_session(mode=Mode.READONLY, case=parse_text_case("1. 检查设置标题"))
    engine = FakeEngine()
    values = iter(["错误值", "设置"])

    async def get_text(element):
        return next(values)

    engine.get_text = get_text
    ctx = make_ctx(engine, session, resolver=FakeResolver())
    tools = {tool.name: tool for tool in (AssertTextTool(), ReportStepTool(), TaskFinishTool())}
    first_expected = "原始预期" if change_expected else "设置"
    planner = FakePlanner([
        {"tool": "assert_text", "args": {"target": "标题", "expected": first_expected}},
        {"tool": "assert_text", "args": {"target": "标题", "expected": "设置"}},
        {"tool": "report_step", "args": {"step": 1, "status": "passed"}},
        _finish_plan(),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == ("failed" if change_expected else "passed")


async def test_step_true_evidence_cannot_be_reused_for_next_step(make_session, monkeypatch):
    tools = _case_tools(monkeypatch)
    session = make_session(mode=Mode.AUTO, case=_case())
    ctx = type("C", (), {"last_observation": {}, "session": session})()
    planner = FakePlanner([
        {"tool": "assert_probe", "args": {"verdict": "true"}},
        {"tool": "report_step", "args": {"step": 1, "status": "passed"}},
        {"tool": "report_step", "args": {"step": 2, "status": "passed"}},
        _finish_plan(),
    ])
    conclusion = await run_goal(session, ctx, planner, tools=tools, auto_observe=False)
    assert conclusion["state"] == "failed"
    assert session.case.step(1).status == "passed"
    assert session.case.step(2).status == "unknown"


async def test_natural_language_txt_pure_action_steps_and_auto_settle(make_session, monkeypatch):
    from uita.tools.dsh.textcase import parse_text_case

    tools = _case_tools(monkeypatch)
    nl_case = parse_text_case(
        "打开示例桌面应用首页，点击快速检索入口，确认扫描进度文字存在",
        default_name="nl_clean",
    )
    # 方式 A：前两步为纯操作步（通过动作工具直接 report_step），末步为断言步
    session_a = make_session(mode=Mode.AUTO, case=nl_case)
    ctx_a = type("C", (), {"last_observation": {}, "session": session_a})()
    planner_a = FakePlanner([
        {"tool": "act_click", "args": {"target": "首页"}},
        {"tool": "report_step", "args": {"step": 1, "status": "passed", "detail": "已打开首页"}},
        {"tool": "act_click", "args": {"target": "快速检索"}},
        {"tool": "report_step", "args": {"step": 2, "status": "passed", "detail": "已点击快速检索"}},
        {"tool": "assert_probe", "args": {"verdict": "true"}},
        {"tool": "report_step", "args": {"step": 3, "status": "passed", "detail": "扫描进度可见"}},
        _finish_plan("passed", "自然语言用例通过"),
    ])
    conclusion_a = await run_goal(session_a, ctx_a, planner_a, tools=tools, auto_observe=False)
    assert conclusion_a["state"] == "passed"
    assert session_a.case.all_passed() is True

    # 方式 B：自由自然语言执行（模型直接顺序执行动作+断言并 task_finish，自动对齐各步证据）
    nl_case_b = parse_text_case(
        "打开示例桌面应用首页，点击快速检索入口，确认扫描进度文字存在",
        default_name="nl_clean_auto",
    )
    session_b = make_session(mode=Mode.AUTO, case=nl_case_b)
    ctx_b = type("C", (), {"last_observation": {}, "session": session_b})()
    planner_b = FakePlanner([
        {"tool": "act_click", "args": {"target": "首页"}},
        {"tool": "act_click", "args": {"target": "快速检索"}},
        {"tool": "assert_probe", "args": {"verdict": "true"}},
        _finish_plan("passed", "已全部完成"),
    ])
    conclusion_b = await run_goal(session_b, ctx_b, planner_b, tools=tools, auto_observe=False)
    assert conclusion_b["state"] == "passed"
    assert session_b.case.all_passed() is True

