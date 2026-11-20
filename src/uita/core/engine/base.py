"""
EngineBase — abstract interface for all UI automation engines.
15 abstract methods + 2 abstract properties + 1 optional screenshot method.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class ElementRef:
    """A lightweight handle to a UI element returned by the engine."""
    element_id: str        # internal unique id (e.g. CSS selector or accessibility id)
    raw: object = None     # backend-specific raw element object (e.g. Playwright Locator)
    tag: str = ""          # optional tag hint
    text: str = ""         # optional visible text hint
    # P4 新增：纯坐标句柄（视觉指纹 L3.8 / LLM 视觉 L4 命中后使用）。raw is None
    # 且 coords 非 None 时，click/type action 应走 pyautogui 坐标点击分支，而非
    # engine.click(element)（坐标定位与具体元素引用无关，见 P4 计划 §3.4）。
    coords: Optional[tuple] = None
    # 2026-07-14 新增：完整 bbox (x, y, w, h)，仅 L3.8 视觉指纹命中时填充，默认
    # None（其余命中路径零回归）。供 click/hover/type action 的 region= kwarg
    # 计算九宫格区域点击坐标使用，见 core/resolver/click_region.py 及计划文档
    # docs/plans/archive/03-resolver-rosetta-实施纪要.md。
    bbox: Optional[tuple] = None


class EngineBase(ABC):
    """Abstract base for all platform engines."""

    # ------------------------------------------------------------------
    # Metadata (2 abstract properties)
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def app_name(self) -> str:
        """The application this engine is attached to (e.g. 'sample_web')."""
        ...

    @property
    @abstractmethod
    def platform(self) -> str:
        """The OS platform (e.g. 'macOS', 'Windows', 'Linux')."""
        ...

    # ------------------------------------------------------------------
    # Lifecycle (3 methods)
    # ------------------------------------------------------------------

    @abstractmethod
    async def launch(self) -> None:
        """Launch / attach to the application."""
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close the engine and release resources."""
        ...

    @abstractmethod
    async def is_healthy(self) -> bool:
        """Return True if the engine is still connected and operational."""
        ...

    # ------------------------------------------------------------------
    # Element finding (4 methods)
    # ------------------------------------------------------------------

    @abstractmethod
    async def find_by_id(self, element_id: str) -> Optional[ElementRef]:
        """Find an element by CSS selector / accessibility id."""
        ...

    @abstractmethod
    async def find_by_text(self, text: str) -> Optional[ElementRef]:
        """Find an element by visible text (partial match)."""
        ...

    @abstractmethod
    async def find_by_role(self, role: str, name: str = "") -> Optional[ElementRef]:
        """Find an element by ARIA role and optional accessible name."""
        ...

    @abstractmethod
    async def find_all_by_role(self, role: str) -> list[ElementRef]:
        """Find all elements with the given ARIA role."""
        ...

    # ------------------------------------------------------------------
    # Actions (5 methods)
    # ------------------------------------------------------------------

    @abstractmethod
    async def click(self, element: ElementRef) -> None:
        """Click the given element."""
        ...

    @abstractmethod
    async def type_text(self, element: ElementRef, text: str, clear: bool = True) -> None:
        """Type text into the element, optionally clearing it first."""
        ...

    async def type_focused(self, text: str, *, via_clipboard: bool = False) -> None:
        """Clear and type into current focused control without resolving it."""
        raise NotImplementedError(
            f"{type(self).__name__} does not support focused text input"
        )

    async def fill_text(self, element: ElementRef, text: str) -> None:
        """Fill an element in one operation, replacing its current value."""
        await self.type_text(element, text, clear=True)

    async def goto_url(self, url: str, *, wait_until: str = "domcontentloaded") -> None:
        raise NotImplementedError(f"{type(self).__name__} does not support web navigation")

    async def reload_page(self, *, wait_until: str = "domcontentloaded") -> None:
        raise NotImplementedError(f"{type(self).__name__} does not support page reload")

    async def wait_load_state(self, state: str, *, timeout_ms: float = 30000) -> None:
        raise NotImplementedError(f"{type(self).__name__} does not support load-state waits")

    @abstractmethod
    async def press_key(self, key: str) -> None:
        """Press a keyboard key (e.g. 'Enter', 'Tab', 'Escape')."""
        ...

    @abstractmethod
    async def hover(self, element: ElementRef) -> None:
        """Hover the mouse over the element."""
        ...

    @abstractmethod
    async def scroll(self, element: Optional[ElementRef], direction: str, amount: int) -> None:
        """Scroll within the element. direction: 'up'|'down'|'left'|'right'.

        element 可为 None（scroll[direction] 无元素名调用场景，语义为"滑动当前
        窗口"，见 docs/plans/archive/02-dsl-actions-实施纪要.md），
        引擎实现需兼容该场景（回退窗口坐标/当前活跃窗口，而非硬取 element.raw）。
        """
        ...

    # ------------------------------------------------------------------
    # Assert helpers (4 methods)
    # ------------------------------------------------------------------

    @abstractmethod
    async def get_text(self, element: ElementRef) -> str:
        """Return the visible text content of the element."""
        ...

    @abstractmethod
    async def get_attribute(self, element: ElementRef, attr: str) -> Optional[str]:
        """Return the value of the given attribute, or None if absent."""
        ...

    @abstractmethod
    async def is_visible(self, element: ElementRef) -> bool:
        """Return True if the element is visible in the viewport."""
        ...

    @abstractmethod
    async def get_window_title(self) -> str:
        """Return the current window/page title."""
        ...

    # ------------------------------------------------------------------
    # Screenshot (optional override)
    # ------------------------------------------------------------------

    async def take_screenshot(self, path: str = "") -> Optional[bytes]:
        """Take a screenshot. Returns raw bytes if path is empty, else saves to path."""
        return None

    async def get_element_rect(self, element: "ElementRef") -> Optional[tuple]:
        """Return element bounding box as (x, y, w, h) in screen logical coordinates.

        Returns None if not supported or element is unavailable.
        Subclasses should override this for annotation support.
        """
        return None

    async def get_window_rect(self) -> Optional[tuple]:
        """Return the main application window rect (x, y, w, h) in screen logical coordinates.

        Used by the annotator to convert absolute element coords to screenshot-relative pixels.
        Returns None if not supported.
        """
        return None

    async def count_by_id(self, element_id: str) -> int:
        """Return the number of elements matching the given selector/id.

        Default implementation delegates to find_by_id (returns 0 or 1).
        Engines that support multi-match counting (e.g. Playwright) should override
        this method to return the precise count.

        Returns:
            0  — no match
            1  — exactly one match  (safe to proceed)
            N  — N > 1 matches (ambiguous; L1.5 treats this as not-found)
        """
        result = await self.find_by_id(element_id)
        return 0 if result is None else 1

    async def get_accessibility_summary(self, max_nodes: int = 200) -> Optional[str]:
        """返回当前页面/窗口的可访问性树摘要（文本形式），供 P4 的 LLM_SELECTOR
        策略推断 selector/AXPath/AutomationId 使用（面向 backlog #2 预留，见
        docs/plans/archive/03-resolver-rosetta-实施纪要.md §3.4b）。

        默认返回 None（不支持）。MacAppKitEngine/WinUIAEngine 未来可 override 实现
        真正的树遍历+裁剪；PlaywrightEngine 不需要 override——Web 引擎走 DOM 摘要，
        逻辑不同，另行处理。Resolver 对返回 None 的情况应直接跳过该策略，不报错。
        """
        return None

    # ------------------------------------------------------------------
    # 系统文件选择对话框（可选 override，见 choose_file[] Action）
    # ------------------------------------------------------------------
    # 计划文档：docs/plans/archive/02-dsl-actions-实施纪要.md
    #
    # 这两个方法跨 MacAppKitEngine/WinUIAEngine 共享同一套调用点（choose_file[]
    # Action 需要引擎无关地判断"系统文件对话框是否已打开"+"跳转路径并确认"），
    # 因此不满足 R9 "引擎专用能力写在具体引擎类，不放入 EngineBase" 的适用前提——
    # 该规则针对的是单一引擎的专属能力；这里是跨两个引擎复用的通用扩展点，与
    # 已有的 take_screenshot()/get_element_rect()/get_window_rect()/
    # get_accessibility_summary() 等"非抽象、默认空实现、可选 override"的设计
    # 先例完全一致。默认返回 False，18 个抽象方法数量与签名不受影响（R9/R10）。

    async def has_open_file_dialog(self) -> bool:
        """判断当前是否存在系统原生文件选择对话框（macOS AXSheet / Windows
        #32770 Common Dialog）。默认返回 False（不支持）。"""
        return False

    async def goto_path_in_file_dialog(self, path: str) -> bool:
        """在已打开的系统文件选择对话框中跳转到 path 并确认选择。

        Args:
            path: 已解析好的绝对路径（choose_file[] Action 调用前已完成路径
                  解析，本方法不再做 ~/$VAR 展开等处理）。

        Returns:
            True 表示跳转+确认操作已发出（不代表对话框业务上一定接受该路径，
            例如路径在对话框语境下不存在，仍返回 True——上层通过后续断言
            判断实际效果）；引擎不支持时返回 False。
        """
        return False

    # ------------------------------------------------------------------
    # 无元素名 scroll[direction] 调用支持（可选 override）
    # ------------------------------------------------------------------
    # 计划文档：docs/plans/archive/02-dsl-actions-实施纪要.md
    #
    # scroll[] 传统调用需要 element_name 触发 resolver.resolve() 的隐式副作用
    # ——刷新窗口栈引用（见 docs/plans/archive/01-engine-lifecycle-实施纪要.md）,
    # 确保后续 get_window_rect() 读到当前活跃窗口而非过期主窗口。scroll[up]
    # 无元素名调用没有 resolve() 可用，因此新增该可选扩展点作为等价触发入口。
    # 默认 no-op（未实现窗口栈自动跟随的引擎、占位引擎等不受影响，零回归）。

    async def refresh_active_window(self) -> None:
        """刷新/reconcile 当前活跃窗口引用。默认 no-op，各引擎按需 override
        （目前 WinUIAEngine/MacAppKitEngine 有窗口栈概念，会 override）。"""
        return None

    # ------------------------------------------------------------------
    # 关联 <label> 文本定位（L2.5 LABEL_MATCH，可选 override）
    # ------------------------------------------------------------------
    # 计划文档：docs/plans/20260803150000-label-match-playwright-get-by-label.md
    #
    # 默认返回 None（不支持）。PlaywrightEngine override 实现 page.get_by_label()；
    # MacAppKitEngine/WinUIAEngine 不 override，继承本默认实现——调用方（Resolver）
    # 无需 hasattr 判断，直接调用即可自然降级为 NOT_FOUND（对齐
    # get_accessibility_summary()/has_open_file_dialog() 现有先例）。

    async def find_by_label(self, text: str) -> Optional[ElementRef]:
        """通过关联 <label> 文本定位元素（仅 Web 引擎实现）。默认返回 None。"""
        return None
