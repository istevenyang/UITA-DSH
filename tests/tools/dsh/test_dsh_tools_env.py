"""环境类工具：生命周期统一入口、安装、探针注入、状态。"""
from __future__ import annotations

import pytest

from dsh_helpers import FakeEngine, make_ctx
from uita.tools.dsh.registry import get_tool, load_builtin_tools


class _Manager:
    def __init__(self, engine):
        self.engine = engine


class _Stack:
    def __init__(self, engine):
        self.engine = engine
        self.enter_calls: list[dict] = []
        self.switch_calls: list[tuple] = []
        self.evicts: list[tuple] = []

    async def enter(self, app_name, platform, *, case_name="", is_first=False,
                    tag_prefix=""):
        self.enter_calls.append({
            "app": app_name, "platform": platform, "case_name": case_name,
            "is_first": is_first, "tag_prefix": tag_prefix,
        })
        return _Manager(self.engine)

    async def switch_to(self, app_name, platform, *, tag_prefix=""):
        self.switch_calls.append((app_name, platform, tag_prefix))
        return _Manager(self.engine)

    async def evict(self, app_name, platform):
        self.evicts.append((app_name, platform))


@pytest.fixture
def tools():
    load_builtin_tools()
    return get_tool


@pytest.fixture
def stack(monkeypatch, fake_engine):
    import uita.tools.dsh.tools.env as env_mod

    instance = _Stack(fake_engine)
    monkeypatch.setattr(env_mod, "_stack", lambda: instance)
    return instance


async def test_app_prepare_first_then_soft(tools, stack, fake_engine):
    ctx = make_ctx(session=None, app_name="demo")
    first = await tools("app_prepare").invoke(ctx, {})
    assert first.ok
    assert stack.enter_calls[0]["is_first"] is True
    assert stack.enter_calls[0]["tag_prefix"] == "dsh"
    assert ctx.engine is fake_engine
    assert first.data["healthy"] is True

    second = await tools("app_prepare").invoke(ctx, {})
    assert second.ok
    assert stack.enter_calls[1]["is_first"] is False


async def test_app_prepare_explicit_is_first_and_app(tools, stack):
    ctx = make_ctx()
    result = await tools("app_prepare").invoke(
        ctx, {"app": "sample_web", "is_first": False, "case_name": "冒烟"})
    assert result.ok
    assert stack.enter_calls[0]["app"] == "sample_web"
    assert stack.enter_calls[0]["is_first"] is False
    assert stack.enter_calls[0]["case_name"] == "冒烟"


async def test_app_prepare_without_app_fails(tools, stack):
    result = await tools("app_prepare").invoke(make_ctx(app_name=""), {})
    assert not result.ok
    assert "未指定 app" in result.error


async def test_app_prepare_health_probe_failure_tolerated(tools, stack, monkeypatch):
    engine = FakeEngine()

    async def _boom():
        raise RuntimeError("probe broken")

    engine.is_healthy = _boom  # type: ignore[assignment]
    stack.engine = engine
    result = await tools("app_prepare").invoke(make_ctx(app_name="demo"), {})
    assert result.ok
    assert "probe_failed" in str(result.data["healthy"])


async def test_env_install_requires_single_source(tools, stack):
    assert not (await tools("env_install").invoke(
        make_ctx(), {"app": "demo", "package_url": "u", "package_file": "f"})).ok
    assert not (await tools("env_install").invoke(make_ctx(), {"app": "demo"})).ok


async def test_env_install_success_collects_facts(tools, monkeypatch):
    import uita.core.lifecycle.app_installer as installer

    monkeypatch.setattr(installer, "install_app", lambda app, url: f"/Applications/{app}.app")
    monkeypatch.setattr(installer, "collect_install_facts",
                        lambda app, path, url, platform, duration_s=0.0: {"version": "1.0"})
    result = await tools("env_install").invoke(
        make_ctx(), {"app": "demo", "package_url": "https://x/demo.dmg"})
    assert result.ok
    assert result.data["target_path"] == "/Applications/demo.app"
    assert result.data["facts"] == {"version": "1.0"}


async def test_env_install_error_becomes_failure(tools, monkeypatch):
    import uita.core.lifecycle.app_installer as installer

    def _fail(app, url):
        raise installer.InstallError("下载失败")

    monkeypatch.setattr(installer, "install_app", _fail)
    result = await tools("env_install").invoke(
        make_ctx(), {"app": "demo", "package_url": "https://x/demo.dmg"})
    assert not result.ok
    assert "安装失败" in result.error


async def test_env_install_from_file(tools, monkeypatch):
    import uita.core.lifecycle.app_installer as installer

    monkeypatch.setattr(installer, "install_app_from_file",
                        lambda app, path: f"/Applications/{app}.app")
    monkeypatch.setattr(installer, "collect_install_facts",
                        lambda *a, **k: {"ok": True})
    result = await tools("env_install").invoke(
        make_ctx(), {"app": "demo", "package_file": "/tmp/demo.dmg"})
    assert result.ok


@pytest.mark.parametrize("status,ok", [("success", True), ("skipped", True), ("failed", False)])
async def test_env_probe_provision_status_mapping(tools, monkeypatch, status, ok):
    import uita.core.lifecycle.probe_provision as provision

    class _Result:
        def __init__(self):
            self.status = status
            self.message = f"status={status}"
            self.report = {"detail": 1}

    monkeypatch.setattr(provision, "run_probe_provision",
                        lambda app, platform=None, report_path="": _Result())
    result = await tools("env_probe_provision").invoke(make_ctx(), {"app": "sample_web"})
    assert result.ok is ok
    assert result.data["status"] == status


async def test_app_status_without_engine(tools):
    result = await tools("app_status").invoke(make_ctx(), {})
    assert not result.ok
    assert result.data["prepared"] is False


async def test_app_status_with_engine(tools, fake_engine):
    ctx = make_ctx(fake_engine)
    result = await tools("app_status").invoke(ctx, {})
    assert result.ok
    assert result.data["healthy"] is True
    assert result.data["window_title"] == "Fake Window"
    assert result.data["window_rect"] == [0, 0, 1280, 800]


async def test_app_switch_and_close(tools, stack, fake_engine):
    ctx = make_ctx()
    switched = await tools("app_switch").invoke(ctx, {"app": "other"})
    assert switched.ok
    assert stack.switch_calls[0][0] == "other"
    assert ctx.engine is fake_engine

    closed = await tools("app_close").invoke(ctx, {})
    assert closed.ok
    assert stack.evicts == [("other", ctx.os_name)]
    assert ctx.engine is None


async def test_app_close_without_app_fails(tools, stack):
    result = await tools("app_close").invoke(make_ctx(app_name=""), {})
    assert not result.ok
