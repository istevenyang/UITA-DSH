"""文本用例解析：标题/前置/编号步骤/预期配对/退化单步。"""
from __future__ import annotations

from uita.tools.dsh.textcase import (STEP_PASSED, STEP_PENDING, TextCase,
                                     case_from_dict, parse_text_case)

SAMPLE = """用例名称：登录后检查隐私保护开关
前置条件：应用已安装并已登录

1. 打开应用首页
   预期：看到首页
2. 点开设置里的「隐私保护」
   预期：出现隐私保护页
3. 断言「已开启」可见
   预期：显示已开启
"""


def test_parse_header_steps_and_expected():
    case = parse_text_case(SAMPLE)
    assert case.parsed is True
    assert case.name == "登录后检查隐私保护开关"
    assert case.preconditions == ["应用已安装并已登录"]
    assert [step.no for step in case.steps] == [1, 2, 3]
    assert case.steps[0].action == "打开应用首页"
    assert case.steps[0].expected == "看到首页"
    assert case.steps[2].expected == "显示已开启"
    assert all(step.status == STEP_PENDING for step in case.steps)


def test_empty_expected_and_comments_do_not_change_action():
    case = parse_text_case("用例名称：空预期\n2. 检查版本\n预期：\n<!-- TODO-ASSERT -->")
    assert case.steps[0].no == 2
    assert case.steps[0].action == "检查版本"
    assert case.steps[0].expected == ""


def test_empty_expected_preserves_next_numbered_step():
    case = parse_text_case("1. 检查版本\n预期：\n2. 检查设置\n预期：标题存在")
    assert len(case.steps) == 2
    assert [step.expected for step in case.steps] == ["", "标题存在"]


def test_parse_inline_expected():
    case = parse_text_case("1. 点击开始扫描（预期：开始扫描）\n2. 等待结果")
    assert case.steps[0].action == "点击开始扫描"
    assert case.steps[0].expected == "开始扫描"
    assert case.steps[1].expected == ""


def test_parse_expect_section_pairs_by_order():
    text = """1. 打开首页
2. 点击设置
预期结果：
1. 首页可见
2. 设置页可见
"""
    case = parse_text_case(text)
    assert case.steps[0].expected == "首页可见"
    assert case.steps[1].expected == "设置页可见"


def test_parse_bullet_steps():
    text = """用例名称：多入口冒烟
- 打开首页
- 进入图库
"""
    case = parse_text_case(text)
    assert [step.action for step in case.steps] == ["打开首页", "进入图库"]
    assert case.steps[0].no == 1


def test_parse_falls_back_to_single_step():
    case = parse_text_case("把设置里的隐私保护打开，然后确认显示已开启")
    assert case.parsed is False
    assert len(case.steps) == 1
    assert "隐私保护" in case.steps[0].action
    assert case.steps[0].no == 1


def test_parse_empty_text():
    case = parse_text_case("   ")
    assert case.steps and case.parsed is False
    assert case.steps[0].action == "执行文本用例"


def test_parse_precondition_bullets():
    text = """前置条件：
- 已登录
- 网络可用
1. 打开首页
"""
    case = parse_text_case(text)
    assert case.preconditions == ["已登录", "网络可用"]
    assert len(case.steps) == 1


def test_step_lookup_and_progress():
    case = parse_text_case(SAMPLE)
    assert case.step(2).action == "点开设置里的「隐私保护」"
    assert case.step(99) is None
    assert case.next_pending().no == 1
    case.steps[0].status = STEP_PASSED
    assert case.next_pending().no == 2
    assert case.all_passed() is False
    summary = case.summary()
    assert summary["total"] == 3 and summary["passed"] == 1 and summary["pending"] == 2


def test_all_passed_and_roundtrip_dict():
    case = parse_text_case(SAMPLE)
    for step in case.steps:
        step.status = STEP_PASSED
    assert case.all_passed() is True
    restored = case_from_dict(case.to_dict())
    assert isinstance(restored, TextCase)
    assert restored.all_passed() is True
    assert restored.name == case.name
    assert case_from_dict({}) is None
    assert case_from_dict(None) is None


def test_parse_txt_multiline_plain_steps_and_default_name():
    txt = "打开首页（预期：快速检索入口存在）\n点击快速检索入口（预期：扫描进度文字存在）\n"
    case = parse_text_case(txt, default_name="my_txt_case")
    assert case.parsed is True
    assert case.name == "my_txt_case"
    assert [s.no for s in case.steps] == [1, 2]
    assert case.steps[0].action == "打开首页"
    assert case.steps[0].expected == "快速检索入口存在"
    assert case.steps[1].action == "点击快速检索入口"
    assert case.steps[1].expected == "扫描进度文字存在"


def test_parse_txt_natural_language_paragraph_and_arrows():
    from uita.tools.dsh.textcase import step_requires_assertion

    # 1) 逗号串联的一段话自然语言
    case1 = parse_text_case(
        "打开示例桌面应用首页，点击快速检索入口，确认扫描进度文字存在",
        default_name="quick_clean",
    )
    assert case1.parsed is True
    assert case1.name == "quick_clean"
    assert [s.action for s in case1.steps] == [
        "打开示例桌面应用首页",
        "点击快速检索入口",
        "确认扫描进度文字存在",
    ]
    assert step_requires_assertion(case1.steps[0]) is False
    assert step_requires_assertion(case1.steps[1]) is False
    assert step_requires_assertion(case1.steps[2]) is True

    # 2) 箭头链与分号/句号自然语言
    case2 = parse_text_case("打开首页 -> 点击快速检索 -> 看到扫描进度文字")
    assert case2.parsed is True
    assert len(case2.steps) == 3

    case3 = parse_text_case("# 快速检索冒烟\n先打开示例桌面应用首页；再点击快速检索入口；最后检查扫描进度是否存在。")
    assert case3.parsed is True
    assert case3.name == "快速检索冒烟"
    assert len(case3.steps) == 3
    assert step_requires_assertion(case3.steps[2]) is True

    # 3) 自然语言内联验证子句（无括号、无冒号）
    case4 = parse_text_case("打开示例桌面应用首页，确认快速检索入口存在\n点击快速检索入口，看到扫描进度文字出现")
    assert case4.parsed is True
    assert case4.steps[0].action == "打开示例桌面应用首页"
    assert case4.steps[0].expected == "快速检索入口存在"
    assert case4.steps[1].action == "点击快速检索入口"
    assert case4.steps[1].expected == "扫描进度文字出现"

