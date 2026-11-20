"""TCS sidecar execution profile loader."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def profile_path_for(case_file: str | Path) -> Path:
    path = Path(case_file)
    return path.with_name(path.name + ".profile.json")


def ai_repaired_profile_path_for(case_file: str | Path) -> Path:
    base = profile_path_for(case_file)
    return base.with_name(base.name.replace(".profile.json", ".profile.ai_repaired.json"))


def load_execution_profile(case_file: str | Path) -> dict[str, Any]:
    if not str(case_file):
        return {}
    base = _load_json(profile_path_for(case_file))
    repaired = _load_json(ai_repaired_profile_path_for(case_file))
    return _merge_profiles(base, repaired)



def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _merge_profiles(base: dict[str, Any], repaired: dict[str, Any]) -> dict[str, Any]:
    if not base:
        return repaired if repaired else {}
    if not repaired:
        return base
    merged = dict(base)
    merged_meta = dict(base.get("_meta") or {})
    merged_meta["ai_repaired_overlay"] = True
    merged_meta["ai_repaired_source"] = (repaired.get("_meta") or {}).get("updated_at", "")
    merged["_meta"] = merged_meta
    base_steps = list(base.get("steps") or [])
    repaired_steps = list(repaired.get("steps") or [])
    max_len = max(len(base_steps), len(repaired_steps))
    steps = []
    for idx in range(max_len):
        b = base_steps[idx] if idx < len(base_steps) and isinstance(base_steps[idx], dict) else {}
        r = repaired_steps[idx] if idx < len(repaired_steps) and isinstance(repaired_steps[idx], dict) else {}
        combined = dict(b)
        if r:
            combined.update({k: v for k, v in r.items() if k not in ("action", "assertion")})
            if "action" in r:
                combined["action"] = {**(b.get("action") or {}), **(r.get("action") or {})}
            if "assertion" in r:
                combined["assertion"] = {**(b.get("assertion") or {}), **(r.get("assertion") or {})}
        steps.append(combined)
    merged["steps"] = steps
    return merged


def profile_step(profile: dict[str, Any] | None, step_index: int) -> dict[str, Any]:

    if not profile:
        return {}
    steps = profile.get("steps") or []
    if not isinstance(steps, list) or step_index < 0 or step_index >= len(steps):
        return {}
    step = steps[step_index]
    return step if isinstance(step, dict) else {}
