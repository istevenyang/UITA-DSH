#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cli/modes/perf_mode.py — ``--mode perf`` 处理器（薄封装）。

职责：参数校验/兜底 → 复用既有 ``--app``/``--cases-dir`` 用例选择能力 → 交给
``core/perf/runner.py::PerfRunner`` 编排（多轮重复 + 冷启动计时 + 进程树资源采样 +
基线判定）→ 打印结论。

用法::

    uita --mode perf --app sample_web --cases-dir cases/sample_web/ \\
         --perf-repeat 3 --perf-cold-start

退出码约定：
  - 用例执行本身失败（TCS 断言失败）→ **仍返回 0**（与 ``--mode runner`` 一致，
    避免改变既有语义；通过率由 result.json 承载）。
  - **性能判定已停用**（2026-10-08 用户要求"不要门禁、不要 Pxx、只要真实值"）：
    本模式不再因 perf 结果返回非 0（原先 ``mode=strict`` 超阈值返回 1 的性能门禁行为
    随之取消）。判定实现仍在 ``core/perf/baseline.py::evaluate``，需要重开时按其模块
    docstring 接回即可。产物只给真实值（逐轮实测 + count/min/avg/max）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from typing import Any, Optional

from uita.cli.modes.common import (
    StartupError,
    _apply_main_app_version,
    _collect_tcs_files,
    _expand_cases_dir,
    _resolve_case_name_tokens,
)

logger = logging.getLogger("uita")

__all__ = ["_handle_perf"]

#: **缺省回落**的多单元顺序（升序，与 perf_baseline.json::tagged_thresholds
#: 的 lib-0k/lib-5k/lib-10k/lib-50k 完全对应）。``0k``（空库）打头，
#: 作为"从 0 到有"的基准点；不建库、不要求 --perf-src，其余增量扩容顺序不变。
#:
#: 多单元编排序列优先取 **app 侧声明**
#: （``apps/<app>/perf/presets.py::UNIT_SEQUENCE``，见
#: ``core/perf/scenarios/app_loader.py::load_app_unit_sequence``）——单元里可能出现
#: **应用自己的场景名**（如 ``stress`` 独立成单元），属应用知识，不能写进
#: core（守 R2）。本常量只在 app **未声明**时使用，保证既有 app 的行为零回归。
_MULTI_TIER_ORDER = ("0k", "5k", "10k", "50k")

#: ``_resolve_preset(lib=...)`` 的哨兵默认值：区分"调用方没传 lib（回退读 cli.perf_lib）"
#: 与"调用方明确传了空串 lib（如 0k 档位主动清空，绝不能再退回 cli.perf_lib）"——
#: 二者都是空串会撞在一起，必须用哨兵对象区分（2026-09-21，0k 档位引入后暴露的坑）。
_LIB_UNSET = object()


def _resolve_perf_files(cli, *, app_name: str = "") -> list[str]:
    """解析待执行 .tcs 列表（与 ``runner_mode._resolve_runner_files`` 同逻辑）。

    未显式给 ``--cases-dir`` 时，回退到 app 预设声明的 ``default_case``
    （app 侧知识，如 ``smoke_home.tcs``）——
    这样"一键跑"只需 ``--app`` + 档位 + 资源目录。给了 ``--perf-preset`` 时
    以**该预设**声明的 ``default_case`` 为准（见 :func:`_app_default_case`）。
    """
    app = (getattr(cli, "app", "") or app_name or "").strip()
    preset_name = (getattr(cli, "perf_preset", "") or "").strip()
    raw = list(getattr(cli, "cases_dir", None) or [])
    # ⚠ argparse 的 `--cases-dir` 默认值是 `["cases"]`（不是空列表），必须把"默认值"
    #   当作"没给用例"处理，否则 `_expand_cases_dir(["cases"])` 会展开成**整个用例库**
    #   （实测 cases/ 下 1158 个 .tcs → 一次 perf 运行会去执行整库 × repeat 轮）。
    #   这也是"单测用手写 stub 不覆盖真实默认值"踩出来的坑 → 另有基于 build_parser()
    #   的回归用例钉住。
    is_default_marker = raw == ["cases"]
    if (not raw or is_default_marker) and app:
        default_case = _app_default_case(app, preset_name=preset_name)
        if default_case:
            raw = [default_case]
            print(f"[perf] 未指定 --cases-dir，使用 app 预设默认用例: {default_case}")
    if not raw:
        raise StartupError(
            "perf 模式需要用例：给 --cases-dir，或让 app 预设声明 default_case"
            f"（当前 app={app or '未指定'}；**不会**默认跑全部 cases/）",
        )
    if raw == ["cases"]:
        hint = (
            f"app {app!r} 未声明 default_case，请显式给 --cases-dir"
            if app else "请给 --app 或明确的 --cases-dir/--case-dir"
        )
        raise StartupError(f"拒绝默认跑整个 cases/ 目录：{hint}（避免误跑全部用例）")
    tokens = _expand_cases_dir(raw)
    tokens = _resolve_case_name_tokens(tokens, app)
    files = _collect_tcs_files(tokens)
    if not files:
        raise StartupError("未找到可执行的 .tcs 用例（--cases-dir/--app 未匹配到任何文件）")
    return files


def _app_default_case(app_name: str, *, preset_name: str = "") -> str:
    """取 app 预设里的 ``default_case``（无预设/未声明则返回空串）。

    给了 ``preset_name`` 时**优先**取该预设自己的 ``default_case``：预设可以要求特定用例
    （如静息专项需要一条极简用例），而"遍历取首个非空"永远返回字典里第一个预设的用例，
    与 ``--perf-preset`` 选中的预设无关。取不到再退回旧的"首个非空"行为（零回归）。
    """
    from uita.core.perf.scenarios import load_app_presets

    presets = load_app_presets(app_name)
    if preset_name:
        case = str((presets.get(preset_name) or {}).get("default_case") or "").strip()
        if case:
            return case
    for spec in presets.values():
        case = str((spec or {}).get("default_case") or "").strip()
        if case:
            return case
    return ""


def _resolve_app(cli, files: list[str]) -> str:
    """app 三级兜底：``--app`` → 首个 .tcs 的 ``app`` 头 → 路径推断 → ``sample_web``。"""
    explicit = (getattr(cli, "app", "") or "").strip()
    if explicit:
        return explicit
    from uita.core.dsl.parser import infer_app_from_path, parse_file

    try:
        case = parse_file(files[0])
        return case.app or infer_app_from_path(files[0]) or "sample_web"
    except Exception:  # noqa: BLE001 - 解析失败时退回路径推断
        return infer_app_from_path(files[0]) or "sample_web"


def _resolve_int(cli_value, default: int, name: str, minimum: int = 1) -> int:
    value = default if cli_value is None else int(cli_value)
    if value < minimum:
        raise StartupError(f"--{name} 需 ≥ {minimum}，当前为 {value}")
    return value


def _fmt_ms(value: Any) -> str:
    """毫秒值的终端展示：缺失记 ``—``（不记 0，避免把"未测到"读成"极快"）。"""
    if value is None:
        return "—"
    try:
        return f"{float(value):.0f}ms"
    except (TypeError, ValueError):
        return str(value)


def _fmt_num(value: Any, unit: str = "") -> str:
    """数值终端展示：缺失记 ``—``（同为"未测到 ≠ 0"口径，见 :func:`_fmt_ms`）。"""
    if value is None:
        return "—"
    try:
        return f"{float(value):.0f}{unit}"
    except (TypeError, ValueError):
        return str(value)


def _parse_scenarios(cli) -> tuple[list[str], dict]:
    """解析 ``--perf-scenario`` / ``--perf-scenario-opt``（未知场景直接报错，避免静默跳过）。

    注意：场景是 **app 侧** 注册的（``apps/<app>/perf/scenarios/``），故调用方必须
    先执行 ``load_app_scenarios(app)``，否则合法场景名会被误判为"未知场景"。
    """
    from uita.core.perf.scenarios import available_scenarios

    raw = (getattr(cli, "perf_scenario", "") or "").replace("，", ",")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    known = set(available_scenarios())
    unknown = [n for n in names if n not in known]
    if unknown:
        raise StartupError(
            f"未知场景 {', '.join(unknown)}（当前支持: {', '.join(sorted(known))}）",
        )

    # 只在「逗号后面紧跟 key=」时才当分隔符：值本身可能带逗号（如 keywords=a,b）。
    # 早期实现直接 split(",")，导致 keywords=sim_1,zzzz_no_match 被拆成两段并报错（真机踩到）。
    raw_opt = (getattr(cli, "perf_scenario_opt", "") or "").replace("，", ",")
    pairs = [p for p in re.split(r",(?=[A-Za-z_][A-Za-z0-9_]*=)", raw_opt) if p.strip()]
    options: dict = {}
    for pair in pairs:
        pair = pair.strip()
        if "=" not in pair:
            raise StartupError(f"--perf-scenario-opt 需要 K=V 形式，收到: {pair!r}")
        key, _, raw_value = pair.partition("=")
        key, raw_value = key.strip(), raw_value.strip()
        value: object = raw_value
        try:
            value = int(raw_value)
        except ValueError:
            try:
                value = float(raw_value)
            except ValueError:
                value = raw_value
        options[key] = value
    return names, options


