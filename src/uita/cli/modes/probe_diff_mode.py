"""--mode probe-diff：对比"真机探查草稿"（*_rosetta_map_probe.json）与现有
`apps/<app>/rosetta_map.json`，生成结构化 diff 报告（新增/消失/变更/selector 冲突）。

对齐 R22：本 mode 只读，从不直接改写 `apps/<app>/rosetta_map.json` 正式配置；
`--apply` 仅代表"diff 报告已人工确认"，追加写入知识库文档（probe-log.md 必写、
known-issues.md 仅风险项才写），是否把草稿内容合并进 rosetta_map.json 正式条目
仍需人工另行操作（保持"探查草稿不直接入库"的宪法要求）。
"""
from __future__ import annotations

import json
import os

from uita.cli.modes.common import StartupError


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        raise StartupError(f"probe-diff 模式：JSON 解析失败 {path}: {exc}") from exc


def _handle_probe_diff(cli) -> int:
    # 惰性 import：tools/rosetta_diff 为纯标准库实现（零第三方依赖，实测见
    # docs/plans/archive/08-ci-delivery-实施纪要.md §5.3），
    # 且随主 wheel 打包。此 try/except 仅为防御 wheel 构建异常导致的模块缺失。
    try:
        from uita.tools.rosetta_diff.differ import diff_rosetta_maps
        from uita.tools.rosetta_diff.knowledge_writer import write_diff_to_knowledge
        from uita.tools.rosetta_diff.report import render_json, render_markdown
    except ImportError as exc:
        raise StartupError(
            f"probe-diff 模式：tools/rosetta_diff 模块缺失（uita 包安装不完整），"
            f"{exc}。请重新安装 uita 包。"
        ) from exc

    # apps/ 为业务资产（WORKSPACE_DIR），不是框架包安装位置（ROOT_DIR/PKG_DIR）。
    # pip 安装场景下二者不同，误用 ROOT_DIR 会导致找不到 apps/<app>/rosetta_map.json。
    from uita.config import WORKSPACE_DIR

    app = (getattr(cli, "app", "") or "").strip()
    if not app:
        raise StartupError("probe-diff 模式需要 --app 指定目标应用")

    new_path = (getattr(cli, "probe_diff_new", "") or "").strip()
    if not new_path:
        raise StartupError("probe-diff 模式需要 --probe-diff-new 指定探查草稿 JSON 路径")
    if not os.path.isfile(new_path):
        raise StartupError(f"probe-diff 模式：探查草稿文件不存在: {new_path}")

    old_path = (getattr(cli, "probe_diff_old", "") or "").strip()
    if not old_path:
        old_path = os.path.join(WORKSPACE_DIR, "apps", app, "rosetta_map.json")

    old_map = _load_json(old_path) if os.path.isfile(old_path) else {}
    new_map = _load_json(new_path)

    diff = diff_rosetta_maps(old_map, new_map, app=app)

    fmt = (getattr(cli, "probe_diff_format", "") or "markdown").strip().lower()
    output = json.dumps(render_json(diff), ensure_ascii=False, indent=2) if fmt == "json" else render_markdown(diff)

    out_path = (getattr(cli, "probe_diff_out", "") or "").strip()
    if out_path:
        out_dir = os.path.dirname(os.path.abspath(out_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"[probe-diff] 报告已写入: {out_path}")
    else:
        print(output)

    if getattr(cli, "apply", False):
        if diff.has_changes:
            # .dsh/knowledge/ 同为业务工作区内容，随 WORKSPACE_DIR 定位。
            result = write_diff_to_knowledge(diff, WORKSPACE_DIR, app, trigger="probe-diff --apply")
            print(f"[probe-diff] 已追加写入知识库: {result['probe_log']}")
            if result.get("known_issues"):
                print(f"[probe-diff] 风险项已追加写入: {result['known_issues']}")
        else:
            print("[probe-diff] 无变更，跳过知识库写入")

    return 0
