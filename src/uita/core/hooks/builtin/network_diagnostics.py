"""
network_diagnostics — Playwright 网络诊断信息提取 + curl/log 文件打包。

背景：Playwright Web 测试中"页面响应失败"常见根因是接口报错。本模块把
`PlaywrightEngine.get_recent_network_errors()` 捕获到的失败响应/请求失败记录
转换为：
  1. 追加进当前步骤错误原因的简要摘要文本（`build_network_diagnostics`）
  2. 每个出错接口一对文件（`{endpoint}.curl` + `{endpoint}.log`），打包为
     zip 并通过 `ResultCollector.add_artifact()` 注册，复用现有 报告平台 上传
     链路（`report_portal CLI` 读取 `result.json.artifacts` 自行上传，本模块不新增
     任何上传代码）。

风险决策（2026-08-19，用户明确）：本功能仅用于 UITA 内部测试系统所测的测试
环境，测试环境无真实生产数据，不存在敏感信息泄露风险，故默认**不脱敏**，
原样打包请求 header/payload/响应内容。保留 `redact` 参数位（默认 False）
供后续场景变化时低成本开启，不强制使用。

2026-08-19 补充二（三态分级 + 精确业务规则，见计划文档 §七）：
  - "疑似后端异常"：4xx/5xx/请求失败，或 HTTP 200 但 body 命中 app 级精确
    业务错误规则（`network_business_error_rules`，见 `_matches_business_error_rule`）
  - "疑似前端脚本异常"：网络层无异常，但检测到 console error / 未捕获
    pageerror（`get_recent_frontend_errors()`）
  - 两者均未检测到：不生成任何附件，摘要如实说明"未检测到已知信号"
  三态互斥判定，避免"网络全部正常、纯前端问题"场景下打包无关的全量响应。

见计划文档：
docs/plans/archive/04-report_portal-report-实施纪要.md
"""
from __future__ import annotations

import json
import logging
import os
import re
import zipfile
from typing import Any, Optional
from urllib.parse import urlsplit

logger = logging.getLogger("hooks.network_diagnostics")

# 摘要文本中每条记录展示的 body 预览长度（比引擎侧 2048 字符截断更短，避免
# 错误原因文本本身过于臃肿；完整内容仍会写入 .log 文件）。
_SUMMARY_BODY_PREVIEW = 300
# 摘要中最多展示的记录条数，避免同一 step 命中大量失败请求时刷屏。
_SUMMARY_MAX_RECORDS = 5

_REDACT_HEADER_KEYS = {"authorization", "cookie", "set-cookie", "x-api-key", "token"}
_REDACTED_PLACEHOLDER = "***REDACTED***"

_FS_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_ENDPOINT_FILENAME_LEN = 120


def _redact_headers(headers: dict, redact: bool) -> dict:
    if not redact or not headers:
        return dict(headers or {})
    return {
        k: (_REDACTED_PLACEHOLDER if k.lower() in _REDACT_HEADER_KEYS else v)
        for k, v in headers.items()
    }


def _endpoint_filename(url: str, fallback: str = "root") -> str:
    """从 URL 提取 path（不含 query string），把 '/' 替换为 '_'，其余文件系统
    不安全字符（?&=: 空格等）保守替换为 '_'，避免不同平台/压缩工具解压异常。

    示例：
        https://api.x.com/api/v1/case-reports/search?foo=bar
        → api_v1_case-reports_search
        https://api.x.com/  （无 path）
        → root（回退值）

    超长 path 截断至 `_MAX_ENDPOINT_FILENAME_LEN` 字符，防止部分文件系统对
    单文件名长度的限制导致写入失败。
    """
    try:
        path = urlsplit(url).path if url else ""
    except Exception:
        path = ""
    path = str(path or "").strip("/")
    if not path:
        return fallback
    name = path.replace("/", "_")
    name = _FS_UNSAFE_CHARS.sub("_", name)
    name = name.strip("_") or fallback
    return name[:_MAX_ENDPOINT_FILENAME_LEN]


