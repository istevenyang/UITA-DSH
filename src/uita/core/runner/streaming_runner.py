"""
StreamingRunner — 逐步执行 TCS 用例，每步完成后触发回调。

设计：
  - 不复用 TCSInterpreter（避免耦合），直接操作 AST + Resolver
  - Hook 四阶段：case_start / step_start / step_end / case_end（finally）
  - @teardown 在 case_end 之后执行（始终运行）
  - on_step 回调异常安全降级，不中断用例
"""
from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import time
from typing import Any, Awaitable, Callable, Optional

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
from uita.core.actions.registry import ActionNotFoundError, get_action
from uita.core.dsl.nodes import ActionNode, AssertNode, CaseNode, SetNode, StepNode, WaitNode
from uita.core.engine.api_base import ApiEngineBase
from uita.core.engine.base import EngineBase
from uita.core.hooks.base import HookContext, HookManager, LastAction
from uita.core.runner.condition_eval import evaluate_condition, iter_effective_body
from uita.core.resolver.resolver import Resolver
from uita.core.result.collector import CaseResult, ResultCollector, StepResult
from uita.core.runner.case_context import CaseContext
from uita.config import ENABLE_PAGE_HASH_VERIFY, NOTICE

# 2026-08-25 新增：switch[api] 引擎类型守卫允许列表，与 core/interpreter.py
# 保持完全一致（见计划文档 20260825090000-switch-api-engine-mode-
# migration.md §2.4），使 --mode batch（本文件）与 --mode runner
# （core/interpreter.py）对 switch[api] 的支持行为一致，避免"单跑支持、
# 批量不支持"的功能割裂。
_API_MODE_ONLY_ACTIONS = frozenset({"get", "post", "put", "delete", "api_assert"})
_API_MODE_ALLOWED_ACTIONS = frozenset({"switch", "switch_back"}) | _API_MODE_ONLY_ACTIONS

logger = logging.getLogger(__name__)


def _hang_trace_start(stage: str, **fields: Any) -> float:
    """记录可能阻塞调用入口；不记录 action 参数，避免泄露输入内容。"""
    details = " ".join(
        f"{key}={str(value).replace(chr(10), ' ').replace(chr(13), ' ')}"
        for key, value in fields.items()
        if value not in (None, "")
    )
    logger.log(NOTICE, "[hang-trace] START stage=%s%s", stage, f" {details}" if details else "")
    return time.monotonic()


def _hang_trace_end(stage: str, started_at: float, **fields: Any) -> None:
    details = " ".join(
        f"{key}={str(value).replace(chr(10), ' ').replace(chr(13), ' ')}"
        for key, value in fields.items()
        if value not in (None, "")
    )
    logger.log(
        NOTICE, "[hang-trace] END stage=%s elapsed_ms=%d%s", stage,
        int((time.monotonic() - started_at) * 1000),
        f" {details}" if details else "",
    )


def _arm_hang_watchdog(case_name: str, step_index: int, step_name: str) -> threading.Timer:
    """step 120 秒未返回时自动输出所有 Python 线程栈；仅诊断，不中断执行。"""
    def _dump() -> None:
        logger.error(
            "[hang-trace] WATCHDOG_TIMEOUT case=%s step=%d name=%s; dumping all thread stacks",
            case_name, step_index + 1, step_name,
        )
        try:
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        except Exception as exc:
            logger.error("[hang-trace] WATCHDOG_DUMP_FAILED: %s", exc)

    timer = threading.Timer(120.0, _dump)
    timer.daemon = True
    timer.start()
    logger.log(
        NOTICE, "[hang-trace] WATCHDOG_ARM case=%s step=%d timeout_s=120",
        case_name, step_index + 1,
    )
    return timer

# 回调签名：(step_index, step_result, case_name) -> None
OnStepCallback = Callable[[int, StepResult, str], Awaitable[None]]


class CaseRetryRequested(RuntimeError):
    """Signal from a step-end guard that current case must restart."""
# 原地自愈回调：runner 只负责在失败点调用和重试，不依赖具体自愈实现。
SelfHealCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


