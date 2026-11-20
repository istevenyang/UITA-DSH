"""DSH 工具注册表：注册/校验/Schema 派生。"""
from __future__ import annotations

import pytest

from uita.tools.dsh.registry import (TIER_ACTION, TIER_READ, ArtifactRef,
                                     DshTool, DshToolError, ToolResult,
                                     build_mcp_tools, build_tool_specs,
                                     get_tool, register_dsh_tool, tool_names)


class _Echo(DshTool):
    name = "echo"
    tier = TIER_READ
    description = "回显"
    params = {"text": {"type": "string"}, "count": {"type": "integer"}}
    required = ("text",)

    async def run(self, ctx, text: str, count: int = 1) -> ToolResult:
        return ToolResult(ok=True, data={"text": text, "count": count})


class _Boom(DshTool):
    name = "boom"
    tier = TIER_ACTION
    params = {}

    async def run(self, ctx) -> ToolResult:
        raise RuntimeError("宿主炸了")


class _NoName(DshTool):
    async def run(self, ctx) -> ToolResult:  # pragma: no cover - 注册即失败
        return ToolResult()


class _BadTier(DshTool):
    name = "bad-tier"
    tier = "danger"
    params = {}

    async def run(self, ctx) -> ToolResult:  # pragma: no cover - 注册即失败
        return ToolResult()


def test_register_and_lookup(registry):
    register_dsh_tool(_Echo)
    assert get_tool("echo") is not None
    assert tool_names() == ["echo"]
    assert get_tool("missing") is None


def test_duplicate_registration_rejected(registry):
    register_dsh_tool(_Echo)
    with pytest.raises(DshToolError):
        register_dsh_tool(_Echo)


def test_missing_name_rejected(registry):
    with pytest.raises(DshToolError):
        register_dsh_tool(_NoName)


def test_invalid_tier_rejected(registry):
    with pytest.raises(DshToolError):
        register_dsh_tool(_BadTier)


def test_schema_is_strict(registry):
    register_dsh_tool(_Echo)
    schema = get_tool("echo").schema()
    assert schema["type"] == "function"
    params = schema["function"]["parameters"]
    assert params["additionalProperties"] is False
    assert params["required"] == ["text"]
    assert set(params["properties"]) == {"text", "count"}


def test_build_tool_specs_and_mcp_tools(registry):
    register_dsh_tool(_Echo)
    specs = build_tool_specs()
    assert specs[0]["function"]["name"] == "echo"
    mcp_tools = build_mcp_tools()
    assert mcp_tools[0]["inputSchema"]["required"] == ["text"]
    assert mcp_tools[0]["tier"] == TIER_READ


async def test_invoke_success_and_timing(registry):
    register_dsh_tool(_Echo)
    result = await get_tool("echo").invoke(None, {"text": "hi"})
    assert result.ok and result.data == {"text": "hi", "count": 1}
    assert result.took_ms >= 0


async def test_invoke_rejects_unknown_arg(registry):
    register_dsh_tool(_Echo)
    result = await get_tool("echo").invoke(None, {"text": "hi", "nope": 1})
    assert not result.ok
    assert "未知参数" in result.error


async def test_invoke_rejects_missing_required(registry):
    register_dsh_tool(_Echo)
    result = await get_tool("echo").invoke(None, {})
    assert not result.ok
    assert "缺少必填参数" in result.error


async def test_invoke_wraps_tool_exception(registry):
    register_dsh_tool(_Boom)
    result = await get_tool("boom").invoke(None, {})
    assert not result.ok
    assert "RuntimeError" in result.error


async def test_invoke_rejects_non_object_args(registry):
    register_dsh_tool(_Echo)
    result = await get_tool("echo").invoke(None, ["text"])
    assert not result.ok


def test_tool_result_dict_and_artifact_ref():
    ref = ArtifactRef(name="shot.png", path="/tmp/shot.png", kind="screenshot")
    result = ToolResult(ok=False, error="x", evidence=[ref])
    payload = result.to_dict()
    assert payload["ok"] is False
    assert payload["evidence"][0]["name"] == "shot.png"
    assert ToolResult.fail("bad", extra=1).data == {"extra": 1}
