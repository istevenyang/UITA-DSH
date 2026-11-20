"""DSH 内置工具集：导入即注册（每个模块只声明工具，不实现编排）。

新增工具只需在此目录下新建模块 + ``@register_dsh_tool``，并加入
``_MODULES``；不改注册表与安全闸（R2）。
"""
from __future__ import annotations

import importlib

_MODULES: tuple[str, ...] = (
    "env",        # 安装 / 探针注入 / 应用生命周期 / 状态
    "observe",    # 观察快照
    "locate",     # 定位
    "act",        # 操作
    "assertion",  # 断言（三态）
    "case",       # 用例执行
    "meta",       # task_finish 等编排元工具
    "workspace",  # 受控工作区文件读写/编辑/检索（无头研发）
    "skill",      # Skill/Agent 加载与 UITA 受控子命令调用
)

_LOADED: set[str] = set()


def load_all() -> None:
    for name in _MODULES:
        if name in _LOADED:
            continue
        importlib.import_module(f"{__name__}.{name}")
        _LOADED.add(name)