async def run_case_streaming(
    case: CaseNode,
    engine: EngineBase,
    resolver: Resolver,
    collector: ResultCollector,
    *,
    hook_manager: Optional[HookManager] = None,
    on_step: Optional[OnStepCallback] = None,
    result_dir: str = "report",
    app_name: str = "",
    self_heal_callback: Optional[SelfHealCallback] = None,
    repair_mode: bool = False,
    failure_reviewer=None,
) -> CaseResult:

    """逐步执行用例，每步完成后触发 on_step 回调。

    执行顺序：
      case_start
      → @setup steps（任一失败则中止 main steps）
      → main steps（失败记录，继续）
      case_end（finally）
      → @teardown steps（始终执行）

    Args:
        app_name: 用于 action 按 app 维度查找（get_action(name, app=app_name)）。
                  默认 "" 等价于改造前行为（全局查找，零影响）。调用方（如
                  BatchRunner）应传入与构建 resolver 时一致的已解析 app 名，
                  保证同一条用例的 resolver/action 使用同一个 app 归属。

    Returns:
        CaseResult（与 collector.finalize() 同一对象）
    """
    case_name = case.name or "unnamed"
    case_ctx = CaseContext()
    from uita.core.runner.execution_profile import load_execution_profile, profile_step
    execution_profile = load_execution_profile(getattr(collector, "_case_file", ""))

    if repair_mode:
        resolver.set_repair_mode(True)

    # 注入 case_context 到 engine（供 Action 读写跨步骤变量）

    try:
        setattr(engine, "_case_context", case_ctx)
    except Exception:
        pass

    # ── M11: profile step 级 engine/app 切换（仅注册 app 间，见
    # core/runner/profile_engine_switch.py）。profile_step 未声明 engine/app
    # 或无 profile 文件时，resolve_for_step 恒返回 False，engine/resolver
    # 与改动前完全一致（零行为变化）。
    from uita.core.registry_common import effective_platform_for_case
    from uita.core.runner.profile_engine_switch import ProfileEngineSwitcher
    _switcher = ProfileEngineSwitcher(
        default_app=app_name or case.app or "",
        default_platform=effective_platform_for_case(case.os),
        engine=engine,
        resolver=resolver,
    )
    _active_engine = engine
    _active_resolver = resolver
    _active_app = app_name or case.app or ""

    # 注入用例级自愈缓冲（延迟提交模式）
    from uita.core.runner.heal_buffer import HealBuffer
    heal_buf = HealBuffer()
    try:
        resolver.set_heal_buffer(heal_buf)
    except Exception:
        heal_buf = None  # resolver 不支持缓冲时降级为立即写

    hook_ctx = HookContext(
        case_name=case_name,
        report_dir=result_dir,
        engine=engine,
    )

    async def _emit(stage: str) -> None:
        if hook_manager:
            await hook_manager.emit(stage, hook_ctx)

    _trace_t0 = _hang_trace_start("case_hook", case=case_name, hook="case_start")
    await _emit("case_start")
    _hang_trace_end("case_hook", _trace_t0, case=case_name, hook="case_start")

    setup_ok = True
    step_index = 0
    _active_watchdog: Optional[threading.Timer] = None

    async def _exec_step(step: StepNode, phase: str) -> StepResult:
        nonlocal step_index, _active_engine, _active_resolver, _active_app, _active_watchdog
        t0 = time.monotonic()
        t0_wall = time.time()  # 供 network_diagnostics 按时间窗口关联本 step 的网络请求
        hook_ctx.step_index = step_index
        hook_ctx.step_name = step.name
        logger.log(
            NOTICE,
            "[hang-trace] STEP_START case=%s phase=%s step=%d name=%s app=%s engine=%s",
            case_name, phase, step_index + 1, step.name, _active_app,
            type(_active_engine).__name__,
        )
        if _active_watchdog is not None:
            _active_watchdog.cancel()
        _active_watchdog = _arm_hang_watchdog(case_name, step_index, step.name)
        _trace_t0 = _hang_trace_start(
            "step_hook", case=case_name, step=step_index + 1, hook="step_start",
        )
        await _emit("step_start")
        _hang_trace_end(
            "step_hook", _trace_t0, case=case_name, step=step_index + 1,
            hook="step_start",
        )

        # 截图目录和开关（直接保存到 .result 目录）
        _screenshot_dir = result_dir if result_dir else ""
        _enable_ss = bool(_screenshot_dir)

        _profile_step = profile_step(execution_profile, step_index)

        # ── M11: profile step 级 engine/app 切换（仅注册 app 间）──────────
        # 未声明 engine/app 或无 profile 时 resolve_for_step 恒返回 False，
        # _active_engine/_active_resolver 保持与改动前一致（零行为变化）。
        try:
            _trace_t0 = _hang_trace_start(
                "profile_switch", case=case_name, step=step_index + 1,
                app=_active_app, engine=type(_active_engine).__name__,
            )
            if await _switcher.resolve_for_step(_profile_step):
                _active_engine = _switcher.engine
                _active_resolver = _switcher.resolver
                hook_ctx.engine = _active_engine
            _hang_trace_end(
                "profile_switch", _trace_t0, case=case_name, step=step_index + 1,
                app=_active_app, engine=type(_active_engine).__name__,
            )
        except Exception as _switch_exc:
            logger.warning("[%s] profile step engine/app 切换异常（忽略，沿用当前引擎）: %s",
                            case_name, _switch_exc)

        # ── 页面快照（PlaywrightEngine 专属，其他引擎 hasattr 守卫跳过）──
        _has_page_mgmt = hasattr(_active_engine, "snapshot_pages")
        _trace_t0 = _hang_trace_start(
            "snapshot_pages", case=case_name, step=step_index + 1,
            enabled=_has_page_mgmt, engine=type(_active_engine).__name__,
        )
        _before_pages: set = _active_engine.snapshot_pages() if _has_page_mgmt else set()
        _hang_trace_end(
            "snapshot_pages", _trace_t0, case=case_name, step=step_index + 1,
            count=len(_before_pages),
        )

        _trace_t0 = _hang_trace_start(
            "step_body", case=case_name, step=step_index + 1,
            app=_active_app, engine=type(_active_engine).__name__,
        )
        action_result, last_action, action_records = await _run_step_body(
            step, _active_engine, _active_resolver,
            screenshot_dir=_screenshot_dir,
            step_index=step_index,
            enable_screenshot=_enable_ss,
            app_name=_active_app,
            profile_step=_profile_step,
            repair_mode=repair_mode,
        )
        _hang_trace_end(
            "step_body", _trace_t0, case=case_name, step=step_index + 1,
            status=action_result.status.value, actions=len(action_records),
        )
        # switch[] / switch_back[] / run_exe[] mutate AppLifecycleStack. Batch
        # execution must follow stack.current exactly like Interpreter does; otherwise
        # next step keeps stale engine/resolver/app and app-scoped actions mis-dispatch.
        if action_result.passed and any(
            record.action in {"switch", "switch_back", "run_exe"}
            for record in action_records
        ):
            try:
                from uita.core.lifecycle.api_engine_slot import get_default_api_slot
                from uita.core.lifecycle.app_lifecycle_stack import get_default_stack
                from uita.core.runner.resolver_factory import build_resolver_for_app
                api_slot = get_default_api_slot()
                if api_slot.is_active:
                    # switch[api] 生效中：engine 指向 API 引擎，resolver 置空
                    # （见 core/interpreter.py::_sync_engine_after_switch 同构逻辑，
                    # 计划文档 20260825090000 §2.4）。
                    _active_engine = api_slot.current
                    _active_resolver = None
                    hook_ctx.engine = _active_engine
                else:
                    manager = get_default_stack().current
                    if manager is not None:
                        _trace_t0 = _hang_trace_start(
                            "post_switch_resolver", case=case_name, step=step_index + 1,
                            app=manager.app_name, engine=type(manager.engine).__name__,
                        )
                        _active_engine = manager.engine
                        _active_app = manager.app_name
                        # CaseContext 平移（2026-08-26 修复）：`_case_context` 是挂在
                        # engine 实例上的动态属性，跨 step 切换后新 engine 没有它，
                        # 后续 set: / if empty[${var}] / exec_cmd[$var] 会失败。用例
                        # 变量是 case 级作用域，必须跨 app 保持可见。step 内切换分支
                        # 已有同样处理，此处补齐。
                        if case_ctx is not None and getattr(
                            _active_engine, "_case_context", None
                        ) is None:
                            try:
                                setattr(_active_engine, "_case_context", case_ctx)
                            except Exception:
                                pass
                        _active_resolver = build_resolver_for_app(
                            _active_engine, _active_app, manager.platform,
                        )
                        if repair_mode:
                            _active_resolver.set_repair_mode(True)
                        try:
                            _active_resolver.set_heal_buffer(heal_buf)
                        except Exception:
                            pass
                        hook_ctx.engine = _active_engine
                        _hang_trace_end(
                            "post_switch_resolver", _trace_t0, case=case_name,
                            step=step_index + 1, app=_active_app,
                            engine=type(_active_engine).__name__,
                        )
            except Exception as switch_exc:
                logger.warning("[%s] switch 后 engine/resolver/app 同步异常: %s",
                               case_name, switch_exc)

        if not action_result.passed and failure_reviewer is not None:
            from uita.core.runner.agent_runtime.integration import review_failed_step

            async def _review_run_step(original_step):
                outcome, _, records = await _run_step_body(
                    original_step, _active_engine, _active_resolver,
                    screenshot_dir=_screenshot_dir, step_index=step_index,
                    enable_screenshot=_enable_ss, app_name=_active_app,
                    profile_step=_profile_step, repair_mode=repair_mode,
                )
                action_records.extend(records)
                return outcome

            action_result = await review_failed_step(failure_reviewer, {
                "case_name": case_name, "case_file": getattr(collector, "_case_file", ""),
                "phase": phase, "step": step, "step_index": step_index,
                "action_result": action_result, "collector": collector,
                "engine": _active_engine, "resolver": _active_resolver,
                "app_name": _active_app, "run_step": _review_run_step,
            })
        elif not action_result.passed and self_heal_callback is not None:
            heal_outcome = await _attempt_self_heal(
                self_heal_callback,
                case_name=case_name,
                case_file=getattr(collector, "_case_file", ""),
                phase=phase,
                step=step,
                step_index=step_index,
                action_result=action_result,
                last_action=last_action,
                action_records=action_records,
                profile_step=_profile_step,
                execution_profile=execution_profile,
                result_dir=result_dir,
                app_name=_active_app,
                engine=_active_engine,
                resolver=_active_resolver,
            )
            if not repair_mode and heal_outcome.get("retry") is True:
                retry_profile_step = heal_outcome.get("profile_step") or _profile_step
                retry_result, retry_last_action, retry_records = await _run_step_body(
                    step, _active_engine, _active_resolver,
                    screenshot_dir=_screenshot_dir,
                    step_index=step_index,
                    enable_screenshot=_enable_ss,
                    app_name=_active_app,
                    profile_step=retry_profile_step,
                    repair_mode=repair_mode,
                )
                _mark_self_heal_records(action_records, retry_records, heal_outcome)
                action_records.extend(retry_records)
                last_action = retry_last_action or last_action
                action_result = retry_result
                if retry_result.passed:
                    action_result.message = heal_outcome.get("message") or f"self-healed: {retry_result.message}"

        # ── 页面检测：step 执行后检测新弹出 CDP 渲染页 ───────────────────

        if _has_page_mgmt and _before_pages:
            try:
                from uita.config import UITA_PAGE_DETECT_WAIT_MS as _wait_ms
            except Exception:
                _wait_ms = 300
            if _wait_ms > 0:
                try:
                    _trace_t0 = _hang_trace_start(
                        "detect_new_page", case=case_name, step=step_index + 1,
                        engine=type(_active_engine).__name__,
                    )
                    _switched = await _active_engine.detect_and_switch_new_page(
                        _before_pages, wait_ms=_wait_ms)
                    _hang_trace_end(
                        "detect_new_page", _trace_t0, case=case_name,
                        step=step_index + 1, switched=_switched,
                    )
                    if _switched:
                        logger.info("[runner] 检测到新页面，已切换: %s",
                                    getattr(getattr(_active_engine, "_page", None), "url", "?")[:60])
                except Exception as _page_exc:
                    logger.debug("[runner] detect_and_switch_new_page 异常（忽略）: %s", _page_exc)
        # ── 页面存活检测：确保 engine._page 可用（弹窗关闭后自动回退）────
        if hasattr(_active_engine, "ensure_page_alive"):
            try:
                _trace_t0 = _hang_trace_start(
                    "ensure_page_alive", case=case_name, step=step_index + 1,
                    engine=type(_active_engine).__name__,
                )
                await _active_engine.ensure_page_alive()
                _hang_trace_end(
                    "ensure_page_alive", _trace_t0, case=case_name,
                    step=step_index + 1,
                )
            except Exception as _alive_exc:
                logger.debug("[runner] ensure_page_alive 异常（忽略）: %s", _alive_exc)

        duration_ms = (time.monotonic() - t0) * 1000
        status = action_result.status.value

        # ── 网络诊断：step 失败时提取 Playwright 引擎捕获的失败网络请求，
        # 追加进错误原因 + 打包上传 报告平台（不支持该能力的引擎优雅降级为
        # no-op）。见 docs/plans/archive/04-report_portal-report-实施纪要.md
        if status == "failed":
            try:
                from uita.core.hooks.builtin.network_diagnostics import (
                    build_network_diagnostics,
                    write_network_diagnostics_artifact,
                )
                diag_summary, diag_net_records, diag_frontend_records = build_network_diagnostics(
                    _active_engine, t0_wall,
                    app_name=_active_app,
                    platform=effective_platform_for_case(case.os),
                )
                if diag_summary:
                    action_result.message = f"{action_result.message}\n\n[网络诊断]\n{diag_summary}"
                if diag_net_records or diag_frontend_records:
                    artifact_rel = write_network_diagnostics_artifact(
                        diag_net_records, collector.result_dir, step_index,
                        frontend_records=diag_frontend_records,
                    )
                    if artifact_rel:
                        collector.add_artifact(f"network_log_step{step_index}", artifact_rel)
            except Exception as diag_exc:
                logger.debug("[runner] 网络诊断注入异常（忽略，不影响用例主流程）: %s", diag_exc)

        if phase == "teardown" and status == "failed":
            logger.warning(
                "[streaming_runner] teardown step '%s' failed (treated as warning): %s",
                step.name, action_result.message,
            )
            status = "skipped"

        # 最后一个有截图的 action 路径作为 step 级 screenshot（向后兼容）
        last_screenshot = next(
            (r.screenshot for r in reversed(action_records) if r.screenshot), None
        )
        sr = StepResult(
            name=step.name,
            status=status,
            duration_ms=duration_ms,
            message=action_result.message,
            screenshot=last_screenshot,
            actions=action_records,
        )

        # step_end hook 不再负责截图（截图已在 action 级完成），仅触发其他 hook
        hook_ctx.step_result = sr
        hook_ctx.last_action = last_action
        _trace_t0 = _hang_trace_start(
            "step_hook", case=case_name, step=step_index + 1, hook="step_end",
        )
        await _emit("step_end")
        _hang_trace_end(
            "step_hook", _trace_t0, case=case_name, step=step_index + 1,
            hook="step_end",
        )

        collector.record_step(step.name, status, duration_ms, action_result.message,
                               last_screenshot, action_records)

        if on_step:
            try:
                _trace_t0 = _hang_trace_start(
                    "on_step_callback", case=case_name, step=step_index + 1,
                )
                await on_step(step_index, sr, case_name)
                _hang_trace_end(
                    "on_step_callback", _trace_t0, case=case_name,
                    step=step_index + 1,
                )
            except CaseRetryRequested:
                logger.warning(
                    "[%s] step %d 请求重跑当前用例", case_name, step_index + 1,
                )
                raise
            except Exception as cb_exc:
                logger.warning("[%s] on_step 回调异常 (step %d): %s", case_name, step_index, cb_exc)

        if _active_watchdog is not None:
            _active_watchdog.cancel()
            _active_watchdog = None
        step_index += 1
        return sr

    # ── @setup ──────────────────────────────────────────────────
    retry_requested = False
    try:
        for step in case.setup_steps:
            sr = await _exec_step(step, "setup")
            if sr.status == "failed":
                setup_ok = False
                logger.error("[%s] @setup '%s' 失败，中止 main steps", case_name, step.name)
                break

        # ── main steps ──────────────────────────────────────────
        if setup_ok:
            for step in case.steps:
                sr = await _exec_step(step, "main")
                if sr.status == "failed":
                    if case.fail_fast:
                        logger.warning(
                            "[%s] step '%s' 失败，fail_fast=true 中止后续 main steps",
                            case_name, step.name,
                        )
                        break
                    if repair_mode:
                        logger.warning(
                            "[%s] step '%s' 失败；repair mode 不兜底，继续后续 step",
                            case_name, step.name,
                        )
                    else:
                        logger.warning("[%s] step '%s' 失败，继续执行", case_name, step.name)

    except CaseRetryRequested:
        retry_requested = True
        raise
    finally:
        if _active_watchdog is not None:
            _active_watchdog.cancel()
            _active_watchdog = None
        hook_ctx.case_result = collector.finalize()
        _trace_t0 = _hang_trace_start("case_hook", case=case_name, hook="case_end")
        await _emit("case_end")
        _hang_trace_end("case_hook", _trace_t0, case=case_name, hook="case_end")

        if not retry_requested:
            # ── @teardown（始终执行）────────────────────────────────
            # 2026-07-29：teardown 失败仅记 warning，不让 case 整体 fail。
            # teardown 是清理步骤，主体（main+setup）已通过则视为 passed。
            # 用 "skipped" 标记（不是 "failed"），与 collector.failed 计数兼容。
            for step in case.teardown_steps:
                await _exec_step(step, "teardown")

    case_result = collector.finalize()

    # ── 自愈缓冲 commit/discard（用例成功 → 批量写 DB，失败 → 丢弃）──
    # 注：heal_buf 注入在初始 resolver 上（见函数开头 resolver.set_heal_buffer），
    # 该引用不随 profile step 级切换而改变，commit/discard 仍对初始 resolver 操作，
    # 与切换机制引入前完全一致（M11 切换出的临时 resolver 不参与自愈缓冲写回）。
    if heal_buf is not None:
        try:
            if case_result.is_passed and not repair_mode and failure_reviewer is None:
                heal_buf.commit(
                    candidate_store=getattr(resolver, "_router_proxy_candidate_store", None),
                    rosetta_map_view=getattr(resolver, "_rosetta_map_view", None),
                    fallback_view=getattr(resolver, "_fallback_view", None),
                    router_proxy_view=getattr(resolver, "_router_proxy_view", None),
                )
            else:
                heal_buf.discard()
        except Exception as exc:
            logger.debug("[HealBuffer] commit/discard 异常（静默）: %s", exc)
        finally:
            # 清空缓冲引用，避免下次用例残留
            try:
                resolver.set_heal_buffer(None)
            except Exception:
                pass

    return case_result