def _collect_lib_options(presets: dict) -> dict[str, list[str]]:
    """汇总 app 各预设声明的 ``lib_options``（场景 → 需要注入样本库路径的选项名）。

    用于"只给了 ``--perf-scenario`` 没给 ``--perf-preset``"的路径：此时仍要让
    ``--perf-lib/--perf-tier`` 生效，否则 scan/thumbs 拿不到样本库目录。
    合成逻辑集中在 app 侧预设里（core 不内置任何应用知识）。
    """
    merged: dict[str, list[str]] = {}
    for spec in (presets or {}).values():
        if not isinstance(spec, dict):
            continue
        for scen, keys in (spec.get("lib_options") or {}).items():
            bucket = merged.setdefault(str(scen), [])
            for key in keys:
                if str(key) not in bucket:
                    bucket.append(str(key))
    return merged


def _resolve_preset(
    cli, app_name: str, *, lib: Any = _LIB_UNSET, preset_override: str = "",
    only: Optional[list[str]] = None, exclude: Optional[list[str]] = None,
):
    """展开 ``--perf-preset``（含样本库注入）；无预设时等价于纯命令行解析。

    Args:
        lib: 已解析（必要时已构建）的样本库目录。**不传该参数**时回退读取
            ``cli.perf_lib``（供单测/简单场景直接调用）；**显式传空串**
            （如 0k 空库档位主动清空）时严格按空串处理，**不会**再回退读取
            ``cli.perf_lib``——否则 0k 档位"忽略已传入的 --perf-lib"会在这里
            被悄悄捡回来，重新给依赖库的场景注入路径（2026-09-21 修复）。
        preset_override: 多单元编排里**单元声明**的预设名（``UNIT_SEQUENCE[].preset``）。
            优先级低于命令行 ``--perf-preset``（用户显式意图最高），高于"未指定则默认 full"。
        only / exclude: 单元声明的场景清单白/黑名单，透传给 :func:`expand_preset`
            （只过滤该预设声明的清单，用户 ``--perf-scenario`` 追加的不受影响）。

    Returns:
        ``(expansion, preset_name)``：``expansion`` 为
        :class:`uita.core.perf.presets.PresetExpansion`（场景清单 + 全局选项 +
        按场景选项 + 跳过项）；无预设时给一个只含命令行场景/选项的空预设展开。
    """
    from uita.core.perf.presets import PresetExpansion, count_lib_files, expand_preset
    from uita.core.perf.scenarios import (
        available_scenarios,
        load_app_default_preset,
        load_app_presets,
    )

    # 命令行显式给的预设优先于单元声明（用户意图最高）；单元声明再优先于"默认 full"。
    preset_name = (getattr(cli, "perf_preset", "") or "").strip() or (preset_override or "").strip()
    if lib is _LIB_UNSET:
        lib = (getattr(cli, "perf_lib", "") or "").strip()
    else:
        lib = (lib or "").strip()
    user_names, user_options = _parse_scenarios(cli)
    presets = load_app_presets(app_name)
    if not preset_name and not user_names:
        # 一键默认：既没给 --perf-preset 也没给 --perf-scenario 时，
        # ① app 声明 DEFAULT_PRESET（apps/<app>/perf/presets.py）→ 用它的场景化预设；
        # ② 否则回溯既有行为：app 提供 full → 用 full。
        # 声明了不存在的预设名属配置错误，直接报错（不要静默跑空场景）。
        declared = load_app_default_preset(app_name)
        if declared:
            if declared not in presets:
                raise StartupError(
                    f"app={app_name} 声明的 DEFAULT_PRESET={declared!r} 不在其 PRESETS 中"
                    f"（现有：{', '.join(sorted(presets)) or '无'}）"
                )
            preset_name = declared
            print(f"[perf] 未指定 --perf-preset/--perf-scenario，默认使用预设 {declared}")
        elif "full" in presets:
            preset_name = "full"
            print("[perf] 未指定 --perf-preset/--perf-scenario，默认使用预设 full")
    if not preset_name:
        # 显式给了场景但没给预设：仍按 app 声明的 lib_options **注入样本库路径**，
        # 否则 `--perf-tier/--perf-src` 建好的库不会被 scan/thumbs 用上
        # （scan 会因缺 path 直接报错）——"给了档位和源目录就应该开箱可用"。
        lib_options = _collect_lib_options(presets)
        if lib and lib_options:
            if not _lib_ready(lib):
                raise StartupError(f"--perf-lib 目录不存在: {lib}")
            spec = {"scenarios": user_names, "lib_options": lib_options}
            expansion = expand_preset(
                spec,
                lib=lib,
                lib_file_count=count_lib_files(lib),
                user_options=user_options,
                available=available_scenarios(),
            )
            for scen, keys in expansion.lib_injected.items():
                print(f"[perf] 样本库注入：{scen} ← {', '.join(keys)} = {lib}")
            expect_min = (expansion.per_scenario_options.get("scan") or {}).get("expect_min")
            if expect_min is not None:
                print(f"[perf] scan.expect_min 自动推导 = {expect_min}（样本库文件数）")
            return expansion, ""
        return PresetExpansion(scenarios=user_names, scenario_options=dict(user_options)), ""

    if not presets:
        raise StartupError(
            f"--perf-preset 需要 app 侧预设模块（apps/{app_name}/perf/presets.py），当前 app 未提供",
        )
    spec = presets.get(preset_name)
    if not isinstance(spec, dict):
        raise StartupError(f"未知预设 {preset_name!r}（可用: {', '.join(sorted(presets))}）")
    if lib and not _lib_ready(lib):
        raise StartupError(f"--perf-lib 目录不存在: {lib}")

    expansion = expand_preset(
        spec,
        lib=lib,
        lib_file_count=count_lib_files(lib) if lib else None,
        user_scenarios=user_names,
        user_options=user_options,
        available=available_scenarios(),
        only=only,
        exclude=exclude,
    )
    if expansion.lib_injected:
        for scen, keys in expansion.lib_injected.items():
            print(f"[perf] 样本库注入：{scen} ← {', '.join(keys)} = {lib}")
    if lib and "scan" in expansion.per_scenario_options:
        expect_min = expansion.per_scenario_options["scan"].get("expect_min")
        if expect_min is not None:
            print(f"[perf] scan.expect_min 自动推导 = {expect_min}（样本库文件数）")
    for item in expansion.skipped:
        print(f"[perf] ⚠ 跳过场景 {item['scenario']}：{item['reason']}")
    return expansion, preset_name


def _warn_idle_scope(
    scenarios: list[str], expansion, *, steady_state_s: float, cold_start: bool,
) -> None:
    """静息口径的两条护栏告警（**只告警、不改判定**，避免静默测出不可比的数据）。

    1. 两套 idle 机制并存：``--perf-steady-state-s``（idle **阶段**）与 ``idle`` **场景**
       共用同一组扁平字段 ``idle_rss_mb_*``/``idle_cpu_percent_*``，聚合时阶段值优先
       （见 ``core/perf/runner.py::_aggregate``）→ 同时启用会让顶层读数只反映阶段窗口。
    2. 关了冷启动（``--no-perf-cold-start``）时，热身轮之后不会重启应用，静息测的是
       "被热身轮用过一遍的进程"，与"重新启动后关窗常驻"不是同一口径。
    """
    if "idle" not in scenarios:
        return
    if steady_state_s > 0:
        print(
            f"[perf] ⚠ 同时启用了 idle 场景与 --perf-steady-state-s {steady_state_s:g}s："
            "两者共用 idle_rss_mb_*/idle_cpu_percent_* 字段，顶层读数以静置阶段窗口为准"
            "（idle 场景数据仍完整保留在 scenarios.idle / resources_by_scenario.idle）",
        )
    if not cold_start:
        print(
            "[perf] ⚠ idle 场景 + --no-perf-cold-start：热身轮后不会重新启动应用，"
            "测的不是\"重新启动后关窗常驻\"的口径",
        )


def _lib_ready(lib: str) -> bool:
    """样本库字符串是否**至少有一个**存在的目录（``--release-res`` 拆分时可能多目录）。"""
    from uita.core.perf.lib_builder import split_lib_dirs

    return any(os.path.isdir(one) for one in split_lib_dirs(lib))


