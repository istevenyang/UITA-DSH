"""断言工具：三态契约，unknown 不折算。"""
from __future__ import annotations

import pytest

from dsh_helpers import (FakeElement, FakeEngine, FakeResolveResult,
                         FakeResolver, make_ctx)
from uita.tools.dsh.registry import get_tool, load_builtin_tools


@pytest.fixture
def tools():
    load_builtin_tools()
    return get_tool


async def test_assert_exist_true(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(True, "L1_MAP_EXACT", FakeElement()))
    result = await tools("assert_exist").invoke(
        make_ctx(fake_engine, resolver=resolver), {"target": "设置"})
    assert result.data["verdict"] == "true"
    assert result.data["level"] == "L1_MAP_EXACT"
    assert result.data["judger"] == "resolver:exists"


async def test_assert_exist_false(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(False, "NOT_FOUND"))
    result = await tools("assert_exist").invoke(
        make_ctx(fake_engine, resolver=resolver), {"target": "设置"})
    assert result.data["verdict"] == "false"


async def test_assert_absent_true(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(False, "NOT_FOUND"))
    result = await tools("assert_absent").invoke(
        make_ctx(fake_engine, resolver=resolver), {"target": "弹窗"})
    assert result.data["verdict"] == "true"


async def test_assert_text_contains(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(True, "L1_MAP_EXACT", FakeElement()))
    result = await tools("assert_text").invoke(
        make_ctx(fake_engine, resolver=resolver),
        {"target": "状态", "expected": "设置"})
    assert result.data["verdict"] == "true"
    assert result.data["actual"] == "设置"


async def test_assert_text_equals_match(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(True, "L1_MAP_EXACT", FakeElement()))
    result = await tools("assert_text").invoke(
        make_ctx(fake_engine, resolver=resolver),
        {"target": "状态", "expected": "设置", "match": "equals"})
    assert result.data["verdict"] == "true"


async def test_assert_text_regex(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(True, "L1_MAP_EXACT", FakeElement()))
    result = await tools("assert_text").invoke(
        make_ctx(fake_engine, resolver=resolver),
        {"target": "状态", "expected": "^设", "match": "regex"})
    assert result.data["verdict"] == "true"


async def test_assert_text_invalid_regex_is_unknown(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(True, "L1_MAP_EXACT", FakeElement()))
    result = await tools("assert_text").invoke(
        make_ctx(fake_engine, resolver=resolver),
        {"target": "状态", "expected": "([", "match": "regex"})
    assert result.data["verdict"] == "unknown"


async def test_assert_text_element_missing_is_false(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(False, "NOT_FOUND"))
    result = await tools("assert_text").invoke(
        make_ctx(fake_engine, resolver=resolver),
        {"target": "状态", "expected": "x"})
    assert result.data["verdict"] == "false"


async def test_assert_value_missing_attribute_is_unknown(tools):
    engine = FakeEngine(attr_value=None)
    resolver = FakeResolver(FakeResolveResult(True, "L1_MAP_EXACT", FakeElement()))
    result = await tools("assert_value").invoke(
        make_ctx(engine, resolver=resolver),
        {"target": "开关", "attr": "checked", "expected": "true"})
    assert result.data["verdict"] == "unknown"


async def test_assert_value_match(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(True, "L1_MAP_EXACT", FakeElement()))
    result = await tools("assert_value").invoke(
        make_ctx(fake_engine, resolver=resolver),
        {"target": "开关", "attr": "checked", "expected": "on"})
    assert result.data["verdict"] == "true"


async def test_assert_js_without_expression_is_unknown(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(), js_expr=None)
    result = await tools("assert_js").invoke(
        make_ctx(fake_engine, resolver=resolver),
        {"target": "标题", "expected": "首页"})
    assert result.data["verdict"] == "unknown"


async def test_assert_js_engine_unsupported_is_unknown(tools):
    engine = FakeEngine()
    engine.eval_js = None  # type: ignore[assignment]
    resolver = FakeResolver(FakeResolveResult())
    result = await tools("assert_js").invoke(
        make_ctx(engine, resolver=resolver), {"target": "标题", "expected": "首页"})
    assert result.data["verdict"] == "unknown"


async def test_assert_js_evaluated(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(), js_expr="document.title")
    result = await tools("assert_js").invoke(
        make_ctx(fake_engine, resolver=resolver),
        {"target": "标题", "expected": "ok"})
    assert result.data["verdict"] == "true"
    assert ("eval_js", "document.title") in fake_engine.calls


async def test_assert_js_lookup_error_is_unknown(tools, fake_engine):
    resolver = FakeResolver(FakeResolveResult(), js_expr="__raise__")
    result = await tools("assert_js").invoke(
        make_ctx(fake_engine, resolver=resolver), {"target": "标题", "expected": "x"})
    assert result.data["verdict"] == "unknown"


async def test_assertions_without_engine_are_unknown(tools):
    for name, payload in (
        ("assert_exist", {"target": "a"}),
        ("assert_absent", {"target": "a"}),
        ("assert_text", {"target": "a", "expected": "b"}),
        ("assert_value", {"target": "a", "attr": "x", "expected": "y"}),
        ("assert_js", {"target": "a", "expected": "y"}),
    ):
        result = await get_tool(name).invoke(make_ctx(), payload)
        assert result.data["verdict"] == "unknown", name


async def test_resolver_exception_is_unknown(tools, fake_engine):
    resolver = FakeResolver(raise_error=RuntimeError("resolver 崩了"))
    result = await tools("assert_exist").invoke(
        make_ctx(fake_engine, resolver=resolver), {"target": "设置"})
    assert result.data["verdict"] == "unknown"
    assert "resolver 崩了" in result.data["detail"]
