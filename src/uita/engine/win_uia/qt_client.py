"""QtProbe Qt 探针通道客户端（MCP Streamable HTTP 薄封装）。

定位：Windows 示例桌面应用（Qt 框架）的**第二数据通道**——与 UIA 通道并列，
不拥有进程生命周期、不参与 launch/restart，只负责「连上探针 + 定位 + 操作」。

传输选型：走 QtProbe MCP server（`http://127.0.0.1:3000/mcp`，JSON-RPC
`tools/call`），复用 `httpx`（已在 requirements.txt，R13 无新增依赖）；
不直连 gRPC——qt-agent 的 proto 不在本仓，直连需 vendored proto + grpcio。

设计约束（见 docs/plans/20261116203324-win-qt-probe-channel-plan.md）：

1. **只连本机**：URL 的 host 必须是 localhost/127.0.0.1/::1，其它一律拒绝
   （避免被配置注入成对内网/外网的请求）。
2. **工具名白名单**：未知工具名直接 `QtProbeError`，防止 QtProbe 侧契约
   变更后静默跑到错误分支。
3. **异常不吞成假成功**：所有失败抛 `QtProbeError` 的子类，由引擎层统一
   转成「本次降级」语义（引擎 attach 失败即视为通道未装配）。
4. 单次调用超时由调用方给定，默认 3s（生命周期钩子里用），定位类用 5s。

本模块不 import 任何 apps/ 或 core/ 运行时模块（R1 分层），仅标准库 + httpx。
"""
from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 允许的探针服务地址主机名（本机回环，见模块 docstring 约束 1）
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

# 工具名白名单：仅收录本通道实际使用的工具。
# 新增工具须同时更新本集合与本模块对应封装方法（P0 真机以 `tools/list` 快照为准）。
KNOWN_TOOLS = frozenset({
    "find_process",
    "launch_or_inject",
    "status",
    "qt_health",
    "qt_find",
    "qt_click",
    "qt_execute",
    "qt_screenshot",
    "qt_ocr_text",
    # 元素探查/断言所需（去掉 objectName 为空时的盲区）
    "qt_snapshot",
    "qt_children",
    "qt_read",
    "qt_bounds",
    "qt_exists",
    "qt_key_press",
})

# 句柄前缀：`qt:<pid>:<objId>`，由引擎按前缀分流到本通道
HANDLE_PREFIX = "qt:"


class QtProbeError(Exception):
    """探针通道调用失败（连接/协议/工具返回错误）。"""


class QtProbeUnavailable(QtProbeError):
    """探针不可用（daemon 未起、端口未监听、attach 未做）。"""


def discover_probe_pid(exe_name: str) -> Optional[int]:
    """按进程名查 QtProbe 已记录的端口文件 → pid（找不到返回 None）。

    QtProbe 自加载/注入成功后会在 `%ProgramData%\\QtProbe\\ports\\<pid>.json`
    写下该进程的 agent 端口；多进程页面（如轻页面 `MiniHomePagePro.exe`）据此
    定位"哪个 pid 可探"。目录可用 `UITA_QT_PROBE_PORTS_DIR` 覆盖（单测用）。
    读取失败（文件消失/半写/权限）一律跳过，不抛异常。
    """
    name = (exe_name or "").strip().lower()
    if not name:
        return None
    ports_dir = os.environ.get("UITA_QT_PROBE_PORTS_DIR", "").strip() or os.path.join(
        os.environ.get("ProgramData") or os.environ.get("PROGRAMDATA") or "",
        "QtProbe", "ports",
    )
    try:
        candidates = sorted(glob.glob(os.path.join(ports_dir, "*.json")))
    except Exception:  # noqa: BLE001 — 目录不可读视为"无端口文件"
        return None
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                info = json.load(handle)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(info, dict):
            continue
        if str(info.get("exe_name", "")).strip().lower() != name:
            continue
        try:
            return int(info["pid"])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def validate_local_url(url: str) -> str:
    """校验并归一化探针地址：必须是 http(s) + 本机回环主机。

    Raises:
        QtProbeError: scheme/主机名不合规（防 SSRF/错误配置）。
    """
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        raise QtProbeError(f"探针地址 scheme 非法（仅 http/https）: {url!r}")
    host = (parsed.hostname or "").lower()
    if host not in _LOCAL_HOSTS:
        raise QtProbeError(
            f"探针地址必须指向本机回环（localhost/127.0.0.1/::1），收到 {host!r}"
        )
    return url