def _release_res_letter(cli) -> str:
    """归一化并校验 ``--release-res``，返回大写盘符（未给返回空串）。

    校验时机：在宿主基准/用例执行**之前**（见 ``_handle_perf``）——盘符拼错或平台不支持
    属"启动失败"（退出码 1），不能等跑完半场才发现素材没落到目标盘。
    结果记忆在 ``cli`` 上：多单元编排里每个单元都会走 ``_prepare_lib``，不必重复枚举磁盘。
    """
    cached = getattr(cli, "_perf_release_letter", None)
    if cached is not None:
        return str(cached)
    raw = (getattr(cli, "release_res", "") or "").strip()
    if not raw:
        setattr(cli, "_perf_release_letter", "")
        return ""

    from uita.core.perf.release_res import ReleaseResError, drive_root, normalize_drive

    try:
        letter = normalize_drive(raw)
    except ReleaseResError as exc:
        raise StartupError(str(exc)) from exc
    if sys.platform != "win32":
        raise StartupError(
            f"--release-res 仅支持 Windows（按盘符释放素材），当前平台 {sys.platform}；"
            "macOS/Linux 省略该参数即可（样本库默认落在用户图片目录）",
        )
    root = drive_root(letter)
    if not os.path.isdir(root):
        raise StartupError(f"--release-res {raw}: 盘符 {letter}: 不存在或不可访问")
    setattr(cli, "_perf_release_letter", letter)
    return letter


def _release_target_count(cli, size: int) -> int:
    """本次规划按多少份做：多单元编排取**最大单元档位**（各单元从同一份规划分配）。"""
    max_count = int(getattr(cli, "_perf_release_max_count", 0) or 0)
    return max(max_count, int(size))


def _release_plan(cli, *, letter: str, src: str, count: int):
    """规划释放位置（体积估算 + 空间判定 + 拆分），结果缓存在 ``cli`` 上。

    缓存必要性：多单元编排（0k→5k→10k→50k）逐单元调用 ``_prepare_lib``，若每次重新规划，
    先跑的 0k 单元会把规划钉在"单盘放得下"，到 50k 单元才发现放不下、临时改盘——
    同一场运行的样本库被拆到不同盘的时机不一致，产物里的样本库路径也会前后漂移。
    """
    cached = getattr(cli, "_perf_release_plan", None)
    if cached is not None and int(getattr(cached, "target_count", 0) or 0) >= int(count):
        return cached

    from uita.core.perf.release_res import (
        ReleaseResError,
        plan_release,
        scan_source,
        summarize_plan,
    )

    try:
        stats = scan_source(src)
        plan = plan_release(letter, int(count), stats)
    except ReleaseResError as exc:
        raise StartupError(f"--release-res {letter}: {exc}") from exc
    for note in plan.notes:
        print(f"[perf] {note}")
    print(f"[perf] 释放规划：{summarize_plan(plan)}")
    setattr(cli, "_perf_release_plan", plan)
    return plan


def _release_part_targets(plan, lib_dirs: list[str], size: int) -> list[tuple[str, int]]:
    """本单元要复制的份数 → 各分片目录（无释放规划时 = 单目录复制全量）。"""
    if plan is None:
        return [(lib_dirs[0], int(size))] if lib_dirs else []

    from uita.core.perf.release_res import ReleaseResError, allocate_counts

    try:
        counts = allocate_counts(plan, int(size))
    except ReleaseResError as exc:
        raise StartupError(str(exc)) from exc
    return [(part.root, count) for part, count in zip(plan.parts, counts)]


def _cleanup_auto_lib(cli, lib: str, *, auto_built: bool) -> None:
    """跑完销毁自动构建的样本库（``<pictures>/perf`` 或 ``<盘>:\\perf``；``--perf-keep-lib`` 可保留）。

    只销毁**本次由工具构建**的库：``--perf-lib`` 显式指定的目录永不删除
    （销毁函数另有目录名 / 父目录 / 文件命名三重护栏，双保险）。
    ``--release-res`` 拆分释放时有多个分片目录，逐个销毁（父目录各自校验：盘根）。
    """
    if not auto_built or not lib:
        return
    if bool(getattr(cli, "perf_keep_lib", False)):
        print(f"[perf] --perf-keep-lib：保留样本库 {lib}")
        return
    from uita.core.perf.lib_builder import remove_sample_lib, split_lib_dirs, user_pictures_dir

    release = getattr(cli, "_perf_release_plan", None) is not None
    for one in split_lib_dirs(lib):
        expect_parent = os.path.dirname(os.path.abspath(one)) if release else user_pictures_dir()
        # 先打"准备销毁"（删除 1 万个克隆文件需数秒，先打日志才能在进程被外部终止时看出是没跑到）
        print(f"[perf] 销毁自动构建的样本库: {one} …", flush=True)
        try:
            result = remove_sample_lib(one, expect_parent=expect_parent)
        except BaseException as exc:  # noqa: BLE001 - 清理失败绝不允许拖垮主流程（含 IDE 护栏的 SystemExit）
            result = {"removed": False, "reason": f"{type(exc).__name__}: {exc}"}
        if result.get("removed"):
            print(
                f"[perf] 已销毁自动构建的样本库: {one}"
                f"（{result['files']} 个文件，表观 {result['apparent_mb']}MB，方式 {result.get('method')}）",
                flush=True,
            )
        else:
            hint = f'rd /s /q "{one}"' if sys.platform == "win32" else f"rm -rf '{one}'"
            print(
                f"[perf] ⚠ 样本库未能自动销毁: {one}（{result.get('reason')}）\n"
                f"       请手动删除: {hint}",
                flush=True,
            )


