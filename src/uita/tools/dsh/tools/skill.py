"""Skill 加载与 UITA 全能力受控编排工具：使 DSH 能发现并编排全部 UITA 子命令与资产。

安全边界（R9）：

- ``skill_load`` 只读 ``.dsh/skills/<name>/`` 与 ``.dsh/agents/<name>.md``，
  校验名字为受控标识（``[A-Za-z0-9_.-]+``），拒绝路径穿越；
- ``uita_capabilities`` 只读汇总 UITA 可编排子命令（``--mode``）、已注册应用、引擎、
  TCS 动作与仓内 Skill/Agent 目录；
- ``uita_invoke`` 允许编排除 ``dsh-*`` 之外的全部 UITA 确定性子命令，一律
  ``subprocess.run(list_args, shell=False)``，禁止传入 ``dsh-*`` 模式以防递归死锁。
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from typing import Any

from ..registry import (TIER_ACTION, TIER_READ, DshTool, ToolResult,
                        register_dsh_tool)

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9_./-]{1,160}$")

#: DSH 可编排的 UITA 子命令目录（模式名 → 用途与关键参数说明）
UITA_MODE_CATALOG: dict[str, dict[str, str]] = {
    "parse": {
        "category": "dsl",
        "summary": "解析 .tcs 用例并打印 AST（不启动引擎）",
        "key_args": "--cases-dir <file.tcs>",
    },
    "lint": {
        "category": "quality",
        "summary": "TCS 或 rosetta_map 格式与规范检查，输出报告",
        "key_args": "--cases-dir <dir_or_file> [--lint-target tcs|rosetta] [--app <app>]",
    },
    "health-check": {
        "category": "quality",
        "summary": "存量 TCS 用例健康检查（Dry-Run，不启动引擎）",
        "key_args": "--app <app> | --cases-dir <dir_or_file>",
    },
    "env_check": {
        "category": "quality",
        "summary": "运行环境与依赖自检",
        "key_args": "[--json]",
    },
    "runner": {
        "category": "execution",
        "summary": "单文件或多文件执行 TCS 用例（支持 --no-engine dry-run）",
        "key_args": "--cases-dir <case.tcs> [--app <app>] [--no-engine] [--hooks screenshot]",
    },
    "batch": {
        "category": "execution",
        "summary": "按目录顺序批量执行 TCS 用例（支持 --tag / --priority 过滤）",
        "key_args": "--cases-dir <dir> [--app <app>] [--tag <tag>] [--priority P0]",
    },
    "api-runner": {
        "category": "execution",
        "summary": "API-only 应用接口用例执行",
        "key_args": "--cases-dir <case.tcs>",
    },
    "perf": {
        "category": "perf",
        "summary": "客户端性能多轮采集（冷启动/场景测量/资源采样/可选 AI 报告）",
        "key_args": "--app <app> [--cases-dir <case.tcs>] [--perf-preset <name>] [--perf-repeat N]",
    },
    "install": {
        "category": "lifecycle",
        "summary": "下载并（覆盖）安装被测应用，输出安装事实 JSON",
        "key_args": "--app <app> --package-url <url> [--uninstall] [--install-report <path>]",
    },
    "pretest": {
        "category": "lifecycle",
        "summary": "执行 apps/<app>/pre_test.py 构造测试前数据与现场",
        "key_args": "--app <app>",
    },
    "probe-provision": {
        "category": "lifecycle",
        "summary": "部署期一次性注入 Qt UI 探针并等待端口就绪",
        "key_args": "--app <app> [--probe-report <path>]",
    },
    "probe": {
        "category": "rosetta",
        "summary": "页面元素探查，生成 rosetta_map 草稿",
        "key_args": "--app <app> [--probe-output <path>] [--probe-page <page>]",
    },
    "probe-diff": {
        "category": "rosetta",
        "summary": "探查草稿与正式 rosetta_map.json 语义 diff（--apply 才落库）",
        "key_args": "--app <app> --probe-diff-new <draft.json> [--apply]",
    },
    "rosetta-fp": {
        "category": "rosetta",
        "summary": "Rosetta 视觉指纹编码、解码与静态截图匹配",
        "key_args": "--fp-mode encode|decode|match --app <app> --element <name>",
    },
}

ALLOWED_UITA_MODES = frozenset(UITA_MODE_CATALOG.keys())


def _skill_roots(ctx: Any) -> list[str]:
    from uita import config

    roots: list[str] = []
    for base in (
        getattr(ctx, "workdir", ""),
        getattr(config, "WORKSPACE_DIR", ""),
        getattr(config, "ROOT_DIR", ""),
    ):
        text = str(base or "").strip()
        if text:
            candidate = os.path.realpath(os.path.join(text, ".dsh"))
            if os.path.isdir(candidate) and candidate not in roots:
                roots.append(candidate)
    return roots


def load_skill_or_agent_bundle(ctx: Any, *, skill: str = "", agent: str = "",
                               reference: str = "") -> dict[str, Any]:
    """读取指定 Skill 或 Agent 定义文本（供工具与 headless 启动器共用）。"""
    roots = _skill_roots(ctx)
    if not roots:
        raise FileNotFoundError("未找到 .dsh 目录")

    if agent:
        if not _SAFE_NAME_RE.match(agent):
            raise ValueError(f"非法 agent 名: {agent!r}")
        for root in roots:
            agent_file = os.path.realpath(os.path.join(root, "agents", f"{agent}.md"))
            if os.path.commonpath([root, agent_file]) == root and os.path.isfile(agent_file):
                with open(agent_file, encoding="utf-8") as handle:
                    return {
                        "kind": "agent",
                        "name": agent,
                        "path": agent_file,
                        "content": handle.read(),
                    }
        raise FileNotFoundError(f"未找到 Agent 定义: {agent}")

    if not skill or not _SAFE_NAME_RE.match(skill):
        raise ValueError(f"非法 skill 名: {skill!r}")
    for root in roots:
        skill_dir = os.path.realpath(os.path.join(root, "skills", skill))
        if os.path.commonpath([root, skill_dir]) != root or not os.path.isdir(skill_dir):
            continue
        skill_md = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        with open(skill_md, encoding="utf-8") as handle:
            content = handle.read()
        ref_content = ""
        ref_path = ""
        if reference:
            if not _SAFE_REF_RE.match(reference) or ".." in reference.split("/"):
                raise ValueError(f"非法 reference 路径: {reference!r}")
            rel = reference if reference.startswith("reference/") else os.path.join("reference", reference)
            candidate = os.path.realpath(os.path.join(skill_dir, rel))
            if os.path.commonpath([skill_dir, candidate]) != skill_dir:
                raise ValueError(f"reference 越界: {reference!r}")
            if not os.path.isfile(candidate):
                raise FileNotFoundError(f"未找到 Skill reference: {reference}")
            ref_path = candidate
            with open(candidate, encoding="utf-8") as handle:
                ref_content = handle.read()
        refs_dir = os.path.join(skill_dir, "reference")
        available_refs: list[str] = []
        if os.path.isdir(refs_dir):
            for dirpath, _dirs, files in os.walk(refs_dir):
                for fname in sorted(files):
                    full = os.path.join(dirpath, fname)
                    available_refs.append(os.path.relpath(full, skill_dir).replace("\\", "/"))
        return {
            "kind": "skill",
            "name": skill,
            "path": skill_md,
            "content": content,
            "reference_path": ref_path,
            "reference_content": ref_content,
            "available_references": available_refs,
        }
    raise FileNotFoundError(f"未找到 Skill 定义: {skill}")


def collect_uita_capabilities(ctx: Any = None, *, category: str = "") -> dict[str, Any]:
    """汇总 UITA 当前可编排的子命令、已注册 App、TCS 动作与 Skill/Agent 列表。"""
    from uita import config
    from ..planner import _available_actions

    cat_filter = str(category or "").strip().lower()
    modes: dict[str, dict[str, str]] = {}
    for name, info in sorted(UITA_MODE_CATALOG.items()):
        if cat_filter and info.get("category") != cat_filter:
            continue
        modes[name] = dict(info)

    apps: list[str] = []
    apps_dir = os.path.join(getattr(config, "WORKSPACE_DIR", ""), "apps")
    if os.path.isdir(apps_dir):
        for entry in sorted(os.listdir(apps_dir)):
            if os.path.isfile(os.path.join(apps_dir, entry, "register.py")):
                apps.append(entry)

    skills: list[str] = []
    agents: list[str] = []
    knowledge_products: list[str] = []
    for root in _skill_roots(ctx):
        s_dir = os.path.join(root, "skills")
        if os.path.isdir(s_dir):
            for item in sorted(os.listdir(s_dir)):
                if os.path.isfile(os.path.join(s_dir, item, "SKILL.md")) and item not in skills:
                    skills.append(item)
        a_dir = os.path.join(root, "agents")
        if os.path.isdir(a_dir):
            for item in sorted(os.listdir(a_dir)):
                if item.endswith(".md"):
                    stem = item[:-3]
                    if stem not in agents:
                        agents.append(stem)
        k_dir = os.path.join(root, "knowledge", "products")
        if os.path.isdir(k_dir):
            for item in sorted(os.listdir(k_dir)):
                if os.path.isdir(os.path.join(k_dir, item)) and item not in knowledge_products:
                    knowledge_products.append(item)

    return {
        "modes": modes,
        "modes_count": len(modes),
        "registered_apps": apps,
        "tcs_actions": _available_actions(),
        "skills": skills,
        "agents": agents,
        "knowledge_products": knowledge_products,
    }


@register_dsh_tool
class SkillLoadTool(DshTool):
    name = "skill_load"
    tier = TIER_READ
    description = (
        "加载 .dsh/skills/<skill>/SKILL.md（及可选 reference 文档）或 "
        ".dsh/agents/<agent>.md 的完整规范与工作流说明。"
    )
    params = {
        "skill": {"type": "string", "description": "Skill 名称（如 ui-automation / tcs-lint）"},
        "agent": {"type": "string", "description": "Agent 名称（与 skill 二选一，如 ui-tester）"},
        "reference": {"type": "string", "description": "可选：Skill 下的 reference 文件名"},
    }

    async def run(self, ctx: Any, skill: str = "", agent: str = "",
                  reference: str = "") -> ToolResult:
        if not skill.strip() and not agent.strip():
            return ToolResult.fail("必须指定 skill 或 agent")
        try:
            bundle = load_skill_or_agent_bundle(
                ctx, skill=skill.strip(), agent=agent.strip(),
                reference=reference.strip(),
            )
        except (ValueError, FileNotFoundError) as exc:
            return ToolResult.fail(str(exc))
        return ToolResult(ok=True, data=bundle)


@register_dsh_tool
class UitaCapabilitiesTool(DshTool):
    name = "uita_capabilities"
    tier = TIER_READ
    description = (
        "查询 DSH 可编排的 UITA 全量子命令（--mode 目录、分类、参数速查）、"
        "已注册被测应用（apps）、TCS 动作关键字及仓内可用 Skill/Agent 清单。"
    )
    params = {
        "category": {
            "type": "string",
            "description": (
                "可选分类过滤：dsl / quality / execution / perf / lifecycle / rosetta"
            ),
        },
    }

    async def run(self, ctx: Any, category: str = "") -> ToolResult:
        return ToolResult(
            ok=True,
            data=collect_uita_capabilities(ctx, category=category),
        )


@register_dsh_tool
class UitaInvokeTool(DshTool):
    name = "uita_invoke"
    tier = TIER_ACTION
    description = (
        "以无 shell 方式（shell=False）受控编排调用 UITA 子命令（支持 parse / lint / "
        "health-check / env_check / runner / batch / api-runner / perf / "
        "install / pretest / probe-provision / probe / "
        "probe-diff / rosetta-fp）。不确定参数时先调用 uita_capabilities 查看 key_args。"
    )
    params = {
        "mode": {
            "type": "string",
            "enum": sorted(ALLOWED_UITA_MODES),
            "description": "UITA 子命令 / --mode 名称",
        },
        "args": {
            "type": "array",
            "items": {"type": "string"},
            "description": "附加 CLI 参数列表（不含 --mode，如 [\"--app\", \"sample_web\", \"--cases-dir\", \"cases/sample_web/smoke.tcs\"]）",
        },
        "timeout_s": {
            "type": "integer",
            "description": "子进程超时秒数（默认 120，最大 1800）",
        },
    }
    required = ("mode",)

    async def run(self, ctx: Any, mode: str, args: list[str] | None = None,
                  timeout_s: int = 120) -> ToolResult:
        clean_mode = str(mode or "").strip()
        if clean_mode == "env-check":
            clean_mode = "env_check"
        if clean_mode not in ALLOWED_UITA_MODES:
            return ToolResult.fail(
                f"不允许的 mode={clean_mode!r}（仅允许：{sorted(ALLOWED_UITA_MODES)}）"
            )
        extra_args: list[str] = []
        for item in list(args or []):
            token = str(item)
            if token == "--mode" or token.startswith("--mode="):
                return ToolResult.fail("args 中禁止重复传入 --mode")
            extra_args.append(token)

        from uita import config

        cwd = (
            str(getattr(ctx, "workdir", "") or "").strip()
            or str(getattr(config, "WORKSPACE_DIR", "") or "").strip()
            or os.getcwd()
        )
        timeout = max(5, min(int(timeout_s or 120), 1800))
        cmd = [sys.executable, "-m", "uita", "--mode", clean_mode, *extra_args]

        def _exec() -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )

        try:
            proc = await asyncio.to_thread(_exec)
        except subprocess.TimeoutExpired:
            return ToolResult.fail(f"uita --mode {clean_mode} 超时（{timeout}s）")
        except Exception as exc:  # noqa: BLE001
            return ToolResult.fail(f"调用失败: {type(exc).__name__}: {exc}")

        return ToolResult(
            ok=proc.returncode == 0,
            data={
                "mode": clean_mode,
                "returncode": proc.returncode,
                "stdout": (proc.stdout or "")[-8000:],
                "stderr": (proc.stderr or "")[-4000:],
            },
            error="" if proc.returncode == 0 else f"退出码 {proc.returncode}: {(proc.stderr or proc.stdout or '')[:300]}",
        )


def _resolve_workspace_root(ctx: Any) -> str:
    from uita import config

    for base in (
        getattr(ctx, "workdir", ""),
        getattr(config, "WORKSPACE_DIR", ""),
        getattr(config, "ROOT_DIR", ""),
    ):
        text = str(base or "").strip()
        if text and os.path.isdir(os.path.join(text, ".dsh")):
            return os.path.realpath(text)
    return os.path.realpath(getattr(config, "WORKSPACE_DIR", "") or os.getcwd())


@register_dsh_tool
class KbRecallTool(DshTool):
    name = "kb_recall"
    tier = TIER_READ
    description = (
        "从 .dsh/knowledge/products/<product>/ 本地知识图谱执行平级召回"
        "（词面 + 五维章节 + kb:auto 自动区 + 关系表 1~2 跳图扩展）。"
    )
    params = {
        "product": {"type": "string", "description": "产品知识目录名（如 sample_web）"},
        "query": {"type": "string", "description": "检索问题或关键词"},
        "dimension": {
            "type": "string",
            "description": (
                "可选五维过滤：business_endpoint / business_flow / "
                "tech_endpoint / tech_arch / test_design"
            ),
        },
        "top_k": {"type": "integer", "description": "返回最大条数（默认 8）"},
        "hop_depth": {"type": "integer", "description": "关系图扩展跳数（默认 1）"},
    }
    required = ("product", "query")

    async def run(
        self,
        ctx: Any,
        product: str,
        query: str,
        dimension: str = "",
        top_k: int = 8,
        hop_depth: int = 1,
    ) -> ToolResult:
        if not _SAFE_NAME_RE.match(str(product or "").strip()):
            return ToolResult.fail(f"非法 product 名: {product!r}")
        from pathlib import Path
        from ci.sentinel_kb_flywheel import InputError, recall_knowledge

        ws_root = Path(_resolve_workspace_root(ctx))
        try:
            report = recall_knowledge(
                ws_root,
                product.strip(),
                query,
                dimension=dimension.strip() or None,
                top_k=max(1, min(int(top_k or 8), 30)),
                hop_depth=max(0, min(int(hop_depth if hop_depth is not None else 1), 2)),
            )
        except InputError as exc:
            return ToolResult.fail(str(exc))
        return ToolResult(ok=True, data=report)


@register_dsh_tool
class KbWritebackTool(DshTool):
    name = "kb_writeback"
    tier = TIER_ACTION
    description = (
        "向 .dsh/knowledge/products/<product>/ 受控回写经验（仅限 <!-- kb:auto:start/end --> "
        "围栏区，支持 run-digest / pitfall / defect-chain / five-dim，手写区字节零改动）。"
    )
    params = {
        "product": {"type": "string", "description": "产品知识目录名（如 sample_web）"},
        "action": {
            "type": "string",
            "enum": ["run-digest", "pitfall", "defect-chain", "five-dim"],
            "description": "回写类型",
        },
        "payload": {
            "type": "object",
            "description": (
                "回写数据字典：pitfall 需 {title, symptom, root_cause, rule, evidence}；"
                "defect-chain 需 {chain_id, title, trigger, propagation, fix_anchor, regression_case, evidence}；"
                "five-dim 需 {dimension, section, id, name, summary, source, evidence}；"
                "run-digest 需 {run_report_path}"
            ),
        },
    }
    required = ("product", "action", "payload")

    async def run(
        self,
        ctx: Any,
        product: str,
        action: str,
        payload: dict[str, Any],
    ) -> ToolResult:
        if not _SAFE_NAME_RE.match(str(product or "").strip()):
            return ToolResult.fail(f"非法 product 名: {product!r}")
        from pathlib import Path
        from ci.sentinel_kb_writeback import (
            GuardViolation,
            InputError,
            append_defect_chain,
            append_five_dim,
            append_pitfall,
            append_run_digest,
            read_json,
        )

        ws_root = Path(_resolve_workspace_root(ctx))
        prod = product.strip()
        data = dict(payload or {})
        try:
            if action == "pitfall":
                out = append_pitfall(
                    ws_root,
                    prod,
                    title=str(data.get("title", "")),
                    symptom=str(data.get("symptom", "")),
                    root_cause=str(data.get("root_cause", "")),
                    rule=str(data.get("rule", "")),
                    evidence=str(data.get("evidence", "")),
                    dedup_key=data.get("dedup_key"),
                )
            elif action == "defect-chain":
                out = append_defect_chain(
                    ws_root,
                    prod,
                    chain_id=str(data.get("chain_id", "")),
                    title=str(data.get("title", "")),
                    trigger=str(data.get("trigger", "")),
                    propagation=str(data.get("propagation", "")),
                    fix_anchor=str(data.get("fix_anchor", "")),
                    regression_case=str(data.get("regression_case", "")),
                    evidence=str(data.get("evidence", "")),
                )
            elif action == "five-dim":
                out = append_five_dim(
                    ws_root,
                    prod,
                    dimension=str(data.get("dimension", "")),
                    section=str(data.get("section", "")),
                    item_id=str(data.get("id", "")),
                    name=str(data.get("name", "")),
                    summary=str(data.get("summary", "")),
                    source=str(data.get("source", "")),
                    evidence=str(data.get("evidence", "")),
                )
            elif action == "run-digest":
                report_path = Path(str(data.get("run_report_path", "")))
                if not report_path.is_absolute():
                    report_path = ws_root / report_path
                report = read_json(report_path)
                if not report:
                    return ToolResult.fail(f"无法读取 run-report: {report_path}")
                out = append_run_digest(ws_root, prod, report)
            else:
                return ToolResult.fail(f"未知回写动作: {action!r}")
        except (InputError, GuardViolation) as exc:
            return ToolResult.fail(str(exc))
        return ToolResult(ok=True, data=out)

