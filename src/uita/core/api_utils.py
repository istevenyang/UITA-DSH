"""API 测试公共工具函数（移植自 SampleAgent 项目 common/ 模块，snake_case 化）。

来源：/Users/friday/PythonProject/SampleAgent/common/__init__.py 及其
common/impl/{value,empty,chars,data}.py。

供后续分期的 API Action（extract[]/assert_json[]）与用例数据构造复用，本期仅
落地为独立库函数，不接入任何调用方（R30 分期原则，见
docs/plans/archive/01-engine-lifecycle-实施纪要.md §2.4）。

零外部依赖，仅标准库（R2：core/ 不得 import engine/、apps/、cli/、tools/）。

安全说明：SampleAgent 原版 loads() 用 eval(text) 作为 JSON 解析失败后的兜底，这对来自
被测 HTTP 服务的响应体（不可信输入）存在任意代码执行风险（安全规范第 2 条 RCE）。
本文件的 safe_loads() 去除了 eval 兜底，仅保留 json.loads + 原样返回两级降级。
"""
from __future__ import annotations

import json
import random
import string
import uuid
from datetime import datetime
from typing import Any


def is_empty(data_object: Any) -> bool:
    """判空：[]/{}/()/0/False/None/"" 均视为空。"""
    if isinstance(data_object, bool):
        return data_object is False
    if isinstance(data_object, int):
        return data_object == 0
    if isinstance(data_object, (str, list, tuple, dict)):
        return len(data_object) == 0
    return data_object is None


def not_empty(data_object: Any) -> bool:
    """is_empty 取反。"""
    return not is_empty(data_object)


def value_by_path(data_object: Any, path: str = "") -> Any:
    """按 "." 分割路径从嵌套 dict/list 取值，取不到返回 None。

    支持 "a.b.0.c" 形式（数字段按 list 下标取值，支持负数下标）；不支持 SampleAgent 原版
    的 "[x=y]" 条件过滤语法（该语法复杂度高、与 JSONPath 标准不兼容，留给后续
    分期评估是否需要移植，或直接采用标准 JSONPath 库替代）。
    """
    current = data_object
    if not path:
        return current
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, (list, tuple)) and part.lstrip("-").isdigit():
            idx = int(part)
            current = current[idx] if -len(current) <= idx < len(current) else None
        else:
            return None
    return current


def format_time(tag: str = "day") -> str:
    """当前时间格式化：tag="day" 返回 "%Y-%m-%d"，其余返回带时分秒的完整格式。"""
    if tag == "day":
        return datetime.now().strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def random_char_by_len(char_len: int, case: str = "mix") -> str:
    """生成指定长度的随机字母串。case: "lower"/"upper"/"mix"（默认大小写混合）。"""
    if char_len <= 0:
        raise ValueError("char_len must be > 0")
    chars = "".join(random.choices(string.ascii_letters, k=char_len))
    if case == "lower":
        return chars.lower()
    if case == "upper":
        return chars.upper()
    return chars


def random_int_by_len(int_len: int) -> int:
    """生成指定位数长度的随机整数（如 int_len=3 → [100, 999]）。"""
    if int_len <= 0:
        raise ValueError("int_len must be > 0")
    return random.randint(10 ** (int_len - 1), 10 ** int_len - 1)


def random_int_by_range(min_int: int, max_int: int) -> int:
    """生成指定范围 [min_int, max_int] 内的随机整数（闭区间）。"""
    min_int, max_int = int(min_int), int(max_int)
    if min_int > max_int:
        raise ValueError("min_int must be <= max_int")
    return random.randint(min_int, max_int)


def guid() -> str:
    """生成 32 位小写 GUID（无连字符）。"""
    return uuid.uuid4().hex


def timestamp(tag: str = "second") -> int:
    """当前时间戳。tag="millisecond" 返回毫秒级，其余（默认 "second"）返回秒级。"""
    now = datetime.now().timestamp()
    return int(now * 1000) if tag == "millisecond" else int(now)


def is_content_same(obj1: Any, obj2: Any) -> bool:
    """深度比较两个对象内容是否相同（dict/list/基本类型递归比较），供响应体
    断言复用。字典比较不关心键顺序，列表比较关心元素顺序。
    """
    if type(obj1) is not type(obj2):
        return False
    if isinstance(obj1, dict):
        return obj1.keys() == obj2.keys() and all(
            is_content_same(obj1[k], obj2[k]) for k in obj1
        )
    if isinstance(obj1, (list, tuple)):
        return len(obj1) == len(obj2) and all(
            is_content_same(a, b) for a, b in zip(obj1, obj2)
        )
    return obj1 == obj2


def safe_loads(text: Any) -> Any:
    """宽容 JSON 解析：已是 dict/list 原样返回；str 解析失败则原样返回原始文本
    （不使用 SampleAgent 原版的 eval() 兜底，见模块顶部安全说明）。
    """
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text