def _dedupe_filename(base_name: str, used_names: set[str]) -> str:
    """同一 step 内多次命中同一 endpoint（如重试）时，追加 _2/_3... 后缀，
    避免 zip 内文件名互相覆盖。"""
    if base_name not in used_names:
        used_names.add(base_name)
        return base_name
    n = 2
    while f"{base_name}_{n}" in used_names:
        n += 1
    candidate = f"{base_name}_{n}"
    used_names.add(candidate)
    return candidate


def format_as_curl(record: dict, redact: bool = False) -> str:
    """把单条网络记录格式化为可读的 curl 命令文本。

    默认原样含真实 headers/body（2026-08-19 用户风险决策：内部测试环境，
    无真实生产数据，不脱敏）；`redact=True` 时对常见敏感 header 做替换。
    仅用于人工/AI 诊断阅读，不会被 subprocess 执行（不涉及 R26 shell 注入
    风险——本函数只拼字符串，不调用任何 shell）。

    多行续行符规则：每一行末尾追加 ``\\`` 续行，唯独最后一行不加——先按
    「URL 行 + 各 header 行 + （可选）body 行」组好完整行列表，再统一处理
    续行符，避免 URL 行在有 header/body 跟随时遗漏续行符导致命令语法不完整。
    """
    method = record.get("method", "GET")
    url = record.get("url", "")
    headers = _redact_headers(record.get("request_headers") or {}, redact)
    body = record.get("request_body") or ""

    parts = [f"curl -X {method}", f"  '{url}'"]
    for key, value in headers.items():
        parts.append(f"  -H '{key}: {value}'")
    if body:
        escaped_body = body.replace("'", "'\\''")
        parts.append(f"  --data-raw '{escaped_body}'")

    lines = [f"{part} \\" for part in parts[:-1]] + [parts[-1]]
    return "\n".join(lines)


def format_as_log_text(record: dict, redact: bool = False) -> str:
    """把单条网络记录格式化为「请求 header+payload / 响应 header+body」的
    结构化可读文本，供 .log 文件使用。"""
    method = record.get("method", "GET")
    url = record.get("url", "")
    status = record.get("status")
    status_text = record.get("status_text", "")
    failure_text = record.get("failure_text", "")
    request_headers = _redact_headers(record.get("request_headers") or {}, redact)
    response_headers = _redact_headers(record.get("response_headers") or {}, redact)

    lines = [
        "=== Request ===",
        f"{method} {url}",
        "",
        "-- Headers --",
    ]
    for key, value in request_headers.items():
        lines.append(f"{key}: {value}")
    lines.append("")
    lines.append("-- Payload --")
    lines.append(record.get("request_body") or "(empty)")
    lines.append("")
    lines.append("=== Response ===")
    if status is not None:
        lines.append(f"HTTP {status} {status_text}".rstrip())
        lines.append("")
        lines.append("-- Headers --")
        for key, value in response_headers.items():
            lines.append(f"{key}: {value}")
        lines.append("")
        lines.append("-- Body --")
        lines.append(record.get("response_body") or "(empty)")
    else:
        lines.append(f"(请求失败，无 HTTP 响应) failure={failure_text or '(unknown)'}")
    return "\n".join(lines)


def format_frontend_errors_text(records: list[dict]) -> str:
    """把前端 JS 异常记录（console error / pageerror）格式化为单份可读文本，
    供 zip 内 `frontend_errors.log` 使用（无 HTTP 请求上下文，不生成 curl）。"""
    if not records:
        return ""
    lines: list[str] = []
    for record in records:
        kind = record.get("kind", "console")
        url = record.get("url") or "(no page url)"
        lines.append(f"[{kind}] {url}")
        lines.append(record.get("text") or "(empty)")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _get_by_path(response_body: str, path: str) -> tuple[bool, Any]:
    """从 JSON 响应 body 按 '.' 分隔路径取嵌套字段值（仅支持 dict 嵌套，不
    支持数组下标）。

    Returns:
        (found, value)：body 非合法 JSON、根节点非 dict、或路径任一段不存在/
        中途遇到非 dict 均返回 (False, None)——安全降级，不抛异常，不误判。
    """
    if not path:
        return False, None
    try:
        data = json.loads(response_body) if response_body else None
    except (ValueError, TypeError):
        return False, None
    if not isinstance(data, dict):
        return False, None
    node: Any = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def _matches_business_error_rule(record: dict, rule: dict) -> bool:
    """单条网络记录是否命中单条业务错误规则（见模块头 §7.3 schema）。"""
    url_contains = rule.get("url_contains") or ""
    if url_contains and url_contains not in (record.get("url") or ""):
        return False
    path = rule.get("path") or ""
    found, value = _get_by_path(record.get("response_body") or "", path)
    if not found:
        return False
    if "not_in" in rule:
        return value not in rule["not_in"]
    if "equals" in rule:
        return value == rule["equals"]
    return False


