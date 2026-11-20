"""
PromptJudger — prompt= 纯视觉断言判定，跳过结构化 resolve，直接截图 + AI 视觉判定。

与 ContentJudger 的 contains= 视觉兜底不同：ContentJudger 只在元素未找到时
才兜底走视觉判定；PromptJudger 恒定跳过 resolve，每次 poll 都直接截图，用
自然语言描述整体页面/区域的期望渲染状态（无对应结构化元素时使用）。

见 docs/plans/archive/02-dsl-actions-实施纪要.md。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from .base import JudgeResult, JudgerBase, JudgerSpec

if TYPE_CHECKING:
    from uita.core.actions.base import ActionContext
    from uita.core.resolver.resolver import ResolveResult

logger = logging.getLogger(__name__)

# 完整视觉判定 prompt 模板：把用例里的 $prompt 描述拼进固定指令框架，强制
# 模型只回答 YES/NO，避免自由文本回复导致下游解析歧义。
_FULL_PROMPT_TEMPLATE = (
    "帮我检查图片，是否满足{prompt}，只需要返回YES或NO，"
    "严格遵守，不允许返回除YES或NO以外的结果"
)


class PromptJudger(JudgerBase):
    name = "prompt"

    async def check(
        self, ctx: "ActionContext", resolve_result: "Optional[ResolveResult]", spec: JudgerSpec
    ) -> JudgeResult:
        prompt = spec.kwargs.get("prompt", "")
        if not prompt:
            return JudgeResult(ok=False, reason="prompt= 断言缺少描述文本")

        vision_client = self._get_vision_client(ctx)
        if vision_client is None:
            return JudgeResult(ok=False, reason="视觉客户端未配置，无法执行 prompt= 断言")

        try:
            screenshot = await ctx.engine.take_screenshot()
        except Exception as exc:
            logger.debug("[PromptJudger] take_screenshot 异常: %s", exc)
            return JudgeResult(ok=False, reason=f"截图失败，无法执行视觉断言: {exc}")
        if not screenshot:
            return JudgeResult(ok=False, reason="截图为空，无法执行视觉断言")

        full_prompt = _FULL_PROMPT_TEMPLATE.format(prompt=prompt)
        try:
            verdict = await vision_client.judge(full_prompt, screenshot)
        except Exception as exc:
            logger.debug("[PromptJudger] vision_client.judge 异常: %s", exc)
            return JudgeResult(ok=False, reason=f"AI 视觉判定异常: {exc}")

        return JudgeResult(
            ok=verdict,
            reason=f"AI 视觉断言: {prompt!r} → {'✅ 通过' if verdict else '❌ 不通过'}",
        )

    @staticmethod
    def _get_vision_client(ctx: "ActionContext") -> Optional[object]:
        """获取视觉客户端，按优先级尝试多条路径。

        与 ContentJudger._get_vision_client 逻辑一致（三级查找），物理独立
        实现以避免跨模块耦合：
        1. ctx.resolver.has_vision_client → 取 resolver.vision_client
        2. ExistenceJudger.fallback_chain → 最后一级 L8 函数闭包中捕获的 vision_client
        3. 自行构造一个新的 DshVisionClient（最后手段）
        """
        if hasattr(ctx.resolver, "has_vision_client") and ctx.resolver.has_vision_client:
            return ctx.resolver.vision_client

        try:
            from uita.core.actions.judgers.existence import ExistenceJudger
            for fn in reversed(ExistenceJudger.fallback_chain):
                if getattr(fn, "_uita_l8_layer", False):
                    cells = getattr(fn, "__closure__", None)
                    if cells:
                        for cell in cells:
                            try:
                                val = cell.cell_contents
                                if val is not None and hasattr(val, "judge"):
                                    return val
                            except (ValueError, AttributeError):
                                continue
        except Exception:
            pass

        try:
            from uita.core.actions.judgers.l8_integration import build_vision_client
            return build_vision_client("dsh")
        except Exception:
            pass

        return None
