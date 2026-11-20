"""DSH 内核：鉴权/来源校验/会话生命周期/长轮询/产物/工具直调。"""
from __future__ import annotations

import json
import time

import pytest

from dsh_helpers import FakePlanner
from uita.tools.dsh.server import DshServer

TOKEN = "test-token"
HOST = "127.0.0.1:8765"


def _server(planner_factory=None, auto_approve=False):
    return DshServer(
        host="127.0.0.1", port=8765, token=TOKEN, mode="auto",
        planner_factory=planner_factory
        or (lambda: FakePlanner([{"tool": "task_finish",
                                  "args": {"status": "passed", "summary": "done"}}])),
        auto_approve=auto_approve,
    )


def _headers(token=TOKEN, host=HOST, origin=""):
    headers = {"Host": host}
    if token:
        headers["X-Uita-Dsh-Token"] = token
    if origin:
        headers["Origin"] = origin
    return headers


def _body(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _call(server, method, path, payload=None, headers=None):
    """返回 (status, payload|None, content_type)。"""
    response = server.handle(
        method, path, body=b"" if payload is None else _body(payload),
        headers=headers if headers is not None else _headers(),
    )
    decoded = None
    if response.content_type.startswith("application/json"):
        decoded = json.loads(response.body.decode("utf-8"))
    return response.status, decoded, response.content_type


def _new_session(server, goal="x"):
    status, payload, _ = _call(server, "POST", "/sessions", {"goal": goal})
    assert status == 200, payload
    return payload["session"]["session_id"]


def _wait_terminal(server, session_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, payload, _ = _call(server, "GET", f"/sessions/{session_id}")
        assert status == 200
        state = payload["session"]["state"]
        if state in {"passed", "failed", "aborted", "error"}:
            # 终态置位后 harness 还会追加 state 事件，稍等避免增量拉取竞态
            time.sleep(0.2)
            return payload["session"]
        time.sleep(0.05)
    raise AssertionError("会话未在超时内结束")


def test_healthz_without_token():
    server = _server()
    response = server.handle("GET", "/healthz", headers={"Host": HOST})
    assert response.status == 200


def test_requires_token():
    server = _server()
    assert _call(server, "GET", "/state", headers=_headers(token=""))[0] == 401
    assert _call(server, "GET", "/state", headers=_headers(token="wrong"))[0] == 401


def test_accepts_bearer_token():
    server = _server()
    status, _payload, _ = _call(
        server, "GET", "/state",
        headers={"Host": HOST, "Authorization": f"Bearer {TOKEN}"})
    assert status == 200


def test_rejects_bad_host_and_origin():
    server = _server()
    assert _call(server, "GET", "/state",
                 headers=_headers(host="evil.example.com"))[0] == 403
    assert _call(server, "GET", "/state",
                 headers=_headers(origin="http://evil.example.com"))[0] == 403


def test_state_lists_tools():
    server = _server()
    status, payload, _ = _call(server, "GET", "/state")
    assert status == 200
    names = {item["name"] for item in payload["tools"]}
    assert {"app_prepare", "observe_snapshot", "locate", "act_click",
            "assert_exist", "case_run", "task_finish"} <= names


def test_create_session_requires_goal():
    server = _server()
    status, payload, _ = _call(server, "POST", "/sessions", {})
    assert status == 400
    assert "goal" in payload["error"]


def test_create_session_runs_to_passed():
    server = _server()
    session_id = _new_session(server, "断言首页")
    final = _wait_terminal(server, session_id)
    assert final["state"] == "passed"
    events = _call(server, "GET", f"/sessions/{session_id}/events?since=0")[1]["events"]
    kinds = [event["kind"] for event in events]
    assert "plan" in kinds and "tool_result" in kinds and "conclusion" in kinds


def test_events_incremental_since():
    server = _server()
    session_id = _new_session(server)
    _wait_terminal(server, session_id)
    first = _call(server, "GET", f"/sessions/{session_id}/events?since=0")[1]
    assert first["last_seq"] >= 1
    second = _call(
        server, "GET", f"/sessions/{session_id}/events?since={first['last_seq']}")[1]
    assert second["events"] == []


def test_second_session_conflicts_while_running():
    class _SlowPlanner:
        async def plan(self, **kwargs):
            import asyncio

            from uita.tools.dsh.planner import Plan

            await asyncio.sleep(1.0)
            return Plan(tool="task_finish", args={"status": "passed", "summary": "慢"})

    server = _server(planner_factory=lambda: _SlowPlanner())
    session_id = _new_session(server, "慢会话")
    status, payload, _ = _call(server, "POST", "/sessions", {"goal": "第二个"})
    assert status == 409
    assert session_id in payload["active"]
    _call(server, "POST", f"/sessions/{session_id}/control", {"action": "stop"})
    final = _wait_terminal(server, session_id)
    assert final["state"] == "aborted"


def test_control_unknown_action_and_approve_without_pending():
    server = _server()
    session_id = _new_session(server)
    _wait_terminal(server, session_id)
    assert _call(server, "POST", f"/sessions/{session_id}/control",
                 {"action": "dance"})[0] == 400
    assert _call(server, "POST", f"/sessions/{session_id}/control",
                 {"action": "approve"})[0] == 409


def test_hint_validation_and_delivery():
    server = _server()
    session_id = _new_session(server)
    assert _call(server, "POST", f"/sessions/{session_id}/hint", {"text": "  "})[0] == 400
    assert _call(server, "POST", f"/sessions/{session_id}/hint",
                 {"text": "先关弹窗"})[0] == 200


def test_artifacts_route():
    server = _server()
    session_id = _new_session(server)
    _wait_terminal(server, session_id)
    assert _call(server, "GET",
                 f"/sessions/{session_id}/artifacts/missing.png")[0] == 404

    session = server.sessions[session_id]
    target = session.store.artifact_path("shot.png")
    with open(target, "wb") as handle:
        handle.write(b"\x89PNG")
    status, _payload, content_type = _call(
        server, "GET", f"/sessions/{session_id}/artifacts/shot.png")
    assert status == 200 and content_type == "image/png"
    assert _call(server, "GET",
                 f"/sessions/{session_id}/artifacts/..%2Fescape")[0] == 404


def test_tool_call_unknown_and_direct_call():
    server = _server()
    assert _call(server, "POST", "/tools/nope", {})[0] == 404
    status, payload, _ = _call(server, "POST", "/tools/app_status", {})
    assert status == 200
    assert payload["ok"] is False
    assert "引擎未准备" in payload["error"]


def test_bad_json_and_unknown_route():
    server = _server()
    response = server.handle("POST", "/sessions", body=b"{oops", headers=_headers())
    assert response.status == 400
    response = server.handle("POST", "/sessions", body=b"[1,2]", headers=_headers())
    assert response.status == 400
    assert _call(server, "GET", "/nope")[0] == 404


def test_console_page_served():
    server = _server()
    status, _payload, content_type = _call(server, "GET", "/")
    assert status == 200
    assert "text/html" in content_type


def test_non_loopback_host_refused():
    server = _server()
    server.host = "0.0.0.0"
    with pytest.raises(RuntimeError):
        server.serve_forever()


CASE_TEXT = """用例名称：隐私开关检查
1. 打开应用首页
   预期：看到首页
2. 断言「已开启」可见
   预期：显示已开启
"""


def test_create_session_with_text_case():
    server = _server()
    status, payload, _ = _call(server, "POST", "/sessions", {"test_case": CASE_TEXT})
    assert status == 200
    snapshot = payload["session"]
    assert snapshot["case"]["name"] == "隐私开关检查"
    assert [step["no"] for step in snapshot["case"]["steps"]] == [1, 2]
    assert snapshot["case_progress"]["total"] == 2
    session_id = snapshot["session_id"]
    final = _wait_terminal(server, session_id)
    # 模型（桩）没有上报步骤，收尾时未完成步骤应为 unknown 且整体不得判过
    assert final["state"] == "failed"
    assert final["case"]["steps"][0]["status"] == "unknown"


def test_report_route_shape():
    server = _server()
    session_id = _new_session(server)
    _wait_terminal(server, session_id)
    status, payload, _ = _call(server, "GET", f"/sessions/{session_id}/report")
    assert status == 200
    report = payload["report"]
    assert report["session_id"] == session_id
    assert set(report) >= {"goal", "app", "state", "conclusion", "case",
                           "case_progress", "assertions", "artifacts", "dir",
                           "duration_s"}


def test_create_session_requires_goal_or_case():
    server = _server()
    status, payload, _ = _call(server, "POST", "/sessions", {})
    assert status == 400
    assert "goal" in payload["error"] and "test_case" in payload["error"]
