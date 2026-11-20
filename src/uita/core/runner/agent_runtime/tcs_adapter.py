"""Read-only TCS failure adapter; no mixed-step replay or implicit model."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from uita.core.actions.base import ActionResult, ActionStatus
from uita.core.dsl.nodes import AssertNode, StepNode, WaitNode

from .planner import safe_reference
from .runtime import AgentRuntime
from .session import Config, Failure, Mode, Review, State, Verification, Verdict


_NUMERIC = {"lt", "le", "gt", "ge", "between"}


def _ast_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {"node_type": type(value).__qualname__, "fields": {
            item.name: _ast_value(getattr(value, item.name)) for item in fields(value)
        }}
    if isinstance(value, (tuple, list)):
        return {"sequence_type": type(value).__name__,
                "items": [_ast_value(item) for item in value]}
    if isinstance(value, dict):
        return {key: _ast_value(item) for key, item in value.items()}
    return value


def contract_ref(step: StepNode) -> str:
    content = json.dumps(_ast_value(step), sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


class TCSFailureReviewer:
    def __init__(
        self, *,
        runtime_factory: Callable[[dict[str, Any], Failure, Callable[[Failure], Awaitable[Verification]]], AgentRuntime] | None = None,
        verification_evidence: Callable[[dict[str, Any], ActionResult], tuple[str, ...]] | None = None,
        config: Config = Config(mode=Mode.READ_ONLY),
    ):
        self._factory = runtime_factory
        self._verification_evidence = verification_evidence
        self._config = config
        self._runtimes: dict[tuple[str, str], AgentRuntime | None] = {}
        self._contexts: dict[tuple[str, str], dict[str, Any]] = {}
        self._roots: dict[tuple[str, str], Path] = {}
        self._active = False

    async def review_action_result(self, context: dict[str, Any]) -> ActionResult:
        from .integration import review_failed_step
        return await review_failed_step(self, context)

    @staticmethod
    def _eligible(context: dict[str, Any]) -> bool:
        step = context.get("step")
        if (context.get("phase") != "main" or not isinstance(step, StepNode)
                or step.is_setup or step.is_teardown or not step.body):
            return False
        from uita.core.actions.judgers import JudgerDispatchError, needs_element
        from uita.core.actions.judgers.path_exists import is_path_like
        from uita.core.actions.judgers.var_expr import is_var_expr

        for node in step.body:
            if type(node) not in (AssertNode, WaitNode):
                return False
            if not node.target or is_path_like(node.target) or is_var_expr(node.target):
                return False
            if isinstance(node, AssertNode):
                if _NUMERIC & set(node.kwargs):
                    return False
                try:
                    if not needs_element(node.kwargs, node.target):
                        return False
                except JudgerDispatchError:
                    return False
            resolver = context.get("resolver")
            lookup = getattr(resolver, "get_js_expr", None)
            if callable(lookup):
                expression = lookup(node.target)
                if isinstance(expression, str) and expression:
                    return False
        return True

    @staticmethod
    def _evidence_present(root: Path, refs: tuple[str, ...]) -> bool:
        if not isinstance(refs, tuple) or not refs:
            return False
        for ref in refs:
            if not safe_reference(ref):
                return False
            candidate = root / ref
            if (any(path.is_symlink() for path in (candidate, *candidate.parents))
                    or not candidate.is_file() or root not in candidate.resolve().parents):
                return False
        return True

    @classmethod
    def _fingerprint(cls, root: Path, ref: str) -> tuple[Any, ...]:
        if not cls._evidence_present(root, (ref,)):
            raise ValueError("unsafe evidence artifact")
        candidate = root / ref
        stat = candidate.stat()
        if stat.st_nlink != 1 or stat.st_size == 0:
            raise ValueError("empty or shared evidence artifact")
        digest = hashlib.sha256()
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        after = candidate.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("evidence changed while being read")
        return (*identity, digest.hexdigest())

    @classmethod
    def _snapshot(cls, root: Path) -> dict[str, tuple[Any, ...]]:
        snapshot = {}
        for candidate in root.rglob("*"):
            if candidate.is_file() and not candidate.is_symlink():
                ref = candidate.relative_to(root).as_posix()
                try:
                    snapshot[ref] = cls._fingerprint(root, ref)
                except ValueError:
                    snapshot[ref] = ()
        return snapshot

    @classmethod
    def _fresh_evidence(cls, root: Path, refs: tuple[str, ...],
                        before: dict[str, tuple[Any, ...]]) -> bool:
        return (cls._evidence_present(root, refs)
                and all(cls._fingerprint(root, ref) != before.get(ref) for ref in refs))

    async def __call__(self, context: dict[str, Any]) -> Review:
        if not isinstance(self._config, Config) or self._config.mode is not Mode.READ_ONLY:
            return Review(State.BLOCKED, "TCS adapter only permits read-only review")
        if self._factory is None or self._verification_evidence is None:
            return Review(State.BLOCKED, "read-only reviewer not configured")
        if self._active or not self._eligible(context):
            return Review(State.BLOCKED, "unsafe phase, mixed step or concurrent review")
        original = context.get("action_result")
        run_step = context.get("run_step")
        collector = context.get("collector")
        if (not isinstance(original, ActionResult) or original.status is not ActionStatus.FAILED
                or not callable(run_step) or collector is None or not collector.result_dir):
            return Review(State.BLOCKED, "missing failed result or verification capability")
        root = Path(collector.result_dir).absolute()
        if any(path.is_symlink() for path in (root, *root.parents)):
            return Review(State.BLOCKED, "unsafe result directory")
        case_id = context.get("case_file") or context.get("case_name")
        if (not isinstance(case_id, str) or not case_id
                or type(context.get("step_index")) is not int or context["step_index"] < 0):
            return Review(State.BLOCKED, "case and step identity required")
        failure = Failure(str(root.parent), case_id,
                          f"main:{context['step_index']}", contract_ref(context["step"]),
                          original.message, category="unknown")
        scope = (failure.run_id, failure.case_id)
        self._active = True
        try:
            if scope in self._roots and self._roots[scope] != root:
                return Review(State.BLOCKED, "case result directory changed")
            current = self._contexts.setdefault(scope, {})
            current.clear()
            current.update(context)
            if scope not in self._runtimes:
                self._roots[scope] = root
                self._runtimes[scope] = None

                async def verify(request: Failure) -> Verification:
                    if ((request.run_id, request.case_id) != scope
                            or Path(current["collector"].result_dir).absolute() != root):
                        return Verification(Verdict.UNKNOWN, request.contract_ref,
                                            message="verification case or result directory changed")
                    step = current["step"]
                    if (request.contract_ref != contract_ref(step)
                            or request.step_id != f"main:{current['step_index']}"):
                        return Verification(Verdict.UNKNOWN, request.contract_ref,
                                            message="original contract changed")
                    frozen = deepcopy(step)
                    before = self._snapshot(root)
                    result = await current["run_step"](frozen)
                    if (not isinstance(result, ActionResult)
                            or contract_ref(current["step"]) != request.contract_ref
                            or contract_ref(step) != request.contract_ref
                            or contract_ref(frozen) != request.contract_ref):
                        return Verification(Verdict.UNKNOWN, request.contract_ref,
                                            message="original contract changed during verification")
                    refs = self._verification_evidence(current, result)
                    evidence_root = Path(current["collector"].result_dir).absolute()
                    if (evidence_root != root or contract_ref(current["step"]) != request.contract_ref
                            or not self._fresh_evidence(root, refs, before)):
                        return Verification(Verdict.UNKNOWN, request.contract_ref,
                                            message="verification evidence missing, stale or out of scope")
                    verdict = {ActionStatus.PASSED: Verdict.PASS,
                               ActionStatus.FAILED: Verdict.FAIL}.get(result.status, Verdict.UNKNOWN)
                    return Verification(verdict, request.contract_ref, refs, result.message)

                self._runtimes[scope] = self._factory(current, failure, verify)
            runtime = self._runtimes[scope]
            if (not isinstance(runtime, AgentRuntime)
                    or runtime.config.mode is not Mode.READ_ONLY):
                return Review(State.BLOCKED, "TCS adapter only permits read-only review")
            review_before = self._snapshot(root)
            result = await runtime.review(failure)
            if contract_ref(context["step"]) != failure.contract_ref:
                return Review(State.UNKNOWN, "original contract changed")
            if result.passed:
                artifact = root / "failure-review.json"
                if (Path(collector.result_dir).absolute() != root
                        or not self._fresh_evidence(root, ("failure-review.json",), review_before)
                        or collector.finalize().artifacts.get("failure_review") != ["failure-review.json"]):
                    return Review(State.UNKNOWN, "first failure review artifact missing")
                data = json.loads(artifact.read_text(encoding="utf-8"))
                if not any(record.get("failure") == asdict(failure)
                           for record in data.get("reviews", [])):
                    return Review(State.UNKNOWN, "first failure identity missing from artifact")
            return result
        except Exception:
            return Review(State.UNKNOWN, "read-only review unavailable")
        finally:
            self._active = False
