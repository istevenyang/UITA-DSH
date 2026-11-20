"""DSH 规划器：一次 HTTP chat/completions 调用产出一条计划。

调用约定（与标准通用 chat/completions curl 等价，`stream=false`）::

    curl --location '${DSH_API_URL}' \\
         --header 'Authorization: Bearer <api_key>' \\
         --header 'Content-Type: application/json' \\
         --data '{"model": "default-model", "messages": [{"role": "user", "content": "..."}],
                  "stream": false}'

- **URL / 密钥只从环境变量读**（R5）：端点 `DSH_API_URL`（兼容旧名
  `UITA_DSH_LLM_URL`）；密钥按 `DSH_API_KEY → UITA_DSH_API_KEY` 顺序取第一个
  非空值。
- **模型回退**：首选 `UITA_DSH_MODEL`（默认 `default-model`），失败后依次试
  `UITA_DSH_MODEL_FALLBACKS`（默认 `default-model,fallback-model`）。
- **计划通道**：该端点默认不下发原生 tool-calling，模型只回文本 JSON
  `{"thought","tool","args","expect"}`；若端点支持（置
  `UITA_DSH_LLM_NATIVE_TOOLS=1`）则同时下发 `tools`，`tool_calls` 通道优先。
- 两条通道共用同一安全闸与预算，模型能力差异不改变安全边界。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

# 连接类失败（DNS/建连/读写超时）在真实内网端点上是间歇性的，单次失败
# 不应直接终结整个会话，因此默认对同一模型做一次退避重试；HTTP 4xx/5xx
# 与响应解析失败不重试（重试同一确定性错误没有意义）。
DEFAULT_CONNECT_RETRIES = 2
CONNECT_RETRY_BACKOFF_S = 1.0

SYSTEM_PROMPT = """你是 UITA 的顶层 AI 编排器（DSH，Deterministic Skill Harness）。你既可以用内置工具直接操作/断言真实应用，也可以读写工作区文件、加载 Skill，并通过 uita_capabilities / uita_invoke 编排 UITA 全部确定性子命令（parse / lint / health-check / runner / batch / perf / install / pretest / probe / probe-diff 等）。

输出格式（硬要求）：只输出一个 JSON 对象，不要任何解释文字、不要 Markdown 代码块：
{"thought": "一句话推理", "tool": "工具名", "args": {...}, "expect": "期望观察到的结果"}

工具纪律：
1. 先观察（observe_snapshot）再行动；不确定元素时先 locate，禁止臆造元素名。
2. 定位优先用元素语义名（rosetta_map 中的名字）；只有元素名缺失或定位失败时才用坐标，
   且必须在 args 里写 source 说明来源。
3. 任何操作后都要用 assert_* 验证；断言三态 true / false / unknown，
   unknown 表示"判定不了"，不是通过。
4. 断言 false 时先重定位重试一次；仍失败则如实报告，不要反复空转。
5. app_prepare 是唯一的启动链路；不要假设应用已在运行。
6. 需要调用 UITA 子命令（如校验 TCS、批量跑用例、跑性能采集、探查元素、生成测试设计）时，
   不确定参数先调 uita_capabilities 查看 key_args，再用 uita_invoke 执行。
7. 给了 test_case 时按步骤号顺序执行：每步做完用 report_step 上报
   （step=步骤号，status=passed/failed/unknown/skipped，detail 写依据），再去下一步。
   含预期/检查语义的步骤必须先有 assert_* 的 true 证据；纯打开/点击等导航操作步可在动作成功后直接 report_step(passed)，但整条用例结束前必须至少完成一次 assert_* 的 true 验证。
8. 目标达成或确认无法达成时，调用 task_finish：status 取 passed / failed，
   summary 一句话写清结论（失败要写清卡在哪一步、看到什么）。