async def _attempt_self_heal(
    callback: SelfHealCallback,
    **payload: Any,
) -> dict[str, Any]:
    try:
        outcome = await callback(_self_heal_payload(**payload))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[%s] self-heal 回调异常 (step %s): %s",
            payload.get("case_name", ""),
            payload.get("step_index", ""),
            exc,
        )
        return {"retry": False, "status": "callback_error", "message": str(exc)}
    return outcome if isinstance(outcome, dict) else {"retry": False, "status": "no_outcome"}


def _self_heal_payload(**payload: Any) -> dict[str, Any]:
    records = [_action_record_to_dict(record) for record in payload.get("action_records") or []]
    failed_action = next((record for record in records if record.get("status") == "failed"), {})
    action_result = payload.get("action_result")
    step = payload.get("step")
    return {
        "case_name": payload.get("case_name", ""),
        "case_file": payload.get("case_file", ""),
        "phase": payload.get("phase", ""),
        "step_index": payload.get("step_index", 0),
        "step_name": getattr(step, "name", ""),
        "step": step,
        "result_dir": payload.get("result_dir", ""),
        "app_name": payload.get("app_name", ""),
        "status": getattr(getattr(action_result, "status", None), "value", "failed"),
        "message": getattr(action_result, "message", ""),
        "profile_step": payload.get("profile_step") or {},
        "execution_profile": payload.get("execution_profile") or {},
        "failed_action": failed_action,
        "action_records": records,
        "last_action": payload.get("last_action"),
        "engine": payload.get("engine"),
        "resolver": payload.get("resolver"),
    }