def _prepare_lib(
    cli, *, tier_override: str = "", tag_override: str = "",
) -> tuple[str, str, bool]:
    """解析样本库与档位（必要时**构建**样本库）。

    三种用法：
      A) **规模档位**（推荐，最小命令）：``--perf-tier 5k|10k|50k`` + ``--perf-src <真实照片目录>``
         → 自动把源照片复制成 N 份到 ``<tmp>/uita-perf-lib-<tier>``（macOS 用 APFS 克隆，
         秒级且几乎不占盘），档位映射为基线档位 ``lib-<tier>``；已存在则**幂等跳过**。
      B) **空库档位**：``--perf-tier 0k``——只跳过"复制样本文件"这一步，**不要求**
         ``--perf-src``；目录解析/``--perf-lib`` 使用/清理时机与其它规模档位**完全一致**
         （给了 ``--perf-lib`` 就直接用该目录，不给就落到共享的 ``auto_lib_dir()``，不做
         强制清空）。测的是应用刚安装/图库为空（或复用已有目录当前实际内容）时的性能基线，
         可与 5k/10k/50k 对比。
      C) 显式：``--perf-lib <目录>``（+ ``--perf-tier <基线档位名>``），行为不变。
      D) **盘符定向释放**（Windows 独有）：``--release-res e`` → 样本库落到 ``E:\\perf``；
         该盘放不下时改用其他足够大的盘（warning 日志），所有单盘都放不下时按各盘
         ``min(剩余空间, 80% 容量)`` 拆分成多个 ``<盘>:\\perf``（见
         ``core/perf/release_res.py``）。多分片以 ``;`` 拼接的**单字符串**返回，
         故本函数签名不变；显式给 ``--perf-lib`` 时 ``--release-res`` 不生效。

    **必须用真实照片**（真机教训）：合成图（ffmpeg testsrc2 + sips）在应用内可能渲染不出来，
    会让滑动帧率变成「渲染空白卡片」的假值。

    Args:
        tier_override: 显式指定**库规格**档位，优先于 ``cli.perf_tier``/``cli.perf_baseline_tag``。
            供多单元编排（:func:`_handle_perf_multi_tier`）逐单元调用时使用，
            单档路径不传（保持 ``cli`` 原生取值不变）。
        tag_override: 显式指定**档位标签**（进产物 ``baseline.tag`` 与 CSV 的 ``tier`` 列）。
            仅当单元的库规格与其身份不一致时才需要——如压测单元库用 ``10k``、身份叫
            ``stress``：沿用 ``lib-10k`` 会与 10k 单元在合并 CSV 里**撞名**（两档数据混在
            一起无法区分）。缺省空串 → 按库规格推成 ``lib-<tier>``（既有产物口径零变化）。

    Returns:
        ``(lib_dir, tier_name, auto_built)``（``tier_name`` 为档位标签，受 ``tag_override``
        影响）；``auto_built=True`` 表示库目录是本次由工具
        按档位自动准备的（未显式给 ``--perf-lib``，跑完由 :func:`_cleanup_auto_lib` 销毁），
        ``--perf-lib`` 显式指定时为 ``False``（0k 档位同样遵循此规则，不再有特例）。
    """
    from uita.core.perf.lib_builder import (
        auto_lib_dir,
        build_sample_lib,
        join_lib_dirs,
        split_lib_dirs,
        tier_size,
    )

    tier = (tier_override or "").strip() or (getattr(cli, "perf_tier", "") or "").strip() or (
        getattr(cli, "perf_baseline_tag", "") or ""
    ).strip()
    lib = (getattr(cli, "perf_lib", "") or "").strip()
    src = (getattr(cli, "perf_src", "") or "").strip()
    size = tier_size(tier)
    release_letter = _release_res_letter(cli)
    # `size is None` = 不是规模档位（如基线档位名/未知名字）；`size == 0` = 合法的
    # "空库"档位（0k，只跳过复制这一步，其余流程与其它规模档位统一处理，不单独分支）。
    if size is None:
        return lib, (tag_override or tier), False

    user_lib = bool(lib)
    if user_lib and release_letter:
        # 显式样本库语义是"只测这一份数据"，且该目录**永不自动销毁** → 盘符释放不生效
        print(
            "[perf] ⚠ 已显式给 --perf-lib，--release-res 不生效"
            "（显式样本库优先，且该目录永不自动销毁）",
        )
        release_letter = ""

    plan = None
    if not user_lib:
        if size > 0 and not src:
            raise StartupError(
                f"--perf-tier {tier} 需要 --perf-src <图片目录>：会从该目录复制 {size} 份到"
                "当前用户图片目录下的 perf/（macOS: ~/Pictures/perf），跑完自动销毁",
            )
        if release_letter:
            plan = _release_plan(
                cli, letter=release_letter, src=src, count=_release_target_count(cli, size),
            )
            lib = join_lib_dirs(plan.roots)
        else:
            lib = auto_lib_dir()
    lib_dirs = split_lib_dirs(lib)

    if size == 0:
        # 0k：跳过复制这一步，其余（目录解析/校验/清理时机）与其它规模档位完全一致，
        # 不做"强制清空为 0 文件"的额外处理（目录若有残留内容原样保留）。
        if user_lib and not _lib_ready(lib):
            raise StartupError(f"--perf-lib 目录不存在: {lib}")
        if not user_lib:
            for one in lib_dirs:
                os.makedirs(one, exist_ok=True)
    elif src:
        targets = [(one, target) for one, target in _release_part_targets(plan, lib_dirs, size)
                   if target > 0]
        part_total = len(targets)
        for index, (one, target) in enumerate(targets, start=1):
            suffix = f"（分片 {index}/{part_total}）" if part_total > 1 else ""
            print(
                f"[perf] 构建样本库：{src} → {one}{suffix}"
                f"（从源素材复制 {target} 份；跑完自动销毁该目录）",
                flush=True,
            )
            stat = build_sample_lib(src, one, target)
            if stat.get("error"):
                raise StartupError(f"样本库构建失败: {stat['error']}")
            print(
                f"[perf] 样本库就绪{suffix}：新增 {stat['photos_created']} 份 / "
                f"已有 {stat['existing_photos']} 份"
                f"（源样本 {stat.get('source_samples')} 个），视频 {stat['videos_created']} 个，"
                f"复制方式 {stat['clone_method'] or '—'}，耗时 {stat['elapsed_ms']}ms"
                + ("（已存在，跳过复制）" if stat["skipped"] else ""),
            )
            if stat["errors"]:
                print(f"[perf] ⚠ 有 {len(stat['errors'])} 个文件复制失败（首条: {stat['errors'][0][:100]}）")
        if part_total > 1:
            print(
                f"[perf] 拆分释放完成：{size} 份分布在 {part_total} 个目录 → "
                + " + ".join(one for one, _target in targets),
            )
    elif not _lib_ready(lib):
        raise StartupError(
            f"--perf-tier {tier} 需要 --perf-src <图片目录> 以构建样本库"
            f"（或先准备好 {lib} 并用 --perf-lib 指向它）",
        )
    if user_lib:
        print(f"[perf] 使用显式样本库 {lib}（--perf-lib 指定的目录**不会**自动销毁）")
    return lib, (tag_override or f"lib-{tier}"), (not user_lib)


def _resolve_tier(cli, app_name: str, *, platform: str, lib: str, baseline_path: str) -> str:
    """解析规模档位：``--perf-tier`` > ``--perf-baseline-tag`` > 由 ``--perf-lib`` 目录名推断。

    自动推断的必要性（"一键跑只需 app + 档位 + 资源目录"）：档位名与样本库目录名通常同源
    （``/tmp/demo-lib-10k`` ↔ ``lib-10k``）。目录名恰好存在于基线 ``tagged_thresholds`` 时
    直接采用并打印，避免"忘了给档位 → 规模相关指标用通用阈值判定"的静默偏差。
    """
    from uita.core.perf.baseline import available_tags
    from uita.core.perf.lib_builder import split_lib_dirs

    tier = (getattr(cli, "perf_tier", "") or "").strip() or (
        getattr(cli, "perf_baseline_tag", "") or ""
    ).strip()
    if tier:
        return tier
    if not lib:
        return ""
    tags = available_tags(app_name, platform=platform, explicit_path=baseline_path)
    # 拆分释放时 lib 是多目录串：目录名推断只看第一个分片（各分片目录名同为 perf，
    # 本就推不出档位，这里只为保持"单目录"路径的原口径）
    lib_dirs = split_lib_dirs(lib)
    candidate = os.path.basename(os.path.normpath(lib_dirs[0])).strip() if lib_dirs else ""
    if candidate and candidate in tags:
        print(f"[perf] 档位由样本库目录名自动推断: {candidate}（可用档位: {', '.join(tags)}）")
        return candidate
    if tags:
        print(f"[perf] ⚠ 未指定档位（--perf-tier），样本库目录名 {candidate!r} 不在基线档位 {tags} 中"
              " → 规模相关指标将用通用阈值判定")
    return ""


def _is_multi_tier(cli) -> bool:
    """是否触发默认多档位编排：未显式给档位/样本库，但给了 ``--perf-src``。

    三者同时满足才生效（避免影响现有用法）：
      - ``--perf-tier``/``--perf-baseline-tag`` 均未传
      - ``--perf-lib`` 未传（用户显式给了固定样本库目录，语义上就是"只测这一份数据"）
      - ``--perf-src`` 已传（否则连一份样本库都建不出来，维持现状报错）
    """
    explicit_tier = (getattr(cli, "perf_tier", "") or "").strip() or (
        getattr(cli, "perf_baseline_tag", "") or ""
    ).strip()
    explicit_lib = (getattr(cli, "perf_lib", "") or "").strip()
    src = (getattr(cli, "perf_src", "") or "").strip()
    return not explicit_tier and not explicit_lib and bool(src)


def _resolve_units(app_name: str) -> list[dict[str, Any]]:
    """取多单元编排序列：app 侧 ``UNIT_SEQUENCE`` 优先，缺省回落 core 默认规模档位序列。

    **为什么序列要下沉到 app 侧**：单元声明里会出现**应用自己的场景名**（如压测
    ``stress`` 独立成单元，并声明它用哪份库规格/哪个预设），属应用知识（守 R2：``core/``
    不得认识某 app 的场景名）。core 只提供通用机制
    （``core/perf/scenarios/app_loader.py::load_app_unit_sequence``）+ 默认兜底。

    Returns:
        归一化后的单元列表；app 未声明（**常态**）时给出 ``0k/5k/10k/50k`` 四个规模单元的
        等价声明（``lib_tier`` = 单元名、``tag`` 空 → 档位标签仍推成 ``lib-<tier>``），
        与引入单元序列之前的既有产物口径**完全一致**（零回归）。
    """
    from uita.core.perf.scenarios import load_app_unit_sequence

    units = load_app_unit_sequence(app_name)
    if units:
        return units
    return [
        {"name": t, "lib_tier": t, "tag": "", "preset": "", "only": None, "exclude": None}
        for t in _MULTI_TIER_ORDER
    ]


def _print_run_setup(prefix: str, setup: dict[str, Any]) -> None:
    """批次级环境准备结果打到控制台（现场据此确认"这批是在什么客户端口径下跑的"）。"""
    if setup.get("errors"):
        print(
            f"[perf] {prefix}⚠ 客户端环境准备未完全成功："
            + "；".join(str(e) for e in setup["errors"]),
        )
    applied = setup.get("applied") or {}
    if applied:
        detail = "、".join(f"{k}={v}" for k, v in applied.items())
        print(f"[perf] {prefix}客户端配置已写入 {setup.get('path') or '(未知路径)'}：{detail}")
    elif setup.get("reason"):
        print(f"[perf] {prefix}客户端配置未改动：{setup['reason']}")
    elif setup.get("error"):
        print(f"[perf] {prefix}⚠ 客户端环境准备异常（按现状继续）：{setup['error']}")


