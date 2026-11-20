"""probe-provision 编排器（探针部署期注入）。

由 ``main.py --mode probe-provision`` 调用，职责仅限于：定位并调用
``apps/<app_name>/probe_provision.py::main(ctx)``，附加超时保护与异常兜底。

背景：Qt UI 探针在本框架里是「部署期注入一次、运行期只连接」的模型——
注入往安装目录部署探针并重启被测进程，属一次性运维动作；
运行期每次 launch/restart 只做 attach（连端口），不需要提权。

与 ``pretest.py`` 同构：
本模块不提供、不假设、不强制任何"标准部署方案"，具体怎么做（源目录在哪里、
拷到哪个子目录、怎么重启、怎么判就绪）完全由各 App 自己的
``probe_provision.py`` 决定。

执行失败**不阻断**调用方流水线（退出码始终 0，与 pretest 一致），
但 stdout 与 ``--probe-report`` 必须如实反映 success/skipped/failed。
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from uita.core.platform.app_registry import AppNotFoundError, AppRegistry
from uita.core.registry_common import UNIVERSAL_PLATFORM, resolve_universal_platform
from uita.config import ROOT_DIR

logger = logging.getLogger("uita.probe_provision")

# 单次执行超时（秒），可用环境变量覆盖。
_DEFAULT_TIMEOUT_SEC = 120.0


@dataclass
class ProbeProvisionResult:
    """探针部署结果。status 取值：success / skipped / failed。"""

    status: str
    message: str = ""
    # 结构化明细（写 --probe-report 用；app 侧可自由填充，框架不解释字段语义）
    report: dict = field(default_factory=dict)


@dataclass
class ProbeProvisionContext:
    """注入 probe_provision.py::main 的上下文。"""

    app_name: str
    platform: str
    extra: dict = field(default_factory=dict)
    root_dir: str = ROOT_DIR


def _get_timeout_sec() -> float:
    raw = os.environ.get("UIA_PROBE_PROVISION_TIMEOUT_SEC")
    if not raw:
        return _DEFAULT_TIMEOUT_SEC
    try:
        value = float(raw)
        return value if value > 0 else _DEFAULT_TIMEOUT_SEC
    except ValueError:
        logger.warning(
            "UIA_PROBE_PROVISION_TIMEOUT_SEC=%r 非法，使用默认值 %s", raw, _DEFAULT_TIMEOUT_SEC,
        )
        return _DEFAULT_TIMEOUT_SEC


def _load_probe_provision_module(app_name: str) -> tuple[Any | None, Optional[str]]:
    """动态加载 apps/<app_name>/probe_provision.py。

    返回 (module, load_error)：
    - (module, None)：加载成功；
    - (None, None)：文件不存在 → skipped（多数 app 无探针需求，天然落此分支）；
    - (None, "<msg>")：模块存在但加载失败 → failed（真实 bug，不可静默吞成 skipped）。
    """
    module_name = f"apps.{app_name}.probe_provision"
    try:
        module = importlib.import_module(module_name)
        return module, None
    except ModuleNotFoundError as exc:
        # 未提供该文件时，exc.name 可能是 f"apps.{app_name}"（父包也不存在）或
        # 完整 module_name；两种都视为「该 app 没有探针部署需求」→ skipped。
        if exc.name and (module_name == exc.name or module_name.startswith(f"{exc.name}.")):
            logger.info("App %s 未提供 probe_provision.py，跳过", app_name)
            return None, None
        msg = f"加载 probe_provision.py 失败（内部 import 缺失依赖 {exc.name}）: {exc}"
        logger.error(msg)
        return None, msg
    except Exception as exc:  # noqa: BLE001
        msg = f"加载 probe_provision.py 失败: {exc}\n{traceback.format_exc()}"
        logger.error(msg)
        return None, msg


def _build_context(app_name: str, platform: str) -> ProbeProvisionContext:
    extra: dict = {}
    try:
        cfg = AppRegistry.get(app_name, platform)
        extra = dict(cfg.extra)
    except AppNotFoundError:
        logger.debug("App %s platform %s 未注册，extra 为空", app_name, platform)
    return ProbeProvisionContext(
        app_name=app_name, platform=platform, extra=extra, root_dir=ROOT_DIR,
    )


def _run_sync_with_timeout(
    main_fn: Callable[[Any], Any], ctx: ProbeProvisionContext, timeout_sec: float,
) -> Any:
    """在守护线程中执行同步 main_fn，超时抛 TimeoutError。"""
    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["result"] = main_fn(ctx)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_sec)
    if thread.is_alive():
        raise TimeoutError(
            f"probe_provision.py::main 执行超时（>{timeout_sec}s），底层线程已放弃等待"
        )
    if "error" in box:
        raise box["error"]
    return box.get("result")


def _write_report(path: str, payload: dict) -> None:
    """写 --probe-report（best-effort，失败仅 warning，不影响结论）。"""
    if not path:
        return
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        logger.info("[probe-provision] 报告已写入 %s", path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[probe-provision] 写报告失败（忽略）: %s", exc)


def run_probe_provision(
    app_name: str,
    platform: Optional[str] = None,
    report_path: str = "",
) -> ProbeProvisionResult:
    """执行单个 App 的探针部署。任何异常都转成 failed，不向上抛。"""
    if not app_name:
        logger.warning("app_name 为空，跳过 probe-provision")
        return ProbeProvisionResult("skipped", "app_name is empty")

    # 平台标识统一小写（与 AppRegistry/引擎 platform 属性口径一致），
    # 便于 app 侧直接比较，也保证 --probe-report 里的字段稳定。
    effective_platform = (platform or resolve_universal_platform(UNIVERSAL_PLATFORM)).strip().lower()
    ctx = _build_context(app_name, effective_platform)

    module, load_error = _load_probe_provision_module(app_name)
    if load_error is not None:
        result = ProbeProvisionResult("failed", load_error)
        _write_report(report_path, _report_payload(app_name, effective_platform, result, 0.0))
        return result
    if module is None:
        result = ProbeProvisionResult("skipped", f"apps/{app_name}/probe_provision.py not found")
        _write_report(report_path, _report_payload(app_name, effective_platform, result, 0.0))
        return result

    main_fn = getattr(module, "main", None)
    if not callable(main_fn):
        logger.warning("probe_provision.py 未暴露 main 函数，跳过: %s", app_name)
        result = ProbeProvisionResult("skipped", "probe_provision.py has no callable main")
        _write_report(report_path, _report_payload(app_name, effective_platform, result, 0.0))
        return result

    timeout_sec = _get_timeout_sec()
    logger.info(
        "开始执行 probe-provision: app=%s platform=%s timeout=%ss",
        app_name, effective_platform, timeout_sec,
    )
    started = time.time()
    try:
        if inspect.iscoroutinefunction(main_fn):
            result = asyncio.run(asyncio.wait_for(main_fn(ctx), timeout=timeout_sec))
        else:
            result = _run_sync_with_timeout(main_fn, ctx, timeout_sec)

        if not isinstance(result, ProbeProvisionResult):
            logger.warning(
                "probe_provision.py::main 返回值不是 ProbeProvisionResult，视作 success: %s",
                type(result),
            )
            result = ProbeProvisionResult(
                "success", "main returned non-ProbeProvisionResult",
            )
    except (asyncio.TimeoutError, TimeoutError) as exc:
        msg = f"执行 probe_provision.py::main 超时（>{timeout_sec}s）: app={app_name}, {exc}"
        logger.error(msg)
        result = ProbeProvisionResult("failed", msg)
    except Exception as exc:  # noqa: BLE001
        msg = f"执行 probe_provision.py::main 异常: {exc}\n{traceback.format_exc()}"
        logger.error(msg)
        result = ProbeProvisionResult("failed", msg)

    _write_report(
        report_path,
        _report_payload(app_name, effective_platform, result, time.time() - started),
    )
    return result


def _report_payload(
    app_name: str, platform: str, result: ProbeProvisionResult, elapsed: float,
) -> dict:
    payload = {
        "app_name": app_name,
        "platform": platform,
        "status": result.status,
        "message": result.message,
        "elapsed": round(elapsed, 3),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if result.report:
        payload["detail"] = result.report
    return payload
