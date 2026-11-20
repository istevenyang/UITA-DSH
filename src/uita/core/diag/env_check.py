"""环境变量配置自检模块（--mode env_check）。

汇总展示 UITA-DSH 中可配置的环境变量及其状态。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

from uita.config import ROOT_DIR


@dataclass(frozen=True)
class EnvVarSpec:
    name: str
    secret: bool
    category: str
    description: str
    required: bool = False
    default: Optional[str] = None
    example: str = ""


ENV_VAR_SPECS: list[EnvVarSpec] = [
    EnvVarSpec(
        "DSH_API_URL", False, "DSH / LLM",
        "统一模型服务接口地址（通用 chat/completions 兼容端点）",
        required=True,
        example="https://api.example.com/v1/chat/completions",
    ),
    EnvVarSpec(
        "DSH_API_KEY", True, "DSH / LLM",
        "统一模型服务 API Key",
        required=True,
        example="sk-your-api-key",
    ),
    EnvVarSpec(
        "UITA_DSH_MODEL", False, "DSH / LLM",
        "DSH 默认模型名称",
        default="default-model",
        example="default-model",
    ),
    EnvVarSpec(
        "UITA_DSH_PORT", False, "DSH / LLM",
        "DSH Web Console 监听端口",
        default="8765",
        example="8765",
    ),
    EnvVarSpec(
        "UITA_ROOT", False, "框架核心",
        "工作区根目录路径覆盖（含 apps/ 和 cases/）",
    ),
    EnvVarSpec(
        "UITA_USE_LOCAL_ROSETTA", False, "框架核心",
        "使用本地 JSON rosetta_map",
        default="1",
        example="1",
    ),
    EnvVarSpec(
        "UITA_LOG_LEVEL", False, "框架核心",
        "日志级别（DEBUG / INFO / NOTICE / WARNING / ERROR）",
        default="INFO",
        example="INFO",
    ),
]


def _mask_secret(val: str) -> str:
    if len(val) <= 8:
        return "***"
    return f"{val[:4]}...{val[-4:]}"


def check() -> dict[str, Any]:
    present: list[EnvVarSpec] = []
    missing: list[EnvVarSpec] = []
    missing_required: list[EnvVarSpec] = []
    categories: dict[str, dict[str, int]] = {}

    for spec in ENV_VAR_SPECS:
        cat = categories.setdefault(spec.category, {"present": 0, "missing": 0})
        raw = os.environ.get(spec.name, "").strip()
        if raw or spec.default is not None:
            present.append(spec)
            cat["present"] += 1
        else:
            missing.append(spec)
            cat["missing"] += 1
            if spec.required:
                missing_required.append(spec)

    return {
        "env_local_exists": os.path.isfile(os.path.join(ROOT_DIR, ".env.local")),
        "total": len(ENV_VAR_SPECS),
        "present": present,
        "missing": missing,
        "missing_required": missing_required,
        "categories": categories,
    }


def print_report(result: Optional[dict[str, Any]] = None) -> None:
    if result is None:
        result = check()
    print("=" * 80)
    print("UITA-DSH 环境变量检查 (--mode env_check)")
    print("=" * 80)
    for spec in ENV_VAR_SPECS:
        raw = os.environ.get(spec.name, "").strip()
        if raw:
            shown = _mask_secret(raw) if spec.secret else raw
            status = f"已设置 ({shown})"
        elif spec.default is not None:
            status = f"默认 ({spec.default})"
        else:
            status = "未设置"
        print(f"  - {spec.name:<24} : {status} — {spec.description}")
    print("=" * 80)


def run_env_check() -> int:
    print_report(check())
    return 0
