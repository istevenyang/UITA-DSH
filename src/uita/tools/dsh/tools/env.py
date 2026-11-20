"""环境类工具：安装、探针注入、应用生命周期、引擎状态。

生命周期纪律（R3）：启动/复位一律经 ``AppLifecycleStack.enter()``，其内部即
``prepare_engine_for_case()``；本模块**不得**直接调用 ``engine.launch()`` /
``engine.restart()``。
"""
from __future__ import annotations

from typing import Any

from ..registry import (TIER_READ, TIER_WRITE, DshTool, ToolResult,
                        register_dsh_tool)


def _stack() -> Any:
    from uita.core.lifecycle.app_lifecycle_stack import get_default_stack

    return get_default_stack()


def _engine_of(manager: Any) -> Any:
    return getattr(manager, "engine", manager)


@register_dsh_tool
class AppPrepareTool(DshTool):
    name = "app_prepare"
    tier = TIER_WRITE
    description = (
        "经 UITA 生命周期统一入口启动/复位被测应用（首次冷启动、用例间软复位、"
        "失败后硬重启），返回引擎健康状态。每个会话第一次操作应用前必须调用。"
    )
    params = {
        "app": {"type": "string", "description": "应用名；缺省用会话目标 app"},
        "case_name": {"type": "string", "description": "标签用用例名"},
        "is_first": {"type": "boolean", "description": "是否首次启动（缺省按会话状态推断）"},
        "force_hard_restart": {"type": "boolean", "description": "强制硬重启"},
    }
    timeout_s = 300.0

    async def run(self, ctx: Any, app: str = "", case_name: str = "",
                  is_first: bool | None = None,
                  force_hard_restart: bool = False) -> ToolResult:
        target = (app or ctx.app_name or "").strip()
        if not target:
            return ToolResult.fail("未指定 app，且会话未声明目标 app")
        first = (not ctx.prepared) if is_first is None else bool(is_first)
        manager = await _stack().enter(
            target, ctx.os_name, case_name=case_name or "dsh",
            is_first=first, tag_prefix="dsh",
        )
        engine = _engine_of(manager)
        ctx.set_engine(engine, target)
        healthy: Any = None
        try:
            healthy = await engine.is_healthy()
        except Exception as exc:  # noqa: BLE001 - 健康探测失败不影响已拿到的引擎
            healthy = f"probe_failed: {exc}"
        return ToolResult(ok=True, data={
            "app": target, "platform": ctx.os_name,
            "is_first": first, "healthy": healthy,
        })


@register_dsh_tool
class EnvInstallTool(DshTool):
    name = "env_install"
    tier = TIER_WRITE
    description = "下载并覆盖安装被测应用（macOS/Windows），返回安装事实（版本/路径/签名/隔离属性）。"
    params = {
        "app": {"type": "string", "description": "应用名"},
        "package_url": {"type": "string", "description": "安装包 URL"},
        "package_file": {"type": "string", "description": "本地安装包路径（与 URL 二选一）"},
    }
    required = ("app",)
    timeout_s = 900.0

    async def run(self, ctx: Any, app: str, package_url: str = "",
                  package_file: str = "") -> ToolResult:
        if bool(package_url) == bool(package_file):
            return ToolResult.fail("package_url 与 package_file 必须二选一")
        from uita.core.lifecycle import app_installer

        source = package_url or package_file
        try:
            if package_url:
                target_path = app_installer.install_app(app, package_url)
            else:
                target_path = app_installer.install_app_from_file(app, package_file)
        except app_installer.InstallError as exc:
            return ToolResult.fail(f"安装失败: {exc}")
        facts: dict[str, Any] = {}
        try:
            facts = app_installer.collect_install_facts(
                app, target_path, source, ctx.os_name,
            )
        except Exception as exc:  # noqa: BLE001 - 事实采集失败不推翻安装成功
            facts = {"error": f"{type(exc).__name__}: {exc}"}
        return ToolResult(ok=True, data={
            "app": app, "target_path": target_path,
            "source": source, "facts": facts,
        })


