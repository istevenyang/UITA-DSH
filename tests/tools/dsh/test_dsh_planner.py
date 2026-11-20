"""DSH 规划器：HTTP 调用约定（Bearer/stream=false/模型回退）、双通道解析、凭据来源。"""
from __future__ import annotations

import json

import httpx
import pytest

from uita.tools.dsh import planner as planner_mod
from uita.tools.dsh.planner import (DshPlanner, HttpChatClient, PlannerError,
                                    plan_from_completion, resolve_api_key,
                                    resolve_llm_url)

URL = "https://llm.example.com/v1/chat/completions"


# ─────────────────────────────────────────────
# 响应解析
# ─────────────────────────────────────────────

def _tool_call(name: str, arguments: str) -> dict:
    return {"choices": [{"message": {
        "content": "先看看界面",
        "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
    }}]}


def test_parse_tool_call_channel():
    plan = plan_from_completion(_tool_call("observe_snapshot", '{"screenshot": true}'))
    assert plan.tool == "observe_snapshot"
    assert plan.args == {"screenshot": True}
    assert plan.thought == "先看看界面"


def test_parse_tool_call_dict_arguments():
    payload = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "locate", "arguments": {"target": "设置"}}}
    ]}}]}
    assert plan_from_completion(payload).args == {"target": "设置"}


def test_parse_invalid_arguments_json():
    with pytest.raises(PlannerError):
        plan_from_completion(_tool_call("locate", "{not json"))


def test_parse_tool_call_without_name():
    payload = {"choices": [{"message": {"tool_calls": [{"function": {"arguments": "{}"}}]}}]}
    with pytest.raises(PlannerError):
        plan_from_completion(payload)


def test_parse_json_text_fallback_with_fence():
    content = """先观察。
```json
{"thought": "先观察", "tool": "observe_snapshot", "args": {}, "expect": "看到首页"}
```"""
    plan = plan_from_completion({"choices": [{"message": {"content": content}}]})
    assert plan.tool == "observe_snapshot"
    assert plan.expect == "看到首页"


def test_parse_json_text_with_noise():
    content = '前缀 {"tool": "locate", "args": {"target": "设置"}} 后缀'
    assert plan_from_completion(
        {"choices": [{"message": {"content": content}}]}).tool == "locate"


def test_parse_text_without_json():
    with pytest.raises(PlannerError):
        plan_from_completion({"choices": [{"message": {"content": "我拒绝输出 JSON"}}]})


def test_parse_empty_response():
    with pytest.raises(PlannerError):
        plan_from_completion({"choices": [{"message": {"content": ""}}]})


def test_parse_text_missing_tool_field():
    with pytest.raises(PlannerError):
        plan_from_completion({"choices": [{"message": {"content": '{"args": {}}'}}]})


def test_parse_text_args_not_object():
    with pytest.raises(PlannerError):
        plan_from_completion(
            {"choices": [{"message": {"content": '{"tool": "x", "args": [1]}'}}]})


def test_parse_non_dict_payload():
    with pytest.raises(PlannerError):
        plan_from_completion(["nope"])


# ─────────────────────────────────────────────
# 凭据 / 端点解析
# ─────────────────────────────────────────────

