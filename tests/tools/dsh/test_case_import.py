"""JSON 导入：纯转换、断言保真、逐步分级、CLI 与输出路径安全。"""
from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from uita.tools.dsh import case_import
from uita.tools.dsh.case_import import (CaseImportError, METADATA_PREFIX,
                                        METADATA_SUFFIX, classify_step,
                                        convert_cases, load_cases, main,
                                        safe_filename, write_cases)
from uita.tools.dsh.textcase import parse_text_case


def step(action="点击首页", expected="首页正常"):
    return {"step_content": action, "expect_content": expected}


def source_file(tmp_path, data):
    path = tmp_path / "source.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_convert_two_cases_three_steps_is_pure_and_deterministic():
    data = {"首页": {"step1": step(), "step2": step("点击设置", "设置页面正常")},
            "版本": {"step1": step("检查版本号", "版本号正常")}}
    original = json.dumps(data, ensure_ascii=False)
    cases = convert_cases(data)
    assert len(cases) == 2
    assert cases == convert_cases(data)
    assert original == json.dumps(data, ensure_ascii=False)
    assert [case.filename for case in cases] == ["01-首页.md", "02-版本.md"]
    assert sum(len(parse_text_case(case.text).steps) for case in cases) == 3
    assert all("用例名称：" in case.text and "1. " in case.text and "预期：" in case.text for case in cases)
    assert cases[0].to_dict()["filename"] == "01-首页.md"


def test_flatten_images_keep_identifiers_and_no_guessed_assertions():
    data = {"页面": {"step1": step("点击\r\n   首页 ![操作](<action-uuid>)",
                                   "UI\n 展示  正确 ![image.png](image-uuid)\r\n 无异常")}}
    case = convert_cases(data)[0]
    parsed = parse_text_case(case.text)
    assert parsed.parsed is True
    assert parsed.steps[0].action == "点击 首页"
    assert parsed.steps[0].expected == "UI 展示 正确 无异常"
    assert "![" not in case.text
    assert "TODO-VISUAL" in case.text and "TODO-ASSERT" in case.text
    assert case.metadata["steps"][0]["todo_visual"] == ["action-uuid", "image-uuid"]
    assert "assert_exist[" not in case.text
    assert "<!--" not in parsed.steps[0].action
    assert "<!--" not in parsed.steps[0].expected


@pytest.mark.parametrize("expected", ["", "   ", "\r\n", "![image.png](uuid)"])
def test_empty_expected_stays_empty_and_does_not_consume_following_step(expected):
    case = convert_cases({"空预期": {"step1": step(expected=expected),
                                       "step2": step("点击设置", "正常打开设置")}})[0]
    parsed = parse_text_case(case.text)
    assert parsed.parsed is True
    assert len(parsed.steps) == 2
    assert parsed.steps[0].expected == ""
    assert parsed.steps[1].expected == "正常打开设置"
    assert case.metadata["steps"][0]["todo_assert"] is True
    assert "TODO-ASSERT step1" in case.text


@pytest.mark.parametrize("expected", ["页面打开", "tips消失", "版本号正常", "开关正常开启和关闭", "正常关闭主界面", "assert_exist[原文]"])
def test_all_unbound_expected_preserved_and_todo_assert(expected):
    case = convert_cases({"原预期": {"step1": step(expected=expected)}})[0]
    assert parse_text_case(case.text).steps[0].expected == expected
    assert case.metadata["steps"][0]["todo_assert"] is True


def test_source_ordinals_sort_numerically_and_only_keeps_source_case_number():
    data = {"第一条": {"step1": step()},
            "第二条": {"step3": step("点击设置", "预期三"), "step1": step("点击首页", "预期一")}}
    case = convert_cases(data, only=["第二条", "第二条"])[0]
    assert case.filename.startswith("02-")
    assert "1. 点击首页" in case.text and "3. 点击设置" in case.text
    assert [item["source_step_no"] for item in case.metadata["steps"]] == [1, 3]
    assert [item.expected for item in parse_text_case(case.text).steps] == ["预期一", "预期三"]


