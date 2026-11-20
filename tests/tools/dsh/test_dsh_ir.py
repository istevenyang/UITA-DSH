"""步骤 IR（uita.ir/1）：结构校验、序列化、转 TextCase。"""
from __future__ import annotations

import json
import os

import pytest

from uita.tools.dsh import ir as ir_mod
from uita.tools.dsh.ir import IR, IRError, IRMeta, IRStep, IRTarget, load_ir, save_ir


def _step(no: int = 1, **overrides) -> IRStep:
    data = {
        "no": no,
        "phase": "main",
        "action": "click",
        "target": {"kind": "element", "name": "首页-立即搜索"},
        "expect": [],
    }
    data.update(overrides)
    return IRStep.from_dict(data)


def _ir(**overrides) -> IR:
    payload = {
        "schema": ir_mod.IR_SCHEMA,
        "meta": {"name": "demo_case", "app": "sample_web", "os": "macOS"},
        "steps": [
            {"no": 1, "phase": "setup", "action": "wait",
             "target": {"kind": "element", "name": "首页-加载完成"},
             "expect": []},
            {"no": 2, "action": "click",
             "target": {"kind": "element", "name": "首页-立即搜索"},
             "expect": [{"kind": "exist", "element": "搜索结果面板"}]},
        ],
    }
    payload.update(overrides)
    return IR.from_dict(payload)


def test_minimal_ir_roundtrip(tmp_path):
    ir = _ir()
    path = str(tmp_path / "case.ir.json")
    save_ir(ir, path)
    loaded = load_ir(path)
    assert loaded.meta.app == "sample_web"
    assert [step.action for step in loaded.steps] == ["wait", "click"]
    assert loaded.steps[1].expect[0].element == "搜索结果面板"


def test_save_is_atomic_without_temp_leftovers(tmp_path):
    path = str(tmp_path / "case.ir.json")
    save_ir(_ir(), path)
    assert os.path.isfile(path)
    leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".ir.")]
    assert leftovers == []


def test_schema_mismatch_rejected():
    with pytest.raises(IRError, match="schema"):
        IR.from_dict({"schema": "uita.ir/9", "meta": {"name": "a", "app": "b"},
                      "steps": [{"no": 1, "action": "click",
                                 "target": {"kind": "element", "name": "x"}}]})


def test_missing_app_rejected():
    with pytest.raises(IRError, match="meta.app"):
        _ir(meta={"name": "case"})


def test_missing_name_rejected():
    with pytest.raises(IRError, match="meta.name"):
        _ir(meta={"app": "sample_web"})


def test_empty_steps_rejected():
    with pytest.raises(IRError, match="steps"):
        _ir(steps=[])


def test_bad_os_rejected():
    with pytest.raises(IRError, match="meta.os"):
        _ir(meta={"name": "c", "app": "sample_web", "os": "iOS"})


def test_universal_os_accepted():
    assert _ir(meta={"name": "c", "app": "sample_web", "os": "universal"}).meta.os == "universal"


def test_unknown_action_rejected():
    with pytest.raises(IRError, match="action"):
        _ir(steps=[{"no": 1, "action": "explode",
                    "target": {"kind": "element", "name": "x"}}])


def test_setup_phase_only_first():
    with pytest.raises(IRError, match="setup"):
        _ir(steps=[
            {"no": 1, "action": "click", "target": {"kind": "element", "name": "a"}},
            {"no": 2, "phase": "setup", "action": "click",
             "target": {"kind": "element", "name": "b"}},
        ])


def test_teardown_phase_only_last():
    with pytest.raises(IRError, match="teardown"):
        _ir(steps=[
            {"no": 1, "phase": "teardown", "action": "click",
             "target": {"kind": "element", "name": "a"}},
            {"no": 2, "action": "click", "target": {"kind": "element", "name": "b"}},
        ])


def test_duplicate_step_no_rejected():
    with pytest.raises(IRError, match="重复"):
        _ir(steps=[
            {"no": 1, "action": "click", "target": {"kind": "element", "name": "a"}},
            {"no": 1, "action": "click", "target": {"kind": "element", "name": "b"}},
        ])


def test_coord_target_requires_source():
    with pytest.raises(IRError, match="coord_source"):
        _ir(steps=[{"no": 1, "action": "click",
                    "target": {"kind": "coord", "coord": [10, 20]}}])


