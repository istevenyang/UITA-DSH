"""app_installer.py — 跨平台 App 安装/覆盖安装编排（--mode install 的业务主体）。

流程：下载 --package-url → （macOS: dmg/pkg/zip 分支安装 + xattr 去隔离 /
Windows: exe 静默安装 + 重新定位真实路径 + 写入 .env.local）。

Windows 安装器若要求管理员权限（清单声明 requireAdministrator），非提权会话
``Popen`` 会抛 ``OSError(winerror=740)``；此时改用 ``win_elevation`` 提权重启
一次（可能弹 UAC 确认），失败则抛带处置指引的 InstallError。见 docs/plans/
docs/plans/done/archive_202610.md。

零 app 专属字符串：所有 app 差异化参数（bundle_id、进程名、候选安装目录等）
均来自 InstallRegistry 注册的 InstallConfig（apps/<name>/register.py 提供），
本文件只依赖标准库 + psutil（进程终止）+ core/ 内部模块（R2 合规）。

下载支持多线程分片（HTTP Range），环境变量 UITA_INSTALL_CHUNKS 控制分片数（默认 4），
设为 1 回退为单线程下载。见 docs/plans/archive/07-db-data-实施纪要.md
和 docs/plans/20260804160000-install-multithread-download.md。
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from uita.config import NOTICE
from uita.core.lifecycle.env_local_store import upsert_env_local
from uita.core.lifecycle.win_elevation import (
    close_process_handle,
    is_elevation_required,
    launch_elevated,
    wait_for_process,
)
from uita.core.platform.install_registry import InstallConfig, InstallRegistry

logger = logging.getLogger(__name__)

_DOWNLOAD_TIMEOUT_S = 300
_WIN_POLL_TIMEOUT_S = 120
_WIN_POLL_INTERVAL_S = 2

# — 多线程分片下载配置（可通过环境变量覆盖）—
_CHUNK_COUNT = int(os.getenv("UITA_INSTALL_CHUNKS", "4"))
_MIN_PARALLEL_SIZE = 10 * 1024 * 1024  # 小于 10MB 的文件不并行
_CHUNK_RETRIES = 2
_CHUNK_TIMEOUT_S = 60  # 单分片连接超时（秒）


class InstallError(Exception):
    """安装流程失败（main.py 转换为 StartupError，退出码 1）。"""


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════

def install_app(app_name: str, package_url: str) -> str:
    """下载并（覆盖）安装 app_name 对应的应用，返回安装后的真实路径。

    失败抛 InstallError。仅支持 macOS(darwin) / Windows(win32)。
    """
    if sys.platform == "darwin":
        platform_name = "macOS"
    elif sys.platform == "win32":
        platform_name = "Windows"
    else:
        raise InstallError(f"--mode install 不支持当前平台: {sys.platform}（仅 macOS/Windows）")

    _validate_url(package_url)

    try:
        cfg = InstallRegistry.get(app_name, platform_name)
    except Exception as exc:
        raise InstallError(str(exc)) from exc

    pkg_path = _download(package_url)
    try:
        return install_app_from_file(app_name, pkg_path)
    finally:
        _cleanup_download(pkg_path)


def install_app_from_file(app_name: str, package_file: str) -> str:
    """安装已由受控下载器落盘的本地包；调用方管理临时文件生命周期。"""
    if sys.platform == "darwin":
        platform_name = "macOS"
    elif sys.platform == "win32":
        platform_name = "Windows"
    else:
        raise InstallError("安装仅支持 macOS/Windows")
    if not os.path.isfile(package_file) or os.path.islink(package_file):
        raise InstallError("本地安装包不存在或为符号链接")
    try:
        cfg = InstallRegistry.get(app_name, platform_name)
    except Exception as exc:
        raise InstallError(str(exc)) from exc
    if platform_name == "macOS":
        return _install_mac(cfg, package_file)
    return _install_windows(cfg, package_file)


# ═══════════════════════════════════════════════════════════════
# 下载（多线程分片 + curl 兜底）
# ═══════════════════════════════════════════════════════════════

def _validate_url(url: str) -> None:
    """拒绝非 http(s):// 前缀，拒绝以 '-' 开头（防止被 shell 解释为参数，见 R26）。"""
    if not url or url.strip().startswith("-"):
        raise InstallError(f"非法的 --package-url: {url!r}")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise InstallError(f"--package-url 必须是 http(s):// 开头，实际: {url!r}")