@pytest.mark.parametrize("name,action,expected,level", [
    ("版本号检查+文件版本号检查", "安装完成检查版本号", "版本号正常", "L1"),
    ("版本号检查+文件版本号检查", "安装后检查4个模块文件版本与安装版本是否一致", "模块与安装版本一致", "L1"),
    ("主界面各入口正常", "点击-设置详情", "正常打开设置详情页面", "L2"),
    ("主界面各入口正常", "点击-检查更新 点击-规则库-提前更新", "规则库升级正常", "L3"),
    ("设置正常", "设置中心-开启权限变更开关/关闭权限变更开关", "可新增删除文件", "L4"),
    ("设置正常", "操作任意开关", "正常操作", "L3"),
    ("软件升级提醒tips", "点击【立即更新】", "更新成功", "L3"),
    ("软件下载、更新、卸载功能正常", "分类页点击安装 更新页点击更新 卸载页点击卸载", "正常", "L3"),
    ("发起远程正常", "输入设备码", "正常发起远程", "L3"),
    ("接受远程正常", "点击-开启", "出现三种方式", "L3"),
    ("卸载应用", "检查卸载页面及流程", "成功卸载", "L4"),
    ("数据归档与迁移正常", "打开数据归档，点击扫描", "扫描完成", "L4"),
    ("数据迁移与还原正常", "选择数据项，点击还原", "正常", "L4"),
    ("系统扫描流程正常", "扫描完成，点击立即优化", "进入优化中页面", "L3"),
    ("系统扫描流程正常", "等待优化完成", "展示优化结果", "L3"),
    ("安全检查流程正常", "勾选异常项，点击立即处理", "修复异常项", "L3"),
    ("批量任务扫描与处理正常", "勾选部分批量删除", "正常处理", "L4"),
    ("截图、录屏、截全屏正常", "点击-录屏", "正常录屏", "L5"),
    ("游戏页面显示正常", "点击各tab页", "游戏和视频显示正常", "L5"),
    ("未知操作", "做点事情", "正常", "L3"),
    ("任意工具", "点击任意指令打开", "正常", "L3"),
    ("智能客服", "上传图片", "正常回复", "L3"),
    ("账号中心", "完成任一一个任务", "成长值增加", "L3"),
])
def test_step_safety(name, action, expected, level):
    actual, reasons, manual = classify_step(name, action, expected)
    assert actual == level
    assert reasons
    if level in {"L3", "L4", "L5"}:
        assert manual is True


def test_high_risk_step_controls_case_even_with_visual_step():
    case = convert_cases({"混合风险": {"step1": step("点击-截图", "正常截图"),
                                        "step2": step("点击立即更新", "更新成功")}})[0]
    assert [item["safety_level"] for item in case.metadata["steps"]] == ["L5", "L3"]
    assert case.metadata["safety_level"] == "L3"
    assert case.metadata["skip"] is True
    assert "SKIP-L3 step2" in case.text
    assert "SKIP-L5" not in case.text


def test_keep_side_effects_preserves_todo_gates_and_all_cases():
    data = {"只读": {"step1": step("检查版本", "版本正常")},
            "卸载应用": {"step1": step("卸载应用", "卸载正常")}}
    default = convert_cases(data)
    kept = convert_cases(data, keep_side_effects=True)
    assert len(default) == len(kept) == 2
    assert default[1].metadata["skip"] is True
    assert kept[1].metadata["skip"] is False
    assert "SKIP-L4" not in kept[1].text
    assert "TODO-ASSERT" in kept[1].text and "TODO-REVIEW" in kept[1].text
    assert kept[1].metadata["steps"][0]["requires_manual_review"] is True


def test_image_only_action_is_blocked_and_original_uuid_kept():
    case = convert_cases({"图操作": {"step1": step("![image](<uuid>)", "")}})[0]
    assert case.metadata["skip"] is True
    assert case.metadata["steps"][0]["todo_visual"] == ["uuid"]
    assert parse_text_case(case.text).steps[0].expected == ""


def test_machine_metadata_cannot_break_html_comment():
    name = "入口 --><!-- evil -->"
    case = convert_cases({name: {"step1": step(expected="预期 --><!-- 内容")}})[0]
    first = case.text.splitlines()[0]
    assert first.startswith(METADATA_PREFIX)
    assert first.count("-->") == 1
    metadata = json.loads(first[len(METADATA_PREFIX):-len(METADATA_SUFFIX)])
    assert metadata == case.metadata
    assert metadata["name"] == name