def _resolve_warmup(cli, app_name: str) -> bool:
    """是否跑热身轮：命令行显式 > app 声明 > config 全局默认。

    为什么由 app 声明（``apps/<app>/perf/presets.py::APP_DEFAULTS["warmup"]``）：热身轮是否
    值得跑取决于该 app 的测量口径——每轮本来就以"磁盘缓存关闭"冷启动的应用（如
    ``--use-cache false``），再跑一轮冷启动去"吸收首次开销"只是白多一轮、白清一次环境。
    app 未声明时返回全局默认（既有行为，零回归）。
    """
    from uita.config import PERF_WARMUP_ENABLED

    explicit = getattr(cli, "perf_warmup", None)
    if explicit is not None:
        return bool(explicit)
    from uita.core.perf.scenarios import load_app_warmup_default

    declared = load_app_warmup_default(app_name)
    if declared is None:
        return bool(PERF_WARMUP_ENABLED)
    if bool(declared) != bool(PERF_WARMUP_ENABLED):
        print(
            f"[perf] 热身轮取 app {app_name} 声明的默认值 warmup={bool(declared)}"
            f"（覆盖全局 UITA_PERF_WARMUP={int(PERF_WARMUP_ENABLED)}）",
        )
    return bool(declared)


def _run_context(cli, load_mode: str = "") -> dict[str, Any]:
    """命令行给的**批次级参数**（透传给 app 侧 ``run_setup(ctx)``）。

    两个参数：``use_cache``（``--use-cache``，默认 False 冷缓存）/ ``load_mode``（``--load-mode``）。
    二者都是"批次开始前写一次"的口径，不参与逐轮流程。

    Args:
        load_mode: 本单元的预加载范围（多单元编排里由 :func:`_resolve_load_modes` 逐个给出）；
            空串 = 该 app 没有这个维度（不写进 ctx）。
    """
    context: dict[str, Any] = {"use_cache": bool(getattr(cli, "use_cache", False))}
    mode = str(load_mode or "").strip().lower()
    if mode:
        context["load_mode"] = mode
    return context


def _resolve_load_modes(cli, app_name: str) -> list[str]:
    """解析本批要跑的预加载范围：``--load-mode all``（默认）→ app 声明的全部取值。

    Returns:
        有序取值列表；``[]`` = 该 app 没有"按预加载范围遍历"这回事（单次运行，零回归）。
        给了具体取值但不在 app 声明里 → ``StartupError``（参数错误，不静默忽略）。
    """
    from uita.core.perf.scenarios import load_app_load_modes

    declared = load_app_load_modes(app_name)
    if not declared:
        return []
    requested = str(getattr(cli, "load_mode", "") or "").strip().lower() or "all"
    if requested == "all":
        return list(declared)
    if requested not in declared:
        raise StartupError(
            f"--load-mode {requested!r} 不在 app {app_name} 声明的取值里"
            f"（可用: all, {', '.join(declared)}）",
        )
    return [requested]


def _describe_run_context(context: dict[str, Any]) -> str:
    """人类可读的批次口径（控制台一行，现场能判断"这次是按什么配置量的"）。"""
    use_cache = bool(context.get("use_cache"))
    load_mode = str(context.get("load_mode") or "")
    parts = [f"预加载范围 mode={load_mode}"] if load_mode else []
    parts.append(
        "磁盘缓存 cef_disk_cache_enabled="
        + ("true（热，需先跑一轮装满缓存）" if use_cache else "false（冷，每次启动都相当于首次打开）")
    )
    return "；".join(parts)


def _perf_ai_analysis_enabled(cli) -> bool:
    """是否追加"AI 分析报告"：``--perf-ai-analysis`` 或 ``UITA_PERF_AI_ANALYSIS=1``。

    **默认关闭**：无头 agent 分析是长耗时 + 消耗模型额度的显式动作。
    开关关闭时，本模式不产生任何分析产物、不起任何进程。
    """
    from uita.core.perf.analysis import analysis_enabled

    if (getattr(cli, "perf_ai_run_dir", "") or "").strip():
        return True
    return analysis_enabled(bool(getattr(cli, "perf_ai_analysis", False)))


def _perf_ai_kwargs(cli) -> dict[str, Any]:
    """分析相关命令行参数（空值回落 ``config`` 默认）。"""
    return {
        "model": (getattr(cli, "perf_ai_model", "") or "").strip(),
        "timeout_s": int(getattr(cli, "perf_ai_timeout_s", 0) or 0),
        "compare_run": (getattr(cli, "perf_ai_compare_run", "") or "").strip() or "auto",
    }


def _resolve_perf_ai_runner() -> Optional[Any]:
    """选择内置 DSH 无头分析 runner（统一使用 DSH_API_URL 与 DSH_API_KEY）。"""
    from uita.tools.dsh.headless import is_dsh_configured, make_perf_ai_runner

    if is_dsh_configured():
        return make_perf_ai_runner()
    return None


def _run_perf_ai_analysis_safe(
    cli, *, tiers: list[tuple[str, Any]], run_dir: str, app_name: str, platform: str,
    notes: Optional[list[str]] = None,
) -> str:
    """跑一次 AI 分析并返回**可用报告路径**（未开启/失败返回空串）。

    best-effort：**绝不改变 perf 退出码**，异常只降级告警，
    分析报告缺席时由调用方回落到 ``perf_report.html``。
    """
    if not _perf_ai_analysis_enabled(cli):
        return ""
    if not run_dir or not os.path.isdir(run_dir) or not tiers:
        print(f"[perf] ⚠ 跳过 AI 分析（批次目录或档位数据缺失）: {run_dir or '(空)'}")
        print("PERF_AI_ANALYSIS: failed(missing-run-dir)")
        return ""
    from uita.core.perf.analysis import run_analysis

    kwargs = _perf_ai_kwargs(cli)
    runner = _resolve_perf_ai_runner()
    print(f"[perf] AI 分析（内置 DSH 直写 HTML）：档位 "
          f"{'、'.join(name for name, _ in tiers)}，模型 {kwargs['model'] or '默认'}")
    try:
        outcome = run_analysis(
            tiers, app_name=app_name, run_dir=run_dir, platform=platform,
            model=kwargs["model"], timeout_s=kwargs["timeout_s"],
            compare_run=kwargs["compare_run"], runner=runner,
            on_log=print, notes=list(notes or []),
        )
    except BaseException as exc:  # noqa: BLE001 - 分析绝不允许拖垮 perf 收尾
        print(f"[perf] ⚠ AI 分析异常（不阻断）: {type(exc).__name__}: {exc}")
        print("PERF_AI_ANALYSIS: failed(unexpected-exception)")
        return ""
    print(outcome.cli_line())
    return outcome.report_path if outcome.usable else ""


def _analyze_existing_run(cli, *, run_dir: str, app_name: str) -> int:
    """对已落盘批次跑一次 AI 分析（``--perf-ai-run-dir``；**不跑 perf**）。"""
    from uita.core.perf.analysis import scan_run_dir

    if not app_name:
        raise StartupError("--perf-ai-run-dir 需要同时指定 --app（facts 需要 app 口径与场景白名单）")
    if not os.path.isdir(run_dir):
        raise StartupError(f"--perf-ai-run-dir 目录不存在: {run_dir}")

    try:
        from uita.core.perf.scenarios import load_app_scenarios

        load_app_scenarios(app_name)
    except Exception as exc:  # noqa: BLE001
        print(f"[perf] ⚠ 加载 app 场景包失败（不影响出报告，但会少指标层事实）: {exc}")

    tiers, skipped = scan_run_dir(run_dir)
    if not tiers:
        raise StartupError(
            f"--perf-ai-run-dir 未发现可分析产物（需要 perf_summary.json 或 tier-*/perf_summary.json）: {run_dir}",
        )
    for note in skipped:
        print(f"[perf] ⚠ 批次产物缺口：{note}")
    platform = str(getattr(tiers[0][1], "platform", "") or "")
    print(f"[perf] 离线再分析：{run_dir}（档位 {'、'.join(str(name) for name, _ in tiers)}"
          f"{'，平台 ' + platform if platform else ''}）")
    report = _run_perf_ai_analysis_safe(
        cli, tiers=tiers, run_dir=run_dir, app_name=app_name, platform=platform, notes=skipped,
    )
    if report:
        print(f"[perf] 分析报告 → {report}")
    return 0