def _download(url: str) -> str:
    """下载安装包到临时目录，返回本地路径。

    优先使用多线程分片下载（HTTP Range），不可用时回退 curl。
    分片数由环境变量 UITA_INSTALL_CHUNKS 控制（默认 4），设为 1 即单线程。
    """
    cache_dir = os.path.join(tempfile.gettempdir(), "uita_install_cache")
    os.makedirs(cache_dir, exist_ok=True)

    filename = os.path.basename(urlparse(url).path) or f"package-{uuid.uuid4().hex}"
    dst = os.path.join(cache_dir, f"{uuid.uuid4().hex}-{filename}")

    logger.log(NOTICE, "[install] 开始下载: %s → %s", url, dst)
    t_start = time.time()

    # 尝试多线程分片下载
    if _CHUNK_COUNT > 1:
        try:
            file_size = _head_content_length(url)
            if file_size and file_size >= _MIN_PARALLEL_SIZE:
                logger.info("[install] 分片下载: 总大小 %s, %d 线程",
                            _format_size(file_size), _CHUNK_COUNT)
                _download_chunked(url, dst, file_size)
                elapsed = time.time() - t_start
                logger.log(NOTICE, "[install] 分片下载完成: %s (%s), 耗时 %.1fs, 速度 %s/s",
                           dst, _format_size(os.path.getsize(dst)),
                           elapsed, _format_size(int(file_size / elapsed)))
                return dst
            logger.info("[install] 文件较小或无法获取大小，回退单线程下载")
        except Exception as exc:
            logger.warning("[install] 分片下载失败，回退 curl: %s", exc)
            _cleanup_download(dst)

    # 回退：单线程 curl
    _download_curl(url, dst)
    elapsed = time.time() - t_start
    logger.info("[install] curl 下载完成: %s (%s), 耗时 %.1fs",
                dst, _format_size(os.path.getsize(dst)), elapsed)
    return dst


def _safe_url(url: str) -> str:
    """将 URL 中的非 ASCII 字符（如中文文件名/路径）百分号编码。

    http.client 拼装 HTTP 请求行时会对 URL 做 `.encode('ascii')`，若 URL
    含未转义的非 ASCII 字符会抛 `UnicodeEncodeError`（现象：
    "'ascii' codec can't encode characters..."），导致 HEAD/Range 请求必然
    失败并回退到较慢的单线程 curl 下载（curl 走 subprocess 参数传递，不受此
    限制，故能正常工作，问题只影响 urllib 分片下载路径）。
    safe 参数保留常见 URL 保留字符及已存在的 `%`，避免对已转义序列二次编码。
    """
    return quote(url, safe=":/?#[]@!$&'()*+,;=%")


def _head_content_length(url: str) -> int | None:
    """获取文件 Content-Length。

    优先 HEAD，失败则用 GET Range: bytes=0-0 探测（COS 临时链接通常
    只签发 GET 权限，HEAD 返回 403）。
    """
    safe_url = _safe_url(url)

    # 策略 1：HEAD（最快，但 COS 临时链接通常不支持）
    try:
        req = Request(safe_url, method="HEAD")
        with urlopen(req, timeout=_CHUNK_TIMEOUT_S) as resp:
            cl = resp.headers.get("Content-Length")
            if cl:
                return int(cl)
    except Exception:
        pass

    # 策略 2：GET Range: bytes=0-0（仅拉 1 字节获取响应头）
    try:
        req = Request(safe_url)
        req.add_header("Range", "bytes=0-0")
        with urlopen(req, timeout=_CHUNK_TIMEOUT_S) as resp:
            content_range = resp.headers.get("Content-Range", "")
            if content_range:
                # "bytes 0-0/123456" → 123456
                parts = content_range.split("/")
                if len(parts) == 2 and parts[1] != "*":
                    return int(parts[1])
            cl = resp.headers.get("Content-Length")
            if cl:
                return int(cl)
    except Exception as exc:
        logger.debug("[install] Content-Length 探测失败: %s", exc)

    return None


