"""CLI mode 处理器共享工具（从 main.py 拆出，见 R23 main.py 治理）。

不含任何 mode 专属业务逻辑，只提供各 mode handler 复用的：
  - StartupError：任务未能正常启动（对应 exit 1）
  - _apply_main_app_version / _inject_app_versions：--version 注入
  - _expand_cases_dir：--cases-dir 展开
  - _resolve_case_name_tokens：--cases-dir 裸用例名 → 文件路径解析
    （batch/runner 模式共用）
  - _collect_tcs_files：把 token 列表（文件/目录路径）收集为 .tcs 文件列表
  - _resolve_single_tcs_file：--cases-dir 解析出恰好一个 .tcs 文件
    （parse/api-runner 模式专用，--file 已废弃移除后的替代方案）
  - _parse_tag_filter：--tag/--priority 解析
  - _validate_html_report：--html-report 合法性校验
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("uita")


class StartupError(Exception):
    """任务未能正常启动（对应 exit 1）。"""


def _apply_main_app_version(app_name: str, version: str) -> None:
    """把 --version 写入主 app 对应的 env 并更新 AppRegistry 中已注册的 app_version。

    --version 是"主 app 的版本兜底"，只有在 --app-version 未精确声明该 app 时才写入。
    由于 _auto_discover() 已执行完毕，AppPlatformConfig 已注册；此处直接更新已注册对象的
    app_version 字段即可，不需要重新注册。
    计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md §4.6
    """
    if not version or not version.strip():
        return
    env_key = f"UITA_{app_name.upper()}_APP_VERSION"
    if os.environ.get(env_key):
        # --app-version 已精确指定，--version 不覆盖
        return
    os.environ[env_key] = version.strip()
    logger.debug("[version] %s=%s（来自 --version 主 app 兜底）", env_key, version.strip())
    # 同步更新已注册的 AppPlatformConfig（_auto_discover 已执行）
    try:
        from uita.core.platform.app_registry import AppRegistry
        from uita.core.registry_common import resolve_universal_platform, UNIVERSAL_PLATFORM
        platform = resolve_universal_platform(UNIVERSAL_PLATFORM)
        cfg = AppRegistry.get(app_name, platform)
        cfg.app_version = version.strip()
    except Exception:
        pass  # 注册未找到时静默忽略，_build_resolver 依赖 os.environ 兜底


def _expand_cases_dir(cases_dir: list[str]) -> list[str]:
    """展开 --cases-dir：支持逗号/中文逗号分隔。"""
    tokens: list[str] = []
    for item in (cases_dir or ["cases"]):
        for token in str(item).replace("\uff0c", ",").split(","):
            token = token.strip()
            if token:
                tokens.append(token)
    return tokens


def _looks_like_path(token: str) -> bool:
    """判断 token 是否"看起来像路径"（含路径分隔符或 .tcs 后缀）。

    用于跳过裸用例名解析：真实存在的目录/文件在 `_resolve_case_name_tokens()`
    中已通过 os.path.isdir/isfile 命中，这里只兜底"路径不存在但形似路径"
    的场景（如拼写错误），避免被误当成用例名再去做全量扫描。
    """
    return ("/" in token) or ("\\" in token) or token.endswith(".tcs")


def _find_case_file_by_name(name: str, search_root: str) -> str:
    """在 search_root 下查找用例：优先 <name>.tcs 文件名匹配，
    未命中再解析 TCS 头 name 字段匹配。

    Returns:
        绝对路径；未找到返回空串。
    """
    if not os.path.isdir(search_root):
        return ""

    target_fname = f"{name}.tcs"
    for root, _dirnames, files in os.walk(search_root):
        if target_fname in files:
            return os.path.abspath(os.path.join(root, target_fname))

    from uita.core.dsl.parser import parse_file
    for root, _dirnames, files in os.walk(search_root):
        for f in sorted(files):
            if not f.endswith(".tcs"):
                continue
            fpath = os.path.join(root, f)
            try:
                case = parse_file(fpath)
            except Exception:
                continue
            if case.name == name:
                return os.path.abspath(fpath)
    return ""


def _resolve_case_name_tokens(tokens: list[str], app: str) -> list[str]:
    """--cases-dir 增强：token 除已支持的目录/.tcs 文件路径外，还支持裸用例名
    （如 "hkgasruguer"），按 "<name>.tcs 文件名 → TCS 头 name 字段" 两级在指定
    范围内匹配为具体文件路径（batch/runner 模式共用）。

    查找范围：--app 指定时限定为 cases/<app>/（避免全量扫描误命中同名用例），
    未指定 --app 时退回默认 "cases" 全量扫描（兼容既有行为，零回归）。

    未匹配到的裸用例名会被丢弃并 warning（而非静默传给下游收集逻辑产生
    0 条结果），已存在的目录/文件路径原样保留。
    """
    from uita.config import ROOT_DIR

    search_root = os.path.join(ROOT_DIR, "cases", app) if app else os.path.join(ROOT_DIR, "cases")

    resolved: list[str] = []
    unresolved: list[str] = []
    for token in tokens:
        if os.path.isdir(token) or os.path.isfile(token):
            resolved.append(token)
            continue
        if _looks_like_path(token):
            # 形似路径但不存在，保留原值交由既有逻辑处理（沿用既有的"未找到"日志行为）
            resolved.append(token)
            continue
        # 裸用例名：按文件名 / TCS 头 name 字段匹配
        match = _find_case_file_by_name(token, search_root)
        if match:
            logger.info("用例名 %s 已解析为: %s", token, match)
            resolved.append(match)
        else:
            unresolved.append(token)

    if unresolved:
        logger.warning(
            "--cases-dir 以下用例名在 %s 下未匹配到任何 .tcs 文件"
            "（文件名/头 name 字段均未命中），已忽略: %s",
            search_root, ", ".join(unresolved),
        )
    return resolved


def _collect_tcs_files(tokens: list[str]) -> list[str]:
    """把 token 列表（文件/目录路径，通常是 `_resolve_case_name_tokens()` 的
    输出）收集为 .tcs 文件路径列表。

    相对路径按 cwd → ROOT_DIR 顺序探测存在性；目录递归扫描并按文件名排序；
    不存在的路径 warning 并跳过（不中断整体收集）。
    """
    from uita.config import ROOT_DIR

    cwd = os.getcwd()
    files: list[str] = []
    for raw in tokens:
        if os.path.isabs(raw):
            resolved = raw
        else:
            cwd_c = os.path.normpath(os.path.join(cwd, raw))
            root_c = os.path.normpath(os.path.join(ROOT_DIR, raw))
            resolved = cwd_c if os.path.exists(cwd_c) else (root_c if os.path.exists(root_c) else cwd_c)

        if os.path.isfile(resolved) and resolved.endswith(".tcs"):
            files.append(resolved)
        elif os.path.isdir(resolved):
            for root, _dirnames, fnames in os.walk(resolved):
                for fname in sorted(fnames):
                    if fname.endswith(".tcs"):
                        files.append(os.path.join(root, fname))
        else:
            logger.warning("路径不存在: %s", resolved)
    return files


def _resolve_single_tcs_file(cli, mode_name: str) -> str:
    """从 --cases-dir/--case-dir 解析出恰好一个 .tcs 文件路径。

    parse / api-runner 模式专用（--file 已废弃并从 parser 中移除）：这两个模式
    语义上只处理单个文件，不像 runner/batch 支持多文件顺序执行，因此这里对
    "解析出 0 个"或"解析出多个"都视为参数错误，直接 StartupError（exit 1）。

    未显式传入 --cases-dir 时（即仍是 parser 默认值 ["cases"]）视为"未指定文件"，
    避免误当作要处理整个 cases/ 目录。
    """
    raw = list(getattr(cli, "cases_dir", None) or ["cases"])
    if raw == ["cases"]:
        raise StartupError(f"{mode_name} 模式需要 --cases-dir/--case-dir 指定单个 .tcs 文件路径")

    app = (getattr(cli, "app", "") or "").strip()
    tokens = _resolve_case_name_tokens(_expand_cases_dir(raw), app)
    files = _collect_tcs_files(tokens)

    if not files:
        raise StartupError(f"{mode_name} 模式未找到 .tcs 文件: {raw}")
    if len(files) > 1:
        preview = ", ".join(files[:5]) + ("..." if len(files) > 5 else "")
        raise StartupError(
            f"{mode_name} 模式只能指定单个 .tcs 文件，但 --cases-dir 解析出 "
            f"{len(files)} 个: {preview}"
        )
    return files[0]


def _parse_tag_filter(tag_str: str | None) -> tuple[list[str], list[str]]:
    """解析 --tag 字符串，返回 (正选列表, 反选列表)。

    拆分规则：半角逗号 , 和中文逗号 ，
    反选判断：token 以 ! 或 not: 开头
    """
    if not tag_str or not tag_str.strip():
        return [], []
    include: list[str] = []
    exclude: list[str] = []
    for token in tag_str.replace("\uff0c", ",").split(","):
        t = token.strip().lower()
        if not t:
            continue
        if t.startswith("not:"):
            exclude.append(t[4:])
        elif t.startswith("!"):
            exclude.append(t[1:])
        else:
            include.append(t)
    return include, exclude


def _inject_app_versions(cli) -> None:
    """把 --version 写入 os.environ，供 register.py 读取。

    支持两种格式：
      --version 2.1.3           → 主 app 版本（handler 推断 app_name 后写 env）
      --version sample_web=2.1.3 → 精确指定某 app 版本（立即写 env）

    必须在 _auto_discover() 之前调用。
    """
    version_str = (getattr(cli, "version", "") or "").strip()
    if not version_str:
        return

    # 格式 APP=X.Y.Z → 立即写入对应环境变量
    if "=" in version_str:
        for item in version_str.replace("\uff0c", ",").split(","):
            item = item.strip()
            if "=" in item:
                app, ver = item.split("=", 1)
                env_key = f"UITA_{app.strip().upper()}_APP_VERSION"
                os.environ[env_key] = ver.strip()
                logger.debug("[version] %s=%s（来自 --version APP=X.Y.Z）", env_key, ver.strip())
    # 格式 X.Y.Z → 留给 handler 推断 app_name 后再写（_apply_main_app_version）
    # cli.version 已由 argparse 提供，handler 通过 _apply_main_app_version() 消费


def _validate_html_report(cli: argparse.Namespace) -> None:
    """校验 --html-report 合法性，不合法时清空忽略。

    接受 http:// 或 https:// 前缀的报告地址。
    """
    html_report = getattr(cli, "html_report", "") or ""
    if not html_report:
        return
    html_report = html_report.strip()
    if not (html_report.startswith("https://") or html_report.startswith("http://")):
        logger.warning(
            "--html-report 不合法（需以 http:// 或 https:// 开头），"
            "已忽略: %s", html_report,
        )
        cli.html_report = ""
    else:
        cli.html_report = html_report
