"""IR 编译与入库：将 ``uita.ir/1`` 编译为 ``.tcs`` 或提升为 ``cases/<app>/ir/*.ir.json``。

设计约束：

- 遵守 R4：``@setup`` 只允许标在首步，``@teardown`` 只允许标在末步；
- 遵守 R11：拒绝导出含 ``target.kind == "coord"`` 的坐标步骤，禁止裸坐标污染正式用例库；
- 编译产物自动经 ``uita.core.dsl.parser.parse_source`` 语法校验，非法直接抛 :class:`IRError`；
- ``promote_ir`` 强制校验目标路径位于 ``CASES_DIR/<app>/ir/`` 内，同名冲突自动加修订后缀或覆盖。
"""
from __future__ import annotations

import os
import re
import tempfile
from typing import Any

from .ir import IR, IRError, IRStep, save_ir

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_\u4e00-\u9fff-]+")


def _safe_identifier(raw: str, fallback: str = "dsh_case") -> str:
    cleaned = _SAFE_NAME_RE.sub("_", str(raw or "").strip()).strip("_")
    return cleaned[:80] or fallback


def _format_expect(exp: Any) -> str:
    target = exp.element
    if exp.kind == "absent":
        return f"assert[{target}, absent=true]"
    if exp.kind == "text" and exp.value not in (None, ""):
        escaped = str(exp.value).replace('"', '\\"')
        return f'assert[{target}, contains="{escaped}"]'
    if exp.kind == "value" and exp.value not in (None, ""):
        escaped = str(exp.value).replace('"', '\\"')
        return f'assert[{target}, equals="{escaped}"]'
    return f"assert[{target}]"


def _format_step_statements(step: IRStep) -> list[str]:
    if step.target.kind == "coord":
        raise IRError(
            f"第 {step.no} 步使用坐标目标（coord={step.target.coord}），"
            "不允许编译为正式 .tcs 用例（违反 R11）"
        )
    stmts: list[str] = []
    action = step.action
    target_name = step.target.name

    if action == "open":
        stmts.append(f"open[{target_name or '首页'}]")
    elif action == "wait":
        timeout = step.args.get("timeout")
        if timeout is not None:
            sec = max(1, int(float(timeout)))
            stmts.append(f"wait[{target_name}, timeout={sec}s]")
        else:
            stmts.append(f"wait[{target_name}]")
    elif action == "click":
        stmts.append(f"click[{target_name}]")
    elif action == "type":
        text = str(step.args.get("text") or "")
        stmts.append(f"type[{target_name}, {text}]")
        if step.args.get("submit"):
            stmts.append("press_key[Enter]")
    elif action == "press_key":
        key = str(step.args.get("key") or target_name or "Enter")
        stmts.append(f"press_key[{key}]")
    elif action == "scroll":
        direction = str(step.args.get("direction") or "down")
        amount = int(step.args.get("amount") or 300)
        if target_name and target_name != "window":
            stmts.append(f"scroll[{target_name}, {direction}, {amount}]")
        else:
            stmts.append(f"scroll[{direction}]")
    elif action == "hover":
        stmts.append(f"hover[{target_name}]")
    elif action == "assert":
        pass
    else:
        raise IRError(f"第 {step.no} 步包含不支持编译的 action: {action!r}")

    for exp in step.expect:
        stmts.append(_format_expect(exp))

    if not stmts:
        raise IRError(f"第 {step.no} 步未产生任何有效 TCS 语句")
    return stmts