def _action_record_to_dict(record: Any) -> dict[str, Any]:
    from dataclasses import asdict

    if hasattr(record, "__dataclass_fields__"):
        data = asdict(record)
    elif isinstance(record, dict):
        data = dict(record)
    else:
        return {"raw": str(record)}
    return _json_safe_extra(data)


def _mark_self_heal_records(
    failed_records: list[Any],
    retry_records: list[Any],
    outcome: dict[str, Any],
) -> None:
    summary = {
        "status": outcome.get("status", "repaired"),
        "strategy": outcome.get("strategy", ""),
        "message": outcome.get("message", ""),
    }
    for record in failed_records:
        if getattr(record, "status", "") == "failed":
            record.extra = {**(record.extra or {}), "self_heal_initial_failure": summary}
    for record in retry_records:
        record.extra = {**(record.extra or {}), "self_heal_retry": summary}


async def _run_step_body(
    step: StepNode,

    engine: EngineBase,
    resolver: Resolver,
    screenshot_dir: str = "",
    step_index: int = 0,
    enable_screenshot: bool = False,
    app_name: str = "",
    profile_step: Optional[dict] = None,
    repair_mode: bool = False,
) -> tuple:

    """执行单个 step 的所有 body 节点，首个失败即停止。

    Args:
        screenshot_dir:    截图目录（绝对路径），空字符串则不截图
        step_index:        步骤编号，用于截图命名
        enable_screenshot: 是否开启 action 级截图
        app_name:          action 按 app 维度查找用（透传给 get_action）

    Returns:
        (ActionResult, LastAction | None, list[ActionRecord])
    """
    from uita.core.result.collector import ActionRecord

    last = ActionResult.ok(f"step '{step.name}' ok")
    last_action: Optional[LastAction] = None
    action_records: list = []
    action_idx = 0

    # L4/L4b LLM prompt 富化：追踪当前 step 名称（见
    # docs/plans/archive/03-resolver-rosetta-实施纪要.md）
    # 2026-08-25 新增 None 防护：switch[api] 模式下 resolver 恒为 None
    # （见计划文档 20260825090000 §2.4），与 core/interpreter.py 同构处理。
    if resolver is not None:
        resolver.set_step_context(step.name)

    case_ctx = getattr(engine, "_case_context", None)

    async def _eval_if_condition(cond):
        _trace_t0 = _hang_trace_start(
            "condition_evaluate", step=step_index + 1, name=step.name,
            app=app_name, engine=type(engine).__name__,
        )
        result = await evaluate_condition(cond, engine, resolver, case_ctx)
        _hang_trace_end(
            "condition_evaluate", _trace_t0, step=step_index + 1,
            name=step.name, result=result,
        )
        return result

    # IfNode 惰性展开（2026-07-25 新增，见 core/runner/condition_eval.py 顶部说明，
    # 与 core/interpreter.py::_run_step() 共用同一套展开/求值逻辑，避免双 runner
    # 分叉，见 docs/plans/archive/02-dsl-actions-实施纪要.md §3.7 风险 2）。
    async for node in iter_effective_body(step.body, _eval_if_condition):
        target = ""
        _before_screenshot_path: Optional[str] = None
        _page_changed: Optional[bool] = None
        if isinstance(node, ActionNode):
            target = node.args[0] if node.args else ""
            logger.log(
                NOTICE,
                "[hang-trace] ACTION_START step=%d name=%s action_index=%d action=%s app=%s engine=%s",
                step_index + 1, step.name, action_idx + 1, node.action,
                app_name, type(engine).__name__,
            )
            # 执行前 resolve：供 before 截图标注与 last_action.element 使用
            # _NO_RESOLVE_ACTIONS：args[0] 不是元素名，或 action 会携带参数自行解析；
            # 跳过通用预解析，避免丢失 image_click.confidence 等动作级参数。
            # get/post/put/delete/api_assert（2026-08-25 新增）：API 模式下
            # resolver 恒为 None（见上方 None 防护 + 计划文档 20260825090000
            # §2.4），且 args[0]/kwargs 是 uri/断言参数而非元素名。
            _NO_RESOLVE_ACTIONS: frozenset[str] = frozenset({
                "press_key", "soft_reset", "run_exe", "choose_file", "coord_key", "type_focused",
                "type_focused_slow", "image_click",
                "activate_windows_window", "focus_uia_window", "optional_group", "powershell",
                "kill_process", "eval_js", "wait_js", "collect_split_table",
                "goto_page", "wait_load_state", "reload_page",
                "eval_js",
                "get", "post", "put", "delete", "api_assert",
            })
            _SCROLL_DIRECTIONS = frozenset({"up", "down", "left", "right"})
            _is_scroll_no_element = (
                node.action == "scroll" and target.lower() in _SCROLL_DIRECTIONS
            )
            action_element = None
            _action_resolve_result = None
            _action_label: Optional[str] = None
            if target and enable_screenshot:
                if node.action in _NO_RESOLVE_ACTIONS or _is_scroll_no_element:
                    _action_label = f"{node.action}[{target}]"
                else:
                    try:
                        _trace_t0 = _hang_trace_start(
                            "pre_resolve", step=step_index + 1, name=step.name,
                            action_index=action_idx + 1, action=node.action,
                        )
                        _action_resolve_result = await resolver.resolve(target, skip_l4=True)
                        _hang_trace_end(
                            "pre_resolve", _trace_t0, step=step_index + 1,
                            action_index=action_idx + 1, action=node.action,
                            found=_action_resolve_result.found,
                        )
                        if _action_resolve_result.found:
                            action_element = _action_resolve_result.element
                    except Exception:
                        pass
            # BEFORE 截图：元素存在时画框，不存在时画 [NOT FOUND] 标签
            # 导航/等待类 action 跳过 before 截图（导航前页面状态价值低，
            # 且 L1 截图等字体加载可能额外卡 10s）
            _NO_BEFORE_SCREENSHOT_ACTIONS: frozenset[str] = frozenset({
                "goto_page", "wait_load_state", "reload_page",
                # 进程生命周期动作可能让当前原生窗口立即失效；继续截图会把
                # WinUIA 的单线程执行器阻塞在已销毁窗口上。
                "powershell", "kill_process",
            })
            if enable_screenshot and screenshot_dir and target and node.action not in _NO_BEFORE_SCREENSHOT_ACTIONS:
                from uita.core.hooks.builtin.action_screenshotter import take_action_screenshot_before
                _trace_t0 = _hang_trace_start(
                    "before_screenshot", step=step_index + 1, name=step.name,
                    action_index=action_idx + 1, action=node.action,
                    engine=type(engine).__name__,
                )
                _before_screenshot_path = await take_action_screenshot_before(
                    engine=engine,
                    keyword=node.action,
                    target_name=target,
                    resolve_result=_action_resolve_result,
                    screenshot_dir=screenshot_dir,
                    step_index=step_index,
                    action_index=action_idx,
                    action_label=_action_label,
                )
                _hang_trace_end(
                    "before_screenshot", _trace_t0, step=step_index + 1,
                    action_index=action_idx + 1, action=node.action,
                    output=bool(_before_screenshot_path),
                )
            # 操作前后页面 hash 对比验证（默认关闭，见
            # docs/plans/archive/02-dsl-actions-实施纪要.md）
            _do_hash_check = ENABLE_PAGE_HASH_VERIFY and node.action in HASH_VERIFY_ACTIONS
            _before_hash = await capture_page_hash(engine) if _do_hash_check else None

            _trace_t0 = _hang_trace_start(
                "action_execute", step=step_index + 1, name=step.name,
                action_index=action_idx + 1, action=node.action,
                app=app_name, engine=type(engine).__name__,
            )
            result = await _dispatch_action(
                node,
                engine,
                resolver,
                app=app_name,
                profile_step=profile_step or {},
                action_index=action_idx,
                screenshot_dir=screenshot_dir,
                step_index=step_index,
                repair_mode=repair_mode,
            )
            _hang_trace_end(
                "action_execute", _trace_t0, step=step_index + 1,
                action_index=action_idx + 1, action=node.action,
                status=result.status.value,
            )
            last_action = LastAction(keyword=node.action, element=action_element)

            if _do_hash_check and result.passed:
                _after_hash = await capture_page_hash(engine)
                if _before_hash and _after_hash:
                    _page_changed = _before_hash != _after_hash
                    if not _page_changed:
                        logger.warning(
                            "[page-hash] %s[%s] 执行后页面 hash 未变化，操作可能未生效",
                            node.action, target,
                        )

            # 与 Interpreter 保持一致：切换动作成功后立即跟随栈顶 app。
            # 否则同一 step 内后续动作仍使用旧 engine/resolver/app。
            if (
                isinstance(node, ActionNode)
                and result.passed
                and node.action in {"switch", "switch_back", "run_exe"}
            ):
                try:
                    from uita.core.lifecycle.api_engine_slot import get_default_api_slot
                    from uita.core.lifecycle.app_lifecycle_stack import get_default_stack
                    from uita.core.runner.resolver_factory import build_resolver_for_app

                    api_slot = get_default_api_slot()
                    if api_slot.is_active:
                        # switch[api] 生效中：engine 指向 API 引擎，resolver 置空
                        # （见 core/interpreter.py::_sync_engine_after_switch 同构
                        # 逻辑，计划文档 20260825090000 §2.4）。
                        engine = api_slot.current
                        resolver = None
                    else:
                        manager = get_default_stack().current
                        if manager is not None:
                            _trace_t0 = _hang_trace_start(
                                "in_step_switch_resolver", step=step_index + 1,
                                name=step.name, action_index=action_idx + 1,
                                app=manager.app_name, engine=type(manager.engine).__name__,
                            )
                            previous_resolver = resolver
                            engine = manager.engine
                            app_name = manager.app_name
                            resolver = build_resolver_for_app(
                                engine, app_name, manager.platform,
                            )
                            if repair_mode:
                                resolver.set_repair_mode(True)
                            resolver.set_step_context(step.name)
                            heal_buffer = getattr(previous_resolver, "_heal_buffer", None)
                            if heal_buffer is not None:
                                try:
                                    resolver.set_heal_buffer(heal_buffer)
                                except Exception:
                                    pass
                            if case_ctx is not None:
                                try:
                                    setattr(engine, "_case_context", case_ctx)
                                except Exception:
                                    pass
                            _hang_trace_end(
                                "in_step_switch_resolver", _trace_t0,
                                step=step_index + 1, action_index=action_idx + 1,
                                app=app_name, engine=type(engine).__name__,
                            )
                except Exception as switch_exc:
                    logger.warning("step 内 switch 后 engine/resolver/app 同步异常: %s", switch_exc)
        elif isinstance(node, WaitNode):
            logger.log(
                NOTICE,
                "[hang-trace] ACTION_START step=%d name=%s action_index=%d action=wait app=%s engine=%s",
                step_index + 1, step.name, action_idx + 1, app_name,
                type(engine).__name__,
            )
            # 2026-08-25 新增：API 模式下（resolver 为 None）wait[<element>]
            # 带元素名调用无法解析，清晰失败而非崩溃（与 core/interpreter.py
            # 同构，见计划文档 20260825090000 §2.4）。裸时长 wait[Ns] 无
            # target，不依赖 resolver，不受影响。
            if resolver is None and node.target:
                result = ActionResult.fail(
                    f"wait[{node.target}]: 当前处于 API 模式（switch[api] "
                    f"生效中），不支持按元素名等待，请先 switch_back[] 切回 UI 引擎"
                )
                element = None
                last_action = LastAction(keyword="wait", element=None)
            else:
                _trace_t0 = _hang_trace_start(
                    "wait_execute", step=step_index + 1, name=step.name,
                    action_index=action_idx + 1, has_target=bool(node.target),
                )
                result, element = await _dispatch_wait_with_element(
                    node, engine, resolver, repair_mode=repair_mode,
                )
                _hang_trace_end(
                    "wait_execute", _trace_t0, step=step_index + 1,
                    action_index=action_idx + 1, status=result.status.value,
                )
                target = node.target or ""
                last_action = LastAction(keyword="wait", element=element)
                # wait 成功时，尝试从元素名推断当前页面上下文
                if result.passed and target:
                    resolver.update_page_context_from_name(target)
        elif isinstance(node, AssertNode):
            logger.log(
                NOTICE,
                "[hang-trace] ACTION_START step=%d name=%s action_index=%d action=%s app=%s engine=%s",
                step_index + 1, step.name, action_idx + 1,
                node.keyword or "assert", app_name, type(engine).__name__,
            )
            _trace_t0 = _hang_trace_start(
                "assert_execute", step=step_index + 1, name=step.name,
                action_index=action_idx + 1, action=node.keyword or "assert",
            )
            result, element = await _dispatch_assert_with_element(
                node, engine, resolver, repair_mode=repair_mode,
            )
            _hang_trace_end(
                "assert_execute", _trace_t0, step=step_index + 1,
                action_index=action_idx + 1, action=node.keyword or "assert",
                status=result.status.value,
            )
            target = node.target
            last_action = LastAction(keyword=node.keyword or "assert", element=element)
        elif isinstance(node, SetNode):
            result = await _dispatch_set(
                node, engine, resolver, app=app_name,
                profile_step=profile_step or {}, action_index=action_idx,
                screenshot_dir=screenshot_dir, step_index=step_index,
                repair_mode=repair_mode,
            )
            target = node.var_name
            last_action = LastAction(keyword="set", element=None)
        else:
            continue

        last = result
        failed = not result.passed

        # ── action 级截图 ──────────────────────────────────────
        # 操作类（click/type/scroll/press_key/set 等）→ before + after 两张；
        # hover → 跳过 done 截图（page.screenshot() 在 Electron 中可能触发
        #         窗口级事件导致 hover 状态丢失，破坏后续 Popover 断言）
        # assert/verify/wait[element] → 单张 after（历史行为，无 suffix）
        # wait[Ns] 纯时长 → 不截图
        _is_bare_wait = isinstance(node, WaitNode) and node.target is None
        _is_hover = isinstance(node, ActionNode) and node.action == "hover"
        _is_screenshot_unsafe = (
            isinstance(node, ActionNode)
            and node.action in {"powershell", "kill_process"}
        )
        screenshot_path: Optional[str] = None
        if (
            enable_screenshot
            and screenshot_dir
            and last_action is not None
            and not _is_bare_wait
            and not _is_hover
            and not _is_screenshot_unsafe
        ):
            from uita.core.hooks.builtin.action_screenshotter import take_action_screenshot
            _ss_suffix = "done" if isinstance(node, ActionNode) else ""
            _trace_t0 = _hang_trace_start(
                "after_screenshot", step=step_index + 1, name=step.name,
                action_index=action_idx + 1,
                action=last_action.keyword if last_action else "unknown",
                engine=type(engine).__name__,
            )
            screenshot_path = await take_action_screenshot(
                engine=engine,
                last_action=last_action,
                screenshot_dir=screenshot_dir,
                step_index=step_index,
                action_index=action_idx,
                failed=failed,
                suffix=_ss_suffix,
            )
            _hang_trace_end(
                "after_screenshot", _trace_t0, step=step_index + 1,
                action_index=action_idx + 1,
                action=last_action.keyword if last_action else "unknown",
                output=bool(screenshot_path),
            )

        action_records.append(ActionRecord(
            action=last_action.keyword if last_action else "unknown",
            target=target,
            status="failed" if failed else "passed",
            screenshot=screenshot_path,
            screenshot_before=_before_screenshot_path,
            existence_strategy=(result.extra or {}).get("existence_strategy"),
            page_changed=_page_changed,
            extra=_json_safe_extra(result.extra or {}),
        ))

        action_idx += 1
        logger.log(
            NOTICE,
            "[hang-trace] ACTION_END step=%d name=%s action_index=%d action=%s status=%s",
            step_index + 1, step.name, action_idx,
            last_action.keyword if last_action else "unknown",
            result.status.value,
        )
        if (result.extra or {}).get("optional_skipped"):
            break
        if failed:
            break

    return last, last_action, action_records


