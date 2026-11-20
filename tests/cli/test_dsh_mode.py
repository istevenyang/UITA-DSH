"""cli/modes/dsh_mode.py — DSH 三个入口的参数与退出码。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from uita.cli.modes import dsh_mode
from uita.cli.parser import build_parser


def _cli(**kwargs):
    base = dict(dsh_host="", dsh_port=0, dsh_token="", dsh_mode="",
                dsh_max_steps=0, dsh_timeout=0, dsh_transport="stdio",
                dsh_auto_approve=False, goal="", app="", case_text="", case_file="",
                dsh_model="", dsh_llm_url="")
    base.update(kwargs)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("mode", ["dsh-serve", "dsh-run", "dsh-mcp"])
def test_parser_accepts_dsh_modes(mode):
    args = build_parser().parse_args(["--mode", mode])
    assert args.mode == mode
    assert args.dsh_transport == "stdio"


def test_parser_accepts_dsh_args():
    args = build_parser().parse_args([
        "--mode", "dsh-run", "--goal", "点开设置", "--app", "sample_web",
        "--dsh-mode", "auto", "--dsh-max-steps", "12", "--dsh-auto-approve",
    ])
    assert args.goal == "点开设置"
    assert args.dsh_mode == "auto"
    assert args.dsh_max_steps == 12
    assert args.dsh_auto_approve is True


def test_dsh_run_without_goal_returns_2():
    assert dsh_mode._handle_dsh_run(_cli(goal="")) == 2


def test_dsh_run_maps_passed_to_zero(monkeypatch):
    import uita.tools.dsh.harness as harness

    async def _fake_run_goal(session, ctx, planner):
        return {"state": "passed", "summary": "ok"}

    monkeypatch.setattr(harness, "run_goal", _fake_run_goal)
    assert dsh_mode._handle_dsh_run(_cli(goal="点开设置", dsh_mode="auto")) == 0


def test_dsh_run_maps_failed_to_one(monkeypatch):
    import uita.tools.dsh.harness as harness

    async def _fake_run_goal(session, ctx, planner):
        return {"state": "failed", "summary": "没找到按钮"}

    monkeypatch.setattr(harness, "run_goal", _fake_run_goal)
    assert dsh_mode._handle_dsh_run(_cli(goal="点开设置", dsh_mode="auto")) == 1


def test_dsh_run_supervised_without_auto_approve_degrades_to_readonly(monkeypatch):
    import uita.tools.dsh.harness as harness

    seen = {}

    async def _fake_run_goal(session, ctx, planner):
        seen["mode"] = session.mode.value
        return {"state": "failed", "summary": "只读"}

    monkeypatch.setattr(harness, "run_goal", _fake_run_goal)
    dsh_mode._handle_dsh_run(_cli(goal="点开设置", dsh_mode="supervised"))
    assert seen["mode"] == "readonly"


def test_dsh_serve_returns_zero_on_clean_exit(monkeypatch):
    from uita.tools.dsh import server as server_mod

    monkeypatch.setattr(server_mod.DshServer, "serve_forever", lambda self: None)
    assert dsh_mode._handle_dsh_serve(_cli()) == 0


def test_dsh_serve_returns_one_on_failure(monkeypatch):
    from uita.tools.dsh import server as server_mod

    def _boom(self):
        raise RuntimeError("端口被占用")

    monkeypatch.setattr(server_mod.DshServer, "serve_forever", _boom)
    assert dsh_mode._handle_dsh_serve(_cli()) == 1


def test_dsh_mcp_stdio_dispatch(monkeypatch):
    from uita.tools.dsh import mcp_face

    seen = {}

    async def _fake_stdio(server):
        seen["called"] = True

    monkeypatch.setattr(mcp_face, "serve_stdio", _fake_stdio)
    assert dsh_mode._handle_dsh_mcp(_cli(dsh_transport="stdio")) == 0
    assert seen.get("called") is True


def test_dsh_mcp_http_failure_maps_to_one(monkeypatch):
    from uita.tools.dsh import mcp_face

    async def _fake_http(server, host="", port=0):
        raise RuntimeError("需要 uvicorn")

    monkeypatch.setattr(mcp_face, "serve_http", _fake_http)
    assert dsh_mode._handle_dsh_mcp(_cli(dsh_transport="http")) == 1


CASE_TEXT = "用例名称：桩用例\n1. 打开首页\n2. 断言已开启"


def test_dsh_run_with_case_text_passes_case_to_session(monkeypatch, tmp_path):
    import uita.tools.dsh.harness as harness

    seen = {}

    async def _fake_run_goal(session, ctx, planner):
        seen["case"] = session.case
        for step in session.case.steps:
            step.status = "passed"
        return {"state": "passed", "summary": "ok"}

    monkeypatch.setattr(harness, "run_goal", _fake_run_goal)
    assert dsh_mode._handle_dsh_run(_cli(case_text=CASE_TEXT, dsh_mode="auto")) == 0
    assert seen["case"].name == "桩用例"
    assert len(seen["case"].steps) == 2


def test_dsh_run_with_case_file(monkeypatch, tmp_path):
    import uita.tools.dsh.harness as harness

    path = tmp_path / "case.md"
    path.write_text(CASE_TEXT, encoding="utf-8")

    async def _fake_run_goal(session, ctx, planner):
        return {"state": "failed", "summary": "没找到按钮"}

    monkeypatch.setattr(harness, "run_goal", _fake_run_goal)
    assert dsh_mode._handle_dsh_run(
        _cli(case_file=str(path), dsh_mode="auto")) == 1


def test_dsh_run_without_goal_and_case_returns_2():
    assert dsh_mode._handle_dsh_run(_cli()) == 2


@pytest.mark.parametrize("mode", ["dsh-replay"])
def test_parser_accepts_dsh_replay_mode(mode):
    args = build_parser().parse_args(["--mode", mode, "--ir", "/tmp/x.ir.json"])
    assert args.mode == mode
    assert args.ir == "/tmp/x.ir.json"
    assert args.ir_save == ""


def test_replay_without_ir_returns_2():
    assert dsh_mode._handle_dsh_replay(_cli()) == 2


def test_replay_invalid_ir_returns_2(tmp_path):
    path = tmp_path / "bad.ir.json"
    path.write_text("{not json", encoding="utf-8")
    assert dsh_mode._handle_dsh_replay(_cli(ir=str(path))) == 2


def test_replay_rejects_invalid_schema(tmp_path):
    import json

    path = tmp_path / "bad2.ir.json"
    path.write_text(json.dumps({"schema": "uita.ir/9", "meta": {}, "steps": []}),
                    encoding="utf-8")
    assert dsh_mode._handle_dsh_replay(_cli(ir=str(path))) == 2


def test_main_module_modes_and_sample_web_smoke():
    import os
    import uita.__main__ as main_mod
    from uita import config
    from uita.cli.modes.env_check_mode import _handle_env_check
    from uita.cli.modes.lint_mode import _handle_lint
    from uita.cli.modes.parse_mode import _handle_parse

    main_mod._auto_discover()
    assert _handle_env_check(SimpleNamespace()) == 0

    sample_tcs = os.path.join(config.CASES_DIR, "sample_web", "smoke_search.tcs")
    assert os.path.isfile(sample_tcs)
    parse_cli = build_parser().parse_args(["--mode", "parse", "--cases-dir", sample_tcs])
    assert _handle_parse(parse_cli) == 0

    lint_cli = build_parser().parse_args(["--mode", "lint", "--app", "sample_web"])
    assert _handle_lint(lint_cli) == 0
