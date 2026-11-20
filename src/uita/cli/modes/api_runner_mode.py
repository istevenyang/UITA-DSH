"""API-only 单文件/多文件顺序执行 mode。"""
from __future__ import annotations

import asyncio
import logging
import os

from uita.cli.modes.common import (
    StartupError,
    _collect_tcs_files,
    _expand_cases_dir,
    _resolve_case_name_tokens,
)
from uita.config import ENABLE_FAILED_REQUEUE, MAX_RETRY_PER_CASE, REPORT_DIR
from uita.core.dsl.parser import parse_file
from uita.core.hooks.base import HookContext, HookManager
from uita.core.hooks.registry import build_hooks
from uita.core.result.collector import CaseResult
from uita.core.runner.api_runner import ApiRunner, ApiRunnerStartupError

logger = logging.getLogger(__name__)


async def _run_file_with_retry(runner: ApiRunner, file: str) -> CaseResult:
    result = None
    failed_retry_count = 0
    while True:
        try:
            result = await runner.run_file(file)
        except ApiRunnerStartupError as exc:
            raise StartupError(str(exc)) from exc
        if (
            result.is_passed
            or not ENABLE_FAILED_REQUEUE
            or failed_retry_count >= MAX_RETRY_PER_CASE
        ):
            return result
        failed_retry_count += 1
        logger.warning(
            "[api-runner] %s 断言/步骤失败，重新执行 %d/%d",
            os.path.basename(file), failed_retry_count, MAX_RETRY_PER_CASE,
        )


async def _api_runner_async(cli) -> int:
    files = _resolve_api_runner_files(cli)
    runner = ApiRunner()
    batch_id = os.environ.get("UITA_BATCH_ID", "")

    hook_names = list(getattr(cli, "hooks", None) or [])
    hooks = build_hooks(hook_names)
    hook_mgr = HookManager(hooks) if hooks else None

    batch_ctx = HookContext(report_dir=REPORT_DIR, run_dir=REPORT_DIR, batch_id=batch_id)
    if hook_mgr:
        await hook_mgr.emit("batch_start", batch_ctx)

    entries: list[dict] = []
    for file in files:
        result = await _run_file_with_retry(runner, file)
        print(
            f"[api-runner] case={result.case_name} "
            f"passed={result.passed} failed={result.failed} result_dir={result.result_dir}"
        )
        status = "passed" if result.is_passed else "failed"
        result_path = os.path.join(result.result_dir, "result.json")
        case_stem = os.path.splitext(os.path.basename(file))[0]
        entries.append({
            "name": case_stem,
            "case_title": result.case_name or case_stem,
            "case_file": file,
            "status": status,
            "duration_ms": result.duration_ms,
            "result_path": result_path,
            "failure_summary": result.first_failure_summary,
        })
        if hook_mgr:
            await hook_mgr.emit(
                "case_result_ready",
                HookContext(
                    case_name=case_stem,
                    case_file=file,
                    report_dir=result.result_dir,
                    run_dir=os.path.dirname(result.result_dir),
                    batch_id=batch_id,
                    case_result=result,
                    extra={
                        "status": status,
                        "result_path": result_path,
                        "duration_ms": result.duration_ms,
                    },
                ),
            )

    if hook_mgr:
        batch_ctx.entries = entries
        try:
            await hook_mgr.emit("batch_end", batch_ctx)
        except Exception as exc:
            logger.warning("[api-runner] batch_end hook 异常: %s", exc)

    return 0


def _resolve_api_runner_files(cli) -> list[str]:
    raw = list(getattr(cli, "cases_dir", None) or ["cases"])
    if raw == ["cases"]:
        raise StartupError("api-runner 模式需要 --cases-dir/--case-dir 指定 .tcs 文件或目录")

    app = (getattr(cli, "app", "") or "").strip()
    tokens = _resolve_case_name_tokens(_expand_cases_dir(raw), app)
    files = sorted(_collect_tcs_files(tokens), key=lambda path: path.casefold())
    files = _filter_api_files_by_meta(
        files,
        getattr(cli, "tag", None),
        getattr(cli, "priority", None),
    )
    if not files:
        raise StartupError(f"api-runner 模式未找到 .tcs 文件: {raw}")
    return files


def _filter_api_files_by_meta(
    files: list[str],
    tag_expr: str | None,
    priority_expr: str | None,
) -> list[str]:
    if not tag_expr and not priority_expr:
        return files
    filtered: list[str] = []
    for f in files:
        try:
            case = parse_file(f)
        except Exception:
            filtered.append(f)
            continue
        if tag_expr and not _match_meta(case.tags, tag_expr, upper=False):
            continue
        if priority_expr and not _match_meta([case.priority], priority_expr, upper=True):
            continue
        filtered.append(f)
    return filtered


def _match_meta(values: list[str], expr: str, *, upper: bool) -> bool:
    values = [v for v in (values or []) if str(v).strip()]
    negate = expr.startswith("!")
    target = expr.lstrip("!").strip()
    if upper:
        matched = any(target.upper() == str(v).upper() for v in values)
    else:
        matched = any(target.lower() == str(v).lower() for v in values)
    return (not matched) if negate else matched


def _handle_api_runner(cli) -> int:
    return asyncio.run(_api_runner_async(cli))
