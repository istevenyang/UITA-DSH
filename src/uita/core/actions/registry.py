"""
Action registry — @register_action decorator + get_action().

支持 app 维度隔离（P1 新增，见 docs/plans/20260703102900-...）：
  - register_action(*names) / register_action(*names, app="sample_web")
    app="" 表示全局注册（现状行为），app="sample_web" 等表示仅该 app 优先命中。
  - register_action_scope_alias("tool", "sample_web")
    显式声明临时 app 的 Action 作用域继承关系，不对未知 app 做任意兜底。
  - get_action(name) / get_action(name, app="sample_web")
    查找优先级：app 精确命中 → 显式来源 app 链 → 全局 → 报错。

Usage:
    @register_action("click")
    class ClickAction(ActionBase): ...

    @register_action("jump", app="sample_web")
    class JumpAction(ActionBase): ...

    action = get_action("click")               # 命中全局
    action = get_action("jump", app="sample_web")      # 命中 sample_web 专属
"""
from __future__ import annotations

from typing import Type

from .base import ActionBase

# key: (app_scope, action_name)，app_scope="" 表示全局
_REGISTRY: dict[tuple[str, str], Type[ActionBase]] = {}
# key: 临时/派生 app scope，value: 显式来源 app scope
_ACTION_SCOPE_ALIASES: dict[str, str] = {}


class ActionNotFoundError(Exception):
    pass


def register_action(*names: str, app: str = ""):
    """Decorator to register an ActionBase subclass under one or more keywords.

    Args:
        names: 一个或多个关键字（如 "click"）。
        app:   限定生效的 app 名（默认 "" = 全局，所有 app 可见）。
    """
    def decorator(cls: Type[ActionBase]) -> Type[ActionBase]:
        for name in names:
            _REGISTRY[(app.lower(), name.lower())] = cls
        return cls
    return decorator


def register_action_scope_alias(alias: str, source_app: str) -> None:
    """显式声明 alias 继承 source_app 的 app-scoped Action。"""
    alias_key = alias.lower()
    source_key = source_app.lower() if source_app else ""
    if not alias_key:
        raise ValueError("Action scope alias 不能为空")
    if not source_key or source_key == alias_key:
        _ACTION_SCOPE_ALIASES.pop(alias_key, None)
        return
    _ACTION_SCOPE_ALIASES[alias_key] = source_key


def _iter_action_scopes(app_key: str):
    """按 alias → 来源链迭代，循环关系安全终止。"""
    seen: set[str] = set()
    scope = app_key
    while scope and scope not in seen:
        seen.add(scope)
        yield scope
        scope = _ACTION_SCOPE_ALIASES.get(scope, "")


def get_action(name: str, app: str = "") -> Type[ActionBase]:
    """按 app 精确命中 → 显式来源 app 链 → 全局 → 报错。"""
    key = name.lower()
    app_key = app.lower() if app else ""
    for scope in _iter_action_scopes(app_key):
        if (scope, key) in _REGISTRY:
            return _REGISTRY[(scope, key)]
    if ("", key) in _REGISTRY:
        return _REGISTRY[("", key)]
    raise ActionNotFoundError(
        f"No action registered for keyword '{name}' (app={app!r}). "
        f"Registered: {sorted(_REGISTRY.keys())}"
    )


def all_actions() -> dict[tuple[str, str], Type[ActionBase]]:
    return dict(_REGISTRY)
