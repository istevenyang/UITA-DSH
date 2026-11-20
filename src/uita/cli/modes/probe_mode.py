"""--mode probe：元素探查，生成 rosetta_map 骨架。"""
from __future__ import annotations

import asyncio
import os

from uita.cli.modes.common import StartupError


def _handle_probe(cli) -> int:
    from uita.config import ROOT_DIR

    import importlib.util
    probe_path = os.path.join(ROOT_DIR, "tools", "auto_probe.py")
    if not os.path.isfile(probe_path):
        raise StartupError(f"auto_probe.py 不存在: {probe_path}")
    spec = importlib.util.spec_from_file_location("auto_probe", probe_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    import argparse as _argparse
    probe_args = _argparse.Namespace(
        app=cli.app, platform=cli.probe_platform, cdp_url=cli.probe_cdp_url,
        output=cli.probe_output, page=cli.probe_page,
    )
    return asyncio.run(module.main_async(probe_args))
