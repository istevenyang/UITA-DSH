"""--mode parse：解析 .tcs 文件打印 AST。"""
from __future__ import annotations

import dataclasses
import json

from uita.cli.modes.common import StartupError, _resolve_single_tcs_file


def _handle_parse(cli) -> int:
    from uita.core.dsl.parser import parse_file
    file = _resolve_single_tcs_file(cli, "parse")
    try:
        case = parse_file(file)
    except FileNotFoundError:
        raise StartupError(f"文件不存在: {file}")

    def _serialize(obj):
        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        return str(obj)

    print(json.dumps(dataclasses.asdict(case), ensure_ascii=False, indent=2, default=_serialize))
    return 0
