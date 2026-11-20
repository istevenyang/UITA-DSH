"""core/api_runner/assert_ops.py — API 断言/响应契约校验纯函数。

原样提取自 core/api_runner/interpreter.py::ApiInterpreter._assert_endpoint_
success()/_execute_assert()（比较算子部分），零行为变化，供 ApiInterpreter
与主 DSL 新增的 get/post/put/delete[]/api_assert[] Action
（core/actions/common/）共用，避免逻辑重复实现。

见 docs/plans/done/archive_202608.md §2.5/§2.6。

零外部依赖（仅标准库），不 import ApiResultView 等具体类型（duck typing，
调用方只需传入具备 .status/.body 属性的对象），避免不必要的跨模块耦合。
"""
from __future__ import annotations

from typing import Any, Callable


class ApiAssertionError(RuntimeError):
    """API 响应契约校验或断言比较失败。"""


def assert_endpoint_success(success_rules: dict[str, Any], result: Any) -> None:
    """校验响应是否满足端点声明的 success_rules（HTTP 状态 + 可选业务错误
    字段）。原样提取自 ApiInterpreter._assert_endpoint_success()。

    Args:
        success_rules: 端点 `api_map.json` 中声明的 success_rules（如
            ``{"error_absent": True}``）。
        result: 具备 ``.status``（int）与 ``.body``（Any）属性的响应视图
            对象（如 ``ApiResultView``），duck typing，不强制类型。

    Raises:
        ApiAssertionError: HTTP 状态非 2xx，或响应体命中业务错误字段。
    """
    if result.status < 200 or result.status >= 300:
        raise ApiAssertionError(f"Unexpected HTTP status {result.status}")
    if success_rules.get("error_absent"):
        body = result.body
        if isinstance(body, dict):
            response = body.get("Response", body)
            if isinstance(response, dict) and response.get("Error"):
                raise ApiAssertionError("API returned a business error")


def compare_values(operator: str, actual: Any, expected: Any) -> bool:
    """比较算子：``==``/``!=``/``<``/``<=``/``>``/``>=``/``contains``/
    ``exists``/``single``。原样提取自 ApiInterpreter._execute_assert()
    内的 ``comparisons`` 字典 + exists/single/contains 分支。

    ``expected`` 对 ``exists``/``single`` 无意义（调用方通常传 ``None``），
    比较逻辑不使用该参数，不做特判校验（与原实现一致）。

    Returns:
        bool，比较是否通过；类型不匹配等异常场景返回 ``False``（不上抛），
        与原实现 ``except (KeyError, TypeError): passed = False`` 一致。
    """
    if operator == "exists":
        return actual is not None
    if operator == "single":
        return isinstance(actual, list) and len(actual) == 1
    if operator == "contains":
        return actual is not None and str(expected) in str(actual)
    comparisons: dict[str, Callable[[], bool]] = {
        "==": lambda: actual == expected,
        "!=": lambda: actual != expected,
        "<": lambda: actual < expected,
        "<=": lambda: actual <= expected,
        ">": lambda: actual > expected,
        ">=": lambda: actual >= expected,
    }
    try:
        return comparisons[operator]()
    except (KeyError, TypeError):
        return False
