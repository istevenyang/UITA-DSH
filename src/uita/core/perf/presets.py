"""性能预设的**展开机制**（一键跑全量）。

分层边界（守 R2）：
  - "跑哪些场景 / 各场景默认选项 / 哪些场景依赖样本库"是**应用知识**
    → 住在 ``apps/<app>/perf/presets.py``（如 ``sample_media``）；
  - 本模块只提供**纯函数展开**：预设 × 样本库 × 用户显式参数 → 最终场景清单与选项，
    并把"因缺素材而跳过"的场景**显式列出来**（绝不静默少测）。

为什么需要预设（此前要背一长串参数）：
    跑全量要手写 10 个场景名 + 一份全局 ``--perf-scenario-opt``（``path=``/``dir=``/``sample_dir=``
    分别属于不同场景、``expect_min`` 还要按样本库大小算）。少写一个键就少测一项，
    且失败是"静默降级"（场景返回 error 但报告仍在）。预设把这些收成一条命令：
    ``--perf-preset full --perf-lib <样本库>``。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("uita")

__all__ = [
    "PresetExpansion",
    "coerce_name_filter",
    "count_lib_files",
    "expand_preset",
]

#: 视为"照片样本"的扩展名（用于按样本库文件数推导 scan.expect_min）
PHOTO_EXTS = (".heic", ".heif", ".jpg", ".jpeg", ".png", ".dng", ".tif", ".tiff")


def count_lib_files(lib_dir: str, exts: tuple[str, ...] = PHOTO_EXTS) -> Optional[int]:
    """数出样本库里符合扩展名的文件数（用于推导 ``scan.expect_min``）。

    为什么必须推导：``expect_min`` 同时决定 ``time_to_target_ms`` 与收敛判定
    （``indexed >= expect_min`` 才认为完成）。写死 1 会让扫描"刚入库一张就收敛"，
    得到的耗时是假值；写死 50000 用在 10k 库上会一直等到超时。

    ``--release-res`` 拆分释放时 ``lib_dir`` 是 ``os.pathsep`` 分隔的多个分片目录，
    按**分片求和**（拆分后扫描路径覆盖全部分片，收敛判据必须覆盖全部分片的份数）。
    全部目录都不可用才返回 ``None``。
    """
    from uita.core.perf.lib_builder import split_lib_dirs

    dirs = split_lib_dirs(lib_dir)
    if not dirs:
        return None
    count = 0
    usable = False
    for one in dirs:
        if not os.path.isdir(one):
            continue
        usable = True
        try:
            with os.scandir(one) as it:
                for entry in it:
                    if entry.is_file() and entry.name.lower().endswith(exts):
                        count += 1
        except OSError as exc:
            logger.warning("[perf] 统计样本库文件数失败: %s (%s)", one, exc)
            return None
    return count if usable else None


@dataclass
class PresetExpansion:
    """展开结果（可直接喂给 PerfRunner）。

    **为什么选项要按场景分开**：``--perf-scenario-opt`` 是**全局扁平**字典（同名键会命中所有
    场景，靠 ``accepted_options`` 过滤），但预设里 ``count``（thumbs 与 video_decode 都有）、
    ``quiet_ms``（dir_view 与 filter 都有）这类**同名不同义**的键必须按场景隔离，
    否则一个场景的默认值会污染另一个。故：
      - :attr:`per_scenario_options`：预设给**每个场景**的默认值（隔离）
      - :attr:`scenario_options`：命令行全局覆盖（对所有场景生效，优先级更高）
    """

    scenarios: list[str] = field(default_factory=list)
    #: 命令行给的全局选项（对所有场景生效，按签名过滤）
    scenario_options: dict[str, Any] = field(default_factory=dict)
    #: 预设声明的重复轮次（``None`` = 该预设不表态，由命令行/config 默认值决定）。
    #: 必要性：静息类预设单轮就要 10 分钟，默认 3 轮毫无收益却让一次运行拖到半小时；
    #: 轮次属于"这个预设该怎么跑"的知识，应与场景清单一起声明，而不是让用户每次手记参数。
    repeat: Optional[int] = None
    #: 只在**最后一轮**执行一次的场景（其余轮次跳过）。
    #: 必要性：静息（idle）这类"等 N 分钟"的场景跨轮重复没有额外信息量，只是线性拉长总时长；
    #: 但它又必须留在全量预设里（否则"一键跑全部"会少测静息）→ 需要"留在清单里但只跑一次"。
    last_round_only: list[str] = field(default_factory=list)
    #: 预设给的按场景选项（隔离；运行时与全局选项合并，全局优先）
    per_scenario_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 场景 → 从 --perf-lib 注入的选项名（日志/报告里说明"素材从哪来"）
    lib_injected: dict[str, list[str]] = field(default_factory=dict)
    #: 因缺素材而跳过的场景：[{"scenario": ..., "reason": ...}]（必须出现在报告里）
    skipped: list[dict[str, str]] = field(default_factory=list)


def expand_preset(
    spec: dict[str, Any],
    *,
    lib: str = "",
    lib_file_count: Optional[int] = None,
    user_scenarios: Optional[list[str]] = None,
    user_options: Optional[dict[str, Any]] = None,
    available: Optional[list[str]] = None,
    only: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None,
) -> PresetExpansion:
    """把一份预设声明展开成最终场景清单与选项。

    规则（优先级从低到高）：
      0. ``only`` / ``exclude`` 对**预设声明的清单**做白/黑名单过滤（保序；二者互斥）。用途：
         同一个预设（如 ``full``）被多单元编排复用时，各单元按需取子集，**无需为每个单元
         复制一份场景清单与选项**——见 ``apps/sample_media/perf/presets.py::UNIT_SEQUENCE``
         （前四档 ``exclude=["stress"]``、压测单元 ``only=["stress"]``）。过滤发生在最前，
         故被排除的场景不参与后续注入/跳过/校验（用户显式追加的仍按规则 3 生效）
      1. 预设 ``scenarios``（顺序即执行顺序，``scan`` 必须在 ``gallery`` 前等约束由预设编排）
      2. ``--perf-lib`` 注入 ``lib_options``（如 scan.path / thumbs.dir / dir_view.sample_dir）
      3. 用户 ``--perf-scenario`` **追加**（去重保序）、``--perf-scenario-opt`` **覆盖**同名键
      4. 未提供 ``--perf-lib`` 时，``require_lib`` 里的场景被**跳过并记录原因**（不静默少测）
      5. 运行时声明（可选）：``repeat``（预设建议轮次，命令行显式给值时不生效）、
         ``last_round_only``（只在最后一轮跑一次的场景，会按最终场景清单过滤）

    Args:
        spec: 预设声明（``apps/<app>/perf/presets.py`` 里的一项）
        lib: 样本库目录（相当于同时提供 path/dir/sample_dir）
        lib_file_count: 样本库文件数（用于推导 ``scan.expect_min``；``None`` = 不推导）
        user_scenarios / user_options: 用户在命令行显式给出的场景与选项
        available: 已注册场景名列表（给了就校验，拼错场景名会**报错**而不是静默跳过）
        only / exclude: 可选的白/黑名单（只过滤预设声明的清单；互斥，同时给会抛 ``ValueError``）
    """
    only_names = coerce_name_filter(only, "only")
    exclude_names = coerce_name_filter(exclude, "exclude")
    if only_names is not None and exclude_names is not None:
        raise ValueError("preset 的 only 与 exclude 不能同时声明（语义冲突，二者只取其一）")
    names: list[str] = [str(n) for n in (spec.get("scenarios") or [])]
    if only_names is not None:
        names = [n for n in names if n in only_names]
    elif exclude_names:
        names = [n for n in names if n not in exclude_names]
    require_lib = {str(n) for n in (spec.get("require_lib") or [])}
    lib_options: dict[str, list[str]] = {
        str(scen): [str(k) for k in keys]
        for scen, keys in (spec.get("lib_options") or {}).items()
    }
    # 预设选项按场景隔离（见 PresetExpansion 文档字符串）。
    # 顶层键必须是**场景名**、值必须是**该场景的选项字典**：写成扁平的 K=V（把选项直接
    # 放在 options 下）会让下面的 dict(opts) 去迭代字符串并抛
    # "dictionary update sequence element #0 has length 1"，现场完全看不出是预设写错了
    # （真机踩到过）→ 这里显式校验并给出可定位的报错。
    per_scenario: dict[str, dict[str, Any]] = {}
    for scen, opts in (spec.get("options") or {}).items():
        if not isinstance(opts, dict):
            raise ValueError(
                f"预设 options 必须是「场景名 → 选项字典」两层结构，"
                f"但 {scen!r} 的值是 {type(opts).__name__}（{opts!r}）"
            )
        per_scenario[str(scen)] = dict(opts)
    expansion = PresetExpansion()

    if lib:
        for scen, keys in lib_options.items():
            if scen not in names:
                continue
            option_keys = dict(per_scenario.get(scen) or {})
            for key in keys:
                option_keys[key] = lib
            per_scenario[scen] = option_keys
            expansion.lib_injected[scen] = list(keys)
        # 按样本库文件数推导 scan.expect_min（用户的全局 --perf-scenario-opt 仍可覆盖）。
        # 必须用 `is not None`（而非 `if lib_file_count:`）：`lib_file_count == 0`
        # （0k 档位/空目录）是合法输入，也要把 expect_min 显式改成 0——否则会残留
        # DEFAULT_OPTIONS 里的 expect_min=1，扫描 0 张照片时永远达不到，一路卡到
        # timeout_ms（30 分钟）才超时退出，而不是正常几秒内收敛。
        if lib_file_count is not None and "scan" in names:
            scan_opts = dict(per_scenario.get("scan") or {})
            scan_opts.setdefault("expect_min", int(lib_file_count))
            scan_opts.setdefault("stable_ticks", 8)
            per_scenario["scan"] = scan_opts
    else:
        skipped: list[dict[str, str]] = []
        for scen in list(names):
            if scen in require_lib:
                skipped.append({
                    "scenario": scen,
                    "reason": "缺少 --perf-lib（该场景依赖样本库目录），已跳过",
                })
                names.remove(scen)
        expansion.skipped = skipped
        if skipped:
            logger.warning(
                "[perf] 预设未提供 --perf-lib，跳过 %d 个场景: %s",
                len(skipped), ", ".join(s["scenario"] for s in skipped),
            )

    # 用户追加的场景（去重保序）
    for scen in (user_scenarios or []):
        if scen not in names:
            names.append(scen)

    if available is not None:
        known = set(available)
        unknown = [n for n in names if n not in known]
        if unknown:
            raise ValueError(
                f"预设/命令行含未注册场景 {', '.join(unknown)}（已注册: {', '.join(sorted(known))}）",
            )

    # 全局选项：只收用户显式给的（预设默认值走 per_scenario_options，运行时按场景合并）
    expansion.scenarios = names
    expansion.scenario_options = {
        str(k): v for k, v in (user_options or {}).items() if v is not None
    }
    expansion.per_scenario_options = per_scenario
    expansion.repeat = _coerce_repeat(spec.get("repeat"))
    # 只保留最终**真的会跑**的场景名：预设写了 last_round_only 但该场景因缺样本库被跳过
    # （或被用户场景清单排除）时，留着会让产物里的口径说明与实际执行不一致。
    expansion.last_round_only = [
        str(n) for n in (spec.get("last_round_only") or []) if str(n) in names
    ]
    return expansion


def coerce_name_filter(raw: Any, label: str) -> Optional[list[str]]:
    """把 ``only`` / ``exclude`` 归一化为 ``list[str]``；``None``/缺省 → ``None``（不过滤）。

    纪律：过滤是**声明式配置**的一部分，写错类型说明配置本身有问题（不是"顺手的容错"），
    故类型不对直接抛 ``ValueError``（与 :func:`_coerce_repeat` 的"非法只告警"不同——那里是
    单个数值的降级，这里错的是整个单元的场景子集，静默吞掉会变成"悄悄多测/少测"）。

    公开导出（非下划线）：``core/perf/scenarios/app_loader.py`` 读 app 侧
    ``UNIT_SEQUENCE`` 时复用同一份校验语义，避免两处各写一套规则而彼此漂移。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        # 单个场景名的简写也接受（如 only="stress"）
        return [raw] if raw.strip() else []
    if not isinstance(raw, (list, tuple, set)):
        raise ValueError(f"preset 的 {label} 必须是场景名列表，实际是 {type(raw).__name__}")
    return [str(n) for n in raw]


def _coerce_repeat(raw: Any) -> Optional[int]:
    """把预设声明的 ``repeat`` 归一化为正整数；非法值记警告并返回 ``None``（不抛）。

    纪律：预设是**声明式配置**，一个手写错的值不该让整次 perf 运行直接崩——回落到
    "该预设不表态"（由命令行/config 默认值接管）并显式告警，是可观测且安全的降级。
    """
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("[perf] 预设 repeat 非法（%r 不可转为整数），已忽略", raw)
        return None
    if value < 1:
        logger.warning("[perf] 预设 repeat 非法（%d < 1），已忽略", value)
        return None
    return value