@pytest.fixture
def clean_env(monkeypatch):
    """清空所有密钥候选环境变量，避免读取真实 .env.local 值。"""
    from uita import config

    for name in (
        *config.UITA_DSH_API_KEY_ENVS,
        "API_KEY",
        "LEGACY_PROXY_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(config, name, "", raising=False)
    for name in config.UITA_DSH_LLM_URL_ENVS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(config, name, "", raising=False)
    monkeypatch.setattr(config, "UITA_DSH_LLM_URL", "", raising=False)
    return monkeypatch


def test_resolve_api_key_priority(clean_env):
    clean_env.setenv("UITA_DSH_API_KEY", "k-dsh")
    assert resolve_api_key() == "k-dsh"
    clean_env.setenv("DSH_API_KEY", "k-dsh-primary")
    assert resolve_api_key() == "k-dsh-primary"
    assert resolve_api_key("k-explicit") == "k-explicit"


def test_resolve_api_key_empty(clean_env):
    assert resolve_api_key() == ""


def test_resolve_api_key_ignores_unrelated_env(clean_env):
    clean_env.setenv("LEGACY_PROXY_TOKEN", "legacy-key")
    clean_env.setenv("UNRELATED_LLM_API_KEY", "other-key")
    assert resolve_api_key() == ""


def test_resolve_llm_url(clean_env):
    assert resolve_llm_url() == ""
    clean_env.setenv("UITA_DSH_LLM_URL", "https://legacy.example.com/v1/chat/completions")
    assert resolve_llm_url() == "https://legacy.example.com/v1/chat/completions"
    clean_env.setenv("DSH_API_URL", URL)
    assert resolve_llm_url() == URL
    assert resolve_llm_url("http://x") == "http://x"


def test_resolve_llm_url_from_config(clean_env):
    from uita import config

    clean_env.setattr(config, "UITA_DSH_LLM_URL", URL)
    assert resolve_llm_url() == URL


# ─────────────────────────────────────────────
# HTTP 客户端
# ─────────────────────────────────────────────

def test_config_requires_url(clean_env):
    clean_env.setenv("DSH_API_KEY", "k")
    with pytest.raises(PlannerError) as excinfo:
        HttpChatClient().config()
    assert "DSH_API_URL" in str(excinfo.value)


def test_config_requires_api_key(clean_env):
    with pytest.raises(PlannerError) as excinfo:
        HttpChatClient(url=URL).config()
    assert "DSH_API_KEY" in str(excinfo.value)


def test_build_body_matches_curl_shape(clean_env):
    client = HttpChatClient(url=URL, api_key="k", native_tools=False)
    body = client.build_body(
        messages=[{"role": "user", "content": "hi"}], model="reasoning-model",
        tools=[{"type": "function", "function": {"name": "x"}}],
    )
    assert body == {"model": "reasoning-model", "messages": [{"role": "user", "content": "hi"}],
                    "stream": False}


def test_build_body_includes_tools_when_native(clean_env):
    client = HttpChatClient(url=URL, api_key="k", native_tools=True)
    body = client.build_body(messages=[], model="reasoning-model",
                             tools=[{"type": "function", "function": {"name": "x"}}])
    assert body["tools"][0]["function"]["name"] == "x"


async def test_complete_sends_bearer_and_json(clean_env):
    seen: list[tuple[str, dict, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), dict(request.headers), json.loads(request.content)))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    client = HttpChatClient(url=URL, api_key="secret-key",
                            transport=httpx.MockTransport(handler))
    payload = await client.complete(messages=[{"role": "user", "content": "hi"}],
                                    model="reasoning-model")
    assert payload["choices"][0]["message"]["content"] == "{}"
    url, headers, body = seen[0]
    assert url == URL
    assert headers["authorization"] == "Bearer secret-key"
    assert headers["content-type"] == "application/json"
    assert body == {"model": "reasoning-model", "messages": [{"role": "user", "content": "hi"}],
                    "stream": False}


async def test_complete_falls_back_across_models(clean_env):
    models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        models.append(model)
        if model == "reasoning-model":
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = HttpChatClient(url=URL, api_key="k", model="reasoning-model",
                            fallbacks=("reasoning-model", "fallback-model", "default-model"),
                            transport=httpx.MockTransport(handler))
    payload = await client.complete(messages=[], model="reasoning-model")
    assert models == ["reasoning-model", "fallback-model"]
    assert payload["choices"][0]["message"]["content"] == "ok"


async def test_complete_all_models_fail(clean_env):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = HttpChatClient(url=URL, api_key="k", model="reasoning-model",
                            fallbacks=("fallback-model",), transport=httpx.MockTransport(handler))
    with pytest.raises(PlannerError) as excinfo:
        await client.complete(messages=[], model="reasoning-model")
    message = str(excinfo.value)
    assert "reasoning-model" in message and "fallback-model" in message


async def test_complete_falls_back_on_non_json(clean_env):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "reasoning-model":
            return httpx.Response(200, text="<html>not json</html>")
        return httpx.Response(200, json={"choices": []})

    client = HttpChatClient(url=URL, api_key="k", model="reasoning-model",
                            fallbacks=("fallback-model",), transport=httpx.MockTransport(handler))
    payload = await client.complete(messages=[], model="reasoning-model")
    assert calls == ["reasoning-model", "fallback-model"]
    assert payload == {"choices": []}


async def test_complete_retries_transient_connect_failure(clean_env):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise httpx.ConnectError("[Errno 11001] getaddrinfo failed")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = HttpChatClient(url=URL, api_key="k", model="default-model", fallbacks=(),
                            backoff_s=0, transport=httpx.MockTransport(handler))
    payload = await client.complete(messages=[{"role": "user", "content": "hi"}])
    assert payload["choices"][0]["message"]["content"] == "ok"
    assert len(calls) == 2


async def test_complete_gives_up_after_connect_retries(clean_env):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(calls) + 1)
        raise httpx.ConnectError("[Errno 11001] getaddrinfo failed")

    client = HttpChatClient(url=URL, api_key="k", model="default-model", fallbacks=(),
                            backoff_s=0, connect_retries=2,
                            transport=httpx.MockTransport(handler))
    with pytest.raises(PlannerError) as excinfo:
        await client.complete(messages=[])
    assert len(calls) == 3
    assert "getaddrinfo" in str(excinfo.value)


async def test_http_error_is_not_retried(clean_env):
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(calls) + 1)
        return httpx.Response(400, text="bad request")

    client = HttpChatClient(url=URL, api_key="k", model="default-model", fallbacks=(),
                            backoff_s=0, transport=httpx.MockTransport(handler))
    with pytest.raises(PlannerError):
        await client.complete(messages=[])
    assert len(calls) == 1


