"""定位工具：层级透出、未命中即失败、不臆造坐标。"""
from __future__ import annotations

import pytest

from dsh_helpers import FakeElement, FakeResolveResult, FakeResolver, make_ctx
from uita.tools.dsh.registry import get_tool, load_builtin_tools


@pytest.fixture
def tools():
    load_builtin_tools()
    return get_tool


async def test_locate_found_returns_level_and_geometry(tools, fake_engine):
    element = FakeElement(coords=(10, 20), bbox=(10, 20, 30, 40), text="设置")
    resolver = FakeResolver(FakeResolveResult(True, "L7_VISUAL_FP", element))
    result = await tools("locate").invoke(
        make_ctx(fake_engine, resolver=resolver), {"target": "设置"})
    assert result.ok
    assert result.data["level"] == "L7_VISUAL_FP"
    assert result.data["coords"] == [10, 20]
    assert result.data["bbox"] == [10, 20, 30, 40]
    assert result.data["rect"] == [5, 6, 7, 8]
    assert result.data["fallback"] is True


async def test_locate_not_found_is_failure(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(False, "NOT_FOUND"))
    result = await tools("locate").invoke(
        make_ctx(fake_engine, resolver=resolver), {"target": "不存在"})
    assert not result.ok
    assert result.data["found"] is False
    assert "未定位到元素" in result.error


async def test_locate_requires_engine(tools):
    result = await tools("locate").invoke(make_ctx(), {"target": "设置"})
    assert not result.ok


async def test_locate_resolver_exception_fails(tools, fake_engine):
    resolver = FakeResolver(raise_error=RuntimeError("resolver 崩了"))
    result = await tools("locate").invoke(
        make_ctx(fake_engine, resolver=resolver), {"target": "设置"})
    assert not result.ok
    assert "RuntimeError" in result.error
