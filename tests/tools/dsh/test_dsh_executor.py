"""IR 执行器：零 LLM 重放、步骤门禁、fail-fast、heal、预算。

约束（R6）：全部用假工具 + 假引擎，不启动真实引擎、不访问网络。
"""
from __future__ import annotations

import pytest

from dsh_helpers import FakeEngine, make_ctx
from uita.tools.dsh import executor as executor_mod
from uita.tools.dsh import registry as dsh_registry
from uita.tools.dsh.ir import IR
from uita.tools.dsh.policy import Mode
from uita.tools.dsh.registry import TIER_ACTION, TIER_READ, TIER_WRITE, ToolResult
from uita.tools.dsh.session import DshSession

_TOOL_PARAMS = {
    name: {"type": "string"}
    for name in ("target", "x", "y", "source", "text", "key", "direction", "amount",
                 "app", "case_name", "expected", "attr", "skip_l4", "is_first",
                 "force_hard_restart", "submit", "clear")
}


def _install(registry, behaviors: dict[str, tuple[str, object]]) -> None:
    """按名字注册假工具：name -> (tier, behavior(kwargs) -> ToolResult)。"""
    for name, (tier, behavior) in behaviors.items():
        async def run(self, ctx, _behavior=behavior, **kwargs):
            return _behavior(kwargs)  # type: ignore[operator]

        cls = type(f"Fake_{name}", (dsh_registry.DshTool,), {
            "name": name, "tier": tier, "description": f"fake {name}",
            "params": _TOOL_PARAMS, "required": (), "run": run,
        })
        dsh_registry.register_dsh_tool(cls)


def _ok(**data):
    return lambda kwargs: ToolResult(ok=True, data=dict(data))


def _fail(error: str):
    return lambda kwargs: ToolResult.fail(error)


def _verdict(value: str):
    return lambda kwargs: ToolResult(ok=True, data={"verdict": value})


def _ir(steps: list[dict], *, app: str = "sample_web", name: str = "demo") -> IR:
    return IR.from_dict({
        "schema": "uita.ir/1",
        "meta": {"name": name, "app": app, "os": "macOS"},
        "steps": steps,
    })


def _session(tmp_path, **kwargs) -> DshSession:
    kwargs.setdefault("goal", "ir")
    kwargs.setdefault("mode", Mode.AUTO)
    kwargs.setdefault("auto_approve", True)
    kwargs.setdefault("root", str(tmp_path / "runs"))
    return DshSession(**kwargs)


def _ctx(session, prepared: bool = True):
    engine = FakeEngine() if prepared else None
    return make_ctx(fake_engine=engine, session=session, app_name="sample_web")


@pytest.mark.asyncio
async def test_all_steps_passed(tmp_path, registry):
    calls: list[tuple[str, dict]] = []

    def click(kwargs):
        calls.append(("click", kwargs))
        return ToolResult(ok=True, data={"clicked": True})

    def exist(kwargs):
        calls.append(("exist", kwargs))
        return ToolResult(ok=True, data={"verdict": "true"})

    _install(registry, {"act_click": (TIER_ACTION, click),
                        "assert_exist": (TIER_READ, exist)})
    session = _session(tmp_path)
    ir = _ir([
        {"no": 1, "action": "click", "target": {"kind": "element", "name": "立即搜索"}},
        {"no": 2, "action": "assert",
         "target": {"kind": "element", "name": "立即搜索"},
         "expect": [{"kind": "exist", "element": "搜索结果面板"}]},
    ])
    conclusion = await executor_mod.run_ir(session, _ctx(session), ir)

    assert conclusion["state"] == "passed"
    assert all(step.status == "passed" for step in session.case.steps)
    assert session.assertions["true"] == 1
    assert [name for name, _ in calls] == ["click", "exist"]


@pytest.mark.asyncio
async def test_assert_false_fails_after_one_heal(tmp_path, registry):
    hits: list[str] = []

    def exist(kwargs):
        hits.append(kwargs.get("target", ""))
        return ToolResult(ok=True, data={"verdict": "false"})

    _install(registry, {"assert_exist": (TIER_READ, exist)})
    session = _session(tmp_path)
    ir = _ir([{"no": 1, "action": "assert",
               "target": {"kind": "element", "name": "结果面板"},
               "expect": [{"kind": "exist", "element": "结果面板"}]}])
    conclusion = await executor_mod.run_ir(session, _ctx(session), ir)

    assert conclusion["state"] == "failed"
    assert conclusion.get("failed_step") == 1
    assert len(hits) == 2, "一次 heal 重试"
    assert session.heal_rounds == 1
    assert session.assertions["false"] == 2


@pytest.mark.asyncio
async def test_unknown_blocks_pass(tmp_path, registry):
    _install(registry, {"assert_exist": (TIER_READ, _verdict("unknown"))})
    session = _session(tmp_path)
    ir = _ir([{"no": 1, "action": "assert",
               "target": {"kind": "element", "name": "面板"},
               "expect": [{"kind": "exist", "element": "面板"}]}])
    conclusion = await executor_mod.run_ir(session, _ctx(session), ir)

    assert conclusion["state"] == "failed"
    assert "unknown" in conclusion["summary"]
    assert session.assertions["unknown"] >= 1