def _json_safe_extra(extra: dict[str, Any]) -> dict[str, Any]:
    import json

    def normalize(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, list):
            return [normalize(item) for item in value]
        if isinstance(value, tuple):
            return [normalize(item) for item in value]
        if isinstance(value, dict):
            return {str(k): normalize(v) for k, v in value.items()}
        return str(value)

    normalized = normalize(extra)
    try:
        json.dumps(normalized, ensure_ascii=False)
    except TypeError:
        return {"raw": str(extra)}
    return normalized if isinstance(normalized, dict) else {}


async def _dispatch_action(
    node: ActionNode,
    engine: EngineBase,
    resolver: Resolver,
    app: str = "",
    profile_step: Optional[dict] = None,
    action_index: int = 0,
    screenshot_dir: str = "",
    step_index: int = 0,
    repair_mode: bool = False,
) -> ActionResult:
    # 2026-08-25 新增：引擎类型守卫（switch[api] 模式），与
    # core/interpreter.py 保持一致，见计划文档
    # docs/plans/done/archive_202608.md §2.4。
    if isinstance(engine, ApiEngineBase):
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
        action_cls = get_action(node.action, app=app)
    except ActionNotFoundError as exc:
        return ActionResult.fail(str(exc))
    extra = {
        "profile_step": profile_step or {},
        "profile_action": (profile_step or {}).get("action") or {},
        "profile_assertion": (profile_step or {}).get("assertion") or {},
        "profile_patterns": (profile_step or {}).get("patterns") or [],
        "profile_action_index": action_index,
        "screenshot_dir": screenshot_dir,
        "step_index": step_index,
        "repair_mode": repair_mode,
    }
    kwargs = dict(node.kwargs)
    if repair_mode:
        kwargs["retry"] = "0"
    optional = str(kwargs.pop("optional", "false")).strip().lower() in {"1", "true", "yes", "on"}
    kwargs.pop("cleanup", None)  # source marker; batch already continues later steps
    ctx = ActionContext(engine=engine, resolver=resolver, args=node.args, kwargs=kwargs, extra=extra)
    try:
        result = await action_cls().execute(ctx)
    except Exception as exc:
        if not optional:
            raise
        result = ActionResult.ok(f"optional action skipped: {type(exc).__name__}: {exc}")
        result.extra = {"optional_skipped": True, "reason": str(exc)}
        return result
    if optional and not result.passed:
        skipped = ActionResult.ok(f"optional action skipped: {result.message}", duration_ms=result.duration_ms)
        skipped.extra = {**(result.extra or {}), "optional_skipped": True, "reason": result.message}
        return skipped
    return result