def select_business_error_records(records: list[dict], rules: list[dict]) -> list[dict]:
    """从全量响应记录（含 2xx）中筛出命中任一业务错误规则的记录。

    规则/记录本身格式异常时单条降级跳过（记录 debug 日志），不影响其余记录
    的判定，也不影响用例主流程。
    """
    if not rules or not records:
        return []
    hits: list[dict] = []
    for record in records:
        for rule in rules:
            try:
                if _matches_business_error_rule(record, rule):
                    hits.append(record)
                    break
            except Exception as exc:
                logger.debug(
                    "[network_diagnostics] 业务错误规则匹配异常（忽略）: %s", exc
                )
    return hits


def build_network_diagnostics(
    engine: Any,
    since_ts: Optional[float] = None,
    app_name: str = "",
    platform: str = "",
) -> tuple[str, list[dict], list[dict]]:
    """从 engine 提取网络/前端信号，做三态分级判定，返回
    (摘要文本, 网络记录列表, 前端异常记录列表)。

    engine 不支持 `get_recent_network_errors`（非 Playwright 引擎，或未来
    其他不支持网络监听的引擎）时优雅降级，返回 ("", [], [])，不抛异常。

    Args:
        engine: 当前活跃引擎实例（`hook_ctx.engine`）。
        since_ts: 仅统计该时间戳（`time.time()` 量纲）之后发生的记录，用于
            按当前 step 的时间窗口关联，避免把历史 step 的失败请求也算进来。
        app_name/platform: 用于按 R16b 读取 app 级 `network_business_error_rules`
            精确判定规则（`get_extra_config`）；留空时不启用精确判定（仅
            4xx/5xx/请求失败信号）。

    Returns:
        (summary, network_records, frontend_records)：
        - 判定为"疑似后端异常"时 network_records 非空、frontend_records 为 []；
        - 判定为"疑似前端脚本异常"时 frontend_records 非空、network_records 为 []；
        - 均未检测到已知信号时三者分别为 ""/[]/[]，调用方不应生成任何附件。
        三态互斥，不会同时非空。
    """
    get_errors = getattr(engine, "get_recent_network_errors", None)
    if get_errors is None:
        return "", [], []

    try:
        network_errors = get_errors(since_ts=since_ts) or []
    except Exception as exc:
        logger.debug("[network_diagnostics] get_recent_network_errors 调用异常（忽略）: %s", exc)
        network_errors = []

    get_all_responses = getattr(engine, "get_recent_responses", None)
    try:
        all_responses = get_all_responses(since_ts=since_ts) if get_all_responses else []
    except Exception as exc:
        logger.debug("[network_diagnostics] get_recent_responses 调用异常（忽略）: %s", exc)
        all_responses = []

    business_error_records: list[dict] = []
    if app_name and platform and all_responses:
        from uita.core.platform.app_registry import get_extra_config
        rules = get_extra_config(app_name, platform, "network_business_error_rules", default=[])
        business_error_records = select_business_error_records(all_responses, rules)

    # 合并信号①（4xx/5xx/请求失败）与命中业务规则的 2xx 记录，去重
    # （同一条记录理论上不会同时出现在两个来源，用 id() 兜底防重复）
    combined: list[dict] = list(network_errors)
    seen_ids = {id(r) for r in combined}
    for record in business_error_records:
        if id(record) not in seen_ids:
            combined.append(record)
            seen_ids.add(id(record))

    if combined:
        lines = [f"检测到 {len(combined)} 条网络请求异常（step 执行期间）："]
        for record in combined[:_SUMMARY_MAX_RECORDS]:
            method = record.get("method", "GET")
            url = record.get("url", "")
            status = record.get("status")
            if status is not None:
                status_text = record.get("status_text", "")
                head = f"- [{status} {status_text}]".rstrip() + f" {method} {url}"
            else:
                failure_text = record.get("failure_text") or "unknown"
                head = f"- [请求失败: {failure_text}] {method} {url}"
            lines.append(head)
            body_preview = (record.get("response_body") or "")[:_SUMMARY_BODY_PREVIEW]
            if body_preview:
                lines.append(f"  响应片段: {body_preview}")
        remaining = len(combined) - _SUMMARY_MAX_RECORDS
        if remaining > 0:
            lines.append(f"  ...（另有 {remaining} 条，完整内容见附件 network_log）")
        return "\n".join(lines), combined, []

    get_frontend = getattr(engine, "get_recent_frontend_errors", None)
    try:
        frontend_errors = get_frontend(since_ts=since_ts) if get_frontend else []
    except Exception as exc:
        logger.debug("[network_diagnostics] get_recent_frontend_errors 调用异常（忽略）: %s", exc)
        frontend_errors = []

    if frontend_errors:
        lines = [
            f"未检测到网络层异常，但发现 {len(frontend_errors)} 条前端脚本异常"
            "（console error / 未捕获异常），疑似前端展示/逻辑问题："
        ]
        for record in frontend_errors[:_SUMMARY_MAX_RECORDS]:
            preview = (record.get("text") or "")[:_SUMMARY_BODY_PREVIEW]
            lines.append(f"- [{record.get('kind', 'console')}] {preview}")
        remaining = len(frontend_errors) - _SUMMARY_MAX_RECORDS
        if remaining > 0:
            lines.append(f"  ...（另有 {remaining} 条，完整内容见附件 network_log）")
        return "\n".join(lines), [], frontend_errors

    return "", [], []