@pytest.mark.asyncio
async def test_action_failure_is_fail_fast(tmp_path, registry):
    after: list[str] = []

    def boom(kwargs):
        return ToolResult.fail("点击失败: 元素不可见")

    def exist(kwargs):
        after.append("assert")
        return ToolResult(ok=True, data={"verdict": "true"})

    _install(registry, {"act_click": (TIER_ACTION, boom),
                        "assert_exist": (TIER_READ, exist)})
    session = _session(tmp_path)
    ir = _ir([
        {"no": 1, "action": "click", "target": {"kind": "element", "name": "立即搜索"}},
        {"no": 2, "action": "assert",
         "target": {"kind": "element", "name": "结果面板"},
         "expect": [{"kind": "exist", "element": "结果面板"}]},
    ])
    conclusion = await executor_mod.run_ir(session, _ctx(session), ir)

    assert conclusion["state"] == "failed"
    assert after == [], "首步失败后不得继续执行"
    assert session.case.step(1).status == "failed"
    assert session.case.step(2).status == "unknown", "未执行步骤收尾为 unknown"


@pytest.mark.asyncio
async def test_wait_step_uses_locate(tmp_path, registry):
    levels: list[str] = []

    def locate(kwargs):
        levels.append(kwargs.get("target", ""))
        return ToolResult(ok=True, data={"level": "L3_TEXT_MATCH", "found": True})

    _install(registry, {"locate": (TIER_READ, locate)})
    session = _session(tmp_path)
    ir = _ir([{"no": 1, "action": "wait",
               "target": {"kind": "element", "name": "首页-加载完成"},
               "args": {"timeout": 2}}])
    conclusion = await executor_mod.run_ir(session, _ctx(session), ir)

    assert conclusion["state"] == "passed"
    assert levels == ["首页-加载完成"]


@pytest.mark.asyncio
async def test_wait_step_timeout(tmp_path, registry):
    _install(registry, {"locate": (TIER_READ, _fail("未定位到元素"))})
    session = _session(tmp_path)
    ir = _ir([{"no": 1, "action": "wait",
               "target": {"kind": "element", "name": "永不出现的元素"},
               "args": {"timeout": 0.05}}])
    conclusion = await executor_mod.run_ir(session, _ctx(session), ir)

    assert conclusion["state"] == "failed"
    assert "等待超时" in conclusion["summary"]


@pytest.mark.asyncio
async def test_coord_target_passes_source(tmp_path, registry):
    seen: list[dict] = []

    def click(kwargs):
        seen.append(kwargs)
        return ToolResult(ok=True)

    _install(registry, {"act_click": (TIER_ACTION, click)})
    session = _session(tmp_path)
    ir = _ir([{"no": 1, "action": "click",
               "target": {"kind": "coord", "coord": [12, 34],
                          "coord_source": "人工目测"}}])
    conclusion = await executor_mod.run_ir(session, _ctx(session), ir)

    assert conclusion["state"] == "passed"
    assert seen[0]["x"] == 12.0
    assert seen[0]["y"] == 34.0
    assert seen[0]["source"] == "人工目测"


@pytest.mark.asyncio
async def test_budget_exhausted_aborts(tmp_path, registry):
    _install(registry, {"act_click": (TIER_ACTION, _ok())})
    session = _session(tmp_path, max_steps=1)
    ir = _ir([
        {"no": 1, "action": "click", "target": {"kind": "element", "name": "a"}},
        {"no": 2, "action": "click", "target": {"kind": "element", "name": "b"}},
    ])
    conclusion = await executor_mod.run_ir(session, _ctx(session), ir)

    assert conclusion["state"] == "aborted"
    assert "预算耗尽" in conclusion["summary"]


@pytest.mark.asyncio
async def test_missing_tool_fails_gracefully(tmp_path, registry):
    _install(registry, {"assert_exist": (TIER_READ, _verdict("true"))})
    session = _session(tmp_path)
    ir = _ir([{"no": 1, "action": "click",
               "target": {"kind": "element", "name": "a"}}])
    conclusion = await executor_mod.run_ir(session, _ctx(session), ir)

    assert conclusion["state"] == "failed"
    assert "工具缺失" in conclusion["summary"]


@pytest.mark.asyncio
async def test_prepares_app_when_engine_missing(tmp_path, registry):
    prepared: list[dict] = []

    def app_prepare(kwargs):
        prepared.append(kwargs)
        return ToolResult(ok=True, data={"healthy": True})

    _install(registry, {"app_prepare": (TIER_WRITE, app_prepare),
                        "act_click": (TIER_ACTION, _ok())})
    session = _session(tmp_path)
    ir = _ir([{"no": 1, "action": "click",
               "target": {"kind": "element", "name": "a"}}])
    conclusion = await executor_mod.run_ir(session, _ctx(session, prepared=False), ir)

    assert conclusion["state"] == "passed"
    assert prepared and prepared[0]["app"] == "sample_web"


@pytest.mark.asyncio
async def test_no_planner_is_constructed(tmp_path, registry, monkeypatch):
    from uita.tools.dsh import planner as planner_mod

    def _boom(self, *args, **kwargs):
        raise AssertionError("IR 重放不得构造 Planner")

    monkeypatch.setattr(planner_mod.DshPlanner, "__init__", _boom)
    _install(registry, {"act_click": (TIER_ACTION, _ok())})
    session = _session(tmp_path)
    ir = _ir([{"no": 1, "action": "click",
               "target": {"kind": "element", "name": "a"}}])
    conclusion = await executor_mod.run_ir(session, _ctx(session), ir)
    assert conclusion["state"] == "passed"


@pytest.mark.asyncio
async def test_session_snapshot_exposes_ir(tmp_path, registry):
    _install(registry, {"act_click": (TIER_ACTION, _ok())})
    session = _session(tmp_path)
    ir = _ir([{"no": 1, "action": "click",
               "target": {"kind": "element", "name": "a"}}], name="snap_case")
    await executor_mod.run_ir(session, _ctx(session), ir)

    snapshot = session.snapshot()
    assert snapshot["ir"]["meta"]["name"] == "snap_case"
    assert snapshot["ir_revision"] == 1
