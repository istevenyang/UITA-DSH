"""Hook 七阶段系统 — 基础类型定义。

生命周期阶段：
    batch_start → [ case_start → [ step_start → step_end ]×N → case_end
                    → case_result_ready ]×M → batch_end

向后兼容：
    - 现有 4 阶段 Hook（case_start/end + step_start/end）无需改动即可工作
    - HookContext 新增字段均有默认值
    - report_dir 保留原名，output_dir 为别名 property
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class LastAction:
    """记录 step 最后一个操作，供截图标注使用。"""
    keyword: str          # "click" / "type" / "assert" / "wait" / "hover" ...
    element: Any = None   # ElementRef 或 None（纯计时 wait 无元素）


@dataclass
class HookContext:
    """传递给 Hook 各阶段的上下文。

    新增字段均有默认值，向后兼容。
    report_dir 与 output_dir 等价（output_dir 为 property 别名）。
    """
    # ── 现有字段（向后兼容）──
    case_name: str = ""
    case_file: str = ""
    report_dir: str = ""
    step_index: int = 0
    step_name: str = ""
    step_result: Optional[Any] = None   # StepResult（StreamingRunner 中注入）
    case_result: Optional[Any] = None   # CaseResult（case_end 时注入）
    engine: Optional[Any] = None        # EngineBase 实例（可用于截图）
    last_action: Optional[Any] = None   # LastAction(keyword, element)（截图标注用）
    extra: dict = field(default_factory=dict)

    # ── 新增字段（batch 级 + 状态隔离）──
    stage: str = ""                               # 当前阶段名（emit 时自动赋值）
    engines: dict = field(default_factory=dict)   # 多引擎引用（key=engine_name）
    entries: list = field(default_factory=list)    # batch 级用例结果列表（batch_end 可用）
    run_dir: str = ""                             # batch 运行目录
    batch_id: str = ""                            # 批次 ID
    html_report: str = ""                         # 聚合报告站点 URL
    _state: dict = field(default_factory=dict)    # Hook 间状态隔离

    @property
    def output_dir(self) -> str:
        """report_dir 的别名（向后兼容）。"""
        return self.report_dir

    def state(self, hook_name: str) -> dict:
        """获取指定 Hook 的私有状态空间（before 写入 / after 读取）。"""
        return self._state.setdefault(hook_name, {})


class HookBase(ABC):
    """所有 Hook 的抽象基类。

    只需覆盖需要的阶段方法，其余阶段默认 no-op。
    新增 on_batch_start / on_batch_end 默认 no-op，现有 Hook 子类无需改动。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Hook 注册名（与 @register_hook 的 name 参数对应）。"""
        ...

    async def on_batch_start(self, ctx: HookContext) -> None:
        pass

    async def on_batch_end(self, ctx: HookContext) -> None:
        pass

    async def on_case_start(self, ctx: HookContext) -> None:
        pass

    async def on_step_start(self, ctx: HookContext) -> None:
        pass

    async def on_step_end(self, ctx: HookContext) -> None:
        pass

    async def on_case_end(self, ctx: HookContext) -> None:
        pass

    async def on_case_result_ready(self, ctx: HookContext) -> None:
        """单条用例 result.json 已落盘，可安全上报。"""
        pass

class HookManager:
    """管理一组 Hook 实例，统一触发各阶段。

    end 类阶段（batch_end / case_end / step_end）按注册逆序执行（洋葱模型）。
    """

    def __init__(self, hooks: list[HookBase]) -> None:
        self._hooks = list(hooks)

    @property
    def hooks(self) -> list[HookBase]:
        return self._hooks

    async def emit(self, stage: str, ctx: HookContext) -> None:
        """触发指定阶段，异常安全降级（不中断用例执行）。"""
        import logging
        logger = logging.getLogger("hooks")

        ctx.stage = stage
        method_name = f"on_{stage}"

        # end 类阶段反序执行（洋葱模型）
        hooks = reversed(self._hooks) if stage.endswith("_end") else self._hooks

        for hook in hooks:
            fn = getattr(hook, method_name, None)
            if fn is None:
                continue
            try:
                await fn(ctx)
            except Exception as exc:
                logger.warning("[HookManager] %s.%s 异常: %s", hook.name, stage, exc)
