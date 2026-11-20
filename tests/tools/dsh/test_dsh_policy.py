"""DSH 权限闸：三档模式 × 三级工具的放行矩阵。"""
from __future__ import annotations

import pytest

from uita.tools.dsh.policy import Mode, PolicyGate
from uita.tools.dsh.registry import (TIER_ACTION, TIER_READ, TIER_WRITE,
                                     DshTool, ToolResult)


def _tool(tier: str, name: str = "t") -> DshTool:
    class _T(DshTool):
        async def run(self, ctx) -> ToolResult:  # pragma: no cover - 不执行
            return ToolResult()

    instance = _T()
    instance.name = name
    instance.tier = tier
    instance.params = {}
    return instance


@pytest.mark.parametrize("tier", [TIER_ACTION, TIER_WRITE])
def test_readonly_denies_mutations(tier):
    decision = PolicyGate(Mode.READONLY).check(_tool(tier))
    assert decision.allowed is False
    assert decision.needs_approval is False
    assert "readonly" in decision.reason


def test_readonly_allows_read():
    assert PolicyGate(Mode.READONLY).check(_tool(TIER_READ)).allowed


@pytest.mark.parametrize("tier", [TIER_ACTION, TIER_WRITE])
def test_supervised_requires_approval(tier):
    gate = PolicyGate(Mode.SUPERVISED)
    pending = gate.check(_tool(tier))
    assert pending.allowed is False
    assert pending.needs_approval is True
    assert gate.check(_tool(tier), approved=True).allowed is True


def test_auto_allows_everything():
    gate = PolicyGate(Mode.AUTO)
    for tier in (TIER_READ, TIER_ACTION, TIER_WRITE):
        assert gate.check(_tool(tier)).allowed


def test_allowlist_blocks_tool():
    gate = PolicyGate(Mode.AUTO, allowlist=["other"])
    decision = gate.check(_tool(TIER_READ, name="mine"))
    assert decision.allowed is False
    assert "白名单" in decision.reason


def test_mode_parse_defaults_and_case_insensitive():
    assert Mode.parse("AUTO") is Mode.AUTO
    assert Mode.parse("") is Mode.SUPERVISED
    assert Mode.parse("weird", Mode.READONLY) is Mode.READONLY