def make_handle(pid: int, obj_id: str) -> str:
    """构造通道句柄：`qt:<pid>:<objId>`。"""
    return f"{HANDLE_PREFIX}{int(pid)}:{obj_id}"


def is_qt_handle(element_id: str) -> bool:
    """判断 ElementRef.element_id 是否为本通道句柄。"""
    return isinstance(element_id, str) and element_id.startswith(HANDLE_PREFIX)


def parse_handle(handle: str) -> Optional[tuple[int, str]]:
    """解析句柄 → (pid, objId)；非法句柄返回 None（调用方据此判失效）。"""
    if not is_qt_handle(handle):
        return None
    rest = handle[len(HANDLE_PREFIX):]
    pid_part, sep, obj_id = rest.partition(":")
    if not sep or not obj_id:
        return None
    try:
        return int(pid_part), obj_id
    except ValueError:
        return None


def _extract_payload(body: dict) -> dict:
    """把 JSON-RPC 响应归一化为业务 dict。

    MCP `tools/call` 的 result 形如
    `{"content": [{"type": "text", "text": "<json 或纯文本>"}], "isError": bool}`；
    文本能解析为 JSON 时用解析结果，否则包成 `{"_text": ...}` 供上层按需取用。
    """
    if not isinstance(body, dict):
        raise QtProbeError(f"响应不是 JSON 对象: {type(body)}")
    if body.get("error"):
        err = body["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise QtProbeError(f"JSON-RPC error: {msg}")

    result = body.get("result")
    if not isinstance(result, dict):
        return {"result": result}

    texts = [
        item.get("text", "")
        for item in (result.get("content") or [])
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    text = "\n".join(t for t in texts if t)
    if result.get("isError"):
        raise QtProbeError(f"工具返回错误: {text or result!r}")
    if not text:
        return dict(result)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {"_text": text}
    return parsed if isinstance(parsed, dict) else {"_value": parsed}


class QtProbeClient:
    """QtProbe MCP 客户端（单连接，pid 绑定当前注入/自加载的进程）。"""

    def __init__(self, url: str, *, timeout: float = 3.0, pid: Optional[int] = None) -> None:
        self._url = validate_local_url(url)
        self._timeout = float(timeout)
        self.pid: Optional[int] = int(pid) if pid else None
        self._client: Any = None   # 惰性创建 httpx.AsyncClient

    # ── 基础调用 ──────────────────────────────────────────────────────

    async def _ensure_client(self):
        if self._client is None:
            import httpx  # 局部导入：无探针场景不付出导入成本

            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                follow_redirects=False,
            )
        return self._client

    async def call(self, tool: str, arguments: dict, *, timeout: Optional[float] = None) -> dict:
        """调用单个 MCP 工具，返回归一化后的 dict。

        Raises:
            QtProbeUnavailable: 连接失败/超时（daemon 未起等）。
            QtProbeError: 工具名未在白名单、协议错误、工具返回 isError。
        """
        if tool not in KNOWN_TOOLS:
            raise QtProbeError(f"未登记的工具名 {tool!r}（白名单见 KNOWN_TOOLS）")

        client = await self._ensure_client()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }
        try:
            resp = await client.post(self._url, json=payload, timeout=timeout or self._timeout)
        except Exception as exc:  # noqa: BLE001 — 连接类异常统一归为「不可用」
            raise QtProbeUnavailable(f"连接探针失败（{self._url}）: {exc}") from exc

        if resp.status_code >= 400:
            raise QtProbeError(f"探针返回 HTTP {resp.status_code}: {resp.text[:200]}")

        text = resp.text or ""
        if "text/event-stream" in (resp.headers.get("content-type") or ""):
            text = _last_sse_data(text)
        try:
            body = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise QtProbeError(f"探针响应不是 JSON: {text[:200]!r}") from exc
        return _extract_payload(body)

    async def health(self, *, timeout: Optional[float] = None) -> Optional[dict]:
        """`qt_health`；失败返回 None（不抛，供 attach 判定用）。"""
        try:
            return await self.call("qt_health", self._pid_args(), timeout=timeout)
        except QtProbeError as exc:
            logger.debug("[qt-probe] health 失败: %s", exc)
            return None

    async def close(self) -> None:
        """断开 HTTP 连接（不杀被测进程）。"""
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:  # noqa: BLE001 — 关连接失败不影响主流程
                pass
            self._client = None

    # ── 通道语义封装 ──────────────────────────────────────────────────

    def _pid_args(self) -> dict:
        return {"pid": self.pid} if self.pid else {}

    async def status(self) -> Optional[dict]:
        """`status`；失败返回 None（供 connect 判断 daemon 是否已连上该 pid）。"""
        try:
            return await self.call("status", self._pid_args())
        except QtProbeError as exc:
            logger.debug("[qt-probe] status 失败: %s", exc)
            return None

    async def launch_or_inject(self) -> Optional[dict]:
        """`launch_or_inject`；失败返回 None。"""
        try:
            return await self.call("launch_or_inject", self._pid_args())
        except QtProbeError as exc:
            logger.debug("[qt-probe] launch_or_inject 失败: %s", exc)
            return None

    async def connect(
        self, *, attempts: int = 3, backoff_s: float = 0.8,
    ) -> Optional[dict]:
        """确保 daemon 已为当前 pid 建立探针连接，返回 `qt_health` 结果（失败 None）。

        QtProbe daemon 只记录 agent 端口，不会自动连；未 `launch_or_inject` 时
        `qt_health` 会一直返回 `EADDRNOTAVAIL`（报错信息误导为网络故障）。
        因此这里按 `qt_health → status → launch_or_inject → qt_health` 顺序建立
        连接，并按 `attempts`/`backoff_s` 做有界重试；全部失败返回 None，由调用方
        降级 UIA（不抛异常，避免打断主链路）。
        """
        total = max(1, int(attempts))
        for index in range(total):
            health = await self.health()
            if health:
                return health
            if not bool((await self.status() or {}).get("connected")):
                if await self.launch_or_inject() is not None:
                    health = await self.health()
                    if health:
                        return health
            if index + 1 < total and backoff_s > 0:
                await asyncio.sleep(backoff_s * (index + 1))
        return None

    async def find(self, path: str, *, timeout_s: float = 5.0) -> Optional[dict]:
        """`qt_find` 按 XPath 定位，返回首个命中对象 dict（无命中返回 None）。

        返回对象至少含 `id`，通常含 `bounds`/`visible`/`className` 等字段
        （具体字段以 P0 真机 `tools/list` + 实测响应为准）。
        """
        payload = await self.call(
            "qt_find",
            {**self._pid_args(), "realName": {"path": path}, "timeout": int(timeout_s * 1000)},
        )
        return _first_object(payload)

    async def click(self, obj_id: str) -> None:
        """`qt_click` 对象级点击（PostMessage，不抢真实鼠标）。"""
        await self.call("qt_click", {**self._pid_args(), "id": obj_id})

    async def properties(self, obj_id: str) -> dict:
        """读控件属性（`qt_execute action=properties`）→ `{属性名: 字符串值}`。

        动作参数必须嵌在 `params` 内（schema：`{action, params:{id}}`）；扁平传
        `id` 会被 daemon 丢弃并返回 `OBJECT_NOT_FOUND: object not found: `。
        返回体为 `{ok, data}`（文本形态，含一行 contract 说明），此处归一化为 `data`。
        """
        payload = await self.call(
            "qt_execute",
            {**self._pid_args(), "action": "properties", "params": {"id": obj_id}},
        )
        return _execute_data(payload)

    async def bounds(self, obj_id: str) -> Optional[tuple]:
        """读控件几何 → `(x, y, w, h)`（**屏幕物理像素**，与 UIA 侧同坐标系），失败 None。

        走专用工具 `qt_bounds`（返回 `{bounds:{x,y,w,h}, frameX, frameY, ...}`）；
        `qt_execute action=bounds` 与扁平参数组合在真机上恒返回 OBJECT_NOT_FOUND，
        因此不作为主路径。探针给出的是窗口内坐标，这里按 `frameX/frameY` 偏移
        换算成屏幕坐标——`get_element_rect()`/`is_visible()` 都按屏幕坐标比对
        窗口矩形，不换算会整体偏出窗口框。
        """
        try:
            payload = await self.call("qt_bounds", {**self._pid_args(), "id": obj_id})
        except QtProbeError as exc:
            logger.debug("[qt-probe] bounds 失败: %s", exc)
            return None
        rect = _rect_from(payload)
        if rect is None:
            return None
        box = payload.get("bounds") if isinstance(payload.get("bounds"), dict) else payload
        try:
            offset_x = float(payload.get("frameX", box.get("frameX", 0)) or 0)
            offset_y = float(payload.get("frameY", box.get("frameY", 0)) or 0)
        except (TypeError, ValueError):
            offset_x = offset_y = 0.0
        return (rect[0] + offset_x, rect[1] + offset_y, rect[2], rect[3])

    async def execute(self, action: str, **kwargs) -> dict:
        """`qt_execute` 通用入口（inputSequence/scrollTo/getProperty 等）。

        动作参数统一嵌进 `params`（扁平传参会被 daemon 丢弃，见 `properties`）。
        返回 `data` 字段（失败抛 `QtProbeError`，不把错误当空结果）。
        """
        payload = await self.call(
            "qt_execute",
            {**self._pid_args(), "action": action, "params": dict(kwargs)},
        )
        return _execute_data(payload)


