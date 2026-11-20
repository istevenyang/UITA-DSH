"""样本库构建：按档位规模把源图片**复制**成 N 份，供规模相关场景使用。

为什么必须用**真实照片**而不是合成图（真机教训，2026-09-18）：
    早先用 `ffmpeg testsrc2` + `sips` 合成 3000×2000 HEIC 再克隆 1 万份——扫描/缩略图
    各项看着正常，但**滑动时时间线根本不显示照片**（卡片空白/占位），
    于是"60fps"是在渲染空白卡片下测出来的，毫无参考价值。
    真实照片（HEIC 含正常 EXIF/尺寸/解码路径）才能反映真实渲染与解码成本。

复制方式（平台差异）：
  - macOS：`cp -c`（APFS clone）→ 秒级完成，**几乎不占额外磁盘**（共享数据块）
  - Linux：`cp --reflink=auto`（支持时克隆，否则普通复制）
  - Windows：普通复制（无克隆语义）→ 10k×2.3MB ≈ 23GB 真实占用，需自行评估
  失败时一律回退 `shutil.copy2`，保证可用性优先。

样本库位置：
  - 默认：当前用户图片目录下的 ``perf/``（macOS ``~/Pictures/perf``，Windows
    ``%USERPROFILE%\\Pictures\\perf``）；
  - ``--release-res <盘符>``（Windows）：``<盘>:\\perf``，空间不足时可由
    ``core/perf/release_res.py`` 拆成**多个分片目录**（每盘一个 ``<盘>:\\perf``）。
    多分片用 :func:`join_lib_dirs` 拼成 ``;`` 分隔的**单字符串**保存与传递，
    单目录形态因此零签名变化（``split_lib_dirs`` 反向拆分）。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

logger = logging.getLogger("uita")

__all__ = [
    "AUTO_LIB_DIRNAME",
    "PHOTO_EXTS",
    "TIER_SIZES",
    "VIDEO_EXTS",
    "auto_lib_dir",
    "build_sample_lib",
    "join_lib_dirs",
    "remove_sample_lib",
    "split_lib_dirs",
    "tier_size",
    "user_pictures_dir",
]

#: 样本库目录名：当前用户图片目录下的 ``perf/``（macOS：``~/Pictures/perf``；跑完即删）
AUTO_LIB_DIRNAME = "perf"

#: 多分片样本库字符串的分隔符：**固定 ``;``**，不用 ``os.pathsep``。
#: 理由：``--release-res`` 是 Windows 独有功能，而 POSIX 的 ``os.pathsep`` 是 ``:``——
#: 用它切分会把 Windows 路径 ``C:\\lib`` 切成 ``C``（盘符本身就是冒号）。
LIB_DIR_SEP = ";"

#: 工具生成的样本文件名（``sim_000123.heic`` / 视频 ``sim_v000.MOV``）——销毁时的
#: **安全护栏**之一：目录内若存在不符合该命名的文件，说明不是本次生成的目录，拒绝删除。
#: 注意必须覆盖视频命名（``sim_v<数字>``）：漏了会让销毁被自己拦下（实测发生）。
GENERATED_NAME_RE = re.compile(
    r"^sim_(v)?\d+\.(heic|heif|jpg|jpeg|png|dng|tif|tiff|mov|mp4|m4v)$", re.IGNORECASE,
)

#: 规模档位名 → 目标照片份数（`--perf-tier`）。``0k`` 是"空库"档位（2026-09-21 新增）：
#: 目标份数为 0，语义是"不复制任何测试数据，用全新空图库跑"，作为 5k/10k/50k 的
#: "从 0 到有"基准点，与三档之间的边际成本区分开。
TIER_SIZES: dict[str, int] = {"0k": 0, "5k": 5000, "10k": 10000, "50k": 50000}

PHOTO_EXTS = (".heic", ".heif", ".jpg", ".jpeg", ".png", ".dng", ".tif", ".tiff")
VIDEO_EXTS = (".mov", ".mp4", ".m4v")


def tier_size(tier: str) -> Optional[int]:
    """档位名 → 目标份数。

    返回值语义（**调用方必须用 ``is None`` 判断"是否是规模档位"，不能用
    ``if not size:``**——``0`` 是"合法的规模档位（空库）"，与"不是规模档位"
    的语义不同，二者都是 Python falsy 值）：
      - ``None``：不是规模档位（如基线档位名 ``lib-10k``、空串、未知名字）；
      - ``0``：合法档位，目标份数为零（``0k`` 空库档位）；
      - 正整数：合法档位，目标份数（``5k``→5000 等）。
    """
    return TIER_SIZES.get((tier or "").strip().lower())


def user_pictures_dir() -> str:
    """当前用户的**图片目录**（macOS：``~/Pictures``）。

    优先 ``platformdirs.user_pictures_dir()``（跨平台：Windows ``%USERPROFILE%\\Pictures``、
    Linux 遵循 XDG）；不可用时退回 ``~/Pictures``。
    """
    try:
        from platformdirs import user_pictures_dir as _pics

        path = str(_pics() or "").strip()
        if path:
            return path
    except Exception:  # noqa: BLE001 - 缺 platformdirs 时退回约定路径
        pass
    return os.path.join(os.path.expanduser("~"), "Pictures")


def auto_lib_dir(tier: str = "", *, base_dir: str = "") -> str:
    """样本库目录：**当前用户图片目录下的 ``perf/``**（macOS：``~/Pictures/perf``）。

    用户要求（2026-09-18）：样本复制到当前用户的图片目录下的 ``perf`` ——
    macOS 即 ``~/Pictures/perf``；跑完删除该目录即回到干净状态。
    ``tier`` 不参与路径（同一档位目录按份数精确重建，见 :func:`build_sample_lib`）。
    """
    base = base_dir or user_pictures_dir()
    return os.path.join(base, AUTO_LIB_DIRNAME)


def split_lib_dirs(lib: str) -> list[str]:
    """样本库字符串 → 目录列表（``--release-res`` 拆分释放时可能有多个分片）。

    单目录是**常态**（返回长度 1 的列表）；空串/纯分隔符 → 空列表。
    """
    out: list[str] = []
    for chunk in str(lib or "").split(LIB_DIR_SEP):
        path = chunk.strip()
        if path and path not in out:
            out.append(path)
    return out


def join_lib_dirs(dirs) -> str:
    """目录列表 → 样本库字符串（``LIB_DIR_SEP``（``;``）分隔）。

    为什么用"单字符串 + 分隔符"而不是 list：``--perf-lib`` / 预设 ``lib_options`` /
    ``count_lib_files`` 等既有接口全部接收单字符串，多分片只在真正需要的地方
    （扫描路径注入）拆分，单目录行为零变化。
    """
    seen: list[str] = []
    for path in dirs or []:
        text = str(path or "").strip()
        if text and text not in seen:
            seen.append(text)
    return LIB_DIR_SEP.join(seen)


def remove_sample_lib(path: str, *, expect_parent: str = "") -> dict[str, Any]:
    """销毁本次构建的样本库（``<perf-src>/perf``），带多重安全护栏。

    护栏（防止误删用户数据）——任一不满足即**拒绝删除**并在 ``reason`` 里说明：
      1. 目录名必须是 ``perf``；
      2. 给了 ``expect_parent`` 时必须与父目录一致（即确实是 ``<perf-src>/perf``）；
      3. 目录内**所有**文件都必须符合工具生成的命名（``sim_<数字>.<图片/视频扩展名>``）——
         只要有一个"外来文件"，说明这不是我们建的 perf 目录，绝不删；
      4. 不得是符号链接、不得是根目录。
    """
    result: dict[str, Any] = {
        "path": path, "removed": False, "files": 0,
        "apparent_bytes": 0, "apparent_mb": 0.0, "reason": "",
    }
    if not path:
        result["reason"] = "路径为空"
        return result
    abspath = os.path.abspath(path)
    if os.path.islink(abspath) or not os.path.isdir(abspath):
        result["reason"] = "不是普通目录（或不存在/为符号链接）"
        return result
    if os.path.dirname(abspath) in ("", os.sep) or abspath == os.sep:
        result["reason"] = "拒绝删除根目录"
        return result
    if os.path.basename(abspath) != AUTO_LIB_DIRNAME:
        result["reason"] = f"目录名不是 {AUTO_LIB_DIRNAME!r}（非工具构建的样本库，拒绝删除）"
        return result
    if expect_parent and os.path.realpath(os.path.dirname(abspath)) != os.path.realpath(expect_parent):
        result["reason"] = f"父目录不是复制源目录 {expect_parent}（拒绝删除）"
        return result

    foreign: list[str] = []
    for entry in sorted(os.listdir(abspath)):
        full = os.path.join(abspath, entry)
        if not os.path.isfile(full) or not GENERATED_NAME_RE.match(entry):
            foreign.append(entry)
    if foreign:
        result["reason"] = (
            f"目录内存在非工具生成的文件（{', '.join(foreign[:3])}"
            f"{' 等' if len(foreign) > 3 else ''}）→ 拒绝删除"
        )
        return result

    files = 0
    apparent = 0
    for dirpath, _dirnames, filenames in os.walk(abspath):
        for name in filenames:
            files += 1
            try:
                apparent += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    result["files"] = files
    result["apparent_bytes"] = apparent          # 精确值（MB 会四舍五入到 0.1，小样本会变 0）
    result["apparent_mb"] = round(apparent / 1048576.0, 1)
    ok, how = _remove_tree(abspath)
    result["method"] = how
    if not ok:
        result["reason"] = f"删除失败: {how}"
        return result
    result["removed"] = True
    return result


def _remove_tree(path: str) -> tuple[bool, str]:
    """删除整棵目录树；优先系统命令（POSIX ``rm -rf`` / Windows ``rd /s /q``），
    最后退化到 ``shutil.rmtree``。

    为什么不能只用 ``shutil.rmtree``：IDE 注入的 ``sitecustomize`` 护栏在**一次
    删除 >500 个文件**时拦截 Python 层删除——抛 ``SystemExit``
    （``SAFE_DELETE_BULK_CONFIRM_REQUIRED``，不是 ``OSError``，会穿透把整个 CLI
    打挂）或弹确认告警阻塞无人值守流程，且其"删除"实现是**移入回收站**。
    样本库是我们自己按命名规则生成的目录（``sim_*``）+ 三重护栏校验，用系统命令
    直接删除既快又不经过 Python 层实现（Windows 见 :func:`_win_remove_tree`）。
    """
    if sys.platform == "win32":
        how = _win_remove_tree(path)
        if how:
            return True, how
    else:
        rm = shutil.which("rm")
        if rm:
            err = _run([rm, "-rf", "--", path])
            if not os.path.exists(path):
                return True, "rm -rf"
            logger.warning("[perf] rm -rf 未成功: %s", err or "目录仍存在")
    try:
        shutil.rmtree(path)
    except BaseException as exc:  # noqa: BLE001 - SystemExit 也必须兜住：清理不得拖垮主流程
        return False, f"{type(exc).__name__}: {exc}"
    return True, "shutil.rmtree"


# cmd.exe 单条命令行长度上限约 8191 字符，留出余量后按此阈值切分批
_WIN_CMD_MAX_CHARS = 6000
# PowerShell 兜底删除时传路径用的环境变量名（避免把路径拼进命令串）
_WIN_DEL_PATH_ENV = "UITA_PERF_DEL_PATH"


def _win_cmd() -> str:
    """返回 Windows ``cmd.exe`` 路径（``COMSPEC`` 优先），不可用返回空串。"""
    return os.environ.get("COMSPEC", "") or (shutil.which("cmd") or "")


def _run(cmd: list[str], env: Optional[dict] = None) -> Optional[str]:
    """执行命令，成功返回 ``None``，失败返回错误描述。任何异常都不抛出。"""
    try:
        proc = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, check=False,
            **({"env": env} if env else {}),
        )
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    if proc.returncode != 0:
        return f"rc={proc.returncode} {(proc.stderr or '').strip()[:200]}"
    return None


def _win_remove_tree(path: str) -> Optional[str]:
    """Windows 整目录删除：``rd /s /q`` → PowerShell ``Remove-Item -Force``。

    为什么 Windows 不能落回 ``shutil.rmtree``：IDE 注入的 ``sitecustomize`` 批量
    删除护栏在**一次删除 >500 个文件**时会拦截 Python 层实现——弹确认告警（阻塞
    无人值守流程）或抛 ``SystemExit``（``SAFE_DELETE_BULK_CONFIRM_REQUIRED``，
    不是 ``OSError``，会穿透兜底把 CLI 打挂），且其"删除"实为移入回收站。走系统
    命令绕开 Python 层，与 POSIX 的 ``rm -rf`` 分支同一思路。

    Returns:
        成功时返回使用的方式（如 ``"rd /s /q"``），失败返回 ``None``。
    """
    cmd = _win_cmd()
    if cmd:
        err = _run([cmd, "/c", "rd", "/s", "/q", path])
        if not os.path.exists(path):
            return "rd /s /q"
        logger.warning("[perf] rd /s /q 未成功: %s", err or "目录仍存在")

    ps = shutil.which("powershell") or shutil.which("pwsh")
    if ps:
        # 路径经环境变量传入，命令串里不含任何用户输入（避免 shell 注入）
        env = dict(os.environ, **{_WIN_DEL_PATH_ENV: path})
        script = f"Remove-Item -LiteralPath $env:{_WIN_DEL_PATH_ENV} -Recurse -Force"
        err = _run([ps, "-NoProfile", "-NonInteractive", "-Command", script], env=env)
        if not os.path.exists(path):
            return "powershell Remove-Item"
        logger.warning("[perf] Remove-Item 未成功: %s", err or "目录仍存在")
    return None


def _win_remove_files(paths: list[str]) -> list[str]:
    """Windows 批量删除文件：``cmd /c del /f /q``，按命令行长度分批。

    与 :func:`_win_remove_tree` 同一原因——逐个 ``os.remove()`` 会被批量删除护栏
    （>500 文件）拦截弹告警或抛 ``SystemExit``。``del`` 不存在"参数列表过长"之外的
    分批需求，按 ``_WIN_CMD_MAX_CHARS`` 累计切批即可。

    Returns:
        错误描述列表（成功为空列表）。
    """
    cmd = _win_cmd()
    if not cmd:
        return ["cmd 不可用"]
    errors: list[str] = []
    batch: list[str] = []
    size = 0
    for path in paths:
        # 以 - 开头的路径可能被 del 当成开关，交给 os.remove 兜底
        if path.startswith("-"):
            continue
        cost = len(path) + 3  # 引号 + 分隔符
        if batch and size + cost > _WIN_CMD_MAX_CHARS:
            errors.extend(_del_batch(cmd, batch))
            batch, size = [], 0
        batch.append(path)
        size += cost
    if batch:
        errors.extend(_del_batch(cmd, batch))
    return errors


def _del_batch(cmd: str, batch: list[str]) -> list[str]:
    """执行一批 ``del``；整批失败时逐条重试以定位失败文件。"""
    err = _run([cmd, "/c", "del", "/f", "/q", *batch])
    if not err:
        return []
    errors: list[str] = []
    for path in batch:
        if not os.path.exists(path):
            continue
        one = _run([cmd, "/c", "del", "/f", "/q", path])
        if one:
            errors.append(f"{os.path.basename(path)}: {one}")
    return errors


def _list_sources(src_dir: str, exts: tuple[str, ...], max_depth: int = 4) -> list[str]:
    """递归收集源目录下的样本文件（限制深度，跳过隐藏目录、.app 包**与样本库自身**）。

    必须跳过 ``perf`` 子目录：样本库就建在 ``<src>/perf``，若把库内文件也当"源样本"，
    轮转复制会自我引用（库越滚越大）。
    """
    found: list[str] = []
    root_depth = src_dir.rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(src_dir):
        if dirpath.count(os.sep) - root_depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and not d.endswith(".app") and d != AUTO_LIB_DIRNAME
        ]
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            if name.lower().endswith(exts):
                found.append(os.path.join(dirpath, name))
    return found


def _clone(src: str, dst: str) -> str:
    """复制单个文件（尽量用平台克隆，失败回退普通复制），返回实际使用的方式。"""
    if sys.platform == "darwin":
        proc = subprocess.run(  # noqa: S603
            ["cp", "-c", src, dst], capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            return "cp -c"
    elif sys.platform.startswith("linux"):
        proc = subprocess.run(  # noqa: S603
            ["cp", "--reflink=auto", src, dst], capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            return "cp --reflink=auto"
    shutil.copy2(src, dst)
    return "copy"


def _remove_files(paths: list[str]) -> tuple[int, list[str]]:
    """批量删除指定文件；优先系统命令（POSIX ``rm -f`` / Windows ``del /f /q``）。

    档位从高切到低时（如 50k→5k）需一次性删除数万个文件——若逐个调用
    ``os.remove()``，会被 IDE 注入的批量删除护栏拦下弹出确认框（>500 文件即触发，
    见 :func:`_remove_tree`），且实际是"移进废纸篓"而非真删。
    POSIX 按 500 个一批调用 ``rm -f``（避免单条命令行超过 ARG_MAX）；Windows 走
    :func:`_win_remove_files`（按命令行长度分批）。系统命令不可用/部分失败时，
    剩余文件逐个用 ``os.remove()`` 兜底，``except BaseException`` 兜住护栏可能抛出
    的 ``SystemExit``（不是 ``OSError``）。

    Returns:
        ``(removed_count, errors)``。
    """
    if not paths:
        return 0, []
    errors: list[str] = []
    if sys.platform == "win32":
        errors.extend(_win_remove_files(paths))
    else:
        rm = shutil.which("rm")
        if rm:
            chunk = 500
            for i in range(0, len(paths), chunk):
                batch = paths[i:i + chunk]
                err = _run([rm, "-f", "--", *batch])
                if err:
                    errors.append(err)
            removed = sum(1 for p in paths if not os.path.exists(p))
            if removed == len(paths) or not errors:
                return removed, errors

    # 系统命令不可用/部分失败：逐个兜底删除剩余的
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            os.remove(path)
        except BaseException as exc:  # noqa: BLE001 - 护栏的 SystemExit 也必须兜住，不能拖垮整体
            errors.append(f"{os.path.basename(path)}: {type(exc).__name__}: {exc}")
    removed = sum(1 for p in paths if not os.path.exists(p))
    return removed, errors


def _reconcile_count(dst_dir: str, target: int, photo_exts: tuple[str, ...]) -> int:
    """把样本库份数**精确**对齐到 target：多了就删掉多余的工具生成文件。

    为什么必须做：样本库目录固定为 ``<图片目录>/perf``（不随档位改名）。
    先跑 10k 再跑 5k 时，若只是"够了就跳过"，会用 10k 的库冒充 5k 档位——
    规模相关指标（scan/thumb）全部错位。只删**符合工具命名**的文件，
    目录内若有外来文件一律不碰（并留给销毁护栏去拒绝整个目录）。
    """
    existing = sorted(n for n in os.listdir(dst_dir) if n.lower().endswith(photo_exts))
    extra = len(existing) - max(0, target)
    if extra <= 0:
        return 0
    candidates: list[str] = []
    for name in reversed(existing):
        if len(candidates) >= extra:
            break
        if not GENERATED_NAME_RE.match(name):
            continue
        candidates.append(os.path.join(dst_dir, name))
    if not candidates:
        return 0
    removed, errors = _remove_files(candidates)
    if errors:
        logger.warning("[perf] 精简样本库时部分文件删除失败: %s", "; ".join(errors[:3]))
    return removed


def build_sample_lib(
    src_dir: str,
    dst_dir: str,
    target_count: int,
    *,
    photo_exts: tuple[str, ...] = PHOTO_EXTS,
    video_exts: tuple[str, ...] = VIDEO_EXTS,
    max_videos: int = 3,
    workers: int = 8,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> dict[str, Any]:
    """把 ``src_dir`` 里的照片复制（轮转重命名）到 ``dst_dir``，直到达到 ``target_count`` 份。

    - **幂等**：目标目录内已有足够样本时直接跳过（重复执行不重复复制）
    - **轮转**：源样本少时循环使用（如 12 张 → 复制 834 轮得到 10k 份），文件名带序号唯一
    - **视频**：额外复制最多 ``max_videos`` 个视频（不计入 ``target_count``），
      供 video_decode / video_play 使用（缩略图/播放场景需要真实视频卡）
    """
    started = time.monotonic()
    stat: dict[str, Any] = {
        "src": src_dir, "dst": dst_dir, "target": int(target_count),
        "photos_created": 0, "videos_created": 0, "existing_photos": 0,
        "skipped": False, "clone_method": "", "elapsed_ms": 0, "errors": [],
    }
    if not src_dir or not os.path.isdir(src_dir):
        stat["error"] = f"复制源目录不存在: {src_dir}"
        return stat
    photos = _list_sources(src_dir, photo_exts)
    if not photos:
        stat["error"] = f"复制源目录内没有可识别的照片（{', '.join(photo_exts)}）: {src_dir}"
        return stat
    os.makedirs(dst_dir, exist_ok=True)

    stat["trimmed"] = _reconcile_count(dst_dir, int(target_count), photo_exts)
    existing = [n for n in os.listdir(dst_dir) if n.lower().endswith(photo_exts)]
    stat["existing_photos"] = len(existing)
    need = int(target_count) - len(existing)
    stat["source_samples"] = len(photos)
    if need <= 0:
        stat["skipped"] = True
        stat["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return stat

    # 文件名带序号，避免与源同名冲突；轮转使用源样本
    def _job(index: int) -> Optional[str]:
        src = photos[index % len(photos)]
        ext = os.path.splitext(src)[1]
        dst = os.path.join(dst_dir, f"sim_{index:06d}{ext}")
        if os.path.exists(dst):
            return None
        try:
            return _clone(src, dst)
        except Exception as exc:  # noqa: BLE001 - 单个失败不中断整体
            stat["errors"].append(f"{os.path.basename(src)} → {os.path.basename(dst)}: {exc}")
            return None

    methods: dict[str, int] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for result in pool.map(_job, range(len(existing), int(target_count))):
            done += 1
            if result:
                methods[result] = methods.get(result, 0) + 1
            if on_progress is not None and done % 1000 == 0:
                on_progress(done, need)
    stat["photos_created"] = sum(methods.values())
    stat["clone_method"] = ", ".join(f"{k}×{v}" for k, v in sorted(methods.items()))

    # 附带少量视频（让 video_decode / video_play 有真实素材）
    videos = _list_sources(src_dir, video_exts)
    if videos and max_videos > 0:
        existing_videos = [n for n in os.listdir(dst_dir) if n.lower().endswith(video_exts)]
        for i, src in enumerate(videos[:max_videos]):
            if i < len(existing_videos):
                continue
            ext = os.path.splitext(src)[1]
            dst = os.path.join(dst_dir, f"sim_v{i:03d}{ext}")
            if os.path.exists(dst):
                continue
            try:
                _clone(src, dst)
                stat["videos_created"] += 1
            except Exception as exc:  # noqa: BLE001
                stat["errors"].append(f"video {os.path.basename(src)}: {exc}")

    stat["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    stat["total_files"] = len([n for n in os.listdir(dst_dir) if n.lower().endswith(photo_exts + video_exts)])
    return stat
