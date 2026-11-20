"""IR 相关 HTTP 接口：GET/PUT /sessions/<id>/ir、POST /sessions/<id>/replay。"""
from __future__ import annotations

import json
import time

import pytest

from uita.tools.dsh import registry as dsh_registry
from uita.tools.dsh.ir import IR
from uita.tools.dsh.registry import TIER_READ, TIER_WRITE, ToolResult
from uita.tools.dsh.server import DshServer

TOKEN = "test-token"
HOST = "127.0.0.1:8765"

_PARAMS = {"target": {"type": "string"}, "app": {"type": "string"},
           "case_name": {"type": "string"}}


def _install(registry) -> None:
    async def locate_run(self, ctx, **kwargs):
        return ToolResult(ok=True, data={"level": "L1_MAP_EXACT", "found": True})

    async def exist_run(self, ctx, **kwargs):
        return ToolResult(ok=True, data={"verdict": "true"})

    async def prepare_run(self, ctx, **kwargs):
        return ToolResult(ok=True, data={"healthy": True})

    for name, run, tier in (("app_prepare", prepare_run, TIER_WRITE),
                            ("locate", locate_run, TIER_READ),
                            ("assert_exist", exist_run, TIER_READ)):
        cls = type(f"Fake_{name}", (dsh_registry.DshTool,), {
            "name": name, "tier": tier, "description": "fake",
            "params": _PARAMS, "required": (), "run": run,
        })
        dsh_registry.register_dsh_tool(cls)


def _ir_dict(steps=None) -> dict:
    return {
        "schema": "uita.ir/1",
        "meta": {"name": "ir_case", "app": "sample_web", "os": "macOS"},
        "steps": steps or [
            {"no": 1, "action": "wait",
             "target": {"kind": "element", "name": "首页-加载完成"}},
            {"no": 2, "action": "assert",
             "target": {"kind": "element", "name": "首页-加载完成"},
             "expect": [{"kind": "exist", "element": "首页-加载完成"}]},
        ],
    }


def _server() -> DshServer:
    return DshServer(host="127.0.0.1", port=8765, token=TOKEN, mode="auto",
                     auto_approve=True, store_root="")


def _call(server, method, path, payload=None, token=TOKEN):
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Host": HOST}
    if token:
        headers["X-Uita-Dsh-Token"] = token
    response = server.handle(method, path, body=body, headers=headers)
    return response.status, json.loads(response.body.decode("utf-8"))


