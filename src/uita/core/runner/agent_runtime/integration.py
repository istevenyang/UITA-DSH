"""Opt-in runner port; never replay a mixed action step."""
from __future__ import annotations

from typing import Any

from uita.core.actions.base import ActionResult

from .session import Review, State


async def review_failed_step(reviewer: Any, context: dict[str, Any]) -> ActionResult:
    from .tcs_adapter import TCSFailureReviewer

    original = context["action_result"]
    if original.passed or reviewer is None:
        return original
    if not isinstance(reviewer, TCSFailureReviewer):
        return original
    try:
        review = await reviewer(context)
    except Exception:
        return original
    if isinstance(review, Review) and review.state is State.PASSED:
        return ActionResult.ok(
            "agent-reviewed: original read-only contract reverified; first failure retained",
            duration_ms=original.duration_ms,
        )
    return original
