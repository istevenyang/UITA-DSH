"""
AST Node definitions for TCS (TestCaseScript).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, Union


@dataclass
class ActionNode:
    """Represents a single action statement, e.g. click[xxx], type[xxx, text]."""
    action: str          # action name: click / type / press_key ...
    args: list[str]      # positional arguments
    kwargs: dict[str, str] = field(default_factory=dict)  # key=value 形式的关键字参数（P2 新增）
    raw: str = ""        # original source line (for error messages)


@dataclass
class WaitNode:
    """Represents wait: [element, timeout=Ns] or wait: Ns.

    state=gone（2026-08-21 新增，见 docs/plans/20260821100000-hotkey-edge-
    hover-floatball-menu-tray-transient-ui-solution.md S2）：反向轮询语义，
    等待元素"消失"（找不到 / 不可见）而非默认的"出现"。用于边缘唤醒浮层/
    悬浮球等"鼠标移开后状态回退"场景的验证。裸时长 wait（target=None）不受
    影响；不声明 state=（默认 "visible"）时行为与改造前完全一致，零回归。
    """
    target: Optional[str] = None   # element name; None means bare duration wait
    timeout_s: float = 10.0        # timeout in seconds (default 10s)
    state: str = "visible"         # "visible"（默认）| "gone"
    #: 轮询间隔（秒）。默认 1.0 与改造前硬编码一致；``poll=200ms`` 可细粒度等待
    #: （性能类用例需要 <1s 精度时用）。
    poll_interval_s: float = 1.0
    #: ``True`` 时每轮**只**做 L3.8 视觉指纹匹配（``Resolver.resolve_visual``），
    #: 不碰 SEMANTIC/ROLE/LLM 等策略。默认 ``False`` = 走原全链（零回归）。
    #: 注意：只在 rosetta_map 里配了 ``pattern_*`` 的元素上才有意义。
    visual_only: bool = False
    raw: str = ""


@dataclass
class AssertNode:
    """Represents assert: [xxx] or verify: [xxx]."""
    target: str = ""              # element name or text to assert
    keyword: str = "assert"       # "assert" | "verify"
    kwargs: dict[str, str] = field(default_factory=dict)  # contains=/count_min=/js= 等判定器 kwarg（P2 新增）
    raw: str = ""


@dataclass
class SetNode:
    """Represents set: var_name = action[element]."""
    var_name: str = ""            # variable name to store into
    source_action: str = ""       # action keyword: get_text / get_attribute / ...
    source_args: list[str] = field(default_factory=list)
    raw: str = ""
    source_kwargs: dict[str, str] = field(default_factory=dict)


@dataclass
class ConditionNode:
    """Represents a single if/elif condition expression（含 not/empty 修饰）。

    见 docs/plans/archive/02-dsl-actions-实施纪要.md §3.2/§3.3。

    Attributes:
        raw_expr:       方括号内原始文本（未经 CaseContext.render() 渲染），
                         如 "text=xxx" / "元素描述" / "${var}"
        negate:         True 表示 not[...] 或 ![...] 修饰（内部归一化为同一表示，
                         不区分书写形式）
        is_empty_check: True 表示 empty[...] 或 not empty[...]/!empty[...] 修饰；
                         empty 与 negate 可同时为 True（对应 not empty[]/!empty[]）
    """
    raw_expr: str = ""
    negate: bool = False
    is_empty_check: bool = False
    raw: str = ""


@dataclass
class IfNode:
    """Represents if/elif/else 控制流块。

    branches[0] 是 if，后续（若有）是 elif，统一列表结构，不单独区分首个 if
    （简化 interpreter/streaming_runner 侧遍历逻辑）。else_body 为空列表表示
    无 else 分支。
    """
    branches: list[tuple[ConditionNode, list["StepBodyNode"]]] = field(default_factory=list)
    else_body: list["StepBodyNode"] = field(default_factory=list)
    raw: str = ""


# Union type for step body nodes
StepBodyNode = Union[ActionNode, WaitNode, AssertNode, SetNode, IfNode]


@dataclass
class StepNode:
    """Represents a single test step."""
    name: str
    body: list[StepBodyNode] = field(default_factory=list)
    is_setup: bool = False
    is_teardown: bool = False


@dataclass
class CaseNode:
    """Root AST node for a TCS test case file."""
    name: str = ""
    app: str = ""
    os: str = ""
    priority: str = ""
    owner: str = ""
    zyid: str = ""       # 外部用例 ID（可选，逗号分隔支持多个）
    description: str = ""
    tags: list[str] = field(default_factory=list)
    retryable: bool = True         # 用例失败后是否允许自动重试（batch 模式本地重跑消费此字段，默认真）
    fail_fast: bool = False        # 主流程任一步失败后是否停止后续 main steps；teardown 始终执行
    browser_mode: str = ""         # 浏览器接入模式（可选）。默认空 = 引擎按 app 配置常规启动；
                                    # "managed_chrome_cdp" = 接入本机 9222 调试端口的受管 Chrome
    setup_steps: list[StepNode] = field(default_factory=list)
    teardown_steps: list[StepNode] = field(default_factory=list)
    steps: list[StepNode] = field(default_factory=list)
