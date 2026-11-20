"""L8（assert/verify 视觉大模型兜底）生产装配辅助。"""
from __future__ import annotations

import logging
from typing import Optional

from uita.core.actions.judgers.existence import ExistenceCheckFn, ExistenceJudger

logger = logging.getLogger(__name__)


def _is_l8_layer(fn: ExistenceCheckFn) -> bool:
    return bool(getattr(fn, "_uita_l8_layer", False))


def _strip_l8_layers(chain: list[ExistenceCheckFn]) -> list[ExistenceCheckFn]:
    return [fn for fn in chain if not _is_l8_layer(fn)]


def _build_l8_question(
    template: str, description: str, usage: str = "", step_context: str = "",
) -> str:
    tpl = template or "请判断当前界面中是否存在以下元素：{description}"
    try:
        base = tpl.format(description=description)
    except Exception:
        logger.debug("[L8] question template 格式化失败，回退默认模板")
        base = f"请判断当前界面中是否存在以下元素：{description}"

    context_lines = []
    if step_context:
        context_lines.append(f"当前测试步骤：{step_context}")
    if usage:
        context_lines.append(f"该元素用途说明：{usage}")
    if not context_lines:
        return base
    return "\n".join(context_lines) + "\n\n" + base


def build_vision_client(provider: str = "default"):
    """按 provider 构造通用视觉客户端（需要配置 DSH_API_URL / DSH_API_KEY）。"""
    from uita.tools.dsh.planner import resolve_api_key, resolve_llm_url

    if not (resolve_llm_url() and resolve_api_key()):
        raise RuntimeError("视觉 LLM 未配置 DSH_API_URL 或 DSH_API_KEY")
    from uita.tools.dsh.vision import DshVisionClient

    return DshVisionClient()


def build_vision_client_safe(provider: str = "default") -> Optional[object]:
    """构造视觉客户端，失败时静默返回 None（不抛异常）。"""
    try:
        return build_vision_client(provider)
    except Exception as exc:
        logger.info("[vision_client] 默认装配跳过（凭据/依赖未就绪，L8 视觉层不启用）: %s", exc)
        return None


def configure_l8_fallback(
    *,
    enabled: bool,
    provider: str,
    question_template: str,
):
    """根据配置启用/关闭 L8 视觉兜底，并返回用于 Resolver 的 vision_client。"""
    base_chain = _strip_l8_layers(ExistenceJudger.fallback_chain)
    if not enabled:
        ExistenceJudger.fallback_chain = base_chain
        return None

    try:
        vision_client = build_vision_client(provider)
    except Exception as exc:
        logger.warning("[L8] 视觉客户端构造失败，回退关闭态: %s", exc)
        ExistenceJudger.fallback_chain = base_chain
        return None

    ExistenceJudger.fallback_chain = base_chain
    return vision_client
