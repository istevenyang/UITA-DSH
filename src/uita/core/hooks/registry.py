"""Hook 注册表 — @register_hook 装饰器 + get_hook() + build_hooks()。"""
from __future__ import annotations

from typing import Type

from .base import HookBase

_REGISTRY: dict[str, Type[HookBase]] = {}


class HookNotFoundError(Exception):
    pass


def register_hook(*names: str):
    """类装饰器，将 HookBase 子类注册到全局表。"""
    def decorator(cls: Type[HookBase]) -> Type[HookBase]:
        for name in names:
            _REGISTRY[name.lower()] = cls
        return cls
    return decorator


def get_hook(name: str) -> Type[HookBase]:
    key = name.lower()
    if key not in _REGISTRY:
        raise HookNotFoundError(
            f"未找到 Hook '{name}'，已注册: {sorted(_REGISTRY.keys())}"
        )
    return _REGISTRY[key]


def build_hooks(names: list[str]) -> list[HookBase]:
    """按名称列表构建 Hook 实例列表，忽略 'none'/'null'/'off'。"""
    _ensure_builtins_registered()

    skip_tokens = {"none", "null", "off", "false", ""}
    instances: list[HookBase] = []
    for name in names:
        if name.strip().lower() in skip_tokens:
            continue
        try:
            cls = get_hook(name.strip())
            instances.append(cls())
        except HookNotFoundError as exc:
            import logging
            logging.getLogger("hooks").warning("Hook '%s' 未找到，跳过: %s", name, exc)
    return instances


def _ensure_builtins_registered() -> None:
    """懒加载内置 Hook，确保注册。"""
    try:
        from uita.core.hooks.builtin import screenshot_hook  # noqa: F401
    except Exception:
        pass
