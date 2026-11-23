"""DSH 工具注册表：对外能力面的唯一来源。

一个工具在此声明一次，同时派生三处消费者：

- LLM tool-calling 的 ``tools=[...]``；
- MCP 外壳的 ``list_tools``；
- 控制页面「工具调试台」的可用工具清单。

三处共用同一 JSON Schema，不会漂移。

扩展方式（R2）：在 ``tools/`` 下新建模块，继承 :class:`DshTool` 并标注
``@register_dsh_tool``，不修改本文件的注册/校验逻辑。
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

# 工具分级：read 只读观察/定位/断言；action 操作被测应用；write 改环境
# （安装/探针注入/应用启停/用例执行）。
TIER_READ = "read"
TIER_ACTION = "action"
TIER_WRITE = "write"
TIERS = (TIER_READ, TIER_ACTION, TIER_WRITE)


class DshToolError(Exception):
    """工具注册/调用约定违规。"""


class DshToolAbort(Exception):
    """工具内部要求立即终止会话（不可继续的宿主环境问题）。"""


@dataclass(frozen=True)
class ArtifactRef:
    """一条证据引用（截图/日志/结果文件）。"""

    name: str
    path: str
    kind: str = "file"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "path": self.path, "kind": self.kind}


@dataclass
class ToolResult:
    """工具执行结果。

    ``ok=False`` 表示工具明确失败（含宿主环境不可用），**不抛异常**；
    断言类工具的三态判定放在 ``data["verdict"]``（true/false/unknown），
    ``ok`` 只表达"工具是否完成了一次判定"。
    """

    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    evidence: list[ArtifactRef] = field(default_factory=list)
    took_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "error": self.error,
            "evidence": [ref.to_dict() for ref in self.evidence],
            "took_ms": self.took_ms,
        }

    @classmethod
    def fail(cls, error: str, **data: Any) -> "ToolResult":
        return cls(ok=False, data=dict(data), error=error)


class DshTool(ABC):
    """DSH 工具基类。

    子类只需声明类属性并实现 :meth:`run`；参数一律关键字传入，未声明的
    关键字参数视为调用错误（由 :meth:`invoke` 统一拦截）。
    """

    name: str = ""
    tier: str = TIER_READ
    description: str = ""
    # JSON Schema properties（不含 type/required/additionalProperties）
    params: dict[str, Any] = {}
    required: tuple[str, ...] = ()
    timeout_s: float = 120.0

    def schema(self) -> dict[str, Any]:
        """返回通用 function-calling 形态的 schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": dict(self.params),
                    "required": list(self.required),
                    "additionalProperties": False,
                },
            },
        }

    def parameters(self) -> dict[str, Any]:
        """返回参数 JSON Schema 本体（MCP ``inputSchema`` 用）。"""
        return self.schema()["function"]["parameters"]

    def validate_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """校验并规整调用参数（只做结构性校验，不做业务校验）。"""
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise DshToolError(f"{self.name}: 参数必须是对象")
        unknown = set(args) - set(self.params)
        if unknown:
            raise DshToolError(
                f"{self.name}: 未知参数 {sorted(unknown)}（允许：{sorted(self.params)}）"
            )
        missing = [key for key in self.required if key not in args]
        if missing:
            raise DshToolError(f"{self.name}: 缺少必填参数 {missing}")
        return args

    async def invoke(self, ctx: Any, args: Optional[dict[str, Any]] = None, *,
                     timeout_s: Optional[float] = None) -> ToolResult:
        """带计时、参数校验与超时的统一调用入口（harness/MCP/调试台共用）。

        ``timeout_s is None`` 时使用工具自身的 ``timeout_s``（<=0 表示不限）。
        显式传入时以该值为硬上限；<=0 表示预算已耗尽，不启动工具。
        """
        started = time.monotonic()
        try:
            payload = self.validate_args(dict(args or {}))
            if timeout_s is None:
                limit = float(self.timeout_s or 0)
                limited = limit > 0
            else:
                limit = float(timeout_s)
                limited = True
            if limited and limit <= 0:
                result = ToolResult.fail(f"{self.name}: 执行超时（预算已耗尽）")
            elif limited:
                result = await self._run_with_timeout(ctx, payload, limit)
            else:
                result = await self.run(ctx, **payload)
            if not isinstance(result, ToolResult):
                result = ToolResult(ok=True, data={"value": result})
        except DshToolError as exc:
            result = ToolResult.fail(str(exc))
        except DshToolAbort:
            raise
        except Exception as exc:  # noqa: BLE001 - 工具失败必须可观测不可中断会话
            result = ToolResult.fail(f"{type(exc).__name__}: {exc}")
        result.took_ms = int((time.monotonic() - started) * 1000)
        return result

    async def _run_with_timeout(self, ctx: Any, payload: dict[str, Any],
                                limit: float) -> ToolResult:
        """在 ``limit`` 秒内执行 ``run``；超时取消任务并返回失败，不把它当成工具自己的异常。"""
        task = asyncio.ensure_future(self.run(ctx, **payload))
        try:
            done, _pending = await asyncio.wait({task}, timeout=limit)
        except asyncio.CancelledError:
            task.cancel()
            await _drain_task(task)
            raise
        if task not in done:
            task.cancel()
            await _drain_task(task)
            shown = f"{limit:.0f}s" if limit >= 10 else f"{limit:.2f}s"
            return ToolResult.fail(f"{self.name}: 执行超时（{shown}）")
        return task.result()

    @abstractmethod
    async def run(self, ctx: Any, **args: Any) -> ToolResult:
        """执行工具。宿主能力缺失时返回 ``ok=False``，不抛异常。"""


