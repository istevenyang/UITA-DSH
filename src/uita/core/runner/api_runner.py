"""API-only Case Runner：初始化 target、认证、Engine 后执行独立 API DSL。

2026-08-25 起：引擎四件套构造逻辑已提取到 core/engine/api_factory.py::
build_api_engine()（纯提取重构，零行为变化），供 core/lifecycle/
api_engine_slot.py（主 DSL switch[api]）共用。本类保留全部对外行为
（ApiRunnerStartupError 异常类型、_validate_host 签名）不变。

见 docs/plans/done/archive_202608.md §2.3。
"""
from __future__ import annotations

import uuid

from uita.core.api_dsl.api_parser import ApiParseError, parse_api_file
from uita.core.api_runner.interpreter import ApiInterpreter
from uita.core.engine.api_factory import ApiEngineFactoryError, build_api_engine, validate_api_host
from uita.core.registry_common import effective_platform_for_case, platform_matches_host
from uita.core.result.collector import CaseResult, make_run_result_dir


class ApiRunnerStartupError(RuntimeError):
    """API Case 在执行前无法安全启动。"""


class ApiRunner:
    async def run_file(self, case_file: str) -> CaseResult:
        try:
            case = parse_api_file(case_file)
        except (OSError, ApiParseError) as exc:
            raise ApiRunnerStartupError(str(exc)) from exc
        if not platform_matches_host(case.os):
            raise ApiRunnerStartupError("API case OS does not match current host")
        platform = effective_platform_for_case(case.os)

        try:
            bundle = await build_api_engine(case.app, platform)
        except ApiEngineFactoryError as exc:
            raise ApiRunnerStartupError(str(exc)) from exc

        try:
            result_dir = make_run_result_dir(case_file)
            interpreter = ApiInterpreter(
                case=case,
                catalog=bundle.catalog,
                engine=bundle.engine,
                runtime_config=bundle.runtime_config,
                result_dir=result_dir,
                run_id=uuid.uuid4().hex,
                case_file=case_file,
            )
            return await interpreter.run()
        except ApiRunnerStartupError:
            raise
        except Exception as exc:
            raise ApiRunnerStartupError(f"API runner startup failed: {type(exc).__name__}") from exc
        finally:
            await bundle.engine.close()
            await bundle.auth_provider.close()

    @staticmethod
    def _validate_host(endpoint: str, allowed_hosts: tuple[str, ...]) -> str:
        """向后兼容包装：原样委托 api_factory.validate_api_host()（提取自
        本类），保留原方法名/签名/异常类型不变，供既有单测
        （tests/core/runner/test_api_runner.py）零改动通过。"""
        try:
            return validate_api_host(endpoint, allowed_hosts)
        except ApiEngineFactoryError as exc:
            raise ApiRunnerStartupError(str(exc)) from exc
