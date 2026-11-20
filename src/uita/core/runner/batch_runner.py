"""
BatchRunner — 顺序批量执行目录下的 TCS 用例。

功能：
  - 扫描目录（递归）收集 .tcs 文件
  - 路径解析：优先 CWD → 兜底 ROOT_DIR
  - tag / priority 过滤
  - 每条用例调用 prepare_engine_for_case（三段式生命周期）
  - 执行异常时硬重启引擎并仅重试同一用例一次
  - 通过 StreamingRunner 执行 + on_step 实时打印
  - 结果写入 report/<run_ts>/<case>.tcs.result/result.json
  - 生成 report/<run_ts>/summary.json
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

from uita.core.dsl.parser import infer_app_from_path, parse_file
from uita.core.engine.base import EngineBase
from uita.core.hooks.base import HookManager
from uita.core.hooks.registry import build_hooks
from uita.core.lifecycle.engine_lifecycle import prepare_engine_for_case
from uita.core.registry_common import effective_platform_for_case, platform_matches_host
from uita.core.resolver.resolver import Resolver
from uita.core.result.collector import ResultCollector, make_run_result_dir
from uita.core.runner.resolver_factory import build_resolver_for_app
from uita.core.runner.streaming_runner import (
    CaseRetryRequested, OnStepCallback, SelfHealCallback, run_case_streaming,
)
from uita.config import ENABLE_FAILED_REQUEUE, MAX_RETRY_PER_CASE, NOTICE


logger = logging.getLogger(__name__)

# 字母前缀编排：形如 `A01_`/`b02_`（单字母 + 严格2位数字 + 下划线）的用例文件名。
_PREFIX_ORDER_RE = re.compile(r"^([A-Za-z])(\d{2})_")
_SERVER_ERROR_MAX_RETRIES = 3


def _write_fallback_result_json(
    case_name: str, case_file: str, result_dir: str, result_path: str, *, reason: str,
) -> None:
    """用例级异常（parse_file 失败/硬重启失败/重试后仍抛异常）兜底落盘 result.json。"""
    if os.path.isfile(result_path):
        return
    try:
        collector = ResultCollector(
            case_name=case_name, case_file=case_file, result_dir=result_dir,
        )
        collector.record_step("batch_failure", "failed", message=reason)
        collector.write_json()
    except Exception as e:
        logger.warning("[batch] %s 兜底落盘 result.json 异常: %s", case_name, e)


def _reorder_lettered_prefix(fnames: list[str]) -> list[str]:
    """同目录内 `A01_`/`b02_` 前缀用例统一按 (字母不分大小写, 数字) 升序编排。

    合并规则：排序后键值最小的前缀用例（锚点）留在原始位置不动；其余前缀
    用例从原位置摘除，按排序结果整体插入到锚点位置之后；未匹配前缀的用例
    维持原有相对顺序，仅因摘除动作自动前移填补空位。匹配到的前缀用例 ≤ 1
    个时原样返回（等价于正常编排，零改动）。仅在传入的这一批 fnames（同一
    目录）内生效，不跨目录合并排序。
    """
    indexed = list(enumerate(fnames))
    prefixed = [(i, f) for i, f in indexed if _PREFIX_ORDER_RE.match(f)]
    if len(prefixed) <= 1:
        return fnames

    def _key(item: tuple[int, str]) -> tuple[str, int]:
        m = _PREFIX_ORDER_RE.match(item[1])
        return (m.group(1).upper(), int(m.group(2)))

    sorted_prefixed = sorted(prefixed, key=_key)
    anchor_idx = sorted_prefixed[0][0]
    movers = [f for _, f in sorted_prefixed[1:]]
    mover_names = {f for _, f in sorted_prefixed[1:]}

    result: list[str] = []
    for i, f in indexed:
        if f in mover_names:
            continue
        result.append(f)
        if i == anchor_idx:
            result.extend(movers)
    return result


@dataclass
class BatchSummary:
    total: int = 0
    passed: int = 0
    failed: int = 0
    duration_ms: int = 0
    cases: list[dict] = field(default_factory=list)


class BatchRunner:
    """顺序批量执行 TCS 用例。

    用法::

        runner = BatchRunner(engine, cases_dirs=["cases/demo"], result_dir="report")
        summary = await runner.run()
    """

    def __init__(
        self,
        engine: EngineBase,
        cases_dirs: list[str] | str,
        result_dir: str = "report",
        hook_names: Optional[list[str]] = None,
        tag_filter: Optional[str] = None,
        priority_filter: Optional[str] = None,
        random_mode: bool = False,
        on_step: Optional[OnStepCallback] = None,
        batch_id: str = "",
        self_heal_callback: Optional[SelfHealCallback] = None,
        html_report: str = "",
        repair_mode: bool = False,
        failure_reviewer=None,
    ) -> None:
        self._engine = engine

        self._cases_dirs = [cases_dirs] if isinstance(cases_dirs, str) else list(cases_dirs)
        self._result_dir = result_dir
        self._hook_names = hook_names or []
        self._tag_filter = tag_filter
        self._priority_filter = priority_filter
        self._random_mode = random_mode
        self._on_step = on_step
        self._batch_id = batch_id
        self._self_heal_callback = self_heal_callback
        self._failure_reviewer = failure_reviewer
        self._html_report = html_report
        self._repair_mode = repair_mode

    async def run(self) -> BatchSummary:
        tcs_files = self._collect_files()
        if not tcs_files:
            raise RuntimeError(f"未找到任何 .tcs 文件: {self._cases_dirs}")

        # tag / priority 过滤
        tcs_files = self._apply_filters(tcs_files)
        if not tcs_files:
            raise RuntimeError("过滤后无可执行用例")

        # 随机模式：每个目录取一个
        if self._random_mode:
            tcs_files = self._random_sample(tcs_files)

        logger.info("[batch] 共 %d 个用例，开始顺序执行", len(tcs_files))

        # 批次共享同一时间戳目录：result/<YYYYMMDD-HHmmss>/
        from datetime import datetime
        run_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        hooks: list = build_hooks(self._hook_names)
        hook_mgr = HookManager(hooks) if hooks else None

        # batch 运行目录（用于 batch_end hook 采集日志/上报等）
        batch_run_dir = os.path.join(self._result_dir, run_ts)
        os.makedirs(batch_run_dir, exist_ok=True)

        # 构造 batch 级 HookContext

        from uita.core.hooks.base import HookContext
        batch_ctx = HookContext(
            report_dir=batch_run_dir,
            run_dir=batch_run_dir,
            engine=self._engine,
            batch_id=self._batch_id,
            html_report=self._html_report,
        )
        if self._engine:
            batch_ctx.engines = {"default": self._engine}
        batch_ctx.extra["total"] = len(tcs_files)

        # 触发 batch_start
        if hook_mgr:
            _diag_t0 = time.monotonic()
            logger.log(NOTICE, "[hang-trace] START stage=batch_hook hook=batch_start")
            await hook_mgr.emit("batch_start", batch_ctx)
            logger.log(
                NOTICE, "[hang-trace] END stage=batch_hook hook=batch_start elapsed_ms=%d",
                int((time.monotonic() - _diag_t0) * 1000),
            )

        summary = BatchSummary(total=len(tcs_files))
        t_start = time.monotonic()

        for idx, tcs_file in enumerate(tcs_files):
            case_stem = os.path.splitext(os.path.basename(tcs_file))[0]
            # 每条用例目录：result/<run_ts>/<case_file>.result/
            case_result_dir = make_run_result_dir(tcs_file, base_dir=self._result_dir, run_ts=run_ts)

            os.makedirs(case_result_dir, exist_ok=True)
            result_path = os.path.join(case_result_dir, "result.json")

            logger.info("[batch] [%d/%d] %s", idx + 1, len(tcs_files), case_stem)
            logger.log(
                NOTICE,
                "[hang-trace] CASE_START index=%d/%d case=%s file=%s",
                idx + 1, len(tcs_files), case_stem, tcs_file,
            )
            t_case = time.monotonic()
            case = None
            result = None
            failed_retry_count = 0
            app_name = ""
            case_os = ""
            failure_summary = ""

            try:
                # 三段式引擎生命周期
                _diag_t0 = time.monotonic()
                logger.log(
                    NOTICE, "[hang-trace] START stage=prepare_engine case=%s first=%s",
                    case_stem, idx == 0,
                )
                await prepare_engine_for_case(
                    self._engine,
                    case_name=case_stem,
                    is_first=(idx == 0),
                    tag_prefix="batch",
                )
                logger.log(
                    NOTICE, "[hang-trace] END stage=prepare_engine case=%s elapsed_ms=%d",
                    case_stem, int((time.monotonic() - _diag_t0) * 1000),
                )

                _diag_t0 = time.monotonic()
                logger.log(NOTICE, "[hang-trace] START stage=parse_case case=%s", case_stem)
                case = parse_file(tcs_file)
                logger.log(
                    NOTICE, "[hang-trace] END stage=parse_case case=%s elapsed_ms=%d",
                    case_stem, int((time.monotonic() - _diag_t0) * 1000),
                )

                # 构建 resolver（app_name 与 run_case_streaming 保持一致，
                # 避免同一条用例的 resolver/action 归属不同 app）
                app_name = case.app or infer_app_from_path(tcs_file) or "sample_web"
                case_os = getattr(case, "os", "") or ""

                async def _run_attempt():
                    _attempt_t0 = time.monotonic()
                    logger.log(
                        NOTICE,
                        "[hang-trace] START stage=build_resolver case=%s app=%s engine=%s",
                        case_stem, app_name, type(self._engine).__name__,
                    )
                    resolver = self._build_resolver(case, tcs_file, app_name=app_name)
                    logger.log(
                        NOTICE,
                        "[hang-trace] END stage=build_resolver case=%s app=%s elapsed_ms=%d",
                        case_stem, app_name,
                        int((time.monotonic() - _attempt_t0) * 1000),
                    )
                    collector = ResultCollector(
                        case_name=case_stem,
                        case_file=tcs_file,
                        result_dir=case_result_dir,
                    )
                    _stream_t0 = time.monotonic()
                    logger.log(
                        NOTICE, "[hang-trace] START stage=run_case_streaming case=%s app=%s",
                        case_stem, app_name,
                    )
                    result = await run_case_streaming(
                        case,
                        self._engine,
                        resolver,
                        collector,
                        hook_manager=hook_mgr,
                        on_step=self._make_step_cb(case_stem, case, app_name=app_name),
                        result_dir=case_result_dir,
                        app_name=app_name,
                        self_heal_callback=self._self_heal_callback,
                        repair_mode=self._repair_mode,
                        **({"failure_reviewer": self._failure_reviewer}
                           if self._failure_reviewer is not None else {}),
                    )
                    logger.log(
                        NOTICE,
                        "[hang-trace] END stage=run_case_streaming case=%s elapsed_ms=%d passed=%s",
                        case_stem, int((time.monotonic() - _stream_t0) * 1000),
                        result.is_passed,
                    )
                    return result, collector

                async def _hard_restart_for_retry(tag_prefix: str, reason: str) -> None:
                    try:
                        await prepare_engine_for_case(
                            self._engine,
                            case_name=case_stem,
                            is_first=False,
                            tag_prefix=tag_prefix,
                            force_hard_restart=True,
                        )
                    except Exception as restart_exc:
                        raise RuntimeError(
                            f"{case_stem} {reason}后硬重启失败: {restart_exc}"
                        ) from restart_exc

                async def _run_with_execution_retry():
                    """执行 `_run_attempt()`；遇到执行异常时按策略硬重启重试。"""
                    if self._repair_mode:
                        return await _run_attempt()

                    server_error_retries = 0
                    execution_retry_used = False
                    while True:
                        try:
                            return await _run_attempt()
                        except CaseRetryRequested:
                            if server_error_retries >= _SERVER_ERROR_MAX_RETRIES:
                                logger.error(
                                    "[batch] %s 服务器响应错误，已重试 %d 次，停止重试",
                                    case_stem, server_error_retries,
                                )
                                raise
                            server_error_retries += 1
                            logger.warning(
                                "[batch] %s 检测到服务器响应错误；硬重启后重试 %d/%d",
                                case_stem, server_error_retries,
                                _SERVER_ERROR_MAX_RETRIES,
                            )
                            await _hard_restart_for_retry(
                                "batch-server-retry", "服务器响应错误",
                            )
                            logger.info(
                                "[batch] %s 硬重启完成，开始服务器错误重试 %d/%d",
                                case_stem, server_error_retries,
                                _SERVER_ERROR_MAX_RETRIES,
                            )
                        except Exception as execution_exc:
                            if execution_retry_used:
                                logger.error(
                                    "[batch] %s 第 2/2 次执行异常", case_stem,
                                    exc_info=True,
                                )
                                raise
                            execution_retry_used = True
                            logger.error(
                                "[batch] %s 第 1/2 次执行异常，硬重启后重试: %s",
                                case_stem, execution_exc, exc_info=True,
                            )
                            await _hard_restart_for_retry("batch-retry", "首次执行异常")
                            logger.info(
                                "[batch] %s 硬重启完成，开始第 2/2 次执行", case_stem,
                            )

                result, collector = await _run_with_execution_retry()

                # 失败重试：正常返回但 result.is_passed=False 时，
                # 若全局开关开启 + 用例未声明 retryable=false + 重试次数未超限，
                # 硬重启后本地重跑同一用例；仅保留最后一次结果，不逐次落盘。
                while (
                    not result.is_passed
                    and not self._repair_mode
                    and ENABLE_FAILED_REQUEUE
                    and getattr(case, "retryable", True)
                    and failed_retry_count < MAX_RETRY_PER_CASE
                ):
                    failed_retry_count += 1
                    logger.warning(
                        "[batch] %s 断言/步骤失败，硬重启后本地重试 %d/%d",
                        case_stem, failed_retry_count, MAX_RETRY_PER_CASE,
                    )
                    try:
                        await _hard_restart_for_retry(
                            "batch-failed-retry", "断言/步骤失败",
                        )
                    except Exception as restart_exc:
                        logger.error(
                            "[batch] %s 失败重试硬重启异常，停止重试，保留当前失败结果: %s",
                            case_stem, restart_exc,
                        )
                        break
                    result, collector = await _run_with_execution_retry()

                _diag_t0 = time.monotonic()
                logger.log(NOTICE, "[hang-trace] START stage=write_result case=%s", case_stem)
                collector.write_json()
                logger.log(
                    NOTICE, "[hang-trace] END stage=write_result case=%s elapsed_ms=%d",
                    case_stem, int((time.monotonic() - _diag_t0) * 1000),
                )

                status = "passed" if result.is_passed else "failed"
                failure_summary = str(getattr(result, "first_failure_summary", "") or "")
                if result.is_passed:
                    summary.passed += 1
                else:
                    summary.failed += 1

            except Exception as exc:
                logger.error("[batch] %s 执行异常: %s", case_stem, exc, exc_info=True)
                status = "error"
                failure_summary = str(exc)
                summary.failed += 1
                _write_fallback_result_json(
                    case_stem, tcs_file, case_result_dir, result_path, reason=str(exc),
                )

            duration_ms = int((time.monotonic() - t_case) * 1000)
            overall = "PASSED" if status == "passed" else "FAILED"
            case_title = case.name if case else case_stem
            case_summary_line = f"[{case_stem}]{case_title} → {overall}"
            print(f"\n{case_summary_line}")
            logger.log(NOTICE, "[batch] %s", case_summary_line)
            case_entry = {
                "name": case_stem,
                "case_title": case_title,
                "case_file": tcs_file,
                "app_name": app_name,
                "case_os": case_os,
                "status": status,
                "duration_ms": duration_ms,
                "result_path": result_path,
                "failure_summary": failure_summary,
                "retry_count": failed_retry_count,
            }
            summary.cases.append(case_entry)
            batch_ctx.entries.append(case_entry)

            # case_end 发生在 StreamingRunner 写盘前；单条报告必须等 result.json
            # 就绪后再触发，且 await 保证本条上报完成后才开始下一条用例。
            if hook_mgr:
                case_report_ctx = HookContext(
                    case_name=case_stem,
                    case_file=tcs_file,
                    report_dir=case_result_dir,
                    run_dir=batch_run_dir,
                    engine=self._engine,
                    batch_id=self._batch_id,
                    html_report=self._html_report,
                    case_result=result,
                    extra={
                        "status": status,
                        "result_path": result_path,
                        "duration_ms": duration_ms,
                    },
                )
                _diag_t0 = time.monotonic()
                logger.log(
                    NOTICE, "[hang-trace] START stage=case_hook case=%s hook=case_result_ready",
                    case_stem,
                )
                await hook_mgr.emit("case_result_ready", case_report_ctx)
                logger.log(
                    NOTICE,
                    "[hang-trace] END stage=case_hook case=%s hook=case_result_ready elapsed_ms=%d",
                    case_stem, int((time.monotonic() - _diag_t0) * 1000),
                )
        summary.duration_ms = int((time.monotonic() - t_start) * 1000)

        # 触发 batch_end（始终执行，即使中间有异常）
        batch_ctx.case_result = summary
        if hook_mgr:
            try:
                _diag_t0 = time.monotonic()
                logger.log(NOTICE, "[hang-trace] START stage=batch_hook hook=batch_end")
                await hook_mgr.emit("batch_end", batch_ctx)
                logger.log(
                    NOTICE, "[hang-trace] END stage=batch_hook hook=batch_end elapsed_ms=%d",
                    int((time.monotonic() - _diag_t0) * 1000),
                )
            except Exception as exc:
                logger.warning("[batch] batch_end hook 异常: %s", exc, exc_info=True)

        # 写 summary.json 到本次批次时间戳目录
        batch_dir = os.path.join(self._result_dir, run_ts)
        summary_path = os.path.join(batch_dir, "summary.json")
        _diag_t0 = time.monotonic()
        logger.log(
            NOTICE, "[hang-trace] START stage=write_summary path=%s", summary_path,
        )
        os.makedirs(batch_dir, exist_ok=True)
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(asdict(summary), f, ensure_ascii=False, indent=2)
        logger.log(
            NOTICE, "[hang-trace] END stage=write_summary elapsed_ms=%d path=%s",
            int((time.monotonic() - _diag_t0) * 1000), summary_path,
        )

        overall = "ALL PASSED" if summary.failed == 0 else f"{summary.failed} FAILED"
        print(f"\n{overall}  {summary.passed}/{summary.total} 通过  ({summary.duration_ms}ms)")
        print(f"Summary → {summary_path}")
        logger.info("[batch] %s  %s/%s 通过  (%sms)", overall, summary.passed, summary.total, summary.duration_ms)
        logger.info("[batch] Summary → %s", summary_path)
        return summary

    # ── 内部方法 ────────────────────────────────────────────────

    def _collect_files(self) -> list[str]:
        from uita.config import ROOT_DIR
        cwd = os.getcwd()
        files: list[str] = []
        for raw in self._cases_dirs:
            # 路径解析
            if os.path.isabs(raw):
                resolved = raw
            else:
                cwd_c = os.path.normpath(os.path.join(cwd, raw))
                root_c = os.path.normpath(os.path.join(ROOT_DIR, raw))
                resolved = cwd_c if os.path.exists(cwd_c) else (root_c if os.path.exists(root_c) else cwd_c)

            if os.path.isfile(resolved) and resolved.endswith(".tcs"):
                files.append(resolved)
            elif os.path.isdir(resolved):
                for root, _, fnames in os.walk(resolved):
                    tcs_names = [f for f in sorted(fnames) if f.endswith(".tcs")]
                    for fname in _reorder_lettered_prefix(tcs_names):
                        files.append(os.path.join(root, fname))
            else:
                logger.warning("[batch] 路径不存在: %s", resolved)
        return files

    def _apply_filters(self, files: list[str]) -> list[str]:
        """按 os（是否与当前宿主机匹配）+ tag + priority 三维过滤。

        os 过滤无条件应用（不再像 tag/priority 那样"未传参就整体跳过"）——
        因为空/universal 的用例总是匹配，只有声明了具体平台且与宿主机不一致
        的用例才会被排除，对未声明 os 的既有用例零影响。
        """
        filtered = []
        skipped_os = 0
        for f in files:
            try:
                case = parse_file(f)
                if not platform_matches_host(case.os):
                    skipped_os += 1
                    continue
                if self._tag_filter and not self._match_tag(case, self._tag_filter):
                    continue
                if self._priority_filter and not self._match_priority(case, self._priority_filter):
                    continue
                filtered.append(f)
            except Exception:
                filtered.append(f)  # 解析失败的不过滤
        if skipped_os:
            logger.info("[batch] os 平台过滤：%d 个用例因平台不匹配被排除", skipped_os)
        return filtered

    @staticmethod
    def _match_tag(case, tag_expr: str) -> bool:
        case_tags = getattr(case, "tags", []) or []
        if isinstance(case_tags, str):
            case_tags = [t.strip() for t in case_tags.split(",")]
        if not case_tags:
            return True  # 无 tag 字段则不过滤
        negate = tag_expr.startswith("!")
        target = tag_expr.lstrip("!").lower().strip()
        matched = any(target == t.lower() for t in case_tags)
        return (not matched) if negate else matched

    @staticmethod
    def _match_priority(case, priority_expr: str) -> bool:
        case_prio = getattr(case, "priority", "") or ""
        if not case_prio:
            return True  # 无 priority 字段则不过滤
        negate = priority_expr.startswith("!")
        target = priority_expr.lstrip("!").upper().strip()
        matched = case_prio.upper() == target
        return (not matched) if negate else matched

    def _random_sample(self, files: list[str]) -> list[str]:
        import random
        from collections import defaultdict
        by_dir: dict[str, list[str]] = defaultdict(list)
        for f in files:
            by_dir[os.path.dirname(f)].append(f)
        sampled = []
        for flist in by_dir.values():
            sampled.append(random.choice(flist))
        return sampled

    def _build_resolver(self, case, case_file: str = "", app_name: str = "") -> Resolver:
        """组装 resolver（提取到 core/runner/resolver_factory.py::build_resolver_for_app()，
        与 interpreter.py 共用，纯提取零行为变化）。

        app_name/os_name 解析失败（如未注册的 app）时降级为空 ElementMapLoader，
        与提取前行为一致。
        """
        app_name = app_name or case.app or infer_app_from_path(case_file) or "sample_web"
        os_name = effective_platform_for_case(case.os)
        try:
            return build_resolver_for_app(self._engine, app_name, os_name)
        except Exception:
            from uita.core.platform.element_map_loader import ElementMapLoader as EML
            element_map = EML.__new__(EML)
            element_map._entries = {}
            element_map._loaded = True
            return Resolver(self._engine, element_map)

    def _make_step_cb(self, case_name: str, case=None, app_name: str = "") -> OnStepCallback:
        n_setup = len(case.setup_steps) if case else 0
        n_main = len(case.steps) if case else 0

        async def _cb(idx, sr, _name):
            icon = "PASSED" if sr.status == "passed" else "FAILED"
            msg = f" — {sr.message}" if sr.message else ""
            if idx >= n_setup + n_main:
                prefix = "[结束]"
            elif idx < n_setup:
                prefix = "[初始化]" if n_setup == 1 else f"[初始化{idx + 1}]"
            else:
                prefix = f"[{idx - n_setup + 1}]"
            summary_line = f"{prefix}{sr.name} ({int(sr.duration_ms)}ms){msg} → {icon}"
            print(f"    {summary_line}")
            logger.log(NOTICE, "[batch] %s %s", case_name, summary_line)
        return _cb