def _wait_terminal(server, session_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, payload = _call(server, "GET", f"/sessions/{session_id}")
        if status == 200 and payload["session"]["state"] in {
                "passed", "failed", "aborted", "error"}:
            return payload["session"]
        time.sleep(0.05)
    raise AssertionError("会话未在超时内收敛")


def test_create_session_with_ir_runs_and_passes(registry, tmp_path):
    _install(registry)
    server = _server()
    server.store_root = str(tmp_path / "runs")
    status, payload = _call(server, "POST", "/sessions", {"ir": _ir_dict()})
    assert status == 200, payload
    session = _wait_terminal(server, payload["session"]["session_id"])
    assert session["state"] == "passed"
    assert session["ir"]["meta"]["name"] == "ir_case"
    assert session["ir_revision"] == 1


def test_get_ir_returns_404_for_react_session(registry):
    _install(registry)
    from dsh_helpers import FakePlanner

    server = _server()
    server._planner_factory = lambda: FakePlanner([
        {"tool": "task_finish", "args": {"status": "failed", "summary": "done"}},
    ])
    status, payload = _call(server, "POST", "/sessions", {"goal": "随便看看"})
    session_id = payload["session"]["session_id"]
    _wait_terminal(server, session_id)
    status, payload = _call(server, "GET", f"/sessions/{session_id}/ir")
    assert status == 404
    assert "IR" in payload["error"]


def test_get_ir_after_replay(registry):
    _install(registry)
    server = _server()
    status, payload = _call(server, "POST", "/sessions", {"ir": _ir_dict()})
    session_id = payload["session"]["session_id"]
    _wait_terminal(server, session_id)
    status, payload = _call(server, "GET", f"/sessions/{session_id}/ir")
    assert status == 200
    assert payload["revision"] == 1
    assert payload["ir"]["steps"][0]["action"] == "wait"


def test_put_ir_on_running_session_conflicts(registry, tmp_path):
    _install(registry)
    server = _server()
    session = server.sessions
    assert session == {}
    # 直接构造一个非终态会话占位（不起线程）
    from uita.tools.dsh.ir import IR as _IR
    from uita.tools.dsh.session import DshSession

    live = DshSession(goal="g", mode="auto", auto_approve=True,
                      root=str(tmp_path / "runs"))
    live.ir = _IR.from_dict(_ir_dict())
    server.sessions[live.session_id] = live
    status, payload = _call(server, "PUT", f"/sessions/{live.session_id}/ir",
                            {"ir": _ir_dict()})
    assert status == 409
    assert "运行中" in payload["error"]


def test_put_ir_bumps_revision_and_marks_human(registry, tmp_path):
    _install(registry)
    server = _server()
    status, payload = _call(server, "POST", "/sessions", {"ir": _ir_dict()})
    session_id = payload["session"]["session_id"]
    _wait_terminal(server, session_id)

    edited = _ir_dict()
    edited["steps"][0]["target"]["name"] = "首页-改名后的元素"
    edited["steps"][0]["source"] = "human"
    status, payload = _call(server, "PUT", f"/sessions/{session_id}/ir",
                            {"ir": edited})
    assert status == 200, payload
    assert payload["revision"] == 2
    assert payload["ir"]["steps"][0]["human_edited"] is True
    assert payload["ir"]["steps"][0]["target"]["name"] == "首页-改名后的元素"


def test_put_invalid_ir_rejected(registry):
    _install(registry)
    server = _server()
    status, payload = _call(server, "POST", "/sessions", {"ir": _ir_dict()})
    session_id = payload["session"]["session_id"]
    _wait_terminal(server, session_id)

    bad = _ir_dict()
    bad["steps"][0]["action"] = "explode"
    status, payload = _call(server, "PUT", f"/sessions/{session_id}/ir", {"ir": bad})
    assert status == 400
    assert "action" in payload["error"]


def test_replay_creates_new_session_bound_to_source(registry):
    _install(registry)
    server = _server()
    status, payload = _call(server, "POST", "/sessions", {"ir": _ir_dict()})
    source_id = payload["session"]["session_id"]
    _wait_terminal(server, source_id)

    status, payload = _call(server, "POST", f"/sessions/{source_id}/replay", {})
    assert status == 200, payload
    assert payload["source_session_id"] == source_id
    replay_id = payload["session"]["session_id"]
    assert replay_id != source_id

    replay = _wait_terminal(server, replay_id)
    assert replay["state"] == "passed"
    assert any(event["kind"] == "replay_of" for event in
               server.sessions[replay_id].events)


def test_replay_requires_ir(registry):
    _install(registry)
    from dsh_helpers import FakePlanner

    server = _server()
    server._planner_factory = lambda: FakePlanner([
        {"tool": "task_finish", "args": {"status": "failed", "summary": "done"}},
    ])
    status, payload = _call(server, "POST", "/sessions", {"goal": "x"})
    session_id = payload["session"]["session_id"]
    _wait_terminal(server, session_id)
    status, payload = _call(server, "POST", f"/sessions/{session_id}/replay", {})
    assert status == 400
    assert "没有 IR" in payload["error"]


def test_ir_endpoints_require_token(registry):
    _install(registry)
    server = _server()
    status, _ = _call(server, "GET", "/sessions/x/ir", token="")
    assert status == 401