def _download_chunked(url: str, dst: str, total_size: int) -> None:
    """多线程分片下载，合并写入 dst。

    策略：
        1. 按 _CHUNK_COUNT 均分 total_size
        2. ThreadPoolExecutor 并发下载各分片
        3. 按序写入临时文件，最后合并
        4. 单分片失败自动重试 _CHUNK_RETRIES 次
    """
    chunk_size = total_size // _CHUNK_COUNT
    # 每分片的字节范围（左闭右闭）
    ranges: list[tuple[int, int, int]] = []  # (chunk_id, start, end)
    for i in range(_CHUNK_COUNT):
        start = i * chunk_size
        end = total_size - 1 if i == _CHUNK_COUNT - 1 else (i + 1) * chunk_size - 1
        ranges.append((i, start, end))

    # 临时目录放分片
    chunks_dir = f"{dst}.chunks"
    os.makedirs(chunks_dir, exist_ok=True)

    def _download_range(chunk_id: int, start: int, end: int) -> str:
        """下载单个分片 → 临时文件。"""
        chunk_path = os.path.join(chunks_dir, f"chunk_{chunk_id:04d}")
        for attempt in range(1 + _CHUNK_RETRIES):
            try:
                req = Request(_safe_url(url))
                req.add_header("Range", f"bytes={start}-{end}")
                with urlopen(req, timeout=_CHUNK_TIMEOUT_S) as resp:
                    data = resp.read()
                with open(chunk_path, "wb") as f:
                    f.write(data)
                actual = os.path.getsize(chunk_path)
                expected = end - start + 1
                if actual != expected:
                    raise IOError(f"分片大小不匹配: expected={expected}, actual={actual}")
                return chunk_path
            except Exception:
                if attempt < _CHUNK_RETRIES:
                    logger.debug("[install] 分片 %d 第 %d 次重试", chunk_id, attempt + 1)
                    time.sleep(1)
                else:
                    raise

    # 并发下载
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=_CHUNK_COUNT) as executor:
        future_map = {}
        for chunk_id, start, end in ranges:
            future_map[executor.submit(_download_range, chunk_id, start, end)] = chunk_id

        for future in as_completed(future_map):
            chunk_id = future_map[future]
            try:
                future.result()
            except Exception as exc:
                failed.append(f"chunk_{chunk_id}: {exc}")

    if failed:
        raise InstallError(f"分片下载失败: {', '.join(failed)}")

    # 按序合并分片
    with open(dst, "wb") as out:
        for chunk_id in range(_CHUNK_COUNT):
            chunk_path = os.path.join(chunks_dir, f"chunk_{chunk_id:04d}")
            if not os.path.isfile(chunk_path):
                raise InstallError(f"分片 {chunk_id} 丢失，合并失败")
            with open(chunk_path, "rb") as f:
                out.write(f.read())

    # 校验总大小
    actual_total = os.path.getsize(dst)
    if actual_total != total_size:
        raise InstallError(
            f"下载文件大小不匹配: expected={total_size}, actual={actual_total}"
        )

    # 清理分片临时目录
    shutil.rmtree(chunks_dir, ignore_errors=True)


def _download_curl(url: str, dst: str) -> None:
    """单线程 curl 下载（兜底，保持原逻辑不变）。"""
    logger.log(NOTICE, "[install] curl 单线程下载")
    try:
        proc = subprocess.run(
            ["curl", "-fL", "--connect-timeout", "30", "-o", dst, url],
            timeout=_DOWNLOAD_TIMEOUT_S,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise InstallError(f"下载超时（>{_DOWNLOAD_TIMEOUT_S}s）: {url}") from exc
    except FileNotFoundError as exc:
        raise InstallError("下载失败：本机未安装 curl") from exc

    if proc.returncode != 0 or not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        _cleanup_download(dst)
        raise InstallError(
            f"下载失败（curl returncode={proc.returncode}）: {url}\n{proc.stderr.strip()}"
        )
    logger.log(NOTICE, "[install] curl 下载完成: %s (%d bytes)", dst, os.path.getsize(dst))


def _format_size(size_bytes: int) -> str:
    """格式化字节数为可读字符串。"""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f}{unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f}TB"


