"""观察类工具：把"当前界面"变成模型可读的载荷。

输出刻意保持小体积：截图只回会话目录下的**文件名**（由控制页面按文件路由
读取），可访问性树摘要截断到固定长度，避免把模型上下文撑爆。
"""
from __future__ import annotations

import os
import time
from typing import Any

from ..registry import (TIER_READ, ArtifactRef, DshTool, ToolResult,
                        register_dsh_tool)

_A11Y_CHAR_LIMIT = 8000


@register_dsh_tool
class ObserveSnapshotTool(DshTool):
    name = "observe_snapshot"
    tier = TIER_READ
    description = (
        "对当前应用窗口拍一次快照：窗口标题/矩形、可访问性树摘要、截图文件名。"
        "每次操作后都应重新观察，确认界面处于预期状态。"
    )
    params = {
        "screenshot": {"type": "boolean", "description": "是否截图（默认 true）"},
        "accessibility": {"type": "boolean", "description": "是否附可访问性树摘要（默认 true）"},
    }

    async def run(self, ctx: Any, screenshot: bool = True,
                  accessibility: bool = True) -> ToolResult:
        engine = ctx.require_engine()
        data: dict[str, Any] = {"app": ctx.app_name, "platform": ctx.os_name}
        evidence: list[ArtifactRef] = []

        try:
            data["window_title"] = await engine.get_window_title()
        except Exception as exc:  # noqa: BLE001 - 可选能力缺失
            data["window_title_error"] = str(exc)

        rect_fn = getattr(engine, "get_window_rect", None)
        if callable(rect_fn):
            try:
                rect = await rect_fn()
                if rect:
                    data["window_rect"] = list(rect)
            except Exception as exc:  # noqa: BLE001
                data["window_rect_error"] = str(exc)

        if accessibility:
            summary_fn = getattr(engine, "get_accessibility_summary", None)
            if callable(summary_fn):
                try:
                    summary = await summary_fn()
                    if summary:
                        text = str(summary)
                        data["accessibility"] = text[:_A11Y_CHAR_LIMIT]
                        data["accessibility_truncated"] = len(text) > _A11Y_CHAR_LIMIT
                except Exception as exc:  # noqa: BLE001
                    data["accessibility_error"] = str(exc)
            else:
                data["accessibility_error"] = "engine 未实现可访问性摘要"

        if screenshot:
            store = getattr(ctx.session, "store", None)
            if store is None:
                data["screenshot_error"] = "会话未配置证据目录"
            else:
                name = f"shot-{time.time_ns()}.png"
                try:
                    path = store.artifact_path(name)
                    saved = await engine.take_screenshot(path)
                    if not saved or not os.path.isfile(path) or os.path.getsize(path) == 0:
                        raise OSError("截图返回为空、未生成或文件为空")
                    data["screenshot"] = name
                    data["screenshot_saved"] = True
                    evidence.append(ArtifactRef(name=name, path=path, kind="screenshot"))
                except Exception as exc:  # noqa: BLE001 - 截图失败不中断会话
                    data["screenshot_saved"] = False
                    data["screenshot_error"] = f"{type(exc).__name__}: {exc}"

        ctx.set_observation(data)
        return ToolResult(ok=True, data=data, evidence=evidence)
