"""观察工具：截图落会话目录、证据引用、能力缺失降级。"""
from __future__ import annotations

import os

import pytest

from dsh_helpers import FakeEngine, make_ctx
from uita.tools.dsh.registry import get_tool, load_builtin_tools


@pytest.fixture
def tools():
    load_builtin_tools()
    return get_tool


async def test_snapshot_saves_screenshot_and_evidence(tools, fake_engine, make_session):
    session = make_session()
    ctx = make_ctx(fake_engine, session=session)
    result = await tools("observe_snapshot").invoke(ctx, {})
    assert result.ok
    name = result.data["screenshot"]
    assert name.endswith(".png")
    assert os.path.isfile(session.store.artifact_path(name))
    assert result.evidence and result.evidence[0].kind == "screenshot"
    assert ctx.last_observation["screenshot"] == name


@pytest.mark.parametrize("empty_file", [False, True])
async def test_snapshot_does_not_claim_missing_or_empty_screenshot(
        tools, fake_engine, make_session, empty_file):
    async def no_screenshot(path):
        if empty_file:
            with open(path, "wb"):
                pass
        return path

    fake_engine.take_screenshot = no_screenshot
    result = await tools("observe_snapshot").invoke(
        make_ctx(fake_engine, session=make_session()), {})
    assert result.data.get("screenshot_saved") is not True
    assert "screenshot" not in result.data
    assert result.evidence == []
    assert "screenshot_error" in result.data


async def test_snapshot_without_session_skips_screenshot(tools, fake_engine):
    ctx = make_ctx(fake_engine, session=None)
    result = await tools("observe_snapshot").invoke(ctx, {})
    assert result.ok
    assert "screenshot_error" in result.data
    assert "screenshot" not in result.data


async def test_snapshot_screenshot_failure_tolerated(tools, fake_engine, make_session):
    engine = FakeEngine()
    engine.screenshot_error = OSError("截图不可用")
    ctx = make_ctx(engine, session=make_session())
    result = await tools("observe_snapshot").invoke(ctx, {})
    assert result.ok
    assert "截图不可用" in result.data["screenshot_error"]


async def test_snapshot_requires_engine(tools):
    result = await tools("observe_snapshot").invoke(make_ctx(), {})
    assert not result.ok
    assert "引擎未准备" in result.error


async def test_accessibility_truncated(tools, fake_engine):
    async def _long(max_nodes: int = 200) -> str:
        return "A" * 9000

    fake_engine.get_accessibility_summary = _long  # type: ignore[assignment]
    result = await tools("observe_snapshot").invoke(
        make_ctx(fake_engine), {"screenshot": False})
    assert result.ok
    assert len(result.data["accessibility"]) == 8000
    assert result.data["accessibility_truncated"] is True
    assert "screenshot" not in result.data


async def test_engine_without_accessibility_support(tools, fake_engine):
    fake_engine.get_accessibility_summary = None  # type: ignore[assignment]
    result = await tools("observe_snapshot").invoke(
        make_ctx(fake_engine), {"screenshot": False})
    assert result.ok
    assert "未实现可访问性摘要" in result.data["accessibility_error"]


@pytest.mark.parametrize("saved", [None, b"", False])
async def test_empty_capture_return_never_authorizes_existing_file(
        tools, fake_engine, make_session, saved):
    async def capture(path):
        with open(path, "wb") as handle:
            handle.write(b"stale-or-partial-capture")
        return saved

    fake_engine.take_screenshot = capture
    result = await tools("observe_snapshot").invoke(
        make_ctx(fake_engine, session=make_session()), {})
    assert result.data.get("screenshot_saved") is False
    assert "screenshot" not in result.data
    assert "screenshot_error" in result.data
    assert result.evidence == []
