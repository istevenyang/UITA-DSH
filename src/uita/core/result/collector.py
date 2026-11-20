"""
ResultCollector — 聚合步骤结果，写出 result.json。

result.json 格式对齐 标准报告规范（外部报告解析器 已适配）：
  - 顶层 标准报告规范 字段：case_file/case_id/case_name/case_model/case_description/
    total/passed/failed/skipped/steps/artifacts
  - step 标准报告规范 字段：index/name/status/screenshots/assertion_text/error/
    begin_time/end_time/duration_ms/description/raw_step
  - UITA 额外字段（追加在末尾，外部报告解析器 不解析）：duration_ms/result_dir
  - step 额外字段：message/screenshot(向后兼容)/actions

结果目录规范：
  report/<YYYYMMDD-HHmmss>/<case_file>.result/
    ├── result.json
    └── screenshots/
        └── step_NN_<name>.png
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class ActionRecord:
    """单个 action 执行记录（每步可含多个）。"""
    action: str          # "click" / "assert" / "wait" / "type" ...
    target: str = ""     # 操作目标元素名
    status: str = "passed"
    screenshot: Optional[str] = None         # 操作后截图（after），相对 result_dir 路径
    screenshot_before: Optional[str] = None  # 操作前截图（before），ActionNode 专属
    existence_strategy: Optional[str] = None  # assert/verify 可观测字段：structured/l4b_visual
    # 2026-07-15 新增：click/type/hover 操作前后页面 hash 是否发生变化（可观测字段，默认
    # 关闭 UITA_ENABLE_PAGE_HASH_VERIFY）。None=未启用该验证/无法判定（截图或 hash 计算
    # 失败/action 本身失败/非适用 action）；True=变化；False=未变化（已记录 warning，
    # 不影响 action 本身的 passed/failed）。见 docs/plans/20260715180000-...。
    page_changed: Optional[bool] = None
    extra: dict[str, Any] = field(default_factory=dict)  # action 输出的结构化诊断信息


@dataclass
class StepResult:
    """单步执行结果。

    标准报告规范 字段（外部报告解析器 必须解析）：index/name/status/screenshots/
    assertion_text/error/begin_time/end_time/duration_ms/description/raw_step
    UITA 额外字段（追加）：message/screenshot(向后兼容)/actions
    """
    # ── 现有字段（向后兼容 + UITA 额外）──
    name: str = ""
    status: str = "passed"        # "passed" | "failed" | "skipped"
    duration_ms: float = 0.0
    message: str = ""             # UITA 额外：断言/错误消息
    screenshot: Optional[str] = None   # UITA 额外：向后兼容（末尾 action 截图）
    actions: list = field(default_factory=list)  # UITA 额外：list[ActionRecord]

    # ── 标准报告规范 必须字段（新增）──
    index: int = 0
    screenshots: list = field(default_factory=list)  # 截图路径列表
    assertion_text: str = ""
    error: Optional[str] = None
    begin_time: int = 0
    end_time: int = 0
    description: str = ""
    raw_step: list = field(default_factory=list)


@dataclass
class CaseResult:
    """单条用例执行结果。

    标准报告规范 字段（外部报告解析器 必须解析）：case_file/case_id/case_name/case_model/
    case_description/total/passed/failed/skipped/steps/artifacts
    UITA 额外字段（追加）：duration_ms/result_dir

    注意：passed/failed/skipped/total 为 computed property（从 steps 推导），
    is_passed 替代旧的 passed: bool 语义。
    """
    case_name: str = ""
    case_file: str = ""
    case_id: str = ""
    case_model: str = ""
    case_description: str = ""
    result_dir: str = ""
    duration_ms: float = 0.0
    steps: list[StepResult] = field(default_factory=list)
    artifacts: dict = field(default_factory=dict)

    def add_step(self, step: StepResult) -> None:
        self.steps.append(step)

    # ── 标准报告规范 computed properties ──

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def passed(self) -> int:
        """通过 step 数（int，标准报告规范 格式）。"""
        return sum(1 for s in self.steps if s.status == "passed")

    @property
    def failed(self) -> int:
        """失败 step 数（int，标准报告规范 格式）。"""
        return sum(1 for s in self.steps if s.status == "failed")

    @property
    def skipped(self) -> int:
        """跳过 step 数（int，标准报告规范 格式）。"""
        return sum(1 for s in self.steps if s.status == "skipped")

    @property
    def is_passed(self) -> bool:
        """是否全部通过（bool，替代旧的 passed: bool 语义）。"""
        return self.failed == 0

    @property
    def first_failure_summary(self) -> str:
        """取第一个 status=="failed" 的 step 的失败摘要（error 优先，回退
        message），供测试平台 POST_COMMENT 评论组装使用（见
        core/result/result_sync.py::build_comment()）。无失败 step 返回空串。
        """
        for step in self.steps:
            if step.status == "failed":
                return step.error or step.message or ""
        return ""


def make_run_result_dir(case_file: str, base_dir: str = "", run_ts: str = "") -> str:
    """
    生成本次执行的结果目录路径（不创建目录）。

    路径格式：<base_dir>/<YYYYMMDD-HHmmss>/<case_filename>.result
    """
    if not base_dir:
        try:
            from uita.config import REPORT_DIR
            base_dir = REPORT_DIR
        except ImportError:
            base_dir = os.path.join(os.getcwd(), "report")

    if not run_ts:
        run_ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    case_filename = os.path.basename(case_file)
    result_dirname = f"{case_filename}.result"

    return os.path.join(base_dir, run_ts, result_dirname)


class ResultCollector:
    def __init__(self, case_name: str, case_file: str = "", result_dir: str = ""):
        self._case_name = case_name
        self._case_file = case_file
        self._result_dir = result_dir
        self._result = CaseResult(
            case_name=case_name,
            case_file=case_file,
            result_dir=result_dir,
        )
        self._start_time: float = time.monotonic()
        self._step_index: int = 0

    @property
    def result_dir(self) -> str:
        return self._result_dir

    @property
    def screenshot_dir(self) -> str:
        return os.path.join(self._result_dir, "screenshots")

    def record_step(
        self,
        name: str,
        status: str,
        duration_ms: float = 0.0,
        message: str = "",
        screenshot: Optional[str] = None,
        actions: Optional[list] = None,
    ) -> None:
        """记录一步结果。

        screenshot 参数同时写入 screenshots 列表（标准报告规范 格式）和
        screenshot 字段（UITA 向后兼容）。
        """
        now = _now_ms()
        screenshots = []
        if screenshot:
            screenshots.append(screenshot)

        sr = StepResult(
            name=name,
            status=status,
            duration_ms=duration_ms,
            message=message,
            screenshot=screenshot,
            actions=actions or [],
            index=self._step_index,
            screenshots=screenshots,
            assertion_text=message if status == "passed" else "",
            error=message if status != "passed" else None,
            begin_time=now,
            end_time=now,
            description=name,
            raw_step=[],
        )
        self._result.add_step(sr)
        self._step_index += 1

    def add_artifact(self, name: str, relative_path: str) -> None:
        """登记受控运行产物的相对路径，禁止写入源 URL 或绝对路径。

        报告平台 服务端要求 artifacts 每个字段值为列表（{collector_name: [url, ...]}），
        故此处存单元素 list 而非字符串。
        """
        if not name or not relative_path or os.path.isabs(relative_path) or ".." in relative_path.replace("\\", "/").split("/"):
            raise ValueError("Artifact must have a safe relative path")
        self._result.artifacts[name] = [relative_path.replace("\\", "/")]

    def finalize(self) -> CaseResult:
        self._result.duration_ms = (time.monotonic() - self._start_time) * 1000
        return self._result

    def write_json(self, path: str = "") -> str:
        """将结果写入 result.json。

        输出格式：标准报告规范 字段在前（外部报告解析器 解析），UITA 额外字段在后。
        """
        result = self.finalize()

        if not path:
            if self._result_dir:
                path = os.path.join(self._result_dir, "result.json")
            else:
                path = "result.json"

        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

        # ── 标准报告规范 顶层字段（严格对齐，外部报告解析器 解析）──
        data: dict[str, Any] = {
            "case_file": result.case_file,
            "case_id": result.case_id,
            "case_name": result.case_name,
            "case_model": result.case_model,
            "case_description": result.case_description,
            "total": result.total,
            "passed": result.passed,
            "failed": result.failed,
            "skipped": result.skipped,
            "steps": [self._step_to_dict(s) for s in result.steps],
        }

        # artifacts 可选（有 collector 产出物时才出现）
        if result.artifacts:
            data["artifacts"] = result.artifacts

        # ── UITA 额外字段（追加在末尾，外部报告解析器 不解析）──
        data["duration_ms"] = int(result.duration_ms)
        data["result_dir"] = result.result_dir

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    def _step_to_dict(self, step: StepResult) -> dict:
        """将 StepResult 序列化为 dict。

        标准报告规范 字段在前，UITA 额外字段在后。
        screenshot 为 None 时省略（向后兼容）；screenshots 始终输出（标准报告规范 格式）。
        """
        d: dict[str, Any] = {
            # ── 标准报告规范 字段 ──
            "index": step.index,
            "name": step.name,
            "status": step.status,
            "screenshots": step.screenshots,
            "assertion_text": step.assertion_text,
            "error": step.error,
            "begin_time": step.begin_time,
            "end_time": step.end_time,
            "duration_ms": int(step.duration_ms),
            "description": step.description,
            "raw_step": step.raw_step,
        }

        # ── UITA 额外字段（有值时才输出）──
        if step.message:
            d["message"] = step.message
        if step.screenshot is not None:
            d["screenshot"] = step.screenshot
        if step.actions:
            d["actions"] = [
                asdict(a) if hasattr(a, "__dataclass_fields__") else a
                for a in step.actions
            ]
            # 清理 actions 内 None 字段（保持输出简洁）
            for act in d["actions"]:
                if isinstance(act, dict) and act.get("screenshot") is None:
                    act.pop("screenshot", None)
                if isinstance(act, dict) and act.get("screenshot_before") is None:
                    act.pop("screenshot_before", None)
                if isinstance(act, dict) and act.get("existence_strategy") is None:
                    act.pop("existence_strategy", None)
                if isinstance(act, dict) and act.get("page_changed") is None:
                    act.pop("page_changed", None)
                if isinstance(act, dict) and not act.get("extra"):
                    act.pop("extra", None)

        return d