def _handle_perf(cli) -> int:
    from uita.config import PERF_REPEAT_DEFAULT, PERF_SAMPLE_INTERVAL_MS
    from uita.core.perf.console import enable_concise_console
    from uita.core.registry_common import UNIVERSAL_PLATFORM, resolve_universal_platform

    # 控制台精简（只留轮次/节点耗时/失败，过程日志只进 logs/<date>.log）：
    # 必须在任何跑批日志之前启用，否则前几十行仍是全量。`UITA_PERF_CONCISE=0`
    # 或 `run_perf_admin.ps1 -FullLog` 恢复原样（见 core/perf/console.py）。
    enable_concise_console()

    # 离线再分析（`--perf-ai-run-dir`）：对**已落盘批次**跑一次 AI 分析，不跑 perf、不上传。
    # 用途：拿到一份跑好的产物（如流水线留下的 tier-*/）后单独出分析报告、迭代 Skill 提示词，
    # 不必重跑整批压测（省机器时间与样本库重建）。
    analysis_run_dir = (getattr(cli, "perf_ai_run_dir", "") or "").strip()
    if analysis_run_dir:
        return _analyze_existing_run(cli, run_dir=analysis_run_dir,
                                     app_name=(getattr(cli, "app", "") or ""))

    files = _resolve_perf_files(cli, app_name=(getattr(cli, "app", "") or ""))
    app_name = _resolve_app(cli, files)
    _apply_main_app_version(app_name, getattr(cli, "version", "") or "")

    bucket_size = _resolve_int(getattr(cli, "perf_repeat", None), PERF_REPEAT_DEFAULT, "perf-repeat")
    interval_ms = _resolve_int(
        getattr(cli, "perf_sample_interval_ms", None), PERF_SAMPLE_INTERVAL_MS, "perf-sample-interval-ms", minimum=50,
    )
    cold_start = getattr(cli, "perf_cold_start", True)
    if cold_start is None:
        cold_start = True
    warmup = _resolve_warmup(cli, app_name)
    baseline_path = (getattr(cli, "perf_baseline", "") or "").strip()
    if baseline_path and not os.path.isfile(baseline_path):
        raise StartupError(f"--perf-baseline 文件不存在: {baseline_path}")
    metric_filter = [
        m.strip() for m in (getattr(cli, "perf_metrics", "") or "").replace("，", ",").split(",") if m.strip()
    ]
    if metric_filter:
        # 判定已停用（2026-10-08）：该参数只影响"判定哪些阈值键"，现已无判定可言。
        # 参数保留以免破坏既有命令行/脚本，但明确告知不再生效（不静默忽略）。
        print("[perf] ⚠ --perf-metrics 已停用：性能判定已关闭，不再比阈值（参数保留仅为兼容旧命令）")
    # 场景由 app 侧注册（apps/<app>/perf/scenarios/，导入即注册）→ 必须先加载再校验场景名，
    # 否则合法场景会被判为"未知场景"。core 不静态依赖 apps（R2），故走动态 import。
    from uita.core.perf.scenarios import load_app_scenarios

    # --release-res 早失败校验（非 Windows / 盘符非法或不存在）——必须早于宿主基准与用例执行：
    # 盘符写错时"跑完半场才发现素材没落到目标盘"是最贵的失败方式。
    _release_res_letter(cli)

    load_app_scenarios(app_name)
    os_name = resolve_universal_platform(UNIVERSAL_PLATFORM)

    # 主机性能基准：每次 --mode perf 调用只跑一次，早于任何用例执行/样本库构建
    # （见 docs/plans/archive/10-platform-scale-实施纪要.md）。
    from uita.core.perf.host_bench import benchmark_host

    host_info = benchmark_host(on_log=print)

    common_kwargs: dict[str, Any] = dict(
        app_name=app_name, files=files, os_name=os_name, bucket_size=bucket_size,
        interval_ms=interval_ms, cold_start=cold_start, warmup=bool(warmup), baseline_path=baseline_path,
        metric_filter=metric_filter, host_info=host_info,
    )

    # 预加载范围（`--load-mode`）：app 声明了取值清单时，`all`（默认）逐个跑并聚合对比。
    load_modes = _resolve_load_modes(cli, app_name)
    if len(load_modes) > 1:
        return _handle_perf_load_modes(cli, load_modes=load_modes, **common_kwargs)
    single_load_mode = load_modes[0] if load_modes else ""

    if _is_multi_tier(cli):
        return _handle_perf_multi_tier(cli, **common_kwargs)

    lib, baseline_tag, auto_built = _prepare_lib(cli)
    try:
        exit_code, _result = _execute_perf(
            cli, lib=lib, baseline_tag=baseline_tag, run_dir="", label="",
            load_mode=single_load_mode, **common_kwargs,
        )
        return exit_code
    finally:
        # 跑完自动销毁"本次构建"的样本库（--perf-keep-lib 可保留；用户给的 --perf-lib 永不删）
        _cleanup_auto_lib(cli, lib, auto_built=auto_built)


