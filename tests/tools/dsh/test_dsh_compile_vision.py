"""DSH v2 闭环单测：会话导出 IR、IR 编译 .tcs、promote 入库、server 路由、多模态三契约、JSON 用例转换。"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from uita.core.dsl.parser import parse_source
from uita.tools.dsh.case_import import (
    convert_json_cases,
    convert_json_cases_text,
    safe_case_filename,
)
from uita.tools.dsh.compile import compile_ir_file, compile_ir_to_tcs, promote_ir
from uita.tools.dsh.ir import IR, IRError, build_ir_from_session
from uita.tools.dsh.planner import HttpChatClient
from uita.tools.dsh.policy import Mode
from uita.tools.dsh.server import DshServer
from uita.tools.dsh.session import DshSession
from uita.tools.dsh.textcase import parse_text_case
from uita.tools.dsh.vision import DshVisionClient


def _sample_ir_dict(*, with_coord: bool = False) -> dict:
    steps = [
        {
            "no": 1,
            "phase": "setup",
            "action": "open",
            "target": {"kind": "element", "name": "首页"},
            "note": "打开首页",
        },
        {
            "no": 2,
            "phase": "main",
            "action": "click",
            "target": (
                {"kind": "coord", "coord": [100, 200], "coord_source": "vision"}
                if with_coord
                else {"kind": "element", "name": "快速检索入口"}
            ),
            "expect": [
                {"kind": "exist", "element": "扫描进度文字"},
                {"kind": "absent", "element": "错误弹窗"},
            ],
            "note": "点击快速检索并验证",
        },
        {
            "no": 3,
            "phase": "teardown",
            "action": "open",
            "target": {"kind": "element", "name": "首页"},
            "note": "恢复首页",
        },
    ]
    return {
        "schema": "uita.ir/1",
        "meta": {"name": "sample_web_smoke", "app": "sample_web", "os": "macOS"},
        "steps": steps,
    }


def test_compile_ir_to_tcs_validates_with_dsl_parser(tmp_path):
    ir = IR.from_dict(_sample_ir_dict())
    tcs_text = compile_ir_to_tcs(ir)
    assert "@setup" in tcs_text
    assert "@teardown" in tcs_text
    assert "click[快速检索入口]" in tcs_text
    assert "assert[扫描进度文字]" in tcs_text
    assert "assert[错误弹窗, absent=true]" in tcs_text
    ast = parse_source(tcs_text)
    assert ast.name == "sample_web_smoke"
    assert ast.app == "sample_web"
    assert len(ast.setup_steps) == 1
    assert len(ast.steps) == 1
    assert len(ast.teardown_steps) == 1

    ir_file = tmp_path / "case.ir.json"
    tcs_file = tmp_path / "case.tcs"
    ir_file.write_text(json.dumps(_sample_ir_dict()), encoding="utf-8")
    _, out_path = compile_ir_file(str(ir_file), str(tcs_file))
    assert Path(out_path).is_file()


def test_compile_ir_rejects_coord_step():
    ir = IR.from_dict(_sample_ir_dict(with_coord=True))
    with pytest.raises(IRError) as excinfo:
        compile_ir_to_tcs(ir)
    assert "R11" in str(excinfo.value)


def test_promote_ir_writes_to_cases_dir_and_handles_conflict(tmp_path):
    ir = IR.from_dict(_sample_ir_dict())
    first = promote_ir(ir, cases_dir=str(tmp_path))
    second = promote_ir(ir, cases_dir=str(tmp_path), overwrite=False)
    assert Path(first).name == "sample_web_smoke.ir.json"
    assert Path(second).name == "sample_web_smoke_v2.ir.json"


def test_build_ir_from_session_extracts_steps(tmp_path):
    sess = DshSession(goal="检查设置", app_name="sample_web", mode=Mode.AUTO, root=str(tmp_path))
    sess.append_event("tool_result", tool="app_prepare", args={"app": "sample_web"}, ok=True, data={})
    sess.append_event("tool_result", tool="act_click", args={"target": "设置按钮"}, ok=True, data={})
    sess.append_event(
        "tool_result",
        tool="assert_exist",
        args={"target": "设置标题"},
        ok=True,
        data={"verdict": "true"},
    )
    ir = sess.to_ir(persist=True)
    assert ir is not None
    assert len(ir.steps) == 2
    assert ir.steps[0].action == "open"
    assert ir.steps[0].phase == "setup"
    assert ir.steps[1].action == "click"
    assert ir.steps[1].expect[0].element == "设置标题"
    assert (Path(sess.store.session_dir) / "plan.ir.json").is_file()


def test_server_ir_compile_and_promote_routes(tmp_path, monkeypatch):
    from uita import config

    monkeypatch.setattr(config, "CASES_DIR", str(tmp_path / "cases"))
    server = DshServer(host="127.0.0.1", port=8765, token="t1", mode="auto")
    headers = {"Host": "127.0.0.1:8765", "X-Uita-Dsh-Token": "t1"}

    res_c = server.handle(
        "POST",
        "/ir/compile",
        body=json.dumps({"ir": _sample_ir_dict()}).encode("utf-8"),
        headers=headers,
    )
    assert res_c.status == 200
    assert "click[快速检索入口]" in res_c.json()["tcs"]

    res_p = server.handle(
        "POST",
        "/ir/promote",
        body=json.dumps({"ir": _sample_ir_dict()}).encode("utf-8"),
        headers=headers,
    )
    assert res_p.status == 200
    assert Path(res_p.json()["path"]).is_file()


async def test_vision_contracts_and_cache(tmp_path):
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\nfake-image")
    calls = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        body = json.loads(request.content)
        text = body["messages"][0]["content"][0]["text"]
        if "定位目标元素" in text:
            reply = {"found": True, "bbox": [10, 20, 100, 40], "name_guess": "清理", "kind": "button", "confidence": 0.92}
        elif "验证断言意图" in text:
            reply = {"verdict": "true", "reason": "按钮可见", "evidence_bbox": [10, 20, 100, 40]}
        else:
            reply = {"elements": [{"name": "清理按钮", "bbox": [10, 20, 100, 40], "kind": "button", "text": "清理"}]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}}]})

    client = HttpChatClient(
        url="https://llm.example.com/v1/chat/completions",
        api_key="k",
        transport=httpx.MockTransport(_handler),
    )
    vision = DshVisionClient(client=client)

    loc1 = await vision.locate("清理按钮", str(shot))
    loc2 = await vision.locate("清理按钮", str(shot))
    assert loc1["found"] is True
    assert loc1["bbox"] == [10.0, 20.0, 100.0, 40.0]
    assert loc2 == loc1
    assert len(calls) == 1  # 同图同查询命中缓存

    ver = await vision.verify("清理按钮可见", str(shot))
    assert ver["verdict"] == "true"

    ext = await vision.extract(str(shot))
    assert len(ext) == 1 and ext[0]["name"] == "清理按钮"

    # 缺失截图不抛异常，verify 降级为 unknown
    missing_ver = await vision.verify("清理按钮可见", str(tmp_path / "nope.png"))
    assert missing_ver["verdict"] == "unknown"


def test_case_import_roundtrip_and_sanitization(tmp_path):
    raw_cases = {
        "主界面/各入口:正常": {
            "step1": {
                "step_content": "打开应用主界面\r\n并观察",
                "expect_content": "UI展示正确 ![image.png](uuid-1234)",
            },
            "step2": {
                "step_content": "点击设置详情",
                "expect_content": "设置详情窗口打开",
            },
        },
        "卸载应用": {
            "step1": {
                "step_content": "点击卸载",
                "expect_content": "卸载完成",
            },
        },
    }
    src = tmp_path / "unboxing.json"
    src.write_text(json.dumps(raw_cases, ensure_ascii=False), encoding="utf-8")
    out_dir = tmp_path / "out"

    files = convert_json_cases(str(src), str(out_dir))
    assert len(files) == 2
    for fpath in files:
        text = Path(fpath).read_text(encoding="utf-8")
        case = parse_text_case(text)
        assert case.parsed is True
        assert len(case.steps) >= 1

    first_text = Path(files[0]).read_text(encoding="utf-8")
    assert "TODO-VISUAL" in first_text
    assert "TODO-ASSERT" in first_text
    assert "uuid-1234" in first_text
    assert "![image.png]" not in first_text

    second_text = Path(files[1]).read_text(encoding="utf-8")
    assert "SKIP-" in second_text

    # 非法或不存在的 --only 报错
    with pytest.raises(KeyError):
        convert_json_cases_text(json.dumps(raw_cases), only="不存在的用例")
    assert "/" not in safe_case_filename(1, "a/b:c*d")
