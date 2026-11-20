"""
ScreenshotHook — 注册"screenshot" hook 名称。

截图逻辑已下沉到 action 级（streaming_runner / interpreter 在每个 action 后截图），
本 Hook 保留注册以维持向后兼容（--hooks screenshot 仍可指定开启截图）。
step_end 阶段不再重复截图，仅作为开关标识。
"""
from __future__ import annotations

import logging

from uita.core.hooks.base import HookBase, HookContext
from uita.core.hooks.registry import register_hook

logger = logging.getLogger("hooks.screenshot")


@register_hook("screenshot")
class ScreenshotHook(HookBase):
    """截图 Hook（action 级）。

    截图实际由 streaming_runner / interpreter 在每个 action 执行后调用
    core.hooks.builtin.action_screenshotter.take_action_screenshot 完成。
    本 Hook 的注册使 build_hooks(["screenshot"]) 成功，
    TCSInterpreter._resolve_hook_names 据此判断是否开启截图。
    """

    @property
    def name(self) -> str:
        return "screenshot"

    # step_end / case_end 等阶段均为 no-op，截图已在 action 层完成
