"""--mode pretest：测试前数据/现场构造（调用 apps/<app>/pre_test.py）。"""
from __future__ import annotations

import logging

logger = logging.getLogger("uita")

_PRETEST_ICON = {"success": "\u2705", "skipped": "\u26a0\ufe0f", "failed": "\u274c"}


def _handle_pretest(cli) -> int:
    """pretest 模式：调用 apps/<app>/pre_test.py 构造测试数据/测试现场。

    pretest 数据构造失败**不阻断**后续 batch/runner 等模式执行，
    因此退出码始终为 0（符合 R24：用例/构造失败不影响退出码）。
    但 stdout 必须如实反映 success/skipped/failed 真实状态（而非一律报告
    success），否则 CI 无法从命令输出感知构造失败，只能去翻日志文件。
    缺少 --app 时 warning 并跳过，同样返回 0。
    """
    if not cli.app:
        logger.warning("[pretest] 缺少 --app 参数，跳过 pretest")
        print("\u26a0\ufe0f  --mode pretest 需要 --app <app_name>，已跳过")
        return 0

    from uita.core.lifecycle.pretest import run_pretest

    result = run_pretest(cli.app)
    if result.status == "failed":
        logger.error("[pretest] %s 执行失败: %s", cli.app, result.message)
    elif result.status == "skipped":
        logger.info("[pretest] %s 已跳过: %s", cli.app, result.message)
    else:
        logger.info("[pretest] %s 执行成功: %s", cli.app, result.message)

    icon = _PRETEST_ICON.get(result.status, "?")
    print(f"{icon} [pretest] {cli.app} \u2192 {result.status}: {result.message}")
    return 0
