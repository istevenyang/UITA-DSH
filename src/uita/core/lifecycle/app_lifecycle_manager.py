"""AppLifecycleManager — (app_name, platform, engine) 的面向对象生命周期封装。

设计原则：
  - 纯委托：所有方法委托给 core/lifecycle/engine_lifecycle.py 中已验证的纯函数，
    不重复实现任何生命周期逻辑，零行为变化。
  - 零平台专有依赖：只依赖 EngineBase 抽象接口，不 import atomacos/pywinauto/
    Quartz 等平台专有模块，任意平台（macOS/Windows/Linux）均可安全 import。
  - 供 core/lifecycle/app_lifecycle_stack.py（嵌套栈）与 main.py/interpreter.py/
    batch_runner.py 等调用点统一使用。

recover() 的 per-app 定制点（2026-07-02 补充）：
  不同 app 的恢复策略差异，统一通过 LaunchPolicyRegistry 查找 app 在
  apps/<name>/<name>_launch_policy.py 中注册的 LaunchPolicyBase 子类的
  recover() 方法覆盖——core/lifecycle/ 内本文件不出现任何 app 特判分支。
  未覆盖（返回 None）或未注册该 app 的 policy 时，使用 _default_recover()
  的通用算法（软复位优先，失败降级硬重启），行为与本次改动前完全一致。

计划文档：docs/plans/archive/01-engine-lifecycle-实施纪要.md
        docs/plans/archive/01-engine-lifecycle-实施纪要.md（方案B）
"""
from __future__ import annotations

import logging

from uita.core.engine.base import EngineBase
from uita.core.lifecycle.engine_lifecycle import (
    clean_launch,
    prepare_engine_for_case,
    soft_reset_to_home,
)
from uita.core.lifecycle.launch_policy import LaunchPolicyRegistry

logger = logging.getLogger("lifecycle.app_manager")


