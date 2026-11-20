"""core/engine/api_factory.py — API 引擎四件套统一构造工厂。

原样提取自 core/runner/api_runner.py::ApiRunner.run_file()（20260720150000-
api-engine-layer-completion.md 起已验证的逻辑），零行为变化，供
core/lifecycle/api_engine_slot.py（switch[api]）与 ApiRunner（--mode
api-runner）共用，避免逻辑重复/校验规则（尤其 host allowlist 安全校验，
宪法 R9）产生分叉。

见 docs/plans/done/archive_202608.md §2.3。

R1 依赖方向：`core/` 内部依赖顺序 dsl/→engine/→platform/→resolver/→
actions/→hooks/→runner/→lifecycle/，本模块位于 `core/engine/`，严禁
import `core/runner/`、`core/lifecycle/` 任何内容（反向依赖）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from uita.config import WORKSPACE_DIR
from uita.core.engine.api_auth import ApiAuthProvider, ApiAuthRegistry
from uita.core.engine.api_base import ApiEngineBase
from uita.core.engine.api_config import ApiRuntimeConfig, ApiRuntimeRegistry
from uita.core.engine.api_endpoint_catalog import ApiEndpointCatalog
from uita.core.engine.api_registry import ApiEngineRegistry

__all__ = ["ApiEngineFactoryError", "ApiEngineBundle", "validate_api_host", "build_api_engine"]


class ApiEngineFactoryError(RuntimeError):
    """API 引擎四件套构造失败（注册缺失/catalog 不可用/环境变量缺失/host
    不在 allowlist 等）。调用方（``ApiRunner``/``ApiEngineSlot``）按自身
    语义转译为各自的 Startup 类异常，本类不直接对外暴露给用例作者。"""


@dataclass
class ApiEngineBundle:
    """构造成功后的 API 引擎四件套，供调用方（`ApiRunner`/`ApiEngineSlot`）
    分别接管生命周期（构造后何时 close，由调用方决定，本模块不管理）。"""

    engine: ApiEngineBase
    auth_provider: ApiAuthProvider
    catalog: ApiEndpointCatalog
    runtime_config: ApiRuntimeConfig


def validate_api_host(endpoint: str, allowed_hosts: tuple[str, ...]) -> str:
    """校验并返回规范化的 host（含端口后缀）。原样提取自
    ``ApiRunner._validate_host()``，逐字节行为不变。

    Raises:
        ApiEngineFactoryError: endpoint 非 HTTPS/含路径或 query/不在
            allowlist 内。
    """
    parsed = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host or parsed.path not in ("", "/") or parsed.query:
        raise ApiEngineFactoryError("API endpoint must be an HTTPS host without path or query")
    # 支持 host 或 host:port 两种 allowlist 写法（私有化端口如 8443 需随 host 注入）
    port_suffix = f":{parsed.port}" if parsed.port else ""
    if host.lower() not in {item.lower() for item in allowed_hosts} and (
        f"{host}{port_suffix}".lower() not in {item.lower() for item in allowed_hosts}
    ):
        raise ApiEngineFactoryError("API endpoint is not in the configured allowlist")
    return f"{host}{port_suffix}"


async def build_api_engine(app_name: str, platform: str) -> ApiEngineBundle:
    """从 (app_name, platform) 构造完整可用的 API 引擎四件套：

    1. 查 ``ApiRuntimeRegistry``/``ApiAuthRegistry``/``ApiEngineRegistry``
       三个注册表；
    2. 加载 ``apps/<app_name>/api_map.json``（``ApiEndpointCatalog``）；
    3. 校验 endpoint/region 环境变量是否已配置；
    4. host allowlist 校验（``validate_api_host``）；
    5. 构造鉴权上下文（``provider.initialize()``）+ 引擎实例。

    任一步失败均抛 ``ApiEngineFactoryError``（原始异常通过 ``__cause__``
    保留，供调用方日志排查）。第 5 步中，若鉴权已初始化成功但引擎构造
    失败，会先尝试关闭已初始化的 ``provider`` 再抛异常（避免遗留未关闭
    的鉴权资源——调用方此时尚未拿到 bundle，无法自行关闭）。

    Raises:
        ApiEngineFactoryError
    """
    try:
        runtime = ApiRuntimeRegistry.get(app_name, platform)
        provider = ApiAuthRegistry.get(app_name, platform)()
        engine_cls = ApiEngineRegistry.get(app_name, platform)
    except Exception as exc:
        raise ApiEngineFactoryError("API-only App registration is incomplete") from exc

    # apps/ 为业务资产（WORKSPACE_DIR），非框架包安装位置，pip 场景下二者不同。
    catalog_path = Path(WORKSPACE_DIR) / "apps" / app_name / "api_map.json"
    try:
        catalog = ApiEndpointCatalog.from_file(catalog_path)
    except Exception as exc:
        raise ApiEngineFactoryError("API endpoint catalog is unavailable") from exc

    endpoint = os.environ.get(runtime.endpoint_env, "").strip()
    region = os.environ.get(runtime.region_env, "").strip()
    if not endpoint:
        raise ApiEngineFactoryError("Required API endpoint environment configuration is missing")
    # SaaS TC 需要 region；私有化无需（region_env 留空）
    if runtime.target != "private" and not region:
        raise ApiEngineFactoryError("Required API region environment configuration is missing")

    host = validate_api_host(endpoint, runtime.host_allowlist)

    auth_context = None
    try:
        auth_context = await provider.initialize()
        engine = engine_cls(
            app_name=app_name,
            endpoint=host,
            region=region,
            auth_context=auth_context,
            allowed_hosts=runtime.host_allowlist,
        )
    except Exception as exc:
        try:
            await provider.close()
        except Exception:
            pass
        raise ApiEngineFactoryError(f"API engine construction failed: {type(exc).__name__}") from exc

    return ApiEngineBundle(engine=engine, auth_provider=provider, catalog=catalog, runtime_config=runtime)
