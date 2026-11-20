"""--mode dsh-serve / dsh-run / dsh-mcp：DSH 编排层的三个入口。

薄适配层：只做参数校验、运行方式选择与退出码映射，业务实现全部在
``uita.tools.dsh``（惰性导入，避免拖慢 `uita --help`）。

退出码：
- ``0`` 任务完整跑完（``dsh-serve`` 正常退出；``dsh-run`` 结论 passed）
- ``1`` 未能正常启动/结论非 passed
- ``2`` CLI 参数错误
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger("uita")


class _StdoutMux:
    """把 ``print``/日志导向 stderr，仅把 ``.buffer`` 留给 MCP JSON-RPC。"""

    encoding = "utf-8"

    def __init__(self, real_fd: int) -> None:
        self._real = os.fdopen(real_fd, "wb", buffering=0)
        self.buffer = self._real

    def write(self, text):  # noqa: D401 - 与 file 协议对齐
        return sys.stderr.write(text)

    def writelines(self, lines):
        return sys.stderr.writelines(lines)

    def flush(self):
        return sys.stderr.flush()

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return sys.stderr.fileno()

    def close(self) -> None:  # pragma: no cover - 进程退出时由 OS 回收
        pass


def install_stdio_protocol_stream() -> None:
    """让 stdio 传输独占 stdout。

    UITA 启动链会向 stdout 打印加载信息、日志也会落到原 stdout 对象；MCP stdio
    传输把 stdout 当作 JSON-RPC 管道，任何杂音都会让客户端解析失败。做法：

    1. 复制原始 stdout fd 留作协议通道；
    2. 把 fd 1 重定向到 stderr（既有日志 handler 持旧对象，写入即落 stderr）；
    3. ``sys.stdout`` 换成 mux：``print`` 走 stderr，``.buffer`` 走协议通道。
    """
    real_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = _StdoutMux(real_fd)  # type: ignore[assignment]


def _make_server(cli):
    from uita.tools.dsh.server import DshServer

    return DshServer(
        host=getattr(cli, "dsh_host", "") or "",
        port=int(getattr(cli, "dsh_port", 0) or 0),
        token=getattr(cli, "dsh_token", "") or "",
        mode=getattr(cli, "dsh_mode", "") or "",
        max_steps=int(getattr(cli, "dsh_max_steps", 0) or 0),
        timeout_s=int(getattr(cli, "dsh_timeout", 0) or 0),
        model=getattr(cli, "dsh_model", "") or "",
        llm_url=getattr(cli, "dsh_llm_url", "") or "",
    )


def _make_planner(cli):
    """按 CLI 覆盖项构造规划器；URL/密钥缺省仍从环境变量读。"""
    from uita.tools.dsh.planner import DshPlanner, HttpChatClient

    client = HttpChatClient(url=getattr(cli, "dsh_llm_url", "") or "")
    return DshPlanner(client=client, model=getattr(cli, "dsh_model", "") or "")


def _handle_dsh_serve(cli) -> int:
    """启动本地内核 + 控制台（阻塞）。"""
    try:
        server = _make_server(cli)
    except Exception as exc:  # noqa: BLE001 - 配置/端口问题统一按启动失败处理
        logger.error("DSH 启动失败: %s", exc)
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("[DSH] 用户中断，正在收尾")
    except Exception as exc:  # noqa: BLE001
        logger.error("DSH 运行异常: %s", exc)
        return 1
    return 0


def _handle_dsh_run(cli) -> int:
    """无头跑一次目标（一次性会话），结论非 passed 即返回 1。"""
    from uita.tools.dsh.context import ToolContext
    from uita.tools.dsh.harness import run_goal
    from uita.tools.dsh.policy import Mode
    from uita.tools.dsh.session import DshSession

    goal = str(getattr(cli, "goal", "") or "").strip()
    case_text, default_case_name = _load_case_text_with_name(cli)
    if not goal and not case_text:
        logger.error("dsh-run 模式必须传 --goal、--case-text 或 --case-file")
        return 2

    from uita import config

    mode = Mode.parse(getattr(cli, "dsh_mode", "") or config.UITA_DSH_MODE)
    auto_approve = bool(getattr(cli, "dsh_auto_approve", False)) or mode is Mode.AUTO
    if mode is Mode.SUPERVISED and not auto_approve:
        logger.warning(
            "[DSH] 无头运行无法人工审批，已降级为只读；如需动作自动化请加 "
            "--dsh-auto-approve 或 --dsh-mode auto"
        )
        mode = Mode.READONLY

    case_obj = None
    if case_text:
        from uita.tools.dsh.textcase import parse_text_case

        case_obj = parse_text_case(case_text, default_name=default_case_name)
    session = DshSession(
        goal=goal,
        case=case_obj,
        app_name=str(getattr(cli, "app", "") or ""),
        mode=mode,
        max_steps=int(getattr(cli, "dsh_max_steps", 0) or config.UITA_DSH_MAX_STEPS),
        timeout_s=int(getattr(cli, "dsh_timeout", 0) or config.UITA_DSH_TIMEOUT_S),
        approval_timeout_s=float(config.UITA_DSH_APPROVAL_TIMEOUT_S),
        auto_approve=auto_approve,
        max_loop_repeat=int(getattr(config, "UITA_DSH_LOOP_REPEAT", 4) or 4),
        stall_timeout_s=float(getattr(config, "UITA_DSH_STALL_S", 300) or 300),
    )
    ctx = ToolContext(session=session, app_name=session.app_name)

    async def _run_and_cleanup():
        try:
            return await run_goal(session, ctx, _make_planner(cli))
        finally:
            await ctx.cleanup_engine()

    try:
        conclusion = asyncio.run(_run_and_cleanup())
    except Exception as exc:  # noqa: BLE001
        logger.error("[DSH] 运行异常: %s", exc)
        return 1

    ir_obj = session.to_ir(persist=True)
    out_path = str(getattr(cli, "ir_save", "") or "").strip()
    if out_path and ir_obj is not None:
        try:
            from uita.tools.dsh.ir import save_ir

            save_ir(ir_obj, out_path)
            print(f"[DSH] IR 已另存: {out_path}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DSH] IR 另存失败: %s", exc)

    tcs_out = str(getattr(cli, "tcs_out", "") or "").strip()
    promote = bool(getattr(cli, "promote", False))
    if (tcs_out or promote) and conclusion.get("state") == "passed" and ir_obj is not None:
        from uita.tools.dsh.compile import (
            compile_ir_to_tcs,
            promote_ir,
            resolve_tcs_output_path,
        )

        if tcs_out:
            try:
                resolved_tcs = resolve_tcs_output_path(
                    tcs_out, ir_obj, preferred_name=default_case_name or ir_obj.meta.name,
                )
                tcs_text = compile_ir_to_tcs(ir_obj)
                os.makedirs(os.path.dirname(os.path.abspath(resolved_tcs)) or ".", exist_ok=True)
                with open(resolved_tcs, "w", encoding="utf-8") as handle:
                    handle.write(tcs_text)
                print(f"[DSH] TCS 已自动编译落盘: {resolved_tcs}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[DSH] TCS 自动编译失败: %s", exc)
        if promote:
            try:
                promoted = promote_ir(ir_obj, name=default_case_name or ir_obj.meta.name)
                print(f"[DSH] IR 已提升入库: {promoted}")
            except Exception as exc:  # noqa: BLE001
                logger.warning("[DSH] IR 提升入库失败: %s", exc)

    fmt = str(getattr(cli, "dsh_output_format", "") or "text").strip()
    if fmt in {"json", "stream-json"}:
        import json
        from uita.tools.dsh.headless import build_result_envelope

        envelope = build_result_envelope(session, conclusion, 0.0)
        print(json.dumps(envelope, ensure_ascii=False))
    else:
        print(f"\n[DSH] 结论: {conclusion.get('state')} — {conclusion.get('summary', '')}")
        if case_obj is not None:
            for step in case_obj.steps:
                print(f"  [{step.status}] {step.no}. {step.action}"
                      + (f" — {step.detail}" if step.detail else ""))
        print(f"[DSH] 证据目录: {session.store.session_dir if session.store else '-'}")
    return 0 if conclusion.get("state") == "passed" else 1


def _handle_dsh_replay(cli) -> int:
    """按步骤 IR 执行一次（零 LLM），结论非 passed 即返回 1。"""
    from uita import config
    from uita.tools.dsh.context import ToolContext
    from uita.tools.dsh.executor import run_ir
    from uita.tools.dsh.ir import IRError, load_ir, save_ir
    from uita.tools.dsh.policy import Mode
    from uita.tools.dsh.session import DshSession

    path = str(getattr(cli, "ir", "") or "").strip()
    if not path:
        logger.error("dsh-replay 模式必须传 --ir <path>（uita.ir/1 JSON）")
        return 2
    try:
        ir = load_ir(path)
    except IRError as exc:
        logger.error("[DSH] IR 非法: %s", exc)
        return 2

    # IR 已经过人审，重放默认免审批；显式传 --dsh-mode 才按该模式走
    explicit_mode = str(getattr(cli, "dsh_mode", "") or "").strip()
    mode = Mode.parse(explicit_mode) if explicit_mode else Mode.AUTO
    auto_approve = bool(getattr(cli, "dsh_auto_approve", False)) or mode is Mode.AUTO
    if mode is Mode.SUPERVISED and not auto_approve:
        logger.warning("[DSH] 无头重放无法人工审批，已按 auto 执行")
        mode, auto_approve = Mode.AUTO, True

    os_name = ir.meta.os if ir.meta.os in {"macOS", "Windows", "Linux"} else ""
    session = DshSession(
        goal=ir.meta.name,
        case=ir.to_text_case(),
        app_name=ir.meta.app,
        mode=mode,
        max_steps=int(getattr(cli, "dsh_max_steps", 0) or config.UITA_DSH_MAX_STEPS),
        timeout_s=int(getattr(cli, "dsh_timeout", 0) or config.UITA_DSH_TIMEOUT_S),
        approval_timeout_s=float(config.UITA_DSH_APPROVAL_TIMEOUT_S),
        auto_approve=auto_approve,
    )
    ctx = ToolContext(session=session, app_name=session.app_name, os_name=os_name)
    try:
        conclusion = asyncio.run(run_ir(session, ctx, ir))
    except Exception as exc:  # noqa: BLE001
        logger.error("[DSH] IR 执行异常: %s", exc)
        return 1

    out_path = str(getattr(cli, "ir_save", "") or "").strip()
    if out_path:
        try:
            save_ir(ir, out_path)
            print(f"[DSH] IR 已另存: {out_path}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DSH] IR 另存失败: %s", exc)

    print(f"\n[DSH] 结论: {conclusion.get('state')} — {conclusion.get('summary', '')}")
    for step in (session.case.steps if session.case else []):
        print(f"  [{step.status}] {step.no}. {step.action}"
              + (f" — {step.detail}" if step.detail else ""))
    print(f"[DSH] 断言: {session.assertions}")
    print(f"[DSH] 证据目录: {session.store.session_dir if session.store else '-'}")
    return 0 if conclusion.get("state") == "passed" else 1


def _load_case_text_with_name(cli) -> tuple[str, str]:
    """读取文本用例及默认用例名：--case-text 若给 .txt/.md/.json 文件路径自动按文件读取。"""
    from uita.tools.dsh.cli import _looks_like_case_file_path

    text = str(getattr(cli, "case_text", "") or "").strip()
    path = str(getattr(cli, "case_file", "") or "").strip()
    if not path and _looks_like_case_file_path(text):
        path = text
        text = ""
    if text:
        return text, ""
    if not path:
        return "", ""
    stem = os.path.splitext(os.path.basename(path))[0].strip()
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read().strip()
    except OSError as exc:
        logger.error("读取用例文件失败 %s: %s", path, exc)
        return "", ""
    if path.lower().endswith(".json") and raw.startswith("{"):
        try:
            from uita.tools.dsh.case_import import convert_json_cases_text

            app = str(getattr(cli, "app", "") or "sample_web").strip() or "sample_web"
            items = convert_json_cases_text(raw, app=app)
            if items:
                return items[0][1], items[0][0] or stem
        except Exception as exc:  # noqa: BLE001
            logger.warning("JSON 用例自动转换失败，回退原始文本: %s", exc)
    return raw, stem


def _load_case_text(cli) -> str:
    return _load_case_text_with_name(cli)[0]


def _handle_dsh_mcp(cli) -> int:
    """以 MCP 工具面启动（stdio / streamable-http）。"""
    from uita.tools.dsh import mcp_face

    transport = str(getattr(cli, "dsh_transport", "") or "stdio").strip().lower()
    try:
        server = _make_server(cli)
    except Exception as exc:  # noqa: BLE001
        logger.error("DSH MCP 启动失败: %s", exc)
        return 1
    try:
        if transport == "http":
            asyncio.run(mcp_face.serve_http(
                server, host=server.host, port=server.port + 1,
            ))
        else:
            asyncio.run(mcp_face.serve_stdio(server))
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        logger.error("DSH MCP 运行异常: %s", exc)
        return 1
    return 0
