"""通用注册表常量与平台解析工具。

UNIVERSAL_PLATFORM：(app_name, platform) 二元注册表（AppRegistry/EngineRegistry/
CollectorRegistry/LaunchPolicyRegistry）的平台哨兵值。当某个能力的实现本身跨平台
一致时（如同一个 Playwright DOM 操作类在 macOS/Windows 表现完全相同），调用
xxxRegistry.register(app_name, UNIVERSAL_PLATFORM, cls) 一次即可——resolve_universal_
platform() 会在【注册的那一刻】把 "universal" 解析为当前宿主机的真实平台字符串
（macos/windows/linux），实际存入注册表的 key 永远是具体平台，不是字面 "universal"。

查找方法 get()/try_get() 因此保持原有精确匹配语义，零改动、零心智负担。

若某 app 在不同平台需要使用不同的底层实现（如 sample_web 的 MacAppKitEngine 与假设的
WinUIAEngine 并非同一个类），不适用本机制——继续在 apps/<name>/register.py 里
用 sys.platform 分支显式声明各平台对应的类。

本模块同时供 tcs 用例的 os 字段语义使用（见 docs/plans/
archive/02-dsl-actions-实施纪要.md）。合法取值（宪法
R12，2026-07-28 更新）：`macOS`/`Windows`/`Linux`（具体平台，大小写不敏感）
或 `universal`/`all`（二者完全等价，表示跨平台通用，`_UNIVERSAL_OS_ALIASES`）：
  - platform_matches_host()：判断 case.os 是否与当前宿主机匹配（空/universal/
    all 总是匹配，具体平台名需与宿主机一致），用于 runner 模式的"不匹配则跳过"。
  - effective_platform_for_case()：返回 case 实际应使用的平台字符串（用于引擎
    路由 + rosetta 展平），空/universal/all 解析为宿主机平台，具体平台名原样返回。

零外部依赖（仅标准库 sys），可被 core/ 内任意层安全引用，不违反宪法 R2/R3。
"""
from __future__ import annotations

import sys

UNIVERSAL_PLATFORM = "universal"

# tcs 用例 os 字段中表示"跨平台通用"的别名集合（2026-07-28 新增 "all"，与
# "universal" 完全等价，见宪法 R12）。仅用于 platform_matches_host()/
# effective_platform_for_case() 的 tcs os 字段语义判断；不影响
# resolve_universal_platform() 的 Registry 哨兵值解析契约——该函数仅由各
# Registry 的 register() 以字面量 UNIVERSAL_PLATFORM 常量调用（非用户输入），
# 无需感知 "all" 别名。
_UNIVERSAL_OS_ALIASES = {UNIVERSAL_PLATFORM, "all"}

# sys.platform → 项目统一使用的平台字符串（与现有各 Registry key 的小写规范一致）
_HOST_PLATFORM_ALIASES = {
    "darwin": "macos",
    "win32": "windows",
    "linux": "linux",
}


def _is_universal_os(platform: str) -> bool:
    """判断 tcs case.os 字段值是否属于"跨平台通用"别名（"universal"/"all"，
    大小写不敏感）。"""
    return platform.strip().lower() in _UNIVERSAL_OS_ALIASES


def resolve_universal_platform(platform: str) -> str:
    """若 platform 为 UNIVERSAL_PLATFORM（大小写不敏感），返回当前宿主机实际
    运行平台对应的规范字符串（macos/windows/linux）；否则原样返回 platform
    （不做任何转换，保持向后兼容——非 universal 输入不受 sys.platform 影响）。

    调用时机：仅在各 Registry 的 register() 内部调用一次，即完成"决策"；
    get()/try_get() 不需要、也不应该调用此函数（它们保持精确匹配语义）。

    未识别的 sys.platform（如 cygwin）会原样返回 sys.platform 本身，不抛异常
    （当前项目仅支持 macOS/Windows，见宪法 R12，此为宽容兜底，不影响主流程）。
    """
    if platform.strip().lower() != UNIVERSAL_PLATFORM:
        return platform
    return _HOST_PLATFORM_ALIASES.get(sys.platform, sys.platform)


def platform_matches_host(platform: str) -> bool:
    """判断 tcs 用例声明的 os（platform）是否与当前宿主机匹配。

    - 空字符串 / "universal" / "all"（大小写不敏感，"all" 与 "universal"
      完全等价，2026-07-28 新增，见宪法 R12）→ 通用用例，总是匹配。
    - 具体平台名（如 "macOS"/"Windows"）→ 与当前宿主机平台字符串（忽略大小写）
      比较，一致才算匹配。

    用于 runner 模式："声明了具体平台但与当前宿主机不匹配"时应跳过整条用例。
    """
    if not platform or _is_universal_os(platform):
        return True
    return platform.strip().lower() == resolve_universal_platform(UNIVERSAL_PLATFORM)


def effective_platform_for_case(platform: str) -> str:
    """返回该 case 实际应使用的平台字符串（用于引擎路由 + rosetta 展平）。

    - 空 / "universal" / "all"（二者等价，见宪法 R12） → 解析为当前宿主机平台。
    - 具体平台名 → 原样返回（调用前应先用 platform_matches_host() 判断是否
      应该执行该 case，本函数不做匹配校验）。
    """
    if not platform or _is_universal_os(platform):
        return resolve_universal_platform(UNIVERSAL_PLATFORM)
    return platform
