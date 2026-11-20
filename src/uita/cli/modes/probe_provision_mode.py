"""--mode probe-provision：部署期注入 Qt UI 探针（调用 apps/<app>/probe_provision.py）。"""
from __future__ import annotations

import logging

logger = logging.getLogger("uita")

_STATUS_ICON = {"success": "\u2705", "skipped": "\u26a0\ufe0f", "failed": "\u274c"}


def _handle_probe_provision(cli) -> int:
    """probe-provision 模式：部署期一次性注入探针（拷探针 → 重启 → 等端口）。

    与 `--mode pretest` 同构的运维辅助动作：
    - 缺少 `--app` → warning 并跳过，返回 0；
    - 执行 failed 也返回 0（不阻断流水线后续的用例执行步骤），
      但 stdout 必须如实报告状态，不能一律打印 success。
    - `--probe-report <path>` 写结构化事实（install_dir/dll/ports/elapsed），
      供 CI 合并进 context。
    """
    if not cli.app:
        logger.warning("[probe-provision] 缺少 --app 参数，跳过")
        print("\u26a0\ufe0f  --mode probe-provision 需要 --app <app_name>，已跳过")
        return 0

    from uita.core.lifecycle.probe_provision import run_probe_provision

    result = run_probe_provision(cli.app, report_path=getattr(cli, "probe_report", "") or "")
    if result.status == "failed":
        logger.error("[probe-provision] %s 执行失败: %s", cli.app, result.message)
    elif result.status == "skipped":
        logger.info("[probe-provision] %s 已跳过: %s", cli.app, result.message)
    else:
        logger.info("[probe-provision] %s 执行成功: %s", cli.app, result.message)

    icon = _STATUS_ICON.get(result.status, "?")
    print(f"{icon} [probe-provision] {cli.app} \u2192 {result.status}: {result.message}")
    return 0
