"""perf AI 分析执行器：基于内置 DSH 无头会话（统一使用 DSH_API_URL 与 DSH_API_KEY）。"""
from __future__ import annotations

import re
import uuid
from typing import Any, Callable, Optional

from uita import config

__all__ = [
    "default_workdir",
    "run_agent",
    "session_id_for",
]


def default_workdir() -> str:
    return config.ROOT_DIR


def session_id_for(*, app: str = "", run_id: str = "") -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", f"{app}-{run_id}").strip("-")
    token = uuid.uuid4().hex[:8]
    return f"perf-{slug}-{token}" if slug else f"perf-{token}"


def run_agent(
    prompt: str,
    *,
    workdir: str = "",
    model: str = "",
    session_id: str = "",
    timeout_s: int = 0,
    heartbeat_s: int = 0,
    out_dir: str = "",
    on_log: Optional[Callable[[str], None]] = None,
    stall_timeout_s: int = 0,
    loop_repeat: int = 0,
    artifact_path: str = "",
    artifact_grace_s: int = 0,
    **_extra: Any,
) -> Any:
    """调用内置 DSH 无头分析引擎（统一读取 DSH_API_URL / DSH_API_KEY）。"""
    from uita.tools.dsh.headless import make_perf_ai_runner

    runner = make_perf_ai_runner()
    return runner(
        prompt,
        out_dir=out_dir,
        model=model or config.PERF_AI_MODEL,
        timeout_s=timeout_s or config.PERF_AI_TIMEOUT_S,
        heartbeat_s=heartbeat_s or config.PERF_AI_HEARTBEAT_S,
        stall_s=stall_timeout_s or config.PERF_AI_STALL_S,
        loop_repeat=loop_repeat or config.PERF_AI_LOOP_REPEAT,
        artifact_path=artifact_path,
        artifact_grace_s=float(artifact_grace_s or 0.0),
        session_id=session_id,
        workdir=workdir or default_workdir(),
        on_log=on_log,
    )
