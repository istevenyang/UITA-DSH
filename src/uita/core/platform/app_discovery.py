"""App 别名动态发现。

扫描 ``apps/*/configs.py``，读取各 app 的 ``APP_ALIASES``（tuple[str]），
构建 alias → app_name 映射。未声明 ``APP_ALIASES`` 的 app 静默跳过。
"""
from __future__ import annotations

import importlib
import os
import sys
from typing import Dict


def discover_app_aliases() -> Dict[str, str]:
    """扫描所有 apps/*/configs.py，构建 alias → app_name 映射表。"""
    try:
        from uita.config import WORKSPACE_DIR as ROOT_DIR
    except ImportError:
        ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    apps_dir = os.path.join(ROOT_DIR, "apps")
    if not os.path.isdir(apps_dir):
        return {}

    if ROOT_DIR not in sys.path:
        sys.path.insert(0, ROOT_DIR)

    aliases: Dict[str, str] = {}
    for entry in sorted(os.listdir(apps_dir)):
        app_path = os.path.join(apps_dir, entry)
        if not os.path.isdir(app_path):
            continue
        if entry.startswith("_") or entry.startswith("."):
            continue
        config_file = os.path.join(app_path, "configs.py")
        if not os.path.isfile(config_file):
            continue

        try:
            module = importlib.import_module(f"apps.{entry}.configs")
            app_aliases = getattr(module, "APP_ALIASES", None)
            if app_aliases and isinstance(app_aliases, (tuple, list)):
                for alias in app_aliases:
                    alias_str = str(alias).strip()
                    if alias_str:
                        aliases[alias_str] = entry
        except Exception:
            continue

    return aliases
