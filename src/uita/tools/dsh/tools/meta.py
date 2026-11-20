"""编排元工具：任务收尾等"非操作类"工具。

``task_finish`` 由 harness 拦截并转成会话终态；单独通过 HTTP/MCP 调用时只
回一行确认，不改变任何会话状态（避免外部调用意外终结别人的会话）。
"""
from __future__ import annotations

import os
from typing import Any

from ..registry import (TIER_READ, ArtifactRef, DshTool, ToolResult,
                        register_dsh_tool)


@register_dsh_tool
class TaskFinishTool(DshTool):
    name = "task_finish"
    tier = TIER_READ
    description = (
        "结束本次编排：status=passed 表示目标已被断言验证通过；failed 表示确认无法达成"
        "（summary 必须写清卡在哪一步、看到什么）。调用后不再继续操作。"
    )
    params = {
        "status": {"type": "string", "enum": ["passed", "failed"]},
        "summary": {"type": "string", "description": "一句话结论"},
    }
    required = ("status", "summary")

    async def run(self, ctx: Any, status: str, summary: str) -> ToolResult:
        if status not in {"passed", "failed"}:
            return ToolResult.fail(f"非法 status={status!r}")
        return ToolResult(ok=True, data={"status": status, "summary": summary})


@register_dsh_tool
class ReportStepTool(DshTool):
    name = "report_step"
    tier = TIER_READ
    description = (
        "把文本用例某一步的结论写进步骤表（status=passed/failed/unknown/skipped）。"
        "passed 必须先有 assert_* 的 true 证据；判定不了用 unknown，不要猜。"
        "detail 写清依据（看到什么、命中哪个元素、断言结果）。"
    )
    params = {
        "step": {"type": "integer", "description": "步骤号（从 1 开始）"},
        "status": {"type": "string", "enum": ["passed", "failed", "unknown", "skipped"]},
        "detail": {"type": "string", "description": "结论依据（一句话）"},
    }
    required = ("step", "status")

    async def run(self, ctx: Any, step: int, status: str,
                  detail: str = "") -> ToolResult:
        session = getattr(ctx, "session", None)
        if session is None:
            return ToolResult.fail("当前会话不可用（无会话上下文）")
        if getattr(session, "case", None) is None:
            return ToolResult.fail("当前会话没有文本用例，无需 report_step")
        evidence: list[ArtifactRef] = []
        last = getattr(ctx, "last_observation", None) or {}
        shot = last.get("screenshot")
        store = getattr(session, "store", None)
        if shot and store is not None:
            try:
                path = store.artifact_path(str(shot))
                if os.path.isfile(path) and os.path.getsize(path) > 0:
                    evidence.append(ArtifactRef(name=str(shot), path=path, kind="screenshot"))
            except Exception:  # noqa: BLE001 - 证据缺失不阻断状态上报
                evidence = []
        try:
            evidence_fn = getattr(session, "step_completion_evidence", session.step_assertion_evidence)
            assertion_evidence = (evidence_fn(int(step))
                                  if status == "passed" else [])
            event = session.record_case_step(
                int(step), str(status), str(detail or ""),
                assertion_evidence + [ref.to_dict() for ref in evidence] or None,
            )
        except ValueError as exc:
            return ToolResult.fail(str(exc))
        payload_step = event.get("no")
        return ToolResult(ok=True, data={
            "step": payload_step,
            "event_seq": event["seq"],
            "assertion_evidence": assertion_evidence,
            "status": event.get("status"),
            "progress": session.case_progress(),
            "next_step": (session.case.next_pending().to_dict()
                          if session.case.next_pending() else None),
        }, evidence=evidence)
