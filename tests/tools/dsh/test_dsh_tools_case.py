"""用例工具：路径边界（只允许 CASES_DIR 内 .tcs）与解析。"""
from __future__ import annotations

import os

import pytest

from dsh_helpers import make_ctx
from uita.tools.dsh.registry import get_tool, load_builtin_tools

_MINIMAL_TCS = """name:     dsh_probe_case
app:      demo
os:       all

step: 仅解析不执行
    wait[首页, timeout=1s]
"""


@pytest.fixture
def tools():
    load_builtin_tools()
    return get_tool


@pytest.fixture
def cases_dir(tmp_path, monkeypatch):
    import uita.config as config

    root = tmp_path / "cases"
    root.mkdir()
    (root / "probe.tcs").write_text(_MINIMAL_TCS, encoding="utf-8")
    monkeypatch.setattr(config, "CASES_DIR", str(root), raising=True)
    return root


def test_resolve_case_path_accepts_relative(cases_dir):
    from uita.tools.dsh.tools.case import resolve_case_path

    assert resolve_case_path("probe.tcs") == str(cases_dir / "probe.tcs")


def test_resolve_case_path_rejects_outside(cases_dir, tmp_path):
    from uita.tools.dsh.tools.case import resolve_case_path

    outside = tmp_path / "outside.tcs"
    outside.write_text(_MINIMAL_TCS, encoding="utf-8")
    with pytest.raises(ValueError):
        resolve_case_path(str(outside))


def test_resolve_case_path_rejects_non_tcs(cases_dir):
    from uita.tools.dsh.tools.case import resolve_case_path

    (cases_dir / "note.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError):
        resolve_case_path("note.txt")


def test_resolve_case_path_rejects_missing(cases_dir):
    from uita.tools.dsh.tools.case import resolve_case_path

    with pytest.raises(ValueError):
        resolve_case_path("nope.tcs")


def test_resolve_case_path_rejects_empty(cases_dir):
    from uita.tools.dsh.tools.case import resolve_case_path

    with pytest.raises(ValueError):
        resolve_case_path("  ")


async def test_case_parse_returns_header(tools, cases_dir):
    result = await tools("case_parse").invoke(make_ctx(), {"case_path": "probe.tcs"})
    assert result.ok
    assert result.data["name"] == "dsh_probe_case"
    assert result.data["app"] == "demo"
    assert result.data["steps"] == 1


async def test_case_parse_rejects_outside_path(tools, cases_dir, tmp_path):
    outside = tmp_path / "outside.tcs"
    outside.write_text(_MINIMAL_TCS, encoding="utf-8")
    result = await tools("case_parse").invoke(
        make_ctx(), {"case_path": str(outside)})
    assert not result.ok
    assert "CASES_DIR" in result.error or "必须位于" in result.error


async def test_case_run_rejects_bad_path_before_engine(tools, cases_dir):
    result = await tools("case_run").invoke(make_ctx(), {"case_path": "nope.tcs"})
    assert not result.ok
    assert os.path.sep in result.error or "不存在" in result.error
