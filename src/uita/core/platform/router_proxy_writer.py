"""
router_proxy_writer — 向 apps/<app>/router_proxy.json 写入 `runtime_define` 字段。

计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md §4.3/§4.4

仅供人工触发的 tools/promote_router_proxy_candidate.py 调用（--apply 分支），
不会在自动化测试跑批过程中被调用（见计划文档 §2 设计取舍）。

写入流程（"锁内重读最新内容再修改"，避免"锁前读、锁后写"丢更新）：
  1. 获取 <map_path>.lock 独占锁（超时 lock_timeout 秒仍未获取则抛 TimeoutError）
  2. 锁内重新从磁盘读取最新内容（不使用锁前缓存的旧内容）
  3. 修改 raw[element_id]["runtime_define"]，其余字段/其他 element_id 一律不变；
     element_id 不存在时新建 {"runtime_define": "..."}（default 留空）
  4. 写临时文件 + os.replace() 原子替换
  5. 释放锁
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from typing import Iterator


def _read_raw(map_path: str) -> dict:
    if not os.path.exists(map_path):
        return {}
    with open(map_path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {}


def _atomic_write(map_path: str, raw: dict) -> None:
    tmp_path = f"{map_path}.tmp{os.getpid()}"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=4)
            f.write("\n")
        os.replace(tmp_path, map_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


@contextmanager
def _file_lock(lock_path: str, timeout: float) -> Iterator[None]:
    """跨平台独占文件锁（尽力而为，见计划文档 §4.4 Windows 局限说明）。

    POSIX: fcntl.flock 独占阻塞锁，用 LOCK_NB + 轮询实现 timeout 语义。
    Windows: fcntl 不可用，退化为 O_CREAT|O_EXCL 独占文件创建锁，失败则轮询
    重试，直到超时；非跨平台强一致，仅覆盖本计划"单次人工执行工具"场景。
    """
    deadline = time.monotonic() + timeout
    try:
        import fcntl  # POSIX only

        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"获取文件锁超时（{timeout}s）: {lock_path}"
                        )
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    except ImportError:
        # Windows 退化方案：O_CREAT|O_EXCL 独占创建锁文件
        while True:
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.close(fd)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"获取文件锁超时（{timeout}s）: {lock_path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            try:
                os.remove(lock_path)
            except OSError:
                pass


def write_runtime_define(
    map_path: str,
    element_id: str,
    strategies: list[str],
    lock_timeout: float = 10.0,
) -> dict:
    """读-改-写 router_proxy.json：仅设置/覆盖 element_id 的 runtime_define 字段。

    Args:
        map_path: apps/<app>/router_proxy.json 路径。
        element_id: rosetta_map.json 对应元素的 id 字段。
        strategies: 大写 token 列表（如 ["TEXT_MATCH", "VISUAL_FP"]），写入时
                    join 为逗号分隔字符串。
        lock_timeout: 获取文件锁的超时秒数，超时抛 TimeoutError（不写入部分数据，
                      异常前文件内容不受影响）。

    Returns:
        写入后的完整 raw dict。
    """
    lock_path = f"{map_path}.lock"
    with _file_lock(lock_path, lock_timeout):
        raw = _read_raw(map_path)
        entry = raw.setdefault(element_id, {})
        if not isinstance(entry, dict):
            entry = {}
        entry["runtime_define"] = ",".join(strategies)
        raw[element_id] = entry
        _atomic_write(map_path, raw)
        return raw
