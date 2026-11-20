"""DSH（Deterministic Skill Harness）编排层。

把 UITA 的安装 / 探针注入 / 引擎生命周期 / 定位 / 断言 / 用例执行收敛成一套
带 JSON Schema 的工具面，由 LLM tool-calling 循环驱动，另提供本地控制台
（HTTP/JSON 内核）与 MCP 外壳。

设计依据：``docs/plans/20261117160000-dsh-harness-engine-exposure-plan.md``。
"""
from .compile import compile_ir_file, compile_ir_to_tcs, promote_ir
from .context import ToolContext, host_os_name
from .evidence import EvidenceStore, new_session_id
from .harness import FINISH_TOOL, OBSERVE_TOOL, run_goal
from .headless import DshHeadlessResult, doctor_check, is_dsh_configured, run_headless
from .planner import DshPlanner, Plan, PlannerError, plan_from_completion
from .policy import Decision, Mode, PolicyGate
from .registry import (DshTool, DshToolError, TIER_ACTION, TIER_READ, TIER_WRITE,
                       ToolResult, all_tools, build_tool_specs, get_tool,
                       load_builtin_tools, register_dsh_tool, tool_names)
from .session import (DshSession, InvalidTransition, SessionState, SessionStopped)
from .vision import DshVisionClient

__all__ = [
    "DshHeadlessResult", "DshPlanner", "DshSession", "DshTool", "DshToolError",
    "DshVisionClient", "Decision", "EvidenceStore", "FINISH_TOOL",
    "InvalidTransition", "Mode", "OBSERVE_TOOL", "Plan", "PlannerError",
    "PolicyGate", "SessionState", "SessionStopped", "TIER_ACTION", "TIER_READ",
    "TIER_WRITE", "ToolContext", "ToolResult", "all_tools", "build_tool_specs",
    "compile_ir_file", "compile_ir_to_tcs", "doctor_check", "get_tool",
    "host_os_name", "is_dsh_configured", "load_builtin_tools", "new_session_id",
    "plan_from_completion", "promote_ir", "register_dsh_tool", "run_goal",
    "run_headless", "tool_names",
]
