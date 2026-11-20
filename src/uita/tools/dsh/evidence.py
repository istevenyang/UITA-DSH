"""会话证据落盘：``<REPORT_DIR>/dsh_runs/<session_id>/``。

目录约定（R9：截图/产物只写 report 目录，URL 只回相对会话目录的文件名）：

- ``session.json``   会话快照（状态、预算、结论）
- ``events.jsonl``   逐条事件（seq 单调递增，可回放/可增量拉取）
- ``artifacts/``     截图等二进制产物
"""
from __future__ import annotations

import json
import os
import re
import secrets
import time
from typing import Any, Optional

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


class EvidenceError(Exception):
    """证据路径非法或落盘失败。"""


def new_session_id(prefix: str = "dsh") -> str:
    """生成 ``dsh-<YYYYmmdd-HHMMSS>-<hex6>`` 形态的会话 id。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}-{secrets.token_hex(3)}"


def _safe_join(root: str, name: str) -> str:
    """把 ``name`` 安全拼到 ``root`` 下：只接受单层文件名，拒绝穿越。"""
    candidate = str(name or "").strip()
    if not candidate or not _SAFE_NAME_RE.match(candidate):
        raise EvidenceError(f"非法文件名: {name!r}")
    if candidate in {".", ".."} or os.path.sep in candidate or "/" in candidate:
        raise EvidenceError(f"非法文件名: {name!r}")
    target = os.path.abspath(os.path.join(os.path.abspath(root), candidate))
    if os.path.dirname(target) != os.path.abspath(root):
        raise EvidenceError(f"路径越界: {name!r}")
    return target


class EvidenceStore:
    """单个会话的证据目录。"""

    def __init__(self, session_id: str, root: str = "") -> None:
        if not root:
            from uita.config import UITA_DSH_OUT_ROOT

            root = UITA_DSH_OUT_ROOT
        self.root = os.path.abspath(root)
        self.session_id = session_id
        self.session_dir = _safe_join(self.root, session_id)
        self.artifacts_dir = os.path.join(self.session_dir, "artifacts")
        self.events_path = os.path.join(self.session_dir, "events.jsonl")
        self.session_path = os.path.join(self.session_dir, "session.json")
        self._prepared = False

    # ------------------------------------------------------------------

    def ensure(self) -> str:
        if not self._prepared:
            os.makedirs(self.artifacts_dir, exist_ok=True)
            self._prepared = True
        return self.session_dir

    def artifact_path(self, name: str) -> str:
        self.ensure()
        return _safe_join(self.artifacts_dir, name)

    def append_event(self, event: dict[str, Any]) -> str:
        self.ensure()
        with open(self.events_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return self.events_path

    def write_json(self, name: str, data: dict[str, Any]) -> str:
        self.ensure()
        path = _safe_join(self.session_dir, name)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path

    def read_events(self, limit: int = 0) -> list[dict[str, Any]]:
        if not os.path.isfile(self.events_path):
            return []
        events: list[dict[str, Any]] = []
        with open(self.events_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # 坏行容错：崩溃写入的半行不影响回放
        if limit > 0:
            return events[-limit:]
        return events

    def list_artifacts(self) -> list[dict[str, Any]]:
        if not os.path.isdir(self.artifacts_dir):
            return []
        items = []
        for name in sorted(os.listdir(self.artifacts_dir)):
            full = os.path.join(self.artifacts_dir, name)
            if not os.path.isfile(full):
                continue
            items.append({
                "name": name,
                "size_bytes": os.path.getsize(full),
                "url": f"/sessions/{self.session_id}/artifacts/{name}",
            })
        return items

    def resolve_artifact(self, name: str) -> Optional[str]:
        """产物读取入口：非法名返回 None（由 HTTP 层转 404）。"""
        try:
            path = _safe_join(self.artifacts_dir, name)
        except EvidenceError:
            return None
        return path if os.path.isfile(path) else None