def _cleanup_download(pkg_path: str) -> None:
    try:
        if pkg_path and os.path.isfile(pkg_path):
            os.remove(pkg_path)
    except Exception as exc:  # noqa: BLE001  best-effort 清理
        logger.debug("[install] 清理下载临时文件失败（忽略）: %s", exc)


# ═══════════════════════════════════════════════════════════════
# macOS 安装
# ═══════════════════════════════════════════════════════════════

def _install_mac(cfg: InstallConfig, pkg_path: str) -> str:
    ext = _strip_query(pkg_path).lower()
    if ext.endswith(".dmg"):
        return _install_mac_dmg(cfg, pkg_path)
    if ext.endswith(".pkg"):
        return _install_mac_pkg(cfg, pkg_path)
    if ext.endswith(".zip"):
        return _install_mac_zip(cfg, pkg_path)
    raise InstallError(f"不支持的 macOS 安装包格式: {pkg_path}（仅支持 .dmg/.pkg/.zip）")


def _strip_query(path: str) -> str:
    return path.split("?", 1)[0]


def _install_mac_dmg(cfg: InstallConfig, dmg_path: str) -> str:
    mount_point = os.path.join(tempfile.gettempdir(), f"uita_dmg_{uuid.uuid4().hex}")
    os.makedirs(mount_point, exist_ok=True)
    proc = subprocess.run(
        ["hdiutil", "attach", "-nobrowse", "-noverify", "-readonly",
         "-mountpoint", mount_point, dmg_path],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise InstallError(f"hdiutil attach 失败: {proc.stderr.strip()}")
    try:
        app_src = _find_app_bundle(mount_point)
        if not app_src:
            raise InstallError(f"dmg 挂载点未找到 .app: {mount_point}")
        return _overwrite_install_mac_app(cfg, app_src)
    finally:
        subprocess.run(["hdiutil", "detach", "-force", mount_point],
                        capture_output=True, text=True)


def _install_mac_pkg(cfg: InstallConfig, pkg_path: str) -> str:
    """installer -pkg 需要 root，密码走 env-only（UITA_MAC_INSTALLER_SUDO_PASSWORD，
    见 .env.local），不硬编码在源码中；sudo -S 从 stdin 读取，不出现在命令行参数/
    进程列表中。"""
    password = os.environ.get("UITA_MAC_INSTALLER_SUDO_PASSWORD", "")
    if not password:
        raise InstallError(
            "安装 .pkg 需要管理员权限，请在 .env.local 配置 "
            "UITA_MAC_INSTALLER_SUDO_PASSWORD"
        )
    proc = subprocess.run(
        ["sudo", "-S", "installer", "-pkg", pkg_path, "-target", "/"],
        input=f"{password}\n",
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise InstallError(f"installer -pkg 失败 (returncode={proc.returncode})")
    if not cfg.mac_app_dst_path or not os.path.isdir(cfg.mac_app_dst_path):
        raise InstallError(
            f".pkg 安装命令已执行成功，但未在预期路径找到应用: {cfg.mac_app_dst_path}"
        )
    logger.log(NOTICE, "[install] .pkg 安装完成: %s", cfg.mac_app_dst_path)
    _xattr_remove_quarantine(cfg.mac_app_dst_path)
    _verify_bundle_health(cfg.mac_app_dst_path)
    return cfg.mac_app_dst_path


def _install_mac_zip(cfg: InstallConfig, zip_path: str) -> str:
    extract_dir = os.path.join(tempfile.gettempdir(), f"uita_zip_{uuid.uuid4().hex}")
    os.makedirs(extract_dir, exist_ok=True)
    proc = subprocess.run(["unzip", "-q", "-o", zip_path, "-d", extract_dir],
                           capture_output=True, text=True)
    if proc.returncode != 0:
        raise InstallError(f"unzip 失败: {proc.stderr.strip()}")
    app_src = _find_app_bundle(extract_dir)
    if not app_src:
        raise InstallError(f"zip 解压后未找到 .app: {extract_dir}")
    try:
        return _overwrite_install_mac_app(cfg, app_src)
    finally:
        shutil.rmtree(extract_dir, ignore_errors=True)


def _find_app_bundle(root: str) -> Optional[str]:
    matches = glob.glob(os.path.join(root, "*.app")) or glob.glob(os.path.join(root, "**", "*.app"), recursive=True)
    return matches[0] if matches else None


def _overwrite_install_mac_app(cfg: InstallConfig, app_src: str) -> str:
    """覆盖安装 .app（dmg/zip 共用）：杀进程 → 备份旧版本 → rsync 拷贝
    （失败 ditto 兜底）→ 成功删备份/失败还原 → 去隔离。"""
    dst = cfg.mac_app_dst_path
    if not dst:
        raise InstallError("InstallConfig.mac_app_dst_path 未配置，无法确定安装目标路径")

    if cfg.mac_process_name:
        subprocess.run(["pkill", "-x", cfg.mac_process_name], capture_output=True)
        time.sleep(1)

    backup_path = ""
    if os.path.exists(dst):
        backup_path = f"{dst}.bak-{int(time.time())}"
        shutil.move(dst, backup_path)

    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        proc = subprocess.run(
            ["rsync", "-a", "--delete-during", f"{app_src}/", f"{dst}/"],
            capture_output=True, text=True,
        )
        if proc.returncode != 0:
            logger.warning("[install] rsync 失败，尝试 ditto 兜底: %s", proc.stderr.strip())
            if os.path.exists(dst):
                shutil.rmtree(dst, ignore_errors=True)
            proc2 = subprocess.run(["ditto", "--noqtn", app_src, dst], capture_output=True, text=True)
            if proc2.returncode != 0:
                raise InstallError(f"覆盖安装失败（rsync 与 ditto 均失败）: {proc2.stderr.strip()}")
    except InstallError:
        _restore_backup(dst, backup_path)
        raise
    except Exception as exc:
        _restore_backup(dst, backup_path)
        raise InstallError(f"覆盖安装异常: {exc}") from exc

    if backup_path and os.path.exists(backup_path):
        shutil.rmtree(backup_path, ignore_errors=True)

    logger.log(NOTICE, "[install] 覆盖安装完成: %s", dst)
    _xattr_remove_quarantine(dst)
    _verify_bundle_health(dst)
    return dst


def _restore_backup(dst: str, backup_path: str) -> None:
    if not backup_path or not os.path.exists(backup_path):
        return
    if os.path.exists(dst):
        shutil.rmtree(dst, ignore_errors=True)
    shutil.move(backup_path, dst)
    logger.info("[install] 安装失败，已从备份还原: %s", dst)


def _text(value) -> str:
    """把 subprocess 输出字段安全转成字符串（单测里可能被 mock 成非 str）。"""
    return value if isinstance(value, str) else ""


def quarantine_value(app_path: str) -> str:
    """读取 app_path 上的 com.apple.quarantine 属性值；不存在/读取失败返回空串。"""
    try:
        proc = subprocess.run(
            ["xattr", "-p", "com.apple.quarantine", app_path],
            capture_output=True, text=True,
        )
    except Exception as exc:  # noqa: BLE001  xattr 缺失等，按"无隔离"处理并留痕
        logger.debug("[install] 读取隔离属性失败（按无隔离处理）: %s", exc)
        return ""
    if getattr(proc, "returncode", 1) != 0:
        return ""
    return _text(getattr(proc, "stdout", "")).strip()


def _sudo_clear_quarantine(app_path: str) -> bool:
    """用 sudo -S 重试 `xattr -cr`（密码 env-only，只走 stdin，不进命令行参数）。

    返回是否成功。未配置 `UITA_MAC_INSTALLER_SUDO_PASSWORD` 时直接返回 False。
    """
    password = os.environ.get("UITA_MAC_INSTALLER_SUDO_PASSWORD", "")
    if not password:
        return False
    try:
        proc = subprocess.run(
            ["sudo", "-S", "xattr", "-cr", app_path],
            input=f"{password}\n", capture_output=True, text=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[install] sudo xattr -cr 执行异常: %s", exc)
        return False
    if proc.returncode != 0:
        logger.warning("[install] sudo xattr -cr 失败: %s", _text(proc.stderr).strip())
        return False
    return True


def _xattr_remove_quarantine(app_path: str) -> None:
    """去除 Gatekeeper 隔离属性（fail-loud：清不掉就抛 InstallError）。

    去隔离是 `--mode install` 的契约步骤之一，不能"warning 后照样报安装成功"：
        1. 先以当前用户执行 `xattr -cr`（dmg/zip 覆盖安装后的常规路径）；
        2. 失败（典型：bundle 内文件属主为 root）→ 用 `sudo -S` 重试一次，
           密码只从 `UITA_MAC_INSTALLER_SUDO_PASSWORD` 读取；
        3. 无论走哪条路径，最后都用 `xattr -p com.apple.quarantine` 复核，
           属性仍在则抛 `InstallError`（`xattr -cr` 返回 0 不等于已清干净）。
    """
    try:
        proc = subprocess.run(["xattr", "-cr", app_path], capture_output=True, text=True)
    except Exception as exc:  # noqa: BLE001
        raise InstallError(f"去隔离失败：无法执行 xattr（{exc}）") from exc

    if proc.returncode != 0:
        logger.warning(
            "[install] xattr -cr 返回非 0，改用 sudo 重试: %s", _text(proc.stderr).strip(),
        )
        if not _sudo_clear_quarantine(app_path):
            raise InstallError(
                "去隔离失败：xattr -cr 返回非 0，且 sudo 重试不可用或失败。"
                "请在 .env.local 配置 UITA_MAC_INSTALLER_SUDO_PASSWORD（需管理员密码），"
                f"或手工执行 sudo xattr -cr {app_path!r} 后重试安装"
            )

    remaining = quarantine_value(app_path)
    if remaining:
        raise InstallError(
            f"去隔离失败：{app_path} 仍带 com.apple.quarantine（{remaining}）"
        )
    logger.log(NOTICE, "[install] 已去除隔离属性（xattr 复核通过）: %s", app_path)


def _quarantine_deep(app_path: str) -> str:
    """递归扫描 bundle 内所有 com.apple.quarantine 命中行（顶层干净 ≠ 内层干净）。

    返回前 5 行摘要；干净或读取失败返回空串（读取失败不阻断，避免 xattr 缺失误杀）。
    """
    try:
        proc = subprocess.run(["xattr", "-lr", app_path], capture_output=True, text=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[install] 递归读取隔离属性失败（按无隔离处理）: %s", exc)
        return ""
    if getattr(proc, "returncode", 1) != 0:
        return ""
    hits = [
        line.strip()
        for line in _text(getattr(proc, "stdout", "")).splitlines()
        if "com.apple.quarantine" in line
    ]
    return " | ".join(hits[:5])


def _codesign_state(app_path: str) -> tuple[Optional[bool], str]:
    """`codesign --verify --deep --strict` 结论：True=有效，False=无效，None=无法评估。

    无法评估（codesign 缺失/异常）按 unknown 处理、不阻断安装，避免工具缺失误杀。
    """
    try:
        proc = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", app_path],
            capture_output=True, text=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[install] 代码签名校验无法执行（按 unknown 处理）: %s", exc)
        return None, ""
    detail = (_text(getattr(proc, "stdout", "")) + _text(getattr(proc, "stderr", ""))).strip()
    return getattr(proc, "returncode", 1) == 0, detail[:500]


def _spctl_state(app_path: str) -> tuple[Optional[bool], str]:
    """`spctl -a -vvv` 结论：True=accepted，False=rejected，None=无法评估。"""
    try:
        proc = subprocess.run(["spctl", "-a", "-vvv", app_path], capture_output=True, text=True)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[install] Gatekeeper 评估无法执行（按 unknown 处理）: %s", exc)
        return None, ""
    detail = (_text(getattr(proc, "stdout", "")) + _text(getattr(proc, "stderr", ""))).strip()
    if getattr(proc, "returncode", 1) != 0:
        return False, detail[:500]
    return "accepted" in detail.lower(), detail[:500]


def bundle_health(app_path: str) -> dict:
    """macOS 产物体检汇总：递归隔离 + 代码签名 + Gatekeeper 评估。

    `ok` 只由**阻断项**决定（递归隔离残留 / 签名无效）；Gatekeeper 评估只作事实回传，
    不阻断（未公证的内测包历史上可正常运行，不能被体检误杀）。
    """
    deep = _quarantine_deep(app_path)
    codesign_ok, codesign_detail = _codesign_state(app_path)
    spctl_ok, spctl_detail = _spctl_state(app_path)
    blocking: list[str] = []
    if deep:
        blocking.append(f"bundle 内仍带 com.apple.quarantine（{deep}）")
    if codesign_ok is False:
        blocking.append(f"代码签名校验失败（{codesign_detail or '无输出'}）")
    return {
        "ok": not blocking,
        "blocking": blocking,
        "quarantine_deep": deep,
        "codesign_ok": codesign_ok,
        "codesign_detail": codesign_detail,
        "spctl_ok": spctl_ok,
        "spctl_detail": spctl_detail,
    }


def _verify_bundle_health(app_path: str) -> dict:
    """安装产物体检（fail-loud）：隔离残留 / 签名无效直接抛 InstallError。

    背景：真机链路上出现过「install 报成功 → 真机启动被 macOS 判『已损坏』→
    退化为 session_unavailable」的链路，缺陷被推迟到真机阶段且难以归因。把体检
    前置到 install，让包/签名问题在安装阶段以明确错误暴露（CI 据此终止整轮）。
    """
    logger.log(NOTICE, "[install] 产物体检（递归隔离 / 代码签名 / Gatekeeper）: %s", app_path)
    health = bundle_health(app_path)
    if health["spctl_ok"] is False:
        logger.warning(
            "[install] Gatekeeper 评估未通过（不阻断安装，但真机启动可能被拦截，"
            "常见于未公证的内测包）: %s", health["spctl_detail"] or "<无输出>",
        )
    if not health["ok"]:
        raise InstallError(
            "安装产物体检失败：" + "；".join(health["blocking"])
            + "。该产物在真机会被 macOS 判为「已损坏」或直接拒启，请重新出包"
            f"（重签/重新公证）后重试；人工复查可执行 `codesign --verify --deep "
            f"--strict {app_path!r}` 与 `xattr -lr {app_path!r} | grep quarantine`"
        )
    logger.log(NOTICE, "[install] 产物体检通过（无隔离残留 · 签名有效）: %s", app_path)
    return health


def _read_mac_bundle_version(app_path: str) -> str:
    """读 macOS bundle 的 CFBundleShortVersionString；读不到返回空串。"""
    plist = os.path.join(app_path or "", "Contents", "Info.plist")
    if not os.path.isfile(plist):
        return ""
    try:
        proc = subprocess.run(
            ["/usr/libexec/PlistBuddy", "-c", "Print :CFBundleShortVersionString", plist],
            capture_output=True, text=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[install] 读取版本失败（忽略）: %s", exc)
        return ""
    if proc.returncode != 0:
        return ""
    return _text(getattr(proc, "stdout", "")).strip()


def collect_install_facts(
    app_name: str,
    target_path: str,
    package_url: str,
    platform_name: str,
    duration_s: float = 0.0,
) -> dict:
    """汇总安装事实（供 `--install-report` 落盘、CI context 与报告引用）。

    字段：app / package_url / platform / target_path / version / quarantine /
    quarantine_deep / codesign / spctl / duration_s / installed_at。
    `quarantine` 为顶层属性值（空串 = 无），`quarantine_deep` 为递归扫描摘要
    （空串 = bundle 内也干净），`codesign` / `spctl` 取 `ok|invalid|unknown` /
    `accepted|rejected|unknown`，供执行机与报告直接引用。
    """
    facts = {
        "app": app_name,
        "package_url": package_url,
        "platform": platform_name,
        "target_path": target_path or "",
        "version": "",
        "quarantine": "",
        "quarantine_deep": "",
        "codesign": "",
        "spctl": "",
        "duration_s": round(float(duration_s or 0.0), 1),
        "installed_at": datetime.now().isoformat(timespec="seconds"),
    }
    if platform_name == "macOS":
        facts["version"] = _read_mac_bundle_version(target_path)
        facts["quarantine"] = quarantine_value(target_path)
        health = bundle_health(target_path)
        facts["quarantine_deep"] = health["quarantine_deep"]
        facts["codesign"] = {True: "ok", False: "invalid"}.get(
            health["codesign_ok"], "unknown",
        )
        facts["spctl"] = {True: "accepted", False: "rejected"}.get(
            health["spctl_ok"], "unknown",
        )
    return facts


# ═══════════════════════════════════════════════════════════════
# Windows 安装
# ═══════════════════════════════════════════════════════════════

def _install_windows(cfg: InstallConfig, exe_path: str) -> str:
    if cfg.win_process_name:
        _kill_windows_process(cfg.win_process_name)

    silent_args = cfg.win_silent_args or ["/S"]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    elevated_handle: Optional[int] = None
    try:
        proc = subprocess.Popen([exe_path] + list(silent_args), creationflags=creationflags)
        proc.wait(timeout=_WIN_POLL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        pass  # 部分安装器静默安装不会及时退出，继续走轮询检测真实路径
    except OSError as exc:
        # WinError 740：安装器清单要求管理员权限，非提权 CreateProcess 被 OS 拒绝，
        # 此时不存在可等待的进程对象 → 改用 ShellExecuteExW("runas") 提权重启一次。
        if not is_elevation_required(exc):
            raise InstallError(f"启动安装程序失败: {exc}") from exc
        logger.warning(
            "[install] 安装器要求管理员权限（WinError 740），改用提权方式启动"
            "（可能弹出 UAC 确认）: %s", exe_path,
        )
        elevated_handle = launch_elevated(exe_path, list(silent_args))
        if elevated_handle is None:
            raise InstallError(
                "启动安装程序失败: 安装器要求管理员权限（WinError 740），自动提权失败"
                "（UAC 被取消，或当前为无交互桌面会话）。请以管理员身份运行本进程后重试。"
            )
        # 与上方非提权分支同语义等待安装器退出：否则安装器仍在写盘时，
        # _poll_resolve_win_path 可能命中未被覆盖的旧版本残留路径。
        wait_for_process(elevated_handle, _WIN_POLL_TIMEOUT_S)
    except Exception as exc:
        raise InstallError(f"启动安装程序失败: {exc}") from exc
    finally:
        if elevated_handle is not None:
            close_process_handle(elevated_handle)

    resolved = _poll_resolve_win_path(cfg)
    if not resolved:
        raise InstallError(
            f"安装命令已执行，但在 {_WIN_POLL_TIMEOUT_S}s 内未能定位到 {cfg.win_exe_name} "
            f"真实路径（可能安装器仍有确认弹窗未处理）"
        )
    upsert_env_local(cfg.win_env_cache_key, resolved)
    logger.info("[install] Windows 安装完成，路径已写入 .env.local: %s=%s",
                cfg.win_env_cache_key, resolved)
    return resolved


def _kill_windows_process(process_name: str) -> None:
    try:
        import psutil
    except ImportError:
        logger.debug("[install] psutil 不可用，跳过关闭运行中实例")
        return
    target = process_name.lower()
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name == target or name == f"{target}.exe":
                proc.kill()
        except Exception:  # noqa: BLE001
            continue
    time.sleep(1)


def _poll_resolve_win_path(cfg: InstallConfig) -> Optional[str]:
    from uita.core.lifecycle.win_app_locator import resolve_win_app_path

    deadline = time.monotonic() + _WIN_POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        resolved = resolve_win_app_path(
            cfg.win_exe_name,
            install_roots=cfg.win_install_roots,
            shortcut_paths=cfg.win_shortcut_paths,
            registry_keywords=cfg.win_registry_keywords,
            force=True,
        )
        if resolved:
            return resolved
        time.sleep(_WIN_POLL_INTERVAL_S)
    return None


__all__ = [
    "install_app", "InstallError", "collect_install_facts", "quarantine_value",
    "bundle_health",
]