def test_default_model_is_default_model(clean_env):
    from uita import config

    clean_env.setattr(config, "UITA_DSH_MODEL", "default-model")
    clean_env.setattr(config, "UITA_DSH_MODEL_FALLBACKS", ("default-model", "fallback-model", "reasoning-model"))
    client = HttpChatClient(url=URL, api_key="k")
    assert client._models("") == ["default-model", "fallback-model", "reasoning-model"]


def test_models_dedup_and_primary_first(clean_env):
    client = HttpChatClient(url=URL, api_key="k", model="default-model",
                            fallbacks=("reasoning-model", "default-model", "fallback-model"))
    assert client._models("") == ["default-model", "reasoning-model", "fallback-model"]


# ─────────────────────────────────────────────
# 规划器输入构造
# ─────────────────────────────────────────────

def test_build_messages_single_user_message(make_session):
    session = make_session(goal="点开隐私保护")
    session.add_hint("先关弹窗")
    planner = DshPlanner(client=object(), model="m")
    messages = planner.build_messages(session, {"window_title": "首页"},
                                      ["observe_snapshot", "task_finish"])
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    body = messages[0]["content"]
    assert "点开隐私保护" in body
    assert "先关弹窗" in body
    assert "首页" in body
    assert "observe_snapshot" in body
    assert "只输出 JSON" in body


async def test_plan_uses_client_and_model(make_session):
    class _Client:
        def __init__(self):
            self.seen = None

        async def complete(self, *, messages, model, tools=None):
            self.seen = {"messages": messages, "model": model, "tools": tools}
            return _tool_call("task_finish", '{"status": "failed", "summary": "跑不动"}')

    client = _Client()
    session = make_session()
    planner = DshPlanner(client=client, model="fallback-model")
    plan = await planner.plan(session=session, observation={},
                              tools=[{"type": "function", "function": {"name": "x"}}])
    assert plan.tool == "task_finish"
    assert client.seen["model"] == "fallback-model"
    assert client.seen["tools"][0]["function"]["name"] == "x"


async def test_text_channel_exposes_only_available_tool_parameter_schemas(make_session):
    from uita.tools.dsh.tools.assertion import AssertExistTool

    class Client:
        async def complete(self, *, messages, model, tools):
            content = messages[0]["content"]
            marker = "当前上下文（JSON）：\n"
            payload = json.loads(content.split(marker, 1)[1].split("\n\n只输出 JSON 计划。", 1)[0])
            schemas = payload["tool_schemas"]
            assert len(schemas) == 1
            params = schemas[0]["function"]["parameters"]
            assert params["required"] == ["target"]
            assert "name" not in params["properties"]
            assert params["additionalProperties"] is False
            return _tool_call("assert_exist", '{"target":"CPU占用文字"}')

    planner = DshPlanner(client=Client())
    plan = await planner.plan(session=make_session(), tools=[AssertExistTool().schema()],
                              tool_names=["assert_exist"])
    assert plan.args == {"target": "CPU占用文字"}


def test_available_actions_from_registry():
    # 不预先 import 任何 action 模块：清单必须自己触发注册，否则真机上恒为空
    actions = planner_mod._available_actions()
    assert isinstance(actions, list)
    assert "assert" in actions
    assert "click" in actions
    # 不得把 app scope（全局为 ""）当成动作名返回
    assert all(actions)


def test_available_actions_self_loads_registry(monkeypatch):
    """回归守卫：注册表未加载时，_available_actions 必须主动触发导入。"""
    import uita.core.actions.registry as action_registry

    monkeypatch.setattr(planner_mod, "_ACTIONS_LOADED", False)
    monkeypatch.setattr(action_registry, "_REGISTRY", {})
    calls = []

    def _fake_ensure():
        calls.append(1)
        action_registry._REGISTRY[("", "assert")] = object()

    monkeypatch.setattr(planner_mod, "_ensure_actions_registered", _fake_ensure)
    assert planner_mod._available_actions() == ["assert"]
    assert calls == [1]


def test_available_actions_ignores_scope_and_returns_names(monkeypatch):
    class _Dummy:
        pass

    fake = {("", "click"): _Dummy, ("sample_web", "jump"): _Dummy}
    monkeypatch.setattr("uita.core.actions.registry.all_actions", lambda: fake)
    assert planner_mod._available_actions() == ["click", "jump"]


def test_available_actions_empty_registry(monkeypatch):
    monkeypatch.setattr("uita.core.actions.registry.all_actions", lambda: {})
    assert planner_mod._available_actions() == []


def test_available_actions_swallows_registry_error(monkeypatch):
    def _boom():
        raise RuntimeError("registry broken")

    monkeypatch.setattr("uita.core.actions.registry.all_actions", _boom)
    assert planner_mod._available_actions() == []
