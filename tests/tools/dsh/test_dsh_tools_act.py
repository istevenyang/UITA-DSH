"""操作工具：元素优先、坐标护栏（R11）、定位失败不静默兜底。"""
from __future__ import annotations

import pytest

from dsh_helpers import FakeElement, FakeResolveResult, FakeResolver, make_ctx
from uita.tools.dsh.registry import get_tool, load_builtin_tools


@pytest.fixture
def tools():
    load_builtin_tools()
    return get_tool


async def test_click_by_element_resolves_then_clicks(tools, fake_engine):
    element = FakeElement()
    resolver = FakeResolver(FakeResolveResult(True, "L1_MAP_EXACT", element))
    result = await tools("act_click").invoke(
        make_ctx(fake_engine, resolver=resolver), {"target": "设置"})
    assert result.ok
    assert result.data["mode"] == "element"
    assert ("click", element) in fake_engine.calls


async def test_coords_without_source_rejected(tools, fake_engine):
    result = await tools("act_click").invoke(
        make_ctx(fake_engine, resolver=FakeResolver()), {"x": 10, "y": 20})
    assert not result.ok
    assert "source" in result.error
    assert not any(call[0] == "click" for call in fake_engine.calls)


async def test_coords_with_source_clicks_coords(tools, fake_engine):
    result = await tools("act_click").invoke(
        make_ctx(fake_engine, resolver=FakeResolver()),
        {"x": 10, "y": 20, "source": "视觉指纹命中"})
    assert result.ok
    assert result.data["mode"] == "coords"
    clicked = [call for call in fake_engine.calls if call[0] == "click"][0][1]
    assert clicked.coords == (10.0, 20.0)


async def test_click_without_target_or_coords_rejected(tools, fake_engine):
    result = await tools("act_click").invoke(
        make_ctx(fake_engine, resolver=FakeResolver()), {})
    assert not result.ok
    assert "target" in result.error


async def test_click_resolve_failure_does_not_fall_back_to_coords(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(False, "NOT_FOUND"))
    result = await tools("act_click").invoke(
        make_ctx(fake_engine, resolver=resolver), {"target": "幽灵按钮"})
    assert not result.ok
    assert "未定位到元素" in result.error
    assert not any(call[0] == "click" for call in fake_engine.calls)


async def test_type_with_submit_presses_enter(tools, fake_engine):
    element = FakeElement()
    resolver = FakeResolver(FakeResolveResult(True, "L3_TEXT_MATCH", element))
    result = await tools("act_type").invoke(
        make_ctx(fake_engine, resolver=resolver),
        {"target": "搜索框", "text": "abc", "submit": True})
    assert result.ok
    assert ("type_text", element, "abc", True) in fake_engine.calls
    assert ("press_key", "Enter") in fake_engine.calls


async def test_press_key(tools, fake_engine):
    result = await tools("act_press_key").invoke(make_ctx(fake_engine), {"key": "Escape"})
    assert result.ok
    assert ("press_key", "Escape") in fake_engine.calls


async def test_hover(tools, fake_engine):
    element = FakeElement()
    resolver = FakeResolver(FakeResolveResult(True, "L1_MAP_EXACT", element))
    result = await tools("act_hover").invoke(
        make_ctx(fake_engine, resolver=resolver), {"target": "菜单"})
    assert result.ok
    assert ("hover", element) in fake_engine.calls


async def test_scroll_without_target_passes_none(tools, fake_engine):
    result = await tools("act_scroll").invoke(
        make_ctx(fake_engine), {"direction": "down", "amount": 120})
    assert result.ok
    assert ("scroll", None, "down", 120) in fake_engine.calls


async def test_scroll_with_target_resolves(tools, fake_engine):
    element = FakeElement()
    resolver = FakeResolver(FakeResolveResult(True, "L1_MAP_EXACT", element))
    result = await tools("act_scroll").invoke(
        make_ctx(fake_engine, resolver=resolver),
        {"direction": "up", "target": "列表"})
    assert result.ok
    assert ("scroll", element, "up", 300) in fake_engine.calls


async def test_scroll_invalid_direction_rejected(tools, fake_engine):
    result = await tools("act_scroll").invoke(
        make_ctx(fake_engine), {"direction": "sideways"})
    assert not result.ok
    assert "direction" in result.error