async def _dispatch_set(
    node: SetNode, engine: EngineBase, resolver: Resolver, **action_options: Any,
) -> ActionResult:
    t0 = time.monotonic()
    if not node.source_action:
        return ActionResult.fail(f"set: 语句缺少 source_action，var={node.var_name}")
    case_ctx = getattr(engine, "_case_context", None)
    if case_ctx is None:
        return ActionResult.fail(
            f"set: {node.var_name} — 引擎未绑定 CaseContext，无法赋值"
        )
    if node.source_action == "raw":
        value = node.source_args[0] if node.source_args else ""
    else:
        action_node = ActionNode(
            action=node.source_action, args=node.source_args,
            kwargs=node.source_kwargs, raw=node.raw,
        )
        result = await _dispatch_action(action_node, engine, resolver, **action_options)
        if not result.passed:
            return ActionResult.fail(
                f"set: {node.var_name} — source_action '{node.source_action}' 失败: {result.message}",
                duration_ms=(time.monotonic() - t0) * 1000,
            )
        value = result.message
    case_ctx.set(node.var_name, value)
    return ActionResult.ok(
        f"set {node.var_name} = '{value}'",
        duration_ms=(time.monotonic() - t0) * 1000,
    )


async def _dispatch_wait(node: WaitNode, engine: EngineBase, resolver: Resolver) -> ActionResult:
    from uita.core.actions.common.action_wait import WaitAction
    ctx = ActionContext(
        engine=engine, resolver=resolver,
        args=[node.target] if node.target else [],
        extra={"wait_node": node},
    )
    return await WaitAction().execute(ctx)


