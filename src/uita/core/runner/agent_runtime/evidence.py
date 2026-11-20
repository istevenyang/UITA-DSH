"""Atomic review artifacts using the existing collector contract."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .session import Failure


class EvidenceWriter:
    def __init__(self, collector: Any):
        if not collector.result_dir:
            raise ValueError("review evidence requires a case result directory")
        self._collector = collector
        self._supplied_root = Path(collector.result_dir).absolute()
        self._reject_links()
        self._root = self._supplied_root.resolve()
        self._records: list[dict[str, Any]] = []
        self._scope: tuple[str, str] | None = None

    def _reject_links(self) -> None:
        if any(path.is_symlink() for path in (self._supplied_root, *self._supplied_root.parents)):
            raise ValueError("review result directory must not traverse symbolic links")

    def __call__(self, failure: Failure, events: list[dict[str, Any]]) -> None:
        scope = (failure.run_id, failure.case_id)
        if self._scope is not None and self._scope != scope:
            raise ValueError("review evidence belongs to another run or case")
        if Path(self._collector.result_dir).absolute() != self._supplied_root:
            raise ValueError("collector result directory changed")
        self._reject_links()
        if self._supplied_root.resolve() != self._root:
            raise ValueError("review result directory changed")
        self._root.mkdir(parents=True, exist_ok=True)
        destination = self._root / "failure-review.json"
        if destination.is_symlink():
            raise ValueError("review artifact must not be a symbolic link")
        record = {"failure": asdict(failure), "events": events}
        records = [*self._records, record]
        content = json.dumps({"version": "1.0", "reviews": records}, ensure_ascii=False,
                             allow_nan=False, indent=2)
        descriptor, name = tempfile.mkstemp(prefix=".failure-review-", dir=self._root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
            os.replace(name, destination)
            self._collector.add_artifact("failure_review", "failure-review.json")
            self._records = records
            self._scope = scope
        finally:
            if os.path.exists(name):
                os.unlink(name)
