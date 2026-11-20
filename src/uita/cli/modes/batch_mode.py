"""--mode batch：顺序批量执行目录。"""
from __future__ import annotations

import asyncio
import glob as _glob
import logging
import os
import time

from uita.cli.modes.common import (
    StartupError,
    _apply_main_app_version,
    _expand_cases_dir,
    _parse_tag_filter,
    _resolve_case_name_tokens,
)

logger = logging.getLogger("uita")


async def _batch_async(cli) -> int:
    from uita.core.runner.batch_runner import BatchRunner
    from uita.core.result.archiver import generate_html_report
    from uita.config import DISPATCH_BATCH_ID, REPORT_DIR

    raw_tokens = _expand_cases_dir(cli.cases_dir)
    if not raw_tokens:
        raise StartupError("--cases-dir 未指定")
    raw_tokens = _resolve_case_name_tokens(raw_tokens, (getattr(cli, "app", "") or "").strip())
    if not raw_tokens:
        raise StartupError("--cases-dir 指定的用例名均未匹配到任何 .tcs 文件")

    hook_names = list(cli.hooks or [])
    html_report = (getattr(cli, "html_report", "") or "").strip()

    engine = await _resolve_engine_for_batch(raw_tokens)

    _batch_app = _infer_batch_app(raw_tokens)
    _apply_main_app_version(_batch_app, cli.version or "")

    tag_include, tag_exclude = _parse_tag_filter(cli.tag)
    pri_include, pri_exclude = _parse_tag_filter(cli.priority)

    batch_id = os.environ.get("UITA_BATCH_ID", "").strip() or DISPATCH_BATCH_ID

    runner = BatchRunner(
        engine=engine,
        cases_dirs=raw_tokens,
        result_dir=REPORT_DIR,
        hook_names=hook_names,
        tag_filter=cli.tag,
        priority_filter=cli.priority,
        random_mode=cli.random,
        batch_id=batch_id,
        html_report=html_report,
    )
    try:
        await runner.run()
    finally:
        _diag_t0 = time.monotonic()
        logger.log(
            25, "[hang-trace] START stage=engine_close engine=%s",
            type(engine).__name__,
        )
        try:
            await engine.close()
            logger.log(
                25, "[hang-trace] END stage=engine_close elapsed_ms=%d engine=%s",
                int((time.monotonic() - _diag_t0) * 1000), type(engine).__name__,
            )
        except Exception as exc:
            logger.warning(
                "[hang-trace] ERROR stage=engine_close elapsed_ms=%d engine=%s error=%s",
                int((time.monotonic() - _diag_t0) * 1000), type(engine).__name__, exc,
                exc_info=True,
            )
        _diag_t0 = time.monotonic()
        logger.log(25, "[hang-trace] START stage=stack_close_all")
        try:
            from uita.core.lifecycle.app_lifecycle_stack import get_default_stack
            await get_default_stack().close_all()
            logger.log(
                25, "[hang-trace] END stage=stack_close_all elapsed_ms=%d",
                int((time.monotonic() - _diag_t0) * 1000),
            )
        except Exception as exc:
            logger.warning(
                "[hang-trace] ERROR stage=stack_close_all elapsed_ms=%d error=%s",
                int((time.monotonic() - _diag_t0) * 1000), exc,
                exc_info=True,
            )

    _log_tag_filter_info(cli.tag, tag_include, tag_exclude)
    _log_tag_filter_info(cli.priority, pri_include, pri_exclude, label="priority")

    _ts_dirs = sorted(_glob.glob(os.path.join(REPORT_DIR, "*")), reverse=True)
    summary_path = next(
        (os.path.join(d, "summary.json") for d in _ts_dirs if os.path.isfile(os.path.join(d, "summary.json"))), "")
    if summary_path and os.path.isfile(summary_path):
        _diag_t0 = time.monotonic()
        logger.log(25, "[hang-trace] START stage=html_report path=%s", summary_path)
        try:
            report_path = generate_html_report(summary_path)
            print(f"Report  \u2192 {report_path}")
            logger.log(
                25, "[hang-trace] END stage=html_report elapsed_ms=%d path=%s",
                int((time.monotonic() - _diag_t0) * 1000), report_path,
            )
        except Exception as exc:
            logger.warning(
                "[hang-trace] ERROR stage=html_report elapsed_ms=%d error=%s",
                int((time.monotonic() - _diag_t0) * 1000), exc,
                exc_info=True,
            )
    logger.log(25, "[hang-trace] BATCH_ASYNC_RETURN")
    return 0


def _log_tag_filter_info(raw: str | None, inc: list[str], exc: list[str],
                         label: str = "tag") -> None:
    if not raw:
        return
    parts = []
    if inc:
        parts.append(f"include={inc}")
    if exc:
        parts.append(f"exclude={exc}")
    logger.info("[batch] %s 过滤: %s (%s)", label, raw, ", ".join(parts))


def _infer_batch_app(cases_dirs: list[str]) -> str:
    """从 cases_dirs 推断主 app（取首个 .tcs 的 app 字段 / 路径）。"""
    from uita.core.dsl.parser import infer_app_from_path, parse_file
    for raw in cases_dirs:
        first_tcs = ""
        if os.path.isfile(raw) and raw.endswith(".tcs"):
            first_tcs = raw
        elif os.path.isdir(raw):
            for r, _, fs in os.walk(raw):
                for f in sorted(fs):
                    if f.endswith(".tcs"):
                        first_tcs = os.path.join(r, f)
                        break
                if first_tcs:
                    break
        if first_tcs:
            try:
                case = parse_file(first_tcs)
                return case.app or infer_app_from_path(first_tcs) or "sample_web"
            except Exception:
                return infer_app_from_path(first_tcs) or "sample_web"
    return "sample_web"


async def _resolve_engine_for_batch(cases_dirs: list[str]) -> object:
    """按首个 .tcs 的 app 经 AppLifecycleStack 统一路由获取引擎。"""
    from uita.core.dsl.parser import infer_app_from_path, parse_file
    from uita.core.lifecycle.app_lifecycle_stack import get_default_stack
    from uita.core.registry_common import UNIVERSAL_PLATFORM, resolve_universal_platform

    first_tcs = ""
    for raw in cases_dirs:
        if os.path.isfile(raw) and raw.endswith(".tcs"):
            first_tcs = raw
            break
        if os.path.isdir(raw):
            for r, _, fs in os.walk(raw):
                for f in sorted(fs):
                    if f.endswith(".tcs"):
                        first_tcs = os.path.join(r, f)
                        break
                if first_tcs:
                    break
        if first_tcs:
            break

    app_name = "sample_web"
    if first_tcs:
        try:
            case = parse_file(first_tcs)
            app_name = case.app or infer_app_from_path(first_tcs) or app_name
        except Exception:
            pass

    os_name = resolve_universal_platform(UNIVERSAL_PLATFORM)

    mgr = await get_default_stack().get_or_create(app_name, os_name)
    get_default_stack().register_root(app_name, os_name, mgr.engine)
    logger.info("[batch] 引擎 %s/%s（via AppLifecycleStack）", app_name, os_name)
    return mgr.engine


def _handle_batch(cli) -> int:
    started = time.monotonic()
    logger.log(25, "[hang-trace] START stage=asyncio_run mode=batch")
    result = asyncio.run(_batch_async(cli))
    logger.log(
        25, "[hang-trace] END stage=asyncio_run elapsed_ms=%d mode=batch",
        int((time.monotonic() - started) * 1000),
    )
    return result
