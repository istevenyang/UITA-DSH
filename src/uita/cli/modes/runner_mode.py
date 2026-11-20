"""--mode runner：单文件/多文件顺序执行（本地调试）。

R30 变更：去除 --file 必填参数，改为通过 --app + --cases-dir/--case-dir 定位
用例——支持目录、.tcs 文件路径、裸用例名（按 <name>.tcs 文件名 → TCS 头
name 字段两级匹配，--app 用于限定裸用例名的查找范围为 cases/<app>/）。
解析结果可能是单个文件（原有行为不变）或多个文件（逐个顺序执行，各自独立
启动/关闭引擎，互不共享生命周期，区别于 batch 模式的跨 case 引擎复用）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from uita.cli.modes.common import (
    StartupError,
    _apply_main_app_version,
    _collect_tcs_files,
    _expand_cases_dir,
    _resolve_case_name_tokens,
)

logger = logging.getLogger("uita")


async def _close_lifecycle_stack() -> None:
    """清理 AppLifecycleStack 中的全部引擎（含 switch[]/run_exe[] 创建的）。

    异常安全：清理失败只记录警告，不改变用例执行结果与退出码。
    """
    started = time.monotonic()
    logger.log(25, "[hang-trace] START stage=stack_close_all")
    try:
        from uita.core.lifecycle.app_lifecycle_stack import get_default_stack
        await get_default_stack().close_all()
        logger.log(
            25, "[hang-trace] END stage=stack_close_all elapsed_ms=%d",
            int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:
        logger.warning(
            "[hang-trace] ERROR stage=stack_close_all elapsed_ms=%d error=%s",
            int((time.monotonic() - started) * 1000), exc, exc_info=True,
        )


async def _run_one_case(case_file: str, cli) -> int:
    """执行单个 .tcs 文件（原 runner 模式单文件逻辑，供多文件场景复用）。"""
    from uita.core.dsl.parser import parse_file
    from uita.core.interpreter import TCSInterpreter
    from uita.core.result.collector import make_run_result_dir

    case_file = os.path.abspath(case_file)
    try:
        case = parse_file(case_file)
    except FileNotFoundError:
        raise StartupError(f"文件不存在: {case_file}")

    # --version 主 app 兜底：推断 app_name 后写入版本
    from uita.core.dsl.parser import infer_app_from_path as _infer
    _app = case.app or _infer(case_file) or "sample_web"
    _apply_main_app_version(_app, cli.version or "")

    if cli.no_engine:
        logger.info("[dry-run] 跳过引擎启动")
        print(f"[dry-run] 解析成功: {case.name}  {len(case.setup_steps + case.steps + case.teardown_steps)} 个 step")
        return 0

    result_dir = make_run_result_dir(case_file)
    result_json = os.path.join(result_dir, "result.json")
    print(f"\n\u25b6  {case.name}")

    case_stem = os.path.splitext(os.path.basename(case_file))[0]

    async def _on_step(step_name: str, status: str, duration_ms: float, message: str,
                       step_num: int = 0, phase: str = "main") -> None:
        icon = "\u2705 PASSED" if status == "passed" else "\u274c FAILED"
        msg = f" \u2014 {message}" if message else ""
        if phase == "setup":
            prefix = "[初始化]" if len(case.setup_steps) == 1 else f"[初始化{step_num}]"
        elif phase == "teardown":
            prefix = "[结束]"
        else:
            prefix = f"[{step_num}]"
        print(f"  {prefix}{step_name} ({int(duration_ms)}ms){msg} \u2192 {icon}")

    hook_names = cli.hooks or None
    interp = TCSInterpreter(case, case_file=case_file, result_dir=result_dir,
                            on_step=_on_step, hook_names=hook_names)
    try:
        collector = await interp.run()
    finally:
        # interpreter 只 close() 当前栈顶引擎；用例以 switch[<other_app>] 收尾时
        # 根引擎不会被关闭。与 batch 模式一致地清理整个 AppLifecycleStack。
        await _close_lifecycle_stack()
    result = collector.finalize()

    overall = "\u2705 \u2705 \u2705 PASSED" if result.is_passed else "\u274c \u274c \u274c FAILED"
    case_title = result.case_name or case_stem
    print(f"\n[{case_stem}]{case_title} \u2192 {overall}")
    print(f"Result \u2192 {result_json}")
    return 0


def _resolve_runner_files(cli) -> list[str]:
    """解析 runner 模式待执行的 .tcs 文件列表：

    --cases-dir/--case-dir 展开 → 裸用例名按 --app 限定范围解析为文件路径
    → 目录递归收集为文件列表。默认值为全局共享的 `["cases"]`（未显式传参时），
    此时若也未指定 --app，视为"未明确目标"直接报错，避免误跑全部 cases/。
    """
    app = (getattr(cli, "app", "") or "").strip()
    raw = list(cli.cases_dir or [])
    if not app and raw == ["cases"]:
        raise StartupError(
            "runner 模式需要 --app 或明确的 --cases-dir/--case-dir 定位用例"
            "（避免默认误跑全部 cases/ 用例）",
        )

    tokens = _expand_cases_dir(cli.cases_dir)
    tokens = _resolve_case_name_tokens(tokens, app)
    files = _collect_tcs_files(tokens)
    if not files:
        raise StartupError("未找到可执行的 .tcs 用例（--cases-dir/--app 未匹配到任何文件）")
    return files


async def _runner_async(cli) -> int:
    files = _resolve_runner_files(cli)

    if len(files) == 1:
        return await _run_one_case(files[0], cli)

    logger.info("[runner] 共 %d 个用例，开始顺序执行", len(files))
    for case_file in files:
        await _run_one_case(case_file, cli)
    return 0


def _handle_runner(cli) -> int:
    return asyncio.run(_runner_async(cli))
