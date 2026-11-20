"""--mode install：下载并（覆盖）安装被测 App（--app + --package-url）。"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

from uita.cli.modes.common import StartupError
from uita.config import NOTICE

logger = logging.getLogger("uita")


def _handle_install(cli) -> int:
    """install 模式：下载并（覆盖）安装被测 App，见 docs/plans/
    archive/07-db-data-实施纪要.md。

    2026-07-29 行为调整：`--package-url` 为空时不再报 `StartupError`（exit 1），
    改为打印 warning 并直接跳过（return 0）——服务于流水线场景：
        python main.py --mode install --app sample_web --package-url {{dst_url}}
        python main.py --mode {{mode}} --cases-dir {{case_dir}}
    当上游未解析出有效的 `{{dst_url}}`（如未配置安装包分发链接）时，install 步骤
    不应中断流水线导致后续用例执行被跳过；`--app` 仍是硬性必填（缺失属于调用方
    参数错误，与"没有可用安装包"是不同性质的问题，维持原 StartupError 行为）。

    2026-08-11 新增 `--uninstall` 参数：先卸载旧版本再安装新版本。
    适用于 CI 流水线场景：自动化测试机需要始终有 App 存在，卸载后立即安装新版本。
    见 docs/plans/archive/09-apps-business-实施纪要.md。

    `--install-report <path>`：安装成功后把事实（目标路径 / 版本 / 隔离属性复核 /
    耗时 / 时间戳）写成 JSON，供 CI wrapper 合并进 context 并注入 prompt。
    设计依据见 docs/plans/done/archive_202610.md。
    """
    if not cli.app:
        raise StartupError("--app 是 install 模式的必填参数")
    if not cli.package_url:
        logger.warning("[install] 没有找到有效的安装包下载链接（--package-url 为空），跳过安装，不阻断后续执行")
        print("\u26a0\ufe0f  没有找到有效的安装包下载链接，跳过安装")
        return 0

    # --uninstall=true 时先卸载旧版本
    if getattr(cli, "uninstall", False):
        from uita.core.lifecycle.app_uninstaller import UninstallError, uninstall_app
        try:
            uninstall_app(cli.app)
            logger.log(NOTICE, "[install] 旧版本卸载完成：%s", cli.app)
            print(f"\u2705 旧版本卸载完成：{cli.app}")
        except UninstallError as exc:
            raise StartupError(str(exc)) from exc

    from uita.core.lifecycle.app_installer import (
        InstallError,
        collect_install_facts,
        install_app,
    )

    platform_name = "macOS" if sys.platform == "darwin" else "Windows"
    t_start = time.time()
    try:
        resolved_path = install_app(cli.app, cli.package_url)
    except InstallError as exc:
        raise StartupError(str(exc)) from exc
    duration_s = time.time() - t_start

    facts = collect_install_facts(
        cli.app, resolved_path, cli.package_url, platform_name, duration_s,
    )
    logger.log(
        NOTICE,
        "[install] 安装事实: app=%s path=%s version=%s quarantine=%s 耗时=%.1fs",
        facts["app"], facts["target_path"], facts["version"] or "unknown",
        facts["quarantine"] or "<none>", facts["duration_s"],
    )
    print(
        f"[install] 目标路径: {facts['target_path']} | 版本: {facts['version'] or 'unknown'} | "
        f"隔离属性: {facts['quarantine'] or '无（已清除）'}"
    )

    report_path = getattr(cli, "install_report", "") or ""
    if report_path:
        _write_install_report(report_path, facts)

    print(f"\u2705 安装完成：{cli.app} → {resolved_path}")
    return 0


def _write_install_report(report_path: str, facts: dict) -> None:
    """把安装事实写成 JSON。写失败只告警，不阻断安装（事实缺失由 CI 侧记 unknown）。"""
    try:
        parent = os.path.dirname(os.path.abspath(report_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(facts, f, ensure_ascii=False, indent=2)
        logger.log(NOTICE, "[install] 安装事实已落盘: %s", report_path)
    except Exception as exc:  # noqa: BLE001  best-effort，不影响安装结果
        logger.warning("[install] 安装事实落盘失败（忽略）: %s", exc)


__all__ = ["_handle_install"]
