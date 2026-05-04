from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import PROJECT_ROOT


PERSISTENT_BEST_PARAMS_JSON = PROJECT_ROOT / "run_history" / "_persistent_best_params_v3.json"
PERSISTENT_TOP10_JSON = PROJECT_ROOT / "run_history" / "_persistent_top10_v3.json"


def _looks_int(value: str) -> bool:
    text = str(value).strip()
    if not text:
        return False
    if text.startswith(("+", "-")):
        text = text[1:]
    return text.isdigit()


def _parse_number(value: str) -> int | float:
    text = str(value).strip()
    if _looks_int(text):
        return int(text)
    parsed = float(text)
    return int(parsed) if parsed.is_integer() else parsed


def parse_param_preset_file(path: str | Path) -> dict[str, dict[str, Any]]:
    preset_path = Path(path)
    if not preset_path.exists():
        return {}

    space: dict[str, dict[str, Any]] = {}
    for raw_line in preset_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, values = line.split("=", 1)
        parts = [part.strip() for part in values.split(",")]
        if len(parts) != 3:
            continue
        start_raw, stop_raw, step_raw = parts
        is_int = _looks_int(start_raw) and _looks_int(stop_raw) and _looks_int(step_raw)
        start = _parse_number(start_raw)
        stop = _parse_number(stop_raw)
        step = _parse_number(step_raw)
        space[name.strip()] = {
            "name": name.strip(),
            "type": "int" if is_int else "float",
            "start": start,
            "stop": stop,
            "step": step,
        }
    return space


def load_persistent_best_params() -> dict[str, Any]:
    if not PERSISTENT_BEST_PARAMS_JSON.exists():
        return {}
    try:
        payload = json.loads(PERSISTENT_BEST_PARAMS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    params = payload.get("params") or {}
    return params if isinstance(params, dict) else {}


def load_persistent_top10_rows(limit: int = 10) -> list[dict[str, Any]]:
    if not PERSISTENT_TOP10_JSON.exists():
        return []
    try:
        payload = json.loads(PERSISTENT_TOP10_JSON.read_text(encoding="utf-8"))
    except Exception:
        return []

    rows = payload.get("rows") or []
    if not isinstance(rows, list):
        return []

    normalized: list[dict[str, Any]] = []
    for row in rows[: max(0, int(limit))]:
        if not isinstance(row, dict):
            continue
        normalized.append(dict(row))
    return normalized


def load_optimization_reference_bundle(limit: int = 10) -> dict[str, Any]:
    top10_rows = load_persistent_top10_rows(limit=limit)
    return {
        "best_params": load_persistent_best_params(),
        "top10_rows": top10_rows,
        "top10_count": len(top10_rows),
    }


def merge_space_with_reference_values(
    space: dict[str, dict[str, Any]],
    reference_values: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
    merged = {name: dict(spec) for name, spec in space.items()}
    for name, raw_value in (reference_values or {}).items():
        if name not in merged:
            continue
        spec = merged[name]
        try:
            value = _parse_number(str(raw_value))
        except Exception:
            continue
        spec["start"] = min(spec["start"], value)
        spec["stop"] = max(spec["stop"], value)
    return merged


def load_research_param_space(preset_path: str | None) -> dict[str, dict[str, Any]]:
    if not preset_path:
        return {}
    base_space = parse_param_preset_file(preset_path)
    if not base_space:
        return {}
    merged_space = merge_space_with_reference_values(base_space, load_persistent_best_params())
    for row in load_persistent_top10_rows(limit=10):
        merged_space = merge_space_with_reference_values(merged_space, row)
    return merged_space


def _round_to_step(value: float, start: float, step: float) -> float:
    if step == 0:
        return value
    units = round((value - start) / step)
    return start + units * step


def normalize_params_to_space(
    params: dict[str, Any],
    param_space: dict[str, dict[str, Any]],
) -> dict[str, int | float | str]:
    normalized: dict[str, int | float | str] = {}
    for name, raw_value in params.items():
        spec = param_space.get(name)
        if spec is None:
            normalized[name] = raw_value
            continue

        value = _parse_number(str(raw_value))
        start = float(spec["start"])
        stop = float(spec["stop"])
        step = float(spec["step"])
        value_f = float(value)
        value_f = min(max(value_f, start), stop)
        value_f = _round_to_step(value_f, start, step)
        value_f = min(max(value_f, start), stop)
        if spec["type"] == "int":
            normalized[name] = int(round(value_f))
        else:
            normalized[name] = round(float(value_f), 10)
    return normalized