class AppLifecycleManager:
    """单个 (app_name, platform) 的生命周期管理器，内部持有一个 EngineBase 实例。

    用法::

        mgr = AppLifecycleManager("sample_web", "macOS", engine)
        await mgr.prepare_for_case(case_name="smoke_01", is_first=True)
        ...
        action = "healthy" | "soft_reset" | "hard_restart"
        action = await mgr.recover()
    """

    def __init__(self, app_name: str, platform: str, engine: EngineBase) -> None:
        self.app_name = app_name
        self.platform = platform
        self.engine = engine

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"AppLifecycleManager(app_name={self.app_name!r}, platform={self.platform!r})"

    # ── 需求2：启动状态验证 + 生命周期管理 ──────────────────────

    async def verify_health(self) -> bool:
        """委托 engine.is_healthy()。"""
        return await self.engine.is_healthy()

    async def prepare_for_case(
        self,
        *,
        case_name: str = "",
        is_first: bool = False,
        force_hard_restart: bool = False,
        tag_prefix: str = "lifecycle",
    ) -> None:
        """委托 prepare_engine_for_case()（三段式策略：冷启动/软复位/硬重启）。

        供 batch/runner 等按 case 粒度调用的场景使用。
        """
        await prepare_engine_for_case(
            self.engine,
            case_name=case_name or self.app_name,
            is_first=is_first,
            tag_prefix=tag_prefix,
            force_hard_restart=force_hard_restart,
        )

    async def soft_reset(self, tag: str = "") -> bool:
        """委托 soft_reset_to_home()。引擎不支持时返回 False（不抛异常）。"""
        return await soft_reset_to_home(self.engine, tag=tag or f"manager[{self.app_name}]")

    async def restart(self, tag: str = "") -> None:
        """委托 clean_launch()（硬重启）。"""
        await clean_launch(self.engine, tag=tag or f"manager[{self.app_name}]")

    async def recover(self, tag: str = "") -> str:
        """需求2 核心方法：连接不健康时的自动降级恢复策略。

        恢复决策优先查找 LaunchPolicyRegistry 中该 (app_name, platform) 注册的
        自定义 recover() 覆盖（见 launch_policy.py::LaunchPolicyBase.recover 文档）；
        未注册/未覆盖（返回 None）/执行异常时，均降级到 _default_recover() 通用算法。

        Returns:
            "healthy"      — 健康检查通过，未采取任何动作
            "soft_reset"   — 不健康，软复位成功（默认算法或自定义覆盖返回）
            "hard_restart" — 不健康且软复位失败/不支持，已执行硬重启（默认算法或自定义覆盖返回）
            <自定义标签>    — 自定义覆盖执行了非默认动作时返回的其他标签
        """
        _tag = tag or f"manager[{self.app_name}]"
        if await self.verify_health():
            logger.debug("[%s] recover: 引擎健康，跳过", _tag)
            return "healthy"

        policy_cls = LaunchPolicyRegistry.try_get(self.app_name, self.platform)
        if policy_cls is not None:
            policy = policy_cls()
            try:
                custom = await policy.recover(self.engine, lambda: self._default_recover(_tag))
            except Exception as exc:
                logger.warning(
                    "[%s] LaunchPolicy[%s].recover 异常，降级走默认算法: %s", _tag, policy.name, exc
                )
                custom = None
            if custom is not None:
                logger.info("[%s] LaunchPolicy[%s] recover 覆盖结果 → %s", _tag, policy.name, custom)
                return custom

        return await self._default_recover(_tag)

    async def _default_recover(self, tag: str) -> str:
        """框架默认恢复算法：软复位优先，失败降级硬重启。

        独立成方法，供 LaunchPolicyBase.recover() 的 default_recover 参数复用
        （子类可在自定义逻辑前后调用它，也可以完全不调用而自行接管）。
        """
        logger.info("[%s] recover: 引擎不健康，尝试软复位", tag)
        if await self.soft_reset(tag=tag):
            return "soft_reset"

        logger.info("[%s] recover: 软复位未命中，执行硬重启", tag)
        await self.restart(tag=tag)
        return "hard_restart"

    async def close(self) -> None:
        """委托 engine.close()，异常仅记录不抛出（收尾操作应尽力而为）。"""
        try:
            await self.engine.close()
        except Exception as exc:
            logger.warning("[manager:%s] close 异常: %s", self.app_name, exc)

    async def ensure_alive(self, tag: str = "") -> str:
        """switch[]/switch_back[] 用例内引擎池保活：健康检查优先；不健康则
        reattach_alive()（附着/冷启动，不 kill）；reattach 后仍不健康（进程
        确已不可用）才降级走 recover()（软复位/硬重启）。

        与 prepare_for_case()（用例间生命周期，受 RESTART_BEFORE_FIRST_CASE/
        RESTART_BETWEEN_CASES 控制）职责完全分离：switch_to()/switch_back()
        只调用本方法，不再调用 enter()/prepare_for_case()，避免"用例内切换"
        错误触发"用例间批次起点"的强制 kill+relaunch 语义，从根本上消除
        "switch[]/switch_back[] 误杀已存在健康引擎"的问题。

        见 docs/plans/archive/02-dsl-actions-实施纪要.md。

        Returns:
            "healthy"    — 已健康，未采取任何动作
            "reattached" — 不健康，reattach（launch()，未 kill）后恢复健康
            <recover() 返回值："soft_reset"/"hard_restart"/自定义标签>
                — reattach 仍不健康，降级走 recover()
        """
        _tag = tag or f"manager[{self.app_name}]"
        if await self.verify_health():
            return "healthy"
        from uita.core.lifecycle.engine_lifecycle import reattach_alive
        if await reattach_alive(self.engine, tag=_tag):
            logger.info("[%s] ensure_alive: reattach/launch 成功，无需 kill", _tag)
            return "reattached"
        logger.warning(
            "[%s] ensure_alive: reattach 仍不健康，降级走 recover()（软复位/硬重启）", _tag,
        )
        return await self.recover(tag=_tag)
