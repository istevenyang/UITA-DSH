"""Single-case, no-fallback runner for failed-case-repairer."""
from __future__ import annotations

from pathlib import Path

from uita.core.runner.batch_runner import BatchRunner


class RepairRunner(BatchRunner):
    """Run every step once while keeping automation fallbacks disabled."""

    def __init__(self, *args, cases_dirs: list[str] | str, **kwargs) -> None:
        paths = [cases_dirs] if isinstance(cases_dirs, str) else list(cases_dirs)
        if len(paths) != 1 or not Path(paths[0]).is_file() or Path(paths[0]).suffix.lower() != ".tcs":
            raise ValueError("RepairRunner requires exactly one existing .tcs file")
        super().__init__(*args, cases_dirs=paths, repair_mode=True, **kwargs)
