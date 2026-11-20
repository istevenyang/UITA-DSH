"""DSH v2 步骤 IR（``uita.ir/1``）：可编辑、可重放、零 LLM 的权威用例资产。

设计要点：

- IR 是**唯一权威资产**：``rosetta_map``（元素先验）与 ``.tcs``（发布形态）都是它的投影；
- 人可编辑：允许改元素名/参数/期望值，改过的步骤标 ``source="human"``，Planner 不得覆盖；
- 校验前置于执行：非法 IR 直接拒绝（``IRError``），不允许"跑起来再说"；
- 计划文档：``docs/plans/20261117200000-dsh-v2-ir-flow-plan.md`` §5。
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Optional

IR_SCHEMA = "uita.ir/1"
ACTIONS = ("open", "wait", "click", "type", "press_key", "scroll", "hover", "assert")
PHASES = ("setup", "main", "teardown")
TARGET_KINDS = ("element", "coord", "text")
EXPECT_KINDS = ("exist", "absent", "text", "value", "js")
SOURCES = ("planner", "human", "template", "explore")
OS_VALUES = ("macOS", "Windows", "Linux", "all", "universal")


class IRError(ValueError):
    """IR 非法（结构/取值/顺序），消息可直接回给界面或命令行。"""


def _as_str(value: Any, limit: int = 512) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


@dataclass
class IRTarget:
    kind: str = "element"
    name: str = ""
    anchor_id: str = ""
    platform: dict[str, str] = field(default_factory=dict)
    coord: Optional[list[float]] = None
    coord_source: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "kind": self.kind, "name": self.name, "anchor_id": self.anchor_id,
            "platform": dict(self.platform), "coord": list(self.coord) if self.coord else None,
            "coord_source": self.coord_source,
        }
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "IRTarget":
        if not isinstance(data, dict):
            raise IRError("target 必须是对象")
        coord = data.get("coord")
        parsed_coord: Optional[list[float]] = None
        if isinstance(coord, (list, tuple)) and len(coord) == 2:
            parsed_coord = [_as_float(coord[0]), _as_float(coord[1])]
        platform = data.get("platform")
        return cls(
            kind=_as_str(data.get("kind"), 16) or "element",
            name=_as_str(data.get("name"), 200),
            anchor_id=_as_str(data.get("anchor_id"), 120),
            platform={str(k): _as_str(v, 400) for k, v in platform.items()}
            if isinstance(platform, dict) else {},
            coord=parsed_coord,
            coord_source=_as_str(data.get("coord_source"), 200),
        )


@dataclass
class IRExpect:
    kind: str = "exist"
    element: str = ""
    value: Any = None
    attr: str = "value"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "element": self.element,
                "value": self.value, "attr": self.attr}

    @classmethod
    def from_dict(cls, data: Any) -> "IRExpect":
        if not isinstance(data, dict):
            raise IRError("expect 条目必须是对象")
        return cls(
            kind=_as_str(data.get("kind"), 16) or "exist",
            element=_as_str(data.get("element"), 200),
            value=data.get("value"),
            attr=_as_str(data.get("attr"), 32) or "value",
        )


@dataclass
class IRStep:
    no: int
    phase: str = "main"
    action: str = ""
    target: IRTarget = field(default_factory=IRTarget)
    args: dict[str, Any] = field(default_factory=dict)
    expect: list[IRExpect] = field(default_factory=list)
    confidence: float = 0.0
    source: str = "planner"
    human_edited: bool = False
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "no": self.no, "phase": self.phase, "action": self.action,
            "target": self.target.to_dict(), "args": dict(self.args),
            "expect": [item.to_dict() for item in self.expect],
            "confidence": self.confidence, "source": self.source,
            "human_edited": self.human_edited, "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "IRStep":
        if not isinstance(data, dict):
            raise IRError("steps 条目必须是对象")
        no = data.get("no")
        if isinstance(no, bool) or not isinstance(no, int):
            raise IRError("steps[].no 必须是整数")
        expect = data.get("expect")
        return cls(
            no=no,
            phase=_as_str(data.get("phase"), 16) or "main",
            action=_as_str(data.get("action"), 32),
            target=IRTarget.from_dict(data.get("target") or {}),
            args=dict(data.get("args") or {}),
            expect=[IRExpect.from_dict(item) for item in expect]
            if isinstance(expect, list) else [],
            confidence=_as_float(data.get("confidence")),
            source=_as_str(data.get("source"), 16) or "planner",
            human_edited=bool(data.get("human_edited", False)),
            note=_as_str(data.get("note"), 400),
        )

    def describe(self) -> str:
        label = self.target.name or self.target.anchor_id or ""
        if self.action == "assert":
            kinds = ",".join(item.kind for item in self.expect) or "-"
            return f"assert[{label or kinds}]"
        return f"{self.action}[{label}]" if label else self.action


@dataclass
class IRMeta:
    name: str = ""
    app: str = ""
    os: str = ""
    created_from: str = ""
    generator: dict[str, Any] = field(default_factory=dict)
    revision: int = 1
    locked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "app": self.app, "os": self.os,
            "created_from": self.created_from, "generator": dict(self.generator),
            "revision": self.revision, "locked": self.locked,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "IRMeta":
        if not isinstance(data, dict):
            raise IRError("meta 必须是对象")
        revision = data.get("revision", 1)
        if isinstance(revision, bool) or not isinstance(revision, int):
            revision = 1
        return cls(
            name=_as_str(data.get("name"), 120),
            app=_as_str(data.get("app"), 64),
            os=_as_str(data.get("os"), 16),
            created_from=_as_str(data.get("created_from"), 120),
            generator=dict(data.get("generator") or {}),
            revision=revision,
            locked=bool(data.get("locked", False)),
        )


@dataclass
class IR:
    meta: IRMeta
    steps: list[IRStep] = field(default_factory=list)
    schema: str = IR_SCHEMA
    preconditions: dict[str, Any] = field(default_factory=dict)

    # -- 序列化 --------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "meta": self.meta.to_dict(),
            "preconditions": dict(self.preconditions),
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "IR":
        if not isinstance(data, dict):
            raise IRError("IR 根节点必须是对象")
        steps = data.get("steps")
        ir = cls(
            meta=IRMeta.from_dict(data.get("meta") or {}),
            steps=[IRStep.from_dict(item) for item in steps] if isinstance(steps, list) else [],
            schema=_as_str(data.get("schema"), 32) or IR_SCHEMA,
            preconditions=dict(data.get("preconditions") or {}),
        )
        ir.validate()
        return ir

    # -- 校验 ----------------------------------------------------------

    def validate(self) -> None:
        if self.schema != IR_SCHEMA:
            raise IRError(f"不支持的 IR schema: {self.schema!r}（期望 {IR_SCHEMA}）")
        if not self.meta.app:
            raise IRError("meta.app 不能为空")
        if not self.meta.name:
            raise IRError("meta.name 不能为空")
        if self.meta.os and self.meta.os not in OS_VALUES:
            raise IRError(f"meta.os 只能是 {'/'.join(OS_VALUES)}（当前 {self.meta.os!r}）")
        if not self.steps:
            raise IRError("steps 不能为空")

        seen: set[int] = set()
        for index, step in enumerate(self.steps):
            where = f"steps[{index}]（no={step.no}）"
            if step.no in seen:
                raise IRError(f"{where} 步骤号重复")
            seen.add(step.no)
            if step.action not in ACTIONS:
                raise IRError(f"{where} action 只能是 {'/'.join(ACTIONS)}")
            if step.phase not in PHASES:
                raise IRError(f"{where} phase 只能是 {'/'.join(PHASES)}")
            if step.phase == "setup" and index != 0:
                raise IRError(f"{where} phase=setup 只允许出现在首步（R4）")
            if step.phase == "teardown" and index != len(self.steps) - 1:
                raise IRError(f"{where} phase=teardown 只允许出现在末步（R4）")
            if step.source not in SOURCES:
                raise IRError(f"{where} source 只能是 {'/'.join(SOURCES)}")
            if not 0.0 <= step.confidence <= 1.0:
                raise IRError(f"{where} confidence 必须在 0–1 之间")
            self._validate_target(where, step)
            if step.action == "assert" and not step.expect:
                raise IRError(f"{where} action=assert 必须至少一条 expect")
            for pos, exp in enumerate(step.expect):
                self._validate_expect(f"{where}.expect[{pos}]", exp)

    @staticmethod
    def _validate_target(where: str, step: IRStep) -> None:
        target = step.target
        if target.kind not in TARGET_KINDS:
            raise IRError(f"{where} target.kind 只能是 {'/'.join(TARGET_KINDS)}")
        if target.kind == "coord":
            if not target.coord:
                raise IRError(f"{where} target.kind=coord 必须提供 coord=[x, y]")
            if not target.coord_source:
                raise IRError(f"{where} 坐标目标必须填 coord_source（R11）")
            return
        if not target.name and step.action != "open":
            # open 无元素名 = 仅启动/切回应用本身，不定位控件
            raise IRError(f"{where} target.name 不能为空（{target.kind}）")

    @staticmethod
    def _validate_expect(where: str, exp: IRExpect) -> None:
        if exp.kind not in EXPECT_KINDS:
            raise IRError(f"{where} kind 只能是 {'/'.join(EXPECT_KINDS)}")
        if not exp.element:
            raise IRError(f"{where} element 不能为空")
        if exp.kind in {"text", "value", "js"} and exp.value is None:
            raise IRError(f"{where} kind={exp.kind} 必须提供 value")

    # -- 便捷 ----------------------------------------------------------

    def bump_revision(self) -> int:
        self.meta.revision = max(1, int(self.meta.revision)) + 1
        return self.meta.revision

    def step(self, no: int) -> Optional[IRStep]:
        for item in self.steps:
            if item.no == no:
                return item
        return None

    def to_text_case(self) -> Any:
        """转成 ``TextCase``，让会话的步骤表/门禁/报告逻辑原样复用。"""
        from .textcase import CaseStep, TextCase

        steps = []
        for item in self.steps:
            expected = "; ".join(
                f"{exp.kind}:{exp.element}" + (f"={exp.value}" if exp.value is not None else "")
                for exp in item.expect
            )
            steps.append(CaseStep(
                no=item.no,
                action=f"{item.describe()}" + (f"（{item.note}）" if item.note else ""),
                expected=expected,
            ))
        return TextCase(name=self.meta.name, preconditions=[], steps=steps, parsed=True)


def load_ir(path: str) -> IR:
    """读并校验 IR 文件；失败抛 :class:`IRError`（含路径上下文）。"""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise IRError(f"IR 文件不存在: {path}") from exc
    except (OSError, ValueError) as exc:
        raise IRError(f"IR 文件无法解析: {path}: {exc}") from exc
    try:
        return IR.from_dict(raw)
    except IRError as exc:
        raise IRError(f"{path}: {exc}") from exc


def save_ir(ir: IR, path: str) -> str:
    """原子写 IR（同目录临时文件 + ``os.replace``），返回落盘路径。"""
    ir.validate()
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ir.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(ir.to_dict(), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


_TOOL_TO_IR_ACTION = {
    "app_prepare": "open",
    "app_switch": "open",
    "act_click": "click",
    "act_type": "type",
    "act_press_key": "press_key",
    "act_scroll": "scroll",
    "act_hover": "hover",
}

_ASSERT_TOOL_TO_KIND = {
    "assert_exist": "exist",
    "assert_absent": "absent",
    "assert_text": "text",
    "assert_value": "value",
    "assert_js": "js",
}


def build_ir_from_session(session: Any, *, name: str = "",
                          os_name: str = "") -> Optional[IR]:
    """从 ReAct 会话事件流提取已成功执行的步骤，生成 ``uita.ir/1`` IR 草稿。

    仅收录 ``ok=True`` 的工具调用；``assert_*`` 优先合并到紧邻的前序动作步骤
    ``expect`` 中，若无前序动作则独立成 ``action="assert"`` 步骤。
    """
    existing = getattr(session, "ir", None)
    if isinstance(existing, IR):
        return existing

    app_name = _as_str(getattr(session, "app_name", ""), 64)
    events = list(getattr(session, "events", []) or [])
    steps: list[IRStep] = []

    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "tool_result":
            continue
        if not event.get("ok"):
            continue
        tool = str(event.get("tool") or "").strip()
        args = dict(event.get("args") or {})
        data = dict(event.get("data") or {})
        if not app_name:
            app_name = _as_str(args.get("app") or data.get("app") or "", 64)

        if tool in _TOOL_TO_IR_ACTION:
            action = _TOOL_TO_IR_ACTION[tool]
            if action == "open":
                target = IRTarget(
                    kind="element",
                    name=_as_str(args.get("app") or app_name, 200),
                )
                step_args: dict[str, Any] = {}
            elif args.get("x") is not None and args.get("y") is not None:
                target = IRTarget(
                    kind="coord",
                    coord=[_as_float(args["x"]), _as_float(args["y"])],
                    coord_source=_as_str(args.get("source") or "explore", 200),
                )
                step_args = {}
            elif action == "press_key":
                key_val = _as_str(args.get("key") or "", 64)
                if not key_val:
                    continue
                target = IRTarget(kind="element", name=key_val)
                step_args = {"key": key_val}
            elif action == "scroll":
                direction = _as_str(args.get("direction") or "down", 16)
                t_name = _as_str(args.get("target") or "window", 200)
                target = IRTarget(kind="element", name=t_name)
                step_args = {"direction": direction}
                if args.get("amount") is not None:
                    step_args["amount"] = int(args["amount"])
            else:
                t_name = _as_str(args.get("target") or "", 200)
                if not t_name:
                    continue
                target = IRTarget(kind="element", name=t_name)
                step_args = {}
                if action == "type":
                    step_args["text"] = str(args.get("text") or "")
                    if "submit" in args:
                        step_args["submit"] = bool(args["submit"])

            phase = "setup" if (not steps and action == "open") else "main"
            steps.append(IRStep(
                no=len(steps) + 1,
                phase=phase,
                action=action,
                target=target,
                args=step_args,
                expect=[],
                confidence=0.9,
                source="explore",
            ))
        elif tool in _ASSERT_TOOL_TO_KIND:
            verdict = str(data.get("verdict") or "unknown")
            if verdict != "true":
                continue
            exp_kind = _ASSERT_TOOL_TO_KIND[tool]
            elem = _as_str(args.get("target") or data.get("target") or "", 200)
            if not elem:
                continue
            val = args.get("expected")
            if exp_kind in {"text", "value", "js"} and val is None:
                val = data.get("expected") if data.get("expected") is not None else ""
            exp = IRExpect(
                kind=exp_kind,
                element=elem,
                value=val,
                attr=_as_str(args.get("attr") or "value", 32) or "value",
            )
            if steps and steps[-1].action != "assert":
                steps[-1].expect.append(exp)
            else:
                steps.append(IRStep(
                    no=len(steps) + 1,
                    phase="main",
                    action="assert",
                    target=IRTarget(kind="element", name=elem),
                    expect=[exp],
                    confidence=0.9,
                    source="explore",
                ))

    if not steps or not app_name:
        return None

    from .context import host_os_name

    resolved_os = os_name if os_name in OS_VALUES else host_os_name()
    case_name = getattr(getattr(session, "case", None), "name", "")
    ir_name = _as_str(
        name or case_name or getattr(session, "goal", "") or f"{app_name}_dsh_case",
        120,
    )
    meta = IRMeta(
        name=ir_name,
        app=app_name,
        os=resolved_os,
        created_from=_as_str(getattr(session, "session_id", ""), 120),
        generator={"kind": "explore"},
        revision=1,
    )
    ir = IR(meta=meta, steps=steps)
    ir.validate()
    return ir

