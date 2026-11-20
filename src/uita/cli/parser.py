"""UITA-DSH CLI 解析器 — --mode 统一入口风格。"""
from __future__ import annotations

import argparse

_TRUE_WORDS = {"true", "yes", "y", "1", "on"}
_FALSE_WORDS = {"false", "no", "n", "0", "off"}


def _str2bool(raw: str) -> bool:
    text = str(raw or "").strip().lower()
    if text in _TRUE_WORDS:
        return True
    if text in _FALSE_WORDS:
        return False
    raise argparse.ArgumentTypeError(
        f"需要布尔值（true/false/yes/no/1/0/on/off），收到: {raw!r}",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="uita",
        description="UITA-DSH — Universal Intelligent Test Architecture With Deterministic Skill Harness & Cross-Platform UI Automation Runtime",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # Parse：解析 TCS 打印 AST
  uita --mode parse --cases-dir cases/sample_web/smoke_search.tcs

  # Runner：单文件/多文件执行
  uita --mode runner --cases-dir cases/sample_web/smoke_search.tcs
  uita --mode runner --cases-dir cases/sample_web/smoke_search.tcs --no-engine

  # Batch：目录批量 + tag/priority 过滤
  uita --mode batch --cases-dir cases/sample_web --tag smoke --priority P0

  # DSH：无头执行 / 控制台 / IR 重放 / MCP
  uita --mode dsh-run --goal "打开首页并检查搜索框" --app sample_web
  uita --mode dsh-serve --dsh-port 8765
  uita --mode dsh-replay --ir report/dsh_runs/<id>/plan.ir.json
  uita --mode dsh-mcp --dsh-transport stdio
""",
    )

    p.add_argument(
        "--mode", required=True,
        choices=[
            "parse", "runner", "perf", "api-runner", "batch",
            "probe", "probe-diff", "rosetta-fp",
            "install", "lint", "pretest", "probe-provision",
            "env_check", "health-check",
            "dsh-serve", "dsh-run", "dsh-mcp", "dsh-replay",
        ],
        help="执行模式",
    )

    p.add_argument(
        "--cases-dir", "--case-dir", dest="cases_dir", nargs="+", default=["cases"],
        metavar="DIR_OR_FILE",
        help="用例目录或 .tcs 文件（支持逗号分隔 + 多次传参）",
    )

    p.add_argument(
        "--lint-target", choices=["tcs", "rosetta"], default="tcs",
        help="lint 模式检查对象：tcs（默认）或 rosetta",
    )

    p.add_argument("--tag", default=None, metavar="TAG[,TAG...]", help="用例 tag 过滤")
    p.add_argument("--priority", default=None, metavar="P0|P1|P2|P3", help="用例 priority 过滤")
    p.add_argument("--random", action="store_true", default=False, help="随机选取模式")
    p.add_argument("--no-notify", action="store_true", default=False, help="跳过通知")
    p.add_argument("--no-engine", action="store_true", default=False, help="dry-run 模式（仅 runner）")

    # perf 参数
    p.add_argument("--perf-repeat", type=int, default=None, metavar="N", help="同一用例重复轮次")
    p.add_argument("--perf-sample-interval-ms", type=int, default=None, metavar="MS", help="资源采样间隔")
    p.add_argument("--perf-cold-start", dest="perf_cold_start", action="store_true", default=True)
    p.add_argument("--no-perf-cold-start", dest="perf_cold_start", action="store_false")
    p.add_argument("--perf-warmup", dest="perf_warmup", action="store_true", default=None)
    p.add_argument("--no-perf-warmup", dest="perf_warmup", action="store_false")
    p.add_argument("--use-cache", "--perf-use-cache", dest="use_cache", type=_str2bool, default=False)
    p.add_argument("--load-mode", "--perf-load-mode", dest="load_mode", default="all")
    p.add_argument("--perf-baseline", default="", metavar="FILE")
    p.add_argument("--perf-tier", default="", metavar="SIZE")
    p.add_argument("--perf-src", default="", metavar="DIR")
    p.add_argument("--release-res", default="", metavar="LETTER")
    p.add_argument("--perf-keep-lib", action="store_true")
    p.add_argument("--perf-baseline-tag", default="", metavar="TAG")
    p.add_argument("--perf-metrics", default="", metavar="KEY[,KEY...]")
    p.add_argument("--perf-keep-screenshot", action="store_true", default=False)
    p.add_argument("--perf-keep-app", action="store_true", default=False)
    p.add_argument("--perf-preset", default="", metavar="NAME")
    p.add_argument("--perf-lib", default="", metavar="DIR")
    p.add_argument("--perf-scenario", default="", metavar="NAME[,NAME...]")
    p.add_argument("--perf-scenario-opt", default="", metavar="K=V[,K=V...]")
    p.add_argument("--perf-steady-state-s", type=float, default=0.0, metavar="SEC")
    p.add_argument("--perf-process-name", default="", metavar="NAME")
    p.add_argument("--perf-user-data-dir", default="", metavar="DIR")
    p.add_argument("--perf-ai-analysis", dest="perf_ai_analysis", action="store_true", default=False)
    p.add_argument("--perf-ai-model", default="", metavar="MODEL")
    p.add_argument("--perf-ai-timeout-s", dest="perf_ai_timeout_s", type=int, default=0, metavar="SEC")
    p.add_argument("--perf-ai-compare-run", dest="perf_ai_compare_run", default="auto")
    p.add_argument("--perf-ai-run-dir", dest="perf_ai_run_dir", default="")

    # install / probe 参数
    p.add_argument("--package-url", default="", nargs="?", const="")
    p.add_argument("--install-report", default="", metavar="PATH")
    p.add_argument("--probe-report", default="", metavar="PATH")
    p.add_argument("--uninstall", action="store_true", default=False)
    p.add_argument("--html-report", default="")
    p.add_argument("--workspace", default="", metavar="DIR", help="业务资产根目录")
    p.add_argument("--app", default=None, help="目标应用名称（如 sample_web）")
    p.add_argument("--version", default="", metavar="[APP=]X.Y.Z", help="被测 App 版本号")
    p.add_argument("--app-version", dest="app_versions", action="append", default=[], metavar="APP=X.Y.Z")
    p.add_argument("--hooks", nargs="*", default=None, help="Hook 列表")

    # probe / probe-diff / rosetta-fp 参数
    p.add_argument("--probe-output", default="rosetta_map_probe.json")
    p.add_argument("--probe-page", default="未知页面")
    p.add_argument("--probe-diff-new", default="", metavar="FILE")
    p.add_argument("--probe-diff-old", default="", metavar="FILE")
    p.add_argument("--probe-diff-format", choices=["text", "json"], default="text")
    p.add_argument("--probe-diff-output", default="", metavar="FILE")
    p.add_argument("--apply", dest="probe_diff_apply", action="store_true", default=False)
    p.add_argument("--fp-mode", choices=["encode", "decode", "match"], default=None)
    p.add_argument("--pattern", default="")
    p.add_argument("--element", default="")
    p.add_argument("--platform", default="", choices=["mac", "win", "web", ""])
    p.add_argument("--screenshot", default="")
    p.add_argument("--threshold", type=float, default=0.80)
    p.add_argument("--rosetta-path", default="")
    p.add_argument("--fp-output", default="")
    p.add_argument("--json", action="store_true", default=False)

    # DSH 参数
    p.add_argument("--goal", default="", help="dsh-run：单次自然语言目标")
    p.add_argument("--case-text", default="", help="dsh-run：文本用例正文或文件路径")
    p.add_argument("--case-file", default="", help="dsh-run：文本用例文件路径（.md/.txt/.json）")
    p.add_argument("--ir", default="", help="dsh-replay：步骤 IR 文件路径（uita.ir/1 JSON）")
    p.add_argument("--ir-save", "--ir-out", dest="ir_save", default="", help="dsh-run/dsh-replay：将会话 IR 另存到该路径")
    p.add_argument("--out", "--tcs-out", dest="tcs_out", nargs="?", const="auto", default="", help="dsh-run/dsh-replay：自动编译输出 .tcs 路径")
    p.add_argument("--promote", action="store_true", default=False, help="dsh-run：同时将 IR 提升入库到 cases/<app>/ir/")
    p.add_argument("--dsh-host", default="", help="dsh-serve/dsh-mcp：监听地址（仅允许 127.0.0.1/localhost）")
    p.add_argument("--dsh-port", type=int, default=0, help="dsh-serve/dsh-mcp：监听端口（默认 8765）")
    p.add_argument("--dsh-token", default="", help="dsh-serve：访问 token")
    p.add_argument("--dsh-mode", choices=["readonly", "supervised", "auto", ""], default="", help="DSH 权限模式")
    p.add_argument("--dsh-auto-approve", action="store_true", default=False, help="dsh-run：无头自动批准动作")
    p.add_argument("--dsh-max-steps", type=int, default=0, help="DSH 单会话最大步数（默认 30）")
    p.add_argument("--dsh-timeout", type=int, default=0, help="DSH 单会话超时秒数（默认 600）")
    p.add_argument("--dsh-model", default="", help="DSH 模型名覆盖")
    p.add_argument("--dsh-llm-url", default="", help="DSH 模型端点覆盖")
    p.add_argument("--dsh-output-format", choices=["text", "json", "stream-json"], default="text", help="dsh-run 输出格式")
    p.add_argument("--dsh-transport", choices=["stdio", "http"], default="stdio", help="dsh-mcp 传输方式")

    return p
