"""DSH 会话：状态机 + 事件流 + 预算 + 审批闸。

状态迁移（非法迁移直接抛 :class:`InvalidTransition`，不静默忽略）：

.. code-block:: text

    IDLE ──► PREPARING ──► RUNNING ──► PAUSED ──► RUNNING
      │           │           │
      └───────────┴───────────┴──► PASSED / FAILED / ABORTED / ERROR（终态）

事件流：``seq`` 从 1 起单调递增，供控制页面按 ``since`` 增量拉取；同时落
``events.jsonl``（落盘失败只告警，不中断会话）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .evidence import EvidenceStore, new_session_id
from .policy import Mode
from .textcase import (STEP_PENDING, STEP_TERMINAL, STEP_UNKNOWN, STEP_STATUSES,
                       TextCase)

logger = logging.getLogger("uita.dsh")


class SessionState(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    RUNNING = "running"
    PAUSED = "paused"
    PASSED = "passed"
    FAILED = "failed"
    ABORTED = "aborted"
    ERROR = "error"


TERMINAL_STATES = frozenset({
    SessionState.PASSED, SessionState.FAILED,
    SessionState.ABORTED, SessionState.ERROR,
})

_ALLOWED_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.IDLE: frozenset({
        SessionState.PREPARING, SessionState.RUNNING,
        SessionState.ABORTED, SessionState.ERROR,
    }),
    SessionState.PREPARING: frozenset({
        SessionState.RUNNING, SessionState.FAILED,
        SessionState.ABORTED, SessionState.ERROR,
    }),
    SessionState.RUNNING: frozenset({
        SessionState.PAUSED, SessionState.PASSED, SessionState.FAILED,
        SessionState.ABORTED, SessionState.ERROR,
    }),
    SessionState.PAUSED: frozenset({
        SessionState.RUNNING, SessionState.ABORTED, SessionState.ERROR,
    }),
}


class InvalidTransition(Exception):
    """非法状态迁移。"""


class SessionStopped(Exception):
    """会话被人工中止（harness 内部用于跳出循环）。"""


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    reason: str = ""


@dataclass
class DshSession:
    """一次"目标驱动"会话。"""

    goal: str
    app_name: str = ""
    mode: Mode = Mode.SUPERVISED
    max_steps: int = 30
    timeout_s: int = 600
    max_heal_rounds: int = 1
    approval_timeout_s: float = 300.0
    auto_approve: bool = False
    session_id: str = ""
    root: str = ""
    case: Optional[TextCase] = None  # 文本用例（有则按步骤表编排/回报）
    ir: Any = None  # 步骤 IR（DSH v2：uita.tools.dsh.ir.IR，重放用）
    now: Any = None  # 可注入时钟（单测用）
    max_loop_repeat: int = 0  # 同一 (tool, args) 连续重复上限（0=不限）
    stall_timeout_s: float = 0.0  # 静默超时秒数（0=不限）
    artifact_path: str = ""  # 无头必交产物路径（非空且配 artifact_grace_s 时启用）
    artifact_grace_s: float = 0.0  # 产物宽限期秒数（0=不限）
    on_event: Any = None  # 流式事件回调 Callable[[dict], None]

    state: SessionState = field(default=SessionState.IDLE, init=False)
    conclusion: dict[str, Any] = field(default_factory=dict, init=False)
    steps_used: int = field(default=0, init=False)
    heal_rounds: int = field(default=0, init=False)
    assertions: dict[str, int] = field(
        default_factory=lambda: {"true": 0, "false": 0, "unknown": 0}, init=False,
    )
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
        },
        init=False,
    )
    events: list[dict[str, Any]] = field(default_factory=list, init=False)
    created_at: float = field(default=0.0, init=False)
    updated_at: float = field(default=0.0, init=False)
    store: Optional[EvidenceStore] = field(default=None, init=False)
    _seq: int = field(default=0, init=False)
    _started_at: float = field(default=0.0, init=False)
    _last_activity_at: float = field(default=0.0, init=False)
    _loop_signature: str = field(default="", init=False)
    _loop_count: int = field(default=0, init=False)
    _hints: list[str] = field(default_factory=list, init=False)
    _stop_requested: bool = field(default=False, init=False)
    _approval: Optional[dict[str, Any]] = field(default=None, init=False)
    _approval_event: Optional[asyncio.Event] = field(default=None, init=False)
    _approval_decision: Optional[ApprovalDecision] = field(default=None, init=False)
    _loop: Any = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.now = self.now or time.time
        self.mode = self.mode if isinstance(self.mode, Mode) else Mode.parse(self.mode)
        self.session_id = self.session_id or new_session_id()
        self.store = EvidenceStore(self.session_id, self.root)
        self.created_at = self.updated_at = float(self.now())
        self._started_at = self.created_at
        self._last_activity_at = self.created_at
        if self.case is not None and not self.goal:
            self.goal = self.case.name or (self.case.steps[0].action if self.case.steps else "")
        self.append_event(
            "session_created", goal=self.goal, app=self.app_name,
            mode=self.mode.value, max_steps=self.max_steps,
            case=(self.case.to_dict() if self.case is not None else None),
        )

    # ------------------------------------------------------------------
    # 状态机
    # ------------------------------------------------------------------

    def transition(self, target: SessionState, reason: str = "") -> None:
        target = SessionState(target)
        if target is self.state:
            return
        allowed = _ALLOWED_TRANSITIONS.get(self.state, frozenset())
        if target not in allowed:
            raise InvalidTransition(f"{self.state.value} → {target.value} 非法")
        previous = self.state
        self.state = target
        self.updated_at = float(self.now())
        self.append_event(
            "state", **{"from": previous.value, "to": target.value, "reason": reason},
        )

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def pause(self) -> bool:
        if self.state is not SessionState.RUNNING:
            return False
        self.transition(SessionState.PAUSED, "human pause")
        return True

    def resume(self) -> bool:
        if self.state is not SessionState.PAUSED:
            return False
        self.transition(SessionState.RUNNING, "human resume")
        return True

    def request_stop(self) -> None:
        self._stop_requested = True
        self.append_event("stop_requested")
        if self._approval is not None:
            self.resolve_approval(False, "会话被中止")

    def stop_requested(self) -> bool:
        return self._stop_requested

    # ------------------------------------------------------------------
    # 事件
    # ------------------------------------------------------------------

    def append_event(self, kind: str, **payload: Any) -> dict[str, Any]:
        self._seq += 1
        event = {
            "seq": self._seq,
            "ts": round(float(self.now()), 3),
            "kind": kind,
            **payload,
        }
        self.events.append(event)
        self.updated_at = event["ts"]
        if kind != "heartbeat":
            self._last_activity_at = event["ts"]
        if kind == "plan":
            tool_name = str(payload.get("tool") or "").strip()
            raw_args = payload.get("args") or {}
            try:
                args_sig = json.dumps(raw_args, ensure_ascii=False, sort_keys=True)
            except TypeError:
                args_sig = str(raw_args)
            sig = f"{tool_name}::{args_sig[:200]}"
            if tool_name and sig == self._loop_signature:
                self._loop_count += 1
                if self.max_loop_repeat > 1 and self._loop_count == self.max_loop_repeat - 1:
                    warn = (
                        f"警告：你已连续 {self._loop_count} 次以相同参数调用 {tool_name}，"
                        "禁止继续重复同一调用；请更换策略或调用 task_finish 收尾。"
                    )
                    if warn not in self._hints:
                        self._hints.append(warn)
            else:
                self._loop_signature = sig
                self._loop_count = 1 if tool_name else 0
        if self.store is not None:
            try:
                self.store.append_event(event)
            except Exception as exc:  # noqa: BLE001 - 证据落盘失败不中断会话
                logger.warning("dsh: 事件落盘失败 %s: %s", self.session_id, exc)
        if callable(self.on_event):
            try:
                self.on_event(event)
            except Exception as exc:  # noqa: BLE001 - 回调失败不中断会话
                logger.debug("dsh: on_event 回调异常: %s", exc)
        return event

    def events_since(self, seq: int) -> list[dict[str, Any]]:
        return [event for event in self.events if event["seq"] > int(seq)]

    # ------------------------------------------------------------------
    # 文本用例步骤
    # ------------------------------------------------------------------

    def record_case_step(self, no: int, status: str, detail: str = "",
                         evidence: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
        """记录一个用例步骤的结论（非法步骤号/状态直接抛错，不静默吞）。"""
        if self.case is None:
            raise ValueError("当前会话没有文本用例")
        step = self.case.step(no)
        if step is None:
            raise ValueError(f"用例没有第 {no} 步（共 {len(self.case.steps)} 步）")
        if status not in STEP_STATUSES:
            raise ValueError(f"非法步骤状态: {status!r}")
        if step.is_terminal() and step.status != status:
            raise ValueError(f"第 {no} 步已是终态 {step.status}，不能改成 {status}")
        step.status = status
        if detail:
            step.detail = str(detail)
        if evidence:
            step.evidence.extend(evidence)
        event = self.append_event(
            "case_step", no=step.no, status=step.status,
            action=step.action, detail=step.detail,
            evidence=list(step.evidence),
        )
        return event

    def case_progress(self) -> Optional[dict[str, Any]]:
        if self.case is None:
            return None
        return self.case.summary()

    def unresolved_steps(self) -> list[dict[str, Any]]:
        """仍未到终态的步骤（收尾时按 unknown 处理并说明）。"""
        if self.case is None:
            return []
        return [
            {"no": step.no, "action": step.action, "status": step.status}
            for step in self.case.steps if step.status not in STEP_TERMINAL
        ]

    def close_open_steps(self, status: str = STEP_UNKNOWN, reason: str = "") -> list[int]:
        """把未完成步骤收敛到终态，返回被改动的步骤号。"""
        if self.case is None:
            return []
        changed: list[int] = []
        for step in self.case.steps:
            if step.status in STEP_TERMINAL:
                continue
            step.status = status
            if reason:
                step.detail = reason
            changed.append(step.no)
            self.append_event("case_step", no=step.no, status=status,
                              action=step.action, detail=step.detail, evidence=[])
        return changed

    # ------------------------------------------------------------------
    # 预算
    # ------------------------------------------------------------------

    def bump_step(self, count: int = 1) -> int:
        self.steps_used += count
        return self.steps_used

    def record_verdict(self, verdict: str, *, tool: str = "",
                       args: Optional[dict[str, Any]] = None,
                       step_no: Optional[int] = None,
                       tool_result_seq: Optional[int] = None) -> None:
        key = verdict if verdict in self.assertions else "unknown"
        self.assertions[key] += 1
        self.append_event("assertion", verdict=key, tool=tool,
                          args=dict(args or {}), step_no=step_no,
                          tool_result_seq=tool_result_seq)

    @staticmethod
    def _assertion_source(event: dict[str, Any], sources: dict[int, dict[str, Any]]) -> Optional[dict[str, Any]]:
        source = sources.get(event.get("tool_result_seq"))
        if source and source.get("ok") is True and \
                str(source.get("tool", "")).startswith("assert_") and \
                source.get("tool") == event.get("tool") and \
                source.get("step_no") == event.get("step_no") and \
                source["seq"] < event["seq"] and \
                source.get("data", {}).get("verdict") == event.get("verdict"):
            return source
        return None

    def unresolved_false_assertions(self) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        sources = {event["seq"]: event for event in self.events
                   if event["kind"] == "tool_result"}
        for event in self.events:
            if event["kind"] != "assertion":
                continue
            if event["verdict"] != "false" and self._assertion_source(event, sources) is None:
                continue
            args = {key: value for key, value in event.get("args", {}).items()
                    if key in {"target", "expected", "attr", "match"}}
            if event.get("tool") == "assert_text":
                args.setdefault("match", "contains")
            identity = json.dumps([event.get("step_no"), event.get("tool"), args],
                                  sort_keys=True, ensure_ascii=False)
            latest[identity] = event
        return [event for event in latest.values() if event["verdict"] == "false"]

    def step_assertion_evidence(self, no: int) -> list[dict[str, Any]]:
        if self.case is None or self.case.next_pending() is None or \
                self.case.next_pending().no != no:
            raise ValueError(f"必须按顺序上报当前步骤，不能提前上报第 {no} 步")
        assertions = [event for event in self.events
                      if event["kind"] == "assertion" and event.get("step_no") == no]
        if any(event["verdict"] == "unknown" for event in assertions):
            raise ValueError(f"第 {no} 步存在 unknown，不能上报 passed")
        if any(event.get("step_no") == no
               for event in self.unresolved_false_assertions()):
            raise ValueError(f"第 {no} 步存在未解决的 false，不能上报 passed")
        sources = {event["seq"]: event for event in self.events
                   if event["kind"] == "tool_result"}
        consumed = {ref.get("tool_result_seq") for step in self.case.steps
                    for ref in step.evidence if ref.get("kind") == "assertion"}
        verified = []
        for event in assertions:
            source = self._assertion_source(event, sources)
            if event["verdict"] == "true" and source and source["seq"] not in consumed:
                verified.append({"kind": "assertion", "seq": event["seq"],
                                 "tool_result_seq": source["seq"],
                                 "tool": source["tool"], "verdict": "true"})
                consumed.add(source["seq"])
        if not verified:
            raise ValueError(f"第 {no} 步缺少真实 assert_* true 证据，不能上报 passed")
        return verified

    def step_completion_evidence(self, no: int) -> list[dict[str, Any]]:
        """供 ``report_step(status='passed')`` 使用的步骤完成证据校验。

        - 若该步已有真实 ``assert_* == true`` 证据，直接沿用 :meth:`step_assertion_evidence`；
        - 若该步为自然语言纯操作步骤（无显式 ``expected`` 且不含「确认/验证/检查/断言」语义），
          允许凭当前步内未消费的成功动作工具（``app_prepare`` / ``app_switch`` / ``act_*``）通过。
        """
        from .textcase import step_requires_assertion

        try:
            return self.step_assertion_evidence(no)
        except ValueError as exc:
            msg = str(exc)
            if "缺少真实 assert_* true 证据" not in msg or self.case is None:
                raise
            step_obj = self.case.step(no)
            if step_obj is None or step_requires_assertion(step_obj):
                raise
            consumed = {
                ref.get("tool_result_seq")
                for step in self.case.steps
                for ref in step.evidence
                if ref.get("tool_result_seq") is not None
            }
            action_ev: list[dict[str, Any]] = []
            for event in self.events:
                if event.get("kind") != "tool_result" or event.get("step_no") != no:
                    continue
                if event.get("ok") is not True or event.get("seq") in consumed:
                    continue
                tool_name = str(event.get("tool") or "")
                if tool_name in {"app_prepare", "app_switch"} or tool_name.startswith("act_"):
                    action_ev.append({
                        "kind": "action",
                        "seq": event["seq"],
                        "tool_result_seq": event["seq"],
                        "tool": tool_name,
                        "verdict": "ok",
                    })
                    consumed.add(event["seq"])
            if not action_ev:
                raise
            return action_ev

    def auto_settle_open_steps(self, detail: str = "") -> list[int]:
        """在 ``task_finish(status='passed')`` 收尾时，按时间序将已有未消费真实证据的待结步骤自动记为 passed。"""
        from .textcase import step_requires_assertion

        if self.case is None or self.assertions.get("true", 0) < 1:
            return []
        if self.unknown_count() > 0 or self.unresolved_false_assertions():
            return []

        consumed = {
            ref.get("tool_result_seq")
            for step in self.case.steps
            for ref in step.evidence
            if ref.get("tool_result_seq") is not None
        }
        sources = {
            event["seq"]: event
            for event in self.events
            if event.get("kind") == "tool_result"
        }
        true_by_source_seq: dict[int, dict[str, Any]] = {}
        for event in self.events:
            if event.get("kind") != "assertion" or event.get("verdict") != "true":
                continue
            src = self._assertion_source(event, sources)
            if src is not None:
                true_by_source_seq[src["seq"]] = {
                    "kind": "assertion",
                    "seq": event["seq"],
                    "tool_result_seq": src["seq"],
                    "tool": src["tool"],
                    "verdict": "true",
                }

        settled: list[int] = []
        last_seq = 0
        for step in self.case.steps:
            if step.is_terminal():
                for ref in step.evidence:
                    t_seq = int(ref.get("tool_result_seq") or 0)
                    if t_seq > last_seq:
                        last_seq = t_seq
                continue
            needs_assert = step_requires_assertion(step)
            matched_ev: Optional[dict[str, Any]] = None
            for event in self.events:
                if event.get("kind") != "tool_result" or event.get("ok") is not True:
                    continue
                seq = int(event.get("seq") or 0)
                if seq <= last_seq or seq in consumed:
                    continue
                tool_name = str(event.get("tool") or "")
                if seq in true_by_source_seq:
                    matched_ev = true_by_source_seq[seq]
                    break
                if not needs_assert and (
                    tool_name in {"app_prepare", "app_switch"} or tool_name.startswith("act_")
                ):
                    matched_ev = {
                        "kind": "action",
                        "seq": seq,
                        "tool_result_seq": seq,
                        "tool": tool_name,
                        "verdict": "ok",
                    }
                    break
            if matched_ev is None:
                break
            consumed.add(matched_ev["tool_result_seq"])
            last_seq = int(matched_ev["tool_result_seq"])
            self.record_case_step(
                step.no, "passed", detail or "自然语言步骤已执行验证", [matched_ev],
            )
            settled.append(step.no)
        return settled

    def unknown_count(self) -> int:
        return self.assertions["unknown"]

    def record_usage(self, raw: dict[str, Any]) -> dict[str, int]:
        """累加一次模型调用的 token 用量。"""
        if not isinstance(raw, dict):
            return dict(self.usage)
        in_tok = int(raw.get("input_tokens") or raw.get("prompt_tokens") or 0)
        out_tok = int(raw.get("output_tokens") or raw.get("completion_tokens") or 0)
        cached_tok = int(
            raw.get("cached_tokens")
            or raw.get("cache_read_input_tokens")
            or (raw.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        )
        total_tok = int(raw.get("total_tokens") or (in_tok + out_tok))
        self.usage["input_tokens"] += max(0, in_tok)
        self.usage["output_tokens"] += max(0, out_tok)
        self.usage["cached_tokens"] += max(0, cached_tok)
        self.usage["total_tokens"] += max(0, total_tok)
        return dict(self.usage)

    def artifact_ready(self) -> bool:
        """检查目标产物是否已落盘且非空。"""
        if not self.artifact_path:
            return True
        try:
            return os.path.isfile(self.artifact_path) and os.path.getsize(self.artifact_path) > 0
        except OSError:
            return False

    def can_continue(self) -> tuple[bool, str]:
        if self._stop_requested:
            return False, "人工中止"
        if self.max_loop_repeat > 0 and self._loop_count >= self.max_loop_repeat:
            return (
                False,
                f"agent-loop: 工具循环空转（{self._loop_signature} 连续重复 {self._loop_count} 次）",
            )
        if self.steps_used >= self.max_steps:
            return False, f"步数预算耗尽（max_steps={self.max_steps}）"
        now_ts = float(self.now())
        elapsed = now_ts - self._started_at
        if elapsed >= self.timeout_s:
            return False, f"时长预算耗尽（timeout_s={self.timeout_s}）"
        if self.stall_timeout_s > 0 and (now_ts - self._last_activity_at) >= self.stall_timeout_s:
            return False, f"stalled: 静默超时（stall_timeout_s={self.stall_timeout_s}）"
        if self.artifact_path and self.artifact_grace_s > 0 and elapsed >= self.artifact_grace_s:
            if not self.artifact_ready():
                return (
                    False,
                    f"no-artifact: 产物宽限期超时（{self.artifact_grace_s}s 未产出 {self.artifact_path}）",
                )
        return True, ""

    def budget_left(self) -> dict[str, Any]:
        return {
            "steps_left": max(0, self.max_steps - self.steps_used),
            "seconds_left": round(max(0.0, self.timeout_s - (float(self.now()) - self._started_at)), 1),
            "heal_rounds_left": max(0, self.max_heal_rounds - self.heal_rounds),
        }

    # ------------------------------------------------------------------
    # 人工插话
    # ------------------------------------------------------------------

    def add_hint(self, text: str) -> None:
        content = str(text or "").strip()
        if not content:
            return
        self._hints.append(content)
        self.append_event("hint", text=content)

    def drain_hints(self) -> list[str]:
        hints = list(self._hints)
        self._hints.clear()
        return hints

    # ------------------------------------------------------------------
    # 暂停/中止闸（harness 每步调用）
    # ------------------------------------------------------------------

    async def wait_if_paused(self, interval: float = 0.05) -> None:
        while self.state is SessionState.PAUSED:
            if self._stop_requested:
                raise SessionStopped("暂停中被中止")
            await asyncio.sleep(interval)
        if self._stop_requested:
            raise SessionStopped("会话被中止")

    # ------------------------------------------------------------------
    # 审批闸
    # ------------------------------------------------------------------

    def bind_loop(self, loop: Any) -> None:
        """绑定 harness 所在事件循环，供跨线程 resolve 使用。"""
        self._loop = loop

    @property
    def pending_approval(self) -> Optional[dict[str, Any]]:
        return dict(self._approval) if self._approval else None

    async def request_approval(self, *, step: int, tool: str,
                               args: dict[str, Any]) -> ApprovalDecision:
        if self.auto_approve:
            decision = ApprovalDecision(True, "auto 模式免审批")
            self.append_event("approval", step=step, tool=tool, approved=True,
                              reason=decision.reason)
            return decision
        self._approval_decision = None
        self._approval = {"step": step, "tool": tool, "args": dict(args or {})}
        self._approval_event = asyncio.Event()
        self.append_event("approval_request", step=step, tool=tool, args=dict(args or {}))
        timeout = float(self.approval_timeout_s)
        try:
            await asyncio.wait_for(self._approval_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._approval = None
            self._approval_event = None
            self.append_event("approval_timeout", step=step, tool=tool)
            return ApprovalDecision(False, f"审批超时（{timeout:.0f}s）")
        decision = self._approval_decision or ApprovalDecision(False, "无审批决定")
        self._approval = None
        self._approval_event = None
        self.append_event("approval", step=step, tool=tool,
                          approved=decision.approved, reason=decision.reason)
        return decision

    def resolve_approval(self, approved: bool, reason: str = "") -> bool:
        if self._approval is None:
            return False
        self._approval_decision = ApprovalDecision(bool(approved), str(reason or ""))
        event = self._approval_event
        if event is None:
            return True
        loop = self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is loop:
            event.set()
        elif loop is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)
        else:
            event.set()
        return True

    # ------------------------------------------------------------------
    # 结论与快照
    # ------------------------------------------------------------------

    def set_conclusion(self, state: SessionState, summary: str,
                       **extra: Any) -> dict[str, Any]:
        self.conclusion = {
            "state": SessionState(state).value,
            "summary": summary,
            "unknown_count": self.unknown_count(),
            **extra,
        }
        self.append_event("conclusion", **self.conclusion)
        return self.conclusion

    def to_ir(self, *, name: str = "", os_name: str = "",
              persist: bool = True) -> Any:
        """从会话事件流构建（或直接返回已绑定的）步骤 IR，并写入 ``plan.ir.json``。"""
        from .ir import build_ir_from_session, save_ir

        ir = self.ir or build_ir_from_session(self, name=name, os_name=os_name)
        if ir is None:
            return None
        self.ir = ir
        if persist and self.store is not None:
            try:
                ir_path = os.path.join(self.store.ensure(), "plan.ir.json")
                save_ir(ir, ir_path)
            except Exception as exc:  # noqa: BLE001 - IR 落盘失败不影响会话结论
                logger.warning("dsh: plan.ir.json 落盘失败: %s", exc)
        return ir

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "goal": self.goal,
            "app": self.app_name,
            "mode": self.mode.value,
            "state": self.state.value,
            "steps_used": self.steps_used,
            "max_steps": self.max_steps,
            "heal_rounds": self.heal_rounds,
            "assertions": dict(self.assertions),
            "usage": dict(self.usage),
            "case": self.case.to_dict() if self.case is not None else None,
            "case_progress": self.case_progress(),
            "ir": self.ir.to_dict() if self.ir is not None else None,
            "ir_revision": int(getattr(getattr(self.ir, "meta", None), "revision", 0) or 0),
            "budget": self.budget_left(),
            "conclusion": dict(self.conclusion),
            "pending_approval": self.pending_approval,
            "events": len(self.events),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "artifacts": self.store.list_artifacts() if self.store else [],
            "dir": self.store.session_dir if self.store else "",
        }