def test_coord_target_with_source_ok():
    ir = _ir(steps=[{"no": 1, "action": "click",
                     "target": {"kind": "coord", "coord": [10, 20],
                                "coord_source": "人工目测"}}])
    assert ir.steps[0].target.coord == [10.0, 20.0]


def test_element_target_requires_name():
    with pytest.raises(IRError, match="target.name"):
        _ir(steps=[{"no": 1, "action": "click", "target": {"kind": "element"}}])


def test_assert_action_requires_expect():
    with pytest.raises(IRError, match="expect"):
        _ir(steps=[{"no": 1, "action": "assert",
                    "target": {"kind": "element", "name": "a"}, "expect": []}])


def test_expect_text_requires_value():
    with pytest.raises(IRError, match="value"):
        _ir(steps=[{"no": 1, "action": "assert",
                    "target": {"kind": "element", "name": "a"},
                    "expect": [{"kind": "text", "element": "b"}]}])


def test_expect_unknown_kind_rejected():
    with pytest.raises(IRError, match="kind"):
        _ir(steps=[{"no": 1, "action": "assert",
                    "target": {"kind": "element", "name": "a"},
                    "expect": [{"kind": "vibes", "element": "b"}]}])


def test_confidence_out_of_range_rejected():
    with pytest.raises(IRError, match="confidence"):
        _ir(steps=[{"no": 1, "action": "click",
                    "target": {"kind": "element", "name": "a"}, "confidence": 1.5}])


def test_bump_revision():
    ir = _ir()
    assert ir.meta.revision == 1
    assert ir.bump_revision() == 2
    assert ir.meta.revision == 2


def test_human_source_and_edited_preserved():
    ir = _ir(steps=[{"no": 1, "action": "click", "source": "human",
                     "human_edited": True, "note": "人工改正元素名",
                     "target": {"kind": "element", "name": "首页-立即搜索"}}])
    data = ir.to_dict()
    assert data["steps"][0]["source"] == "human"
    assert data["steps"][0]["human_edited"] is True
    assert data["steps"][0]["note"] == "人工改正元素名"


def test_locked_flag_roundtrip():
    ir = _ir(meta={"name": "c", "app": "sample_web", "locked": True})
    assert ir.meta.locked is True
    assert ir.to_dict()["meta"]["locked"] is True


def test_to_text_case_maps_steps():
    case = _ir().to_text_case()
    assert case.name == "demo_case"
    assert [step.no for step in case.steps] == [1, 2]
    assert case.steps[1].action.startswith("click")
    assert "exist:搜索结果面板" in case.steps[1].expected
    assert case.all_passed() is False


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(IRError, match="不存在"):
        load_ir(str(tmp_path / "nope.ir.json"))


def test_load_bad_json_raises(tmp_path):
    path = tmp_path / "bad.ir.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(IRError, match="无法解析"):
        load_ir(str(path))


def test_load_invalid_semantic_reports_path(tmp_path):
    path = tmp_path / "sem.ir.json"
    path.write_text(json.dumps({"schema": ir_mod.IR_SCHEMA,
                                "meta": {"name": "c", "app": ""},
                                "steps": []}), encoding="utf-8")
    with pytest.raises(IRError) as exc:
        load_ir(str(path))
    assert "sem.ir.json" in str(exc.value)


def test_ir_meta_defaults():
    meta = IRMeta.from_dict({"name": "c", "app": "sample_web"})
    assert meta.revision == 1
    assert meta.locked is False


def test_ir_target_defaults():
    target = IRTarget.from_dict({"kind": "element", "name": "x"})
    assert target.coord is None
    assert target.platform == {}


def test_open_without_target_name_allowed():
    ir = _ir(steps=[
        {"no": 1, "phase": "setup", "action": "open",
         "target": {"kind": "element", "name": ""}},
        {"no": 2, "action": "click", "target": {"kind": "element", "name": "a"}},
    ])
    assert ir.steps[0].target.name == ""


def test_click_without_target_name_rejected():
    with pytest.raises(IRError, match="target.name"):
        _ir(steps=[{"no": 1, "action": "click",
                    "target": {"kind": "element", "name": ""}}])
