"""WinUIAEngine window_hint 窗口作用域单测（全 mock，不依赖真机）。

覆盖：
  1. 作用域内只搜声明窗口，不触发子窗口跟随 / 回退主窗口；
  2. 同名元素分布在两个窗口时按 hint 命中正确窗口；
  3. hint 未命中：默认降级旧启发式 / 严格模式判未找到；
  4. role=main 取栈底；hint→hwnd 缓存命中与失效重解析；
  5. 命中窗口属于应用进程树时并入 hwnd 栈；
  6. 作用域退出后无 hint 元素行为不变；close/kill 清缓存。
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from uita.core.platform.window_hint import WindowRef, parse_window_hint
from uita.engine.win_uia import engine as engine_mod
from uita.engine.win_uia.engine import _SCOPE_MISS, WinUIAEngine


def _engine(stack=(100,)) -> WinUIAEngine:
    e = WinUIAEngine.__new__(WinUIAEngine)
    e._hwnd_stack = list(stack)
    e._hwnd = stack[-1] if stack else None
    e._qt_probe = None
    e._app_name = "demo"
    e._scope_state = None
    e._hint_hwnd_cache = {}
    e._win_backend = None
    return e


class _FakeBackend:
    name = "uiautomation"

    def __init__(self, windows):
        self._windows = windows
        self.find_calls: list = []

    def enumerate_windows(self):
        return list(self._windows)


def _w(hwnd, title, pid=1, proc="app.exe", cls="C", area=100):
    return WindowRef(hwnd=hwnd, pid=pid, title=title, class_name=cls,
                     process_name=proc, area=area)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("UIA_WINDOW_HINT_STRICT", "UIA_WINDOW_HINT_WAIT_S", "UIA_WIN_BACKEND",
              "UIA_FOLLOW_CHILD_WINDOW"):
        monkeypatch.delenv(k, raising=False)


def _patched(engine, windows, valid=True, app_pids=frozenset({1})):
    """统一 patch 系统调用：窗口枚举 / hwnd 有效性 / 可见性 / ensure / 进程树。"""
    backend = _FakeBackend(windows)
    engine._win_backend = backend
    return (
        patch.object(WinUIAEngine, "_is_hwnd_valid", return_value=valid),
        patch.object(WinUIAEngine, "_is_window_visible", return_value=True),
        patch.object(WinUIAEngine, "_ensure_hwnd", return_value=engine._hwnd),
        patch.object(WinUIAEngine, "_app_pid_set", return_value=set(app_pids)),
        patch.object(WinUIAEngine, "_get_pid_from_hwnd", return_value=1),
    )


def _enter(engine, hint_raw, patches):
    hint = parse_window_hint(hint_raw)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        engine._sync_enter_window_scope(hint)
    return hint


# ── 作用域解析 ───────────────────────────────────────────────────────

def test_scope_resolves_matching_window():
    e = _engine()
    _enter(e, "title=设置", _patched(e, [_w(100, "主窗口"), _w(200, "设置")]))
    assert e._scope_state == 200


def test_scope_role_main_uses_stack_bottom():
    e = _engine(stack=(100, 300))
    _enter(e, "main", _patched(e, []))
    assert e._scope_state == 100


def test_scope_miss_degrades_to_heuristics_by_default():
    e = _engine()
    _enter(e, "title=不存在", _patched(e, [_w(100, "主窗口")]))
    assert e._scope_state is None


def test_scope_miss_strict_mode(monkeypatch):
    monkeypatch.setenv("UIA_WINDOW_HINT_STRICT", "1")
    e = _engine()
    _enter(e, "title=不存在", _patched(e, [_w(100, "主窗口")]))
    assert e._scope_state is _SCOPE_MISS
    assert e._sync_find_by_id("@Name='x'") is None


def test_scope_ignores_windows_outside_app_process_tree():
    e = _engine()
    wins = [_w(500, "设置", pid=999)]   # 无关应用的同名窗口
    _enter(e, "title=设置", _patched(e, wins, app_pids={1}))
    assert e._scope_state is None


def test_scope_with_process_hint_matches_outside_process_tree():
    e = _engine()
    wins = [_w(500, "辅助工具", pid=999, proc="HelperTool.exe")]
    _enter(e, {"process": "HelperTool.exe"}, _patched(e, wins, app_pids={1}))
    assert e._scope_state == 500


def test_scope_excludes_console_window():
    e = _engine()
    wins = [_w(500, "设置", cls="ConsoleWindowClass")]
    _enter(e, "title=设置", _patched(e, wins))
    assert e._scope_state is None


# ── 缓存 ─────────────────────────────────────────────────────────────

def test_cache_hit_skips_enumeration():
    e = _engine()
    hint = _enter(e, "title=设置", _patched(e, [_w(200, "设置")]))
    assert e._hint_hwnd_cache[hint] == 200
    e._win_backend = MagicMock()
    e._win_backend.enumerate_windows.side_effect = AssertionError("不应再枚举")
    with patch.object(WinUIAEngine, "_is_hwnd_valid", return_value=True), \
         patch.object(WinUIAEngine, "_is_window_visible", return_value=True):
        assert e._resolve_hint_hwnd(hint) == 200


def test_cache_stale_handle_triggers_reresolve():
    e = _engine()
    hint = _enter(e, "title=设置", _patched(e, [_w(200, "设置")]))
    patches = _patched(e, [_w(250, "设置")])
    valid = lambda hwnd: hwnd != 200
    with patch.object(WinUIAEngine, "_is_hwnd_valid", side_effect=valid), \
         patches[1], patches[2], patches[3], patches[4]:
        assert e._resolve_hint_hwnd(hint) == 250
    assert e._hint_hwnd_cache[hint] == 250


def test_reset_clears_scope_and_cache():
    e = _engine()
    e._scope_state = 200
    e._hint_hwnd_cache[parse_window_hint("main")] = 100
    e._reset_window_hint_state()
    assert e._scope_state is None and e._hint_hwnd_cache == {}


# ── 栈并入 ───────────────────────────────────────────────────────────

def test_hint_window_in_process_tree_is_pushed_onto_stack():
    e = _engine()
    _enter(e, "title=设置", _patched(e, [_w(200, "设置")]))
    assert e._hwnd_stack == [100, 200]
    assert e._hwnd == 200


def test_hint_window_already_in_stack_not_duplicated():
    e = _engine(stack=(100, 200))
    _enter(e, "title=设置", _patched(e, [_w(200, "设置")]))
    assert e._hwnd_stack == [100, 200]


def test_main_hint_does_not_mutate_stack():
    e = _engine(stack=(100, 300))
    _enter(e, "main", _patched(e, []))
    assert e._hwnd_stack == [100, 300]


# ── 查找行为：只搜声明窗口，不触发启发式 ─────────────────────────────

def test_find_in_scope_searches_only_scoped_window_and_skips_heuristics():
    e = _engine(stack=(100, 300))
    e._scope_state = 200
    fake_uia = MagicMock()
    roots = {200: "ROOT200"}
    fake_uia.ControlFromHandle.side_effect = lambda h: roots[h]
    target = object()
    with patch.object(engine_mod, "_uia", fake_uia), \
         patch.object(engine_mod, "HAS_UIA", True), \
         patch.object(WinUIAEngine, "_find_uia_control", return_value=target) as find, \
         patch.object(WinUIAEngine, "_try_follow_child_window") as follow:
        assert e._sync_find_by_id("@AutomationId='btn'") is target
    assert find.call_args[0][0] == "ROOT200"
    follow.assert_not_called()


def test_find_in_scope_miss_does_not_fall_back_to_other_windows():
    """同名元素在主窗口存在，但 hint 指向的窗口里没有 → 必须判未找到，不得误命中主窗口。"""
    e = _engine(stack=(100, 200))
    e._scope_state = 200
    fake_uia = MagicMock()
    fake_uia.ControlFromHandle.side_effect = lambda h: f"ROOT{h}"
    calls: list = []

    def finder(root, *a, **k):
        calls.append(root)
        return object() if root == "ROOT100" else None

    with patch.object(engine_mod, "_uia", fake_uia), \
         patch.object(engine_mod, "HAS_UIA", True), \
         patch.object(WinUIAEngine, "_find_uia_control", side_effect=finder), \
         patch.object(WinUIAEngine, "_try_follow_child_window") as follow:
        assert e._sync_find_by_id("@AutomationId='btn'") is None
    assert calls == ["ROOT200"]
    follow.assert_not_called()


def test_same_name_element_resolves_to_hinted_window():
    """两个窗口各有同名按钮：不同 hint 命中各自窗口的控件。"""
    e = _engine(stack=(100, 200))
    fake_uia = MagicMock()
    fake_uia.ControlFromHandle.side_effect = lambda h: f"ROOT{h}"
    ctrl = {"ROOT100": "ctrl-A", "ROOT200": "ctrl-B"}
    with patch.object(engine_mod, "_uia", fake_uia), \
         patch.object(engine_mod, "HAS_UIA", True), \
         patch.object(WinUIAEngine, "_find_uia_control",
                      side_effect=lambda root, *a, **k: ctrl[root]):
        e._scope_state = 100
        assert e._sync_find_by_id("@AutomationId='btnOK'") == "ctrl-A"
        e._scope_state = 200
        assert e._sync_find_by_id("@AutomationId='btnOK'") == "ctrl-B"


def test_parent_scope_inside_hinted_window_uses_uiautomation_for_parent_subtree():
    e = _engine()
    e._scope_state = 200
    fake_uia = MagicMock()
    fake_uia.ControlFromHandle.return_value = "ROOT200"
    seen: list = []

    def finder(root, *a, **k):
        seen.append(root)
        return "PARENT" if root == "ROOT200" else "CHILD"

    with patch.object(engine_mod, "_uia", fake_uia), \
         patch.object(engine_mod, "HAS_UIA", True), \
         patch.object(WinUIAEngine, "_find_uia_control", side_effect=finder):
        got = e._sync_find_by_id("@ParentClassName='Card'@ClassName='Arrow'")
    assert got == "CHILD"
    assert seen == ["ROOT200", "PARENT"]


# ── 作用域退出 / 无 hint 元素零回归 ──────────────────────────────────

@pytest.mark.asyncio
async def test_window_scope_context_clears_state_on_exit():
    e = _engine()

    async def run_on_sta(fn, *args, timeout=None):
        return fn(*args)

    e._run_on_sta = run_on_sta
    hint = parse_window_hint("main")
    with patch.object(WinUIAEngine, "_sync_enter_window_scope",
                      side_effect=lambda h: setattr(e, "_scope_state", 100)):
        async with e.window_scope(hint):
            assert e._scope_state == 100
    assert e._scope_state is None


@pytest.mark.asyncio
async def test_window_scope_clears_state_when_body_raises():
    e = _engine()

    async def run_on_sta(fn, *args, timeout=None):
        e._scope_state = 100

    e._run_on_sta = run_on_sta
    with pytest.raises(RuntimeError):
        async with e.window_scope(parse_window_hint("main")):
            raise RuntimeError("boom")
    assert e._scope_state is None


def test_no_scope_find_keeps_original_heuristic_path():
    """无作用域时：主窗口未命中 → 仍走子窗口跟随兜底（改造前行为）。"""
    e = _engine()
    fake_uia = MagicMock()
    fake_uia.ControlFromHandle.return_value = MagicMock()
    with patch.object(engine_mod, "_uia", fake_uia), \
         patch.object(engine_mod, "HAS_UIA", True), \
         patch.object(WinUIAEngine, "_ensure_hwnd", return_value=100), \
         patch.object(WinUIAEngine, "_find_uia_control", return_value=None), \
         patch.object(WinUIAEngine, "_try_follow_child_window", return_value=False) as follow:
        assert e._sync_find_by_id("@Name='x'") is None
    follow.assert_called_once()