async def _dispatch_assert(node: AssertNode, engine: EngineBase, resolver: Resolver) -> ActionResult:
    from uita.core.actions.common.action_assert import AssertAction
    ctx = ActionContext(
        engine=engine, resolver=resolver,
        args=[node.target],
        kwargs=node.kwargs,
        extra={"assert_node": node},
    )
    return await AssertAction().execute(ctx)


async def _dispatch_wait_with_element(
    node: WaitNode, engine: EngineBase, resolver: Resolver, *, repair_mode: bool = False,
):
    """执行 wait，同时返回 (ActionResult, element_or_None)。

    wait 执行完后元素一定存在（或超时失败），执行后再 resolve 获取实际元素。
    """
    from uita.core.actions.common.action_wait import WaitAction
    ctx = ActionContext(
        engine=engine, resolver=resolver,
        args=[node.target] if node.target else [],
        extra={"wait_node": node, "repair_mode": repair_mode},
    )
    result = await WaitAction().execute(ctx)
    # 优先复用 WaitAction 回传的 resolve 结果（ctx.extra["_wait_resolve_result"]），
    # 避免为截图标注再做一次全链路 resolve。无该 key（失败/超时/裸时长 wait）时
    # 不 resolve——元素不存在，截图标注也不需要坐标。
    # 见 docs/plans/archive/02-dsl-actions-实施纪要.md。
    resolved_elem = None
    if node.target and result.passed:
        cached = ctx.extra.pop("_wait_resolve_result", None)
        if cached is not None and cached.found:
            resolved_elem = cached.element
    return result, resolved_elem


