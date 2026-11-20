"""UITA-DSH 统一入口。

执行模式（通过 --mode 分发）：
  parse          — 解析 .tcs 文件打印 AST
  runner         — 单文件/多文件执行 TCS 用例
  batch          — 顺序批量执行目录
  probe          — 元素探查，生成 rosetta_map 骨架
  probe-diff     — 探查草稿 vs 现有 rosetta_map.json 语义 diff
  rosetta-fp     — Rosetta 视觉指纹编解码与静态截图匹配
  install        — 下载并（覆盖）安装被测 App
  lint           — TCS / rosetta_map 格式检查，生成 HTML 报告
  pretest        — 测试前数据/现场构造（调用 apps/<app>/pre_test.py）
  probe-provision — 部署期一次性注入 Qt UI 探针
  env_check      — 运行环境与依赖自检
  health-check   — 存量 TCS 用例健康检查（Dry-Run）
  perf           — 客户端性能多轮采集与可选 AI 报告
  api-runner     — API-only 应用接口用例执行
  dsh-serve      — DSH 本地控制台与 HTTP 网关
  dsh-run        — DSH 无头执行自然语言目标或文本用例
  dsh-replay     — DSH 零 LLM 重放步骤 IR
  dsh-mcp        — DSH MCP 工具面服务（stdio / http）
"""
from __future__ import annotations

import os
import sys

_ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if _ROOT_DIR not in sys.path:
    sys.path.insert(0, _ROOT_DIR)
_SRC_DIR = os.path.dirname(_ROOT_DIR)
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)


