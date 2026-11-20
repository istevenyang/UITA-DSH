"""ProfileEngineSwitcher — profile step 级 engine/app 自动切换（仅注册 app 间）。"""
from __future__ import annotations

from typing import Any, Optional

from uita.core.platform.app_registry import AppNotFoundError, AppRegistry


class ProfileEngineSwitcher:
    """按 profile_step 的 engine/app_name 决定当前 step 应使用的 (engine, resolver)。"""

    def __init__(
        self,
        default_app: str,
        default_platform: str,
        engine: Any,
        resolver: Any,
    ) -> None:
        self._default_app = default_app
        self._default_platform = default_platform
        self._default_engine = engine
        self._default_resolver = resolver
        self._current_app = default_app
        self._engine = engine
        self._resolver = resolver

    @property
    def engine(self) -> Any:
        return self._engine

    @property
    def resolver(self) -> Any:
        return self._resolver

    @property
    def current_app(self) -> str:
        return self._current_app

    async def resolve_for_step(self, profile_step: Optional[dict]) -> bool:
        target_app = self._decide_target_app(profile_step or {})
        if target_app is None or target_app == self._current_app:
            return False

        if target_app == self._default_app:
            self._engine = self._default_engine
            self._resolver = self._default_resolver
            self._current_app = self._default_app
            return True

        if not self._is_registered(target_app):
            return False

        engine = await self._acquire_engine(target_app)
        from uita.core.runner.resolver_factory import build_resolver_for_app
        resolver = build_resolver_for_app(engine, target_app, self._default_platform)

        self._engine = engine
        self._resolver = resolver
        self._current_app = target_app
        return True

    def _decide_target_app(self, profile_step: dict) -> Optional[str]:
        engine_kind = str(profile_step.get("engine") or "").strip().lower()
        if not engine_kind:
            return None
        app_name = str(profile_step.get("app_name") or "").strip()
        if engine_kind == "playwright":
            return app_name or "sample_web"
        if engine_kind == "uia":
            return app_name or self._default_app
        return None

    def _is_registered(self, app_name: str) -> bool:
        try:
            AppRegistry.get(app_name, self._default_platform)
            return True
        except AppNotFoundError:
            return False

    async def _acquire_engine(self, app_name: str) -> Any:
        from uita.core.lifecycle.app_lifecycle_stack import get_default_stack
        stack = get_default_stack()
        mgr = await stack.switch_to(app_name, self._default_platform, tag_prefix="profile_switch")
        return mgr.engine
