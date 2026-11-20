"""UITA-DSH 分类日志管理器（Categorized Logging Manager）。

将日志按职责自动分流输出至项目根目录 `logs/` 下的独立文件：
  1. `logs/execution.log`（执行日志类）：
     - 记录用例执行流水、元素降级链路由决策（L1~L9 候选链与来源）、逐层定位尝试过程、
       命中层级/选择器/坐标/bbox、以及定位点击/输入/滑动/断言/等待的闭环详情。
  2. `logs/actions.log`（动作日志类）：
     - 记录 `click`、`type`、`scroll`、`assert`/`verify`、`wait`、`switch`/`switch_back`
       等动作的参数、重试、区域坐标计算（`region=` / 框选子区域）与 Judger 判定明细。
  3. `logs/kernel.log`（内核日志类）：
     - 记录解释器调度（Interpreter / StreamingRunner / BatchRunner）、DSL 解析、
       跨平台引擎（Playwright / MacAppKit / WinUIA / HTTP）、生命周期栈、Hook 与 DSH 编排日志。
  4. `logs/uita.log`（全量汇总日志）：
     - 全量汇总所有类别日志，便于需要全局时间线对齐时排障。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional, Sequence

LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(name)s] %(message)s"

_EXECUTION_PREFIXES = (
    "uita.execution",
    "uita.core.resolver",
    "uita.core.platform",
    "uita.core.rosetta",
    "resolver",
)

_ACTION_PREFIXES = (
    "uita.actions",
    "uita.core.actions",
    "actions",
)

_STRATEGY_TO_LEVEL_LABEL = {
    "map_exact": "L1_MAP_EXACT",
    "semantic": "L2_SEMANTIC",
    "text_match": "L3_TEXT_MATCH",
    "label_match": "L4_LABEL_MATCH",
    "role_match": "L5_ROLE_MATCH",
    "llm_selector": "L6_LLM_SELECTOR",
    "visual_fp": "L7_VISUAL_FP",
    "llm_visual": "L8_LLM_VISUAL",
    "qt_probe": "L9_QT_PROBE",
}


class CategoryLogFilter(logging.Filter):
    """按 logger 名称前缀分流日志类别：execution / actions / kernel。"""

    def __init__(self, category: str) -> None:
        super().__init__()
        self.category = category

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        is_exec = name.startswith(_EXECUTION_PREFIXES)
        is_action = name.startswith(_ACTION_PREFIXES)

        if self.category == "execution":
            return is_exec
        if self.category == "actions":
            return is_action
        if self.category == "kernel":
            return not is_exec and not is_action
        return True


def configure_categorized_logging(log_dir: str, console_level: str = "INFO", force: bool = False) -> dict[str, str]:
    """初始化控制台输出与根目录 `logs/` 下的分类日志文件（幂等）。

    Returns:
        各类别日志文件的绝对路径字典：`{"execution": ..., "actions": ..., "kernel": ..., "all": ...}`。
    """
    os.makedirs(log_dir, exist_ok=True)
    paths = {
        "execution": os.path.join(log_dir, "execution.log"),
        "actions": os.path.join(log_dir, "actions.log"),
        "kernel": os.path.join(log_dir, "kernel.log"),
        "all": os.path.join(log_dir, "uita.log"),
    }

    root_logger = logging.getLogger()
    if force:
        for h in list(root_logger.handlers):
            if getattr(h, "_uita_categorized", False):
                root_logger.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
    elif any(getattr(h, "_uita_categorized", False) for h in root_logger.handlers):
        return paths

    root_logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(LOG_FORMAT)
    num_level = getattr(logging, (console_level or "INFO").upper(), logging.INFO)

    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler) for h in root_logger.handlers):
        ch = logging.StreamHandler()
        ch.setLevel(num_level)
        ch.setFormatter(formatter)
        ch._uita_categorized = True  # type: ignore[attr-defined]
        root_logger.addHandler(ch)

    for category, file_path in paths.items():
        try:
            fh = logging.FileHandler(file_path, encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(formatter)
            if category != "all":
                fh.addFilter(CategoryLogFilter(category))
            fh._uita_categorized = True  # type: ignore[attr-defined]
            fh._uita_category = category  # type: ignore[attr-defined]
            root_logger.addHandler(fh)
        except OSError:
            pass

    return paths


def get_execution_logger() -> logging.Logger:
    """获取执行日志类 Logger（输出至 `logs/execution.log`）。"""
    return logging.getLogger("uita.execution")


def get_action_logger(action_name: str = "") -> logging.Logger:
    """获取动作日志类 Logger（输出至 `logs/actions.log`）。"""
    suffix = f".{action_name}" if action_name else ""
    return logging.getLogger(f"uita.actions{suffix}")


def get_kernel_logger(component: str = "") -> logging.Logger:
    """获取内核日志类 Logger（输出至 `logs/kernel.log`）。"""
    suffix = f".{component}" if component else ""
    return logging.getLogger(f"uita.kernel{suffix}")


def format_strategy_chain(strategies: Sequence[Any]) -> str:
    """将策略列表格式化为易读的 `L1_MAP_EXACT -> L3_TEXT_MATCH -> ...` 字符串。"""
    labels: list[str] = []
    for s in strategies:
        val = getattr(s, "value", str(s))
        labels.append(_STRATEGY_TO_LEVEL_LABEL.get(val, str(val).upper()))
    return " -> ".join(labels) if labels else "EMPTY"


def log_route_decision(
    element_name: str,
    strategies: Sequence[Any],
    chain_label: str,
    reason: str = "",
    skip_infos: Optional[Sequence[str]] = None,
) -> None:
    """向 `logs/execution.log` 打印元素降级链路由决策。"""
    chain_str = format_strategy_chain(strategies)
    skip_str = f" | skipped=[{'; '.join(skip_infos)}]" if skip_infos else ""
    reason_str = f" | reason={reason}" if reason else ""
    get_execution_logger().info(
        "[ROUTE] target=%r source=%s chain=[%s]%s%s",
        element_name,
        chain_label,
        chain_str,
        reason_str,
        skip_str,
    )


def log_locate_attempt(
    element_name: str,
    strategy: Any,
    result: Any,
    latency_ms: float,
    step_idx: int = 0,
) -> None:
    """向 `logs/execution.log` 打印单层降级定位尝试与命中详情（选择器 / 坐标 / bbox）。"""
    strat_val = getattr(strategy, "value", str(strategy))
    strat_label = _STRATEGY_TO_LEVEL_LABEL.get(strat_val, str(strat_val).upper())
    found = bool(getattr(result, "found", False))
    if found:
        level_obj = getattr(result, "level", None)
        level_name = getattr(level_obj, "name", str(level_obj or strat_label))
        elem = getattr(result, "element", None)
        elem_id = getattr(elem, "element_id", "") if elem is not None else ""
        coords = getattr(elem, "coords", None) if elem is not None else None
        bbox = getattr(elem, "bbox", None) if elem is not None else None
        get_execution_logger().info(
            "[LOCATE_HIT] target=%r step=#%d strategy=%s level=%s selector=%r coords=%s bbox=%s elapsed=%.1fms",
            element_name,
            step_idx + 1,
            strat_label,
            level_name,
            elem_id,
            coords,
            bbox,
            latency_ms,
        )
    else:
        get_execution_logger().debug(
            "[LOCATE_TRY] target=%r step=#%d strategy=%s -> MISS (%.1fms)",
            element_name,
            step_idx + 1,
            strat_label,
            latency_ms,
        )


def log_action_execution(
    action: str,
    target: str,
    *,
    passed: bool,
    duration_ms: float,
    level: str = "",
    selector: str = "",
    coords: Optional[tuple] = None,
    bbox: Optional[tuple] = None,
    region: str = "",
    message: str = "",
) -> None:
    """同时在 `logs/execution.log` 与 `logs/actions.log` 记录动作定位与执行闭环详情。"""
    status = "PASS" if passed else "FAIL"
    loc_parts: list[str] = []
    if level:
        loc_parts.append(f"level={level}")
    if selector:
        loc_parts.append(f"selector={selector!r}")
    if coords is not None:
        loc_parts.append(f"click_coords={coords}")
    if bbox is not None:
        loc_parts.append(f"bbox={bbox}")
    if region:
        loc_parts.append(f"region={region}")
    loc_suffix = (" | " + " ".join(loc_parts)) if loc_parts else ""
    msg_suffix = f" | msg={message}" if message else ""

    line = (
        f"[ACTION_{status}] action={action} target={target!r}"
        f"{loc_suffix} | elapsed={duration_ms:.1f}ms{msg_suffix}"
    )
    exec_logger = get_execution_logger()
    act_logger = get_action_logger(action)
    if passed:
        exec_logger.info(line)
        act_logger.info(line)
    else:
        exec_logger.warning(line)
        act_logger.warning(line)
