"""
TCSInterpreter — walks the CaseNode AST and executes it.

Lifecycle:
  1. @setup steps  (if any step fails → abort entire case)
  2. main steps    (step fails → record, continue)
  3. @teardown steps (always executed, even if earlier steps failed)

Routing: case.app + case.os → EngineRegistry → engine instance

Result directory:
  result/<YYYYMMDD-HHmmss>/<case_file>.result/
    ├── result.json
    └── step_NN_<action>.png  (截图直接存放，无子目录)
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from uita.core.actions.base import ActionContext, ActionResult, ActionStatus
from uita.core.actions.common import (  # noqa: F401
    action_assert,
    action_click,
    action_scroll,
    action_switch,
    action_type,
    action_wait,
)

from uita.core.actions.common._page_hash_helper import HASH_VERIFY_ACTIONS, capture_page_hash
from uita.core.actions.judgers.l8_integration import build_vision_client_safe, configure_l8_fallback
from uita.core.actions.registry import ActionNotFoundError, get_action
from uita.core.dsl.nodes import ActionNode, AssertNode, CaseNode, ConditionNode, SetNode, StepNode, WaitNode
from uita.core.dsl.parser import infer_app_from_path
from uita.core.runner.condition_eval import evaluate_condition, iter_effective_body
from uita.core.engine.api_base import ApiEngineBase
from uita.core.engine.base import EngineBase
from uita.core.hooks.base import HookContext, HookManager, LastAction
from uita.core.hooks.registry import build_hooks
from uita.core.registry_common import effective_platform_for_case, platform_matches_host
from uita.core.resolver.resolver import Resolver
from uita.core.lifecycle.app_lifecycle_stack import build_engine_for_app
from uita.core.lifecycle.engine_lifecycle import prepare_engine_for_case
from uita.core.result.collector import ResultCollector, StepResult, make_run_result_dir
from uita.config import (
    ENABLE_PAGE_HASH_VERIFY,
    ROUTER_PROXY_CANDIDATE_DIR,
    UITA_ENABLE_L8_ASSERT,
    UITA_L8_PROVIDER,
    UITA_L8_QUESTION_TEMPLATE,
)

logger = logging.getLogger(__name__)

# 2026-08-25 新增：switch[api] 引擎类型守卫允许列表（见计划文档
# docs/plans/done/archive_202608.md §2.4）。
_API_MODE_ONLY_ACTIONS = frozenset({"get", "post", "put", "delete", "api_assert"})
_API_MODE_ALLOWED_ACTIONS = frozenset({"switch", "switch_back"}) | _API_MODE_ONLY_ACTIONS


class InterpreterError(Exception):
    pass


class TCSInterpreter:
    def __init__(
        self,
        case: CaseNode,
        case_file: str = "",
        result_dir: str = "",
        on_step=None,
        hook_names: Optional[list] = None,
        engine: Optional[EngineBase] = None,
        # 向后兼容旧参数名（忽略）
        result_path: str = "",
        failure_reviewer=None,
    ):
        """
        Args:
            case:        已解析的 CaseNode
            case_file:   .tcs 源文件路径（用于目录命名）
            result_dir:  指定结果目录（不指定则由 make_run_result_dir 自动生成）
            on_step:     可选回调 async (step_name, status, duration_ms, message) -> None
                         每步执行完后立即调用，用于实时打印进度
            hook_names:  Hook 名称列表（如 ["screenshot"]），None 则使用 settings 默认策略
            engine:      外部已准备好的引擎实例（可选）。传入时本 Interpreter 不再自行
                         调用 _build_engine()（不重复冷启动），run() 结束时也不会
                         close() 该引擎——生命周期完全交由调用方管理（见 batch
                         的跨用例引擎复用场景）。默认 None 时行为与改动前完全一致。
            result_path: 已废弃，保留兼容，忽略
        """
        self._case = case
        self._case_file = case_file
        self._result_dir = result_dir or make_run_result_dir(case_file or (case.name or "unnamed") + ".tcs")
        self._on_step = on_step
        self._failure_reviewer = failure_reviewer
        self._hook_names: list = self._resolve_hook_names(hook_names)
        self._external_engine: Optional[EngineBase] = engine
        self._engine: Optional[EngineBase] = None
        self._resolver: Optional[Resolver] = None
        self._collector: Optional[ResultCollector] = None
        self._hook_manager: Optional[HookManager] = None
        from uita.core.runner.execution_profile import load_execution_profile
        self._execution_profile = load_execution_profile(case_file)


    @staticmethod
    def _resolve_hook_names(hook_names: Optional[list]) -> list:
        """解析 hook 名称列表：None 时读 settings 默认策略。"""
        if hook_names is not None:
            return list(hook_names)
        try:
            from uita.config import SCREENSHOT_ON_EACH_STEP
            if SCREENSHOT_ON_EACH_STEP:
                return ["screenshot"]
        except ImportError:
            pass
        return []

    @property
    def result_dir(self) -> str:
        return self._result_dir

    async def run(self) -> ResultCollector:
        case = self._case
        self._collector = ResultCollector(
            case_name=case.name or "unnamed",
            case_file=self._case_file,
            result_dir=self._result_dir,
        )

        # ---- os 平台匹配检查（空/universal 总是匹配；声明具体平台则须与当前
        # 宿主机一致，否则整条用例跳过，不启动引擎）----
        if not platform_matches_host(case.os):
            logger.info(
                "用例声明 os=%r，与当前宿主机平台不匹配，跳过执行", case.os
            )
            self._collector.record_step(
                "os_check", "skipped",
                message=f"用例声明 os={case.os!r}，与当前宿主机平台不匹配，已跳过",
            )
            self._collector.write_json()
            return self._collector

        # ---- bootstrap engine ----
        try:
            if self._external_engine is not None:
                # 外部已准备好的引擎（调用方已完成生命周期管理，如 batch
                # _ensure_engine），跳过内部冷启动
                self._engine = self._external_engine
            else:
                self._engine = await self._build_engine()
            self._resolver = await self._build_resolver()
            # CaseContext：set:/if empty[${var}] 共用的变量存储（挂在 engine 实例上
            # 的动态属性，与 core/runner/streaming_runner.py 保持一致的接入方式，见
            # docs/plans/archive/02-dsl-actions-实施纪要.md §3.7 风险 1）。
            from uita.core.runner.case_context import CaseContext
            setattr(self._engine, "_case_context", CaseContext())
        except Exception as exc:
            logger.error("Failed to initialize engine: %s", exc)
            self._collector.record_step("engine_init", "failed", message=str(exc))
            self._collector.write_json()
            return self._collector

        # ---- 构建 HookManager ----
        hooks = build_hooks(self._hook_names)
        self._hook_manager = HookManager(hooks) if hooks else None

        hook_ctx = HookContext(
            case_name=case.name or "unnamed",
            case_file=self._case_file,
            report_dir=self._result_dir,
            engine=self._engine,
        )

        async def _emit_hook(stage: str) -> None:
            if self._hook_manager:
                await self._hook_manager.emit(stage, hook_ctx)

        await _emit_hook("case_start")

        # ---- @setup ----
        setup_ok = True
        step_index = 0
        setup_num = 1
        for step in case.setup_steps:
            result, last_action, action_records = await self._run_step(step, step_index)
            sr = await self._record_with_hook(
                step, result, last_action, action_records, step_index, hook_ctx, _emit_hook
            )
            await self._emit_step(step.name, result, setup_num, "setup")
            step_index += 1
            setup_num += 1
            if not result.passed:
                setup_ok = False
                logger.error("@setup step '%s' failed — aborting case", step.name)
                break

        # ---- main steps (only if setup passed) ----
        main_num = 1
        if setup_ok:
            for step in case.steps:
                result, last_action, action_records = await self._run_step(step, step_index)
                review_port = getattr(self._failure_reviewer, "review_action_result", None)
                if not result.passed and callable(review_port):
                    async def _review_run_step(original_step):
                        outcome, _, records = await self._run_step(original_step, step_index)
                        action_records.extend(records)
                        return outcome

                    try:
                        reviewed = await review_port({
                            "case_name": case.name, "case_file": self._case_file,
                            "phase": "main", "step": step, "step_index": step_index,
                            "action_result": result, "collector": self._collector,
                            "engine": self._engine, "resolver": self._resolver,
                            "app_name": case.app, "run_step": _review_run_step,
                        })
                        if isinstance(reviewed, ActionResult):
                            result = reviewed
                    except Exception as exc:
                        logger.warning("read-only failure review unavailable: %s", type(exc).__name__)
                await self._record_with_hook(
                    step, result, last_action, action_records, step_index, hook_ctx, _emit_hook
                )
                await self._emit_step(step.name, result, main_num, "main")
                step_index += 1
                main_num += 1
                if not result.passed:
                    logger.warning("Step '%s' failed: %s", step.name, result.message)
                    if case.fail_fast:
                        logger.error("fail_fast — aborting remaining main steps")
                        break

        # ---- case_end hook ----
        hook_ctx.case_result = self._collector.finalize()
        await _emit_hook("case_end")

        # ---- @teardown (always) ----
        # 2026-07-29：teardown 失败仅记 warning，不让 case 整体 fail。
        # teardown 是清理步骤，主体（main+setup）已通过则视为 passed。
        # 用 "skipped" 标记（不是 "failed"），与 collector.failed 计数兼容。
        teardown_num = 1
        for step in case.teardown_steps:
            result, last_action, action_records = await self._run_step(step, step_index)
            await self._record_with_hook(
                step, result, last_action, action_records, step_index, hook_ctx, _emit_hook
            )
            if not result.passed:
                logger.warning(
                    "[interpreter] teardown step '%s' failed (treated as warning, "
                    "case 主体已过): %s",
                    step.name, result.message,
                )
                # 覆盖最近一个 step 的 status 为 skipped（不影响 failed 计数）
                last_sr = self._collector._result.steps[-1]
                last_sr.status = "skipped"
            await self._emit_step(step.name, result, teardown_num, "teardown")
            step_index += 1
            teardown_num += 1

        # ---- close engine ----
        # 外部注入的引擎不在此关闭，生命周期交由调用方统一管理（如 batch
        # _run_loop 循环结束/超时/异常时才 _kill_engine_safe）
        if self._external_engine is None:
            try:
                await self._engine.close()
            except Exception as exc:
                logger.warning("Engine close error: %s", exc)

        self._collector.write_json()
        return self._collector

    async def _record_with_hook(
        self,
        step: StepNode,
        result,
        last_action,
        action_records: list,
        step_index: int,
        hook_ctx: HookContext,
        emit_hook,
    ) -> StepResult:
        """触发 step_end hook，将 action_records 写入 collector。"""
        # 最后一张有截图的 action 作为 step 级 screenshot（向后兼容）
        last_screenshot = next(
            (r.screenshot for r in reversed(action_records) if r.screenshot), None
        )
        sr = StepResult(
            name=step.name,
            status=result.status.value,
            duration_ms=result.duration_ms,
            message=result.message,
            screenshot=last_screenshot,
            actions=action_records,
        )
        hook_ctx.step_index = step_index
        hook_ctx.step_name = step.name
        hook_ctx.step_result = sr
        hook_ctx.last_action = last_action
        await emit_hook("step_end")
        self._collector.record_step(
            step.name, result.status.value, result.duration_ms, result.message,
            last_screenshot, action_records,
        )
        return sr

    async def _emit_step(self, step_name: str, result, step_num: int = 0, phase: str = "main") -> None:
        """触发 on_step 回调（异常安全）。

        Args:
            step_name:  步骤名称
            result:     ActionResult
            step_num:   当前阶段内的序号（setup 用全局 index，main 从 0 开始，teardown 用全局 index）
            phase:      "setup" | "main" | "teardown"
        """
        if self._on_step is None:
            return
        try:
            await self._on_step(step_name, result.status.value, result.duration_ms, result.message,
                                step_num, phase)
        except Exception as exc:
            logger.debug("on_step 回调异常: %s", exc)

    # ------------------------------------------------------------------

    def _resolve_app_name(self) -> str:
        """三级兜底：case.app 声明值 → 从 case_file 路径推断（cases/<app>/...）→
        最终硬编码兜底 "sample_web"（见 docs/plans/20260702180700-...）。"""
        case = self._case
        return case.app or infer_app_from_path(self._case_file) or "sample_web"

    async def _build_engine(self) -> EngineBase:
        case = self._case
        app_name = self._resolve_app_name()
        os_name = effective_platform_for_case(case.os)
        engine = build_engine_for_app(app_name, os_name)

        # 用例头字段 browser_mode 注入引擎（须在 prepare_engine_for_case 触发 launch
        # 之前）。用例自声明浏览器接入模式（如 managed_chrome_cdp = 接入受 SampleAgent 管控的
        # 真实 Chrome），使执行命令保持统一、无需任何外部环境变量/命令行参数。
        # 引擎无该属性时静默跳过（非 Playwright 引擎零影响）。
        browser_mode = getattr(case, "browser_mode", "") or ""
        if browser_mode and hasattr(engine, "browser_mode"):
            engine.browser_mode = browser_mode

        # 通过统一生命周期策略启动引擎（读取 settings 中的重启配置）
        await prepare_engine_for_case(
            engine,
            case_name=case.name or "unnamed",
            is_first=True,   # interpreter 每次只运行一条用例，始终视为"首条"
            tag_prefix="runner",
        )

        # 把本 case 的根引擎登记进 AppLifecycleStack，供 switch[]/run_exe[]
        # 在本 case 内首次调用时 switch_back[] 能正确切回（见
        # docs/plans/archive/01-engine-lifecycle-实施纪要.md §11）。
        from uita.core.lifecycle.app_lifecycle_stack import get_default_stack
        get_default_stack().register_root(app_name, os_name, engine)
        return engine

    async def _build_resolver(self) -> Resolver:
        case = self._case
        app_name = self._resolve_app_name()
        os_name = effective_platform_for_case(case.os)
        return await self._build_resolver_for_app(app_name, os_name)

    async def _build_resolver_for_app(self, app_name: str, os_name: str) -> Resolver:
        """为指定 app 构造 resolver（switch[] 切换后重建用，M10 新增）。

        与 _build_resolver() 逻辑一致，区别是 app_name 来自 switch[] 参数
        而非 case.app 头字段。

        组装逻辑（含 L7 视觉指纹/L8 OCR/L7 assert_fingerprint/L8 视觉大模型全部降级层
        装配）已下沉至 core/runner/resolver_factory.py::build_resolver_for_app()，
        与 batch_runner.py/profile_engine_switch.py 三处完全共用，2026-07-27
        起不再有 interpreter 专属差异，见
        docs/plans/archive/03-resolver-rosetta-实施纪要.md。
        """
        from uita.core.runner.resolver_factory import build_resolver_for_app
        return build_resolver_for_app(self._engine, app_name, os_name)

    async def _sync_engine_after_switch(self) -> None:
        """switch[]/switch_back[] 成功后，同步 interpreter 的 self._engine /
        self._resolver（M10 新增；2026-08-25 新增 API 模式分支，见计划文档
        docs/plans/done/archive_202608.md §2.4）。

        SwitchAction/SwitchBackAction 内部已调 stack.switch_to()/switch_back()
        或 api_slot.activate()/deactivate()，此方法仅做"同步"，不做业务判断。

        API 模式优先判断：`switch[api]` 成功后 `api_slot.is_active` 为
        True，直接把 self._engine 指向 ApiEngineBase 实例，self._resolver
        置空（API 模式下 UI Action 已被 `_dispatch_action()` 守卫拦截，不会
        有代码路径尝试用 None resolver 解析元素）。`switch_back[]` 退出 API
        模式后 `api_slot.is_active` 已变回 False（SwitchBackAction 内部先
        deactivate 再返回），走原有 AppLifecycleStack 同步逻辑——栈内容在
        整个 API 模式期间从未被触碰，原有 UI 引擎天然保持不变，无需额外
        保存/恢复逻辑。

        CaseContext 迁移（2026-08-26 修复）：`_case_context` 是挂在 engine 实例上
        的动态属性（见 run() 初始化处），切换后新 engine 没有该属性，后续
        `set:` / `if empty[${var}]` / `exec_cmd[$var]` 会失败并报"引擎未绑定
        CaseContext"。必须把同一个 CaseContext 实例平移到新 engine——用例变量是
        **case 级**作用域，跨 app 切换后必须保持可见（例如 `switch[sample_agent]` 后
        `set: cmd = "..."`，再 `exec_cmd[$cmd]`）。
        `core/runner/streaming_runner.py` 的 step 内切换分支已有同样处理，此处
        补齐以消除双 runner 行为分叉。
        """
        from uita.core.lifecycle.api_engine_slot import get_default_api_slot
        from uita.core.lifecycle.app_lifecycle_stack import get_default_stack
        from uita.core.registry_common import effective_platform_for_case

        api_slot = get_default_api_slot()
        if api_slot.is_active:
            self._engine = api_slot.current
            self._resolver = None
            logger.info("switch 完成：engine=%s（API 模式，resolver 已置空）",
                        type(self._engine).__name__)
            return

        stack = get_default_stack()
        mgr = stack.current
        if mgr is None:
            logger.warning("switch 后 stack.current 为 None，跳过 engine/resolver 同步")
            return

        case_ctx = getattr(self._engine, "_case_context", None)
        self._engine = mgr.engine
        # 同一个实例平移，不新建：新建会丢掉切换前已赋值的变量。
        if case_ctx is not None and getattr(self._engine, "_case_context", None) is None:
            try:
                setattr(self._engine, "_case_context", case_ctx)
            except Exception as exc:  # noqa: BLE001 — 引擎禁止动态属性时不阻断切换
                logger.warning("switch 后 CaseContext 迁移失败: %s", exc)
        os_name = effective_platform_for_case(self._case.os)
        self._resolver = await self._build_resolver_for_app(mgr.app_name, os_name)
        logger.info("switch 完成：engine=%s resolver 已重建为 app=%s",
                    type(self._engine).__name__, mgr.app_name)

    # ------------------------------------------------------------------

    async def _run_step(self, step: StepNode, step_index: int = 0) -> tuple:
        """执行 step，返回 (ActionResult, LastAction | None, list[ActionRecord])。"""
        import os as _os
        from uita.core.result.collector import ActionRecord
        from uita.core.runner.execution_profile import profile_step
        _profile_step = profile_step(self._execution_profile, step_index)


        t0 = time.monotonic()
        last_result = ActionResult.ok(f"step '{step.name}' ok")
        last_action: Optional[LastAction] = None
        action_records: list = []
        action_idx = 0

        # L4/L4b LLM prompt 富化：追踪当前 step 名称（见
        # docs/plans/archive/03-resolver-rosetta-实施纪要.md）
        if self._resolver is not None:
            self._resolver.set_step_context(step.name)

        # 截图开关：hook_manager 中含 screenshot hook 时开启
        _enable_ss = self._hook_manager is not None and any(
            h.name == "screenshot" for h in self._hook_manager._hooks
        )
        _screenshot_dir = self._result_dir if _enable_ss else ""

        # IfNode 惰性展开（2026-07-25 新增，见 core/runner/condition_eval.py 顶部
        # 说明）：迭代到 IfNode 时才求值条件，命中分支的子 body 原样并入迭代序列，
        # 下方 isinstance 分支感知不到 IfNode 存在，无需改动既有截图/hash 校验逻辑。
        async for node in iter_effective_body(step.body, self._eval_if_condition):
            target = ""
            _before_screenshot_path: Optional[str] = None
            _page_changed: Optional[bool] = None
            if isinstance(node, ActionNode):
                target = node.args[0] if node.args else ""
                # 执行前 resolve：供 before 截图标注与 last_action.element 使用
                # _NO_RESOLVE_ACTIONS：不需要通用预解析或会携带参数自行解析的动作；
                # image_click 必须由动作自身传递 confidence，避免预解析使用默认阈值。
                # run_exe：args[0] 是本地 exe 路径而非元素名（见
                # docs/plans/archive/01-engine-lifecycle-实施纪要.md），
                # 误 resolve 会白白触发一次真实 L3.5 llm_selector 调用且必然 NOT_FOUND，
                # 与 scroll[direction] 无元素名调用是同一类问题。
                # get/post/put/delete/api_assert（2026-08-25 新增）：API 模式下
                # self._resolver 恒为 None（见 _sync_engine_after_switch），且
                # args[0]/kwargs 是 uri/断言参数而非元素名，同样不应触发通用
                # resolve（见计划文档 20260825090000 §2.4）。
                _NO_RESOLVE_ACTIONS: frozenset[str] = frozenset({
                    "press_key", "soft_reset", "run_exe", "choose_file", "optional_group",
                    "type_focused", "type_focused_slow", "image_click",
                    "goto_page", "wait_load_state", "reload_page",
                    "eval_js",
                    "get", "post", "put", "delete", "api_assert",
                })
                # scroll[direction, amount] 无元素名调用（见 docs/plans/
                # docs/plans/archive/02-dsl-actions-实施纪要.md）：
                # args[0] 是方向词而非元素名，误 resolve 会白白触发一次 L3.5
                # llm_selector 真实 LLM 调用且必然 NOT_FOUND，需与 scroll[element,
                # direction, amount] 三段式区分（后者 args[0] 是真实元素名，仍需
                # 正常走 resolve 标注）。
                _SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})
                _is_scroll_no_element = (
                    node.action == "scroll" and target.lower() in _SCROLL_DIRECTIONS
                )
                action_element = None
                _action_resolve_result = None
                _action_label: Optional[str] = None
                if target and _enable_ss:
                    if node.action in _NO_RESOLVE_ACTIONS or _is_scroll_no_element:
                        # 不走 resolve，截图直接标注动作文字
                        _action_label = f"{node.action}[{target}]"
                    else:
                        try:
                            # skip_l4=True：截图标注不触发 L4 VL（不做 LLM 视觉调用）
                            _action_resolve_result = await self._resolver.resolve(
                                target, skip_l4=True)
                            if _action_resolve_result.found:
                                action_element = _action_resolve_result.element
                        except Exception:
                            pass
                # BEFORE 截图：元素存在→画框；无元素 action→蓝色文字；not found→红色文字
                # 导航/等待类 action 跳过 before 截图（导航前页面状态价值低，
                # 且 L1 截图等字体加载可能额外卡 10s）
                _NO_BEFORE_SCREENSHOT_ACTIONS: frozenset[str] = frozenset({
                    "goto_page", "wait_load_state", "reload_page",
                    # 进程生命周期动作可能让当前原生窗口立即失效；继续截图会把
                    # WinUIA 的单线程执行器阻塞在已销毁窗口上。
                    "powershell", "kill_process",
                })
                if _enable_ss and target and node.action not in _NO_BEFORE_SCREENSHOT_ACTIONS:
                    from uita.core.hooks.builtin.action_screenshotter import take_action_screenshot_before
                    _before_screenshot_path = await take_action_screenshot_before(
                        engine=self._engine,
                        keyword=node.action,
                        target_name=target,
                        resolve_result=_action_resolve_result,
                        screenshot_dir=_screenshot_dir,
                        step_index=step_index,
                        action_index=action_idx,
                        action_label=_action_label,
                    )
                # 操作前后页面 hash 对比验证（默认关闭，见
                # docs/plans/archive/02-dsl-actions-实施纪要.md）
                _do_hash_check = ENABLE_PAGE_HASH_VERIFY and node.action in HASH_VERIFY_ACTIONS
                _before_hash = await capture_page_hash(self._engine) if _do_hash_check else None

                result = await self._dispatch_action(node, profile_step=_profile_step, action_index=action_idx)
                last_action = LastAction(keyword=node.action, element=action_element)

                if _do_hash_check and result.passed:
                    _after_hash = await capture_page_hash(self._engine)
                    if _before_hash and _after_hash:
                        _page_changed = _before_hash != _after_hash
                        if not _page_changed:
                            logger.warning(
                                "[page-hash] %s[%s] 执行后页面 hash 未变化，操作可能未生效",
                                node.action, target,
                            )

                # M10：switch / switch_back 成功后更新 engine + resolver
                # 为目标 app 的实例（跨引擎类型切换天然支持）。
                # 计划文档：docs/plans/archive/02-dsl-actions-实施纪要.md §3.3
                # run_exe 成功后同理需要同步（2026-07-28 新增，见
                # docs/plans/archive/01-engine-lifecycle-实施纪要.md）
                if result.passed and node.action in ("switch", "switch_back", "run_exe"):
                    try:
                        await self._sync_engine_after_switch()
                    except Exception as exc:
                        logger.warning("switch 后 engine/resolver 同步异常: %s", exc)
            elif isinstance(node, WaitNode):
                # 2026-08-25 新增：API 模式下（self._resolver 为 None）
                # wait[<element>] 带元素名调用无法解析，清晰失败而非崩溃
                # （见计划文档 20260825090000 §2.4，与 Action 守卫同一
                # 设计原则："调用即报错"）。裸时长 wait[Ns] 无 target，
                # 不依赖 resolver，不受影响。
                if self._resolver is None and node.target:
                    result = ActionResult.fail(
                        f"wait[{node.target}]: 当前处于 API 模式（switch[api] "
                        f"生效中），不支持按元素名等待，请先 switch_back[] 切回 UI 引擎"
                    )
                    last_action = LastAction(keyword="wait", element=None)
                else:
                    result, elem = await self._dispatch_wait_with_element(node)
                    target = node.target or ""
                    last_action = LastAction(keyword="wait", element=elem)
                    # wait 成功时，尝试从元素名推断当前页面上下文（含"标题"/"页面"等关键词）
                    if result.passed and target and self._resolver is not None:
                        self._resolver.update_page_context_from_name(target)
            elif isinstance(node, AssertNode):
                # 同上：API 模式下 assert[<element>] 无法解析，清晰失败而非崩溃。
                result, elem = await self._dispatch_assert_with_element(node)
                target = node.target
                last_action = LastAction(keyword=node.keyword or "assert", element=elem)
            elif isinstance(node, SetNode):
                result = await self._dispatch_set(node)
                last_action = LastAction(keyword="set", element=None)
            else:
                continue

            last_result = result
            failed = not result.passed

            # action 级截图（按规则过滤）：
            #   操作类（click/type/scroll/press_key/set 等）→ 每次截图
            #   hover → 跳过 done 截图（page.screenshot() 在 Electron 中可能触发
            #           窗口级事件导致 hover 状态丢失，破坏后续 Popover 断言）
            #   assert/verify → 仅截图（pass 或 fail 均截，重试中间不截，已是单次结果）
            #   wait[element]  → 截图（等待到元素后截）
            #   wait[Ns] 纯时长 → 不截图
            _is_bare_wait = isinstance(node, WaitNode) and node.target is None
            _is_hover = isinstance(node, ActionNode) and node.action == "hover"
            _is_screenshot_unsafe = (
                isinstance(node, ActionNode)
                and node.action in {"powershell", "kill_process"}
            )
            screenshot_path: Optional[str] = None
            if (
                _enable_ss
                and last_action is not None
                and not _is_bare_wait
                and not _is_hover
                and not _is_screenshot_unsafe
            ):
                from uita.core.hooks.builtin.action_screenshotter import take_action_screenshot
                # ActionNode 操作后截图使用 "done" 后缀（before < done 字母序符合时序）；
                # assert/wait 沿用历史命名（无后缀）
                _ss_suffix = "done" if isinstance(node, ActionNode) else ""
                screenshot_path = await take_action_screenshot(
                    engine=self._engine,
                    last_action=last_action,
                    screenshot_dir=_screenshot_dir,
                    step_index=step_index,
                    action_index=action_idx,
                    failed=failed,
                    suffix=_ss_suffix,
                )

            action_records.append(ActionRecord(
                action=last_action.keyword if last_action else "unknown",
                target=target,
                status="failed" if failed else "passed",
                screenshot=screenshot_path,
                screenshot_before=_before_screenshot_path,
                existence_strategy=(result.extra or {}).get("existence_strategy"),
                page_changed=_page_changed,
            ))
            action_idx += 1

            if failed:
                break

        last_result.duration_ms = (time.monotonic() - t0) * 1000
        return last_result, last_action, action_records

    async def _dispatch_action(
        self,
        node: ActionNode,
        profile_step: Optional[dict] = None,
        action_index: int = 0,
    ) -> ActionResult:
        # 2026-08-25 新增：引擎类型守卫（switch[api] 模式），见计划文档
        # docs/plans/done/archive_202608.md §2.4。
        # 唯一改动点：UI 用例的 self._engine 恒为 EngineBase 实例，
        # isinstance(..., ApiEngineBase) 恒为 False，两个分支均不生效，
        # 对既有全部 UI 用例零回归。
        if isinstance(self._engine, ApiEngineBase):
            if node.action not in _API_MODE_ALLOWED_ACTIONS:
                return ActionResult.fail(
                    f"{node.action}[...]: 当前处于 API 模式（switch[api] 生效中），"
                    f"不支持 UI Action，请先 switch_back[] 切回 UI 引擎"
                )
        elif node.action in _API_MODE_ONLY_ACTIONS:
            return ActionResult.fail(
                f"{node.action}[...]: 当前未处于 API 模式，请先执行 switch[api]"
            )

        try:
            active_app = getattr(self._engine, "app_name", "") or self._resolve_app_name()
            action_cls = get_action(node.action, app=active_app)
        except ActionNotFoundError as exc:
            return ActionResult.fail(str(exc))
        extra = {
            "profile_step": profile_step or {},
            "profile_action": (profile_step or {}).get("action") or {},
            "profile_assertion": (profile_step or {}).get("assertion") or {},
            "profile_patterns": (profile_step or {}).get("patterns") or [],
            "profile_action_index": action_index,
            "screenshot_dir": self._result_dir,
            "step_index": action_index,
        }
        ctx = ActionContext(
            engine=self._engine, resolver=self._resolver, args=node.args, kwargs=node.kwargs, extra=extra,
        )
        return await action_cls().execute(ctx)


    async def _dispatch_wait(self, node: WaitNode) -> ActionResult:
        result, _ = await self._dispatch_wait_with_element(node)
        return result

    async def _dispatch_assert(self, node: AssertNode) -> ActionResult:
        result, _ = await self._dispatch_assert_with_element(node)
        return result

    async def _dispatch_wait_with_element(self, node: WaitNode):
        from uita.core.actions.common.action_wait import WaitAction
        ctx = ActionContext(
            engine=self._engine,
            resolver=self._resolver,
            args=[node.target] if node.target else [],
            extra={"wait_node": node},
        )
        result = await WaitAction().execute(ctx)
        # 优先复用 WaitAction 回传的 resolve 结果（ctx.extra["_wait_resolve_result"]），
        # 避免为截图标注再做一次全链路 resolve。无该 key（失败/超时/裸时长 wait）时
        # 不 resolve——元素不存在，截图标注也不需要坐标。
        # 见 docs/plans/archive/02-dsl-actions-实施纪要.md。
        element = None
        if node.target and result.passed:
            cached = ctx.extra.pop("_wait_resolve_result", None)
            if cached is not None and cached.found:
                element = cached.element
        return result, element

    async def _dispatch_assert_with_element(self, node: AssertNode):
        from uita.core.actions.common.action_assert import AssertAction, split_or_targets
        # 纯 JS 断言元素（rosetta_map 只有 js 字段无 selector）跳过 resolve，
        # 因为没有视觉位置可标注，避免无意义的 LLM 链路调用。OR 逻辑
        # （assert[A|B]，2026-08-06 新增）：逐候选尝试，第一个 resolve 命中即
        # 停止用于截图高亮；无 `|` 时候选只有一个，行为与改造前一致（零回归）。
        element = None
        prewarmed_resolve: dict = {}
        from uita.core.actions.judgers import JudgerDispatchError, needs_element
        for target in split_or_targets(node.target):
            try:
                require_element = needs_element(node.kwargs, target)
            except JudgerDispatchError as exc:
                return ActionResult.fail(f"assert: {exc}"), None
            if not require_element:
                continue
            if self._resolver is None:
                return ActionResult.fail(
                    f"assert[{node.target}]: 当前处于 API 模式（switch[api] 生效中），"
                    "不支持按元素名断言，请使用 api_assert[] 或先 switch_back[] 切回 UI 引擎"
                ), None
            if self._resolver.get_js_expr(target):
                continue
            r = await self._resolver.resolve(target)
            prewarmed_resolve[target] = r
            if r.found:
                element = r.element
                break
        ctx = ActionContext(
            engine=self._engine,
            resolver=self._resolver,
            args=[node.target],
            kwargs=node.kwargs,
            extra={"assert_node": node, "prewarmed_resolve": prewarmed_resolve},
        )
        result = await AssertAction().execute(ctx)
        return result, element

    async def _dispatch_set(self, node: SetNode) -> ActionResult:
        import time as _time
        t0 = _time.monotonic()
        if not node.source_action:
            return ActionResult.fail(f"set: 语句缺少 source_action，var={node.var_name}")

        case_ctx = getattr(self._engine, "_case_context", None)
        if case_ctx is None:
            return ActionResult.fail(
                f"set: {node.var_name} — 引擎未绑定 CaseContext，无法赋值"
            )

        # raw 模式：直接赋值字符串，不走 Action 注册表
        if node.source_action == "raw":
            raw_val = node.source_args[0] if node.source_args else ""
            case_ctx.set(node.var_name, raw_val)
            return ActionResult.ok(
                f"set {node.var_name} = '{raw_val[:60]}'",
                duration_ms=(_time.monotonic() - t0) * 1000,
            )

        action_node = ActionNode(
            action=node.source_action, args=node.source_args,
            kwargs=node.source_kwargs, raw=node.raw,
        )
        result = await self._dispatch_action(action_node)
        if not result.passed:
            return ActionResult.fail(
                f"set: {node.var_name} — source_action '{node.source_action}' 失败: {result.message}",
                duration_ms=(_time.monotonic() - t0) * 1000,
            )

        case_ctx.set(node.var_name, result.message)

        return ActionResult.ok(
            f"set {node.var_name} = '{result.message}'",
            duration_ms=(_time.monotonic() - t0) * 1000,
        )

    async def _eval_if_condition(self, cond: ConditionNode) -> bool:
        """绑定 self._engine/self._resolver/CaseContext 的条件求值闭包，供
        core/runner/condition_eval.py::iter_effective_body() 调用（IfNode 惰性
        展开）。见 docs/plans/archive/02-dsl-actions-实施纪要.md。"""
        case_ctx = getattr(self._engine, "_case_context", None)
        return await evaluate_condition(cond, self._engine, self._resolver, case_ctx)
