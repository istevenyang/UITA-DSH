"""DSH 单测共享夹具：注册表隔离 + 假引擎 + 临时会话。

约束（R6）：不启动真实引擎/CDP/App、不访问网络、不写仓库 report 目录
（会话根一律用 ``tmp_path``）。
"""
from __future__ import annotations

import pytest

from dsh_helpers import FakeEngine, make_ctx
from uita.tools.dsh import registry as dsh_registry
from uita.tools.dsh.policy import Mode
from uita.tools.dsh.session import DshSession


@pytest.fixture
def registry():
    """隔离注册表：测试内可自由注册，结束后还原全局状态。"""
    saved = dict(dsh_registry._REGISTRY)
    saved_loaded = dsh_registry._BUILTIN_LOADED
    dsh_registry._REGISTRY.clear()
    dsh_registry._BUILTIN_LOADED = True
    try:
        yield dsh_registry
    finally:
        dsh_registry._REGISTRY.clear()
        dsh_registry._REGISTRY.update(saved)
        dsh_registry._BUILTIN_LOADED = saved_loaded


@pytest.fixture
def fake_engine():
    return FakeEngine()


@pytest.fixture
def make_session(tmp_path):
    def _make(**kwargs):
        kwargs.setdefault("goal", "测试目标")
        kwargs.setdefault("root", str(tmp_path / "runs"))
        kwargs.setdefault("mode", Mode.SUPERVISED)
        return DshSession(**kwargs)

    return _make


@pytest.fixture
def ctx_factory():
    return make_ctx