def _execute_data(payload: dict) -> dict:
    """归一化 `qt_execute` 返回 → `data` 字典。

    真机返回有两种形态：结构化 `{ok, data}`，或整包塞进 `_text` 的文本
    `{"ok":..,"data":..}\\ncontract: {...}`（`contract` 行导致整体不是合法 JSON）。
    两者都按 `data` 取值；`ok=false` 抛 `QtProbeError`（带 errorCode），避免把
    错误当成"读到空属性"静默吞掉。
    """
    if not isinstance(payload, dict):
        return {}
    body: dict = payload
    if "ok" not in payload and "data" not in payload:
        text = payload.get("_text")
        if isinstance(text, str) and text.strip():
            try:
                parsed = json.loads(text.splitlines()[0].strip())
            except (ValueError, TypeError):
                return payload
            if isinstance(parsed, dict):
                body = parsed
            else:
                return payload
    if body.get("ok") is False:
        raise QtProbeError(
            f"qt_execute 失败: {body.get('errorCode')} {body.get('errorMessage')}"
        )
    data = body.get("data")
    if isinstance(data, dict):
        return data
    return {} if "ok" in body else body


def rect_from(payload: dict) -> Optional[tuple]:
    """公开包装：从探针返回体中提取 `(x, y, w, h)`（供引擎构造 ElementRef.bbox）。"""
    return _rect_from(payload)


def _last_sse_data(body: str) -> str:
    """从 text/event-stream 响应中取最后一条 `data:` 载荷。"""
    data = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
    return data[-1] if data else ""


def _first_object(payload: dict) -> Optional[dict]:
    """从 qt_find 的多种返回形态中取首个对象。"""
    if not isinstance(payload, dict):
        return None
    objects = payload.get("objects")
    if isinstance(objects, list):
        for item in objects:
            if isinstance(item, dict) and item.get("id") is not None:
                return item
        return None
    if payload.get("id") is not None:
        return payload
    return None


def _rect_from(payload: dict) -> Optional[tuple]:
    """从 bounds/properties 返回中提取 (x, y, w, h)。"""
    if not isinstance(payload, dict):
        return None
    rect = payload.get("bounds") or payload.get("rect") or payload
    if isinstance(rect, (list, tuple)) and len(rect) >= 4:
        try:
            return (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
        except (TypeError, ValueError):
            return None
    if isinstance(rect, dict):
        try:
            return (
                float(rect["x"]), float(rect["y"]),
                float(rect.get("width", rect.get("w", 0))),
                float(rect.get("height", rect.get("h", 0))),
            )
        except (KeyError, TypeError, ValueError):
            return None
    return None
