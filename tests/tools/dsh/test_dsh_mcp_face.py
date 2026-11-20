"""MCP 外壳：工具清单同源、策略闸、低层 Server 装配。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from uita.tools.dsh import mcp_face
from uita.tools.dsh.policy import Mode
from uita.tools.dsh.registry import all_tools, load_builtin_tools


@pytest.fixture
def fake_dsh():
    load_builtin_tools()
    return SimpleNamespace(sessions={}, contexts={}, host="127.0.0.1", port=8765)


def test_tool_list_matches_registry():
    load_builtin_tools()
    listing = {item["name"] for item in mcp_face._tool_list()}
    assert listing == set(all_tools())
    assert "app_prepare" in listing


async def test_call_tool_unknown(fake_dsh):
    payload = await mcp_face.call_tool_async(fake_dsh, "nope", {})
    assert payload["ok"] is False
    assert "未知工具" in payload["error"]


async def test_readonly_gate_denies_action(fake_dsh):
    payload = await mcp_face.call_tool_async(fake_dsh, "act_click",
                                            {"target": "设置"})
    assert payload["ok"] is False
    assert "readonly" in payload["error"]


async def test_supervised_gate_requires_console_approval(fake_dsh):
    session = SimpleNamespace(session_id="s1", mode=Mode.SUPERVISED, app_name="demo")
    fake_dsh.sessions = {"s1": session}
    payload = await mcp_face.call_tool_async(
        fake_dsh, "app_prepare", {"app": "demo"}, session_id="s1")
    assert payload["ok"] is False
    assert "人工批准" in payload["error"]


async def test_read_tool_runs(fake_dsh):
    payload = await mcp_face.call_tool_async(fake_dsh, "app_status", {})
    assert payload["tool"] == "app_status"
    assert payload["ok"] is False
    assert "引擎未准备" in payload["error"]


def test_build_server_registers_handlers(fake_dsh):
    from mcp.types import CallToolRequest, ListToolsRequest

    server = mcp_face.build_server(fake_dsh)
    assert ListToolsRequest in server.request_handlers
    assert CallToolRequest in server.request_handlers


def test_serve_http_without_uvicorn_gives_guidance(monkeypatch, fake_dsh):
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "uvicorn":
            raise ImportError("no uvicorn")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(RuntimeError) as excinfo:
        import asyncio

        asyncio.run(mcp_face.serve_http(fake_dsh))
    assert "uvicorn" in str(excinfo.value)
