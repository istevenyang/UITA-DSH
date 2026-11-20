"""DSH 单测替身：假引擎、假定位器、上下文工厂。

放在独立模块（而非 conftest）以便测试文件直接 import；``conftest.py`` 只转发
fixture。
"""
from __future__ import annotations

from typing import Any


class FakeElement:
    def __init__(self, element_id: str = "el-1", coords: Any = None,
                 bbox: Any = None, text: str = "") -> None:
        self.element_id = element_id
        self.raw = None
        self.tag = "button"
        self.text = text
        self.coords = coords
        self.bbox = bbox


class FakeEngine:
    """只实现 DSH 工具会用到的方法，记录调用轨迹。"""

    def __init__(self, healthy: bool = True, title: str = "Fake Window",
                 text_value: str = "设置", attr_value: Any = "on",
                 js_value: Any = "ok") -> None:
        self.healthy = healthy
        self.title = title
        self.text_value = text_value
        self.attr_value = attr_value
        self.js_value = js_value
        self.calls: list[tuple] = []
        self.screenshot_error: Exception | None = None

    async def is_healthy(self) -> bool:
        self.calls.append(("is_healthy",))
        return self.healthy

    async def get_window_title(self) -> str:
        self.calls.append(("get_window_title",))
        return self.title

    async def get_window_rect(self):
        return (0, 0, 1280, 800)

    async def get_accessibility_summary(self, max_nodes: int = 200) -> str:
        return "AXWindow\n  AXButton '确定'"

    async def take_screenshot(self, path: str = ""):
        if self.screenshot_error is not None:
            raise self.screenshot_error
        self.calls.append(("take_screenshot", path))
        if path:
            with open(path, "wb") as handle:
                handle.write(b"\x89PNG\r\n\x1a\nfake")
        return b"png"

    async def click(self, element: Any) -> None:
        self.calls.append(("click", element))

    async def type_text(self, element: Any, text: str, clear: bool = True) -> None:
        self.calls.append(("type_text", element, text, clear))

    async def press_key(self, key: str) -> None:
        self.calls.append(("press_key", key))

    async def hover(self, element: Any) -> None:
        self.calls.append(("hover", element))

    async def scroll(self, element: Any, direction: str, amount: int) -> None:
        self.calls.append(("scroll", element, direction, amount))

    async def get_text(self, element: Any) -> str:
        return self.text_value

    async def get_attribute(self, element: Any, attr: str):
        return self.attr_value

    async def is_visible(self, element: Any) -> bool:
        return True

    async def get_element_rect(self, element: Any):
        return (5, 6, 7, 8)

    async def eval_js(self, expression: str):
        self.calls.append(("eval_js", expression))
        return self.js_value


class FakeResolveResult:
    def __init__(self, found: bool = True, level: str = "L1_MAP_EXACT",
                 element: Any = None) -> None:
        class _Level:
            value = level

        self.found = found
        self.level = _Level()
        self.element = element if element is not None else (FakeElement() if found else None)


class FakeResolver:
    def __init__(self, result: FakeResolveResult | None = None,
                 raise_error: Exception | None = None,
                 js_expr: str | None = "document.title") -> None:
        self.result = result or FakeResolveResult()
        self.raise_error = raise_error
        self.js_expr = js_expr

    async def resolve(self, element_name: str, *, skip_l4: bool = False, **_: Any):
        if self.raise_error is not None:
            raise self.raise_error
        return self.result

    def get_js_expr(self, element_name: str):
        if self.js_expr == "__raise__":
            raise RuntimeError("js expr lookup failed")
        return self.js_expr


class FakePlanner:
    """按预设剧本吐计划；剧本耗尽后抛 PlannerError。"""

    def __init__(self, plans: list[dict[str, Any]]) -> None:
        self.plans = list(plans)
        self.calls: list[dict[str, Any]] = []

    async def plan(self, *, session: Any, observation: Any = None,
                   tools: Any = None, tool_names: Any = None):
        from uita.tools.dsh.planner import Plan, PlannerError

        self.calls.append({"observation": observation, "tools": list(tool_names or [])})
        if not self.plans:
            raise PlannerError("剧本已耗尽")
        item = self.plans.pop(0)
        return Plan(
            thought=str(item.get("thought", "")),
            tool=str(item.get("tool", "")),
            args=dict(item.get("args") or {}),
            expect=str(item.get("expect", "")),
        )


def make_ctx(fake_engine=None, session=None, app_name: str = "demo",
             os_name: str = "macOS", resolver=None):
    from uita.tools.dsh.context import ToolContext

    ctx = ToolContext(session=session, app_name=app_name, os_name=os_name)
    if fake_engine is not None:
        ctx.set_engine(fake_engine, app_name)
    if resolver is not None:
        ctx.get_resolver = lambda engine=None: resolver  # type: ignore[assignment]
    return ctx
