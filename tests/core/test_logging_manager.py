"""测试根目录 logs/ 分类日志分流（execution.log / actions.log / kernel.log / uita.log）。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from uita.config import LOG_DIR, ROOT_DIR, setup_logging
from uita.core.logging_manager import (
    get_action_logger,
    get_execution_logger,
    get_kernel_logger,
    log_action_execution,
    log_locate_attempt,
    log_route_decision,
)
from uita.core.resolver.resolver import ResolveLevel
from uita.core.resolver.router import ResolveStrategy


@dataclass
class _DummyElem:
    element_id: str = "#submit-btn"
    coords: tuple[float, float] | None = (120.0, 240.0)
    bbox: tuple[int, int, int, int] | None = (100, 220, 40, 40)


@dataclass
class _DummyResolveResult:
    found: bool
    level: ResolveLevel
    element: _DummyElem | None = None


def test_default_log_dir_points_to_root_logs() -> None:
    """默认 LOG_DIR 指向项目根目录 logs/。"""
    assert Path(LOG_DIR) == Path(ROOT_DIR) / "logs"


def test_categorized_logs_separate_execution_actions_and_kernel(tmp_path: Path) -> None:
    """验证执行日志、动作日志、内核日志分别落盘到独立文件，并汇总到 uita.log。"""
    paths = setup_logging(level="DEBUG", log_dir=str(tmp_path), force=True)

    # 1. 打印降级链路由决策与定位尝试（execution.log）
    log_route_decision(
        "页面主标题",
        [ResolveStrategy.MAP_EXACT, ResolveStrategy.TEXT_MATCH, ResolveStrategy.VISUAL_FP],
        chain_label="元素自定义级(定制全链)",
        reason="router_proxy 定制命中",
    )
    log_locate_attempt(
        "页面主标题",
        ResolveStrategy.MAP_EXACT,
        _DummyResolveResult(found=False, level=ResolveLevel.NOT_FOUND),
        latency_ms=1.2,
        step_idx=0,
    )
    log_locate_attempt(
        "页面主标题",
        ResolveStrategy.VISUAL_FP,
        _DummyResolveResult(found=True, level=ResolveLevel.L7_VISUAL_FP, element=_DummyElem()),
        latency_ms=14.5,
        step_idx=2,
    )

    # 2. 打印定位点击执行日志（同时进入 execution.log 与 actions.log）
    log_action_execution(
        "click",
        "页面主标题",
        passed=True,
        duration_ms=18.3,
        level="L7_VISUAL_FP",
        selector="#submit-btn",
        coords=(120.0, 240.0),
        bbox=(100, 220, 40, 40),
        region="top_right",
        message="clicked '页面主标题' via viewport coords=(120, 240)",
    )

    # 3. 打印纯动作日志与纯内核日志
    get_action_logger("type").info("typing into search box")
    get_kernel_logger("interpreter").info("kernel step scheduling")

    exec_text = Path(paths["execution"]).read_text(encoding="utf-8")
    act_text = Path(paths["actions"]).read_text(encoding="utf-8")
    kernel_text = Path(paths["kernel"]).read_text(encoding="utf-8")
    all_text = Path(paths["all"]).read_text(encoding="utf-8")

    # execution.log 包含降级链、定位命中与定位点击，不含纯 kernel 调度
    assert "[ROUTE] target='页面主标题'" in exec_text
    assert "L1_MAP_EXACT -> L3_TEXT_MATCH -> L7_VISUAL_FP" in exec_text
    assert "[LOCATE_TRY]" in exec_text
    assert "[LOCATE_HIT]" in exec_text
    assert "level=L7_VISUAL_FP" in exec_text
    assert "click_coords=(120.0, 240.0)" in exec_text
    assert "region=top_right" in exec_text
    assert "kernel step scheduling" not in exec_text

    # actions.log 包含动作执行与输入日志，不含纯 kernel 调度
    assert "[ACTION_PASS] action=click target='页面主标题'" in act_text
    assert "typing into search box" in act_text
    assert "kernel step scheduling" not in act_text

    # kernel.log 包含内核调度日志，不含动作/定位明细
    assert "kernel step scheduling" in kernel_text
    assert "[ROUTE]" not in kernel_text
    assert "typing into search box" not in kernel_text

    # uita.log 汇总全部
    assert "[ROUTE]" in all_text
    assert "typing into search box" in all_text
    assert "kernel step scheduling" in all_text