def _execute_perf(
    cli, *, app_name: str, files: list[str], os_name: str, bucket_size: int, interval_ms: int,
    cold_start: bool, warmup: bool = True, baseline_path: str, metric_filter: list[str], host_info: dict[str, Any],
    lib: str, baseline_tag: str, run_dir: str = "", label: str = "",
    preset_override: str = "", only: Optional[list[str]] = None,
    exclude: Optional[list[str]] = None, analyze_report: bool = True,
    load_mode: str = "",
) -> tuple[int, Any]:
    """跑一次完整 perf 编排（单档路径的唯一一次调用，或多单元编排中的一个单元）。

    Args:
        lib / baseline_tag: 已由调用方通过 :func:`_prepare_lib` 解析（本函数不再解析样本库，
            仅在 ``baseline_tag`` 仍为空时走 :func:`_resolve_tier` 兜底推断）。
        run_dir: 产物根目录；多单元路径传 ``report/perf/perf_<ts>/tier-<name>/``，单档路径传空串
            （由 ``PerfRunner`` 用默认 ``REPORT_DIR/perf/perf_<ts>``）。
        label: 打印前缀（多单元路径传单元名，如 ``"5k"``），单档路径传空串（不加前缀）。
        preset_override / only / exclude: 单元声明的预设与场景清单过滤（``UNIT_SEQUENCE``），
            单档路径与"纯命令行"路径一律为空（行为零变化）。
        analyze_report: 跑完是否追加 AI 分析报告（受 ``--perf-ai-analysis`` /
            ``UITA_PERF_AI_ANALYSIS`` 闸门约束，见 :func:`_run_perf_ai_analysis_safe`）。
            多单元路径传 ``False``——**只在顶层做一次**。

    Returns:
        ``(exit_code, PerfRunResult)``；``exit_code=1`` 表示 strict 模式下有用例超阈值。
    """
    from uita.config import PERF_ENABLE_RESOURCE_SAMPLING
    from uita.core.registry_common import UNIVERSAL_PLATFORM, resolve_universal_platform

    if not baseline_tag:
        baseline_tag = _resolve_tier(
            cli, app_name, platform=resolve_universal_platform(UNIVERSAL_PLATFORM),
            lib=lib, baseline_path=baseline_path,
        )
    expansion, preset_name = _resolve_preset(
        cli, app_name, lib=lib,
        preset_override=preset_override, only=only, exclude=exclude,
    )
    scenarios = expansion.scenarios
    scenario_options = expansion.scenario_options
    skipped_scenarios = expansion.skipped
    steady_state_s = float(getattr(cli, "perf_steady_state_s", 0.0) or 0.0)

    # 轮次：命令行显式给值（argparse 默认 None）最优先，其次预设声明，最后 config 默认值
    # （已由 _handle_perf 解析进 bucket_size）。静息类预设单轮就要 10 分钟，默认 3 轮
    # 只会把一次运行拖到半小时，故轮次属于"这个预设该怎么跑"的知识。
    if getattr(cli, "perf_repeat", None) is None and expansion.repeat:
        if expansion.repeat != bucket_size:
            print(
                f"[perf] 预设 {preset_name or '(无)'} 指定轮次={expansion.repeat}"
                f"（命令行未给 --perf-repeat，原默认 {bucket_size}）",
            )
        bucket_size = int(expansion.repeat)

    _warn_idle_scope(scenarios, expansion, steady_state_s=steady_state_s, cold_start=cold_start)

    # 一键运行（用了预设或样本库）默认给隔离 profile：避免顺手污染用户真实 profile/缓存，
    # 也让两轮之间的索引状态可预期（用户显式传 --perf-user-data-dir 时以用户为准）。
    user_data_dir = (getattr(cli, "perf_user_data_dir", "") or "").strip()
    if not user_data_dir and (preset_name or lib):
        import tempfile
        from datetime import datetime as _dt

        suffix = f"-{label}" if label else ""
        user_data_dir = os.path.join(
            tempfile.gettempdir(), f"uita-perf-{_dt.now().strftime('%Y%m%d-%H%M%S')}{suffix}",
        )
        print(f"[perf] 隔离 profile（预设默认）: {user_data_dir}")

    prefix = f"[{label}] " if label else ""
    print(
        f"[perf] {prefix}app={app_name} platform={os_name} cases={len(files)} "
        f"repeat={bucket_size} cold_start={cold_start} warmup={warmup} sample_interval={interval_ms}ms"
        + (f" steady_state={steady_state_s:g}s" if steady_state_s > 0 else "")
        + (f" preset={preset_name}" if preset_name else "")
        + (f" scenarios={','.join(scenarios)}" if scenarios else " scenarios=（无，仅启动/资源）")
        + (
            f" last_round_only={','.join(expansion.last_round_only)}"
            if expansion.last_round_only else ""
        ),
    )
    if "idle" in scenarios:
        # 与 runner 的 _options_for() 同一合并口径（预设按场景选项 + 命令行全局覆盖，后者优先），
        # 否则用户用 --perf-scenario-opt duration_ms= 改了时长，这里会打出预设里的旧值（误导）
        idle_opts = dict(expansion.per_scenario_options.get("idle") or {})
        idle_opts.update(scenario_options)
        print(
            f"[perf] {prefix}静息 idle：静置 {int(idle_opts.get('duration_ms', 0)) / 1000:g}s，"
            f"关主窗口={'是' if idle_opts.get('close_window', 1) else '否'}"
            + ("（仅最后一轮执行一次）" if "idle" in expansion.last_round_only else ""),
        )

    from uita.core.perf.runner import PerfRunner
    from uita.core.perf.scenarios import (
        load_app_launch_probe,
        load_app_presets,
        load_app_round_prepare,
        load_app_round_prepare_before_retry,
        load_app_round_teardown,
        load_app_run_setup,
        load_app_startup_ready_js,
    )

    # 启动就绪探针（app 侧 JS：窗口出现 + 首屏已渲染，见
    # apps/<app>/perf/startup_ready.py）。未提供该模块的 app 退回通用口径
    # document.readyState === 'complete'（零回归），故这里只打一行口径说明。
    startup_ready_js = load_app_startup_ready_js(app_name)
    # 原生引擎探针（进程 + 窗口类名等应用知识）：Electron 之外的 app 靠它拿真实
    # startup_ms，否则只有"调用 prepare 的墙钟"。未提供 = 沿用默认探针（零回归）。
    launch_probe = load_app_launch_probe(app_name)
    # 每轮启动前置准备（探测是否在运行 → quit_app 退出 → 校验）：只在 app 显式提供时启用。
    round_prepare = load_app_round_prepare(app_name)
    # 启动失败重试前是否也过一遍上面这道闸门（app 声明 PERF_ROUND_PREPARE_BEFORE_RETRY；默认关）
    round_prepare_before_retry = load_app_round_prepare_before_retry(app_name)
    # 每轮收尾（场景测量之后按 app 口径退出应用），与前置准备对称，同样只在 app 提供时启用。
    round_teardown = load_app_round_teardown(app_name)
    # 批次级环境准备（写被测客户端的启动期配置）：同样只在 app 显式提供时启用。
    run_setup = load_app_run_setup(app_name)
    run_context = _run_context(cli, load_mode)
    # 资源采样目标进程：命令行 > 预设 monitor_process > 空（由 app_path 推导）
    process_name = (getattr(cli, "perf_process_name", "") or "").strip()
    if not process_name and preset_name:
        preset_spec = load_app_presets(app_name).get(preset_name) or {}
        process_name = str(preset_spec.get("monitor_process") or "").strip()
    print(
        f"[perf] {prefix}启动耗时口径："
        + (
            f"app 侧原生探针（render_probe={getattr(launch_probe, 'render_probe', '') or 'ready'}）"
            if launch_probe is not None else
            "app 侧就绪探针（窗口出现且左上角品牌已渲染）"
            if startup_ready_js else
            "通用兜底 document.readyState === 'complete'（该 app 未提供 perf/startup_ready.py）"
        ),
    )
    if process_name:
        print(f"[perf] {prefix}资源采样目标进程：{process_name}（含进程树）")
    if round_prepare is not None:
        print(f"[perf] {prefix}每轮前置准备：已启用（app 侧 perf/round_prepare.py）")
        if round_prepare_before_retry:
            print(f"[perf] {prefix}启动重试前闸门：已启用（每次重试前也先准备，退不干净则不启动）")
    else:
        print(f"[perf] {prefix}每轮前置准备：未提供（app 未实现 perf/round_prepare.py）")
    if run_setup is not None:
        print(f"[perf] {prefix}批次环境准备：已启用（app 侧 perf/run_setup.py，开跑前执行一次）")
    print(f"[perf] {prefix}客户端环境：{_describe_run_context(run_context)}")
    if "stress" in scenarios:
        print(
            f"[perf] {prefix}压测 stress：先关掉应用 → 逐级（+250MB）挤占**系统内存**（不加载 CPU 负载）→ "
            "每级尝试启动被测应用 → 直到启动失败；记录失败时的内存水位"
            "（已占用/总内存 + **系统剩余可用**），并把应用日志打包为 "
            "<run_dir>/stress_logs/<档位>_<轮次>_<时间戳>.zip"
            "；**占用默认防压缩/防释放**（Windows 硬钉最小工作集 + VirtualLock + 1s 一次 keep-alive 触页，"
            "生效证据看产物的 lock_summary/stress_locked_mb；keepalive_ms=0 可关）"
            "；步进可用 --perf-scenario-opt step_mb= 调；注意：**无占用上限、无内存阈值**——"
            "内存吃满后继续占用，逼迫系统释放，直到给不出内存（alloc_retry_rounds= 控重试轮数），"
            "请只在专用测试机跑（不要在开发机跑）",
        )

    def _on_round(case_report, round_record) -> None:
        res = round_record.resources or {}
        print(
            f"  {prefix}[{case_report.case_name}] 轮 {round_record.round_index}/{bucket_size} "
            f"launch={round_record.launch_ms:.0f}ms startup="
            f"{'—' if round_record.startup_ms is None else format(round_record.startup_ms, '.0f') + 'ms'} "
            f"RSS峰值={res.get('rss_mb_peak')}MB CPU均值={res.get('cpu_percent_avg')}% "
            f"用例耗时={round_record.case_duration_ms:.0f}ms TCS={round_record.tcs_status}",
        )
        attempts = round_record.launch_attempts or []
        fail = round_record.launch_failure or {}
        if fail:
            # 启动失败现场也打到控制台（口径同 stress 的失败水位）：否则要跑完整批再翻报告
            # 才知道"这一轮为什么这么快就过了"；本轮余下步骤已跳过（见 runner._run_case）
            print(
                f"  {prefix}[{case_report.case_name}] ⚠ 轮 {round_record.round_index} 启动失败"
                f"（{len(attempts)} 次尝试全部失败，末次 "
                f"{fail.get('stage') or fail.get('reason') or '未就绪'}）：系统已占用 "
                f"{_fmt_num(fail.get('mem_used_mb'))}/{_fmt_num(fail.get('mem_total_mb'))}MB、"
                f"剩余可用 {_fmt_num(fail.get('mem_available_mb'))}MB、"
                f"占比 {_fmt_num(fail.get('mem_percent'))}% → 现场已记录，本轮结束，进入下一轮",
            )
        elif attempts:
            # 重试后成功：本轮照常出数据，但"启动不稳"这件事必须在控制台看得见（报告里也有）
            first = attempts[0]
            print(
                f"  {prefix}[{case_report.case_name}] ⚠ 轮 {round_record.round_index} "
                f"第 {len(attempts)} 次启动失败（{first.get('stage') or '未就绪'}）后重试成功，"
                f"本轮继续（失败现场已记录：系统已占用 "
                f"{_fmt_num(first.get('mem_used_mb'))}/{_fmt_num(first.get('mem_total_mb'))}MB、"
                f"剩余可用 {_fmt_num(first.get('mem_available_mb'))}MB）",
            )

    runner = PerfRunner(
        app_name=app_name,
        os_name=os_name,
        case_files=files,
        repeat=bucket_size,
        sample_interval_ms=interval_ms,
        cold_start=bool(cold_start),
        warmup=bool(warmup),
        baseline_path=baseline_path,
        baseline_tag=baseline_tag,
        enable_resource_sampling=bool(PERF_ENABLE_RESOURCE_SAMPLING),
        keep_screenshot=bool(getattr(cli, "perf_keep_screenshot", False)),
        metric_filter=metric_filter,
        cleanup_app=not bool(getattr(cli, "perf_keep_app", False)),
        scenarios=scenarios,
        scenario_options=scenario_options,
        last_round_only_scenarios=expansion.last_round_only,
        steady_state_s=steady_state_s,
        user_data_dir=user_data_dir,
        preset_name=preset_name,
        skipped_scenarios=skipped_scenarios,
        scenario_options_by_scenario=expansion.per_scenario_options,
        host_info=host_info,
        startup_ready_js=startup_ready_js,
        round_prepare=round_prepare,
        round_prepare_before_retry=round_prepare_before_retry,
        round_teardown=round_teardown,
        run_setup=run_setup,
        run_context=run_context,
        launch_probe=launch_probe,
        process_name=process_name,
        run_dir=run_dir,
        on_round=_on_round,
    )
    result = asyncio.run(runner.run())

    print(f"\n[perf] {prefix}汇总 → {result.summary_path}")
    print(f"[perf] {prefix}报告 → {result.report_path}")
    setup = dict(getattr(result, "setup", None) or {})
    if setup:
        _print_run_setup(prefix, setup)
    print(f"[perf] {prefix}性能判定：未启用（只记录真实值，逐轮明细见报告与分析文件）")
    for case in result.cases:
        agg = case.aggregate or {}
        startup = agg.get("startup_ms") or {}
        launch = agg.get("launch_ms") or {}
        print(
            f"[perf] {prefix}{case.case_name}: "
            f"启动 均值/最差={_fmt_ms(startup.get('avg'))}/{_fmt_ms(startup.get('max'))} "
            f"连接 均值/最差={_fmt_ms(launch.get('avg'))}/{_fmt_ms(launch.get('max'))} "
            f"RSS峰值={agg.get('rss_mb_peak')}MB",
        )
    run_dir_path = str(getattr(result, "run_dir", "") or "")
    if analyze_report:
        _run_perf_ai_analysis_safe(
            cli,
            tiers=[(baseline_tag or preset_name or "run", result)],
            run_dir=run_dir_path,
            app_name=app_name,
            platform=os_name,
        )

    return 0, result