9. 一次只给一个工具调用。
"""

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


class PlannerError(Exception):
    """规划输出不可用（非法 JSON / 缺工具名 / 凭据缺失 / 端点不可用）。"""


@dataclass
class Plan:
    """单步计划。"""

    thought: str = ""
    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    expect: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "thought": self.thought,
            "tool": self.tool,
            "args": dict(self.args),
            "expect": self.expect,
        }


# ─────────────────────────────────────────────
# 凭据与端点解析（全部来自环境变量）
# ─────────────────────────────────────────────

def _settings_values() -> dict[str, str]:
    """控制台「设置」写入的配置文件值（缺失/损坏时全空，不抛异常）。"""
    try:
        from . import settings_store

        return settings_store.resolve_llm()
    except Exception:  # noqa: BLE001 - 配置读取失败一律按未配置处理
        return {"url": "", "api_key": "", "model": ""}


def resolve_api_key(explicit: str = "") -> str:
    """按优先级取第一个非空密钥；都没有时返回空串。

    优先级：显式参数 → 环境变量（DSH_API_KEY / UITA_DSH_API_KEY）→
    配置文件（界面设置）→ ``config.py`` 同名项。严格隔离凭据来源。
    """
    if explicit.strip():
        return explicit.strip()
    from uita import config

    candidates = list(getattr(config, "UITA_DSH_API_KEY_ENVS", ("DSH_API_KEY", "UITA_DSH_API_KEY")) or ())
    for name in candidates:
        value = (os.environ.get(name, "") or "").strip()
        if value:
            return value
    settings_key = str(_settings_values().get("api_key") or "").strip()
    if settings_key:
        return settings_key
    for name in candidates:
        value = str(getattr(config, name, "") or "").strip()
        if value:
            return value
    return ""


def resolve_llm_url(explicit: str = "") -> str:
    """端点优先级：显式参数 → `DSH_API_URL` → `UITA_DSH_LLM_URL` → 配置文件 → config。"""
    if explicit.strip():
        return explicit.strip()
    from uita import config

    for name in getattr(config, "UITA_DSH_LLM_URL_ENVS", ("DSH_API_URL", "UITA_DSH_LLM_URL")):
        value = (os.environ.get(name, "") or "").strip()
        if value:
            return value
    settings_url = str(_settings_values().get("url") or "").strip()
    if settings_url:
        return settings_url
    cfg_url = str(getattr(config, "UITA_DSH_LLM_URL", "") or "").strip()
    if cfg_url:
        return cfg_url
    return ""


def resolve_model(explicit: str = "") -> str:
    """模型优先级：显式参数 → `UITA_DSH_MODEL` → 配置文件 → `config.py` 默认值。"""
    if explicit.strip():
        return explicit.strip()
    value = (os.environ.get("UITA_DSH_MODEL", "") or "").strip()
    if value:
        return value
    settings_model = str(_settings_values().get("model") or "").strip()
    if settings_model:
        return settings_model
    from uita import config

    return str(getattr(config, "UITA_DSH_MODEL", "") or "").strip()


# ─────────────────────────────────────────────
# 响应解析（tool_calls 通道 与 JSON 文本通道）
# ─────────────────────────────────────────────

def _extract_json_object(text: str) -> dict[str, Any]:
    blocks = _JSON_BLOCK_RE.findall(text or "")
    candidates = list(blocks)
    stripped = (text or "").strip()
    candidates.append(stripped)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start >= 0 and end > start:
        candidates.append(stripped[start:end + 1])
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise PlannerError("模型输出中找不到合法 JSON 计划")


def plan_from_completion(payload: dict[str, Any]) -> Plan:
    """把一次模型响应解析为 :class:`Plan`（两种通道统一出口）。"""
    if not isinstance(payload, dict):
        raise PlannerError(f"响应不是对象: {type(payload).__name__}")

    message: dict[str, Any] = {}
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message") or {}
        if not isinstance(message, dict):
            message = {}

    tool_calls = message.get("tool_calls") or []
    if isinstance(tool_calls, list) and tool_calls:
        call = tool_calls[0] if isinstance(tool_calls[0], dict) else {}
        function = call.get("function") or {}
        name = str(function.get("name") or "").strip()
        raw_args = function.get("arguments")
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
            except json.JSONDecodeError as exc:
                raise PlannerError(f"工具参数不是合法 JSON: {exc}") from exc
        elif isinstance(raw_args, dict):
            args = dict(raw_args)
        else:
            args = {}
        if not name:
            raise PlannerError("tool_calls 缺少函数名")
        if not isinstance(args, dict):
            raise PlannerError("工具参数必须是对象")
        return Plan(thought=str(message.get("content") or ""), tool=name,
                    args=args, raw=payload)

    content = message.get("content") or payload.get("content") or ""
    if not isinstance(content, str) or not content.strip():
        raise PlannerError("模型既未给出工具调用也未给出计划文本")
    plan = _extract_json_object(content)
    name = str(plan.get("tool") or "").strip()
    args = plan.get("args") or {}
    if not name:
        raise PlannerError("计划缺少 tool 字段")
    if not isinstance(args, dict):
        raise PlannerError("计划 args 必须是对象")
    return Plan(
        thought=str(plan.get("thought") or ""),
        tool=name,
        args=args,
        expect=str(plan.get("expect") or ""),
        raw=plan,
    )


# ─────────────────────────────────────────────
# HTTP 客户端
# ─────────────────────────────────────────────

class HttpChatClient:
    """chat/completions HTTP 客户端（httpx，惰性导入，可注入 transport 做单测）。"""

    def __init__(self, *, url: str = "", api_key: str = "", model: str = "",
                 fallbacks: Optional[tuple[str, ...]] = None,
                 timeout_s: float = 0.0, native_tools: Optional[bool] = None,
                 transport: Any = None, connect_retries: Optional[int] = None,
                 backoff_s: float = CONNECT_RETRY_BACKOFF_S) -> None:
        self._url = url
        self._api_key = api_key
        self._model = model
        self._fallbacks = fallbacks
        self._timeout_s = timeout_s
        self._native_tools = native_tools
        self._transport = transport
        self._connect_retries = (DEFAULT_CONNECT_RETRIES if connect_retries is None
                                 else max(0, int(connect_retries)))
        self._backoff_s = max(0.0, float(backoff_s))

    # -- 配置读取 ------------------------------------------------------

    def config(self) -> tuple[str, str, float]:
        from uita import config

        url = resolve_llm_url(self._url)
        if not url:
            raise PlannerError(
                "缺少模型端点：请设置环境变量 DSH_API_URL"
                "（chat/completions 完整地址，取自 .env.local 或系统环境变量）"
            )
        api_key = resolve_api_key(self._api_key)
        if not api_key:
            raise PlannerError(
                "缺少模型凭据：请在 .env.local / 系统环境变量中提供 "
                "DSH_API_KEY（兼容 UITA_DSH_API_KEY；R5：密钥只走 env）"
            )
        timeout = float(self._timeout_s or getattr(config, "UITA_DSH_LLM_TIMEOUT_S", 120))
        return url, api_key, timeout

    def _models(self, model: str) -> list[str]:
        from uita import config

        primary = (model or resolve_model(self._model)
                   or getattr(config, "UITA_DSH_MODEL", "default-model") or "default-model").strip()
        fallbacks = self._fallbacks
        if fallbacks is None:
            fallbacks = tuple(getattr(config, "UITA_DSH_MODEL_FALLBACKS", ()) or ())
        ordered: list[str] = []
        for item in [primary, *fallbacks]:
            name = str(item or "").strip()
            if name and name not in ordered:
                ordered.append(name)
        return ordered or ["default-model"]

    def _use_native_tools(self) -> bool:
        from uita import config

        if self._native_tools is not None:
            return bool(self._native_tools)
        return bool(getattr(config, "UITA_DSH_LLM_NATIVE_TOOLS", False))

    # -- 请求 ----------------------------------------------------------

    def build_body(self, *, messages: list[dict[str, Any]], model: str,
                   tools: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "stream": False,
        }
        if tools and self._use_native_tools():
            body["tools"] = list(tools)
        return body

    async def complete(self, *, messages: list[dict[str, Any]], model: str = "",
                       tools: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        url, api_key, timeout = self.config()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - 依赖缺失属打包问题
            raise PlannerError(f"httpx 不可用: {exc}") from exc

        errors: list[str] = []
        models = self._models(model)
        for name in models:
            body = self.build_body(messages=messages, model=name, tools=tools)
            attempt = 0
            while True:
                attempt += 1
                try:
                    client_kwargs: dict[str, Any] = {"timeout": timeout}
                    if self._transport is not None:
                        client_kwargs["transport"] = self._transport
                    async with httpx.AsyncClient(**client_kwargs) as client:
                        response = await client.post(url, headers=headers, json=body)
                    if response.status_code >= 400:
                        errors.append(f"{name}: HTTP {response.status_code} {response.text[:200]}")
                        break
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        errors.append(f"{name}: 响应不是 JSON（{exc}）")
                        break
                    if not isinstance(payload, dict):
                        errors.append(f"{name}: 响应不是对象")
                        break
                    return payload
                except httpx.TransportError as exc:
                    # 连接/DNS/超时类：可重试，退避后重试同一模型
                    errors.append(f"{name}: {type(exc).__name__}: {exc}")
                    if attempt > self._connect_retries:
                        break
                    if self._backoff_s:
                        await asyncio.sleep(self._backoff_s * attempt)
                except Exception as exc:  # noqa: BLE001 - 逐个模型回退，全部失败才报错
                    errors.append(f"{name}: {type(exc).__name__}: {exc}")
                    break
        detail = "；".join(errors[-4:]) or "未知原因"
        raise PlannerError(f"模型调用失败（已试 {models}）：{detail}")


# ─────────────────────────────────────────────
# 规划器
# ─────────────────────────────────────────────

def _compact_events(events: list[dict[str, Any]], limit: int) -> list[str]:
    lines: list[str] = []
    for event in events[-limit:]:
        kind = event.get("kind", "?")
        detail = {k: v for k, v in event.items() if k not in {"seq", "ts", "kind"}}
        text = json.dumps(detail, ensure_ascii=False)
        if len(text) > 400:
            text = text[:400] + "…"
        lines.append(f"#{event.get('seq')} {kind} {text}")
    return lines


class DshPlanner:
    """构造模型输入并解析计划。"""

    def __init__(self, client: Optional[Any] = None, model: str = "",
                 system_prompt: str = SYSTEM_PROMPT,
                 history_limit: int = 20) -> None:
        self.client = client or HttpChatClient()
        self.model = model
        self.system_prompt = system_prompt
        self.history_limit = history_limit

    def build_messages(self, session: Any, observation: Optional[dict[str, Any]],
                       tool_names: list[str],
                       tool_schemas: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
        """单条 user 消息（含纪律 + 上下文），与该端点的最小调用形态对齐。"""
        payload = {
            "goal": getattr(session, "goal", ""),
            "app": getattr(session, "app_name", ""),
            "mode": getattr(getattr(session, "mode", None), "value", "supervised"),
            "budget": session.budget_left() if hasattr(session, "budget_left") else {},
            "available_tools": tool_names,
            "tool_schemas": [spec for spec in (tool_schemas or [])
                             if isinstance(spec, dict) and
                             (spec.get("function") or {}).get("name") in tool_names],
            "available_actions": _available_actions(),
            "test_case": _case_payload(session),
            "recent_events": _compact_events(
                list(getattr(session, "events", []) or []), self.history_limit,
            ),
            "last_observation": observation or {},
            "hints": list(getattr(session, "_hints", []) or [])[-8:],
        }
        content = (
            self.system_prompt
            + "\n\n当前上下文（JSON）：\n"
            + json.dumps(payload, ensure_ascii=False)
            + "\n\n只输出 JSON 计划。"
        )
        return [{"role": "user", "content": content}]

    async def plan(self, *, session: Any, observation: Optional[dict[str, Any]] = None,
                   tools: Optional[list[dict[str, Any]]] = None,
                   tool_names: Optional[list[str]] = None) -> Plan:
        if tool_names is not None:
            names = list(tool_names)
        else:
            names = [
                str((spec.get("function") or {}).get("name") or "")
                for spec in (tools or []) if isinstance(spec, dict)
            ]
            names = [name for name in names if name]
        messages = self.build_messages(session, observation, names, list(tools or []))
        payload = await self.client.complete(
            messages=messages, model=self.model, tools=list(tools or []),
        )
        if isinstance(payload, dict) and isinstance(payload.get("usage"), dict):
            record_fn = getattr(session, "record_usage", None)
            if callable(record_fn):
                record_fn(payload["usage"])
        return plan_from_completion(payload)


def _case_payload(session: Any) -> Optional[dict[str, Any]]:
    """文本用例上下文：步骤清单 + 当前待执行步骤 + 进度。"""
    case = getattr(session, "case", None)
    if case is None:
        return None
    current = case.next_pending()
    return {
        "name": case.name,
        "parsed": case.parsed,
        "preconditions": list(case.preconditions),
        "progress": case.summary(),
        "current_step": current.to_dict() if current is not None else None,
        "steps": [
            {"no": step.no, "action": step.action, "expected": step.expected,
             "status": step.status, "detail": step.detail}
            for step in case.steps
        ],
    }


_ACTIONS_LOADED = False


def _ensure_actions_registered() -> None:
    """确保动作模块已导入（幂等）。

    Action 只在模块导入时通过 ``@register_action`` 落到全局 registry，而
    ``uita.core.interpreter`` 是仓库里唯一集中导入全部 Action 模块的入口
    （``core/interpreter.py`` 顶部那段 ``from uita.core.actions.common import ...``）。
    不主动触发导入时 registry 是空的，动作清单恒为 ``[]``，模型无从得知应用侧
    能做什么——因此提示词构造前必须先导入一次，避免"清单永远为空"的静默降级。
    """
    global _ACTIONS_LOADED
    if _ACTIONS_LOADED:
        return
    import uita.core.interpreter  # noqa: F401 - 导入副作用即注册全部 Action
    _ACTIONS_LOADED = True


def _available_actions() -> list[str]:
    """可用动作清单（从已注册 action 派生，供模型了解应用侧能做什么）。"""
    try:
        from uita.core.actions.registry import all_actions

        _ensure_actions_registered()
        # all_actions() 的键是 (app_scope, action_name)：app_scope 为 "" 表示全局，
        # 故取键的第二个元素才是动作关键字。
        return sorted({name for _app, name in all_actions()})
    except Exception:  # noqa: BLE001 - 动作清单只是提示信息，缺失不影响编排
        return []
