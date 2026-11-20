"""WinUIA 窗口后端（uiautomation 默认 / pywinauto 灰度）单测（全 mock）。

覆盖：后端名解析（app 级 > 环境变量 > 默认）、pywinauto 不可用回退、
pywinauto 后端的属性匹配 / 多语言候选 / ChildText 谓词 / 控件转换 / 窗口枚举、
引擎 `_search_root` 的灰度分发与异常回退、`win32ui` 桩注册。
"""
from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from uita.engine.win_uia import engine as engine_mod
from uita.engine.win_uia import pywinauto_backend as pwb
from uita.engine.win_uia import window_contract as wc
from uita.engine.win_uia.engine import WinUIAEngine


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("UIA_WIN_BACKEND", raising=False)


# ── 后端名解析 ───────────────────────────────────────────────────────

def test_backend_default_is_uiautomation():
    assert wc.resolve_backend_name("no_such_app") == wc.BACKEND_UIAUTOMATION


def test_backend_env_selects_pywinauto(monkeypatch):
    monkeypatch.setenv("UIA_WIN_BACKEND", "PyWinAuto")
    assert wc.resolve_backend_name("no_such_app") == wc.BACKEND_PYWINAUTO


def test_backend_app_extra_overrides_env(monkeypatch):
    monkeypatch.setenv("UIA_WIN_BACKEND", "pywinauto")
    with patch("uita.core.platform.app_registry.get_extra_config",
               return_value="uiautomation") as getter:
        assert wc.resolve_backend_name("demo") == wc.BACKEND_UIAUTOMATION
    assert getter.call_args.kwargs["default"] == "pywinauto"


def test_backend_unknown_value_falls_back_to_default():
    assert wc.normalize_backend_name("selenium") == wc.BACKEND_UIAUTOMATION
    assert wc.normalize_backend_name("") == wc.BACKEND_UIAUTOMATION
    assert wc.normalize_backend_name(None) == wc.BACKEND_UIAUTOMATION


# ── 引擎装配：灰度开关 / 不可用回退 ──────────────────────────────────

def _engine(app="demo") -> WinUIAEngine:
    e = WinUIAEngine.__new__(WinUIAEngine)
    e._app_name = app
    e._win_backend = None
    return e


def test_engine_default_backend_is_uiautomation():
    assert _engine()._get_backend().name == wc.BACKEND_UIAUTOMATION


def test_engine_pywinauto_requested_but_unavailable_falls_back(monkeypatch):
    monkeypatch.setenv("UIA_WIN_BACKEND", "pywinauto")
    with patch.object(engine_mod, "create_pywinauto_backend", return_value=None):
        backend = _engine()._get_backend()
    assert backend.name == wc.BACKEND_UIAUTOMATION


def test_engine_pywinauto_requested_and_available(monkeypatch):
    monkeypatch.setenv("UIA_WIN_BACKEND", "pywinauto")
    fake = SimpleNamespace(name=wc.BACKEND_PYWINAUTO)
    with patch.object(engine_mod, "create_pywinauto_backend", return_value=fake):
        e = _engine()
        assert e._get_backend() is fake
        assert e._get_backend() is fake      # 已缓存，不重复装配


def test_search_root_default_calls_legacy_with_unchanged_args():
    e = _engine()
    e._win_backend = wc.UiaLibBackend(MagicMock(), e._find_uia_control)
    loc = wc.ControlLocator(name="n", automation_id="a", control_type="ButtonControl",
                            class_name="c", child_text="t", sibling_text="s",
                            name_regex="r", max_depth=7)
    with patch.object(WinUIAEngine, "_find_uia_control", return_value="X") as legacy:
        assert e._search_root("ROOT", 100, loc) == "X"
    legacy.assert_called_once_with(
        "ROOT", "n", "a", "ButtonControl", class_name="c", child_text="t",
        sibling_text="s", name_regex="r", max_depth=7,
    )


