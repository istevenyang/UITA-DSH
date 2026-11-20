"""``dsh`` 顶层统一命令行入口（随 ``pip install uita`` 自动安装，``uita`` 的 AI 编排超集）。

支持三类调用方式：

1. **DSH 无头 AI 编排与 IR 闭环子命令**::

    dsh -p "点开设置并验证版本" --app sample_web
    dsh -p -y --model default-model --output-format stream-json -- "分析并输出报告"
    dsh run --case-file docs/cases.json --app sample_web --ir-save ./plan.ir.json
    dsh replay --ir report/dsh_runs/<id>/plan.ir.json
    dsh compile --ir plan.ir.json --out cases/sample_web/smoke.tcs --promote
    dsh serve --port 8765
    dsh mcp --transport stdio
    dsh doctor --json
    dsh modes [--category execution] [--json]

2. **直接编排/直达全部 UITA 确定性子命令（无需写 ``--mode``）**::

    dsh runner --cases-dir cases/sample_web/smoke_search.tcs --no-engine
    dsh batch --cases-dir cases/sample_web --tag smoke --priority P0
    dsh perf --app sample_web --perf-preset smoke
    dsh lint --cases-dir cases/sample_web
    dsh health-check --app sample_web
    dsh env-check --json
    dsh install --app sample_web --package-url https://example.com/app.dmg
    dsh probe --app sample_web
    dsh probe-diff --app sample_web --probe-diff-new rosetta_map_probe.json

3. **完全兼容 ``uita --mode <mode>`` 传统参数形态**::

    dsh --mode runner --cases-dir cases/sample_web/smoke_search.tcs --no-engine
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from types import SimpleNamespace
from typing import Optional

from .headless import OUTPUT_FORMATS, doctor_check, run_headless
from .policy import Mode
from .tools.skill import UITA_MODE_CATALOG, collect_uita_capabilities

DSH_SUBCOMMANDS = (
    "run", "replay", "compile", "serve", "mcp", "doctor", "status",
    "modes", "capabilities",
)

#: 便捷子命令别名 → UITA 标准 mode 名
UITA_MODE_ALIASES: dict[str, str] = {
    "tcs": "runner",
    "env-check": "env_check",
    "check": "health-check",
}

ALL_UITA_SUBCOMMANDS = frozenset(UITA_MODE_CATALOG.keys()) | frozenset(UITA_MODE_ALIASES.keys())


def resolve_uita_subcommand(token: str) -> str:
    """若 ``token`` 是可直达的 UITA 子命令（或其别名），返回对应 ``--mode`` 名；否则返回空串。"""
    clean = str(token or "").strip()
    if clean in UITA_MODE_ALIASES:
        return UITA_MODE_ALIASES[clean]
    if clean in UITA_MODE_CATALOG:
        return clean
    return ""


def _dispatch_uita_mode(mode_name: str, mode_argv: list[str]) -> int:
    """在当前进程内直接解析并分发 UITA ``--mode <mode_name>``，保持退出码（0/1/2）一致。"""
    from uita.__main__ import _MODE_HANDLERS, _auto_discover
    from uita.cli.modes.common import (
        StartupError,
        _inject_app_versions,
        _validate_html_report,
    )
    from uita.cli.parser import build_parser

    parser = build_parser()
    try:
        cli = parser.parse_args(["--mode", mode_name, *mode_argv])
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    try:
        _inject_app_versions(cli)
        _auto_discover()
        _validate_html_report(cli)
        handler = _MODE_HANDLERS.get(cli.mode)
        if handler is None:
            print(f"[DSH] 未知 UITA 模式: {cli.mode}", file=sys.stderr)
            return 2
        return int(handler(cli) or 0)
    except StartupError as exc:
        print(f"[DSH] 任务未能正常启动: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"[DSH] 任务执行异常: {exc}", file=sys.stderr)
        return 1


def _looks_like_case_file_path(value: str) -> bool:
    clean = str(value or "").strip()
    if not clean or "\n" in clean or "\r" in clean:
        return False
    if os.path.isfile(clean):
        return True
    lower = clean.lower()
    return lower.endswith((".txt", ".md", ".json"))


def _load_case_or_json(case_text: str, case_file: str,
                       app_name: str = "sample_web") -> tuple[str, str]:
    """返回 ``(用例文本正文, 默认用例名stem)``；``--case-text`` 传文件路径（如 ``xxxx.txt``）时自动读取。"""
    text = str(case_text or "").strip()
    path = str(case_file or "").strip()
    if not path and _looks_like_case_file_path(text):
        path = text
        text = ""
    if text:
        return text, ""
    if not path:
        return "", ""
    stem = os.path.splitext(os.path.basename(path))[0].strip()
    with open(path, encoding="utf-8") as handle:
        raw = handle.read().strip()
    if path.lower().endswith(".json") or raw.startswith("{"):
        from .case_import import convert_json_cases_text

        converted = convert_json_cases_text(raw, app=app_name or "sample_web")
        if converted:
            return converted[0][1], converted[0][0] or stem
    return raw, stem


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dsh",
        description=(
            "DSH（Deterministic Skill Harness）— UITA 顶层无头 AI 编排器与统一命令行入口。\n"
            "既支持 AI 无头编排（run / -p / replay / compile / serve / mcp / doctor / modes），\n"
            "也支持直接调用全部 UITA 子命令（runner / batch / perf / lint / probe / install 等）。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "subcommand_or_prompt",
        nargs="*",
        help=(
            "DSH 子命令（run / replay / compile / serve / mcp / doctor / modes）、"
            "UITA 子命令（runner / batch / perf / lint / health-check / install / probe ...）"
            "或无头自然语言目标"
        ),
    )
    p.add_argument(
        "-p", "--print", "--prompt",
        dest="headless_flag",
        action="store_true",
        default=False,
        help="无头单次执行模式（打印结果后退出，默认免人工审批）",
    )
    p.add_argument(
        "-y", "--yes", "--dsh-auto-approve",
        dest="auto_approve",
        action="store_true",
        default=False,
        help="无头运行免人工审批（无头模式默认开启）",
    )
    p.add_argument("--prompt-file", default="", help="从文件读取无头 prompt")
    p.add_argument("--goal", default="", help="编排目标（自然语言）")
    p.add_argument("--case-text", default="",
                   help="文本用例正文，或直接传 .txt/.md/.json 用例文件路径（如 xxxx.txt）")
    p.add_argument("--case-file", default="", help="文本用例文件（.md/.txt/.json）")
    p.add_argument("--app", default="", help="目标应用名（如 sample_web）")
    p.add_argument("--workdir", "--workspace", dest="workdir", default="",
                   help="受控工作目录（缺省为当前工作区）")
    p.add_argument(
        "--mode", "--dsh-mode",
        dest="dsh_mode",
        choices=["readonly", "supervised", "auto"],
        default="",
        help="权限模式（无头直跑默认 auto；serve 默认 supervised）",
    )
    p.add_argument("--model", "--dsh-model", dest="model", default="",
                   help="模型名（默认读 UITA_DSH_MODEL）")
    p.add_argument("--llm-url", "--dsh-llm-url", dest="llm_url", default="",
                   help="模型 chat/completions 地址（默认读 DSH_API_URL）")
    p.add_argument("--session-id", default="", help="指定会话 ID")
    p.add_argument("--agent", default="", help="加载 .dsh/agents/<name>.md")
    p.add_argument("--skill", default="", help="加载 .dsh/skills/<name>/SKILL.md")
    p.add_argument("--max-steps", "--max-turns", "--dsh-max-steps",
                   dest="max_steps", type=int, default=0,
                   help="最大步数/回合数（默认 30）")
    p.add_argument("--timeout", "--dsh-timeout", dest="timeout_s", type=int, default=0,
                   help="总超时秒数（默认 600）")
    p.add_argument("--stall-timeout", dest="stall_s", type=float, default=None,
                   help="静默看门狗超时秒数（默认 300）")
    p.add_argument("--loop-repeat", dest="loop_repeat", type=int, default=None,
                   help="连续重复工具调用上限（默认 4）")
    p.add_argument("--heartbeat", dest="heartbeat_s", type=float, default=None,
                   help="无头保活心跳间隔秒数（默认 60，0=关闭）")
    p.add_argument("--artifact-path", default="", help="必交产物文件路径（看门狗监控）")
    p.add_argument("--artifact-grace", dest="artifact_grace_s", type=float, default=0.0,
                   help="必交产物宽限期秒数")
    p.add_argument(
        "--output-format", "--dsh-output-format",
        dest="output_format",
        choices=list(OUTPUT_FORMATS),
        default="text",
        help="输出格式：text（默认）/ json / stream-json（NDJSON）",
    )
    p.add_argument("--ir", default="", help="replay/compile：输入 IR 文件路径")
    p.add_argument("--ir-save", "--ir-out", dest="ir_save", default="",
                   help="run/replay：将会话 IR 另存到该路径")
    p.add_argument(
        "--out", "--tcs-out",
        dest="tcs_out",
        nargs="?",
        const="auto",
        default="",
        help="compile/replay/run：编译输出 .tcs 路径（不带路径参数时自动落盘至 cases/<app>/<用例名>.tcs）",
    )
    p.add_argument("--promote", action="store_true", default=False,
                   help="compile/run：同时将 IR 提升入库到 cases/<app>/ir/")
    p.add_argument("--category", default="",
                   help="modes/capabilities：按分类过滤（execution/quality/perf/lifecycle/rosetta/...）")
    p.add_argument("--host", "--dsh-host", dest="host", default="",
                   help="serve/mcp：监听地址（默认 127.0.0.1）")
    p.add_argument("--port", "--dsh-port", dest="port", type=int, default=0,
                   help="serve/mcp：监听端口（默认 8765）")
    p.add_argument("--token", "--dsh-token", dest="token", default="",
                   help="serve：访问 token")
    p.add_argument("--transport", "--dsh-transport", dest="transport",
                   choices=["stdio", "http"], default="stdio",
                   help="mcp：传输方式（stdio / http）")
    p.add_argument("--keep-app", action="store_true", default=False,
                   help="run：无头结束后保留被测应用不关闭")
    p.add_argument("--json", dest="json_flag", action="store_true", default=False,
                   help="doctor/status/modes：以 JSON 输出")
    p.add_argument("--strict", action="store_true", default=False,
                   help="doctor/status：未配置端点或密钥时返回非零退出码")
    p.add_argument("--permission-mode", default="", help=argparse.SUPPRESS)
    p.add_argument("--disallowedTools", default="", help=argparse.SUPPRESS)
    return p


def _ensure_uita_discovered(app_name: str = "") -> None:
    if not app_name:
        return
    try:
        from uita.__main__ import _auto_discover

        _auto_discover()
    except Exception:  # noqa: BLE001
        pass


def _cmd_doctor(args: argparse.Namespace) -> int:
    info = doctor_check()
    if args.json_flag or args.output_format in {"json", "stream-json"}:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    else:
        status = "READY" if info["ok"] else "NOT_CONFIGURED"
        print(f"[DSH Doctor] status={status} tools={info['tools_count']} model={info['model']}")
        print(f"  llm_url: {info['llm_url'] or '(未配置)'}")
        print(f"  api_key: {info['api_key_mask'] if info['api_key_set'] else '(未配置)'}")
        print(f"  workspace: {info['workspace_dir']}")
        if info["issues"]:
            for item in info["issues"]:
                print(f"  [!] {item}")
    if args.strict and not info["ok"]:
        return 1
    return 0


def _cmd_modes(args: argparse.Namespace) -> int:
    caps = collect_uita_capabilities(category=args.category)
    if args.json_flag or args.output_format in {"json", "stream-json"}:
        print(json.dumps(caps, ensure_ascii=False, indent=2))
        return 0
    print(f"[DSH 可编排 UITA 子命令] 共 {caps['modes_count']} 个模式 | "
          f"已注册 App: {', '.join(caps['registered_apps']) or '-'}")
    for mode_name, meta in caps["modes"].items():
        print(f"  dsh {mode_name:<24} [{meta['category']}] {meta['summary']}")
        print(f"      参数: {meta['key_args']}")
    return 0


def _cmd_compile(args: argparse.Namespace) -> int:
    from .compile import compile_ir_file, promote_ir
    from .ir import IRError, load_ir

    if not args.ir:
        print("[DSH] compile 需要 --ir <path>", file=sys.stderr)
        return 2
    try:
        source, out_path = compile_ir_file(args.ir, args.tcs_out)
        if args.promote:
            ir_obj = load_ir(args.ir)
            promoted = promote_ir(ir_obj)
            print(f"[DSH] IR 已提升入库: {promoted}", file=sys.stderr)
    except IRError as exc:
        print(f"[DSH] IR 编译失败: {exc}", file=sys.stderr)
        return 2
    if out_path:
        print(f"[DSH] TCS 已写入: {out_path}")
    else:
        print(source, end="")
    return 0


def _cmd_replay(args: argparse.Namespace) -> int:
    from uita.cli.modes.dsh_mode import _handle_dsh_replay

    _ensure_uita_discovered(args.app)
    cli = SimpleNamespace(
        ir=args.ir,
        ir_save=args.ir_save,
        dsh_mode=args.dsh_mode,
        dsh_auto_approve=True,
        dsh_max_steps=args.max_steps,
        dsh_timeout=args.timeout_s,
    )
    rc = _handle_dsh_replay(cli)
    if rc == 0 and args.tcs_out and args.ir:
        from .compile import compile_ir_file
        from .ir import IRError

        try:
            compile_ir_file(args.ir, args.tcs_out)
            print(f"[DSH] TCS 已编译: {args.tcs_out}")
        except IRError as exc:
            print(f"[DSH] TCS 编译警告: {exc}", file=sys.stderr)
    return rc


def _cmd_serve(args: argparse.Namespace) -> int:
    from uita.cli.modes.dsh_mode import _handle_dsh_serve

    _ensure_uita_discovered(args.app)
    cli = SimpleNamespace(
        dsh_host=args.host,
        dsh_port=args.port,
        dsh_token=args.token,
        dsh_mode=args.dsh_mode,
        dsh_max_steps=args.max_steps,
        dsh_timeout=args.timeout_s,
        dsh_model=args.model,
        dsh_llm_url=args.llm_url,
    )
    return _handle_dsh_serve(cli)


def _cmd_mcp(args: argparse.Namespace) -> int:
    from uita.cli.modes.dsh_mode import _handle_dsh_mcp, install_stdio_protocol_stream

    if args.transport == "stdio":
        install_stdio_protocol_stream()
    _ensure_uita_discovered(args.app)
    cli = SimpleNamespace(
        dsh_transport=args.transport,
        dsh_host=args.host,
        dsh_port=args.port,
        dsh_token=args.token,
        dsh_mode=args.dsh_mode,
        dsh_max_steps=args.max_steps,
        dsh_timeout=args.timeout_s,
        dsh_model=args.model,
        dsh_llm_url=args.llm_url,
    )
    return _handle_dsh_mcp(cli)


def _cmd_run(args: argparse.Namespace, positional_prompt: str = "") -> int:
    from .textcase import parse_text_case

    goal = str(args.goal or positional_prompt or "").strip()
    if not goal and args.prompt_file:
        try:
            with open(args.prompt_file, encoding="utf-8") as handle:
                goal = handle.read().strip()
        except OSError as exc:
            print(f"[DSH] 读取 prompt 文件失败: {exc}", file=sys.stderr)
            return 2

    try:
        case_text, default_case_name = _load_case_or_json(args.case_text, args.case_file, args.app)
    except Exception as exc:  # noqa: BLE001
        print(f"[DSH] 读取用例失败: {exc}", file=sys.stderr)
        return 2

    if not goal and not case_text:
        print("[DSH] 无头运行需要提供目标（-p <prompt> / --goal / --case-file / --case-text）",
              file=sys.stderr)
        return 2

    _ensure_uita_discovered(args.app)
    explicit_mode = str(args.dsh_mode or "").strip()
    mode = Mode.parse(explicit_mode, Mode.AUTO) if explicit_mode else Mode.AUTO
    auto_approve = bool(args.auto_approve) or mode is Mode.AUTO
    if mode is Mode.SUPERVISED and not auto_approve:
        mode = Mode.READONLY

    case_obj = (
        parse_text_case(case_text, default_name=default_case_name)
        if case_text
        else None
    )
    result = run_headless(
        goal=goal,
        case=case_obj,
        app_name=args.app,
        workdir=args.workdir,
        mode=mode,
        auto_approve=auto_approve,
        session_id=args.session_id,
        max_steps=args.max_steps,
        timeout_s=args.timeout_s,
        stall_s=args.stall_s,
        loop_repeat=args.loop_repeat,
        heartbeat_s=args.heartbeat_s,
        artifact_path=args.artifact_path,
        artifact_grace_s=args.artifact_grace_s,
        model=args.model,
        llm_url=args.llm_url,
        skill=args.skill,
        agent=args.agent,
        output_format=args.output_format,
        ir_save=args.ir_save,
        tcs_out=args.tcs_out,
        promote=args.promote,
        keep_app=args.keep_app,
    )
    return 0 if result.returncode == 0 else 1


def _sniff_legacy_uita_mode(raw_argv: list[str]) -> Optional[tuple[str, list[str]]]:
    """兼容 ``dsh --mode <uita_mode> ...``：若传入非 DSH 的传统 mode，拆出并返回。"""
    for idx, token in enumerate(raw_argv):
        if token == "--mode" and idx + 1 < len(raw_argv):
            val = raw_argv[idx + 1]
            if val not in {"readonly", "supervised", "auto"}:
                rest = raw_argv[:idx] + raw_argv[idx + 2:]
                return val, rest
        if token.startswith("--mode="):
            val = token.split("=", 1)[1]
            if val not in {"readonly", "supervised", "auto"}:
                rest = raw_argv[:idx] + raw_argv[idx + 1:]
                return val, rest
    return None


def main(argv: Optional[list[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)

    # 1. 首参数是 UITA 子命令（如 dsh runner / dsh batch / dsh perf / dsh lint ...）→ 原样转交 UITA 模式处理器
    if raw_argv and not raw_argv[0].startswith("-"):
        uita_mode = resolve_uita_subcommand(raw_argv[0])
        if uita_mode:
            code = _dispatch_uita_mode(uita_mode, raw_argv[1:])
            if argv is None:
                sys.exit(code)
            return code

    # 2. 兼容 dsh --mode <uita_mode> ...
    legacy = _sniff_legacy_uita_mode(raw_argv)
    if legacy is not None:
        legacy_mode, rest_argv = legacy
        resolved = resolve_uita_subcommand(legacy_mode) or legacy_mode
        code = _dispatch_uita_mode(resolved, rest_argv)
        if argv is None:
            sys.exit(code)
        return code

    # 3. DSH 原生子命令与无头模式
    parser = _build_parser()
    args = parser.parse_args(raw_argv)

    tokens = list(args.subcommand_or_prompt or [])
    subcmd = ""
    positional_prompt = ""
    if tokens and tokens[0] in DSH_SUBCOMMANDS and not args.headless_flag:
        subcmd = tokens[0]
        positional_prompt = " ".join(tokens[1:]).strip()
    else:
        subcmd = "run"
        positional_prompt = " ".join(tokens).strip()

    if subcmd in {"doctor", "status"}:
        code = _cmd_doctor(args)
    elif subcmd in {"modes", "capabilities"}:
        code = _cmd_modes(args)
    elif subcmd == "compile":
        code = _cmd_compile(args)
    elif subcmd == "replay":
        code = _cmd_replay(args)
    elif subcmd == "serve":
        code = _cmd_serve(args)
    elif subcmd == "mcp":
        code = _cmd_mcp(args)
    else:
        code = _cmd_run(args, positional_prompt=positional_prompt)

    if argv is None:
        sys.exit(code)
    return code