async def _dispatch_assert_with_element(
    node: AssertNode, engine: EngineBase, resolver: Resolver, *, repair_mode: bool = False,
):
    """执行 assert，同时返回 (ActionResult, element_or_None)。"""
    from uita.core.actions.common.action_assert import AssertAction, split_or_targets
    from uita.core.actions.judgers import JudgerDispatchError, needs_element
    kwargs = dict(node.kwargs)
    optional = str(kwargs.pop("optional", "false")).strip().lower() in {"1", "true", "yes", "on"}
    cleanup = str(kwargs.pop("cleanup", "false")).strip().lower() in {"1", "true", "yes", "on"}
    # 纯 JS 断言元素（rosetta_map 只有 js 字段无 selector）跳过 resolve，
    # 因为没有视觉位置可标注，避免无意义的 LLM 链路调用。OR 逻辑
    # （assert[A|B]，2026-08-06 新增）：逐候选尝试，第一个 resolve 命中即
    # 停止用于截图高亮；无 `|` 时候选只有一个，行为与改造前一致（零回归）。
    element = None
    prewarmed_resolve: dict = {}
    for target in split_or_targets(node.target):
        try:
            require_element = needs_element(kwargs, target)
        except JudgerDispatchError as exc:
            return ActionResult.fail(f"assert: {exc}"), None
        if resolver is None and target and require_element:
            return ActionResult.fail(
                f"assert[{node.target}]: 当前处于 API 模式（switch[api] "
                f"生效中），不支持按元素名断言，请使用 api_assert[] 或先 "
                f"switch_back[] 切回 UI 引擎"
            ), None
        if not require_element or resolver is None or resolver.get_js_expr(target):
            continue
        r = await resolver.resolve(target)
        prewarmed_resolve[target] = r
        if r.found:
            element = r.element
            break
    ctx = ActionContext(
        engine=engine, resolver=resolver,
        args=[node.target],
        kwargs=kwargs,
        extra={
            "assert_node": None if optional or cleanup else node,
            "prewarmed_resolve": prewarmed_resolve,
            "repair_mode": repair_mode,
        },
    )
    result = await AssertAction().execute(ctx)
    if optional and not result.passed:
        skipped = ActionResult.ok(f"optional assertion skipped: {result.message}", duration_ms=result.duration_ms)
        skipped.extra = {**(result.extra or {}), "optional_skipped": True, "reason": result.message}
        return skipped, element
    return result, element
