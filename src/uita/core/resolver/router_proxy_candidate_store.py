"""
RouterProxyCandidateStore — router_proxy.json `runtime_define` 候选记录（阶段 1）。

计划文档：docs/plans/archive/03-resolver-rosetta-实施纪要.md §4.1

与 core/resolver/l3_8_visual_fingerprint.py::VisualFingerprintResolver 的
fingerprint_index.json 命中次数模式完全同构：本机缓存，容错加载，原子写，
不加跨进程锁（理由见计划文档 §2/§4.4：候选记录即使偶发计数丢失，只影响
"阈值达成时间"，不影响正确性）。

本机缓存目录结构：resources/router_proxy_candidates/<app>_<platform>.json
{
    "<element_id>": {
        "description": "元素描述（供人工审阅）",
        "candidates": {
            "<STRATEGY>": {"hit_count": N, "first_hit_at": "...", "last_hit_at": "..."}
        }
    }
}

本模块只做候选记录的读写，不引用 core/resolver/router.py 的 ResolveStrategy
枚举（strategy 参数为字符串 token，由调用方传入 .value），保持与
core/platform/router_proxy_loader.py 一致的"字符串级"处理原则。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_CANDIDATE_DIR = "resources/router_proxy_candidates"


class RouterProxyCandidateStore:
    def __init__(
        self,
        app_name: str,
        platform: str,
        candidate_dir: str = DEFAULT_CANDIDATE_DIR,
    ):
        self._path = Path(candidate_dir) / f"{app_name}_{platform}.json"
        self._data: dict = self._load()

    # ── 加载 / 保存（容错，同 fingerprint_index.json 模式）─────────────

    def _load(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.debug(
                "[router_proxy_candidate] 候选文件读取失败（忽略，视为空）: %s", exc
            )
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        """原子写：temp file + os.replace，不加跨进程锁（见模块 docstring）。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + f".tmp{os.getpid()}")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self._path)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    # ── 记录 / 查询 ─────────────────────────────────────────────────

    def record_hit(self, element_id: str, strategy: str, description: str = "") -> None:
        """累加 (element_id, strategy) 命中次数；element_id 为空直接忽略。"""
        if not element_id:
            return
        now = datetime.utcnow().isoformat()
        # 重读磁盘最新内容，减少多进程并发下"读旧值再写"的丢失概率（尽力而为，
        # 非强一致，见计划文档 §4.4 权衡说明）。
        self._data = self._load() or self._data

        entry = self._data.setdefault(element_id, {"description": description, "candidates": {}})
        if description:
            entry["description"] = description
        candidates = entry.setdefault("candidates", {})
        record = candidates.setdefault(
            strategy, {"hit_count": 0, "first_hit_at": now, "last_hit_at": now}
        )
        record["hit_count"] = record.get("hit_count", 0) + 1
        record["last_hit_at"] = now
        record.setdefault("first_hit_at", now)

        self._save()

    def all_candidates(self) -> dict:
        """供 promote 工具读取，返回结构化候选（element_id → description/candidates）。"""
        return dict(self._data)