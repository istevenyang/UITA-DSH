"""perf 场景测量框架（核心：**只提供机制，不含任何应用知识**）。

分层（宪法 R2）：
  - ``core/perf/scenarios/`` ← 注册表、桥接、通用跨窗口 target 工具、app 场景包加载器
  - ``apps/<app>/perf/scenarios/`` ← 具体场景（选择器/URL/注入 API 等应用知识）

core 不静态 import ``apps.*``；app 场景包由 CLI 层调 :func:`load_app_scenarios`
按约定动态导入（导入即注册）。场景模块契约见 :mod:`uita.core.perf.scenarios.registry`。

对应计划附录 B.3 的 13 项客户端性能场景，实现位置见
``apps/sample_media/perf/scenarios/``（DemoGallery 专属）。
"""
from __future__ import annotations

from uita.core.perf.scenarios.app_loader import (
    load_app_cross_unit_charts,
    load_app_default_preset,
    load_app_load_modes,
    load_app_launch_probe,
    load_app_presets,
    load_app_round_prepare,
    load_app_round_prepare_before_retry,
    load_app_round_teardown,
    load_app_run_setup,
    load_app_scenarios,
    load_app_startup_ready_js,
    load_app_unit_sequence,
    load_app_warmup_default,
)
from uita.core.perf.scenarios.bridge import ScenarioBridge, ScenarioUnsupported
from uita.core.perf.scenarios.registry import (
    SCENARIOS,
    accepted_options,
    aggregate_fields,
    aggregate_pick,
    as_list,
    available_scenarios,
    default_options,
    register_scenario,
    report_detail,
    report_keys,
    run_scenario,
    scenario_label,
)
from uita.core.perf.scenarios.targets import (
    await_target,
    cdp_port,
    close_target,
    endpoint_alive,
    evaluate_in_target,
    list_targets,
    parse_payload,
    pick_target,
    pick_target_by_url,
)

__all__ = [
    "SCENARIOS",
    "ScenarioBridge",
    "ScenarioUnsupported",
    "accepted_options",
    "aggregate_fields",
    "aggregate_pick",
    "as_list",
    "available_scenarios",
    "await_target",
    "cdp_port",
    "close_target",
    "default_options",
    "endpoint_alive",
    "evaluate_in_target",
    "list_targets",
    "load_app_cross_unit_charts",
    "load_app_default_preset",
    "load_app_load_modes",
    "load_app_launch_probe",
    "load_app_presets",
    "load_app_round_prepare",
    "load_app_round_prepare_before_retry",
    "load_app_round_teardown",
    "load_app_run_setup",
    "load_app_scenarios",
    "load_app_startup_ready_js",
    "load_app_unit_sequence",
    "load_app_warmup_default",
    "parse_payload",
    "pick_target",
    "pick_target_by_url",
    "register_scenario",
    "report_detail",
    "report_keys",
    "run_scenario",
    "scenario_label",
]
