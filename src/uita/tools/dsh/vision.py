"""DSH v2 多模态三契约（locate / verify / extract）。

设计要点（见 ``docs/plans/20261117200000-dsh-v2-ir-flow-plan.md`` §10）：

- 截图缓存：同一截图路径 + 同一查询只请求一次模型，避免同一步重复调用；
- 异常/超时不伪装成功：``verify`` 降级返回 ``verdict="unknown"`` 并写明原因，
  ``locate`` 返回 ``found=False`` + ``confidence=0.0``，``extract`` 返回空列表；
- 复用 ``planner.HttpChatClient`` 与严格 DSH 凭据边界。
"""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Any, Optional

from .planner import HttpChatClient, PlannerError, _extract_json_object


def _encode_image_data_url(shot_path: str) -> str:
    if not shot_path or not os.path.isfile(shot_path):
        raise FileNotFoundError(f"截图文件不存在: {shot_path}")
    with open(shot_path, "rb") as handle:
        raw = handle.read()
    if not raw:
        raise ValueError(f"截图文件为空: {shot_path}")
    ext = os.path.splitext(shot_path)[1].lower()
    mime = "image/jpeg" if ext in {".jpg", ".jpeg"} else "image/png"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _shot_digest(shot_path: str) -> str:
    try:
        st = os.stat(shot_path)
        return hashlib.sha1(f"{shot_path}:{st.st_size}:{st.st_mtime_ns}".encode("utf-8")).hexdigest()[:16]
    except OSError:
        return shot_path


class DshVisionClient:
    """多模态三契约客户端（带单会话级缓存与超时降级）。"""

    def __init__(self, client: Optional[Any] = None, model: str = "") -> None:
        self.client = client or HttpChatClient()
        self.model = model
        self._cache: dict[tuple[str, str, str], dict[str, Any]] = {}

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    async def _ask_json(self, contract: str, shot_path: str,
                        prompt: str) -> dict[str, Any]:
        key = (contract, _shot_digest(shot_path), prompt)
        if key in self._cache:
            return dict(self._cache[key])

        data_url = _encode_image_data_url(shot_path)
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }]
        payload = await self.client.complete(messages=messages, model=self.model)
        choices = payload.get("choices") if isinstance(payload, dict) else None
        text = ""
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            msg = first.get("message") or {}
            text = str(msg.get("content") or "")
        elif isinstance(payload, dict):
            text = str(payload.get("content") or "")
        parsed = _extract_json_object(text)
        self._cache[key] = dict(parsed)
        return parsed

    async def locate(self, target: str, shot_path: str,
                     window_rect: Optional[list[float]] = None) -> dict[str, Any]:
        """定位契约：返回 ``{found, bbox, name_guess, kind, confidence, candidates, error}``。"""
        clean_target = str(target or "").strip()
        if not clean_target:
            return {
                "found": False, "bbox": None, "name_guess": "",
                "kind": "unknown", "confidence": 0.0, "candidates": [],
                "error": "target 不能为空",
            }
        prompt = (
            f"在截图中定位目标元素「{clean_target}」。"
            + (f"窗口矩形：{window_rect}。" if window_rect else "")
            + "只输出 JSON：{\"found\": true/false, \"bbox\": [x, y, w, h], "
              "\"name_guess\": \"...\", \"kind\": \"button|text|input|icon|other\", "
              "\"confidence\": 0.0~1.0, \"candidates\": []}"
        )
        try:
            raw = await self._ask_json("locate", shot_path, prompt)
        except (FileNotFoundError, ValueError, PlannerError, Exception) as exc:
            return {
                "found": False, "bbox": None, "name_guess": clean_target,
                "kind": "unknown", "confidence": 0.0, "candidates": [],
                "error": f"{type(exc).__name__}: {exc}",
            }
        bbox = raw.get("bbox")
        valid_bbox = (
            [float(x) for x in bbox]
            if isinstance(bbox, (list, tuple)) and len(bbox) == 4
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in bbox)
            else None
        )
        conf = raw.get("confidence", 0.0)
        confidence = float(conf) if isinstance(conf, (int, float)) and not isinstance(conf, bool) else 0.0
        found = bool(raw.get("found", False)) and valid_bbox is not None and confidence > 0.0
        return {
            "found": found,
            "bbox": valid_bbox if found else None,
            "name_guess": str(raw.get("name_guess") or clean_target),
            "kind": str(raw.get("kind") or "other"),
            "confidence": round(max(0.0, min(1.0, confidence)), 3),
            "candidates": list(raw.get("candidates") or []) if isinstance(raw.get("candidates"), list) else [],
            "error": "",
        }

    async def verify(self, expect: str, shot_path: str) -> dict[str, Any]:
        """断言契约：返回 ``{verdict: 'true'|'false'|'unknown', reason, evidence_bbox}``。"""
        clean_expect = str(expect or "").strip()
        if not clean_expect:
            return {"verdict": "unknown", "reason": "expect 不能为空", "evidence_bbox": None}
        prompt = (
            f"根据截图验证断言意图：「{clean_expect}」。"
            "无法确定时 verdict 必须填 unknown，严禁猜测。"
            "只输出 JSON：{\"verdict\": \"true|false|unknown\", \"reason\": \"一句话依据\", "
            "\"evidence_bbox\": [x, y, w, h] 或 null}"
        )
        try:
            raw = await self._ask_json("verify", shot_path, prompt)
        except (FileNotFoundError, ValueError, PlannerError, Exception) as exc:
            return {
                "verdict": "unknown",
                "reason": f"视觉判定不可用: {type(exc).__name__}: {exc}",
                "evidence_bbox": None,
            }
        verdict = str(raw.get("verdict") or "unknown").strip().lower()
        if verdict not in {"true", "false", "unknown"}:
            verdict = "unknown"
        reason = str(raw.get("reason") or "").strip() or ("判定无法确定" if verdict == "unknown" else verdict)
        bbox = raw.get("evidence_bbox")
        valid_bbox = (
            [float(x) for x in bbox]
            if isinstance(bbox, (list, tuple)) and len(bbox) == 4
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in bbox)
            else None
        )
        return {
            "verdict": verdict,
            "reason": reason,
            "evidence_bbox": valid_bbox,
        }

    async def extract(self, shot_path: str) -> list[dict[str, Any]]:
        """批量提取契约：返回 ``[{name, bbox, kind, text}]``。"""
        prompt = (
            "提取截图中主要可交互元素，只输出 JSON："
            "{\"elements\": [{\"name\": \"...\", \"bbox\": [x, y, w, h], "
            "\"kind\": \"button|text|input|icon\", \"text\": \"...\"}]}"
        )
        try:
            raw = await self._ask_json("extract", shot_path, prompt)
        except (FileNotFoundError, ValueError, PlannerError, Exception):
            return []
        items = raw.get("elements")
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            bbox = item.get("bbox")
            if not name or not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
                continue
            try:
                coords = [float(x) for x in bbox]
            except (TypeError, ValueError):
                continue
            out.append({
                "name": name,
                "bbox": coords,
                "kind": str(item.get("kind") or "other"),
                "text": str(item.get("text") or ""),
            })
        return out
