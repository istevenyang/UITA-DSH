"""MCP 外壳：把同一套工具注册表暴露给外部 Agent。

工具清单与 JSON Schema 直接取自 ``registry``（与 LLM tools、控制台调试台
同源，见 :mod:`uita.tools.dsh.registry`）。

传输：

- ``stdio``（默认、推荐）：本地 harness 直连，无网络面；
- ``streamable-http``：复用 ``mcp`` 的会话管理器 + ``Starlette``；需要
  ``uvicorn``（未在核心依赖内声明，缺失时给明确提示，不静默降级）。
"""
from __future__ import annotations

import json
from typing import Any

from .context import ToolContext
from .policy import Mode, PolicyGate
from .registry import get_tool, load_builtin_tools

SERVER_NAME = "uita-dsh"


def _tool_list() -> list[dict[str, Any]]:
    from .registry import build_mcp_tools

    return build_mcp_tools()


async def call_tool_async(dsh: Any, name: str, arguments: dict[str, Any] | None,
                          session_id: str = "") -> dict[str, Any]:
    """执行一次工具调用（MCP / 调试共用），返回结构化结果 dict。"""
    load_builtin_tools()
    tool = get_tool(name)
    if tool is None:
        return {"ok": False, "error": f"未知工具: {name}"}
    session = dsh.sessions.get(session_id) if session_id else None
    ctx = dsh.contexts.get(session_id) if session_id else None
    if ctx is None:
        ctx = ToolContext(session=session, app_name=getattr(session, "app_name", ""))
    gate = PolicyGate(
        getattr(session, "mode", None) or Mode.READONLY, allowlist=None,
    )
    decision = gate.check(tool)
    if not decision.allowed:
        reason = decision.reason
        if decision.needs_approval:
            reason = "该动作需要在 DSH 控制台人工批准（MCP 通道不代批）"
        return {"ok": False, "error": reason, "tool": tool.name}
    result = await tool.invoke(ctx, dict(arguments or {}))
    payload = result.to_dict()
    payload["tool"] = tool.name
    payload["session_id"] = session_id
    return payload


def build_server(dsh: Any) -> Any:
    """构造低层 MCP Server（schema 由注册表驱动）。"""
    from mcp.server.lowlevel import Server
    from mcp.types import TextContent, Tool

    server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[Any]:
        return [
            Tool(name=item["name"], description=item["description"],
                 inputSchema=item["inputSchema"])
            for item in _tool_list()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any] | None) -> list[Any]:
        session_id = ""
        if isinstance(arguments, dict) and isinstance(arguments.get("session_id"), str):
            session_id = arguments["session_id"]
            arguments = {k: v for k, v in arguments.items() if k != "session_id"}
        payload = await call_tool_async(dsh, name, arguments, session_id)
        return [TextContent(type="text", text=json.dumps(payload, ensure_ascii=False))]

    return server


async def serve_stdio(dsh: Any) -> None:
    """以 stdio 传输运行（阻塞直到 stdin 关闭）。"""
    from mcp.server.stdio import stdio_server

    server = build_server(dsh)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


async def serve_http(dsh: Any, host: str = "127.0.0.1", port: int = 8766) -> None:
    """以 streamable-http 传输运行（需 uvicorn）。"""
    try:
        import uvicorn  # noqa: PLC0415 - 可选依赖，缺失时给明确指引
        from starlette.applications import Starlette
        from starlette.routing import Route
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise RuntimeError(
            "MCP streamable-http 需要 uvicorn（可选依赖）："
            "`pip install uvicorn`；本地编排请优先用 `--dsh-transport stdio` "
            f"或 DSH 自带 HTTP 内核。原始错误：{exc}"
        ) from exc
    from contextlib import asynccontextmanager

    server = build_server(dsh)
    manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=True)

    async def endpoint(scope: Any, receive: Any, send: Any) -> None:
        await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(_app: Any):
        async with manager.run():
            yield

    app = Starlette(routes=[Route("/mcp", endpoint=endpoint)], lifespan=lifespan)
    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    await uvicorn.Server(config).serve()
