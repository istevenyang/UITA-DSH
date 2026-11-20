"""Embedded bounded review; injected host capabilities retain authority."""
from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any, Awaitable, Callable

from .observation import validate_observation
from .planner import parse_plan, safe_reference
from .policy import BLOCKED_CATEGORIES, PolicyGate, Tool
from .session import (Config, Failure, Mode, Observation, Review, Session, State,
                      Verification, Verdict)


class AgentRuntime:
    def __init__(
        self, *, config: Config = Config(),
        observe: Callable[[Failure], Awaitable[Observation]],
        plan: Callable[[Failure, Observation, list[dict[str, Any]], float], Awaitable[Any]],
        verify: Callable[[Failure], Awaitable[Verification]],
        evidence: Callable[[Failure, list[dict[str, Any]]], None],
        tools: tuple[Tool, ...] = (),
    ):
        self.config = config
        self._observe = observe
        self._plan = plan
        self._verify = verify
        self._evidence = evidence
        self._gate = PolicyGate(config.mode, tools)
        self._scope: tuple[str, str] | None = None
        self._deadline: float | None = None
        self._reviewed: set[str] = set()
        self._active = False

    async def _bounded(self, work: Awaitable[Any], session: Session) -> Any:
        if not session.remaining():
            if inspect.iscoroutine(work):
                work.close()
            elif isinstance(work, asyncio.Future):
                work.cancel()
            raise TimeoutError("review deadline exhausted before invocation")
        task = asyncio.ensure_future(work)
        try:
            done, _ = await asyncio.wait({task}, timeout=session.remaining())
            if not done:
                raise TimeoutError("review deadline exceeded")
            result = task.result()
            if not session.remaining():
                raise TimeoutError("review deadline exceeded")
            return result
        finally:
            if not task.done():
                task.cancel()
                task.add_done_callback(self._discard_late_result)

    @staticmethod
    def _discard_late_result(task: asyncio.Future[Any]) -> None:
        if not task.cancelled():
            task.exception()

    def _finish(self, session: Session, state: State, reason: str) -> Review:
        session.transition(state, reason=reason)
        try:
            self._evidence(session.failure, list(session.events))
            if state is State.PASSED and not session.remaining():
                session.state = State.UNKNOWN
                session.events[-1].update(state=State.UNKNOWN.value,
                                          reason="deadline exceeded while recording evidence")
                self._evidence(session.failure, list(session.events))
                return Review(State.UNKNOWN, "deadline exceeded while recording evidence")
        except Exception:
            return Review(State.UNKNOWN, "required review evidence unavailable")
        return Review(state, reason)

    async def review(self, failure: Failure) -> Review:
        if self.config.mode is Mode.OFF:
            return Review(State.BLOCKED, "review disabled")
        scope = (failure.run_id, failure.case_id)
        if self._scope is not None and self._scope != scope:
            return Review(State.BLOCKED, "runtime is scoped to one run and case")
        if self._active:
            return Review(State.BLOCKED, "recursive or concurrent review rejected")
        if (failure.step_id in self._reviewed
                or len(self._reviewed) >= self.config.max_case_reviews):
            return Review(State.BLOCKED, "review count exhausted")
        if self._deadline is None:
            self._deadline = time.monotonic() + self.config.timeout_seconds
        self._scope = scope
        self._reviewed.add(failure.step_id)
        session = Session(failure, self._deadline)
        if failure.phase != "main" or failure.category in BLOCKED_CATEGORIES:
            return self._finish(session, State.BLOCKED, "original safety or product failure")
        self._active = True
        try:
            session.transition(State.OBSERVING)
            observation = validate_observation(
                failure, await self._bounded(self._observe(failure), session),
            )
            session.events[-1].update(evidence_refs=list(observation.evidence_refs))
            if not observation.safety.valid:
                return self._finish(session, State.BLOCKED, "required safety preconditions failed")
            if observation.product_mismatch:
                return self._finish(session, State.FAILED, "confirmed product mismatch")
            session.transition(State.PLANNING)
            raw = await self._bounded(self._plan(
                failure, observation, self._gate.descriptions(observation), session.remaining(),
            ), session)
            plan = parse_plan(raw, observation, self.config.max_actions)
            session.events[-1].update(category=plan.category, evidence_refs=list(plan.evidence_refs))
            if plan.category in {"product", "unknown"}:
                return self._finish(session, State.UNKNOWN, "model diagnosis requires host confirmation")
            session.transition(State.VALIDATING)
            for candidate in plan.candidates:
                self._gate.permit(failure, observation, candidate)
            if plan.candidates:
                session.transition(State.RECOVERING)
            for candidate in plan.candidates:
                current = validate_observation(
                    failure, await self._bounded(self._observe(failure), session),
                )
                tool = self._gate.permit(failure, current, candidate)
                session.events.append({"state": State.RECOVERING.value,
                                       "tool": candidate.tool, "target": candidate.target,
                                       "evidence_refs": list(candidate.evidence_refs),
                                       "time": time.time(), "attempted": True, "executed": False})
                await self._bounded(tool.invoke(candidate.target, dict(candidate.parameters)), session)
                session.events[-1]["executed"] = True
            current = validate_observation(
                failure, await self._bounded(self._observe(failure), session),
            )
            if not current.safety.valid:
                return self._finish(session, State.BLOCKED, "preconditions changed before verification")
            if current.product_mismatch:
                return self._finish(session, State.FAILED, "confirmed product mismatch")
            session.transition(State.VERIFYING)
            verification = await self._bounded(self._verify(failure), session)
            if (not isinstance(verification, Verification)
                    or verification.contract_ref != failure.contract_ref
                    or not isinstance(verification.verdict, Verdict)):
                return self._finish(session, State.UNKNOWN, "original verifier contract mismatch")
            if (not isinstance(verification.evidence_refs, tuple)
                    or not verification.evidence_refs
                    or any(not safe_reference(ref) for ref in verification.evidence_refs)):
                return self._finish(session, State.UNKNOWN, "verification evidence missing")
            after = validate_observation(
                failure, await self._bounded(self._observe(failure), session),
            )
            if not after.safety.valid:
                return self._finish(session, State.BLOCKED, "preconditions changed during verification")
            if after.product_mismatch:
                return self._finish(session, State.FAILED, "confirmed product mismatch")
            session.events[-1].update(verdict=verification.verdict.value,
                                      evidence_refs=list(verification.evidence_refs))
            state = {Verdict.PASS: State.PASSED, Verdict.FAIL: State.FAILED,
                     Verdict.UNKNOWN: State.UNKNOWN}[verification.verdict]
            return self._finish(session, state, "original contract reverified")
        except asyncio.CancelledError:
            self._finish(session, State.CANCELLED, "review cancelled")
            raise
        except ValueError:
            return self._finish(session, State.BLOCKED, "invalid proposal or host gate rejected")
        except Exception as exc:
            return self._finish(session, State.UNKNOWN, type(exc).__name__)
        finally:
            self._active = False
