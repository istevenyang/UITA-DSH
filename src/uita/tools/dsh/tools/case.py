"""用例类工具：解析/执行既有 .tcs（确定性回归由 TCS 承载，AI 只负责编排）。"""
from __future__ import annotations

import os
from typing import Any

from ..registry import (TIER_READ, TIER_WRITE, DshTool, ToolResult,
                        register_dsh_tool)


def resolve_case_path(raw: str) -> str:
    """把入参解析为 ``CASES_DIR`` 内的 .tcs 绝对路径（拒绝越界）。"""
    from uita.config import CASES_DIR

    text = str(raw or "").strip()
    if not text:
        raise ValueError("case_path 不能为空")
    root = os.path.abspath(CASES_DIR)
    candidate = text if os.path.isabs(text) else os.path.join(root, text)
    target = os.path.abspath(candidate)
    if os.path.commonpath([root, target]) != root:
        raise ValueError(f"用例路径必须位于 {root} 内: {text}")
    if not target.endswith(".tcs"):
        raise ValueError("只接受 .tcs 文件")
    if not os.path.isfile(target):
        raise ValueError(f"用例不存在: {target}")
    return target


@register_dsh_tool
class CaseParseTool(DshTool):
    name = "case_parse"
    tier = TIER_READ
    description = "解析 .tcs 用例并返回名称/应用/步骤数（不启动引擎）。"
    params = {"case_path": {"type": "string", "description": "CASES_DIR 内路径或相对路径"}}
    required = ("case_path",)

    async def run(self, ctx: Any, case_path: str) -> ToolResult:
        from uita.core.dsl.parser import parse_file

        try:
            path = resolve_case_path(case_path)
        except ValueError as exc:
            return ToolResult.fail(str(exc))
        case = parse_file(path)
        steps = list(getattr(case, "setup_steps", []) or []) + \
            list(getattr(case, "steps", []) or []) + \
            list(getattr(case, "teardown_steps", []) or [])
        return ToolResult(ok=True, data={
            "case_path": path, "name": getattr(case, "name", ""),
            "app": getattr(case, "app", ""), "steps": len(steps),
        })


@register_dsh_tool
class CaseRunTool(DshTool):
    name = "case_run"
    tier = TIER_WRITE
    description = (
        "执行一个既有 .tcs 用例（确定性回归），返回是否通过、结果目录与失败步骤。"
        "执行完毕引擎状态视为不确定，需要继续编排时先重新 app_prepare。"
    )
    params = {
        "case_path": {"type": "string", "description": "CASES_DIR 内路径或相对路径"},
        "hooks": {"type": "string", "description": "逗号分隔的 Hook 名（可选）"},
    }
    required = ("case_path",)
    timeout_s = 1800.0

    async def run(self, ctx: Any, case_path: str, hooks: str = "") -> ToolResult:
        from uita.core.dsl.parser import parse_file
        from uita.core.interpreter import TCSInterpreter
        from uita.core.result.collector import make_run_result_dir

        try:
            path = resolve_case_path(case_path)
        except ValueError as exc:
            return ToolResult.fail(str(exc))
        case = parse_file(path)
        result_dir = make_run_result_dir(path)
        hook_names = [item.strip() for item in hooks.split(",") if item.strip()] or None
        interpreter = TCSInterpreter(
            case, case_file=path, result_dir=result_dir, hook_names=hook_names,
        )
        collector = await interpreter.run()
        result = collector.finalize()
        # interpreter 只 close 栈顶引擎；编排侧引擎状态此后不可信
        ctx.drop_engine()
        steps = list(getattr(result, "steps", []) or [])
        failed_steps = [
            {"name": step.name, "status": step.status,
             "message": getattr(step, "message", "") or getattr(step, "error", "")}
            for step in steps if getattr(step, "status", "passed") != "passed"
        ]
        return ToolResult(
            ok=True,
            data={
                "case_path": path,
                "case_name": getattr(result, "case_name", "") or getattr(case, "name", ""),
                "passed": bool(getattr(result, "is_passed", False)),
                "failed_steps": failed_steps,
                "steps": len(steps),
                "result_dir": result_dir,
            },
        )
