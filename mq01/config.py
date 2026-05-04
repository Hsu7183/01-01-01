from __future__ import annotations

import os
from dataclasses import dataclass

from .bootstrap import resolve_source_root


DEFAULT_CPU_LIMIT_PCT = 50
DEFAULT_MEMORY_LIMIT_PCT = 50
DEFAULT_ENABLED_PARAMS: set[str] = set()


@dataclass(slots=True)
class OptimizerPaths:
    source_root: str
    xs_path: str
    minute_path: str
    daily_path: str
    param_preset_path: str


def default_paths() -> OptimizerPaths:
    source_root = resolve_source_root()
    return OptimizerPaths(
        source_root=str(source_root),
        xs_path=str(source_root / "strategy" / "1150415.xs"),
        minute_path=str(source_root / "data" / "m1"),
        daily_path=str(source_root / "data" / "d1"),
        param_preset_path=str(source_root / "param_presets" / "1150415.txt"),
    )


def default_max_workers() -> int:
    cpu_count = os.cpu_count() or 4
    if cpu_count <= 2:
        return 1
    return max(1, min(cpu_count - 1, 8))


def default_runtime_settings() -> dict[str, int | float]:
    return {
        "cpu_limit_pct": DEFAULT_CPU_LIMIT_PCT,
        "memory_limit_pct": DEFAULT_MEMORY_LIMIT_PCT,
        "capital": 1_000_000,
        "slip_per_side": 2.0,
        "development_years": 5,
        "max_workers": default_max_workers(),
        "top_n": 3,
        "seed_top_k": 3,
        "seed_keep_count": 3,
        "wfo_train_years": 4,
        "wfo_test_years": 1,
        "wfo_step_years": 1,
        "wfo_gap_days": 0,
        "total_budget": 5_000,
        "initial_samples": 300,
        "neighbors_per_seed": 12,
        "rng_seed": 42,
    }


def default_hard_filters() -> dict[str, int | float | str]:
    return {
        "min_trades": 0,
        "min_total_return": 0.0,
        "max_mdd_pct": 15.0,
        "mdd_mode": "fixed",
        "mdd_start_pct": 3.0,
        "mdd_end_pct": 15.0,
        "mdd_step_pct": 1.0,
    }

