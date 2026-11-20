"""
ActionScreenshotter — 为单个 action 截图并标注。

被 streaming_runner / interpreter 在每个 action 执行前后调用，
不依赖 Hook 系统，直接通过 engine 和结果目录工作。

截图规则（ActionNode，即 click/type/hover 等操作）：
  before：操作前截图（_before.png），元素存在时画框，不存在时画 [NOT FOUND] 标签
  done  ：操作后截图（_done_Passed/Failed.png），显示操作结果

截图规则（AssertNode / WaitNode with element）：
  单张截图（_Passed/Failed.png），与历史行为一致（无 suffix）
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional, TYPE_CHECKING

from uita.config import NOTICE

if TYPE_CHECKING:
    from uita.core.engine.base import EngineBase
    from uita.core.hooks.base import LastAction
    from uita.core.result.collector import ActionRecord

logger = logging.getLogger("hooks.screenshot")


async def take_action_screenshot(
    engine: "EngineBase",
    last_action: "LastAction",
    screenshot_dir: str,
    step_index: int,
    action_index: int,
    failed: bool = False,
    suffix: str = "",
) -> Optional[str]:
    """为单个 action 截图、标注，返回相对于 screenshot_dir 的路径（失败返回 None）。

    Args:
        engine:         引擎实例
        last_action:    LastAction(keyword, element)
        screenshot_dir: 截图保存目录（绝对路径）
        step_index:     步骤编号（0-based）
        action_index:   步骤内 action 编号（0-based）
        failed:         是否失败（影响标注颜色）
        suffix:         文件名中插入的阶段后缀，如 "done"（ActionNode 操作后截图）；
                        空字符串保持历史命名（assert/wait 单张截图，向后兼容）

    Returns:
        相对路径，如 "step_02_01_click_done_Passed.png"，失败时 None
    """
    take_screenshot = getattr(engine, "take_screenshot", None)
    if take_screenshot is None:
        return None

    safe_kw = "".join(c if c.isalnum() else "_" for c in last_action.keyword)
    status_suffix = "Failed" if failed else "Passed"
    if suffix:
        filename = f"step_{step_index:02d}_{action_index:02d}_{safe_kw}_{suffix}_{status_suffix}.png"
    else:
        filename = f"step_{step_index:02d}_{action_index:02d}_{safe_kw}_{status_suffix}.png"
    abs_path = os.path.join(screenshot_dir, filename)

    try:
        os.makedirs(screenshot_dir, exist_ok=True)
        _diag_t0 = time.monotonic()
        logger.log(
            NOTICE,
            "[hang-trace] SCREENSHOT_CALL_START phase=%s step=%d action_index=%d action=%s engine=%s file=%s",
            suffix or "single", step_index + 1, action_index + 1,
            last_action.keyword, type(engine).__name__, filename,
        )
        await take_screenshot(path=abs_path)
        logger.log(
            NOTICE,
            "[hang-trace] SCREENSHOT_CALL_END phase=%s step=%d action_index=%d action=%s elapsed_ms=%d file=%s",
            suffix or "single", step_index + 1, action_index + 1,
            last_action.keyword, int((time.monotonic() - _diag_t0) * 1000), filename,
        )

        if not os.path.exists(abs_path) or os.path.getsize(abs_path) == 0:
            logger.debug("[screenshot] 截图文件为空: %s", abs_path)
            return None

        # 标注：after 截图只展示操作结果页面，不重新定位元素（操作后元素可能已从 DOM
        # 移除，尝试 bounding_box 会等待超时，降低用例速度）；assert/wait 等单张截图
        # 和 before 以外路径仍做标注。
        if suffix != "done":
            await _annotate(engine, last_action, abs_path, failed)

        rel = os.path.relpath(abs_path, screenshot_dir)
        logger.debug("[screenshot] action 截图: %s", rel)
        return rel

    except Exception as exc:
        logger.warning("[screenshot] action 截图失败 (step=%d act=%d): %s",
                       step_index, action_index, exc)
        return None


async def take_action_screenshot_before(
    engine: "EngineBase",
    keyword: str,
    target_name: str,
    resolve_result: Any,
    screenshot_dir: str,
    step_index: int,
    action_index: int,
    action_label: Optional[str] = None,
) -> Optional[str]:
    """操作前截图（ActionNode 专属）。

    - 元素存在（resolve_result.found=True）：截图 + 彩色框标注元素位置
    - 元素不存在且无 action_label：截图 + 红色 [NOT FOUND] 文字标签
    - action_label 不为 None：截图 + 蓝色动作文字标签（press_key 等无需 resolve 的动作）

    文件名：step_SS_AA_<action>_before.png

    Args:
        engine:         引擎实例
        keyword:        操作关键字（如 "click"）
        target_name:    目标元素名（用于 NOT FOUND 标注文字）
        resolve_result: ResolveResult 或 None
        screenshot_dir: 截图保存目录（绝对路径）
        step_index:     步骤编号
        action_index:   步骤内 action 编号
        action_label:   直接标注的动作文字（不走 resolve 的动作传此参数，如 "press_key[Escape]"）

    Returns:
        相对路径，如 "step_02_01_click_before.png"，失败时 None
    """
    take_screenshot = getattr(engine, "take_screenshot", None)
    if take_screenshot is None:
        return None

    safe_kw = "".join(c if c.isalnum() else "_" for c in keyword)
    filename = f"step_{step_index:02d}_{action_index:02d}_{safe_kw}_before.png"
    abs_path = os.path.join(screenshot_dir, filename)

    try:
        os.makedirs(screenshot_dir, exist_ok=True)
        _diag_t0 = time.monotonic()
        logger.log(
            NOTICE,
            "[hang-trace] SCREENSHOT_CALL_START phase=before step=%d action_index=%d action=%s engine=%s file=%s",
            step_index + 1, action_index + 1, keyword,
            type(engine).__name__, filename,
        )
        await take_screenshot(path=abs_path)
        logger.log(
            NOTICE,
            "[hang-trace] SCREENSHOT_CALL_END phase=before step=%d action_index=%d action=%s elapsed_ms=%d file=%s",
            step_index + 1, action_index + 1, keyword,
            int((time.monotonic() - _diag_t0) * 1000), filename,
        )

        if not os.path.exists(abs_path) or os.path.getsize(abs_path) == 0:
            logger.debug("[screenshot] before 截图文件为空: %s", abs_path)
            return None

        # 标注
        if action_label is not None:
            # 无元素 action（press_key 等）：蓝色动作文字标签
            from uita.core.hooks.builtin.annotator import annotate_action_label
            annotate_action_label(abs_path, action_label)
        else:
            found = resolve_result is not None and getattr(resolve_result, "found", False)
            if found:
                # 元素存在：画彩色框
                from uita.core.hooks.base import LastAction as _LA
                _fake = _LA(keyword=keyword, element=resolve_result.element)
                await _annotate(engine, _fake, abs_path, failed=False)
            elif target_name:
                # 元素不存在：画 [NOT FOUND] 文字标签
                from uita.core.hooks.builtin.annotator import annotate_not_found
                annotate_not_found(abs_path, target_name)

        rel = os.path.relpath(abs_path, screenshot_dir)
        logger.debug("[screenshot] before 截图: %s", rel)
        return rel

    except Exception as exc:
        logger.warning("[screenshot] before 截图失败 (step=%d act=%d): %s",
                       step_index, action_index, exc)
        return None


async def _annotate(engine: "EngineBase", last_action: "LastAction",
                    image_path: str, failed: bool) -> None:
    """获取元素坐标，调用 annotator 标注。

    2026-08-20 新增坐标句柄分支（见计划文档 20260820150000-playwright-
    screenshot-l3-thread-leak-and-annotation-coords-fix.md §3.2）：L3.8 视觉
    指纹 / L4 LLM 视觉命中的元素 ``element.raw is None`` 但
    ``element.coords``/``element.bbox`` 已由 Resolver 算好，此前代码只走
    ``engine.get_element_rect(element)``（该方法对 ``element.raw is None``
    恒返回 ``None``），导致这类元素的截图从未画框、且白白调用一次注定失败
    的 ``get_element_rect()``。现直接复用已知坐标构造 ``elem_rect``，不再
    调用 ``get_element_rect()``；``element.raw`` 非空的既有路径逐字节不变。
    """
    element = last_action.element
    if element is None:
        return

    try:
        _diag_t0 = time.monotonic()
        logger.log(
            NOTICE, "[hang-trace] ANNOTATE_START action=%s engine=%s file=%s",
            last_action.keyword, type(engine).__name__, os.path.basename(image_path),
        )

        if getattr(element, "raw", None) is None and getattr(element, "coords", None) is not None:
            # L3.8/L4 坐标句柄：直接用已知坐标构造 elem_rect，无需/无法拿 DOM rect。
            cx, cy = element.coords
            bbox = getattr(element, "bbox", None)
            if bbox is not None:
                bx, by, bw, bh = bbox
                elem_rect = (int(bx), int(by), int(bw), int(bh))
            else:
                # 无 bbox（如 L4 仅有中心点）时退化为以中心点为中心的小方框，
                # 保证仍能大致指示点击位置，而非彻底不画。
                elem_rect = (int(cx) - 10, int(cy) - 10, 20, 20)
        else:
            get_rect = getattr(engine, "get_element_rect", None)
            if get_rect is None:
                return
            elem_rect = await get_rect(element)
            if elem_rect is None:
                return

        get_win_rect = getattr(engine, "get_window_rect", None)
        win_rect = await get_win_rect() if get_win_rect else None
        is_window = getattr(engine, "_last_screenshot_is_window", True)

        from uita.core.hooks.builtin.annotator import annotate
        annotate(
            image_path=image_path,
            elem_rect=elem_rect,
            win_rect=win_rect if is_window else None,
            label=last_action.keyword,
            failed=failed,
            is_window_screenshot=is_window,
        )
        logger.log(
            NOTICE, "[hang-trace] ANNOTATE_END action=%s elapsed_ms=%d file=%s",
            last_action.keyword, int((time.monotonic() - _diag_t0) * 1000),
            os.path.basename(image_path),
        )
    except Exception as exc:
        logger.debug("[screenshot] 标注失败: %s", exc)