def test_search_root_pywinauto_delegates_for_window_root():
    e = _engine()
    backend = MagicMock()
    backend.name = wc.BACKEND_PYWINAUTO
    backend.find_control.return_value = "PW"
    e._win_backend = backend
    loc = wc.ControlLocator(name="n")
    with patch.object(WinUIAEngine, "_find_uia_control") as legacy:
        assert e._search_root("ROOT", 100, loc) == "PW"
    backend.find_control.assert_called_once_with(100, loc)
    legacy.assert_not_called()


def test_search_root_pywinauto_skipped_for_subtree_root():
    """父容器子树（hwnd=None）pywinauto 无法以控件为根 → 必须走 uiautomation。"""
    e = _engine()
    backend = MagicMock()
    backend.name = wc.BACKEND_PYWINAUTO
    e._win_backend = backend
    with patch.object(WinUIAEngine, "_find_uia_control", return_value="LEG"):
        assert e._search_root("SUBROOT", None, wc.ControlLocator(name="n")) == "LEG"
    backend.find_control.assert_not_called()


def test_search_root_pywinauto_exception_falls_back_to_uiautomation():
    e = _engine()
    backend = MagicMock()
    backend.name = wc.BACKEND_PYWINAUTO
    backend.find_control.side_effect = RuntimeError("COM error")
    e._win_backend = backend
    with patch.object(WinUIAEngine, "_find_uia_control", return_value="LEG"):
        assert e._search_root("ROOT", 100, wc.ControlLocator(name="n")) == "LEG"


# ── PywinautoBackend：属性匹配 ───────────────────────────────────────

def _info(name="", aid="", cls="", element=None):
    return SimpleNamespace(name=name, automation_id=aid, class_name=cls,
                           element=element or object())


class _FakeRoot:
    def __init__(self, infos):
        self._infos = infos
        self.control_type_arg = "unset"

    def descendants(self, **kwargs):
        self.control_type_arg = kwargs.get("control_type")
        return list(self._infos)


def _backend(infos, child=lambda c, t: True, sibling=lambda c, t: True):
    root = _FakeRoot(infos)
    pw = SimpleNamespace(
        uia_element_info=SimpleNamespace(UIAElementInfo=lambda hwnd: root),
        Desktop=MagicMock(),
    )
    uia = MagicMock()
    uia.Control.CreateControlFromElement.side_effect = lambda el: ("CTRL", el)
    return pwb.PywinautoBackend(pw, uia, child, sibling), root, uia


def test_pw_find_by_automation_id_returns_converted_uiautomation_control():
    a, b = _info("OK", "btnA"), _info("OK", "btnB")
    backend, _, uia = _backend([a, b])
    got = backend.find_control(1, wc.ControlLocator(automation_id="btnB"))
    assert got == ("CTRL", b.element)


def test_pw_control_type_suffix_stripped_and_passed_to_descendants():
    backend, root, _ = _backend([_info("x")])
    backend.find_control(1, wc.ControlLocator(control_type="ButtonControl"))
    assert root.control_type_arg == "Button"


def test_pw_name_multilang_candidates_and_class_candidates():
    backend, _, _ = _backend([_info("Start Scan", cls="Foo")])
    loc = wc.ControlLocator(name="开始检查|Start Scan", class_name="Bar|Foo")
    assert backend.find_control(1, loc) is not None


def test_pw_name_regex_and_miss():
    backend, _, _ = _backend([_info("检查完成 3 项")])
    assert backend.find_control(1, wc.ControlLocator(name_regex=r"\d+ 项")) is not None
    assert backend.find_control(1, wc.ControlLocator(name_regex=r"^不存在$")) is None


def test_pw_first_match_in_document_order():
    first, second = _info("OK", "a"), _info("OK", "b")
    backend, _, _ = _backend([first, second])
    assert backend.find_control(1, wc.ControlLocator(name="OK"))[1] is first.element


