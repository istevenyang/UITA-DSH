"""--mode env_check handler。"""

from __future__ import annotations

import argparse
import json

from uita.core.diag.env_check import check as _check, print_report as _print_report


def _handle_env_check(cli: argparse.Namespace) -> int:
    """输出 UITA 环境变量检查报告（含 .env.local 模板）。
    支持 --json 输出机器可读格式。
    """
    result = _check()

    if getattr(cli, "json", False):
        output = {
            "env_local_exists": result["env_local_exists"],
            "total": result["total"],
            "present_count": len(result["present"]),
            "missing_count": len(result["missing"]),
            "missing_required_count": len(result["missing_required"]),
            "by_category": result["categories"],
            "present": [e.name for e in result["present"]],
            "missing": [
                {"name": e.name, "category": e.category, "required": e.required,
                 "description": e.description, "example": e.example}
                for e in result["missing"]
            ],
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        _print_report(result)

    return 0

