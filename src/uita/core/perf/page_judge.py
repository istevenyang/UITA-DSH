"""core/perf/page_judge.py — 截图交视觉 LLM 判定「页面是否真实加载完成」。

统一复用 `DSH_API_URL` 与 `DSH_API_KEY` 配置的视觉 LLM 客户端
（``VisionLLMClient.judge(question, screenshot) -> bool``）。

三态纪律：``pass`` / ``fail`` / ``unknown``。无可用 client、超时、异常、解析失败一律
``unknown``——既不当 fail 也不当 pass，报告分列，避免把"没测到"混进结论。
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("uita")

__all__ = ["DEFAULT_QUESTION_TEMPLATE", "PageVerdict", "judge_page_loaded"]

DEFAULT_QUESTION_TEMPLATE = (
    "这是「{page_label}」页面截图。请判断该页面是否已真实加载完成——"
    "关键区域已渲染出实际内容，而不是白屏、骨架屏、加载动画或错误提示。"
    "只回答 YES 或 NO。"
)

_FALLBACK_CHAIN: tuple[str, ...] = ("dsh",)


@dataclass
class PageVerdict:
    """一次页面加载判定的结果（可直接进 JSON 产物）。"""

    verdict: str = "unknown"          # pass / fail / unknown
    judged_ms: int = 0
    provider: str = ""
    model: str = ""
    fallback_from: str = ""
    question: str = ""
    screenshot: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "judged_ms": int(self.judged_ms),
            "provider": self.provider,
            "model": self.model,
            "fallback_from": self.fallback_from,
            "question": self.question,
            "screenshot": self.screenshot,
            "attempts": list(self.attempts),
            "reason": self.reason,
        }


def _monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


def build_question(page_label: str, elements_hint: str = "", extra_rules: str = "") -> str:
    """拼判定问题：默认模板 + 关键区域提示 + 附加约束（都为空时只有默认模板）。"""
    base = DEFAULT_QUESTION_TEMPLATE.format(page_label=page_label)
    extra: list[str] = []
    if elements_hint:
        extra.append(f"该页面应出现的标志性内容：{elements_hint}")
    if extra_rules:
        extra.append(str(extra_rules))
    if not extra:
        return base
    return "\n".join(extra) + "\n\n" + base


def compress_screenshot(
    screenshot: bytes, *, max_side: int = 1280, jpeg_quality: int = 80,
) -> bytes:
    """把截图压成长边 ``max_side`` 的 JPEG，减小上传体积/写超时风险。"""
    if not screenshot or max_side <= 0:
        return screenshot
    try:
        from PIL import Image  # type: ignore
    except Exception:  # noqa: BLE001
        return screenshot
    try:
        with Image.open(io.BytesIO(screenshot)) as img:
            img = img.convert("RGB")
            width, height = img.size
            longest = max(width, height)
            if longest > max_side:
                scale = max_side / float(longest)
                img = img.resize((max(1, int(width * scale)), max(1, int(height * scale))))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=int(jpeg_quality), optimize=True)
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[perf.page_judge] 截图压缩失败，按原图送审: %s", exc)
        return screenshot


def _settings():
    import uita.config as settings

    return settings


def resolve_provider(provider: str = "") -> str:
    """provider 解析顺序：参数 → ``config.PERF_L3_PROVIDER`` → ``dsh``。"""
    explicit = (provider or "").strip().lower()
    if explicit:
        return explicit
    try:
        configured = str(getattr(_settings(), "PERF_L3_PROVIDER", "") or "").strip().lower()
    except Exception:  # noqa: BLE001
        configured = ""
    return configured or "dsh"


def _construct(provider: str):
    """按 provider 名构造客户端（统一走 DSH_API_URL / DSH_API_KEY）。"""
    from uita.core.actions.judgers.l8_integration import build_vision_client

    return build_vision_client(provider or "dsh")


def _build_client(provider: str) -> tuple[Any, str, str]:
    """构造视觉客户端，返回 ``(client, provider_used, fallback_from)``。"""
    order = [provider] + [p for p in _FALLBACK_CHAIN if p != provider]
    for index, name in enumerate(order):
        try:
            client = _construct(name)
            return client, name, ("" if index == 0 else provider)
        except Exception as exc:  # noqa: BLE001
            logger.info("[perf.page_judge] provider=%s 构造失败: %s", name, exc)
    logger.warning("[perf.page_judge] 所有 provider 均不可用（%s）", " → ".join(order))
    return None, "", ""


def _client_model(client: Any) -> str:
    """尽量读到模型名（只影响报告展示，取不到就留空，不做假设）。"""
    for path in (("_llm", "_model"), ("_model",)):
        node = client
        for attr in path:
            node = getattr(node, attr, None)
            if node is None:
                break
        if isinstance(node, str) and node:
            return node
    return ""


def _write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)


async def _save_shot(screenshot: bytes, path: str) -> str:
    """落盘送审截图（失败只告警，不影响判定）。"""
    if not path:
        return ""
    try:
        await asyncio.to_thread(_write_bytes, path, screenshot)
        return path
    except Exception as exc:  # noqa: BLE001
        logger.debug("[perf.page_judge] 截图落盘失败: %s", exc)
        return ""


async def judge_page_loaded(
    screenshot: bytes,
    *,
    page_label: str,
    elements_hint: str = "",
    extra_rules: str = "",
    provider: str = "",
    timeout_s: int = 30,
    retries: int = 1,
    max_side: int = 1280,
    jpeg_quality: int = 80,
    client: Any = None,
    shot_path: str = "",
) -> PageVerdict:
    """判定截图里的页面是否已真实加载完成。"""
    started = _monotonic_ms()
    question = build_question(page_label, elements_hint, extra_rules)
    verdict = PageVerdict(question=question)
    payload = compress_screenshot(screenshot, max_side=max_side, jpeg_quality=jpeg_quality)
    verdict.screenshot = await _save_shot(payload, shot_path)

    resolved = ""
    if client is None:
        resolved = resolve_provider(provider)
        client, used, fallback = _build_client(resolved)
        if client is None:
            verdict.verdict = "unknown"
            verdict.reason = "no_vision_client"
            verdict.judged_ms = _monotonic_ms() - started
            logger.info("[perf.page_judge] 无可用视觉客户端（provider=%s），判定记 unknown", resolved)
            return verdict
        verdict.provider = used
        verdict.fallback_from = fallback
    else:
        verdict.provider = resolved or str(provider or "")

    verdict.model = _client_model(client)
    votes: list[bool] = []
    attempts = max(1, int(retries))
    timeout = max(1, int(timeout_s))

    for index in range(attempts):
        t0 = _monotonic_ms()
        try:
            ok = await asyncio.wait_for(client.judge(question, payload), timeout=timeout)
            item = {"verdict": "pass" if bool(ok) else "fail", "ms": _monotonic_ms() - t0}
            votes.append(bool(ok))
        except asyncio.TimeoutError:
            item = {"verdict": "unknown", "ms": _monotonic_ms() - t0, "reason": "timeout"}
        except Exception as exc:  # noqa: BLE001
            item = {
                "verdict": "unknown",
                "ms": _monotonic_ms() - t0,
                "reason": f"{type(exc).__name__}: {exc}",
            }
        verdict.attempts.append(item)

    if not votes:
        verdict.verdict = "unknown"
        verdict.reason = "all_attempts_failed"
    else:
        passed = sum(1 for v in votes if v)
        verdict.verdict = "pass" if passed * 2 > len(votes) else "fail"
        verdict.reason = "majority_vote" if len(votes) > 1 else "single"

    verdict.judged_ms = _monotonic_ms() - started
    logger.info(
        "[perf.page_judge] %s → %s（provider=%s%s，%dms）",
        page_label, verdict.verdict, verdict.provider or "-",
        f"，回退自 {verdict.fallback_from}" if verdict.fallback_from else "",
        verdict.judged_ms,
    )
    return verdict