@pytest.mark.parametrize("data", [None, [], {}, {"": {"step1": step()}}, {"用例": {}},
    {"用例": {"step0": step()}}, {"用例": {"step01": step()}}, {"用例": {"step100": step()}},
    {"用例": {"step1": []}}, {"用例": {"step1": {"step_content": "操作"}}},
    {"用例": {"step1": {**step(), "extra": "不允许"}}},
    {"用例": {"step1": step(None)}}, {"用例": {"step1": step("")}},
    {"用例": {"step1": step(expected=None)}}, {"用例": {"step1": step("点击\x00首页")}},
])
def test_invalid_schema_rejected(data):
    with pytest.raises(CaseImportError):
        convert_cases(data)


@pytest.mark.parametrize("only", [["不存在"], [123], "首页"])
def test_invalid_only_rejected(only):
    with pytest.raises(CaseImportError):
        convert_cases({"首页": {"step1": step()}}, only=only)


def test_invalid_unselected_case_validated_before_conversion():
    with pytest.raises(CaseImportError):
        convert_cases({"首页": {"step1": step()}, "坏用例": {}}, only=["首页"])


def test_parse_selfcheck_failure_rejected(monkeypatch):
    monkeypatch.setattr(case_import, "parse_text_case", lambda text: parse_text_case("未识别文本"))
    with pytest.raises(CaseImportError, match="自检失败"):
        convert_cases({"首页": {"step1": step()}})


@pytest.mark.parametrize("raw", ['{', '{"重复":{},"重复":{}}', '{"用例":NaN}', '[]'])
def test_load_invalid_json_duplicate_and_nonstandard_constants(tmp_path, raw):
    path = tmp_path / "source.json"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(CaseImportError):
        load_cases(path)


def test_load_utf8_bom(tmp_path):
    data = {"首页": {"step1": step()}}
    path = tmp_path / "source.json"
    path.write_text(json.dumps(data), encoding="utf-8-sig")
    assert load_cases(path) == data


@pytest.mark.parametrize("name", ["../../CON", r"..\..\NUL", 'A/B:C*D?E"F<G>H|I',
                                  "CON", "AUX.txt", "LPT1", "COM¹", " . ", "长名" * 80])
def test_filename_windows_safe_no_traversal_and_length(name):
    filename = safe_filename(1, name)
    assert filename.startswith("01-") and filename.endswith(".md")
    assert len(filename) <= 66
    assert not any(character in filename for character in '/\\:*?"<>|')
    assert Path(filename).name == filename
    assert not filename.endswith((" ", "."))
    case_import._validate_component(filename)


def test_writer_idempotent_preserves_existing_file_timestamp(tmp_path):
    cases = convert_cases({"首页": {"step1": step()}})
    out = tmp_path / "out"
    paths = write_cases(cases, out)
    timestamp = paths[0].stat().st_mtime_ns
    assert paths[0].read_text(encoding="utf-8") == cases[0].text
    assert write_cases(cases, out) == paths
    assert paths[0].stat().st_mtime_ns == timestamp


def test_writer_checks_entire_batch_before_writing_unrelated_collision(tmp_path):
    cases = convert_cases({"第一条": {"step1": step()}, "第二条": {"step1": step()}})
    out = tmp_path / "out"
    out.mkdir()
    unrelated = out / cases[1].filename
    unrelated.write_text("用户原文件", encoding="utf-8")
    with pytest.raises(CaseImportError, match="覆盖"):
        write_cases(cases, out)
    assert not (out / cases[0].filename).exists()
    assert unrelated.read_text(encoding="utf-8") == "用户原文件"


def test_writer_rejects_changed_generated_file(tmp_path):
    cases = convert_cases({"首页": {"step1": step()}})
    write_cases(cases, tmp_path)
    changed = convert_cases({"首页": {"step1": step(expected="不同预期")}})
    with pytest.raises(CaseImportError, match="覆盖"):
        write_cases(changed, tmp_path)


@pytest.mark.parametrize("filename", ["../escape.md", r"..\escape.md", "CON.md", "AUX", "case.md.", "case.md ", "case.txt"])
def test_writer_rejects_unsafe_filenames_before_mkdir(tmp_path, filename):
    case = replace(convert_cases({"首页": {"step1": step()}})[0], filename=filename)
    out = tmp_path / "out"
    with pytest.raises(CaseImportError):
        write_cases([case], out)
    assert not out.exists()


