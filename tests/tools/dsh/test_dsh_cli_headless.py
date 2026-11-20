"""DSH 独立 CLI 与无头执行器单测（四道看门狗 / stream-json / Token 统计 / 引擎回收 / doctor）。"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from dsh_helpers import FakePlanner
from uita.tools.dsh import cli as dsh_cli
from uita.tools.dsh import headless as headless_mod
from uita.tools.dsh.headless import (
    doctor_check,
    make_perf_ai_runner,
    run_headless,
)
from uita.tools.dsh.policy import Mode
from uita.tools.dsh.registry import TIER_READ, DshTool, ToolResult


class _EchoTool(DshTool):
    name = "echo_read"
    tier = TIER_READ
    params = {"msg": {"type": "string"}}

    async def run(self, ctx, msg: str = "") -> ToolResult:
        return ToolResult(ok=True, data={"msg": msg})


class _FinishTool(DshTool):
    name = "task_finish"
    tier = TIER_READ
    params = {"status": {"type": "string"}, "summary": {"type": "string"}}

    async def run(self, ctx, status: str, summary: str) -> ToolResult:
        return ToolResult(ok=True, data={"status": status, "summary": summary})


def _tools(monkeypatch):
    monkeypatch.setattr("uita.tools.dsh.harness.load_builtin_tools", lambda: None)
    return {
        "echo_read": _EchoTool(),
        "task_finish": _FinishTool(),
    }


def test_run_headless_stream_json_and_envelope(monkeypatch, tmp_path):
    tools = _tools(monkeypatch)
    lines: list[str] = []
    planner = FakePlanner([
        {"tool": "echo_read", "args": {"msg": "hello"}},
        {"tool": "task_finish", "args": {"status": "passed", "summary": "全部完成"}},
    ])
    res = run_headless(
        goal="测试流式输出",
        root=str(tmp_path),
        planner=planner,
        tools=tools,
        output_format="stream-json",
        heartbeat_s=0,
        stream_sink=lines.append,
    )
    assert res.returncode == 0
    assert res.state == "passed"
    parsed = [json.loads(line) for line in lines]
    types = [item["type"] for item in parsed]
    assert "assistant" in types
    assert "tool_result" in types
    assert parsed[-1]["type"] == "result"
    assert parsed[-1]["is_error"] is False
    assert parsed[-1]["result"] == "全部完成"


def test_run_headless_json_array_format(monkeypatch, tmp_path):
    tools = _tools(monkeypatch)
    lines: list[str] = []
    planner = FakePlanner([
        {"tool": "task_finish", "args": {"status": "passed", "summary": "ok"}},
    ])
    res = run_headless(
        goal="json格式",
        root=str(tmp_path),
        planner=planner,
        tools=tools,
        output_format="json",
        heartbeat_s=0,
        stream_sink=lines.append,
    )
    assert res.returncode == 0
    arr = json.loads("\n".join(lines))
    assert isinstance(arr, list)
    assert arr[-1]["type"] == "result"


def test_watchdog_loop_repeat_aborts(monkeypatch, tmp_path):
    tools = _tools(monkeypatch)
    planner = FakePlanner([
        {"tool": "echo_read", "args": {"msg": "same"}},
        {"tool": "echo_read", "args": {"msg": "same"}},
        {"tool": "echo_read", "args": {"msg": "same"}},
        {"tool": "task_finish", "args": {"status": "passed", "summary": "不会走到这里"}},
    ])
    res = run_headless(
        goal="测试循环看门狗",
        root=str(tmp_path),
        planner=planner,
        tools=tools,
        loop_repeat=3,
        heartbeat_s=0,
        stream_sink=lambda _: None,
    )
    assert res.returncode == 1
    assert res.state == "aborted"
    assert res.error == "agent-loop"
    assert res.loop_count == 3
    assert "echo_read" in res.loop_signature


def test_watchdog_artifact_grace_aborts_when_missing(monkeypatch, tmp_path):
    tools = _tools(monkeypatch)
    missing_report = tmp_path / "must_exist.html"
    planner = FakePlanner([
        {"tool": "echo_read", "args": {"msg": "step1"}},
        {"tool": "task_finish", "args": {"status": "passed", "summary": "ok"}},
    ])
    # artifact_grace_s 设极小值，首步后即触发检查
    res = run_headless(
        goal="测试产物看门狗",
        root=str(tmp_path),
        planner=planner,
        tools=tools,
        artifact_path=str(missing_report),
        artifact_grace_s=0.0001,
        heartbeat_s=0,
        stream_sink=lambda _: None,
    )
    assert res.returncode == 1
    assert res.state == "aborted"
    assert res.error == "no-artifact"
    assert res.artifact_seen is False


def test_watchdog_artifact_grace_passes_when_written(monkeypatch, tmp_path):
    tools = _tools(monkeypatch)
    report = tmp_path / "written.html"
    report.write_text("<html>ok</html>", encoding="utf-8")
    planner = FakePlanner([
        {"tool": "echo_read", "args": {"msg": "step1"}},
        {"tool": "task_finish", "args": {"status": "passed", "summary": "ok"}},
    ])
    res = run_headless(
        goal="产物已存在",
        root=str(tmp_path),
        planner=planner,
        tools=tools,
        artifact_path=str(report),
        artifact_grace_s=0.0001,
        heartbeat_s=0,
        stream_sink=lambda _: None,
    )
    assert res.returncode == 0
    assert res.artifact_seen is True


def test_session_stall_watchdog():
    clock = [1000.0]
    from uita.tools.dsh.session import DshSession

    sess = DshSession(
        goal="stall test",
        mode=Mode.AUTO,
        stall_timeout_s=10.0,
        now=lambda: clock[0],
    )
    assert sess.can_continue()[0] is True
    clock[0] = 1015.0
    ok, reason = sess.can_continue()
    assert ok is False
    assert "stalled:" in reason


def test_session_records_token_usage():
    from uita.tools.dsh.session import DshSession

    sess = DshSession(goal="usage", mode=Mode.AUTO)
    sess.record_usage({"prompt_tokens": 120, "completion_tokens": 30, "cached_tokens": 20})
    sess.record_usage({"input_tokens": 80, "output_tokens": 20, "total_tokens": 100})
    assert sess.usage == {
        "input_tokens": 200,
        "output_tokens": 50,
        "cached_tokens": 20,
        "total_tokens": 250,
    }


def test_cleanup_engine_called_on_exit(monkeypatch, tmp_path):
    tools = _tools(monkeypatch)
    evicted: list[tuple[str, str]] = []

    class _FakeStack:
        async def evict(self, app_name: str, os_name: str):
            evicted.append((app_name, os_name))

    monkeypatch.setattr(
        "uita.core.lifecycle.app_lifecycle_stack.get_default_stack",
        lambda: _FakeStack(),
    )

    class _PrepTool(DshTool):
        name = "fake_prep"
        tier = TIER_READ
        params = {}

        async def run(self, ctx) -> ToolResult:
            ctx.set_engine(object(), "sample_web")
            return ToolResult(ok=True, data={})

    tools["fake_prep"] = _PrepTool()
    planner = FakePlanner([
        {"tool": "fake_prep", "args": {}},
        {"tool": "task_finish", "args": {"status": "passed", "summary": "done"}},
    ])
    res = run_headless(
        goal="测试引擎清理",
        app_name="sample_web",
        root=str(tmp_path),
        planner=planner,
        tools=tools,
        heartbeat_s=0,
        stream_sink=lambda _: None,
    )
    assert res.returncode == 0
    assert evicted and evicted[0][0] == "sample_web"


def test_perf_ai_runner_compatible_with_pipeline(monkeypatch, tmp_path):
    tools = _tools(monkeypatch)
    monkeypatch.setattr("uita.tools.dsh.harness.all_tools", lambda: tools)
    planner = FakePlanner([
        {"tool": "task_finish", "args": {"status": "passed", "summary": "报告完成"}},
    ])
    runner = make_perf_ai_runner(planner=planner)
    out_dir = tmp_path / "perf_ai_analysis"
    run = runner(
        "请生成性能报告",
        out_dir=str(out_dir),
        session_id="perf-s1",
        max_turns=10,
        timeout_s=60,
    )
    assert run.returncode == 0
    assert run.session_id == "perf-s1"
    assert os.path.isfile(run.stdout_path)
    assert "state=passed" in run.summary()


def test_cli_doctor_json(monkeypatch, capsys):
    monkeypatch.setenv("DSH_API_URL", "https://ds.example.com/v1/chat/completions")
    monkeypatch.setenv("DSH_API_KEY", "sk-test-12345678")
    rc = dsh_cli.main(["doctor", "--json", "--strict"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True
    assert out["api_key_set"] is True
    assert "sk-test-12345678" not in json.dumps(out)
    assert out["tools_count"] >= 28


def test_cli_modes_catalog_json(capsys):
    rc = dsh_cli.main(["modes", "--category", "execution", "--json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "runner" in out["modes"]
    assert "batch" in out["modes"]


def test_cli_dispatches_uita_subcommands_and_aliases(monkeypatch):
    seen = []

    def _fake_dispatch(mode_name, mode_argv):
        seen.append((mode_name, mode_argv))
        return 0

    monkeypatch.setattr(dsh_cli, "_dispatch_uita_mode", _fake_dispatch)
    assert dsh_cli.main(["runner", "--cases-dir", "cases/sample_web/smoke.tcs", "--no-engine"]) == 0
    assert dsh_cli.main(["tcs", "--cases-dir", "cases/sample_web/smoke.tcs"]) == 0
    assert dsh_cli.main(["env-check", "--json"]) == 0
    assert dsh_cli.main(["--mode", "lint", "--cases-dir", "cases/sample_web"]) == 0
    assert seen == [
        ("runner", ["--cases-dir", "cases/sample_web/smoke.tcs", "--no-engine"]),
        ("runner", ["--cases-dir", "cases/sample_web/smoke.tcs"]),
        ("env_check", ["--json"]),
        ("lint", ["--cases-dir", "cases/sample_web"]),
    ]


def test_run_headless_auto_compiles_tcs_only_when_passed(monkeypatch, tmp_path):
    tools = _tools(monkeypatch)

    class _Click(DshTool):
        name = "act_click"
        tier = TIER_READ
        params = {"target": {"type": "string"}}

        async def run(self, ctx, target: str = "") -> ToolResult:
            return ToolResult(ok=True, data={"target": target})

    class _Assert(DshTool):
        name = "assert_exist"
        tier = TIER_READ
        params = {"target": {"type": "string"}}

        async def run(self, ctx, target: str = "") -> ToolResult:
            return ToolResult(ok=True, data={"verdict": "true", "target": target})

    tools["act_click"] = _Click()
    tools["assert_exist"] = _Assert()

    # 1) passed 会话 → 自动编译产出 .tcs
    tcs_pass = tmp_path / "passed.tcs"
    planner_ok = FakePlanner([
        {"tool": "act_click", "args": {"target": "快速检索入口"}},
        {"tool": "assert_exist", "args": {"target": "扫描进度文字"}},
        {"tool": "task_finish", "args": {"status": "passed", "summary": "通过"}},
    ])
    res_ok = run_headless(
        goal="smoke_clean",
        app_name="sample_web",
        root=str(tmp_path / "runs1"),
        planner=planner_ok,
        tools=tools,
        tcs_out=str(tcs_pass),
        heartbeat_s=0,
        stream_sink=lambda _: None,
    )
    assert res_ok.returncode == 0
    assert tcs_pass.is_file()
    assert "click[快速检索入口]" in tcs_pass.read_text(encoding="utf-8")

    # 2) failed 会话 → 不产出 .tcs
    tcs_fail = tmp_path / "failed.tcs"
    planner_fail = FakePlanner([
        {"tool": "act_click", "args": {"target": "快速检索入口"}},
        {"tool": "task_finish", "args": {"status": "failed", "summary": "没过"}},
    ])
    res_fail = run_headless(
        goal="smoke_clean_fail",
        app_name="sample_web",
        root=str(tmp_path / "runs2"),
        planner=planner_fail,
        tools=tools,
        tcs_out=str(tcs_fail),
        heartbeat_s=0,
        stream_sink=lambda _: None,
    )
    assert res_fail.returncode == 1
    assert not tcs_fail.exists()


def test_cli_case_text_txt_file_and_bare_tcs_out_promote(monkeypatch, tmp_path):
    from uita import config

    monkeypatch.setattr(config, "CASES_DIR", str(tmp_path / "cases"))
    txt_file = tmp_path / "smoke_clean_case.txt"
    txt_file.write_text(
        "打开首页（预期：快速检索入口存在）\n"
        "点击快速检索入口（预期：扫描进度文字存在）\n",
        encoding="utf-8",
    )

    captured = {}

    def _fake_run_headless(**kwargs):
        captured.update(kwargs)
        from uita.tools.dsh.compile import (
            compile_ir_file,
            promote_ir,
            resolve_tcs_output_path,
        )
        from uita.tools.dsh.headless import DshHeadlessResult
        from uita.tools.dsh.ir import IR, save_ir

        ir_obj = IR.from_dict({
            "schema": "uita.ir/1",
            "meta": {
                "name": kwargs["case"].name,
                "app": kwargs["app_name"],
                "os": "macOS",
            },
            "steps": [
                {
                    "no": 1,
                    "phase": "setup",
                    "action": "open",
                    "target": {"kind": "element", "name": "首页"},
                },
                {
                    "no": 2,
                    "phase": "main",
                    "action": "click",
                    "target": {"kind": "element", "name": "快速检索入口"},
                    "expect": [{"kind": "exist", "element": "扫描进度文字"}],
                },
            ],
        })
        ir_tmp = str(tmp_path / "plan.ir.json")
        save_ir(ir_obj, ir_tmp)
        tcs_target = resolve_tcs_output_path(kwargs["tcs_out"], ir_obj)
        _, tcs_saved = compile_ir_file(ir_tmp, tcs_target)
        promoted = promote_ir(ir_obj) if kwargs.get("promote") else ""
        return DshHeadlessResult(
            returncode=0, state="passed", tcs_path=tcs_saved, promoted_ir_path=promoted,
        )

    monkeypatch.setattr(dsh_cli, "run_headless", _fake_run_headless)
    rc = dsh_cli.main([
        "run", "--app", "sample_web",
        "--case-text", str(txt_file),
        "--tcs-out", "--promote",
    ])
    assert rc == 0
    assert captured["tcs_out"] == "auto"
    assert captured["promote"] is True
    assert captured["case"].name == "smoke_clean_case"
    assert len(captured["case"].steps) == 2
    assert (tmp_path / "cases" / "sample_web" / "smoke_clean_case.tcs").is_file()
    assert (tmp_path / "cases" / "sample_web" / "ir" / "smoke_clean_case.ir.json").is_file()


def test_cli_headless_flag_invokes_run_headless(monkeypatch, tmp_path):
    captured = {}

    def _fake_run_headless(**kwargs):
        captured.update(kwargs)
        from uita.tools.dsh.headless import DshHeadlessResult

        return DshHeadlessResult(returncode=0, state="passed")

    monkeypatch.setattr(dsh_cli, "run_headless", _fake_run_headless)
    rc = dsh_cli.main([
        "-p", "-y", "--model", "default-model", "--output-format", "stream-json",
        "--session-id", "sid-1", "--max-turns", "15",
        "--", "---\nname: perf-analysis\n请输出报告",
    ])
    assert rc == 0
    assert captured["goal"].startswith("---\nname: perf-analysis")
    assert captured["model"] == "default-model"
    assert captured["output_format"] == "stream-json"
    assert captured["session_id"] == "sid-1"
    assert captured["max_steps"] == 15
    assert captured["mode"] is Mode.AUTO
