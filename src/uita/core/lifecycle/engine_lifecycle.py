"""引擎三段式生命周期策略（batch / runner 共用）。

策略（受 settings 配置控制）：
  is_first=True：
    - RESTART_BEFORE_FIRST_CASE=True  → clean_launch（强制重启）
    - RESTART_BEFORE_FIRST_CASE=False → ensure_clean_launched（幂等冷启动）
  is_first=False：
    - restart_between_cases=True      → 直接 clean_launch（强制重启，跳过软复位）
    - restart_between_cases=False     → soft_reset_to_home → 失败则 clean_launch
  force_hard_restart=True             → 无论配置，直接硬重启（优先级最高）
"""
from __future__ import annotations

import asyncio
import logging

from uita.config import NOTICE
from uita.core.engine.base import EngineBase
from uita.core.lifecycle.launch_policy import LaunchPolicyRegistry

logger = logging.getLogger("engine.lifecycle")

# 已完成冷启动的引擎 id 缓存（避免重复 launch）
_cleaned: set[int] = set()


async def _run_launch_policy(engine: EngineBase, tag: str = "") -> None:
    """launch()/restart() 成功后消费 LaunchPolicyRegistry（未注册则跳过，异常/超时不阻断）。"""
    policy_cls = LaunchPolicyRegistry.try_get(engine.app_name, engine.platform)
    if policy_cls is None:
        return
    from uita.config import LAUNCH_POLICY_TIMEOUT

    policy = policy_cls()
    for hook_name, hook_fn in (
        ("wait_ui_ready", policy.wait_ui_ready),
        ("post_launch", policy.post_launch),
    ):
        try:
            await asyncio.wait_for(hook_fn(engine), timeout=LAUNCH_POLICY_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("[%s] LaunchPolicy[%s].%s 超时(%ss)，忽略继续",
                            tag or "lifecycle", policy.name, hook_name, LAUNCH_POLICY_TIMEOUT)
        except Exception as exc:
            logger.warning("[%s] LaunchPolicy[%s].%s 异常，忽略继续: %s",
                            tag or "lifecycle", policy.name, hook_name, exc)


async def _try_launch_timeout_hook(engine: EngineBase, tag: str) -> bool:
    """launch()/restart() 超时时，调用 LaunchPolicy.on_launch_timeout 钩子。"""
    policy_cls = LaunchPolicyRegistry.try_get(engine.app_name, engine.platform)
    if policy_cls is None:
        return False
    from uita.config import LAUNCH_POLICY_TIMEOUT

    policy = policy_cls()
    logger.log(NOTICE,
        "[%s] launch 超时，调用 LaunchPolicy[%s].on_launch_timeout 钩子",
        tag, policy.name,
    )
    try:
        handled = await asyncio.wait_for(
            policy.on_launch_timeout(engine),
            timeout=LAUNCH_POLICY_TIMEOUT,
        )
        if handled:
            logger.log(NOTICE,
                "[%s] LaunchPolicy[%s].on_launch_timeout 已处理超时根因，将重试 launch",
                tag, policy.name,
            )
        else:
            logger.log(NOTICE,
                "[%s] LaunchPolicy[%s].on_launch_timeout 返回 False（未检测到可处理的阻塞原因）",
                tag, policy.name,
            )
        return handled
    except asyncio.TimeoutError:
        logger.warning(
            "[%s] LaunchPolicy[%s].on_launch_timeout 自身超时(%ss)",
            tag, policy.name, LAUNCH_POLICY_TIMEOUT,
        )
        return False
    except Exception as exc:
        logger.warning(
            "[%s] LaunchPolicy[%s].on_launch_timeout 异常: %s",
            tag, policy.name, exc,
        )
        return False


async def _default_soft_then_hard(engine: EngineBase, tag: str) -> str:
    """框架默认恢复算法：软复位优先，失败降级硬重启。"""
    soft_ok = await soft_reset_to_home(engine, tag=tag)
    if soft_ok:
        logger.info("[%s] 软复位命中，跳过硬重启", tag)
        return "soft_reset"

    logger.info("[%s] 软复位未命中，触发硬重启", tag)
    reset_clean_launched_cache()
    await clean_launch(engine, tag=tag)
    return "hard_restart"


async def _recover_between_cases(engine: EngineBase, tag: str) -> None:
    """非首条用例 + RESTART_BETWEEN_CASES=False 的恢复入口。"""
    policy_cls = LaunchPolicyRegistry.try_get(engine.app_name, engine.platform)

    async def _default() -> str:
        return await _default_soft_then_hard(engine, tag)

    if policy_cls is not None:
        policy = policy_cls()
        try:
            custom = await policy.recover(engine, _default)
        except Exception as exc:
            logger.warning(
                "[%s] LaunchPolicy[%s].recover 异常，降级默认算法: %s", tag, policy.name, exc
            )
            custom = None
        if custom is not None:
            logger.info("[%s] LaunchPolicy recover 结果 → %s", tag, custom)
            return

    await _default()


def reset_clean_launched_cache() -> None:
    """清除冷启动缓存（硬重启前调用，确保 ensure_clean_launched 一定执行）。"""
    _cleaned.clear()


async def ensure_clean_launched(engine: EngineBase, tag: str = "") -> None:
    """幂等冷启动：已在线则跳过；离线则 launch。"""
    eid = id(engine)
    if eid in _cleaned:
        logger.debug("[%s] engine 已启动，跳过重复 launch", tag or "lifecycle")
        return
    healthy = await engine.is_healthy()
    if not healthy:
        logger.info("[%s] engine 不在线，执行 launch()", tag or "lifecycle")
        try:
            await engine.launch()
        except RuntimeError:
            if await _try_launch_timeout_hook(engine, tag or "lifecycle"):
                await engine.launch()
            else:
                raise
        await _run_launch_policy(engine, tag=tag)
    else:
        logger.info("[%s] engine 已在线，跳过 launch()", tag or "lifecycle")
    _cleaned.add(eid)


async def soft_reset_to_home(engine: EngineBase, tag: str = "") -> bool:
    """软复位：调用 engine.reset_page_state('soft')，成功返回 True。"""
    reset_fn = getattr(engine, "reset_page_state", None)
    if reset_fn is None:
        logger.debug("[%s] engine 不支持 reset_page_state，降级硬重启", tag or "lifecycle")
        return False
    try:
        from uita.config import SOFT_RESET_TIMEOUT
        await asyncio.wait_for(reset_fn("soft"), timeout=SOFT_RESET_TIMEOUT)
        logger.info("[%s] 软复位成功", tag or "lifecycle")
        return True
    except asyncio.TimeoutError:
        logger.warning("[%s] 软复位超时，降级硬重启", tag or "lifecycle")
        return False
    except Exception as exc:
        logger.warning("[%s] 软复位异常 (%s)，降级硬重启", tag or "lifecycle", exc)
        return False


async def reattach_alive(engine: EngineBase, tag: str = "") -> bool:
    """引擎保活：尝试 launch()（附着已运行实例或冷启动），绝不主动 kill/终止任何进程。"""
    logger.info(
        "[%s] reattach_alive: 引擎不健康，尝试 launch()（附着/冷启动，不 kill）",
        tag or "lifecycle",
    )
    try:
        await engine.launch()
    except Exception as exc:
        logger.debug(
            "[%s] reattach_alive: launch() 异常（忽略，继续走健康检查）: %s",
            tag or "lifecycle", exc,
        )
    healthy = await engine.is_healthy()
    if healthy:
        await _run_launch_policy(engine, tag=tag)
        logger.info("[%s] reattach_alive: 附着/冷启动成功，无需 kill", tag or "lifecycle")
    return healthy


async def _escalate_ineffective_hard_restart(engine: EngineBase, tag: str) -> None:
    """`clean_launch()` 检测到 `engine.last_kill_effective is False` 时的升级钩子。"""
    policy_cls = LaunchPolicyRegistry.try_get(engine.app_name, engine.platform)
    if policy_cls is None:
        logger.warning(
            "[%s] kill 未真正生效（last_kill_effective=False），且未注册"
            " LaunchPolicy 升级策略，继续走原有健康检查", tag,
        )
        return
    policy = policy_cls()
    try:
        handled = await policy.on_hard_restart_ineffective(engine)
    except Exception as exc:
        logger.warning(
            "[%s] LaunchPolicy[%s].on_hard_restart_ineffective 异常，降级"
            " 继续走原有健康检查: %s", tag, policy.name, exc,
        )
        return
    if handled:
        logger.info(
            "[%s] LaunchPolicy[%s].on_hard_restart_ineffective 升级成功", tag, policy.name,
        )
    else:
        logger.warning(
            "[%s] LaunchPolicy[%s].on_hard_restart_ineffective 未能处理"
            "（kill 仍可能未真正生效），继续走原有健康检查", tag, policy.name,
        )


async def clean_launch(engine: EngineBase, tag: str = "") -> None:
    """硬重启：restart() + 等待 is_healthy。"""
    restart_fn = getattr(engine, "restart", None)
    if restart_fn is None:
        logger.info("[%s] engine 无 restart()，执行 close+launch", tag or "lifecycle")
        await engine.close()
        try:
            await engine.launch()
        except RuntimeError:
            if await _try_launch_timeout_hook(engine, tag or "lifecycle"):
                await engine.launch()
            else:
                raise
    else:
        logger.log(NOTICE, "[%s] 执行 engine.restart()", tag or "lifecycle")
        try:
            await restart_fn()
        except RuntimeError:
            if await _try_launch_timeout_hook(engine, tag or "lifecycle"):
                await engine.launch()
            else:
                raise

    if getattr(engine, "last_kill_effective", None) is False:
        await _escalate_ineffective_hard_restart(engine, tag=tag or "lifecycle")

    for _ in range(10):
        if await engine.is_healthy():
            _cleaned.add(id(engine))
            logger.info("[%s] 硬重启完成，engine 已就绪", tag or "lifecycle")
            await _run_launch_policy(engine, tag=tag)
            engine._fresh_restart = True  # noqa: SLF001
            return
        await asyncio.sleep(1.0)
    raise RuntimeError(f"[{tag}] 硬重启后 engine 仍不健康")


async def prepare_engine_for_case(
    engine: EngineBase,
    *,
    case_name: str,
    is_first: bool,
    tag_prefix: str = "batch",
    force_hard_restart: bool = False,
) -> None:
    """为单条 case 准备引擎（三段式策略入口）。"""
    from uita.config import RESTART_BEFORE_FIRST_CASE, RESTART_BETWEEN_CASES
    from uita.core.platform.app_registry import get_extra_config

    tag = f"{tag_prefix}[{case_name}]"

    try:
        from uita.core.lifecycle.ephemeral_app import sweep_ephemeral_apps
        await sweep_ephemeral_apps(exclude_engine=engine)
    except Exception as exc:
        logger.debug("[%s] ephemeral 兜底清理失败（忽略）: %s", tag, exc)

    foreground_fn = getattr(engine, "bring_to_foreground", None)
    if foreground_fn is not None:
        try:
            await foreground_fn()
        except Exception as exc:
            logger.debug("[%s] bring_to_foreground 失败（忽略）: %s", tag, exc)

    if force_hard_restart:
        logger.info("[%s] force_hard_restart=True，直接硬重启", tag)
        reset_clean_launched_cache()
        await clean_launch(engine, tag=tag)
        return

    if is_first:
        if RESTART_BEFORE_FIRST_CASE:
            logger.info("[%s] 首条用例 + RESTART_BEFORE_FIRST_CASE=True，强制重启", tag)
            reset_clean_launched_cache()
            await clean_launch(engine, tag=tag)
        else:
            logger.info("[%s] 首条用例 + RESTART_BEFORE_FIRST_CASE=False，幂等冷启动", tag)
            await ensure_clean_launched(engine, tag=tag)
        return

    restart_between_cases = get_extra_config(
        engine.app_name, engine.platform, "restart_between_cases",
        default=RESTART_BETWEEN_CASES,
    )
    if restart_between_cases:
        logger.info("[%s] restart_between_cases=True，用例间强制重启", tag)
        reset_clean_launched_cache()
        await clean_launch(engine, tag=tag)
        return

    await _recover_between_cases(engine, tag=tag)
