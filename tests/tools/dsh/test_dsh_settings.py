"""DSH 界面配置：落盘/校验/优先级/掩码/接口。

约束（R6）：不触碰真实用户目录、不访问网络；所有路径用 ``tmp_path``。
"""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from uita.tools.dsh import planner, settings_store

ENV_NAMES = (
    "DSH_API_URL", "UITA_DSH_LLM_URL",
    "DSH_API_KEY", "UITA_DSH_API_KEY", "API_KEY",
    "UITA_DSH_MODEL", "UITA_DSH_PORT", "UITA_DSH_MODE",
    "UITA_DSH_MAX_STEPS", "UITA_DSH_TIMEOUT_S", "UITA_DSH_SETTINGS_FILE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """清空模型相关环境变量。

    必须先 import ``uita.config``：它在导入时把 ``.env.local`` 注入 ``os.environ``，
    若清理发生在导入之前，惰性 import 会把键值重新写回，env 优先级用例会失真。
    """
    from uita import config  # noqa: F401 - 触发 .env.local 注入后再清理

    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def cfg(tmp_path, clean_env, monkeypatch):
    target = str(tmp_path / "dsh_settings.json")
    monkeypatch.setenv("UITA_DSH_SETTINGS_FILE", target)
    return target


def _valid(**overrides):
    payload = {
        "llm": {"url": "https://llm.example.com/v1/chat/completions",
                "api_key": "sk-test", "model": "default-model"},
        "server": {"port": 8765, "mode": "auto"},
        "run": {"max_steps": 20, "timeout_s": 300},
        "defaults": {"app": "sample_media"},
    }
    for section, values in overrides.items():
        payload[section] = {**payload.get(section, {}), **values}
    return payload


# ---------------------------------------------------------------------------
# 路径与读取
# ---------------------------------------------------------------------------

def test_settings_path_in_user_data_dir():
    from uita import config

    assert settings_store.settings_path() == os.path.join(
        config.USER_DATA_DIR, "dsh_settings.json"
    )


def test_load_missing_file_returns_defaults(cfg):
    data = settings_store.load(cfg)
    assert data["llm"]["url"] == ""
    assert data["llm"]["api_key"] == ""
    assert data["server"]["port"] == 0
    assert "error" not in data
    assert not os.path.exists(cfg)


def test_load_invalid_json_degrades_with_error(cfg):
    with open(cfg, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    data = settings_store.load(cfg)
    assert data["server"]["port"] == 0
    assert "无法解析" in data["error"]


def test_load_non_object_json_degrades_with_error(cfg):
    with open(cfg, "w", encoding="utf-8") as handle:
        json.dump([1, 2, 3], handle)
    data = settings_store.load(cfg)
    assert "不是对象" in data["error"]


def test_load_drops_bad_field_types(cfg):
    with open(cfg, "w", encoding="utf-8") as handle:
        json.dump({
            "llm": {"url": 123, "api_key": None, "model": ["x"]},
            "server": {"port": "8765", "mode": "chaos"},
            "run": {"max_steps": True, "timeout_s": "300"},
            "defaults": {"app": "bad name"},
        }, handle)
    data = settings_store.load(cfg)
    assert data["llm"] == {"url": "", "api_key": "", "model": ""}
    assert data["server"] == {"port": 0, "mode": ""}
    assert data["run"] == {"max_steps": 0, "timeout_s": 0}
    assert data["defaults"]["app"] == ""


def test_load_accepts_valid_file(cfg):
    with open(cfg, "w", encoding="utf-8") as handle:
        json.dump(_valid(), handle)
    data = settings_store.load(cfg)
    assert data["server"]["port"] == 8765
    assert data["llm"]["model"] == "default-model"


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------

def test_save_writes_normalized_file(cfg):
    saved = settings_store.save(_valid(), cfg)
    assert saved["server"]["mode"] == "auto"
    with open(cfg, encoding="utf-8") as handle:
        assert json.load(handle)["version"] == settings_store.VERSION


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不走 POSIX 权限位")
def test_save_sets_owner_only_permission(cfg):
    settings_store.save(_valid(), cfg)
    mode = stat.S_IMODE(os.stat(cfg).st_mode)
    assert mode == 0o600


def test_save_overwrites_previous_file(cfg):
    settings_store.save(_valid(), cfg)
    settings_store.save(_valid(**{"defaults": {"app": "sample_web"}}), cfg)
    assert settings_store.load(cfg)["defaults"]["app"] == "sample_web"


def test_save_keeps_key_when_field_absent(cfg):
    settings_store.save(_valid(), cfg)
    settings_store.save({"llm": {"url": "https://other.example.com/x"}}, cfg)
    assert settings_store.load(cfg)["llm"]["api_key"] == "sk-test"


def test_save_keeps_key_when_field_empty(cfg):
    settings_store.save(_valid(), cfg)
    settings_store.save(_valid(**{"llm": {"api_key": ""}}), cfg)
    assert settings_store.load(cfg)["llm"]["api_key"] == "sk-test"


def test_save_clear_api_key(cfg):
    settings_store.save(_valid(), cfg)
    payload = _valid()
    payload["clear_api_key"] = True
    payload["llm"]["api_key"] = ""
    settings_store.save(payload, cfg)
    assert settings_store.load(cfg)["llm"]["api_key"] == ""


def test_save_replaces_key(cfg):
    settings_store.save(_valid(), cfg)
    settings_store.save(_valid(**{"llm": {"api_key": "sk-new"}}), cfg)
    assert settings_store.load(cfg)["llm"]["api_key"] == "sk-new"


def test_save_leaves_no_temp_files(cfg):
    settings_store.save(_valid(), cfg)
    leftovers = [n for n in os.listdir(os.path.dirname(cfg)) if n.startswith(".dsh_settings.")]
    assert leftovers == []


# ---------------------------------------------------------------------------
# 校验矩阵
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "ftp://host/x", "file:///etc/passwd", "llm.example.com/v1", "https://",
])
def test_validate_rejects_bad_url(cfg, url):
    with pytest.raises(ValueError):
        settings_store.save(_valid(**{"llm": {"url": url}}), cfg)


@pytest.mark.parametrize("url,ok", [
    ("", True),
    ("https://host/v1/chat/completions", True),
    ("http://127.0.0.1:8080/v1/chat/completions", True),
])
def test_validate_accepts_urls(cfg, url, ok):
    assert settings_store.normalize(_valid(**{"llm": {"url": url}}))["llm"]["url"] == url


@pytest.mark.parametrize("port,ok", [
    (0, True), (8765, True), (65535, True), (70000, False), (-1, False), ("abc", False), (True, False),
])
def test_validate_port(port, ok):
    payload = _valid(**{"server": {"port": port}})
    if ok:
        assert settings_store.normalize(payload)["server"]["port"] == port
    else:
        with pytest.raises(ValueError):
            settings_store.normalize(payload)


@pytest.mark.parametrize("mode,ok", [
    ("supervised", True), ("readonly", True), ("auto", True), ("", True), ("chaos", False),
])
def test_validate_mode(mode, ok):
    payload = _valid(**{"server": {"mode": mode}})
    if ok:
        assert settings_store.normalize(payload)["server"]["mode"] == mode
    else:
        with pytest.raises(ValueError):
            settings_store.normalize(payload)


@pytest.mark.parametrize("steps,ok", [(0, True), (1, True), (200, True), (201, False), (-1, False)])
def test_validate_max_steps(steps, ok):
    payload = _valid(**{"run": {"max_steps": steps}})
    if ok:
        assert settings_store.normalize(payload)["run"]["max_steps"] == steps
    else:
        with pytest.raises(ValueError):
            settings_store.normalize(payload)


@pytest.mark.parametrize("timeout,ok", [(0, True), (10, True), (7200, True), (5, False), (7201, False)])
def test_validate_timeout(timeout, ok):
    payload = _valid(**{"run": {"timeout_s": timeout}})
    if ok:
        assert settings_store.normalize(payload)["run"]["timeout_s"] == timeout
    else:
        with pytest.raises(ValueError):
            settings_store.normalize(payload)


@pytest.mark.parametrize("app,ok", [("", True), ("sample_media", True), ("sample-console.v2", True),
                                    ("bad app", False), ("a" * 65, False)])
def test_validate_app(app, ok):
    payload = _valid(**{"defaults": {"app": app}})
    if ok:
        assert settings_store.normalize(payload)["defaults"]["app"] == app
    else:
        with pytest.raises(ValueError):
            settings_store.normalize(payload)


def test_validate_rejects_non_object_payload():
    with pytest.raises(ValueError):
        settings_store.normalize(["x"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 生效优先级与界面视图
# ---------------------------------------------------------------------------

def test_effective_defaults_uses_file_when_env_absent(cfg):
    settings_store.save(_valid(), cfg)
    eff = settings_store.effective_defaults(cfg)
    assert eff["port"] == 8765
    assert eff["mode"] == "auto"
    assert eff["llm_url"].startswith("https://llm.example.com")
    assert eff["llm_api_key"] == "sk-test"


@pytest.mark.parametrize("env_name,key,expected", [
    ("UITA_DSH_PORT", "port", 0),
    ("UITA_DSH_MODE", "mode", ""),
    ("UITA_DSH_MAX_STEPS", "max_steps", 0),
    ("UITA_DSH_TIMEOUT_S", "timeout_s", 0),
    ("DSH_API_URL", "llm_url", ""),
    ("DSH_API_KEY", "llm_api_key", ""),
    ("UITA_DSH_MODEL", "llm_model", ""),
])
def test_effective_defaults_env_wins(cfg, monkeypatch, env_name, key, expected):
    settings_store.save(_valid(), cfg)
    monkeypatch.setenv(env_name, "9999" if isinstance(expected, int) else "x")
    assert settings_store.effective_defaults(cfg)[key] == expected


def test_public_view_masks_key(cfg):
    settings_store.save(_valid(), cfg)
    view = settings_store.public_view(cfg)
    dumped = json.dumps(view, ensure_ascii=False)
    assert "sk-test" not in dumped
    assert view["llm"]["api_key_set"] is True
    assert view["llm"]["api_key_mask"] == settings_store.MASK
    assert view["configured"] is True
    assert view["exists"] is True


def test_public_view_unconfigured_without_key(cfg):
    settings_store.save(_valid(**{"llm": {"api_key": ""}}), cfg)
    view = settings_store.public_view(cfg)
    assert view["configured"] is False
    assert view["llm"]["api_key_set"] is False
    assert view["llm"]["api_key_mask"] == ""


def test_public_view_sources(cfg, monkeypatch):
    view = settings_store.public_view(cfg)
    assert view["sources"]["llm.url"] == "default"
    settings_store.save(_valid(), cfg)
    view = settings_store.public_view(cfg)
    assert view["sources"]["llm.url"] == "file"
    monkeypatch.setenv("DSH_API_URL", "https://env.example.com/x")
    assert settings_store.public_view(cfg)["sources"]["llm.url"] == "env"


def test_public_view_reports_parse_error(cfg):
    with open(cfg, "w", encoding="utf-8") as handle:
        handle.write("{oops")
    assert settings_store.public_view(cfg)["error"]


# ---------------------------------------------------------------------------
# planner 兜底链：显式 > env > 文件 > 默认
# ---------------------------------------------------------------------------

def test_planner_url_reads_file(cfg):
    settings_store.save(_valid(), cfg)
    assert planner.resolve_llm_url() == "https://llm.example.com/v1/chat/completions"


def test_planner_url_env_wins(cfg, monkeypatch):
    settings_store.save(_valid(), cfg)
    monkeypatch.setenv("DSH_API_URL", "https://env.example.com/x")
    assert planner.resolve_llm_url() == "https://env.example.com/x"


def test_planner_key_reads_file(cfg):
    settings_store.save(_valid(), cfg)
    assert planner.resolve_api_key() == "sk-test"


def test_planner_key_env_wins(cfg, monkeypatch):
    settings_store.save(_valid(), cfg)
    monkeypatch.setenv("DSH_API_KEY", "sk-env")
    assert planner.resolve_api_key() == "sk-env"


def test_planner_key_explicit_wins(cfg, monkeypatch):
    settings_store.save(_valid(), cfg)
    monkeypatch.setenv("DSH_API_KEY", "sk-env")
    assert planner.resolve_api_key("sk-explicit") == "sk-explicit"


def test_planner_model_reads_file(cfg):
    settings_store.save(_valid(**{"llm": {"model": "file-model"}}), cfg)
    assert planner.resolve_model() == "file-model"


def test_planner_model_env_wins(cfg, monkeypatch):
    settings_store.save(_valid(**{"llm": {"model": "file-model"}}), cfg)
    monkeypatch.setenv("UITA_DSH_MODEL", "env-model")
    assert planner.resolve_model() == "env-model"


def test_planner_model_falls_back_to_config_default(cfg):
    from uita import config

    assert planner.resolve_model() == config.UITA_DSH_MODEL


# ---------------------------------------------------------------------------
# HTTP 接口：GET/POST /settings
# ---------------------------------------------------------------------------

from dsh_helpers import FakePlanner  # noqa: E402
from uita.tools.dsh.server import DshServer  # noqa: E402

TOKEN = "test-token"
HOST = "127.0.0.1:8765"


def _server(settings_file: str, port: int = 8765) -> DshServer:
    return DshServer(
        host="127.0.0.1", port=port, token=TOKEN, mode="auto",
        settings_file=settings_file,
        planner_factory=lambda: FakePlanner([
            {"tool": "task_finish", "args": {"status": "passed", "summary": "done"}},
        ]),
        auto_approve=True,
        store_root="",
    )


def _call(server: DshServer, method: str, path: str, payload=None, headers=None):
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    response = server.handle(
        method, path, body=body,
        headers=headers or {"Host": HOST, "X-Uita-Dsh-Token": TOKEN},
    )
    return response.status, json.loads(response.body.decode("utf-8"))


def test_get_settings_masks_key(cfg):
    settings_store.save(_valid(), cfg)
    server = _server(cfg)
    status, payload = _call(server, "GET", "/settings")
    assert status == 200
    assert "sk-test" not in json.dumps(payload)
    assert payload["settings"]["llm"]["api_key_set"] is True
    assert payload["settings"]["configured"] is True


def test_get_settings_requires_token(cfg):
    server = _server(cfg)
    status, _ = _call(server, "GET", "/settings", headers={"Host": HOST})
    assert status == 401


def test_post_settings_persists(cfg):
    server = _server(cfg)
    status, payload = _call(server, "POST", "/settings", _valid())
    assert status == 200, payload
    assert payload["restart_required"] is False
    assert settings_store.load(cfg)["defaults"]["app"] == "sample_media"
    status, view = _call(server, "GET", "/settings")
    assert view["settings"]["defaults"]["app"] == "sample_media"


def test_post_settings_invalid_does_not_write(cfg):
    server = _server(cfg)
    status, payload = _call(server, "POST", "/settings",
                            _valid(**{"server": {"port": 70000}}))
    assert status == 400
    assert "端口" in payload["error"]
    assert not os.path.exists(cfg)


def test_post_settings_keeps_existing_key(cfg):
    settings_store.save(_valid(), cfg)
    server = _server(cfg)
    body = _valid(**{"llm": {"api_key": ""}})
    status, _ = _call(server, "POST", "/settings", body)
    assert status == 200
    assert settings_store.load(cfg)["llm"]["api_key"] == "sk-test"


def test_post_settings_port_change_needs_restart(cfg):
    server = _server(cfg, port=8765)
    status, payload = _call(server, "POST", "/settings",
                            _valid(**{"server": {"port": 9911}}))
    assert status == 200
    assert payload["restart_required"] is True


def test_saved_defaults_apply_to_new_session(cfg):
    settings_store.save(_valid(**{"defaults": {"app": "sample_web"},
                                  "run": {"max_steps": 7}}), cfg)
    server = _server(cfg)
    status, payload = _call(server, "POST", "/sessions", {"goal": "看一下"})
    assert status == 200, payload
    session = payload["session"]
    assert session["app"] == "sample_web"
    assert session["max_steps"] == 7


def test_state_reports_configuration(cfg):
    server = _server(cfg)
    status, payload = _call(server, "GET", "/state")
    assert status == 200
    assert payload["configured"] is False
    assert payload["settings_path"] == cfg
