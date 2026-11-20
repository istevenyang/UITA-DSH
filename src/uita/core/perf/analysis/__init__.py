"""perf AI 分析报告包（内置 DSH 无头直写 HTML + 事实锚 + 事后校验）。

对外暴露：
  - :func:`collect_facts` / :func:`write_facts`：确定性事实层（数值锚 + 覆盖基准）；
  - :func:`run_analysis` / :func:`analysis_enabled` / :func:`report_path_for`：编排与开关；
  - :func:`verify_report`：事后校验（章节 / 覆盖 / 读数）。
"""
from uita.core.perf.analysis.facts import (
    FACTS_SCHEMA_VERSION,
    collect_facts,
    find_previous_run,
    load_tiers_from_run_dir,
    render_facts_md,
    scan_run_dir,
    write_facts,
)
from uita.core.perf.analysis.pipeline import (
    ANALYSIS_SUBDIR,
    REPORT_NAME,
    AnalysisOutcome,
    analysis_dir,
    analysis_enabled,
    report_path_for,
    run_analysis,
)
from uita.core.perf.analysis.verify import VerifyResult, verify_report

__all__ = [
    "ANALYSIS_SUBDIR",
    "FACTS_SCHEMA_VERSION",
    "REPORT_NAME",
    "AnalysisOutcome",
    "VerifyResult",
    "analysis_dir",
    "analysis_enabled",
    "collect_facts",
    "find_previous_run",
    "load_tiers_from_run_dir",
    "scan_run_dir",
    "render_facts_md",
    "report_path_for",
    "run_analysis",
    "verify_report",
    "write_facts",
]
