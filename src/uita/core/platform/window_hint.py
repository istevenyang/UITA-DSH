"""Windows 多窗口元素的「所属窗口」契约（rosetta_map `window_hint`）。

一个 Windows 应用常有多个顶层窗口（主窗口、独立进程承载的子页面、弹窗）。
rosetta_map 元素用 `window_hint` 声明自己位于哪个窗口，引擎据此在查找前
确定性地切到该窗口，而不是依赖"先失败再猜"的启发式。

仅标准库，平台无关（mac / web 引擎不消费，解析结果被忽略）。
`hwnd` 每次启动都变，不可入库，因此只声明可稳定识别窗口的属性
（标题 / 类名 / 进程名 / 主窗口角色）；hwnd 只作为引擎运行时缓存。

声明形式::

    "window_hint": "main"                                  # 简写：主窗口
    "window_hint": "title=辅助工具窗口|Helper Window"      # 简写：单键
    "window_hint": {"title": "辅助工具窗口", "process": "AppHelper.exe"}

键语义（多键为 AND）：

| 键 | 语义 |
|---|---|
| `role` | `main` = 应用主窗口（hwnd 栈底），不可与其他键同时声明 |
| `title` | 窗口标题；`\\|` 分隔多语言候选；完全相等优先于子串命中 |
| `class` | 窗口类名，不区分大小写精确匹配；`\\|` 分隔候选 |
| `process` | 所属进程名，不区分大小写，可省略 `.exe` |
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

_ALLOWED_KEYS = frozenset({"role", "title", "class", "process"})
_SHORTHAND_KEYS = frozenset({"title", "class", "process"})

# 控制台 / 终端窗口类：命中会把测试进程自身的宿主窗口当成被测窗口
SELF_WINDOW_CLASSES = frozenset({
    "consolewindowclass",
    "cascadia_hosting_window_class",
    "pseudoconsolewindow",
    "windowsterminal",
    "conhostwindowclass",
})


def split_candidates(value: str) -> tuple[str, ...]:
    """按 `|` 拆分多语言候选，去两端空白并丢弃空串。"""
    return tuple(p.strip() for p in value.split("|") if p.strip())


def normalize_process_name(name: str) -> str:
    """进程名规范化：小写并去掉 `.exe` 后缀。"""
    lowered = (name or "").strip().lower()
    return lowered[:-4] if lowered.endswith(".exe") else lowered


@dataclass(frozen=True)
class WindowHint:
    """已规范化的窗口提示（可哈希，引擎用作缓存 key）。"""

    role: str = ""
    titles: tuple[str, ...] = ()
    class_names: tuple[str, ...] = ()
    process: str = ""

    @property
    def is_main(self) -> bool:
        return self.role == "main"

    def describe(self) -> str:
        """日志友好的单行描述。"""
        if self.is_main:
            return "main"
        parts = []
        if self.titles:
            parts.append("title=" + "|".join(self.titles))
        if self.class_names:
            parts.append("class=" + "|".join(self.class_names))
        if self.process:
            parts.append("process=" + self.process)
        return ",".join(parts)


@dataclass(frozen=True)
class WindowRef:
    """顶层窗口只读快照，两个后端共用。"""

    hwnd: int
    pid: int = 0
    title: str = ""
    class_name: str = ""
    process_name: str = ""
    area: int = 0


def _build(role: str, title: str, class_name: str, process: str) -> Optional[WindowHint]:
    if role:
        if role != "main" or title or class_name or process:
            return None
        return WindowHint(role="main")
    titles = split_candidates(title)
    class_names = split_candidates(class_name)
    proc = normalize_process_name(process)
    if not (titles or class_names or proc):
        return None
    return WindowHint(titles=titles, class_names=class_names, process=proc)


def parse_window_hint(raw: object) -> Optional[WindowHint]:
    """把 rosetta_map 的 `window_hint` 原始值规范化。

    空值 / 非法值（未知键、非字符串值、`role` 与其他键并用、无任何有效条件）
    一律返回 None 并记 warning，不抛异常——只影响该元素，等价于未声明。
    """
    if raw is None or raw == "" or raw == {}:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if text.lower() == "main":
            return WindowHint(role="main")
        key, sep, value = text.partition("=")
        key = key.strip().lower()
        if sep and key in _SHORTHAND_KEYS:
            return _build("", value if key == "title" else "",
                          value if key == "class" else "",
                          value if key == "process" else "")
        logger.warning("[window_hint] 非法简写 %r，已忽略", raw)
        return None
    if isinstance(raw, dict):
        unknown = set(raw) - _ALLOWED_KEYS
        if unknown:
            logger.warning("[window_hint] 含未知键 %s，已忽略整条声明", sorted(unknown))
            return None
        values: dict[str, str] = {}
        for key in _ALLOWED_KEYS:
            value = raw.get(key, "")
            if value is None:
                value = ""
            if not isinstance(value, str):
                logger.warning("[window_hint] 键 %r 的值不是字符串（%s），已忽略整条声明",
                               key, type(value).__name__)
                return None
            values[key] = value.strip()
        hint = _build(values["role"].lower(), values["title"], values["class"], values["process"])
        if hint is None:
            logger.warning("[window_hint] 声明无有效条件或 role 与其他键并用：%r，已忽略", raw)
        return hint
    logger.warning("[window_hint] 不支持的类型 %s，已忽略", type(raw).__name__)
    return None


def match_hint(
    hint: WindowHint,
    candidates: Iterable[WindowRef],
    own_pid: int = 0,
) -> Optional[WindowRef]:
    """在候选窗口里挑出满足 hint 的一个（两个后端共用，保证语义一致）。

    - 先排除测试进程自身窗口与控制台/终端窗口类；
    - `class` / `process` / `title` 多键 AND；
    - 声明了 title 时：标题完全相等者优先，其次子串命中；
    - 同池取面积最小者（弹窗远小于主窗口），面积相同取 hwnd 较小者保证确定性。

    `role=main` 不走这里（由引擎取 hwnd 栈底）。
    """
    if hint.is_main:
        return None
    class_set = {c.lower() for c in hint.class_names}
    exact: list[WindowRef] = []
    fuzzy: list[WindowRef] = []
    for win in candidates:
        if own_pid and win.pid == own_pid:
            continue
        if win.class_name.lower() in SELF_WINDOW_CLASSES:
            continue
        if class_set and win.class_name.lower() not in class_set:
            continue
        if hint.process and normalize_process_name(win.process_name) != hint.process:
            continue
        if hint.titles:
            if win.title in hint.titles:
                exact.append(win)
            elif any(t in win.title for t in hint.titles):
                fuzzy.append(win)
        else:
            exact.append(win)
    pool = exact or fuzzy
    if not pool:
        return None
    return min(pool, key=lambda w: (w.area, w.hwnd))