@pytest.mark.parametrize("component", ["..", "CON", "NUL.txt", "trailing.", "trailing "])
def test_writer_rejects_unsafe_output_directory(tmp_path, component):
    out = tmp_path / component / "out"
    with pytest.raises(CaseImportError):
        write_cases(convert_cases({"首页": {"step1": step()}}), out)
    assert not (tmp_path / "out").exists()


def test_writer_rejects_casefold_collision_before_mkdir(tmp_path):
    case = convert_cases({"首页": {"step1": step()}})[0]
    out = tmp_path / "out"
    cases = [replace(case, filename="01-CASE.md"), replace(case, filename="01-case.md")]
    with pytest.raises(CaseImportError, match="碰撞"):
        write_cases(cases, out)
    assert not out.exists()


def test_writer_rejects_existing_casefold_collision(tmp_path):
    case = replace(convert_cases({"首页": {"step1": step()}})[0], filename="01-CASE.md")
    original = tmp_path / "01-case.md"
    original.write_text(case.text, encoding="utf-8")
    with pytest.raises(CaseImportError, match="碰撞"):
        write_cases([case], tmp_path)


def test_writer_rejects_reparse_directory_without_touching_target(tmp_path, monkeypatch):
    out = tmp_path / "linked"
    original = Path.lstat

    def fake_lstat(path):
        if path == out:
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        return original(path)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    with pytest.raises(CaseImportError, match="reparse"):
        write_cases(convert_cases({"首页": {"step1": step()}}), out)
    assert not out.exists()


@pytest.mark.parametrize("failure", ["unknown_only", "invalid_json", "invalid_unselected"])
def test_cli_invalid_input_never_creates_output(tmp_path, capsys, failure):
    data = {"首页": {"step1": step()}}
    src = source_file(tmp_path, data)
    out = tmp_path / "out"
    argv = ["--src", str(src), "--out", str(out)]
    if failure == "unknown_only":
        argv += ["--only", "不存在"]
    elif failure == "invalid_json":
        src.write_text("{", encoding="utf-8")
    else:
        src.write_text(json.dumps({**data, "坏用例": {}}), encoding="utf-8")
        argv += ["--only", "首页"]
    assert main(argv) == 2
    assert not out.exists()
    assert "case_import:" in capsys.readouterr().err


def test_cli_repeated_only_keeps_source_positions_and_side_effects_flag(tmp_path, capsys):
    data = {"首页": {"step1": step()}, "版本": {"step1": step("检查版本")},
            "卸载应用": {"step1": step("卸载应用")}}
    src = source_file(tmp_path, data)
    out = tmp_path / "out"
    assert main(["--src", str(src), "--out", str(out), "--only", "版本",
                 "--only", "卸载应用", "--keep-side-effects"]) == 0
    assert sorted(path.name for path in out.iterdir()) == ["02-版本.md", "03-卸载应用.md"]
    assert json.loads(capsys.readouterr().out) == {"cases": 2, "steps": 2, "skipped_cases": 0}
    assert "TODO-ASSERT" in (out / "03-卸载应用.md").read_text(encoding="utf-8")


def test_multi_cases_roundtrip_and_sensitive_steps_marked(tmp_path):
    raw_data = {
        "主界面各入口正常": {
            "step1": step("点击首页", "首页正常显示"),
            "step2": step("点击检查更新", "更新成功"),
        },
        "设置正常": {
            "step1": step("打开设置页", "设置页正常"),
            "step2": step("设置中心-开启权限变更开关", "可新增删除文件"),
        },
    }
    src = source_file(tmp_path, raw_data)
    data = load_cases(src)
    cases = convert_cases(data)
    assert len(cases) == len(data) == 2
    assert sum(len(case.metadata["steps"]) for case in cases) == sum(len(steps) for steps in data.values())
    assert all(parse_text_case(case.text).parsed for case in cases)
    by_name = {case.metadata["name"]: case for case in cases}
    assert by_name["主界面各入口正常"].metadata["steps"][1]["safety_level"] == "L3"
    assert by_name["设置正常"].metadata["steps"][1]["safety_level"] == "L4"
    for case in cases:
        parsed = parse_text_case(case.text)
        assert len(parsed.steps) == len(data[case.metadata["name"]])
        assert [item.no for item in parsed.steps] == [item["source_step_no"] for item in case.metadata["steps"]]
        assert all(item["todo_assert"] for item in case.metadata["steps"])
        assert [item.expected for item in parsed.steps] == [item["expected"] for item in case.metadata["steps"]]