def test_pw_child_text_predicate_filters_candidates():
    a, b = _info("", "", "Card"), _info("", "", "Card")
    seen: list = []

    def child(ctrl, text):
        seen.append(ctrl)
        return ctrl[1] is b.element

    backend, _, _ = _backend([a, b], child=child)
    got = backend.find_control(1, wc.ControlLocator(class_name="Card", child_text="高级设置"))
    assert got[1] is b.element and len(seen) == 2


def test_pw_sibling_text_predicate_rejects_all():
    backend, _, _ = _backend([_info("", "", "Btn")], sibling=lambda c, t: False)
    assert backend.find_control(1, wc.ControlLocator(class_name="Btn", sibling_text="x")) is None


def test_pw_candidate_property_error_is_skipped_not_raised():
    class Bad:
        element = object()

        @property
        def name(self):
            raise RuntimeError("stale element")

    backend, _, _ = _backend([Bad(), _info("OK")])
    assert backend.find_control(1, wc.ControlLocator(name="OK")) is not None


def test_pw_unknown_control_type_falls_back_to_unfiltered_descendants():
    calls: list = []

    class Root:
        def descendants(self, **kw):
            calls.append(kw)
            if kw:
                raise KeyError("Bogus")
            return [_info("OK")]

    pw = SimpleNamespace(uia_element_info=SimpleNamespace(UIAElementInfo=lambda h: Root()))
    uia = MagicMock()
    uia.Control.CreateControlFromElement.return_value = "CTRL"
    backend = pwb.PywinautoBackend(pw, uia, lambda c, t: True, lambda c, t: True)
    assert backend.find_control(1, wc.ControlLocator(control_type="BogusControl")) == "CTRL"
    assert calls == [{"control_type": "Bogus"}, {}]


# ── 窗口枚举：两个后端共用 Win32 EnumWindows（不经 UIA 桌面遍历）─────

def test_pw_enumerate_windows_uses_shared_win32_enumeration_not_uia_desktop():
    desktop = MagicMock()
    desktop.side_effect = AssertionError("不应经 UIA Desktop 遍历")
    pw = SimpleNamespace(Desktop=desktop)
    backend = pwb.PywinautoBackend(pw, MagicMock(), lambda c, t: True, lambda c, t: True)
    shared = [wc.WindowRef(hwnd=10, pid=7, title="设置", class_name="Qt5",
                           process_name="demo.exe", area=5000)]
    with patch.object(pwb, "enumerate_top_level_windows", return_value=shared):
        assert backend.enumerate_windows() == shared


def test_uialib_backend_enumerate_uses_shared_win32_enumeration():
    backend = wc.UiaLibBackend(MagicMock(), lambda *a, **k: None)
    shared = [wc.WindowRef(hwnd=1)]
    with patch.object(wc, "enumerate_top_level_windows", return_value=shared):
        assert backend.enumerate_windows() == shared


# ── load_pywinauto：win32ui 桩与不可用回退 ───────────────────────────

def test_load_pywinauto_returns_none_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert pwb.load_pywinauto() is None


def test_load_pywinauto_registers_win32ui_stub_when_dll_missing(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delitem(sys.modules, "win32ui", raising=False)
    fake_pw = types.ModuleType("pywinauto")
    monkeypatch.setitem(sys.modules, "pywinauto", fake_pw)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def fake_import(name, *a, **k):
        if name == "win32ui":
            raise ImportError("DLL load failed while importing win32ui")
        return real_import(name, *a, **k)

    with patch("builtins.__import__", side_effect=fake_import):
        assert pwb.load_pywinauto() is fake_pw
    assert isinstance(sys.modules.get("win32ui"), types.ModuleType)
    sys.modules.pop("win32ui", None)


def test_create_backend_returns_none_when_pywinauto_missing():
    with patch.object(pwb, "load_pywinauto", return_value=None):
        assert pwb.create_pywinauto_backend(MagicMock(), lambda c, t: True,
                                            lambda c, t: True) is None
