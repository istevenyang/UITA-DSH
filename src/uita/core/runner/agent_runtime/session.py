"""Bounded, case-scoped contracts for embedded failure review."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Mode(str, Enum):
    OFF = "off"
    READ_ONLY = "read_only"
    RECOVERY = "recovery"


class State(str, Enum):
    NEW = "new"
    OBSERVING = "observing"
    PLANNING = "planning"
    VALIDATING = "validating"
    RECOVERING = "recovering"
    VERIFYING = "verifying"
    PASSED = "passed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Config:
    mode: Mode = Mode.OFF
    timeout_seconds: float = 60.0
    max_case_reviews: int = 2
    max_actions: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.mode, Mode):
            raise ValueError("mode must be a Mode")
        if (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds)
                or self.timeout_seconds <= 0):
            raise ValueError("timeout must be positive and finite")
        for value in (self.max_case_reviews, self.max_actions):
            if type(value) is not int or not 1 <= value <= 10:
                raise ValueError("review/action limits must be integers in [1, 10]")


@dataclass(frozen=True)
class Failure:
    run_id: str
    case_id: str
    step_id: str
    contract_ref: str
    original_message: str
    category: str = "unknown"
    phase: str = "main"

    def __post_init__(self) -> None:
        for value in (self.run_id, self.case_id, self.step_id, self.contract_ref):
            if not isinstance(value, str) or not value or len(value) > 1024:
                raise ValueError("failure identity and contract reference are required")


@dataclass(frozen=True)
class Safety:
    environment_valid: bool = False
    guard_valid: bool = False
    lock_valid: bool = False
    recovery_valid: bool = False

    @property
    def valid(self) -> bool:
        return all(value is True for value in (
            self.environment_valid, self.guard_valid, self.lock_valid,
            self.recovery_valid,
        ))


@dataclass(frozen=True)
class Observation:
    scope: str
    summary: str
    evidence_refs: tuple[str, ...]
    targets: tuple[str, ...]
    safety: Safety
    product_mismatch: bool = False


@dataclass(frozen=True)
class Verification:
    verdict: Verdict
    contract_ref: str
    evidence_refs: tuple[str, ...] = ()
    message: str = ""


@dataclass(frozen=True)
class Candidate:
    tool: str
    target: str
    parameters: dict[str, Any]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    category: str
    candidates: tuple[Candidate, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class Review:
    state: State
    reason: str

    @property
    def passed(self) -> bool:
        return self.state is State.PASSED


@dataclass
class Session:
    failure: Failure
    deadline: float
    state: State = State.NEW
    events: list[dict[str, Any]] = field(default_factory=list)

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def transition(self, state: State, **details: Any) -> None:
        allowed = {
            State.NEW: {State.OBSERVING},
            State.OBSERVING: {State.PLANNING},
            State.PLANNING: {State.VALIDATING},
            State.VALIDATING: {State.RECOVERING, State.VERIFYING},
            State.RECOVERING: {State.VERIFYING},
            State.VERIFYING: set(),
        }
        terminal = {State.PASSED, State.FAILED, State.UNKNOWN,
                    State.BLOCKED, State.CANCELLED}
        if not isinstance(state, State):
            raise ValueError("state must be a State")
        if state is State.PASSED and self.state is not State.VERIFYING:
            raise ValueError("only the original verifier may produce a passing state")
        if self.state in terminal or (state not in terminal
                                     and state not in allowed[self.state]):
            raise ValueError(f"illegal transition: {self.state.value} -> {state.value}")
        self.state = state
        self.events.append({"state": state.value, "time": time.time(), **details})