_REGISTRY: dict[str, DshTool] = {}
_BUILTIN_LOADED = False


def register_dsh_tool(cls: type[DshTool] | None = None):
    """类装饰器：注册一个工具实现（实例化一次并常驻）。"""

    def _register(target: type[DshTool]) -> type[DshTool]:
        instance = target()
        if not instance.name:
            raise DshToolError(f"{target.__name__}: 未声明工具名")
        if instance.tier not in TIERS:
            raise DshToolError(f"{instance.name}: 非法 tier={instance.tier!r}")
        if instance.name in _REGISTRY:
            raise DshToolError(f"{instance.name}: 工具重复注册")
        _REGISTRY[instance.name] = instance
        return target

    return _register(cls) if cls is not None else _register


def bound_timeout(tool: DshTool, session: Any = None) -> Optional[float]:
    """本次调用的超时上限。

    无会话时返回 ``None``（沿用工具自身 ``timeout_s``）。有会话时取工具上限与
    剩余时长的较小值；剩余时长耗尽时返回 ``0``，调用方应立即失败而不启动工具。
    """
    tool_limit = float(getattr(tool, "timeout_s", 0) or 0)
    budget = getattr(session, "budget_left", None)
    if not callable(budget):
        return None
    try:
        left = float(budget().get("seconds_left") or 0)
    except (TypeError, ValueError, AttributeError):
        return None if tool_limit <= 0 else tool_limit
    if tool_limit > 0:
        return max(0.0, min(tool_limit, left))
    return max(0.0, left)


async def _drain_task(task: "asyncio.Future[Any]") -> None:
    """取消后收掉任务，避免 'exception was never retrieved'。"""
    try:
        await task
    except (asyncio.CancelledError, Exception):
        return


def get_tool(name: str) -> DshTool | None:
    return _REGISTRY.get(name)


def all_tools() -> dict[str, DshTool]:
    return dict(_REGISTRY)


def tool_names() -> list[str]:
    return sorted(_REGISTRY)


def build_tool_specs() -> list[dict[str, Any]]:
    """LLM tool-calling 需要的 tools 列表。"""
    return [tool.schema() for _, tool in sorted(_REGISTRY.items())]


def build_mcp_tools() -> list[dict[str, Any]]:
    """MCP ``list_tools`` 的原始描述（不含 mcp 类型，便于单测）。"""
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.parameters(),
            "tier": tool.tier,
        }
        for _, tool in sorted(_REGISTRY.items())
    ]


def clear_registry() -> None:
    """清空注册表（仅测试使用）。"""
    global _BUILTIN_LOADED
    _REGISTRY.clear()
    _BUILTIN_LOADED = False


def load_builtin_tools() -> None:
    """导入内置工具模块以触发装饰器注册（幂等）。"""
    global _BUILTIN_LOADED
    if _BUILTIN_LOADED:
        return
    from . import tools as _tools  # noqa: F401 - 导入副作用即注册

    _tools.load_all()
    _BUILTIN_LOADED = True
