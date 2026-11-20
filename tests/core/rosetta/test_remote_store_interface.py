"""元素与路由远端数据库存储抽象接口（RemoteDbRosettaStoreBase / RemoteDbRouterProxyStoreBase）单测。"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from uita.core.rosetta.base import (
    DbFallbackRosettaStore,
    DbRosettaStore,
    RemoteDbRosettaStoreBase,
    register_rosetta_store,
)
from uita.core.rosetta.router_proxy_base import (
    DbFallbackRouterProxyStore,
    DbRouterProxyStore,
    RemoteDbRouterProxyStoreBase,
    register_router_proxy_store,
)


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    register_rosetta_store(None)
    register_router_proxy_store(None)
    monkeypatch.delenv("UITA_USE_LOCAL_ROSETTA", raising=False)
    monkeypatch.delenv("UITA_ROSETTA_STORE_CLASS", raising=False)
    monkeypatch.delenv("UITA_ROUTER_PROXY_STORE_CLASS", raising=False)
    yield
    register_rosetta_store(None)
    register_router_proxy_store(None)


def test_default_placeholder_db_stores_raise_not_implemented():
    with pytest.raises(NotImplementedError):
        DbRosettaStore("mysql://localhost/db").load_raw("/tmp/sample_web/rosetta_map.json")
    with pytest.raises(NotImplementedError):
        DbRouterProxyStore("mysql://localhost/db").load_raw("/tmp/sample_web/router_proxy.json")


def test_custom_remote_rosetta_store_and_fallback(tmp_path: Path):
    app_dir = tmp_path / "sample_web"
    app_dir.mkdir()
    local_file = app_dir / "rosetta_map.json"
    local_data = {"sample_web": {"首页": {"本地按钮": {"selector": "#local"}}}}
    local_file.write_text(json.dumps(local_data), encoding="utf-8")

    class _MockDbRosetta(RemoteDbRosettaStoreBase):
        def fetch_from_db(self, app_name: str):
            return {app_name: {"首页": {"远端按钮": {"selector": "#remote"}}}}

        def upsert_heal_to_db(self, app_name, os_name, records, reset_ids=None) -> int:
            return len(records)

    register_rosetta_store(_MockDbRosetta("postgresql://db.example.com/uita"))
    store = DbFallbackRosettaStore()
    loaded = store.load_raw(str(local_file))
    assert "远端按钮" in loaded["sample_web"]["首页"]

    inst = _MockDbRosetta("postgresql://db.example.com/uita")
    assert inst.db_url == "postgresql://db.example.com/uita"
    assert inst.apply_heal_records("sample_web", "macos", ["r1", "r2"]) == 2


def test_remote_rosetta_store_failure_falls_back_to_local_json(tmp_path: Path, monkeypatch):
    app_dir = tmp_path / "sample_web"
    app_dir.mkdir()
    local_file = app_dir / "rosetta_map.json"
    local_data = {"sample_web": {"首页": {"本地按钮": {"selector": "#local"}}}}
    local_file.write_text(json.dumps(local_data), encoding="utf-8")

    monkeypatch.setenv("UITA_USE_LOCAL_ROSETTA", "0")
    register_rosetta_store(DbRosettaStore)  # 未实现的占位类，调用必抛 NotImplementedError

    store = DbFallbackRosettaStore()
    loaded = store.load_raw(str(local_file))
    assert "本地按钮" in loaded["sample_web"]["首页"]


def test_env_class_loading_for_router_proxy_store(tmp_path: Path, monkeypatch):
    app_dir = tmp_path / "sample_web"
    app_dir.mkdir()
    local_file = app_dir / "router_proxy.json"
    local_file.write_text(json.dumps({"el-1": {"default": "TEXT_MATCH"}}), encoding="utf-8")

    class _EnvRouterStore(RemoteDbRouterProxyStoreBase):
        def fetch_from_db(self, app_name: str):
            return {"el-remote": {"default": "ROLE_MATCH"}}

        def upsert_routes_to_db(self, app_name, os_name, routes, reset_ids=None) -> int:
            return len(routes)

    fake_mod = types.ModuleType("_custom_store_mod")
    fake_mod.CustomRouterStore = _EnvRouterStore
    monkeypatch.setitem(sys.modules, "_custom_store_mod", fake_mod)
    monkeypatch.setenv("UITA_USE_LOCAL_ROSETTA", "0")
    monkeypatch.setenv("UITA_ROUTER_PROXY_STORE_CLASS", "_custom_store_mod:CustomRouterStore")

    store = DbFallbackRouterProxyStore()
    loaded = store.load_raw(str(local_file))
    assert loaded == {"el-remote": {"default": "ROLE_MATCH"}}
    assert _EnvRouterStore().apply_heal_routes("sample_web", "win", {"a": "TEXT_MATCH"}) == 1
