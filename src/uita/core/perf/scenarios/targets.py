"""通用跨窗口 target 能力：发现 / 求值 / 关闭（应用无关、平台无关）。

适用场景：被测应用是多窗口架构（如 Electron 的独立预览窗），Playwright 的
``connectOverCDP`` 不会自动跟踪运行中**新开**的 BrowserWindow，必须在测量时：
  1. 动作前记录 ``/json/list`` 现有 target id；
  2. 动作后轮询**新出现**且 URL 含指定子串的 target（只认新的，否则会量到上一轮残留窗口 → 0ms 假值）；
  3. 经桥接把 JS 送进该窗口求值；
  4. 测完 ``/json/close/<id>`` 关闭，避免污染下一步。

本模块**不认识任何具体应用**：URL 子串（``url_hint``）与 JS 由调用方（app 侧场景）提供。
仅用 stdlib ``urllib``，不 import ``engine/``（守 R1 / R2）。
"""
from __future__ import annotations

import asyncio
import ast
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("uita")

__all__ = [
    "await_target",
    "cdp_port",
    "close_target",
    "endpoint_alive",
    "evaluate_in_target",
    "list_targets",
    "parse_payload",
    "pick_target",
    "pick_target_by_url",
]


def cdp_port(bridge: Any) -> Optional[int]:
    """取被测应用的 CDP 端口（经公开成员 ``engine.app_process.cdp_port``）。"""
    engine = getattr(bridge, "engine", None)
    app_process = getattr(engine, "app_process", None)
    port = getattr(app_process, "cdp_port", None)
    try:
        port = int(port) if port else 0
    except (TypeError, ValueError):
        return None
    return port or None


def _http_get(port: int, path: str, timeout: float = 2.0) -> Optional[str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.debug("[perf] CDP GET %s 失败: %s", path, exc)
        return None


def list_targets(port: int) -> list[dict[str, Any]]:
    """列出 CDP targets（失败返回空列表，不抛）。"""
    raw = _http_get(port, "/json/list")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [t for t in data if isinstance(t, dict)] if isinstance(data, list) else []


def pick_target(
    targets: list[dict[str, Any]],
    url_hint: str,
    pre_ids: Optional[list[str]] = None,
    *,
    require_fresh: bool = True,
) -> Optional[dict[str, Any]]:
    """挑出 URL 含 ``url_hint`` 的 target。

    ``require_fresh=True``（默认，测量用）：只认**动作后新出现**的 target，
    否则会把上一轮残留窗口当成本次结果（实测会得到 0ms 假值）。
    ``require_fresh=False``：容忍窗口复用（若应用复用同一 BrowserWindow，严格模式会超时）。
    """
    hits = [t for t in targets if url_hint in str(t.get("url", ""))]
    if not hits:
        return None
    pre = set(pre_ids or [])
    fresh = [t for t in hits if t.get("id") not in pre]
    if fresh:
        return fresh[0]
    return None if require_fresh else hits[0]


def pick_target_by_url(
    targets: list[dict[str, Any]], url: str, *, page_only: bool = True,
) -> Optional[dict[str, Any]]:
    """按**完整 URL 精确匹配**挑出 target（默认只认 ``type == "page"``）。

    与 :func:`pick_target` 的分工（两者语义不可互换）：
      - :func:`pick_target`：URL **含子串** + 默认只认「动作后新出现」的窗口
        → 用于"等一个新开的窗口"（如预览窗）；
      - 本函数：URL **全等** + 不看新旧 → 用于"定位一个**已经存在**的具体窗口"
        （如引擎当前操作的主窗口：先 ``evaluate("location.href")`` 拿 URL，再来这里取 target id）。
        子串匹配在这个用途下会误命中（主窗口 URL 常是其它窗口 URL 的前缀）。
    """
    if not url:
        return None
    for target in targets:
        if str(target.get("url", "")) != url:
            continue
        if page_only and str(target.get("type", "page")) != "page":
            continue
        return target
    return None


def endpoint_alive(port: int, timeout: float = 2.0) -> bool:
    """CDP 端点是否仍可达（即被测应用进程是否还活着）。

    **为什么不能用 ``list_targets()`` 判断**：它对「端点失联（进程已退出）」与
    「端点正常但一个窗口都没有」都返回 ``[]``，二者无法区分。而"关掉主窗口后应用
    是否仍常驻后台"恰恰要求区分这两种情况——否则静息期采样全为空时，无法判断是
    "应用后台常驻但无窗口"（合法静息）还是"关窗即退出"（口径不成立）。
    """
    return _http_get(port, "/json/version", timeout=timeout) is not None


def close_target(port: int, target_id: str) -> bool:
    """关闭指定 target（收尾用，避免下一场景复用旧窗口）。"""
    if not target_id:
        return False
    return _http_get(port, f"/json/close/{target_id}") is not None


async def await_target(
    port: int,
    url_hint: str,
    pre_ids: Optional[list[str]] = None,
    *,
    timeout_ms: int = 20000,
    poll_ms: int = 200,
) -> Optional[dict[str, Any]]:
    """轮询等待符合 ``url_hint`` 的新 target 出现（超时返回 ``None``）。"""
    deadline = time.monotonic() + max(0, timeout_ms) / 1000.0
    while time.monotonic() < deadline:
        target = pick_target(await asyncio.to_thread(list_targets, port), url_hint, pre_ids)
        if target is not None:
            return target
        await asyncio.sleep(max(1, poll_ms) / 1000.0)
    return None


def parse_payload(raw: Any) -> dict[str, Any]:
    """统一解析跨窗口返回值。

    三种形态都要吃下（都实测出现过）：
      - ``engine.evaluate`` → 已是 Python 对象（dict）
      - ``engine.evaluate_on_target*`` → ``str(result)``，JS 返回对象时是 **Python repr**
        （单引号、``None``/``True``），不是 JSON → 需 ``ast.literal_eval`` 兜底
      - 失败 → ``"ERROR: ..."`` 文本
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("ERROR:"):
            return {"error": text[:200]}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(text)  # Python repr 兜底
            except (ValueError, SyntaxError):
                return {"error": f"无法解析返回: {text[:120]}"}
        return parsed if isinstance(parsed, dict) else {"error": "返回类型异常"}
    return {"error": f"返回类型异常: {type(raw).__name__}"}


async def evaluate_in_target(
    bridge: Any, target: dict[str, Any], url_hint: str, js: str,
) -> dict[str, Any]:
    """在已发现的 target 上执行 JS 并解析返回。

    优先按 **URL** 求值：部分窗口（实测 DemoGallery 查看器）CDP ``title`` 为空串，
    而引擎的 title 匹配是 ``title_substr in title`` —— 传空串对任意 target 恒真，
    JS 会跑在主窗口里，量出"目标窗口已就绪"的**假结论**。
    引擎不支持按 URL 求值时，才退回 title（且要求 title 非空）。
    """
    if not url_hint:
        return {"error": "url_hint 不能为空（空串会命中任意 target）"}
    if getattr(bridge, "supports_url_target", False):
        return parse_payload(await bridge.evaluate_on_target_url(url_hint, js))
    title = str(target.get("title") or "")
    if not title:
        return {"error": "目标窗口标题为空且引擎不支持按 URL 求值（按标题会命中错误窗口）"}
    return parse_payload(await bridge.evaluate_on_target(title, js))