def _ensure_aqua_session() -> None:
    if sys.platform != "darwin":
        return
    if os.environ.get("UITA_SKIP_AQUA_RELAUNCH", "") == "1":
        return
    if os.environ.get("_UITA_AQUA_RELAUNCHED") == "1":
        return

    import subprocess
    try:
        mgr = subprocess.run(
            ["launchctl", "managername"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
    except Exception:
        return

    if mgr == "Aqua":
        return

    try:
        uid = os.getuid()
        os.environ["_UITA_AQUA_RELAUNCHED"] = "1"
        os.execvp(
            "launchctl",
            ["launchctl", "asuser", str(uid), sys.executable, *sys.argv],
        )
    except Exception as exc:
        print(f"[UITA] Aqua session 自重启失败（忽略，继续原有流程）: {exc}", file=sys.stderr)


from uita.config import setup_logging  # noqa: E402

setup_logging()

import logging  # noqa: E402

logger = logging.getLogger("uita")


def _auto_discover() -> None:
    import importlib

    from uita.config import PKG_DIR, WORKSPACE_DIR

    actions_root = os.path.join(PKG_DIR, "core", "actions", "common")
    registered_actions: list[str] = []
    if os.path.isdir(actions_root):
        for filename in os.listdir(actions_root):
            if filename.startswith("action_") and filename.endswith(".py"):
                module_name = filename[:-3]
                try:
                    importlib.import_module(f"uita.core.actions.common.{module_name}")
                    registered_actions.append(module_name)
                except Exception as exc:
                    logger.warning("Failed to register common actions '%s': %s", module_name, exc)
    if registered_actions:
        logger.debug("Registered common actions (%d): %s", len(registered_actions), ", ".join(registered_actions))

    apps_root = os.path.join(WORKSPACE_DIR, "apps")
    registered_apps: list[str] = []
    if os.path.isdir(apps_root):
        if WORKSPACE_DIR not in sys.path:
            sys.path.insert(0, WORKSPACE_DIR)
        for app_name in os.listdir(apps_root):
            reg = os.path.join(apps_root, app_name, "register.py")
            if os.path.isfile(reg):
                try:
                    importlib.import_module(f"apps.{app_name}.register")
                    registered_apps.append(app_name)
                except Exception as exc:
                    logger.warning("Failed to register app '%s': %s", app_name, exc)
    if registered_apps:
        logger.debug("Registered app (目录扫描): %s", ", ".join(registered_apps))

    try:
        from importlib.metadata import entry_points
        registered_eps: list[str] = []
        for ep in entry_points(group="uita.apps"):
            try:
                ep.load()
                registered_eps.append(ep.name)
            except Exception as exc:
                logger.warning("Failed to register app via entry point '%s': %s", ep.name, exc)
        if registered_eps:
            logger.debug("Registered app (entry_points): %s", ", ".join(registered_eps))
    except Exception as exc:
        logger.debug("entry_points 扫描跳过: %s", exc)

    engine_root = os.path.join(PKG_DIR, "engine")
    registered_engines: list[str] = []
    if os.path.isdir(engine_root):
        for eng_name in os.listdir(engine_root):
            eng_file = os.path.join(engine_root, eng_name, "engine.py")
            if os.path.isfile(eng_file):
                try:
                    importlib.import_module(f"uita.engine.{eng_name}.engine")
                    registered_engines.append(eng_name)
                except Exception as exc:
                    logger.warning("Failed to register engine '%s': %s", eng_name, exc)
    if registered_engines:
        logger.debug("Registered engine: %s", ", ".join(registered_engines))


from uita.cli.modes.common import StartupError, _inject_app_versions, _validate_html_report  # noqa: E402
from uita.cli.modes.parse_mode import _handle_parse  # noqa: E402
from uita.cli.modes.runner_mode import _handle_runner  # noqa: E402
from uita.cli.modes.perf_mode import _handle_perf  # noqa: E402
from uita.cli.modes.api_runner_mode import _handle_api_runner  # noqa: E402
from uita.cli.modes.batch_mode import _handle_batch, _resolve_engine_for_batch  # noqa: E402,F401
from uita.cli.modes.probe_mode import _handle_probe  # noqa: E402
from uita.cli.modes.probe_diff_mode import _handle_probe_diff  # noqa: E402
from uita.cli.modes.install_mode import _handle_install  # noqa: E402
from uita.cli.modes.lint_mode import _handle_lint  # noqa: E402
from uita.cli.modes.rosetta_fingerprint_mode import _handle_rosetta_fingerprint  # noqa: E402
from uita.cli.modes.pretest_mode import _handle_pretest  # noqa: E402
from uita.cli.modes.probe_provision_mode import _handle_probe_provision  # noqa: E402
from uita.cli.modes.env_check_mode import _handle_env_check  # noqa: E402
from uita.cli.modes.health_check_mode import _handle_health_check  # noqa: E402
from uita.cli.modes.dsh_mode import (  # noqa: E402
    _handle_dsh_mcp, _handle_dsh_replay, _handle_dsh_run, _handle_dsh_serve,
)

_MODE_HANDLERS = {
    "parse": _handle_parse,
    "runner": _handle_runner,
    "perf": _handle_perf,
    "api-runner": _handle_api_runner,
    "batch": _handle_batch,
    "probe": _handle_probe,
    "probe-diff": _handle_probe_diff,
    "rosetta-fp": _handle_rosetta_fingerprint,
    "install": _handle_install,
    "lint": _handle_lint,
    "pretest": _handle_pretest,
    "probe-provision": _handle_probe_provision,
    "env_check": _handle_env_check,
    "health-check": _handle_health_check,
    "dsh-serve": _handle_dsh_serve,
    "dsh-run": _handle_dsh_run,
    "dsh-mcp": _handle_dsh_mcp,
    "dsh-replay": _handle_dsh_replay,
}


def main() -> None:
    from uita.cli.parser import build_parser
    parser = build_parser()
    cli = parser.parse_args()

    _inject_app_versions(cli)
    _auto_discover()
    _validate_html_report(cli)

    handler = _MODE_HANDLERS.get(cli.mode)
    if handler is None:
        parser.error(f"未知模式: {cli.mode}")

    exit_code = handler(cli)
    sys.exit(exit_code)


def _is_dsh_mcp_stdio_invocation() -> bool:
    argv = sys.argv[1:]
    if "--mode" not in argv:
        return False
    index = argv.index("--mode")
    if argv[index + 1:index + 2] != ["dsh-mcp"]:
        return False
    if "--dsh-transport" in argv:
        transport_at = argv.index("--dsh-transport")
        value = argv[transport_at + 1:transport_at + 2]
        return not value or value == ["stdio"]
    return True


def _diagnose_win_session() -> None:
    try:
        from uita.core.lifecycle.win_session import log_session_diagnostics
        log_session_diagnostics(logger)
    except Exception:
        pass


def _entrypoint() -> None:
    if sys.argv[1:2] == ["dsh"]:
        from uita.tools.dsh.cli import main as dsh_main

        sys.exit(dsh_main(sys.argv[2:]))
    stdio_protocol = _is_dsh_mcp_stdio_invocation()
    if stdio_protocol:
        from uita.cli.modes.dsh_mode import install_stdio_protocol_stream

        install_stdio_protocol_stream()
    _ensure_aqua_session()
    _diagnose_win_session()
    try:
        main()
    except StartupError as exc:
        logger.error("任务未能正常启动: %s", exc)
        sys.exit(1)
    except Exception as exc:
        logger.error("任务异常: %s", exc)
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("用户中断")
        sys.exit(0)


if __name__ == "__main__":
    _entrypoint()