def _handle_perf_load_modes(
    cli, *, load_modes: list[str], app_name: str, files: list[str], os_name: str,
    bucket_size: int, interval_ms: int, cold_start: bool, warmup: bool = True,
    baseline_path: str, metric_filter: list[str], host_info: dict[str, Any],
) -> int:
    """``--load-mode all``（默认）：按 app 声明的取值逐单元跑一遍，再把结果聚合成一份报告。

    每个单元 = 一个预加载范围（如 ``blank/off/soft/game/both``）× ``--perf-repeat`` 轮；
    单元内照常"结束旧实例 → 冷启动 → 采样 → 测量"，只把客户端配置的 ``mode`` 换成该取值
    （经 ``run_context`` 透传给 app 侧 ``run_setup``，开跑前写一次）。

    聚合产物与多档位编排同款（``perf_summary.json`` / ``perf_report.html`` / ``perf_metrics.csv``），
    各单元的完整单单元报告仍在 ``load-<取值>/perf_report.html``；跨单元对比图（横坐标=取值）
    由 app 声明的 ``CROSS_UNIT_CHARTS`` 决定。
    """
    from datetime import datetime as _dt

    from uita.config import REPORT_DIR
    from uita.core.perf.scenarios import load_app_cross_unit_charts

    run_id = _dt.now().strftime("%Y%m%d-%H%M%S")
    agg_run_dir = os.path.join(REPORT_DIR, "perf", f"perf_{run_id}")
    os.makedirs(agg_run_dir, exist_ok=True)
    print(
        "[perf] --load-mode all → 依次跑全部预加载范围（每个 "
        f"{bucket_size} 轮）: " + " → ".join(load_modes),
    )

    unit_results: list[tuple[str, Any]] = []
    exit_code = 0
    for mode in load_modes:
        code, result = _execute_perf(
            cli, app_name=app_name, files=files, os_name=os_name, bucket_size=bucket_size,
            interval_ms=interval_ms, cold_start=cold_start, warmup=warmup,
            baseline_path=baseline_path, metric_filter=metric_filter, host_info=host_info,
            lib="", baseline_tag="", run_dir=os.path.join(agg_run_dir, f"load-{mode}"),
            label=mode, load_mode=mode, analyze_report=False,
        )
        unit_results.append((mode, result))
        if code != 0:
            exit_code = code

    from uita.core.perf.report import (
        render_multi_tier_html,
        write_merged_metrics_csv,
        write_multi_tier_summary,
    )

    cross_charts = load_app_cross_unit_charts(app_name)
    summary_path = write_multi_tier_summary(unit_results, agg_run_dir, host=host_info)
    report_path = render_multi_tier_html(
        unit_results, agg_run_dir, host=host_info, cross_charts=cross_charts,
    )
    csv_path = write_merged_metrics_csv([r for _, r in unit_results], agg_run_dir)
    print(f"\n[perf] 多单元聚合汇总 → {summary_path}")
    print(f"[perf] 多单元聚合报告 → {report_path}")
    print(f"[perf] 多单元聚合统计(CSV) → {csv_path}")
    if cross_charts:
        print("[perf] 跨单元对比图（横坐标=预加载范围）: " + "、".join(
            str(group.get("title") or "") for group in cross_charts
        ))

    platform = str(getattr(unit_results[0][1], "platform", "") or os_name) if unit_results else os_name
    _run_perf_ai_analysis_safe(
        cli, tiers=unit_results, run_dir=agg_run_dir, app_name=app_name, platform=platform,
    )
    return exit_code


def _handle_perf_multi_tier(
    cli, *, app_name: str, files: list[str], os_name: str, bucket_size: int, interval_ms: int,
    cold_start: bool, warmup: bool = True, baseline_path: str, metric_filter: list[str],
    host_info: dict[str, Any],
) -> int:
    """未指定档位时的默认行为：依次跑全部单元并把报告聚合到同一目录。

    单元序列优先取 app 侧 ``UNIT_SEQUENCE``（见 :func:`_resolve_units`）——如声明为
    ``0k → 5k → 10k → 50k → stress`` 五单元。每个单元跑 ``--perf-repeat`` 轮，全部单元聚合进
    同一份产物（各 ``tier-<name>/`` + 顶层 ``perf_summary.json`` / ``perf_report.html`` /
    ``perf_metrics.csv``）；app 未声明该常量时回落 core 默认 ``0k/5k/10k/50k``（零回归）。
    """
    from datetime import datetime as _dt

    from uita.config import REPORT_DIR
    from uita.core.perf.lib_builder import tier_size

    units = _resolve_units(app_name)
    print(
        "[perf] 未指定 --perf-tier/--perf-lib，且已给 --perf-src → 默认依次跑全部单元: "
        + " → ".join(str(u.get("name") or "") for u in units),
    )
    setattr(
        cli, "_perf_release_max_count",
        max([tier_size(str(u.get("lib_tier") or u.get("name") or "")) or 0 for u in units] + [0]),
    )
    run_id = _dt.now().strftime("%Y%m%d-%H%M%S")
    agg_run_dir = os.path.join(REPORT_DIR, "perf", f"perf_{run_id}")
    os.makedirs(agg_run_dir, exist_ok=True)

    lib = ""
    auto_built = False
    tier_results: list[tuple[str, Any]] = []
    exit_code = 0
    try:
        for unit in units:
            name = str(unit.get("name") or "")
            lib, baseline_tag, auto_built = _prepare_lib(
                cli,
                tier_override=str(unit.get("lib_tier") or name),
                tag_override=str(unit.get("tag") or ""),
            )
            unit_run_dir = os.path.join(agg_run_dir, f"tier-{name}")
            code, result = _execute_perf(
                cli, app_name=app_name, files=files, os_name=os_name, bucket_size=bucket_size,
                interval_ms=interval_ms, cold_start=cold_start, warmup=warmup, baseline_path=baseline_path,
                metric_filter=metric_filter, host_info=host_info, lib=lib,
                baseline_tag=baseline_tag, run_dir=unit_run_dir, label=name,
                preset_override=str(unit.get("preset") or ""),
                only=unit.get("only"),
                exclude=unit.get("exclude"),
                analyze_report=False,
            )
            tier_results.append((name, result))
            if code != 0:
                exit_code = code
    finally:
        _cleanup_auto_lib(cli, lib, auto_built=auto_built)

    from uita.core.perf.report import (
        render_multi_tier_html,
        write_merged_metrics_csv,
        write_multi_tier_summary,
    )

    summary_path = write_multi_tier_summary(tier_results, agg_run_dir, host=host_info)
    report_path = render_multi_tier_html(tier_results, agg_run_dir, host=host_info)
    csv_path = write_merged_metrics_csv([r for _, r in tier_results], agg_run_dir)
    print(f"\n[perf] 多档聚合汇总 → {summary_path}")
    print(f"[perf] 多档聚合报告 → {report_path}")
    print(f"[perf] 多档聚合统计(CSV) → {csv_path}")
    multi_platform = (str(getattr(tier_results[0][1], "platform", "") or os_name)
                      if tier_results else os_name)
    _run_perf_ai_analysis_safe(
        cli, tiers=tier_results, run_dir=agg_run_dir, app_name=app_name, platform=multi_platform,
    )
    return exit_code