def write_network_diagnostics_artifact(
    records: list[dict],
    result_dir: str,
    step_index: int,
    redact: bool = False,
    frontend_records: Optional[list[dict]] = None,
) -> Optional[str]:
    """按每个出错接口生成一对 `{endpoint}.curl`/`{endpoint}.log` 文件，
    `frontend_records` 非空时额外追加一份 `frontend_errors.log`（前端信号
    无 HTTP 请求上下文，不生成 curl），打包为
    `network_logs/network_log_step{N}.zip`。

    Args:
        records: `build_network_diagnostics` 返回的网络记录列表（可为空，
            此时若 frontend_records 非空仍会生成仅含 frontend_errors.log 的 zip）。
        result_dir: 当前用例结果目录（绝对路径，即 `ResultCollector.result_dir`）。
        step_index: 当前步骤编号（0-based），用于文件命名区分不同 step。
        redact: 是否对敏感 header 脱敏，默认 False（见模块头风险决策）。
        frontend_records: 前端 JS 异常记录列表，可选。

    Returns:
        相对 `result_dir` 的 zip 路径（用于 `collector.add_artifact()`），
        两个入参均为空、或写入失败（如目录不可写）时返回 None，不阻断主流程。
    """
    if not records and not frontend_records:
        return None
    try:
        network_logs_dir = os.path.join(result_dir, "network_logs")
        os.makedirs(network_logs_dir, exist_ok=True)
        zip_filename = f"network_log_step{step_index}.zip"
        zip_path = os.path.join(network_logs_dir, zip_filename)

        used_names: set[str] = set()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for record in records:
                base_name = _dedupe_filename(
                    _endpoint_filename(record.get("url", "")), used_names,
                )
                zf.writestr(f"{base_name}.curl", format_as_curl(record, redact))
                zf.writestr(f"{base_name}.log", format_as_log_text(record, redact))
            if frontend_records:
                zf.writestr("frontend_errors.log", format_frontend_errors_text(frontend_records))

        return os.path.relpath(zip_path, result_dir).replace("\\", "/")
    except Exception as exc:
        logger.warning(
            "[network_diagnostics] 写入 network_log_step%d.zip 失败（忽略）: %s",
            step_index, exc,
        )
        return None
