"""DSH 本地内核：HTTP/JSON 网关 + 控制页面静态托管。

安全边界（只做这几件事，不多做）：

- 只允许绑定回环地址（非 127.0.0.1/localhost 直接拒绝启动）；
- 所有业务路由要求 token（``X-Uita-Dsh-Token`` 或 ``Authorization: Bearer``），
  仅 ``/healthz`` 免鉴权；
- 校验 ``Host`` 与 ``Origin``（防 DNS rebinding / 浏览器侧 CSRF）；
- 请求体上限 1 MiB；产物读取只允许会话目录内的单层文件名；
- 会话串行：引擎独占，已有运行中的会话时新建返回 409。

路由与业务逻辑分离：``DshServer.handle()`` 是纯函数式的分发入口，HTTP 层只
负责解析与写回，便于单测直接构造请求而不用起套接字。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .context import ToolContext
from .executor import run_ir
from .harness import run_goal
from .ir import IRError, IR
from .planner import DshPlanner
from .policy import Mode
from .registry import all_tools, get_tool, load_builtin_tools
from .session import DshSession, SessionState
from .textcase import parse_text_case
from . import settings_store

logger = logging.getLogger("uita.dsh")

BODY_LIMIT = 1024 * 1024
_CONSOLE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "console.html")


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"

    def json(self) -> Any:  # pragma: no cover - 调试便利
        return json.loads(self.body.decode("utf-8"))


def _json_response(status: int, payload: Any) -> Response:
    return Response(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _optional_positive_int(value: Any, default: int, name: str) -> int:
    """解析可选正整数。空或 0 沿用默认值；非法或负数直接拒绝。"""
    if value in (None, "", 0, "0"):
        return int(default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if parsed < 0:
        raise ValueError(f"{name} 不能为负数")
    return parsed or int(default)


class DshServer:
    """本地引擎网关。"""

    def __init__(self, *, host: str = "", port: int = 0, token: str = "",
                 mode: Mode | str = "", store_root: str = "",
                 planner_factory: Optional[Callable[[], Any]] = None,
                 max_steps: int = 0, timeout_s: int = 0,
                 approval_timeout_s: float = 0.0, auto_approve: bool = False,
                 model: str = "", llm_url: str = "",
                 settings_file: str = "") -> None:
        from uita import config

        # 界面「设置」落盘的配置：CLI > env > 文件 > config 默认值
        self.settings_path = settings_file or settings_store.settings_path()
        saved = settings_store.effective_defaults(self.settings_path)
        self.host = (host or config.UITA_DSH_HOST).strip() or "127.0.0.1"
        self.port = int(port or saved["port"] or config.UITA_DSH_PORT)
        self.token = (token or config.UITA_DSH_TOKEN or "").strip() \
            or secrets.token_urlsafe(18)
        self.mode = Mode.parse(mode or saved["mode"] or config.UITA_DSH_MODE)
        self.store_root = store_root or config.UITA_DSH_OUT_ROOT
        self.max_steps = int(max_steps or saved["max_steps"] or config.UITA_DSH_MAX_STEPS)
        self.timeout_s = int(timeout_s or saved["timeout_s"] or config.UITA_DSH_TIMEOUT_S)
        self.approval_timeout_s = float(
            approval_timeout_s or config.UITA_DSH_APPROVAL_TIMEOUT_S
        )
        self.auto_approve = bool(auto_approve)
        self.default_app = saved["app"]
        self.model = model or saved["llm_model"]
        self.llm_url = llm_url or saved["llm_url"]
        self.llm_api_key = saved["llm_api_key"]
        self._planner_factory = planner_factory or self._default_planner_factory
        self.sessions: dict[str, DshSession] = {}
        self.contexts: dict[str, ToolContext] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._lock = threading.Lock()
        load_builtin_tools()

    def _default_planner_factory(self) -> Any:
        """默认规划器：端点/密钥/模型由 env 或界面设置提供，CLI 可覆盖。"""
        from .planner import HttpChatClient

        return DshPlanner(
            client=HttpChatClient(url=self.llm_url, api_key=self.llm_api_key),
            model=self.model,
        )

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def serve_forever(self) -> None:
        """启动 HTTP 服务（阻塞）。"""
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError(
                f"DSH 只允许绑定回环地址（当前 {self.host}）；如需远程访问请走 SSH 隧道"
            )
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        logger.info("DSH 控制台: http://%s:%s/  token=%s", self.host, self.port, self.token)
        print(f"\n[DSH] 控制台: http://{self.host}:{self.port}/", flush=True)
        print(f"[DSH] token: {self.token}\n", flush=True)
        try:
            self._httpd.serve_forever()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        for session in list(self.sessions.values()):
            if not session.is_terminal():
                session.request_stop()
        for thread in list(self._threads.values()):
            thread.join(timeout=2.0)
        if self._httpd is not None:
            try:
                self._httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
            self._httpd = None

    # ------------------------------------------------------------------
    # 分发（纯逻辑，可直接单测）
    # ------------------------------------------------------------------

    def handle(self, method: str, raw_path: str,
               query: Optional[dict[str, list[str]]] = None,
               body: bytes = b"",
               headers: Optional[dict[str, str]] = None) -> Response:
        method = method.upper()
        headers = {str(k): str(v) for k, v in (headers or {}).items()}
        lower_headers = {k.lower(): v for k, v in headers.items()}
        parsed = urlparse(raw_path)
        path = unquote(parsed.path or "/")
        query = query or parse_qs(parsed.query)

        if path == "/healthz":
            return _json_response(200, {"ok": True})
        host_error = self._check_host(lower_headers)
        if host_error:
            return _json_response(403, {"ok": False, "error": host_error})
        origin_error = self._check_origin(lower_headers)
        if origin_error:
            return _json_response(403, {"ok": False, "error": origin_error})
        # 控制台页面本身不含数据，浏览器导航无法携带自定义头，故免 token 下发；
        # 页面内所有 API 调用仍需 token。
        if method == "GET" and path == "/":
            return self._console()
        if not self._authorized(lower_headers):
            return _json_response(401, {"ok": False, "error": "缺少或错误的 token"})
        if len(body) > BODY_LIMIT:
            return _json_response(413, {"ok": False, "error": "请求体过大"})

        payload: dict[str, Any] = {}
        if body:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return _json_response(400, {"ok": False, "error": f"请求体不是合法 JSON: {exc}"})
            if not isinstance(payload, dict):
                return _json_response(400, {"ok": False, "error": "请求体必须是 JSON 对象"})

        if path == "/state" and method == "GET":
            view = settings_store.public_view(self.settings_path)
            return _json_response(200, {
                "ok": True,
                "host": self.host,
                "port": self.port,
                "mode": self.mode.value,
                "configured": view["configured"],
                "settings_path": view["path"],
                "sessions": [s.snapshot() for s in self.sessions.values()],
                "tools": self._tool_listing(),
            })
        if path == "/settings" and method == "GET":
            return _json_response(200, {
                "ok": True, "settings": settings_store.public_view(self.settings_path),
            })
        if path == "/settings" and method == "POST":
            return self._save_settings(payload)
        if path == "/sessions" and method == "POST":
            return self._create_session(payload)
        if path == "/ir/compile" and method == "POST":
            return self._compile_ir(payload)
        if path == "/ir/promote" and method == "POST":
            return self._promote_ir(payload)
        if path.startswith("/tools/") and method == "POST":
            return self._call_tool(path[len("/tools/"):], payload)

        parts = [item for item in path.split("/") if item]
        if len(parts) >= 2 and parts[0] == "sessions":
            session = self.sessions.get(parts[1])
            if session is None:
                return _json_response(404, {"ok": False, "error": f"会话不存在: {parts[1]}"})
            if len(parts) == 2 and method == "GET":
                return _json_response(200, {"ok": True, "session": session.snapshot()})
            if len(parts) == 3 and parts[2] == "report" and method == "GET":
                return _json_response(200, {"ok": True, "report": self._report(session)})
            if len(parts) == 3 and parts[2] == "events" and method == "GET":
                return self._session_events(session, query)
            if len(parts) == 3 and parts[2] == "control" and method == "POST":
                return self._session_control(session, payload)
            if len(parts) == 3 and parts[2] == "ir":
                if method == "GET":
                    return self._get_ir(session)
                if method in {"PUT", "POST"}:
                    return self._put_ir(session, payload)
            if len(parts) == 3 and parts[2] == "replay" and method == "POST":
                return self._replay(session, payload)
            if len(parts) == 3 and parts[2] == "hint" and method == "POST":
                text = str(payload.get("text") or "").strip()
                if not text:
                    return _json_response(400, {"ok": False, "error": "hint 文本为空"})
                session.add_hint(text)
                return _json_response(200, {"ok": True})
            if len(parts) == 4 and parts[2] == "artifacts" and method == "GET":
                return self._artifact(session, parts[3])
        return _json_response(404, {"ok": False, "error": f"未知路由: {method} {path}"})

    # ------------------------------------------------------------------
    # 路由实现
    # ------------------------------------------------------------------

    def _console(self) -> Response:
        try:
            with open(_CONSOLE_FILE, "rb") as handle:
                return Response(200, handle.read(), "text/html; charset=utf-8")
        except OSError as exc:
            return _json_response(500, {"ok": False, "error": f"控制台页面缺失: {exc}"})

    def _save_settings(self, payload: dict[str, Any]) -> Response:
        """校验并落盘界面配置；校验失败 400 且不改动原文件。"""
        try:
            settings_store.save(payload, self.settings_path)
        except ValueError as exc:
            return _json_response(400, {"ok": False, "error": str(exc)})
        except OSError as exc:
            return _json_response(500, {"ok": False, "error": f"配置写入失败: {exc}"})
        restart_required = self._apply_settings()
        view = settings_store.public_view(self.settings_path)
        return _json_response(200, {
            "ok": True, "settings": view, "restart_required": restart_required,
        })

    def _apply_settings(self) -> bool:
        """把新配置应用到运行中的服务；端口变更需重启（返回 True 表示需要）。"""
        saved = settings_store.effective_defaults(self.settings_path)
        self.default_app = saved["app"]
        self.llm_url = saved["llm_url"] or self.llm_url
        self.model = saved["llm_model"] or self.model
        self.llm_api_key = saved["llm_api_key"]
        if saved["max_steps"]:
            self.max_steps = int(saved["max_steps"])
        if saved["timeout_s"]:
            self.timeout_s = int(saved["timeout_s"])
        if saved["mode"]:
            self.mode = Mode.parse(saved["mode"])
        return bool(saved["port"]) and int(saved["port"]) != int(self.port)

    def _tool_listing(self) -> list[dict[str, Any]]:
        return [
            {"name": tool.name, "tier": tool.tier, "description": tool.description,
             "parameters": tool.parameters()}
            for _, tool in sorted(all_tools().items())
        ]

    def _report(self, session: DshSession) -> dict[str, Any]:
        """执行结果：结论 + 步骤表 + 断言统计 + 证据文件。"""
        snapshot = session.snapshot()
        return {
            "session_id": snapshot["session_id"],
            "goal": snapshot["goal"],
            "app": snapshot["app"],
            "state": snapshot["state"],
            "conclusion": snapshot["conclusion"],
            "case": snapshot["case"],
            "case_progress": snapshot["case_progress"],
            "assertions": snapshot["assertions"],
            "budget": snapshot["budget"],
            "artifacts": snapshot["artifacts"],
            "dir": snapshot["dir"],
            "duration_s": round(max(0.0, snapshot["updated_at"] - snapshot["created_at"]), 1),
        }

    def _create_session(self, payload: dict[str, Any]) -> Response:
        goal = str(payload.get("goal") or "").strip()
        case_text = str(payload.get("test_case") or "").strip()
        ir_obj: Optional[IR] = None
        if isinstance(payload.get("ir"), dict):
            try:
                ir_obj = IR.from_dict(payload["ir"])
            except IRError as exc:
                return _json_response(400, {"ok": False, "error": f"IR 非法: {exc}"})
        case_obj = None
        if case_text:
            case_obj = parse_text_case(case_text)
            goal = goal or case_obj.name
        if ir_obj is not None:
            case_obj = ir_obj.to_text_case()
            goal = goal or ir_obj.meta.name
        if not goal:
            return _json_response(400, {"ok": False,
                                       "error": "goal / test_case / ir 至少提供一个"})
        try:
            max_steps = _optional_positive_int(
                payload.get("max_steps"), self.max_steps, "max_steps",
            )
            timeout_s = _optional_positive_int(
                payload.get("timeout_s"), self.timeout_s, "timeout_s",
            )
        except ValueError as exc:
            return _json_response(400, {"ok": False, "error": str(exc)})
        with self._lock:
            # IDLE 也算占用：线程刚起、尚未进入 PREPARING 时同样不能并发起新会话
            busy = [
                s.session_id for s in self.sessions.values()
                if not s.is_terminal()
            ]
            if busy:
                return _json_response(409, {
                    "ok": False, "error": "引擎被其它会话占用", "active": busy,
                })
            try:
                mode = Mode.parse(payload.get("mode"), self.mode)
            except Exception:  # noqa: BLE001 - parse 自带默认值
                mode = self.mode
            session = DshSession(
                goal=goal,
                app_name=str(payload.get("app") or "").strip()
                or (ir_obj.meta.app if ir_obj is not None else "") or self.default_app,
                mode=mode,
                max_steps=max_steps,
                timeout_s=timeout_s,
                approval_timeout_s=self.approval_timeout_s,
                auto_approve=self.auto_approve or mode is Mode.AUTO,
                case=case_obj,
                root=self.store_root,
            )
            ctx = ToolContext(session=session, app_name=session.app_name)
            if ir_obj is not None:
                session.ir = ir_obj
            self.sessions[session.session_id] = session
            self.contexts[session.session_id] = ctx
            target = self._run_session
            args: tuple[Any, ...] = (session, ctx)
            if ir_obj is not None:
                target, args = self._run_ir_session, (session, ctx, ir_obj)
            thread = threading.Thread(
                target=target, args=args,
                name=f"dsh-{session.session_id}", daemon=True,
            )
            self._threads[session.session_id] = thread
            thread.start()
        return _json_response(200, {"ok": True, "session": session.snapshot()})

    def _run_session(self, session: DshSession, ctx: ToolContext) -> None:
        def _body(loop: asyncio.AbstractEventLoop) -> None:
            planner = self._planner_factory()
            loop.run_until_complete(run_goal(session, ctx, planner))
            session.to_ir(persist=True)

        self._run_on_thread(session, ctx, _body, "会话")

    def _run_ir_session(self, session: DshSession, ctx: ToolContext,
                        ir: IR) -> None:
        """线程体：按 IR 执行（零 LLM）。"""

        def _body(loop: asyncio.AbstractEventLoop) -> None:
            loop.run_until_complete(run_ir(session, ctx, ir))

        self._run_on_thread(session, ctx, _body, "IR 会话")

    def _run_on_thread(self, session: DshSession, ctx: ToolContext,
                       body: Callable[[asyncio.AbstractEventLoop], None],
                       label: str) -> None:
        """跑会话线程，并保证离开时会话处于终态、引擎已回收。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        session.bind_loop(loop)
        try:
            body(loop)
        except Exception as exc:  # noqa: BLE001 - 线程内异常必须留痕且释放引擎占用
            logger.warning("dsh: %s线程异常 %s: %s", label, session.session_id, exc,
                           exc_info=True)
            if not session.is_terminal():
                try:
                    session.ensure_terminal(
                        SessionState.ERROR, f"{type(exc).__name__}: {exc}",
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("dsh: 无法把会话 %s 收成终态", session.session_id,
                                   exc_info=True)
        finally:
            if not session.is_terminal():
                try:
                    session.ensure_terminal(SessionState.ERROR, f"{label}线程退出但未进入终态")
                except Exception:  # noqa: BLE001
                    logger.warning("dsh: 会话 %s 停在非终态", session.session_id, exc_info=True)
            try:
                loop.run_until_complete(ctx.cleanup_engine())
            except Exception as exc:  # noqa: BLE001
                logger.warning("dsh: 引擎回收失败 %s: %s", session.session_id, exc)
            self._threads.pop(session.session_id, None)
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # IR 编辑 / 重放（DSH v2）
    # ------------------------------------------------------------------

    def _get_ir(self, session: DshSession) -> Response:
        if session.ir is None:
            return _json_response(404, {
                "ok": False, "error": "该会话没有 IR（ReAct 模式会话无步骤 IR）",
            })
        return _json_response(200, {
            "ok": True,
            "ir": session.ir.to_dict(),
            "revision": int(session.ir.meta.revision),
        })

    def _put_ir(self, session: DshSession, payload: dict[str, Any]) -> Response:
        if not session.is_terminal():
            return _json_response(409, {
                "ok": False, "error": "会话运行中，不能修改 IR（等终态或先中止）",
            })
        raw = payload.get("ir") if isinstance(payload.get("ir"), dict) else payload
        try:
            edited = IR.from_dict(raw)
        except IRError as exc:
            return _json_response(400, {"ok": False, "error": f"IR 非法: {exc}"})

        edited.meta.revision = max(
            int(getattr(getattr(session.ir, "meta", None), "revision", 0) or 0),
            int(edited.meta.revision or 0),
        ) + 1
        for step in edited.steps:
            if step.source == "human":
                step.human_edited = True
        session.ir = edited
        session.case = edited.to_text_case()
        session.append_event("ir_edited", revision=edited.meta.revision,
                             steps=len(edited.steps))
        return _json_response(200, {
            "ok": True, "ir": edited.to_dict(), "revision": edited.meta.revision,
        })

    def _replay(self, session: DshSession, payload: dict[str, Any]) -> Response:
        """按会话当前 IR 起一次新会话重放（原会话保持终态，便于对比改前/改后）。"""
        source = session.ir
        if source is None:
            return _json_response(400, {
                "ok": False, "error": "该会话没有 IR，无法重放",
            })
        with self._lock:
            busy = [s.session_id for s in self.sessions.values() if not s.is_terminal()]
            if busy:
                return _json_response(409, {
                    "ok": False, "error": "引擎被其它会话占用", "active": busy,
                })
            try:
                ir_copy = IR.from_dict(source.to_dict())
            except IRError as exc:  # pragma: no cover - 内存对象理应合法
                return _json_response(400, {"ok": False, "error": f"IR 非法: {exc}"})
            try:
                mode = Mode.parse(payload.get("mode"), session.mode)
            except Exception:  # noqa: BLE001
                mode = session.mode
            try:
                max_steps = _optional_positive_int(
                    payload.get("max_steps"), self.max_steps, "max_steps",
                )
                timeout_s = _optional_positive_int(
                    payload.get("timeout_s"), self.timeout_s, "timeout_s",
                )
            except ValueError as exc:
                return _json_response(400, {"ok": False, "error": str(exc)})
            replay = DshSession(
                goal=ir_copy.meta.name,
                app_name=ir_copy.meta.app or session.app_name or self.default_app,
                mode=mode,
                max_steps=max_steps,
                timeout_s=timeout_s,
                approval_timeout_s=self.approval_timeout_s,
                auto_approve=self.auto_approve or mode is Mode.AUTO,
                case=ir_copy.to_text_case(),
                root=self.store_root,
            )
            replay.ir = ir_copy
            replay.append_event("replay_of", source_session=session.session_id,
                                revision=int(ir_copy.meta.revision))
            ctx = ToolContext(session=replay, app_name=replay.app_name)
            self.sessions[replay.session_id] = replay
            self.contexts[replay.session_id] = ctx
            thread = threading.Thread(
                target=self._run_ir_session, args=(replay, ctx, ir_copy),
                name=f"dsh-{replay.session_id}", daemon=True,
            )
            self._threads[replay.session_id] = thread
            thread.start()
        return _json_response(200, {
            "ok": True, "session": replay.snapshot(),
            "source_session_id": session.session_id,
        })

    def _resolve_ir_payload(self, payload: dict[str, Any]) -> IR:
        session_id = str(payload.get("session_id") or "").strip()
        if session_id:
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError(f"会话不存在: {session_id}")
            ir_obj = session.ir or session.to_ir(persist=False)
            if ir_obj is None:
                raise IRError("该会话没有可用 IR")
            return ir_obj
        raw = payload.get("ir") if isinstance(payload.get("ir"), dict) else payload
        return IR.from_dict(raw)

    def _compile_ir(self, payload: dict[str, Any]) -> Response:
        from .compile import compile_ir_to_tcs

        try:
            ir_obj = self._resolve_ir_payload(payload)
            tcs = compile_ir_to_tcs(
                ir_obj,
                priority=str(payload.get("priority") or "P1"),
                owner=str(payload.get("owner") or "dsh"),
            )
        except KeyError as exc:
            return _json_response(404, {"ok": False, "error": str(exc)})
        except IRError as exc:
            return _json_response(400, {"ok": False, "error": f"IR 编译失败: {exc}"})
        return _json_response(200, {"ok": True, "tcs": tcs, "name": ir_obj.meta.name})

    def _promote_ir(self, payload: dict[str, Any]) -> Response:
        from .compile import promote_ir

        try:
            ir_obj = self._resolve_ir_payload(payload)
            saved_path = promote_ir(
                ir_obj,
                name=str(payload.get("name") or ""),
                overwrite=bool(payload.get("overwrite", False)),
            )
        except KeyError as exc:
            return _json_response(404, {"ok": False, "error": str(exc)})
        except IRError as exc:
            return _json_response(400, {"ok": False, "error": f"IR 提升失败: {exc}"})
        except OSError as exc:
            return _json_response(500, {"ok": False, "error": f"IR 落盘失败: {exc}"})
        return _json_response(200, {
            "ok": True, "path": saved_path, "name": ir_obj.meta.name,
        })

    def _session_events(self, session: DshSession,
                        query: dict[str, list[str]]) -> Response:
        def _first(key: str, default: str = "") -> str:
            values = query.get(key) or []
            return str(values[0]) if values else default

        try:
            since = int(_first("since", "0") or 0)
        except ValueError:
            return _json_response(400, {"ok": False, "error": "since 必须是整数"})
        try:
            wait = float(_first("wait", "0") or 0)
        except ValueError:
            wait = 0.0
        wait = max(0.0, min(wait, 25.0))
        deadline = time.monotonic() + wait
        while True:
            events = session.events_since(since)
            if events or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        return _json_response(200, {
            "ok": True,
            "state": session.state.value,
            "events": events[-200:],
            "last_seq": events[-1]["seq"] if events else since,
        })

    def _session_control(self, session: DshSession,
                         payload: dict[str, Any]) -> Response:
        action = str(payload.get("action") or "").strip()
        reason = str(payload.get("reason") or "")
        if action == "pause":
            return _json_response(200, {"ok": session.pause(), "state": session.state.value})
        if action == "resume":
            return _json_response(200, {"ok": session.resume(), "state": session.state.value})
        if action == "stop":
            session.request_stop()
            return _json_response(200, {"ok": True, "state": session.state.value})
        if action in {"approve", "reject"}:
            resolved = session.resolve_approval(action == "approve", reason)
            return _json_response(200 if resolved else 409, {
                "ok": resolved, "state": session.state.value,
                "error": "" if resolved else "当前没有待审批的步骤",
            })
        return _json_response(400, {"ok": False, "error": f"未知 action: {action}"})

    def _artifact(self, session: DshSession, name: str) -> Response:
        store = getattr(session, "store", None)
        path = store.resolve_artifact(name) if store is not None else None
        if not path:
            return _json_response(404, {"ok": False, "error": "产物不存在"})
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError as exc:
            return _json_response(404, {"ok": False, "error": f"读取失败: {exc}"})
        suffix = os.path.splitext(name)[1].lower()
        content_type = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".json": "application/json; charset=utf-8",
            ".txt": "text/plain; charset=utf-8", ".jsonl": "text/plain; charset=utf-8",
        }.get(suffix, "application/octet-stream")
        return Response(200, data, content_type)

    def _call_tool(self, name: str, payload: dict[str, Any]) -> Response:
        tool = get_tool(name)
        if tool is None:
            return _json_response(404, {"ok": False, "error": f"未知工具: {name}"})
        session_id = str(payload.get("session_id") or "").strip()
        args = payload.get("args") or {}
        if not isinstance(args, dict):
            return _json_response(400, {"ok": False, "error": "args 必须是对象"})
        session = self.sessions.get(session_id) if session_id else None
        ctx = self.contexts.get(session_id) if session_id else None
        if session is not None and ctx is None:
            ctx = ToolContext(session=session, app_name=session.app_name)
            self.contexts[session_id] = ctx
        if ctx is None:
            ctx = ToolContext()
        try:
            result = asyncio.run(tool.invoke(ctx, args))
        except RuntimeError as exc:  # 事件循环已在当前线程运行（理论上不会发生）
            return _json_response(500, {"ok": False, "error": f"{exc}"})
        payload_out = result.to_dict()
        payload_out["session_id"] = session_id
        payload_out["tool"] = tool.name
        return _json_response(200, payload_out)

    # ------------------------------------------------------------------
    # 鉴权与来源校验
    # ------------------------------------------------------------------

    def _authorized(self, headers: dict[str, str]) -> bool:
        token = headers.get("x-uita-dsh-token", "")
        if not token:
            auth = headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        return bool(token) and secrets.compare_digest(token, self.token)

    def _allowed_hosts(self) -> set[str]:
        hosts = {
            f"127.0.0.1:{self.port}", f"localhost:{self.port}",
            f"[::1]:{self.port}",
        }
        if self.port == 80:
            hosts |= {"127.0.0.1", "localhost"}
        return hosts

    def _check_host(self, headers: dict[str, str]) -> str:
        host = headers.get("host", "").strip()
        if not host:
            return ""  # HTTP/1.0 或无 Host 的本地调用放行（token 仍是必需项）
        return "" if host in self._allowed_hosts() else f"Host 不被允许: {host}"

    def _check_origin(self, headers: dict[str, str]) -> str:
        origin = headers.get("origin", "").strip()
        if not origin:
            return ""
        allowed = {f"http://{host}" for host in self._allowed_hosts()}
        return "" if origin in allowed else f"Origin 不被允许: {origin}"


def _make_handler(dsh: DshServer) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        server_version = "UITA-DSH/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.debug("dsh-http: " + fmt, *args)

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            body = self.rfile.read(length) if length > 0 else b""
            headers = {key: value for key, value in self.headers.items()}
            try:
                response = dsh.handle(method, self.path, query, body, headers)
            except Exception as exc:  # noqa: BLE001 - 请求处理失败必须返回，不能挂死连接
                logger.warning("dsh-http: %s %s 失败: %s", method, self.path, exc,
                               exc_info=True)
                payload = json.dumps(
                    {"ok": False, "error": "内部错误"}, ensure_ascii=False,
                ).encode("utf-8")
                response = Response(500, payload)
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(response.body)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802 - IR 编辑用（/sessions/<id>/ir）
            self._dispatch("PUT")

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch("GET")

    return _Handler