@register_dsh_tool
class EnvProbeProvisionTool(DshTool):
    name = "env_probe_provision"
    tier = TIER_WRITE
    description = (
        "部署期一次性注入 Qt UI 探针（拷探针 → 重启被测进程 → 等端口就绪）；"
        "运行期不再注入。返回 success/skipped/failed。"
    )
    params = {
        "app": {"type": "string", "description": "应用名"},
        "platform": {"type": "string", "description": "平台名，缺省按宿主推断"},
    }
    required = ("app",)
    timeout_s = 900.0

    async def run(self, ctx: Any, app: str, platform: str = "") -> ToolResult:
        from uita.core.lifecycle import probe_provision

        result = probe_provision.run_probe_provision(
            app, platform or ctx.os_name,
        )
        status = getattr(result, "status", "failed")
        return ToolResult(
            ok=status != "failed",
            data={
                "app": app,
                "status": status,
                "message": getattr(result, "message", ""),
                "report": getattr(result, "report", {}) or {},
            },
            error="" if status != "failed" else getattr(result, "message", "探针注入失败"),
        )


@register_dsh_tool
class AppSwitchTool(DshTool):
    name = "app_switch"
    tier = TIER_WRITE
    description = "切换到另一个已注册应用（引擎池保活，不清掉原应用上下文）；可用 app_close 清理。"
    params = {
        "app": {"type": "string", "description": "目标应用名"},
        "platform": {"type": "string", "description": "平台名，缺省按宿主推断"},
    }
    required = ("app",)
    timeout_s = 300.0

    async def run(self, ctx: Any, app: str, platform: str = "") -> ToolResult:
        manager = await _stack().switch_to(app, platform or ctx.os_name, tag_prefix="dsh")
        ctx.set_engine(_engine_of(manager), app)
        return ToolResult(ok=True, data={"app": app, "platform": ctx.os_name})


@register_dsh_tool
class AppCloseTool(DshTool):
    name = "app_close"
    tier = TIER_WRITE
    description = "关闭应用并移除其生命周期缓存（终止进程），用于会话收尾或换装。"
    params = {
        "app": {"type": "string", "description": "应用名；缺省为会话当前应用"},
    }

    async def run(self, ctx: Any, app: str = "") -> ToolResult:
        target = (app or ctx.app_name or "").strip()
        if not target:
            return ToolResult.fail("未指定 app")
        await _stack().evict(target, ctx.os_name)
        if not app or target == ctx.app_name:
            ctx.drop_engine()
        return ToolResult(ok=True, data={"app": target, "closed": True})


@register_dsh_tool
class AppStatusTool(DshTool):
    name = "app_status"
    tier = TIER_READ
    description = "读取当前引擎健康状态、窗口标题与窗口矩形（不启动任何进程）。"
    params = {}

    async def run(self, ctx: Any) -> ToolResult:
        if ctx.engine is None:
            return ToolResult(ok=False, data={
                "prepared": False, "app": ctx.app_name,
            }, error="引擎未准备（先调用 app_prepare）")
        engine = ctx.engine
        data: dict[str, Any] = {"prepared": True, "app": ctx.app_name,
                                "platform": ctx.os_name}
        try:
            data["healthy"] = await engine.is_healthy()
        except Exception as exc:  # noqa: BLE001
            data["healthy"] = f"probe_failed: {exc}"
        try:
            data["window_title"] = await engine.get_window_title()
        except Exception as exc:  # noqa: BLE001 - 引擎可选能力缺失
            data["window_title_error"] = str(exc)
        rect_fn = getattr(engine, "get_window_rect", None)
        if callable(rect_fn):
            try:
                rect = await rect_fn()
                if rect:
                    data["window_rect"] = list(rect)
            except Exception as exc:  # noqa: BLE001
                data["window_rect_error"] = str(exc)
        return ToolResult(ok=True, data=data)
