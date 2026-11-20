"""Validation for injected observations, without importing engines or Apps."""
from __future__ import annotations

from .planner import safe_reference
from .session import Failure, Observation, Safety


def validate_observation(failure: Failure, value: Observation) -> Observation:
    if not isinstance(value, Observation) or value.scope != failure.case_id:
        raise ValueError("observation belongs to another case")
    if not isinstance(value.safety, Safety) or type(value.product_mismatch) is not bool:
        raise ValueError("invalid safety observation")
    if not isinstance(value.summary, str) or len(value.summary) > 8192:
        raise ValueError("observation summary too large")
    if (not isinstance(value.evidence_refs, tuple) or not value.evidence_refs
            or len(value.evidence_refs) > 16
            or any(not safe_reference(ref) for ref in value.evidence_refs)):
        raise ValueError("observation requires safe evidence references")
    if (not isinstance(value.targets, tuple) or len(value.targets) > 64
            or any(not isinstance(target, str) or not target or len(target) > 1024
                   for target in value.targets)):
        raise ValueError("invalid observed targets")
    return value
