"""window_hint 契约单测：解析/规范化/容错 + match_hint 纯函数（纯 mock）。"""
from __future__ import annotations

import pytest

from uita.core.platform.window_hint import (
    WindowHint,
    WindowRef,
    match_hint,
    normalize_process_name,
    parse_window_hint,
)


# ── parse_window_hint：合法形态 ──────────────────────────────────────

def test_parse_main_shorthand():
    hint = parse_window_hint("main")
    assert hint == WindowHint(role="main")
    assert hint.is_main


def test_parse_main_shorthand_case_insensitive_and_dict_role():
    assert parse_window_hint("MAIN").is_main
    assert parse_window_hint({"role": "Main"}).is_main


def test_parse_title_shorthand_with_multilang_candidates():
    hint = parse_window_hint("title=辅助工具|Helper Tool")
    assert hint.titles == ("辅助工具", "Helper Tool")
    assert not hint.is_main


def test_parse_dict_all_keys_normalized():
    hint = parse_window_hint({
        "title": " 设置 | Settings ", "class": "Qt51514QWindowIcon",
        "process": "HelperTool.EXE",
    })
    assert hint.titles == ("设置", "Settings")
    assert hint.class_names == ("Qt51514QWindowIcon",)
    assert hint.process == "helpertool"


def test_parse_hint_is_hashable_for_cache_key():
    a = parse_window_hint({"title": "A", "process": "x.exe"})
    b = parse_window_hint({"title": "A", "process": "X"})
    assert a == b and hash(a) == hash(b)


# ── parse_window_hint：非法值一律 None、不抛 ─────────────────────────

@pytest.mark.parametrize("raw", [
    None, "", {}, "   ",
    "bogus", "title", "foo=bar",            # 非法简写
    {"unknown": "x"},                        # 未知键
    {"title": 123},                          # 值非字符串
    {"title": ""},                           # 无有效条件
    {"role": "main", "title": "x"},          # role 与其他键并用
    {"role": "popup"},                       # role 仅支持 main
    42, ["main"],                            # 不支持的类型
])
def test_parse_invalid_returns_none(raw):
    assert parse_window_hint(raw) is None


# ── match_hint ───────────────────────────────────────────────────────

def _w(hwnd, title="", cls="C", proc="app.exe", pid=1, area=100):
    return WindowRef(hwnd=hwnd, pid=pid, title=title, class_name=cls,
                     process_name=proc, area=area)


def test_match_exact_title_beats_substring():
    hint = parse_window_hint("title=设置")
    wins = [_w(1, "设置中心", area=10), _w(2, "设置", area=999)]
    assert match_hint(hint, wins).hwnd == 2


def test_match_substring_when_no_exact():
    hint = parse_window_hint("title=设置")
    assert match_hint(hint, [_w(1, "高级设置页")]).hwnd == 1


def test_match_picks_smallest_area_then_lowest_hwnd():
    hint = parse_window_hint("title=X")
    wins = [_w(5, "X", area=500), _w(3, "X", area=50), _w(2, "X", area=50)]
    assert match_hint(hint, wins).hwnd == 2


def test_match_keys_are_and():
    hint = parse_window_hint({"title": "A", "process": "p.exe"})
    wins = [_w(1, "A", proc="other.exe"), _w(2, "B", proc="p.exe")]
    assert match_hint(hint, wins) is None
    assert match_hint(hint, wins + [_w(3, "A", proc="P.EXE")]).hwnd == 3


def test_match_class_case_insensitive_and_candidates():
    hint = parse_window_hint({"class": "FooWnd|BarWnd"})
    assert match_hint(hint, [_w(1, cls="barwnd")]).hwnd == 1
    assert match_hint(hint, [_w(1, cls="Other")]) is None


def test_match_excludes_own_process_and_console_windows():
    hint = parse_window_hint("title=终端")
    wins = [
        _w(1, "终端", pid=99),                          # 自身进程
        _w(2, "终端", cls="ConsoleWindowClass"),         # 控制台类
        _w(3, "终端", cls="CASCADIA_HOSTING_WINDOW_CLASS"),
    ]
    assert match_hint(hint, wins, own_pid=99) is None


def test_match_main_role_never_matches_here():
    assert match_hint(WindowHint(role="main"), [_w(1, "x")]) is None


def test_match_no_candidates():
    assert match_hint(parse_window_hint("title=A"), []) is None


def test_normalize_process_name():
    assert normalize_process_name(" DemoApp.exe ") == "demoapp"
    assert normalize_process_name("") == ""
