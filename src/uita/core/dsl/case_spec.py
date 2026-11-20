"""Deterministic, side-effect-free compilation of reviewed environment case specs."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext
import re
import unicodedata


class CaseSpecError(ValueError):
    """Invalid or blocked input; no runnable source is produced."""


SCHEMA_VERSION = "1.0"
DANGEROUS_TARGETS = frozenset({"彻底删除", "格式化磁盘", "重置系统"})
_TIERS = frozenset({"red", "orange", "normal"})
_SAFE_TEXT = re.compile(r"[\w\u3400-\u9fff .:/@+\-]+", re.UNICODE)
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")
_NUMBER = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")


def _object(value: object, required: set[str], optional: set[str], label: str) -> dict:
    if not isinstance(value, dict):
        raise CaseSpecError(f"{label}: expected object")
    keys = set(value)
    if not required <= keys or keys - required - optional:
        raise CaseSpecError(f"{label}: missing or unknown keys")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CaseSpecError(f"{label}: expected nonempty trimmed text")
    if len(value) > 256 or not _SAFE_TEXT.fullmatch(value):
        raise CaseSpecError(f"{label}: unsafe text")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise CaseSpecError(f"{label}: control characters forbidden")
    return value


def _enum(value: object, allowed: set | frozenset, label: str):
    if not isinstance(value, str) or value not in allowed:
        raise CaseSpecError(f"{label}: unsupported value")
    return value


def _integer(value: object, label: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise CaseSpecError(f"{label}: expected {'positive' if positive else 'nonnegative'} integer")
    return value


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise CaseSpecError(f"{label}: expected finite positive number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise CaseSpecError(f"{label}: invalid number") from exc
    if not number.is_finite() or number <= 0 or not _NUMBER.fullmatch(str(value)):
        raise CaseSpecError(f"{label}: expected finite positive decimal")
    return number


def _list(value: object, label: str, *, nonempty: bool = True) -> list:
    if not isinstance(value, list) or (nonempty and not value):
        raise CaseSpecError(f"{label}: expected {'nonempty ' if nonempty else ''}array")
    return value


def _names(value: object, label: str, *, nonempty: bool = True) -> frozenset[str]:
    entries = [_text(item, label) for item in _list(value, label, nonempty=nonempty)]
    if len(set(entries)) != len(entries):
        raise CaseSpecError(f"{label}: duplicate entries")
    return frozenset(entries)


@dataclass(frozen=True)
class FrozenTierRule:
    ref: str
    unit_bytes: int
    red_free: Decimal
    red_pct: Decimal
    orange_free: Decimal
    orange_pct: Decimal

    @classmethod
    def from_spec(cls, value: object) -> "FrozenTierRule":
        rule = _object(value, {"ref", "confirmed", "unit_bytes", "boundary", "red_free", "red_pct", "orange_free", "orange_pct"}, set(), "rule")
        if rule["confirmed"] is not True:
            raise CaseSpecError("blocked: rule_unconfirmed")
        _enum(rule["boundary"], {"strict_lt_red_lt_orange"}, "rule.boundary")
        result = cls(
            _text(rule["ref"], "rule.ref"), _integer(rule["unit_bytes"], "rule.unit_bytes", positive=True),
            _decimal(rule["red_free"], "rule.red_free"), _decimal(rule["red_pct"], "rule.red_pct"),
            _decimal(rule["orange_free"], "rule.orange_free"), _decimal(rule["orange_pct"], "rule.orange_pct"),
        )
        if result.red_free >= result.orange_free or not result.red_pct < result.orange_pct < 100:
            raise CaseSpecError("rule: thresholds must strictly increase; percentage < 100")
        return result

    def classify(self, *, total_bytes: int, free_bytes: int) -> str:
        total = _integer(total_bytes, "total_bytes", positive=True)
        free = _integer(free_bytes, "free_bytes")
        if free > total:
            raise CaseSpecError("free_bytes exceeds total_bytes")
        if free < self.red_free * self.unit_bytes or free * 100 < self.red_pct * total:
            return "red"
        if free < self.orange_free * self.unit_bytes or free * 100 < self.orange_pct * total:
            return "orange"
        return "normal"


@dataclass(frozen=True)
class EnvironmentTarget:
    tier: str
    drive: str
    total_bytes: int
    initial_free_bytes: int
    target_free_bytes: int
    safe_min_bytes: int
    margin_bytes: int


def select_environment_target(rule: FrozenTierRule, tier: str, plan: object) -> EnvironmentTarget:
    """Select an interior point using caller-supplied observations; never inspect a disk."""
    _enum(tier, _TIERS, "tier")
    plan = _object(plan, {"drive", "total_bytes", "free_bytes", "safe_min_bytes", "margin_bytes", "chunk_bytes", "registered_elements", "registered_apps", "safe_click_targets", "runtime_verified", "runtime_unit_bytes", "runtime_rule_ref"}, set(), "environment_plan")
    if plan["runtime_verified"] is not True:
        raise CaseSpecError("blocked: runtime_capabilities_unverified")
    if _integer(plan["runtime_unit_bytes"], "runtime_unit_bytes", positive=True) != rule.unit_bytes or _text(plan["runtime_rule_ref"], "runtime_rule_ref") != rule.ref:
        raise CaseSpecError("blocked: fixture_frozen_rule_binding_mismatch")
    drive = _text(plan["drive"], "environment_plan.drive")
    if not re.fullmatch(r"[A-Za-z]", drive):
        raise CaseSpecError("environment_plan.drive: expected resolved Windows drive letter")
    total = _integer(plan["total_bytes"], "total_bytes", positive=True)
    free = _integer(plan["free_bytes"], "free_bytes")
    safe = _integer(plan["safe_min_bytes"], "safe_min_bytes", positive=True)
    margin = _integer(plan["margin_bytes"], "margin_bytes", positive=True)
    chunk = _integer(plan["chunk_bytes"], "chunk_bytes", positive=True)
    if chunk > 64 * 1024 ** 2:
        raise CaseSpecError("blocked: construction_tolerance_exceeds_supported_chunk")
    allowance = chunk + margin
    if free > total or safe < 2 * rule.unit_bytes or free < safe + allowance:
        raise CaseSpecError("blocked: unsafe_initial_capacity")
    red = max(rule.red_free * rule.unit_bytes, rule.red_pct * total / 100)
    orange = max(rule.orange_free * rule.unit_bytes, rule.orange_pct * total / 100)
    if tier == "normal":
        if free < orange + allowance:
            raise CaseSpecError("blocked: normal_tier_unreachable_without_deleting_user_data")
        target = free
    else:
        lower = Decimal(safe + allowance) if tier == "red" else max(Decimal(safe + allowance), red + allowance)
        upper = min(Decimal(free), (red if tier == "red" else orange) - allowance)
        lo = int(lower.to_integral_value(rounding=ROUND_CEILING))
        hi = int(upper.to_integral_value(rounding=ROUND_FLOOR))
        if lo >= hi:
            raise CaseSpecError("blocked: no_safe_interior_target")
        target = (lo + hi) // 2
    if rule.classify(total_bytes=total, free_bytes=target) != tier:
        raise CaseSpecError("blocked: target_tier_mismatch")
    return EnvironmentTarget(tier, drive.upper(), total, free, target, safe, margin)


def _assertion(value: object, registered: frozenset[str], label: str) -> str:
    item = _object(value, {"kind", "target", "expect"}, set(), label)
    _enum(item["kind"], {"assert"}, f"{label}.kind")
    target = _text(item["target"], f"{label}.target")
    if target not in registered:
        raise CaseSpecError(f"blocked: unregistered_element: {target}")
    expectation = _enum(item["expect"], {"exists", "absent"}, f"{label}.expect")
    return f"assert[{target}{', absent=true' if expectation == 'absent' else ''}]"


def _guards(value: object, registered: frozenset[str], label: str) -> list[str]:
    return [_assertion(item, registered, label) for item in _list(value, label, nonempty=True)]


def _number_text(value: Decimal | int) -> str:
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def _verify_environment(rule: FrozenTierRule, target: EnvironmentTarget) -> list[str]:
    lines = [
        f"set: env_free = disk_space[drive={target.drive}, mode=read, field=free_bytes]",
        f"set: env_total = disk_space[drive={target.drive}, mode=read, field=total_bytes]",
        f"set: env_pct = disk_space[drive={target.drive}, mode=read, field=free_pct]",
        "assert[${env_total}, gt=0]",
        f"assert[${{env_total}}, ge={target.total_bytes}, le={target.total_bytes}]",
        f"assert[${{env_free}}, ge={target.safe_min_bytes}, le=${{env_total}}]",
        "assert[${env_pct}, ge=0, le=100]",
    ]
    red = _number_text(rule.red_free * rule.unit_bytes)
    orange = _number_text(rule.orange_free * rule.unit_bytes)
    red_pct = _number_text(rule.red_pct)
    orange_pct = _number_text(rule.orange_pct)
    if target.tier == "red":
        lines.extend([f"if[${{env_free}}, lt={red}]", f"    assert[${{env_free}}, lt={red}]", "else:", f"    assert[${{env_pct}}, lt={red_pct}]"])
    elif target.tier == "orange":
        lines.extend([f"assert[${{env_free}}, ge={red}]", f"assert[${{env_pct}}, ge={red_pct}]", f"if[${{env_free}}, lt={orange}]", f"    assert[${{env_free}}, lt={orange}]", "else:", f"    assert[${{env_pct}}, lt={orange_pct}]"])
    else:
        lines.extend([f"assert[${{env_free}}, ge={orange}]", f"assert[${{env_pct}}, ge={orange_pct}]"])
    return lines


def compile_case_spec(spec: object, *, environment_plan: object) -> str:
    """Compile schema 1.0 to a review draft; caller must verify runtime capabilities."""
    spec = _object(spec, {"schema_version", "name", "app", "os", "source", "preconditions", "steps", "teardown", "blocked"}, set(), "case_spec")
    _enum(spec["schema_version"], {SCHEMA_VERSION}, "schema_version")
    blocked = _list(spec["blocked"], "blocked", nonempty=False)
    if blocked:
        raise CaseSpecError("blocked: " + ", ".join(_text(reason, "blocked") for reason in blocked))
    name = _text(spec["name"], "name")
    app = _text(spec["app"], "app")
    if not _IDENTIFIER.fullmatch(app):
        raise CaseSpecError("app: unsafe identifier")
    _enum(spec["os"], {"Windows"}, "os")
    source = _object(spec["source"], {"type", "ref"}, set(), "source")
    _enum(source["type"], {"text_case", "external_issue", "reviewed_spec"}, "source.type")
    source_ref = _text(source["ref"], "source.ref")
    preconditions = _list(spec["preconditions"], "preconditions")
    environments = [item for item in preconditions if isinstance(item, dict) and item.get("kind") == "env"]
    if len(environments) != 1:
        raise CaseSpecError("preconditions: exactly one case environment required")
    env = _object(environments[0], {"kind", "scope", "op", "drive", "tier", "rule_ref", "rule", "verify_at", "guard"}, set(), "environment")
    _enum(env["kind"], {"env"}, "environment.kind")
    _enum(env["scope"], {"case"}, "environment.scope")
    _enum(env["op"], {"disk_space"}, "environment.op")
    _enum(env["tier"], _TIERS, "environment.tier")
    drive = _text(env["drive"], "environment.drive")
    if drive != "system" and not re.fullmatch(r"[A-Za-z]", drive):
        raise CaseSpecError("environment.drive: expected system or drive letter")
    if _names(env["verify_at"], "environment.verify_at") != {"after_construct", "before_ui"}:
        raise CaseSpecError("environment.verify_at: both verification phases required")
    rule = FrozenTierRule.from_spec(env["rule"])
    if _text(env["rule_ref"], "environment.rule_ref") != rule.ref:
        raise CaseSpecError("blocked: frozen_rule_reference_mismatch")
    target = select_environment_target(rule, env["tier"], environment_plan)
    if drive != "system" and drive.upper() != target.drive:
        raise CaseSpecError("blocked: resolved_drive_mismatch")
    if app not in _names(environment_plan["registered_apps"], "registered_apps"):
        raise CaseSpecError("blocked: unregistered_app")
    registered = _names(environment_plan["registered_elements"], "registered_elements")
    safe_clicks = _names(environment_plan["safe_click_targets"], "safe_click_targets", nonempty=False)
    if not safe_clicks <= registered or safe_clicks & DANGEROUS_TARGETS:
        raise CaseSpecError("blocked: unsafe_click_registration")
    guard = _guards(env["guard"], registered, "environment.guard")
    additional = [_assertion(item, registered, "precondition") for item in preconditions if item is not environments[0]]
    teardown = _list(spec["teardown"], "teardown")
    if len(teardown) != 1:
        raise CaseSpecError("teardown: exactly one environment release required")
    release = _object(teardown[0], {"kind", "op", "mode"}, set(), "teardown.release")
    _enum(release["kind"], {"env"}, "teardown.kind")
    _enum(release["op"], {"disk_space"}, "teardown.op")
    _enum(release["mode"], {"release"}, "teardown.mode")
    lines = [f"name: {name}", f"app: {app}", "os: Windows", "fail_fast: true", "retryable: false", f"# source: {source_ref}", f"# frozen_rule: {rule.ref}; unit_bytes={rule.unit_bytes}; boundary=strict_lt_red_lt_orange", "", "@setup", "step: 构造并自证环境"]
    setup = list(guard) + additional
    if target.tier != "normal":
        denominator = rule.unit_bytes
        for factor in (2, 5):
            while denominator % factor == 0:
                denominator //= factor
        if target.target_free_bytes % denominator:
            raise CaseSpecError("blocked: target_unit_conversion_not_exact")
        with localcontext() as context:
            context.prec = len(str(target.target_free_bytes)) + len(str(rule.unit_bytes)) + 32
            gb = Decimal(target.target_free_bytes) / rule.unit_bytes
            if gb * rule.unit_bytes != target.target_free_bytes:
                raise CaseSpecError("blocked: target_unit_conversion_not_exact")
        setup.append(f"disk_space[drive={target.drive}, mode=fill, target_free_gb={_number_text(gb)}]")
    else:
        setup.append(f"disk_space[drive={target.drive}, mode=read, field=free_bytes]")
    setup.extend(_verify_environment(rule, target))
    lines.extend("    " + statement for statement in setup)
    for index, raw in enumerate(_list(spec["steps"], "steps"), 1):
        item = _object(raw, {"kind"}, {"action", "target", "expect", "timeout_s", "state", "guard"}, f"steps[{index}]")
        kind = _enum(item["kind"], {"action", "wait", "assert"}, "step.kind")
        body = _verify_environment(rule, target) + guard
        if kind == "assert":
            body.append(_assertion(item, registered, "step.assert"))
        elif kind == "wait":
            _object(item, {"kind", "target", "timeout_s"}, {"state"}, "step.wait")
            wait_target = _text(item["target"], "step.wait.target")
            if wait_target not in registered:
                raise CaseSpecError(f"blocked: unregistered_element: {wait_target}")
            timeout = _decimal(item["timeout_s"], "step.wait.timeout_s")
            if timeout > 300:
                raise CaseSpecError("step.wait.timeout_s: maximum 300 seconds")
            state = _enum(item.get("state", "visible"), {"visible", "gone"}, "step.wait.state")
            body.append(f"wait[{wait_target}, timeout={_number_text(timeout)}s, state={state}]")
        else:
            action = _enum(item.get("action"), {"restart_for_env", "click"}, "step.action")
            if action == "restart_for_env":
                _object(item, {"kind", "action"}, set(), "step.restart")
                body.append("restart_for_env[]")
            else:
                _object(item, {"kind", "action", "target", "guard"}, set(), "step.click")
                click_target = _text(item["target"], "step.click.target")
                if click_target in DANGEROUS_TARGETS or click_target not in safe_clicks:
                    raise CaseSpecError(f"blocked: dangerous_or_unregistered_click: {click_target}")
                body.extend(_guards(item["guard"], registered, "step.click.guard"))
                body.append(f"click[{click_target}]")
        lines.extend(["", f"step: 业务步骤{index}"])
        lines.extend("    " + statement for statement in body)
    lines.extend(["", "@teardown", "step: 释放并核验环境恢复", f"    disk_space[drive={target.drive}, mode=release]", f"    set: env_restored = disk_space[drive={target.drive}, mode=read, field=free_bytes]", f"    assert[${{env_restored}}, ge={max(target.safe_min_bytes, target.initial_free_bytes - target.margin_bytes)}]", ""])
    return "\n".join(lines)