def compile_ir_to_tcs(ir: IR, *, priority: str = "P1",
                      owner: str = "dsh") -> str:
    """将合法 :class:`IR` 编译为 TCS 源码文本，并通过 DSL Parser 自检。"""
    ir.validate()
    case_name = _safe_identifier(ir.meta.name, f"{ir.meta.app}_dsh_case")
    os_val = ir.meta.os if ir.meta.os in {"macOS", "Windows", "Linux", "all", "universal"} else "all"

    lines: list[str] = [
        f"name:     {case_name}",
        f"app:      {ir.meta.app}",
        f"os:       {os_val}",
        f"priority: {priority}",
        f"owner:    {owner}",
        "",
    ]

    total = len(ir.steps)
    for index, step in enumerate(ir.steps):
        if index == 0 and step.phase == "setup":
            lines.append("@setup")
        elif index == total - 1 and step.phase == "teardown":
            lines.append("@teardown")
        title = (step.note or step.describe() or f"步骤{step.no}").replace("\n", " ").strip()
        lines.append(f"step: {title}")
        for stmt in _format_step_statements(step):
            lines.append(f"    {stmt}")
        lines.append("")

    source = "\n".join(lines).rstrip() + "\n"
    from uita.core.dsl.parser import ParseError, parse_source

    try:
        parse_source(source)
    except ParseError as exc:
        raise IRError(f"IR 编译产物未通过 TCS 语法校验: {exc}") from exc
    return source


def compile_ir_file(ir_path: str, out_tcs_path: str = "", *,
                    priority: str = "P1", owner: str = "dsh") -> tuple[str, str]:
    """读取 IR 文件并编译为 TCS；若给 ``out_tcs_path`` 则原子落盘，返回 ``(source, out_path)``。"""
    from .ir import load_ir

    ir = load_ir(ir_path)
    source = compile_ir_to_tcs(ir, priority=priority, owner=owner)
    target = str(out_tcs_path or "").strip()
    if target:
        directory = os.path.dirname(os.path.abspath(target)) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tcs.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(source)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return source, target


def promote_ir(ir: IR, *, cases_dir: str = "", name: str = "",
               overwrite: bool = False) -> str:
    """将验证通过的 IR 提升入库到 ``<CASES_DIR>/<app>/ir/<name>.ir.json``。"""
    ir.validate()
    for step in ir.steps:
        if step.target.kind == "coord":
            raise IRError(f"第 {step.no} 步为坐标步骤，禁止提升入库（违反 R11）")

    from uita import config

    root = os.path.realpath(os.path.abspath(cases_dir or config.CASES_DIR))
    app_seg = _safe_identifier(ir.meta.app, "")
    if not app_seg:
        raise IRError("meta.app 非法")
    base_name = _safe_identifier(name or ir.meta.name, f"{app_seg}_case")
    ir_dir = os.path.realpath(os.path.join(root, app_seg, "ir"))
    if os.path.commonpath([root, ir_dir]) != root:
        raise IRError("目标路径越界")
    os.makedirs(ir_dir, exist_ok=True)

    candidate = os.path.join(ir_dir, f"{base_name}.ir.json")
    if os.path.exists(candidate) and not overwrite:
        suffix = 2
        while os.path.exists(os.path.join(ir_dir, f"{base_name}_v{suffix}.ir.json")):
            suffix += 1
        candidate = os.path.join(ir_dir, f"{base_name}_v{suffix}.ir.json")

    return save_ir(ir, candidate)


def resolve_tcs_output_path(tcs_out: str, ir: IR, *,
                            cases_dir: str = "",
                            preferred_name: str = "") -> str:
    """解析 ``--tcs-out`` 路径：支持空值/``auto`` 自动推导至 ``cases/<app>/<name>.tcs``。"""
    raw = str(tcs_out or "").strip()
    if not raw:
        return ""
    from uita import config

    app_seg = _safe_identifier(ir.meta.app, "dsh")
    stem = _safe_identifier(preferred_name or ir.meta.name, f"{app_seg}_dsh_case")
    if raw.lower() == "auto":
        root = os.path.abspath(cases_dir or config.CASES_DIR)
        return os.path.join(root, app_seg, f"{stem}.tcs")
    if os.path.isdir(raw) or raw.endswith(("/", "\\")):
        return os.path.join(os.path.abspath(raw), f"{stem}.tcs")
    if not raw.lower().endswith(".tcs"):
        return f"{raw}.tcs"
    return raw
