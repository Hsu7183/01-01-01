from __future__ import annotations

import calendar
import csv
import ctypes
import hashlib
import importlib.util
import json
import math
import os
import re
import time
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from pprint import pformat
from types import SimpleNamespace
from typing import Any, Iterable, Mapping

import pandas as pd

from .bootstrap import resolve_source_root
from .xs_variants import render_indicator_xs, render_trade_xs
from src.backtest.report import build_report
from src.optimize.gui_backend import (
    build_search_space_from_ui,
    RESULT_COLUMNS,
    _apply_plateau_scores,
    _evaluate_task_sequence,
    _build_recent_trials_df,
    _hard_filter_fail_reasons,
    _init_worker_data,
    _passes_hard_filters,
    _run_single_combo,
    _score_row,
    _sort_results_df,
    load_market_data,
    shutdown_cached_worker_executor,
)
from src.research.param_space import PERSISTENT_BEST_PARAMS_JSON, PERSISTENT_TOP10_JSON
from src.core.models import Trade
from src.strategy.strategy_0313plus import run_0313plus_backtest


_LAST_CPU_SAMPLE: tuple[int, int, int] | None = None
_LAST_CPU_PERCENT: float | None = None
_LAST_CPU_SAMPLED_AT: float = 0.0
_STREAMLIT_UPDATE_MIN_INTERVAL_SECONDS = 0.75
_STREAMLIT_UPDATE_MIN_STRIDE = 12
_STREAMLIT_UPDATE_MAX_STRIDE = 200
_GRID_RUN_HARD_LIMIT = 250_000
_BLAS_THREAD_ENV_VARS = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)
_SOURCE_ROOT = resolve_source_root()
_RUN_HISTORY_DIR = _SOURCE_ROOT / "run_history"
_MQ01_EXPORTS_DIR = _RUN_HISTORY_DIR / "mq01_exports"
_FORWARD_TEST_LOG_PATH = _RUN_HISTORY_DIR / "forward_test_log.csv"
_PERSISTENT_TOP10_CSV = _RUN_HISTORY_DIR / "_persistent_top10_v3.csv"
_PERSISTENT_BEST_PARAMS_TXT = _RUN_HISTORY_DIR / "_persistent_best_top1_v3.txt"
_LATEST_RUN_MEMORY_PATH = _SOURCE_ROOT / "src" / "latest_run_memory.py"
_LATEST_STRATEGY_DIR = _SOURCE_ROOT / "strategy"
_LATEST_PARAM_PRESET_DIR = _SOURCE_ROOT / "param_presets"
RESEARCH_PROFILE_TAG_0101 = "01-01_dev_years_validation_v3"
_LEADERBOARD_META_FIELDS = {
    "saved_at",
    "source_saved_at",
    "source_run_dir",
    "strategy_signature",
    "optimization_mode",
    "research_profile_tag",
    "development_years",
    "total_return",
    "mdd_pct",
    "n_trades",
    "year_avg_return",
    "year_return_std",
    "loss_years",
    "composite_score",
    "robust_score",
    "robust_score_pre_plateau",
    "plateau_score",
    "window_count",
    "window_avg_return",
    "window_median_return",
    "window_return_std",
    "window_loss_count",
    "worst_window_return",
    "worst_window_mdd_pct",
    "window_consistency_score",
    "slip2_total_return",
    "slip3_total_return",
    "slip4_total_return",
    "slip_return_avg",
    "slip_return_min",
    "slip_decay_2_to_4",
    "slip_stress_score",
    "xs_path",
    "params_txt_path",
    "params_json",
}


def research_profile_tag_for_development_years(development_years: int | None) -> str:
    if development_years in (None, ""):
        return RESEARCH_PROFILE_TAG_0101
    years = max(1, min(5, int(development_years)))
    return f"{RESEARCH_PROFILE_TAG_0101}_dev{years}y"


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", ctypes.c_ulong),
        ("dwHighDateTime", ctypes.c_ulong),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _read_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_latest_memory() -> dict[str, Any]:
    if not _LATEST_RUN_MEMORY_PATH.exists():
        return {}
    try:
        spec = importlib.util.spec_from_file_location("mq01_latest_run_memory", _LATEST_RUN_MEMORY_PATH)
        if spec is None or spec.loader is None:
            return {}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        payload = getattr(module, "LATEST_OPTIMIZATION_MEMORY", {})
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _system_cpu_percent() -> float | None:
    global _LAST_CPU_SAMPLE, _LAST_CPU_PERCENT, _LAST_CPU_SAMPLED_AT

    if os.name != "nt":
        return None

    now = time.monotonic()
    if _LAST_CPU_PERCENT is not None and (now - _LAST_CPU_SAMPLED_AT) < 0.35:
        return _LAST_CPU_PERCENT

    idle_time = _FILETIME()
    kernel_time = _FILETIME()
    user_time = _FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        return _LAST_CPU_PERCENT

    current_sample = (
        (int(idle_time.dwHighDateTime) << 32) | int(idle_time.dwLowDateTime),
        (int(kernel_time.dwHighDateTime) << 32) | int(kernel_time.dwLowDateTime),
        (int(user_time.dwHighDateTime) << 32) | int(user_time.dwLowDateTime),
    )

    cpu_percent: float | None = None
    if _LAST_CPU_SAMPLE is not None:
        idle_delta = max(current_sample[0] - _LAST_CPU_SAMPLE[0], 0)
        kernel_delta = max(current_sample[1] - _LAST_CPU_SAMPLE[1], 0)
        user_delta = max(current_sample[2] - _LAST_CPU_SAMPLE[2], 0)
        total_delta = kernel_delta + user_delta
        busy_delta = max(total_delta - idle_delta, 0)
        if total_delta > 0:
            cpu_percent = (busy_delta / total_delta) * 100.0

    _LAST_CPU_SAMPLE = current_sample
    _LAST_CPU_SAMPLED_AT = now
    if cpu_percent is not None:
        _LAST_CPU_PERCENT = cpu_percent
    return _LAST_CPU_PERCENT


def _system_memory_snapshot() -> dict[str, Any]:
    if os.name != "nt":
        return {}

    memory_status = _MEMORYSTATUSEX()
    memory_status.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory_status)):
        return {}

    total_gb = float(memory_status.ullTotalPhys) / (1024**3)
    available_gb = float(memory_status.ullAvailPhys) / (1024**3)
    used_gb = max(total_gb - available_gb, 0.0)
    return {
        "memory_pct": float(memory_status.dwMemoryLoad),
        "memory_total_gb": total_gb,
        "memory_used_gb": used_gb,
        "memory_available_gb": available_gb,
    }


def collect_system_snapshot(
    *,
    max_workers: int,
    requested_workers: int | None = None,
    cpu_limit_pct: int | None = None,
    memory_limit_pct: int | None = None,
) -> dict[str, Any]:
    cpu_count = os.cpu_count() or 0
    configured_workers = max(1, int(max_workers or 1))
    requested_worker_count = configured_workers if requested_workers is None else max(1, int(requested_workers or 1))
    memory_snapshot = _system_memory_snapshot()
    effective_cpu_count = _cpu_limited_core_count(cpu_count=cpu_count, cpu_limit_pct=cpu_limit_pct)
    worker_load_pct: float | None = None
    if effective_cpu_count > 0:
        worker_load_pct = (configured_workers / effective_cpu_count) * 100.0
    return {
        "cpu_pct": _system_cpu_percent(),
        "logical_cpu_count": cpu_count,
        "effective_cpu_count": effective_cpu_count,
        "configured_workers": configured_workers,
        "requested_workers": requested_worker_count,
        "cpu_limit_pct": None if cpu_limit_pct is None else int(cpu_limit_pct),
        "memory_limit_pct": None if memory_limit_pct is None else int(memory_limit_pct),
        "worker_load_pct": worker_load_pct,
        **memory_snapshot,
    }


def _cpu_limited_core_count(*, cpu_count: int | None = None, cpu_limit_pct: int | None = None) -> int:
    available_cpu_count = max(1, int(cpu_count or os.cpu_count() or 1))
    capped_limit = max(1, min(100, int(cpu_limit_pct or 100)))
    return max(1, int(math.floor(available_cpu_count * capped_limit / 100.0)))


def resolve_effective_workers(*, requested_workers: int, cpu_limit_pct: int) -> int:
    requested = max(1, int(requested_workers or 1))
    cpu_limited_workers = _cpu_limited_core_count(cpu_limit_pct=cpu_limit_pct)
    return max(1, min(requested, cpu_limited_workers))


def apply_cpu_guard(*, cpu_limit_pct: int) -> None:
    capped_limit = max(1, min(100, int(cpu_limit_pct or 100)))
    for env_name in _BLAS_THREAD_ENV_VARS:
        os.environ[env_name] = "1"
    os.environ["MQ01_CPU_LIMIT_PCT"] = str(capped_limit)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        numeric = float(value)
    except Exception:
        return float(default)
    if pd.isna(numeric):
        return float(default)
    return float(numeric)


def _mdd_sweep_thresholds(hard_filters: Mapping[str, Any] | None) -> list[float]:
    filters = dict(hard_filters or {})
    if str(filters.get("mdd_mode") or "fixed") != "sweep":
        return []
    start = max(0.0, _safe_float(filters.get("mdd_start_pct"), 0.0))
    end = max(0.0, _safe_float(filters.get("mdd_end_pct"), 0.0))
    step = max(0.01, _safe_float(filters.get("mdd_step_pct"), 1.0))
    if end < start:
        start, end = end, start
    thresholds: list[float] = []
    value = start
    guard = 0
    while value <= end + 1e-9 and guard < 1000:
        thresholds.append(round(value, 4))
        value += step
        guard += 1
    return thresholds


def _base_hard_filters(hard_filters: Mapping[str, Any] | None) -> dict[str, Any]:
    filters = dict(hard_filters or {})
    thresholds = _mdd_sweep_thresholds(filters)
    if thresholds:
        filters["max_mdd_pct"] = max(thresholds)
    return filters


def _mdd_pass_thresholds(row: Mapping[str, Any], hard_filters: Mapping[str, Any] | None) -> list[float]:
    mdd_pct = _safe_float(row.get("mdd_pct"), 1e18)
    return [threshold for threshold in _mdd_sweep_thresholds(hard_filters) if mdd_pct <= threshold + 1e-9]


def _annotate_mdd_sweep_row(row: Mapping[str, Any], hard_filters: Mapping[str, Any] | None) -> dict[str, Any]:
    item = dict(row)
    thresholds = _mdd_sweep_thresholds(hard_filters)
    if not thresholds:
        return item
    pass_thresholds = _mdd_pass_thresholds(item, hard_filters)
    item["mdd_pass_thresholds"] = ", ".join(f"{threshold:.2f}%" for threshold in pass_thresholds)
    item["mdd_strictest_pass_pct"] = min(pass_thresholds) if pass_thresholds else None
    return item


def _mdd_sweep_retention_limit(top_n: int, hard_filters: Mapping[str, Any] | None) -> int:
    base_limit = max(1, int(top_n or 1))
    threshold_count = len(_mdd_sweep_thresholds(hard_filters))
    if threshold_count <= 1:
        return base_limit
    return min(max(base_limit * threshold_count, base_limit), 500)


def _trim_accepted_rows_for_mdd_sweep(
    rows: Iterable[Mapping[str, Any]],
    *,
    top_n: int,
    hard_filters: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    annotated_rows = [_annotate_mdd_sweep_row(row, hard_filters) for row in rows]
    if not annotated_rows:
        return []
    thresholds = _mdd_sweep_thresholds(hard_filters)
    base_limit = max(1, int(top_n or 1))
    if not thresholds:
        return sorted(annotated_rows, key=_score_row)[:base_limit]

    selected_rows: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for threshold in thresholds:
        threshold_rows = [
            row for row in annotated_rows if _safe_float(row.get("mdd_pct"), 1e18) <= threshold + 1e-9
        ]
        threshold_rows.sort(key=_score_row)
        for row in threshold_rows[:base_limit]:
            marker = id(row)
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
            selected_rows.append(row)

    if not selected_rows:
        selected_rows = sorted(annotated_rows, key=_score_row)[:base_limit]
    selected_rows.sort(key=_score_row)
    return selected_rows[: _mdd_sweep_retention_limit(top_n, hard_filters)]


def _postprocess_mdd_sweep_update(
    update: Mapping[str, Any],
    *,
    top_n: int,
    hard_filters: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not _mdd_sweep_thresholds(hard_filters):
        return dict(update)

    processed = dict(update)
    accepted_rows = _trim_accepted_rows_for_mdd_sweep(
        processed.get("accepted_rows") or [],
        top_n=top_n,
        hard_filters=hard_filters,
    )
    processed["accepted_rows"] = accepted_rows
    processed["top_df"] = _sort_results_df(pd.DataFrame(accepted_rows)) if accepted_rows else pd.DataFrame(columns=RESULT_COLUMNS)

    recent_trials_df = processed.get("recent_trials_df")
    if isinstance(recent_trials_df, pd.DataFrame) and not recent_trials_df.empty:
        recent_rows = [
            _annotate_mdd_sweep_row(row, hard_filters) for row in recent_trials_df.to_dict("records")
        ]
        processed["recent_trials_df"] = pd.DataFrame(recent_rows)
    row = processed.get("row")
    if isinstance(row, Mapping):
        processed["row"] = _annotate_mdd_sweep_row(row, hard_filters)
    return processed


def build_mdd_sweep_summary(rows: Iterable[Mapping[str, Any]], hard_filters: Mapping[str, Any] | None) -> pd.DataFrame:
    thresholds = _mdd_sweep_thresholds(hard_filters)
    if not thresholds:
        return pd.DataFrame()
    records: list[dict[str, Any]] = []
    for threshold in thresholds:
        passed_rows = [dict(row) for row in rows if _safe_float(row.get("mdd_pct"), 1e18) <= threshold + 1e-9]
        if passed_rows:
            best_row = sorted(passed_rows, key=_score_row)[0]
            records.append(
                {
                    "MDD門檻": f"{threshold:.2f}%",
                    "通過組數": len(passed_rows),
                    "最佳總報酬": _safe_float(best_row.get("total_return"), 0.0),
                    "最佳MDD": _safe_float(best_row.get("mdd_pct"), 0.0),
                    "交易數": int(round(_safe_float(best_row.get("n_trades"), 0.0))),
                    "穩健分數": _safe_float(best_row.get("robust_score", best_row.get("composite_score")), 0.0),
                    "_threshold": threshold,
                }
            )
        else:
            records.append(
                {
                    "MDD門檻": f"{threshold:.2f}%",
                    "通過組數": 0,
                    "最佳總報酬": None,
                    "最佳MDD": None,
                    "交易數": None,
                    "穩健分數": None,
                    "_threshold": threshold,
                }
            )
    return pd.DataFrame(records)


def _year_from_date_int(date_value: Any) -> int:
    return int(int(date_value) // 10000)


def _date_int(year: int, month: int, day: int) -> int:
    return int(year) * 10000 + int(month) * 100 + int(day)


def _date_label(date_value: int) -> str:
    text = f"{int(date_value):08d}"
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"


def _date_to_obj(date_value: int):
    text = f"{int(date_value):08d}"
    return datetime(int(text[0:4]), int(text[4:6]), int(text[6:8])).date()


def _obj_to_date_int(date_value) -> int:
    return _date_int(date_value.year, date_value.month, date_value.day)


def _shift_back_one_year(date_value):
    target_year = date_value.year - 1
    for day in range(date_value.day, 0, -1):
        try:
            return date_value.replace(year=target_year, day=day)
        except ValueError:
            continue
    return date_value.replace(year=target_year, day=28)


def resolve_0101_research_periods(
    *,
    minute_path: str,
    daily_path: str,
    development_years: int | None = None,
) -> dict[str, Any]:
    _minute_bars, daily_bars = load_market_data(minute_path, daily_path)
    return _resolve_0101_research_periods_from_daily_bars(daily_bars, development_years=development_years)


def _resolve_0101_research_periods_from_daily_bars(
    daily_bars: list[Any],
    *,
    development_years: int | None = None,
) -> dict[str, Any]:
    if not daily_bars:
        return {}

    daily_dates = sorted(int(bar.date) for bar in daily_bars)
    first_date = daily_dates[0]
    last_date = daily_dates[-1]
    first_date_obj = _date_to_obj(first_date)
    last_date_obj = _date_to_obj(last_date)

    selected_development_years = None
    if development_years not in (None, ""):
        selected_development_years = max(1, min(5, int(development_years)))
        calendar_end_obj = date(first_date_obj.year + selected_development_years - 1, 12, 31)
        development_end_obj = min(last_date_obj, calendar_end_obj)
    else:
        selected_development_years = 5
        calendar_end_obj = date(first_date_obj.year + selected_development_years - 1, 12, 31)
        development_end_obj = min(last_date_obj, calendar_end_obj)
    if development_end_obj < first_date_obj:
        development_end_obj = first_date_obj

    holdout_start_obj = development_end_obj + timedelta(days=1)
    if holdout_start_obj > last_date_obj:
        holdout_start_obj = last_date_obj

    development_start_date = _obj_to_date_int(first_date_obj)
    development_end_date = _obj_to_date_int(development_end_obj)
    holdout_start_date = _obj_to_date_int(holdout_start_obj)
    holdout_end_date = _obj_to_date_int(last_date_obj)

    development_short_label = f"{_date_label(development_start_date)} ~ {_date_label(development_end_date)}"
    holdout_short_label = f"{_date_label(holdout_start_date)} ~ {_date_label(holdout_end_date)}"
    profile_tag = research_profile_tag_for_development_years(selected_development_years)

    return {
        "research_profile_tag": profile_tag,
        "development_years": selected_development_years,
        "validation_years": max(0, 6 - int(selected_development_years or 5)),
        "development_start_date": development_start_date,
        "development_end_date": development_end_date,
        "holdout_start_date": holdout_start_date,
        "holdout_end_date": holdout_end_date,
        "development_label": development_short_label,
        "holdout_label": holdout_short_label,
        "development_year_label": development_short_label,
        "holdout_year_label": holdout_short_label,
        "latest_full_year": _year_from_date_int(last_date),
        "latest_data_date": _date_label(last_date),
    }


def _filter_bars_for_period(
    minute_bars: list[Any],
    daily_bars: list[Any],
    *,
    start_date: int,
    end_date: int,
) -> tuple[list[Any], list[Any]]:
    filtered_minute = [bar for bar in minute_bars if int(start_date) <= int(bar.date) <= int(end_date)]
    filtered_daily = [bar for bar in daily_bars if int(start_date) <= int(bar.date) <= int(end_date)]
    return filtered_minute, filtered_daily


def _holdout_trade_metrics(trades: list[Any]) -> dict[str, Any]:
    trade_count = len(trades or [])
    if trade_count <= 0:
        return {
            "trade_count": 0,
            "win_rate_pct": 0.0,
            "avg_net_profit": 0.0,
            "net_profit": 0.0,
            "profit_factor": 0.0,
        }

    net_profit = sum(float(getattr(trade, "net_pnl", 0.0) or 0.0) for trade in trades)
    gross_profit = sum(max(float(getattr(trade, "net_pnl", 0.0) or 0.0), 0.0) for trade in trades)
    gross_loss = abs(sum(min(float(getattr(trade, "net_pnl", 0.0) or 0.0), 0.0) for trade in trades))
    win_count = sum(1 for trade in trades if float(getattr(trade, "net_pnl", 0.0) or 0.0) > 0.0)
    if gross_loss > 1e-12:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0.0:
        profit_factor = 999.0
    else:
        profit_factor = 0.0
    return {
        "trade_count": trade_count,
        "win_rate_pct": (win_count / trade_count) * 100.0,
        "avg_net_profit": net_profit / trade_count,
        "net_profit": net_profit,
        "profit_factor": profit_factor,
    }


def _trade_sharpe(trades: list[Any]) -> float:
    pnls = [float(getattr(trade, "net_pnl", 0.0) or 0.0) for trade in trades or []]
    if len(pnls) <= 1:
        return 0.0
    avg = sum(pnls) / len(pnls)
    variance = sum((value - avg) ** 2 for value in pnls) / (len(pnls) - 1)
    stdev = math.sqrt(max(variance, 0.0))
    if stdev <= 1e-12:
        return 0.0
    return float((avg / stdev) * math.sqrt(len(pnls)))


def run_0101_holdout_validation(
    *,
    params: dict[str, Any],
    minute_path: str,
    daily_path: str,
    script_name: str,
    capital: int,
    research_periods: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    minute_bars, daily_bars = load_market_data(minute_path, daily_path)
    periods = dict(research_periods or _resolve_0101_research_periods_from_daily_bars(daily_bars))
    if not periods:
        return {}

    _holdout_minute_bars, holdout_daily_bars = _filter_bars_for_period(
        minute_bars,
        daily_bars,
        start_date=int(periods["holdout_start_date"]),
        end_date=int(periods["holdout_end_date"]),
    )

    slippage_results: list[dict[str, Any]] = []
    primary_report: dict[str, Any] | None = None
    primary_trades: list[Any] = []
    for slip in (2.0, 3.0, 4.0, 5.0):
        full_result = run_0313plus_backtest(
            minute_bars,
            daily_bars,
            params,
            script_name,
            slip_per_side=float(slip),
        )
        holdout_trades = [
            trade
            for trade in list(getattr(full_result, "trades", []) or [])
            if int(periods["holdout_start_date"]) <= int(getattr(trade, "exit_date", 0) or 0) <= int(periods["holdout_end_date"])
        ]
        holdout_result = SimpleNamespace(trades=holdout_trades)
        report = build_report(holdout_result, holdout_daily_bars, capital)
        trade_metrics = _holdout_trade_metrics(holdout_trades)
        row = {
            "slip_per_side": float(slip),
            "net_profit": float(trade_metrics["net_profit"]),
            "trade_count": int(trade_metrics["trade_count"]),
            "win_rate_pct": float(trade_metrics["win_rate_pct"]),
            "avg_net_profit": float(trade_metrics["avg_net_profit"]),
            "profit_factor": float(trade_metrics["profit_factor"]),
            "sharpe": _trade_sharpe(holdout_trades),
            "total_return_pct": float(report.get("total_return", 0.0)) * 100.0,
            "mdd_pct": float(report.get("mdd_pct", 0.0)) * 100.0,
            "mdd_amount": float(report.get("mdd_amount", 0.0)),
            "positive_return": float(report.get("total_return", 0.0)) > 0.0,
        }
        slippage_results.append(row)
        if abs(float(slip) - 2.0) < 1e-9:
            primary_report = report
            primary_trades = holdout_trades

    if primary_report is None:
        return {}

    primary_metrics = _holdout_trade_metrics(primary_trades)
    slip2_row = next((row for row in slippage_results if abs(float(row["slip_per_side"]) - 2.0) < 1e-9), None) or {}
    slip3_row = next((row for row in slippage_results if abs(float(row["slip_per_side"]) - 3.0) < 1e-9), None) or {}
    slip4_row = next((row for row in slippage_results if abs(float(row["slip_per_side"]) - 4.0) < 1e-9), None) or {}
    slip5_row = next((row for row in slippage_results if abs(float(row["slip_per_side"]) - 5.0) < 1e-9), None) or {}
    holdout_pf = float(primary_metrics["profit_factor"])
    holdout_mdd_pct = float(primary_report.get("mdd_pct", 0.0)) * 100.0
    holdout_trade_count = int(primary_metrics["trade_count"])
    slip2_net = float(slip2_row.get("net_profit", 0.0))
    slip3_net = float(slip3_row.get("net_profit", 0.0))
    slip4_net = float(slip4_row.get("net_profit", 0.0))
    slip5_net = float(slip5_row.get("net_profit", 0.0))
    if slip2_net > 0.0 and slip3_net > 0.0 and slip4_net > 0.0 and holdout_pf >= 1.05 and holdout_mdd_pct <= 20.0 and holdout_trade_count >= 8:
        final_verdict = "PASS"
    elif slip2_net > 0.0 and slip3_net > 0.0 and holdout_pf >= 1.0 and holdout_mdd_pct <= 25.0 and holdout_trade_count >= 4:
        final_verdict = "WATCH"
    else:
        final_verdict = "REJECT"
    return {
        "params": {str(key): _json_safe_value(value) for key, value in params.items()},
        "research_profile_tag": str(periods.get("research_profile_tag") or RESEARCH_PROFILE_TAG_0101),
        "development_label": periods["development_label"],
        "holdout_label": periods["holdout_label"],
        "holdout_year_label": periods["holdout_year_label"],
        "net_profit": float(primary_metrics["net_profit"]),
        "trade_count": int(primary_metrics["trade_count"]),
        "win_rate_pct": float(primary_metrics["win_rate_pct"]),
        "avg_net_profit": float(primary_metrics["avg_net_profit"]),
        "profit_factor": float(primary_metrics["profit_factor"]),
        "sharpe": _trade_sharpe(primary_trades),
        "total_return_pct": float(primary_report.get("total_return", 0.0)) * 100.0,
        "mdd_pct": float(primary_report.get("mdd_pct", 0.0)) * 100.0,
        "mdd_amount": float(primary_report.get("mdd_amount", 0.0)),
        "monthly_returns": list(primary_report.get("monthly_returns", []) or []),
        "quarterly_returns": list(primary_report.get("quarterly_returns", []) or []),
        "yearly_returns": list(primary_report.get("yearly_returns", []) or []),
        "slippage_results": slippage_results,
        "final_verdict": final_verdict,
    }


def _shift_months(date_value: date, months: int) -> date:
    month_index = (int(date_value.month) - 1) + int(months)
    year = int(date_value.year) + (month_index // 12)
    month = (month_index % 12) + 1
    day = min(int(date_value.day), calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    pos = max(0.0, min(1.0, float(q))) * (len(ordered) - 1)
    low = int(math.floor(pos))
    high = min(low + 1, len(ordered) - 1)
    weight = pos - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _verdict_rank(verdict: str) -> int:
    return {"PASS": 2, "WATCH": 1, "REJECT": 0}.get(str(verdict or "").upper(), 0)


def _gate_status(*, pass_ok: bool, watch_ok: bool) -> str:
    if pass_ok:
        return "PASS"
    if watch_ok:
        return "WATCH"
    return "REJECT"


def _rollup_verdict(statuses: Iterable[str]) -> str:
    seen = [str(status or "").upper() for status in statuses if str(status or "").strip()]
    if not seen:
        return "REJECT"
    if any(status == "REJECT" for status in seen):
        return "REJECT"
    if any(status == "WATCH" for status in seen):
        return "WATCH"
    return "PASS"


def _walk_forward_slices(
    *,
    start_date: int,
    end_date: int,
    train_months: int = 36,
    test_months: int = 6,
    step_months: int = 3,
) -> list[dict[str, Any]]:
    start_obj = _date_to_obj(start_date)
    end_obj = _date_to_obj(end_date)
    train_start = start_obj
    slices: list[dict[str, Any]] = []
    index = 1

    while train_start <= end_obj:
        train_end = _shift_months(train_start, train_months) - timedelta(days=1)
        test_start = train_end + timedelta(days=1)
        test_end = _shift_months(test_start, test_months) - timedelta(days=1)
        if test_end > end_obj:
            break

        slices.append(
            {
                "index": index,
                "label": f"WF-{index:02d}",
                "train_start_date": _obj_to_date_int(train_start),
                "train_end_date": _obj_to_date_int(train_end),
                "test_start_date": _obj_to_date_int(test_start),
                "test_end_date": _obj_to_date_int(test_end),
                "train_label": f"{train_start.isoformat()} ~ {train_end.isoformat()}",
                "test_label": f"{test_start.isoformat()} ~ {test_end.isoformat()}",
            }
        )

        next_train_start = _shift_months(train_start, step_months)
        if next_train_start <= train_start:
            break
        train_start = next_train_start
        index += 1

    return slices


def _clone_trade_with_slippage(trade: Any, *, slip_per_side: float, point_value: float = 200.0) -> Trade:
    gross_pnl = float(getattr(trade, "gross_pnl", 0.0) or 0.0)
    fee = float(getattr(trade, "fee", 0.0) or 0.0)
    tax = float(getattr(trade, "tax", 0.0) or 0.0)
    slip_cost = float(point_value) * float(slip_per_side) * 2.0
    return Trade(
        entry_date=int(getattr(trade, "entry_date", 0) or 0),
        entry_time=int(getattr(trade, "entry_time", 0) or 0),
        entry_price=float(getattr(trade, "entry_price", 0.0) or 0.0),
        entry_action=str(getattr(trade, "entry_action", "")),
        exit_date=int(getattr(trade, "exit_date", 0) or 0),
        exit_time=int(getattr(trade, "exit_time", 0) or 0),
        exit_price=float(getattr(trade, "exit_price", 0.0) or 0.0),
        exit_action=str(getattr(trade, "exit_action", "")),
        direction=int(getattr(trade, "direction", 0) or 0),
        points=float(getattr(trade, "points", 0.0) or 0.0),
        gross_pnl=gross_pnl,
        fee=fee,
        tax=tax,
        slip_cost=slip_cost,
        net_pnl=gross_pnl - fee - tax - slip_cost,
    )


def _evaluate_walk_forward(
    *,
    params: Mapping[str, Any],
    minute_bars: list[Any],
    daily_bars: list[Any],
    script_name: str,
    capital: int,
    slip_per_side: float,
    start_date: int,
    end_date: int,
) -> dict[str, Any]:
    slices = _walk_forward_slices(start_date=start_date, end_date=end_date)
    rows: list[dict[str, Any]] = []

    for slice_info in slices:
        slice_minute_bars, slice_daily_bars = _filter_bars_for_period(
            minute_bars,
            daily_bars,
            start_date=int(slice_info["train_start_date"]),
            end_date=int(slice_info["test_end_date"]),
        )
        train_minute_bars, train_daily_bars = _filter_bars_for_period(
            slice_minute_bars,
            slice_daily_bars,
            start_date=int(slice_info["train_start_date"]),
            end_date=int(slice_info["train_end_date"]),
        )
        _test_minute_bars, test_daily_bars = _filter_bars_for_period(
            slice_minute_bars,
            slice_daily_bars,
            start_date=int(slice_info["test_start_date"]),
            end_date=int(slice_info["test_end_date"]),
        )

        result = run_0313plus_backtest(
            slice_minute_bars,
            slice_daily_bars,
            dict(params),
            script_name,
            slip_per_side=float(slip_per_side),
        )
        trades = list(getattr(result, "trades", []) or [])
        train_trades = [
            trade
            for trade in trades
            if int(slice_info["train_start_date"]) <= int(getattr(trade, "exit_date", 0) or 0) <= int(slice_info["train_end_date"])
        ]
        test_trades = [
            trade
            for trade in trades
            if int(slice_info["test_start_date"]) <= int(getattr(trade, "exit_date", 0) or 0) <= int(slice_info["test_end_date"])
        ]

        train_report = build_report(SimpleNamespace(trades=train_trades), train_daily_bars, capital)
        test_report = build_report(SimpleNamespace(trades=test_trades), test_daily_bars, capital)
        train_metrics = _holdout_trade_metrics(train_trades)
        test_metrics = _holdout_trade_metrics(test_trades)
        train_total_return_pct = float(train_report.get("total_return", 0.0)) * 100.0
        test_total_return_pct = float(test_report.get("total_return", 0.0)) * 100.0
        test_mdd_pct = float(test_report.get("mdd_pct", 0.0)) * 100.0

        status = _gate_status(
            pass_ok=(
                float(test_metrics["net_profit"]) > 0.0
                and float(test_metrics["profit_factor"]) >= 1.05
                and float(test_mdd_pct) <= 18.0
                and int(test_metrics["trade_count"]) >= 4
            ),
            watch_ok=(
                float(test_metrics["net_profit"]) >= 0.0
                and float(test_metrics["profit_factor"]) >= 0.95
                and float(test_mdd_pct) <= 25.0
                and int(test_metrics["trade_count"]) >= 2
            ),
        )

        rows.append(
            {
                "切片": str(slice_info["label"]),
                "Train 區間": str(slice_info["train_label"]),
                "Test 區間": str(slice_info["test_label"]),
                "Train 滑價淨利": float(train_metrics["net_profit"]),
                "Train 報酬率": float(train_total_return_pct),
                "Train PF": float(train_metrics["profit_factor"]),
                "Train 交易筆數": int(train_metrics["trade_count"]),
                "Test 滑價淨利": float(test_metrics["net_profit"]),
                "Test 報酬率": float(test_total_return_pct),
                "Test PF": float(test_metrics["profit_factor"]),
                "Test MDD": float(test_mdd_pct),
                "Test 交易筆數": int(test_metrics["trade_count"]),
                "狀態": status,
            }
        )

    pass_count = sum(1 for row in rows if str(row.get("狀態")) == "PASS")
    watch_count = sum(1 for row in rows if str(row.get("狀態")) == "WATCH")
    reject_count = sum(1 for row in rows if str(row.get("狀態")) == "REJECT")
    slice_count = len(rows)
    pass_rate = (pass_count / slice_count) if slice_count else 0.0
    worst_test_return_pct = min((float(row.get("Test 報酬率", 0.0) or 0.0) for row in rows), default=0.0)
    median_test_return_pct = float(pd.Series([float(row.get("Test 報酬率", 0.0) or 0.0) for row in rows]).median()) if rows else 0.0
    min_test_trades = min((int(row.get("Test 交易筆數", 0) or 0) for row in rows), default=0)
    verdict = _gate_status(
        pass_ok=(
            slice_count >= 4
            and pass_rate >= 0.60
            and reject_count == 0
            and worst_test_return_pct >= -5.0
            and min_test_trades >= 4
        ),
        watch_ok=(
            slice_count >= 3
            and pass_rate >= 0.40
            and reject_count <= 1
            and worst_test_return_pct >= -8.0
            and min_test_trades >= 2
        ),
    )
    return {
        "train_months": 36,
        "test_months": 6,
        "step_months": 3,
        "slice_count": slice_count,
        "pass_count": pass_count,
        "watch_count": watch_count,
        "reject_count": reject_count,
        "pass_rate": float(pass_rate),
        "worst_test_return_pct": float(worst_test_return_pct),
        "median_test_return_pct": float(median_test_return_pct),
        "min_test_trades": int(min_test_trades),
        "verdict": verdict,
        "rows": rows,
    }


def _daily_regime_context(daily_bars: list[Any]) -> dict[int, dict[str, Any]]:
    if not daily_bars:
        return {}

    rows: list[dict[str, Any]] = []
    for idx, bar in enumerate(daily_bars):
        lookback = daily_bars[max(0, idx - 19) : idx + 1]
        first_close = float(getattr(lookback[0], "close", 0.0) or 0.0)
        last_close = float(getattr(bar, "close", 0.0) or 0.0)
        trend_return = ((last_close / first_close) - 1.0) if abs(first_close) > 1e-12 else 0.0
        vol_values = [
            (float(getattr(item, "high", 0.0) or 0.0) - float(getattr(item, "low", 0.0) or 0.0))
            / max(abs(float(getattr(item, "close", 0.0) or 0.0)), 1e-12)
            for item in lookback
        ]
        avg_range_pct = sum(vol_values) / len(vol_values) if vol_values else 0.0
        rows.append(
            {
                "date": int(getattr(bar, "date", 0) or 0),
                "trend_return": float(trend_return),
                "avg_range_pct": float(avg_range_pct),
            }
        )

    trend_values = [float(row["trend_return"]) for row in rows]
    vol_values = [float(row["avg_range_pct"]) for row in rows]
    trend_q1 = _quantile(trend_values, 1.0 / 3.0)
    trend_q2 = _quantile(trend_values, 2.0 / 3.0)
    vol_q1 = _quantile(vol_values, 1.0 / 3.0)
    vol_q2 = _quantile(vol_values, 2.0 / 3.0)

    context: dict[int, dict[str, Any]] = {}
    for row in rows:
        trend_return = float(row["trend_return"])
        avg_range_pct = float(row["avg_range_pct"])
        if trend_return <= trend_q1:
            trend_label = "下行"
        elif trend_return <= trend_q2:
            trend_label = "盤整"
        else:
            trend_label = "上行"

        if avg_range_pct <= vol_q1:
            vol_label = "低波動"
            extra_slip = 0.0
        elif avg_range_pct <= vol_q2:
            vol_label = "中波動"
            extra_slip = 0.5
        else:
            vol_label = "高波動"
            extra_slip = 1.0

        context[int(row["date"])] = {
            "trend_label": trend_label,
            "vol_label": vol_label,
            "bucket_label": f"{trend_label} × {vol_label}",
            "extra_slip": float(extra_slip),
            "trend_return": trend_return,
            "avg_range_pct": avg_range_pct,
        }
    return context


def _evaluate_regime_concentration(*, trades: list[Any], daily_bars: list[Any]) -> dict[str, Any]:
    regime_context = _daily_regime_context(daily_bars)
    grouped: dict[str, dict[str, Any]] = {}

    for trade in trades or []:
        exit_date = int(getattr(trade, "exit_date", 0) or 0)
        bucket = regime_context.get(exit_date) or {
            "trend_label": "未知",
            "vol_label": "未知",
            "bucket_label": "未知 × 未知",
        }
        bucket_label = str(bucket["bucket_label"])
        row = grouped.setdefault(
            bucket_label,
            {
                "方向": str(bucket["trend_label"]),
                "波動": str(bucket["vol_label"]),
                "Bucket": bucket_label,
                "交易筆數": 0,
                "滑價淨利": 0.0,
                "獲利筆數": 0,
            },
        )
        net_pnl = float(getattr(trade, "net_pnl", 0.0) or 0.0)
        row["交易筆數"] += 1
        row["滑價淨利"] += net_pnl
        if net_pnl > 0.0:
            row["獲利筆數"] += 1

    rows: list[dict[str, Any]] = []
    positive_profit_total = sum(max(float(row["滑價淨利"]), 0.0) for row in grouped.values())
    total_net_profit = sum(float(row["滑價淨利"]) for row in grouped.values())
    for row in grouped.values():
        trades_count = int(row["交易筆數"] or 0)
        win_rate = (int(row["獲利筆數"] or 0) / trades_count) * 100.0 if trades_count else 0.0
        net_profit = float(row["滑價淨利"] or 0.0)
        rows.append(
            {
                "方向": row["方向"],
                "波動": row["波動"],
                "Bucket": row["Bucket"],
                "交易筆數": trades_count,
                "滑價淨利": net_profit,
                "勝率": float(win_rate),
                "平均每筆": (net_profit / trades_count) if trades_count else 0.0,
                "正利潤占比": (max(net_profit, 0.0) / positive_profit_total) if positive_profit_total > 0 else 0.0,
            }
        )

    order_map = {"上行": 0, "盤整": 1, "下行": 2, "未知": 3, "低波動": 0, "中波動": 1, "高波動": 2}
    rows.sort(key=lambda item: (order_map.get(str(item["方向"]), 99), order_map.get(str(item["波動"]), 99)))

    dominant_row = max(rows, key=lambda item: float(item.get("正利潤占比", 0.0) or 0.0), default={})
    strongest_row = max(rows, key=lambda item: float(item.get("滑價淨利", -1e18) or -1e18), default={})
    weakest_row = min(rows, key=lambda item: float(item.get("滑價淨利", 1e18) or 1e18), default={})
    dominant_share = float(dominant_row.get("正利潤占比", 0.0) or 0.0)
    positive_bucket_count = sum(1 for row in rows if float(row.get("滑價淨利", 0.0) or 0.0) > 0.0)
    verdict = _gate_status(
        pass_ok=(dominant_share <= 0.55 and positive_bucket_count >= 3 and total_net_profit > 0.0),
        watch_ok=(dominant_share <= 0.70 and positive_bucket_count >= 2 and total_net_profit > 0.0),
    )
    return {
        "rows": rows,
        "dominant_bucket": str(dominant_row.get("Bucket") or "--"),
        "dominant_share": dominant_share,
        "positive_bucket_count": int(positive_bucket_count),
        "strongest_bucket": str(strongest_row.get("Bucket") or "--"),
        "weakest_bucket": str(weakest_row.get("Bucket") or "--"),
        "verdict": verdict,
    }


def _evaluate_dynamic_slippage(
    *,
    trades: list[Any],
    daily_bars: list[Any],
    capital: int,
    baseline_slip_per_side: float,
    point_value: float = 200.0,
) -> dict[str, Any]:
    regime_context = _daily_regime_context(daily_bars)
    bucket_counts = {"低波動": 0, "中波動": 0, "高波動": 0}
    stressed_trades: list[Trade] = []

    for trade in trades or []:
        context = regime_context.get(int(getattr(trade, "exit_date", 0) or 0)) or {}
        vol_label = str(context.get("vol_label") or "中波動")
        bucket_counts[vol_label] = int(bucket_counts.get(vol_label, 0)) + 1
        stressed_trades.append(
            _clone_trade_with_slippage(
                trade,
                slip_per_side=float(baseline_slip_per_side) + float(context.get("extra_slip", 0.5) or 0.5),
                point_value=float(point_value),
            )
        )

    stressed_result = SimpleNamespace(trades=stressed_trades)
    stressed_report = build_report(stressed_result, daily_bars, capital)
    stressed_metrics = _holdout_trade_metrics(stressed_trades)
    baseline_net = sum(float(getattr(trade, "net_pnl", 0.0) or 0.0) for trade in trades or [])
    stressed_net = float(stressed_metrics["net_profit"])
    decay_pct = ((baseline_net - stressed_net) / abs(baseline_net)) * 100.0 if abs(baseline_net) > 1e-12 else 0.0
    verdict = _gate_status(
        pass_ok=(
            stressed_net > 0.0
            and float(stressed_metrics["profit_factor"]) >= 1.05
            and float(stressed_report.get("mdd_pct", 0.0)) * 100.0 <= 20.0
        ),
        watch_ok=(
            stressed_net > 0.0
            and float(stressed_metrics["profit_factor"]) >= 0.95
            and float(stressed_report.get("mdd_pct", 0.0)) * 100.0 <= 25.0
        ),
    )
    return {
        "scenario_label": "波動分層滑價",
        "baseline_slip_per_side": float(baseline_slip_per_side),
        "bucket_counts": bucket_counts,
        "net_profit": stressed_net,
        "trade_count": int(stressed_metrics["trade_count"]),
        "win_rate_pct": float(stressed_metrics["win_rate_pct"]),
        "avg_net_profit": float(stressed_metrics["avg_net_profit"]),
        "profit_factor": float(stressed_metrics["profit_factor"]),
        "total_return_pct": float(stressed_report.get("total_return", 0.0)) * 100.0,
        "mdd_pct": float(stressed_report.get("mdd_pct", 0.0)) * 100.0,
        "mdd_amount": float(stressed_report.get("mdd_amount", 0.0)),
        "decay_pct": float(decay_pct),
        "verdict": verdict,
    }


def run_0101_development_qualification(
    *,
    params: Mapping[str, Any],
    minute_path: str,
    daily_path: str,
    script_name: str,
    capital: int,
    slip_per_side: float,
    snapshot_metrics: Mapping[str, Any] | None = None,
    research_periods: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    minute_bars, daily_bars = load_market_data(minute_path, daily_path)
    periods = dict(research_periods or _resolve_0101_research_periods_from_daily_bars(daily_bars))
    if not periods:
        return {}

    development_minute_bars, development_daily_bars = _filter_bars_for_period(
        minute_bars,
        daily_bars,
        start_date=int(periods["development_start_date"]),
        end_date=int(periods["development_end_date"]),
    )
    development_result = run_0313plus_backtest(
        development_minute_bars,
        development_daily_bars,
        dict(params),
        script_name,
        slip_per_side=float(slip_per_side),
    )
    development_trades = list(getattr(development_result, "trades", []) or [])
    development_report = build_report(SimpleNamespace(trades=development_trades), development_daily_bars, capital)
    development_metrics = _holdout_trade_metrics(development_trades)
    walk_forward = _evaluate_walk_forward(
        params=params,
        minute_bars=minute_bars,
        daily_bars=daily_bars,
        script_name=script_name,
        capital=int(capital),
        slip_per_side=float(slip_per_side),
        start_date=int(periods["development_start_date"]),
        end_date=int(periods["development_end_date"]),
    )
    regime = _evaluate_regime_concentration(trades=development_trades, daily_bars=development_daily_bars)
    dynamic_slippage = _evaluate_dynamic_slippage(
        trades=development_trades,
        daily_bars=development_daily_bars,
        capital=int(capital),
        baseline_slip_per_side=float(slip_per_side),
    )

    gate_rows: list[dict[str, Any]] = []
    gate_rows.append(
        {
            "Gate": "開發區基礎生存",
            "狀態": _gate_status(
                pass_ok=(
                    float(development_metrics["net_profit"]) > 0.0
                    and float(development_metrics["profit_factor"]) >= 1.15
                    and float(development_report.get("mdd_pct", 0.0)) * 100.0 <= 18.0
                    and int(development_metrics["trade_count"]) >= 60
                ),
                watch_ok=(
                    float(development_metrics["net_profit"]) > 0.0
                    and float(development_metrics["profit_factor"]) >= 1.0
                    and float(development_report.get("mdd_pct", 0.0)) * 100.0 <= 22.0
                    and int(development_metrics["trade_count"]) >= 40
                ),
            ),
            "觀察": (
                f"滑價淨利 {float(development_metrics['net_profit']):,.0f} / "
                f"PF {float(development_metrics['profit_factor']):.2f} / "
                f"MDD {float(development_report.get('mdd_pct', 0.0)) * 100.0:.2f}% / "
                f"交易 {int(development_metrics['trade_count']):,} 筆"
            ),
        }
    )
    gate_rows.append(
        {
            "Gate": "Walk-Forward 穩定度",
            "狀態": str(walk_forward.get("verdict") or "REJECT"),
            "觀察": (
                f"{int(walk_forward.get('pass_count') or 0):,}/{int(walk_forward.get('slice_count') or 0):,} 切片 PASS，"
                f"通過率 {float(walk_forward.get('pass_rate') or 0.0) * 100.0:.1f}% / "
                f"最差切片 {float(walk_forward.get('worst_test_return_pct') or 0.0):.2f}% / "
                f"最少交易 {int(walk_forward.get('min_test_trades') or 0):,} 筆"
            ),
        }
    )
    gate_rows.append(
        {
            "Gate": "Regime 集中度",
            "狀態": str(regime.get("verdict") or "REJECT"),
            "觀察": (
                f"最大優勢桶 {str(regime.get('dominant_bucket') or '--')} 占正利潤 "
                f"{float(regime.get('dominant_share') or 0.0) * 100.0:.1f}% / "
                f"正報酬桶 {int(regime.get('positive_bucket_count') or 0):,} 個"
            ),
        }
    )
    gate_rows.append(
        {
            "Gate": "動態滑價壓測",
            "狀態": str(dynamic_slippage.get("verdict") or "REJECT"),
            "觀察": (
                f"壓測後淨利 {float(dynamic_slippage.get('net_profit') or 0.0):,.0f} / "
                f"PF {float(dynamic_slippage.get('profit_factor') or 0.0):.2f} / "
                f"MDD {float(dynamic_slippage.get('mdd_pct') or 0.0):.2f}% / "
                f"衰減 {float(dynamic_slippage.get('decay_pct') or 0.0):.1f}%"
            ),
        }
    )

    if snapshot_metrics:
        plateau_score = float(snapshot_metrics.get("plateau_score") or 0.0)
        worst_window_return = float(snapshot_metrics.get("worst_window_return") or 0.0)
        gate_rows.append(
            {
                "Gate": "參數平台穩定度",
                "狀態": _gate_status(
                    pass_ok=(plateau_score >= 1.0 and worst_window_return >= 0.0),
                    watch_ok=(plateau_score >= 0.0 and worst_window_return >= -4.0),
                ),
                "觀察": (
                    f"平台分數 {plateau_score:.2f} / "
                    f"最差年窗 {worst_window_return:.2f}% / "
                    f"穩健分數 {float(snapshot_metrics.get('robust_score') or snapshot_metrics.get('composite_score') or 0.0):.2f}"
                ),
            }
        )

    verdict = _rollup_verdict(row.get("狀態") for row in gate_rows)
    summary_lines = [
        f"開發區資格審查結論為 {verdict}，共 {sum(1 for row in gate_rows if str(row.get('狀態')) == 'PASS')}/{len(gate_rows)} 個 gate 達到 PASS。",
        (
            f"Walk-Forward 採 Train 36 個月 / Test 6 個月 / Step 3 個月，"
            f"目前 {int(walk_forward.get('slice_count') or 0)} 個切片中 PASS {int(walk_forward.get('pass_count') or 0)} 個，"
            f"最差切片報酬 {float(walk_forward.get('worst_test_return_pct') or 0.0):.2f}% 。"
        ),
        (
            f"Regime 最集中桶為 {str(regime.get('dominant_bucket') or '--')}，"
            f"吃掉 {float(regime.get('dominant_share') or 0.0) * 100.0:.1f}% 正利潤，"
            f"代表策略是否過度依賴單一市場狀態可一眼看清。"
        ),
        (
            f"動態滑價壓測後淨利 {float(dynamic_slippage.get('net_profit') or 0.0):,.0f}，"
            f"PF {float(dynamic_slippage.get('profit_factor') or 0.0):.2f}，"
            f"MDD {float(dynamic_slippage.get('mdd_pct') or 0.0):.2f}% 。"
        ),
    ]
    return {
        "research_profile_tag": str(periods.get("research_profile_tag") or RESEARCH_PROFILE_TAG_0101),
        "development_label": periods["development_label"],
        "holdout_label": periods["holdout_label"],
        "verdict": verdict,
        "gate_rows": gate_rows,
        "walk_forward": walk_forward,
        "regime": regime,
        "dynamic_slippage": dynamic_slippage,
        "summary_lines": summary_lines,
    }


def build_best_snapshot(source: Mapping[str, Any] | None, param_names: list[str]) -> dict[str, Any]:
    if not source:
        return {}
    params = {
        name: source.get(name)
        for name in param_names
        if name in source and source.get(name) is not None
    }
    year_returns = {
        str(key).replace("year_return_", ""): source.get(key)
        for key in sorted(source.keys())
        if str(key).startswith("year_return_") and source.get(key) is not None
    }
    snapshot = {
        "params": params,
        "total_return": source.get("total_return"),
        "mdd_pct": source.get("mdd_pct"),
        "n_trades": source.get("n_trades"),
        "composite_score": source.get("composite_score"),
        "robust_score": source.get("robust_score", source.get("composite_score")),
        "plateau_score": source.get("plateau_score"),
        "worst_window_return": source.get("worst_window_return"),
        "slip_stress_score": source.get("slip_stress_score"),
        "year_avg_return": source.get("year_avg_return"),
        "year_returns": year_returns,
    }
    source_slip = _normalize_slip_per_side(source.get("slip_per_side"))
    if source_slip is not None:
        snapshot["slip_per_side"] = source_slip
    return snapshot


def build_current_best_snapshot(
    top_df: pd.DataFrame,
    param_names: list[str],
    slip_per_side: float | None = None,
) -> dict[str, Any]:
    if top_df.empty:
        return {}
    row = top_df.iloc[0].to_dict()
    snapshot = build_best_snapshot(row, param_names)
    current_slip = _normalize_slip_per_side(slip_per_side)
    if snapshot and current_slip is not None:
        snapshot["slip_per_side"] = current_slip
    return snapshot


def build_best_export_payload(
    *,
    top_df: pd.DataFrame,
    params_meta: list[dict[str, Any]],
    xs_path: str,
    minute_path: str,
    daily_path: str,
    script_name: str,
    slip_per_side: float,
) -> dict[str, Any]:
    if top_df.empty:
        return {}

    saved_at_dt = datetime.now()
    saved_at_text = saved_at_dt.isoformat(timespec="seconds")
    best_row = {key: _json_safe_value(value) for key, value in top_df.iloc[0].to_dict().items()}
    best_params = _best_params_from_row(best_row, params_meta)
    if not best_params:
        return {}

    signature = hashlib.sha1(
        json.dumps(
            {"params": best_params, "slip_per_side": _normalize_slip_per_side(slip_per_side)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    base_xs_text = Path(xs_path).read_text(encoding="utf-8")
    indicator_xs_text = render_indicator_xs(base_xs_text, best_params)
    trade_xs_text = render_trade_xs(base_xs_text, best_params)

    trade_lines: list[str] = []
    artifact_error: str | None = None
    try:
        minute_bars, daily_bars = load_market_data(minute_path, daily_path)
        result = run_0313plus_backtest(
            minute_bars,
            daily_bars,
            best_params,
            script_name,
            slip_per_side=float(slip_per_side),
        )
        trade_lines = _build_trade_lines(list(getattr(result, "trades", []) or []))
    except Exception as exc:
        artifact_error = str(exc)

    params_header = ",".join(f"{name}={_format_param_value(value)}" for name, value in best_params.items())
    txt_lines = [params_header]
    if trade_lines:
        txt_lines.extend(trade_lines)
    elif artifact_error:
        txt_lines.append(f"匯出交易明細失敗: {artifact_error}")
    else:
        txt_lines.append("無逐筆交易資料")

    return {
        "signature": signature,
        "params": best_params,
        "slip_per_side": _normalize_slip_per_side(slip_per_side),
        "indicator_xs_bytes": indicator_xs_text.encode("utf-8"),
        "trade_xs_bytes": trade_xs_text.encode("utf-8"),
        "txt_bytes": ("\n".join(txt_lines) + "\n").encode("utf-8"),
        "indicator_xs_file_name": f"{script_name}_{signature[:8]}_indicator.xs",
        "trade_xs_file_name": f"{script_name}_{signature[:8]}_trade.xs",
        "txt_file_name": f"{script_name}_{signature[:8]}_best_strategy.txt",
        "artifact_error": artifact_error,
    }


def load_historical_best_snapshot(
    param_names: list[str],
    slip_per_side: float | None = None,
    research_profile_tag: str | None = None,
) -> dict[str, Any]:
    latest_memory = _load_latest_memory()
    persistent_best_payload = _read_json_dict(PERSISTENT_BEST_PARAMS_JSON)
    persistent_top10_payload = _read_json_dict(PERSISTENT_TOP10_JSON)
    normalized_slip = _normalize_slip_per_side(slip_per_side)

    top_rows = persistent_top10_payload.get("rows") or []
    top_candidates = [
        dict(row)
        for row in top_rows
        if isinstance(row, dict)
        and _slippage_matches(row, normalized_slip)
        and _research_profile_matches(row, research_profile_tag)
    ]
    top_best_row = max(top_candidates, key=_historical_compare_key) if top_candidates else {}
    latest_best_source = _payload_best_source(latest_memory)
    if not _slippage_matches(latest_best_source, normalized_slip) or not _research_profile_matches(
        latest_best_source,
        research_profile_tag,
    ):
        latest_best_source = {}
    persistent_best_source = _payload_best_source(persistent_best_payload)
    if not _slippage_matches(persistent_best_source, normalized_slip) or not _research_profile_matches(
        persistent_best_source,
        research_profile_tag,
    ):
        persistent_best_source = {}

    candidate_bundle: list[dict[str, Any]] = []
    if persistent_best_source:
        candidate_bundle.append(
            {
                "source": persistent_best_source,
                "saved_at": persistent_best_payload.get("saved_at"),
                "optimization_mode": persistent_best_payload.get("optimization_mode"),
                "tested_count": persistent_best_payload.get("tested_count"),
                "total_count": persistent_best_payload.get("total_count"),
                "elapsed_seconds": persistent_best_payload.get("elapsed_seconds"),
                "cpu_limit_pct": persistent_best_payload.get("cpu_limit_pct"),
                "effective_workers": persistent_best_payload.get("effective_workers"),
            }
        )
    if top_best_row:
        candidate_bundle.append(
            {
                "source": top_best_row,
                "saved_at": top_best_row.get("saved_at") or persistent_top10_payload.get("saved_at"),
                "optimization_mode": top_best_row.get("optimization_mode"),
                "tested_count": None,
                "total_count": None,
                "elapsed_seconds": None,
                "cpu_limit_pct": None,
                "effective_workers": None,
            }
        )
    if latest_best_source:
        candidate_bundle.append(
            {
                "source": latest_best_source,
                "saved_at": latest_memory.get("saved_at"),
                "optimization_mode": latest_memory.get("optimization_mode"),
                "tested_count": latest_memory.get("tested_count"),
                "total_count": latest_memory.get("total_count"),
                "elapsed_seconds": latest_memory.get("elapsed_seconds"),
                "cpu_limit_pct": latest_memory.get("cpu_limit_pct"),
                "effective_workers": latest_memory.get("effective_workers"),
            }
        )

    if not candidate_bundle:
        return {}
    best_candidate = max(candidate_bundle, key=lambda item: _historical_compare_key(item.get("source")))
    metrics_source = dict(best_candidate.get("source") or {})
    snapshot = build_best_snapshot(metrics_source, param_names)
    if not snapshot:
        return {}

    snapshot.update(
        {
            "saved_at": best_candidate.get("saved_at"),
            "optimization_mode": best_candidate.get("optimization_mode"),
            "tested_count": best_candidate.get("tested_count"),
            "total_count": best_candidate.get("total_count"),
            "elapsed_seconds": best_candidate.get("elapsed_seconds"),
            "cpu_limit_pct": best_candidate.get("cpu_limit_pct"),
            "effective_workers": best_candidate.get("effective_workers"),
        }
    )
    return snapshot

def estimate_run_count(
    mode: str,
    ui_param_specs: list[dict[str, Any]],
    params_meta: list[dict[str, Any]],
    *,
    seed_keep_count: int,
) -> int:
    del mode, seed_keep_count
    _fixed_params, variable_specs = build_search_space_from_ui(ui_param_specs, params_meta)
    if not variable_specs:
        return 1
    return sum(len(spec["values"]) for spec in variable_specs)


def grid_run_block_reason(mode: str, estimated_total: int) -> str | None:
    del mode, estimated_total
    return None


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_slip_per_side(value: Any) -> float | None:
    try:
        numeric = float(value)
    except Exception:
        return None
    if pd.isna(numeric):
        return None
    return float(numeric)


def _slippage_matches(source: Mapping[str, Any] | None, slip_per_side: float | None) -> bool:
    if slip_per_side is None:
        return True
    if not source:
        return False
    source_slip = _normalize_slip_per_side(source.get("slip_per_side"))
    if source_slip is None:
        return False
    return abs(source_slip - float(slip_per_side)) < 1e-9


def _research_profile_matches(source: Mapping[str, Any] | None, research_profile_tag: str | None) -> bool:
    if not research_profile_tag:
        return True
    if not source:
        return False
    return str(source.get("research_profile_tag") or "").strip() == str(research_profile_tag).strip()


def _json_safe_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        if isinstance(value, float) and pd.isna(value):
            return None
        return value
    if pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return _json_safe_value(value.item())
        except Exception:
            pass
    return str(value)


def _format_param_value(value: Any) -> str:
    normalized = _json_safe_value(value)
    if isinstance(normalized, int):
        return str(normalized)
    if isinstance(normalized, float):
        if abs(normalized - round(normalized)) < 1e-9:
            return str(int(round(normalized)))
        return f"{normalized:.4f}".rstrip("0").rstrip(".")
    return str(normalized)


def _format_trade_number(value: Any) -> str:
    try:
        numeric = float(value)
    except Exception:
        return str(value)
    if abs(numeric - round(numeric)) < 1e-9:
        return str(int(round(numeric)))
    return f"{numeric:.4f}".rstrip("0").rstrip(".")


def _build_trade_lines(trades: list[Any]) -> list[str]:
    lines: list[str] = []
    for trade in trades or []:
        entry_action = str(getattr(trade, "entry_action", "") or "").strip()
        exit_action = str(getattr(trade, "exit_action", "") or "").strip()
        if not entry_action:
            entry_action = "新買" if int(getattr(trade, "direction", 1) or 1) >= 0 else "新賣"
        if not exit_action:
            exit_action = "平賣" if int(getattr(trade, "direction", 1) or 1) >= 0 else "平買"
        entry_dt = f"{int(getattr(trade, 'entry_date', 0) or 0)}{int(getattr(trade, 'entry_time', 0) or 0):06d}"
        exit_dt = f"{int(getattr(trade, 'exit_date', 0) or 0)}{int(getattr(trade, 'exit_time', 0) or 0):06d}"
        lines.append(f"{entry_dt} {_format_trade_number(getattr(trade, 'entry_price', ''))} {entry_action}")
        lines.append(f"{exit_dt} {_format_trade_number(getattr(trade, 'exit_price', ''))} {exit_action}")
    return lines


def _best_params_from_row(best_row: Mapping[str, Any], params_meta: list[dict[str, Any]]) -> dict[str, int | float]:
    params: dict[str, int | float] = {}
    for meta in params_meta:
        name = str(meta["name"])
        if name not in best_row:
            continue
        raw_value = _json_safe_value(best_row.get(name))
        if raw_value in (None, ""):
            continue
        value_type = str(meta.get("type") or "float").lower()
        if value_type == "int":
            params[name] = int(round(float(raw_value)))
        else:
            params[name] = round(float(raw_value), 10)
    return params


def _render_optimized_xs_text(base_xs_text: str, best_params: Mapping[str, Any]) -> str:
    rendered_lines: list[str] = []
    for line in base_xs_text.splitlines():
        updated_line = line
        for name, value in best_params.items():
            pattern = rf"^(\s*{re.escape(name)}\s*\()\s*([^,]+)(,.*)$"
            match = re.match(pattern, updated_line)
            if match is None:
                continue
            updated_line = f"{match.group(1)}{_format_param_value(value)}{match.group(3)}"
            break
        rendered_lines.append(updated_line)
    return "\n".join(rendered_lines) + "\n"


def _write_latest_memory(payload: dict[str, Any]) -> None:
    _LATEST_RUN_MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = (
        "# -*- coding: utf-8 -*-\n"
        "from __future__ import annotations\n\n"
        "# Auto-generated by MQQuant 01 when an optimization run is saved.\n"
        "# This file is the code-side memory of the latest optimization result.\n\n"
        f"LATEST_OPTIMIZATION_MEMORY = {pformat(payload, width=100, sort_dicts=False)}\n"
    )
    _LATEST_RUN_MEMORY_PATH.write_text(text, encoding="utf-8")


def _strategy_file_return_suffix(total_return: Any) -> str:
    numeric = _safe_float(total_return, 0.0)
    rounded = int(round(abs(numeric)))
    if numeric < 0:
        return f"n{rounded}"
    return str(rounded)


def _strategy_file_stem(saved_at: datetime, total_return: Any) -> str:
    roc_year = max(int(saved_at.year) - 1911, 0)
    return f"{roc_year:03d}{saved_at:%m%d%H%M}{_strategy_file_return_suffix(total_return)}"


def _write_named_param_preset(
    *,
    target_path: Path,
    best_params: Mapping[str, Any],
    params_meta: list[dict[str, Any]],
) -> None:
    meta_map = {
        str(item.get("name") or ""): dict(item)
        for item in params_meta
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    ordered_names = [name for name in meta_map if name in best_params]
    ordered_names.extend(name for name in best_params if name not in meta_map)

    lines = [
        "# 自動產生：本輪最佳策略參數固定檔",
        "# 格式：參數名=起始,結束,間距",
        "# 這份檔案會把每個參數鎖定在本輪最佳值，方便直接回填到 UI 再往下研究。",
        "",
    ]
    for name in ordered_names:
        value = best_params.get(name)
        value_type = str(meta_map.get(name, {}).get("type") or "")
        is_int = value_type == "int" or (isinstance(value, int) and not isinstance(value, bool))
        value_text = _format_param_value(value)
        step_text = "1" if is_int else "0.01"
        lines.append(f"{name}={value_text},{value_text},{step_text}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_existing_top10_rows() -> list[dict[str, Any]]:
    payload = _read_json_dict(PERSISTENT_TOP10_JSON)
    rows = payload.get("rows") or []
    if isinstance(rows, list):
        return [dict(row) for row in rows if isinstance(row, dict)]
    return []


def _ordered_param_names(rows: list[dict[str, Any]], params_meta: list[dict[str, Any]]) -> list[str]:
    preferred = [str(item["name"]) for item in params_meta]
    discovered = {key for row in rows for key in row.keys() if key not in _LEADERBOARD_META_FIELDS}
    ordered = [name for name in preferred if name in discovered]
    ordered.extend(sorted(name for name in discovered if name not in ordered))
    return ordered


def _historical_compare_key(source: Mapping[str, Any] | None) -> tuple[float, float, float, float]:
    if not source:
        return (-1e18, -1e18, -1e18, -1e18)
    return (
        _safe_float(source.get("robust_score", source.get("composite_score")), -1e18),
        _safe_float(source.get("total_return"), -1e18),
        -_safe_float(source.get("mdd_pct"), 1e18),
        _safe_float(source.get("year_avg_return"), -1e18),
    )


def _merge_best_source(best_result: Any, params: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if isinstance(best_result, Mapping):
        merged.update({str(key): _json_safe_value(value) for key, value in best_result.items()})
    if isinstance(params, Mapping):
        for key, value in params.items():
            merged.setdefault(str(key), _json_safe_value(value))
    return merged


def _payload_best_source(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    merged = _merge_best_source(payload.get("best_result"), payload.get("params"))
    payload_slip = _normalize_slip_per_side(payload.get("slip_per_side"))
    if payload_slip is not None:
        merged["slip_per_side"] = payload_slip
    return merged


def _persist_historical_best_payload(payload: dict[str, Any]) -> None:
    PERSISTENT_BEST_PARAMS_JSON.parent.mkdir(parents=True, exist_ok=True)
    PERSISTENT_BEST_PARAMS_JSON.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    best_params = payload.get("params")
    if not isinstance(best_params, Mapping):
        best_params = {}
    _PERSISTENT_BEST_PARAMS_TXT.write_text(
        "\n".join(f"{name}={_format_param_value(value)}" for name, value in best_params.items()) + "\n",
        encoding="utf-8",
    )


def _historical_best_payload(
    *,
    best_row: dict[str, Any],
    best_params: dict[str, Any],
    mode_label: str,
    export_dir: Path,
    best_indicator_xs_path: Path,
    best_trade_xs_path: Path,
    best_txt_path: Path,
    summary_json_path: Path,
    runtime_settings: dict[str, Any],
    tested_count: int,
    total_count: int,
    elapsed_seconds: float,
    compute_elapsed_seconds: float,
    transition_elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "saved_at": _now_text(),
        "run_dir": str(export_dir),
        "optimization_mode": mode_label,
        "research_profile_tag": str(runtime_settings.get("research_profile_tag") or ""),
        "cpu_limit_pct": int(runtime_settings.get("cpu_limit_pct", 100)),
        "memory_limit_pct": int(runtime_settings.get("memory_limit_pct", 100)),
        "requested_workers": int(runtime_settings.get("requested_workers", runtime_settings["max_workers"])),
        "effective_workers": int(runtime_settings["max_workers"]),
        "tested_count": int(tested_count),
        "total_count": int(total_count),
        "elapsed_seconds": float(elapsed_seconds),
        "compute_elapsed_seconds": float(compute_elapsed_seconds),
        "transition_elapsed_seconds": float(transition_elapsed_seconds),
        "best_xs_path": str(best_indicator_xs_path),
        "best_indicator_xs_path": str(best_indicator_xs_path),
        "best_trade_xs_path": str(best_trade_xs_path),
        "best_txt_path": str(best_txt_path),
        "best_summary_json_path": str(summary_json_path),
        "slip_per_side": float(runtime_settings["slip_per_side"]),
        "development_years": runtime_settings.get("development_years"),
        "development_start_date": runtime_settings.get("development_start_date"),
        "development_end_date": runtime_settings.get("development_end_date"),
        "holdout_start_date": runtime_settings.get("holdout_start_date"),
        "holdout_end_date": runtime_settings.get("holdout_end_date"),
        "params": {str(key): _json_safe_value(value) for key, value in best_params.items()},
        "best_result": {str(key): _json_safe_value(value) for key, value in best_row.items()},
    }


def _update_historical_best(
    *,
    best_row: dict[str, Any],
    best_params: dict[str, Any],
    mode_label: str,
    export_dir: Path,
    best_indicator_xs_path: Path,
    best_trade_xs_path: Path,
    best_txt_path: Path,
    summary_json_path: Path,
    runtime_settings: dict[str, Any],
    tested_count: int,
    total_count: int,
    elapsed_seconds: float,
    compute_elapsed_seconds: float,
    transition_elapsed_seconds: float,
) -> bool:
    normalized_slip = _normalize_slip_per_side(runtime_settings.get("slip_per_side"))
    research_profile_tag = str(runtime_settings.get("research_profile_tag") or "")
    current_payload = _read_json_dict(PERSISTENT_BEST_PARAMS_JSON)
    current_source = _payload_best_source(current_payload)
    if not _slippage_matches(current_source, normalized_slip) or not _research_profile_matches(
        current_source,
        research_profile_tag,
    ):
        current_source = {}
    top_rows = [
        row
        for row in _load_existing_top10_rows()
        if _slippage_matches(row, normalized_slip) and _research_profile_matches(row, research_profile_tag)
    ]
    top_best_source = max(top_rows, key=_historical_compare_key) if top_rows else {}
    latest_memory_source = _payload_best_source(_load_latest_memory())
    if not _slippage_matches(latest_memory_source, normalized_slip) or not _research_profile_matches(
        latest_memory_source,
        research_profile_tag,
    ):
        latest_memory_source = {}
    candidate_payload = _historical_best_payload(
        best_row=best_row,
        best_params=best_params,
        mode_label=mode_label,
        export_dir=export_dir,
        best_indicator_xs_path=best_indicator_xs_path,
        best_trade_xs_path=best_trade_xs_path,
        best_txt_path=best_txt_path,
        summary_json_path=summary_json_path,
        runtime_settings=runtime_settings,
        tested_count=tested_count,
        total_count=total_count,
        elapsed_seconds=elapsed_seconds,
        compute_elapsed_seconds=compute_elapsed_seconds,
        transition_elapsed_seconds=transition_elapsed_seconds,
    )
    candidate_source = _payload_best_source(candidate_payload)
    incumbent_candidates = [source for source in (current_source, top_best_source, latest_memory_source) if source]
    incumbent_source = max(incumbent_candidates, key=_historical_compare_key) if incumbent_candidates else {}
    if incumbent_source and _historical_compare_key(candidate_source) <= _historical_compare_key(incumbent_source):
        return False
    _persist_historical_best_payload(candidate_payload)
    return True


def _write_top10_csv(rows: list[dict[str, Any]], params_meta: list[dict[str, Any]]) -> None:
    fieldnames = list(_LEADERBOARD_META_FIELDS) + _ordered_param_names(rows, params_meta)
    with _PERSISTENT_TOP10_CSV.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _leaderboard_row(
    *,
    best_row: dict[str, Any],
    best_params: dict[str, Any],
    mode_label: str,
    export_dir: Path,
    best_xs_path: Path,
    best_txt_path: Path,
    signature: str,
    slip_per_side: float,
    research_profile_tag: str,
    development_years: int | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "saved_at": _now_text(),
        "source_saved_at": _now_text(),
        "source_run_dir": str(export_dir),
        "strategy_signature": signature,
        "optimization_mode": mode_label,
        "research_profile_tag": str(research_profile_tag or RESEARCH_PROFILE_TAG_0101),
        "development_years": development_years,
        "total_return": _safe_float(best_row.get("total_return"), 0.0),
        "mdd_pct": _safe_float(best_row.get("mdd_pct"), 0.0),
        "n_trades": int(round(_safe_float(best_row.get("n_trades"), 0.0))),
        "year_avg_return": _safe_float(best_row.get("year_avg_return"), 0.0),
        "year_return_std": _safe_float(best_row.get("year_return_std"), 0.0),
        "loss_years": int(round(_safe_float(best_row.get("loss_years"), 0.0))),
        "composite_score": _safe_float(best_row.get("composite_score"), 0.0),
        "robust_score": _safe_float(best_row.get("robust_score", best_row.get("composite_score")), 0.0),
        "robust_score_pre_plateau": _safe_float(best_row.get("robust_score_pre_plateau", best_row.get("composite_score")), 0.0),
        "plateau_score": _safe_float(best_row.get("plateau_score"), 0.0),
        "window_count": int(round(_safe_float(best_row.get("window_count"), 0.0))),
        "window_avg_return": _safe_float(best_row.get("window_avg_return"), 0.0),
        "window_median_return": _safe_float(best_row.get("window_median_return"), 0.0),
        "window_return_std": _safe_float(best_row.get("window_return_std"), 0.0),
        "window_loss_count": int(round(_safe_float(best_row.get("window_loss_count"), 0.0))),
        "worst_window_return": _safe_float(best_row.get("worst_window_return"), 0.0),
        "worst_window_mdd_pct": _safe_float(best_row.get("worst_window_mdd_pct"), 0.0),
        "window_consistency_score": _safe_float(best_row.get("window_consistency_score"), 0.0),
        "slip2_total_return": _safe_float(best_row.get("slip2_total_return"), 0.0),
        "slip3_total_return": _safe_float(best_row.get("slip3_total_return"), 0.0),
        "slip4_total_return": _safe_float(best_row.get("slip4_total_return"), 0.0),
        "slip_return_avg": _safe_float(best_row.get("slip_return_avg"), 0.0),
        "slip_return_min": _safe_float(best_row.get("slip_return_min"), 0.0),
        "slip_decay_2_to_4": _safe_float(best_row.get("slip_decay_2_to_4"), 0.0),
        "slip_stress_score": _safe_float(best_row.get("slip_stress_score"), 0.0),
        "xs_path": str(best_xs_path),
        "params_txt_path": str(best_txt_path),
        "params_json": json.dumps(best_params, ensure_ascii=False, sort_keys=True),
        "slip_per_side": float(slip_per_side),
    }
    for key, value in best_row.items():
        key_text = str(key)
        if key_text == "mdd_amount" or key_text.startswith("year_return_"):
            row[key_text] = _json_safe_value(value)
    for name, value in best_params.items():
        row[name] = value
    return row


def _update_persistent_top10(row: dict[str, Any], params_meta: list[dict[str, Any]]) -> None:
    PERSISTENT_TOP10_JSON.parent.mkdir(parents=True, exist_ok=True)
    rows = _load_existing_top10_rows()
    rows.append(row)
    rows.sort(
        key=lambda item: (
            _safe_float(item.get("robust_score", item.get("composite_score")), -1e18),
            _safe_float(item.get("total_return"), -1e18),
            -_safe_float(item.get("mdd_pct"), 1e18),
            _safe_float(item.get("year_avg_return"), -1e18),
        ),
        reverse=True,
    )

    deduped: list[dict[str, Any]] = []
    seen_signatures: set[str] = set()
    for current in rows:
        signature = str(current.get("strategy_signature") or current.get("params_json") or "").strip()
        if signature and signature in seen_signatures:
            continue
        if signature:
            seen_signatures.add(signature)
        deduped.append(current)
        if len(deduped) >= 10:
            break

    ordered_names = _ordered_param_names(deduped, params_meta)
    best_params = {
        name: deduped[0][name]
        for name in ordered_names
        if deduped and name in deduped[0] and deduped[0].get(name) not in (None, "")
    }

    PERSISTENT_TOP10_JSON.write_text(
        json.dumps(
            {
                "saved_at": _now_text(),
                "count": len(deduped),
                "best_params": best_params,
                "rows": deduped,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _write_top10_csv(deduped, params_meta)


def persist_best_run(
    *,
    top_df: pd.DataFrame,
    params_meta: list[dict[str, Any]],
    xs_path: str,
    minute_path: str,
    daily_path: str,
    script_name: str,
    mode_label: str,
    runtime_settings: dict[str, Any],
    hard_filters: dict[str, Any],
    tested_count: int,
    total_count: int,
    elapsed_seconds: float,
    compute_elapsed_seconds: float,
    transition_elapsed_seconds: float,
    fail_reason_counts: dict[str, int],
) -> dict[str, Any]:
    if top_df.empty:
        return {}

    saved_at_dt = datetime.now()
    saved_at_text = saved_at_dt.isoformat(timespec="seconds")
    best_row = {key: _json_safe_value(value) for key, value in top_df.iloc[0].to_dict().items()}
    best_params = _best_params_from_row(best_row, params_meta)
    if not best_params:
        return {}

    signature = hashlib.sha1(
        json.dumps(
            {"params": best_params, "slip_per_side": _normalize_slip_per_side(runtime_settings["slip_per_side"])},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    export_dir = _MQ01_EXPORTS_DIR / f"{saved_at_dt.strftime('%Y%m%d_%H%M%S')}_{script_name}_{signature[:8]}"
    export_dir.mkdir(parents=True, exist_ok=True)

    base_xs_text = Path(xs_path).read_text(encoding="utf-8")
    best_indicator_xs_path = export_dir / "best_indicator.xs"
    best_trade_xs_path = export_dir / "best_trade.xs"
    best_indicator_xs_path.write_text(render_indicator_xs(base_xs_text, best_params), encoding="utf-8")
    best_trade_xs_path.write_text(render_trade_xs(base_xs_text, best_params), encoding="utf-8")

    latest_file_stem = _strategy_file_stem(saved_at_dt, best_row.get("total_return"))
    latest_strategy_xs_path = _LATEST_STRATEGY_DIR / f"{latest_file_stem}.xs"
    latest_param_preset_path = _LATEST_PARAM_PRESET_DIR / f"{latest_file_stem}.txt"
    latest_strategy_xs_path.parent.mkdir(parents=True, exist_ok=True)
    latest_strategy_xs_path.write_text(render_indicator_xs(base_xs_text, best_params), encoding="utf-8")
    _write_named_param_preset(
        target_path=latest_param_preset_path,
        best_params=best_params,
        params_meta=params_meta,
    )

    trade_lines: list[str] = []
    artifact_error: str | None = None
    try:
        minute_bars, daily_bars = load_market_data(minute_path, daily_path)
        result = run_0313plus_backtest(
            minute_bars,
            daily_bars,
            best_params,
            script_name,
            slip_per_side=float(runtime_settings["slip_per_side"]),
        )
        trade_lines = _build_trade_lines(list(getattr(result, "trades", []) or []))
    except Exception as exc:
        artifact_error = str(exc)

    params_header = ",".join(f"{name}={_format_param_value(value)}" for name, value in best_params.items())
    best_txt_path = export_dir / "best_strategy.txt"
    txt_lines = [params_header]
    if trade_lines:
        txt_lines.extend(trade_lines)
    elif artifact_error:
        txt_lines.append(f"匯出交易明細失敗: {artifact_error}")
    else:
        txt_lines.append("無逐筆交易資料")
    best_txt_path.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")

    summary_json_path = export_dir / "summary.json"
    summary_payload = {
        "saved_at": saved_at_text,
        "script_name": script_name,
        "optimization_mode": mode_label,
        "research_profile_tag": str(runtime_settings.get("research_profile_tag") or ""),
        "cpu_limit_pct": int(runtime_settings.get("cpu_limit_pct", 100)),
        "memory_limit_pct": int(runtime_settings.get("memory_limit_pct", 100)),
        "requested_workers": int(runtime_settings.get("requested_workers", runtime_settings["max_workers"])),
        "effective_workers": int(runtime_settings["max_workers"]),
        "tested_count": int(tested_count),
        "total_count": int(total_count),
        "elapsed_seconds": float(elapsed_seconds),
        "compute_elapsed_seconds": float(compute_elapsed_seconds),
        "transition_elapsed_seconds": float(transition_elapsed_seconds),
        "capital": int(runtime_settings["capital"]),
        "slip_per_side": float(runtime_settings["slip_per_side"]),
        "hard_filters": {key: _json_safe_value(value) for key, value in hard_filters.items()},
        "development_years": runtime_settings.get("development_years"),
        "development_start_date": runtime_settings.get("development_start_date"),
        "development_end_date": runtime_settings.get("development_end_date"),
        "holdout_start_date": runtime_settings.get("holdout_start_date"),
        "holdout_end_date": runtime_settings.get("holdout_end_date"),
        "best_result": best_row,
        "best_params": best_params,
        "best_xs_path": str(best_indicator_xs_path),
        "best_indicator_xs_path": str(best_indicator_xs_path),
        "best_trade_xs_path": str(best_trade_xs_path),
        "best_txt_path": str(best_txt_path),
        "best_strategy_xs_path": str(latest_strategy_xs_path),
        "best_param_preset_path": str(latest_param_preset_path),
        "trade_line_count": len(trade_lines),
        "artifact_error": artifact_error,
        "summary_digest": {
            "development_period": f"{runtime_settings.get('development_start_date')} ~ {runtime_settings.get('development_end_date')}",
            "holdout_period": f"{runtime_settings.get('holdout_start_date')} ~ {runtime_settings.get('holdout_end_date')}",
            "tested_count": int(tested_count),
            "total_count": int(total_count),
            "best_score": _json_safe_value(best_row.get("composite_score")),
            "best_return": _json_safe_value(best_row.get("total_return")),
            "best_mdd": _json_safe_value(best_row.get("mdd_pct")),
            "best_trades": _json_safe_value(best_row.get("n_trades")),
            "best_params_preview": " / ".join(
                f"{name}={_format_param_value(value)}" for name, value in list(best_params.items())[:6]
            ),
        },
    }
    summary_json_path.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    leaderboard_row = _leaderboard_row(
        best_row=best_row,
        best_params=best_params,
        mode_label=mode_label,
        export_dir=export_dir,
        best_xs_path=best_indicator_xs_path,
        best_txt_path=best_txt_path,
        signature=signature,
        slip_per_side=float(runtime_settings["slip_per_side"]),
        research_profile_tag=str(runtime_settings.get("research_profile_tag") or RESEARCH_PROFILE_TAG_0101),
        development_years=runtime_settings.get("development_years"),
    )
    _update_persistent_top10(leaderboard_row, params_meta)
    historical_best_updated = _update_historical_best(
        best_row=best_row,
        best_params=best_params,
        mode_label=mode_label,
        export_dir=export_dir,
        best_indicator_xs_path=best_indicator_xs_path,
        best_trade_xs_path=best_trade_xs_path,
        best_txt_path=best_txt_path,
        summary_json_path=summary_json_path,
        runtime_settings=runtime_settings,
        tested_count=tested_count,
        total_count=total_count,
        elapsed_seconds=elapsed_seconds,
        compute_elapsed_seconds=compute_elapsed_seconds,
        transition_elapsed_seconds=transition_elapsed_seconds,
    )

    latest_memory_payload = {
        "saved_at": saved_at_text,
        "run_dir": str(export_dir),
        "xs_path": xs_path,
        "m1_path": minute_path,
        "d1_path": daily_path,
        "txt_path": str(best_txt_path),
        "best_xs_path": str(best_indicator_xs_path),
        "best_indicator_xs_path": str(best_indicator_xs_path),
        "best_trade_xs_path": str(best_trade_xs_path),
        "best_txt_path": str(best_txt_path),
        "best_strategy_xs_path": str(latest_strategy_xs_path),
        "best_param_preset_path": str(latest_param_preset_path),
        "best_summary_json_path": str(summary_json_path),
        "optimization_mode": mode_label,
        "research_profile_tag": str(runtime_settings.get("research_profile_tag") or ""),
        "run_state": "completed",
        "cpu_limit_pct": int(runtime_settings.get("cpu_limit_pct", 100)),
        "memory_limit_pct": int(runtime_settings.get("memory_limit_pct", 100)),
        "requested_workers": int(runtime_settings.get("requested_workers", runtime_settings["max_workers"])),
        "effective_workers": int(runtime_settings["max_workers"]),
        "elapsed_seconds": float(elapsed_seconds),
        "compute_elapsed_seconds": float(compute_elapsed_seconds),
        "transition_elapsed_seconds": float(transition_elapsed_seconds),
        "tested_count": int(tested_count),
        "total_count": int(total_count),
        "capital": int(runtime_settings["capital"]),
        "slip_per_side": float(runtime_settings["slip_per_side"]),
        "development_years": runtime_settings.get("development_years"),
        "development_start_date": runtime_settings.get("development_start_date"),
        "development_end_date": runtime_settings.get("development_end_date"),
        "holdout_start_date": runtime_settings.get("holdout_start_date"),
        "holdout_end_date": runtime_settings.get("holdout_end_date"),
        "min_trades": int(hard_filters.get("min_trades", 0)),
        "min_total_return": float(hard_filters.get("min_total_return", 0.0)),
        "max_mdd_pct": float(hard_filters.get("max_mdd_pct", 0.0)),
        "best_result": best_row,
        "latest_total_return_pct": _safe_float(best_row.get("total_return"), 0.0),
        "latest_mdd_pct": _safe_float(best_row.get("mdd_pct"), 0.0),
        "latest_n_trades": int(round(_safe_float(best_row.get("n_trades"), 0.0))),
        "historical_best_updated": bool(historical_best_updated),
        "fail_reason_counts": {str(key): int(value) for key, value in fail_reason_counts.items()},
    }
    _write_latest_memory(latest_memory_payload)

    return {
        "saved_at": latest_memory_payload["saved_at"],
        "export_dir": str(export_dir),
        "best_xs_path": str(best_indicator_xs_path),
        "best_indicator_xs_path": str(best_indicator_xs_path),
        "best_trade_xs_path": str(best_trade_xs_path),
        "best_txt_path": str(best_txt_path),
        "best_strategy_xs_path": str(latest_strategy_xs_path),
        "best_param_preset_path": str(latest_param_preset_path),
        "summary_json_path": str(summary_json_path),
        "trade_line_count": len(trade_lines),
        "artifact_error": artifact_error,
        "params": best_params,
        "historical_best_updated": bool(historical_best_updated),
    }


def load_latest_artifact_snapshot(*, research_profile_tag: str | None = None) -> dict[str, Any]:
    latest_memory = _load_latest_memory()
    if research_profile_tag and str(latest_memory.get("research_profile_tag") or "").strip() != str(research_profile_tag).strip():
        return {}
    best_xs_path = str(latest_memory.get("best_xs_path") or "").strip()
    best_indicator_xs_path = str(latest_memory.get("best_indicator_xs_path") or best_xs_path or "").strip()
    best_trade_xs_path = str(latest_memory.get("best_trade_xs_path") or "").strip()
    best_txt_path = str(latest_memory.get("best_txt_path") or latest_memory.get("txt_path") or "").strip()
    best_strategy_xs_path = str(latest_memory.get("best_strategy_xs_path") or "").strip()
    best_param_preset_path = str(latest_memory.get("best_param_preset_path") or "").strip()
    export_dir = str(latest_memory.get("run_dir") or "").strip()
    summary_json_path = str(latest_memory.get("best_summary_json_path") or "").strip()
    if not any(
        [
            best_indicator_xs_path,
            best_trade_xs_path,
            best_txt_path,
            best_strategy_xs_path,
            best_param_preset_path,
            export_dir,
            summary_json_path,
        ]
    ):
        return {}
    return {
        "saved_at": latest_memory.get("saved_at"),
        "export_dir": export_dir,
        "best_xs_path": best_indicator_xs_path,
        "best_indicator_xs_path": best_indicator_xs_path,
        "best_trade_xs_path": best_trade_xs_path,
        "best_txt_path": best_txt_path,
        "best_strategy_xs_path": best_strategy_xs_path,
        "best_param_preset_path": best_param_preset_path,
        "summary_json_path": summary_json_path,
    }


def _normalized_param_value(value: Any) -> int | float | str:
    normalized = _json_safe_value(value)
    if isinstance(normalized, float):
        if abs(normalized - round(normalized)) < 1e-9:
            return int(round(normalized))
        return round(normalized, 10)
    return normalized if normalized is not None else ""


def _performance_signature(row: Mapping[str, Any], focus_param: str) -> tuple[Any, ...]:
    metric_keys = [
        "n_trades",
        "total_return",
        "mdd_amount",
        "mdd_pct",
        "year_avg_return",
        "year_return_std",
        "loss_years",
        "composite_score",
        "robust_score",
        "plateau_score",
        "window_avg_return",
        "window_median_return",
        "window_return_std",
        "window_loss_count",
        "worst_window_return",
        "worst_window_mdd_pct",
        "slip2_total_return",
        "slip3_total_return",
        "slip4_total_return",
        "slip_stress_score",
    ]
    yearly_keys = sorted(key for key in row.keys() if str(key).startswith("year_return_"))
    values: list[Any] = []
    for key in [*metric_keys, *yearly_keys]:
        values.append(_normalized_param_value(row.get(key)))
    values.append(("focus_param", focus_param))
    return tuple(values)


def _stage_candidate_sort_key(row: Mapping[str, Any], focus_param: str) -> tuple[Any, ...]:
    focus_value = row.get(focus_param)
    normalized_focus = _normalized_param_value(focus_value)
    return (*_score_row(dict(row)), normalized_focus)


def _representative_stage_rows(
    rows: list[dict[str, Any]],
    *,
    focus_param: str,
    keep_count: int,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    _apply_plateau_scores(rows)
    ranked_rows = sorted(rows, key=lambda row: _stage_candidate_sort_key(row, focus_param))
    selected_rows: list[dict[str, Any]] = []
    seen_signatures: set[tuple[Any, ...]] = set()
    seen_focus_values: set[Any] = set()
    for row in ranked_rows:
        focus_value = _normalized_param_value(row.get(focus_param))
        performance_signature = _performance_signature(row, focus_param)
        if performance_signature in seen_signatures or focus_value in seen_focus_values:
            continue
        seen_signatures.add(performance_signature)
        seen_focus_values.add(focus_value)
        selected_rows.append(dict(row))
        if len(selected_rows) >= keep_count:
            break
    if selected_rows:
        return selected_rows
    return [dict(ranked_rows[0])]


def _coordinate_cycle_generator(
    *,
    mode: str,
    fixed_params: dict[str, Any],
    variable_specs: list[dict[str, Any]],
    minute_path: str,
    daily_path: str,
    capital: int,
    script_name: str,
    slip_per_side: float,
    max_workers: int,
    top_n: int,
    hard_filters: dict[str, Any],
    keep_count: int,
    development_start_date: int | None = None,
    development_end_date: int | None = None,
) -> Iterable[dict[str, Any]]:
    if not variable_specs:
        single_combo = dict(fixed_params)
        started_at = time.time()
        for update in _evaluate_task_sequence_safe(
            tasks=[(single_combo, {"mode": mode, "cycle_no": 1, "round_no": 1, "stage_name": "固定參數"})],
            minute_path=minute_path,
            daily_path=daily_path,
            capital=capital,
            script_name=script_name,
            slip_per_side=slip_per_side,
            max_workers=max_workers,
            top_n=top_n,
            hard_filters=hard_filters,
            total_planned=1,
            initial_started_at=started_at,
            development_start_date=development_start_date,
            development_end_date=development_end_date,
        ):
            update["compute_elapsed"] = float(update.get("elapsed", 0.0))
            update["transition_elapsed"] = 0.0
            update["summary_lines"] = ["目前沒有勾選可變參數，本輪只會驗證一組固定參數。"]
            update["step_note"] = "固定參數驗證中"
            yield update
        return

    keep_count = max(1, int(keep_count or 3))
    started_at = time.time()
    done = 0
    passed = 0
    planned_total = 0
    cycle_no = 0
    stage_no = 0
    compute_elapsed_total = 0.0
    transition_elapsed_total = 0.0
    accepted_rows: list[dict[str, Any]] = []
    recent_rows: list[dict[str, Any]] = []
    fail_counts: dict[str, int] = {}
    current_combo = dict(fixed_params)
    for spec in variable_specs:
        current_combo.setdefault(spec["name"], spec.get("default", spec["values"][0]))

    previous_cycle_profiles: dict[str, tuple[Any, ...]] | None = None

    while True:
        cycle_no += 1
        cycle_profiles: dict[str, tuple[Any, ...]] = {}
        cycle_done_start = done
        cycle_passed_start = passed

        for param_idx, spec in enumerate(variable_specs, start=1):
            stage_no += 1
            focus_param = str(spec["name"])
            previous_focus_values = list(previous_cycle_profiles.get(focus_param, ())) if previous_cycle_profiles else []
            tasks: list[tuple[dict[str, Any], dict[str, Any]]] = []
            base_combo = dict(current_combo)
            for value in spec["values"]:
                combo = dict(base_combo)
                combo[focus_param] = value
                tasks.append(
                    (
                        combo,
                        {
                            "mode": mode,
                            "cycle_no": cycle_no,
                            "round_no": stage_no,
                            "stage_name": focus_param,
                            "param_index": param_idx,
                            "param_total": len(variable_specs),
                            "candidate_value": value,
                            "keep_count": keep_count,
                        },
                    )
                )

            if not tasks:
                cycle_profiles[focus_param] = (current_combo.get(focus_param),)
                continue

            planned_total += len(tasks)
            stage_rows: list[dict[str, Any]] = []
            stage_compute_started = time.time()

            for update in _evaluate_task_sequence_safe(
                tasks=tasks,
                minute_path=minute_path,
                daily_path=daily_path,
                capital=capital,
                script_name=script_name,
                slip_per_side=slip_per_side,
                max_workers=max_workers,
                top_n=top_n,
                hard_filters=hard_filters,
                initial_done=done,
                initial_passed=passed,
                total_planned=planned_total,
                initial_started_at=started_at,
                accepted_seed_rows=accepted_rows,
                recent_trial_rows=recent_rows,
                fail_reason_counts=fail_counts,
                development_start_date=development_start_date,
                development_end_date=development_end_date,
            ):
                done = int(update.get("done") or 0)
                passed = int(update.get("passed") or 0)
                accepted_rows = list(update.get("accepted_rows") or accepted_rows)
                recent_trials_df = update.get("recent_trials_df")
                if isinstance(recent_trials_df, pd.DataFrame):
                    recent_rows = list(recent_trials_df.to_dict("records"))
                fail_counts = dict(update.get("fail_reason_counts") or fail_counts)
                row = update.get("row")
                if row is not None and "error" not in row:
                    stage_rows.append(dict(row))

                update["compute_elapsed"] = compute_elapsed_total + max(time.time() - stage_compute_started, 0.0)
                update["transition_elapsed"] = transition_elapsed_total
                update["summary_lines"] = [
                    f"第 {cycle_no} 輪，第 {param_idx}/{len(variable_specs)} 個參數 {focus_param} 掃描中。",
                    f"目前只展開 {focus_param}，其他已勾選參數都固定在目前最佳值。",
                    f"本段共 {len(tasks):,} 組；結束後只記住 {focus_param} 的前 {keep_count} 個代表值，並沿用第 1 名繼續。",
                ]
                update["step_note"] = f"第 {cycle_no} 輪掃描 {focus_param}"
                update["stage_name"] = focus_param
                update["cycle_no"] = cycle_no
                update["round_no"] = stage_no
                yield update

            compute_elapsed_total += max(time.time() - stage_compute_started, 0.0)

            transition_started = time.time()
            evaluation_filters = _base_hard_filters(hard_filters)
            ranked_source_rows = [row for row in stage_rows if _passes_hard_filters(row, evaluation_filters)] or stage_rows
            stage_pass_count = sum(1 for row in stage_rows if _passes_hard_filters(row, evaluation_filters))
            representative_rows = _representative_stage_rows(
                ranked_source_rows,
                focus_param=focus_param,
                keep_count=keep_count,
            )
            remembered_values = [_normalized_param_value(row.get(focus_param)) for row in representative_rows]
            if not remembered_values:
                remembered_values = [_normalized_param_value(current_combo.get(focus_param))]
            cycle_profiles[focus_param] = tuple(remembered_values)
            added_values = [value for value in remembered_values if value not in previous_focus_values]
            removed_values = [value for value in previous_focus_values if value not in remembered_values]
            current_combo[focus_param] = (
                representative_rows[0].get(focus_param, current_combo.get(focus_param))
                if representative_rows
                else current_combo.get(focus_param)
            )
            transition_elapsed_total += max(time.time() - transition_started, 0.0)
            remembered_text = " / ".join(str(value) for value in remembered_values)
            best_stage_row = representative_rows[0] if representative_rows else (ranked_source_rows[0] if ranked_source_rows else {})
            yield {
                "done": done,
                "passed": passed,
                "total": max(done, planned_total),
                "elapsed": time.time() - started_at,
                "compute_elapsed": compute_elapsed_total,
                "transition_elapsed": transition_elapsed_total,
                "eta": 0.0,
                "top_df": _sort_results_df(pd.DataFrame(accepted_rows)) if accepted_rows else pd.DataFrame(columns=RESULT_COLUMNS),
                "row": None,
                "meta": None,
                "accepted_rows": list(accepted_rows),
                "recent_trials_df": pd.DataFrame(recent_rows) if recent_rows else pd.DataFrame(),
                "fail_reason_counts": dict(fail_counts),
                "summary_lines": [
                    f"第 {cycle_no} 輪，第 {param_idx}/{len(variable_specs)} 個參數 {focus_param} 已完成；本段共跑 {len(tasks):,} 組，通過硬條件 {stage_pass_count:,} 組。",
                    f"記住的前 {len(remembered_values)} 個代表值：{remembered_text}；相較上一輪新增 {len(added_values)} 個、淘汰 {len(removed_values)} 個。",
                    f"本段第 1 名為 {focus_param}={remembered_values[0]}，總報酬 {_safe_float(best_stage_row.get('total_return'), 0.0):.2f}% / MDD {_safe_float(best_stage_row.get('mdd_pct'), 0.0):.2f}% / 交易數 {int(round(_safe_float(best_stage_row.get('n_trades'), 0.0)))}。",
                ],
                "step_note": f"第 {cycle_no} 輪完成 {focus_param}",
                "stage_name": focus_param,
                "cycle_no": cycle_no,
                "round_no": stage_no,
                "current_param_top_values": list(remembered_values),
            }

        stop_reason = ""
        if previous_cycle_profiles is not None and cycle_profiles == previous_cycle_profiles:
            stop_reason = f"本輪跑完後，所有已勾選參數的前 {keep_count} 名代表值都和上一輪相同，停止。"
        changed_param_count = 0 if previous_cycle_profiles is None else sum(
            1 for key, value in cycle_profiles.items() if tuple(previous_cycle_profiles.get(key, ())) != tuple(value)
        )
        previous_cycle_profiles = dict(cycle_profiles)

        yield {
            "done": done,
            "passed": passed,
            "total": max(done, planned_total),
            "elapsed": time.time() - started_at,
            "compute_elapsed": compute_elapsed_total,
            "transition_elapsed": transition_elapsed_total,
            "eta": 0.0,
            "top_df": _sort_results_df(pd.DataFrame(accepted_rows)) if accepted_rows else pd.DataFrame(columns=RESULT_COLUMNS),
            "row": None,
            "meta": None,
            "accepted_rows": list(accepted_rows),
            "recent_trials_df": pd.DataFrame(recent_rows) if recent_rows else pd.DataFrame(),
            "fail_reason_counts": dict(fail_counts),
            "summary_lines": [
                f"第 {cycle_no} 輪已完成，本輪新增測試 {done - cycle_done_start:,} 組，其中通過硬條件 {passed - cycle_passed_start:,} 組。",
                f"本輪共有 {changed_param_count} 個參數的保留名單與上一輪不同。",
                stop_reason or "下一輪會從目前最佳組合重新開始，再逐參數各掃一次。",
            ],
            "step_note": f"第 {cycle_no} 輪循環完成",
            "cycle_no": cycle_no,
            "stop_reason": stop_reason,
            "cycle_profiles": {key: list(value) for key, value in cycle_profiles.items()},
        }

        if stop_reason:
            break


def _streamlit_update_stride(*, max_workers: int, total: int) -> int:
    worker_stride = max(_STREAMLIT_UPDATE_MIN_STRIDE, max(1, int(max_workers)) * 4)
    if total <= 0:
        return min(worker_stride, _STREAMLIT_UPDATE_MAX_STRIDE)
    total_stride = max(_STREAMLIT_UPDATE_MIN_STRIDE, total // 150)
    return min(max(worker_stride, total_stride), _STREAMLIT_UPDATE_MAX_STRIDE)


def _evaluate_task_sequence_safe(
    *,
    tasks: list[tuple[dict[str, Any], Any]],
    minute_path: str | None,
    daily_path: str | None,
    capital: int,
    script_name: str,
    slip_per_side: float,
    max_workers: int,
    top_n: int,
    hard_filters: dict[str, Any],
    initial_done: int = 0,
    initial_passed: int = 0,
    total_planned: int | None = None,
    initial_started_at: float | None = None,
    accepted_seed_rows: list[dict[str, Any]] | None = None,
    recent_trial_rows: list[dict[str, Any]] | None = None,
    fail_reason_counts: dict[str, int] | None = None,
    development_start_date: int | None = None,
    development_end_date: int | None = None,
) -> Iterable[dict[str, Any]]:
    evaluation_filters = _base_hard_filters(hard_filters)
    retention_top_n = _mdd_sweep_retention_limit(top_n, hard_filters)
    if int(max_workers) > 1:
        for update in _evaluate_task_sequence(
            tasks=tasks,
            minute_path=minute_path,
            daily_path=daily_path,
            capital=capital,
            script_name=script_name,
            slip_per_side=slip_per_side,
            max_workers=max_workers,
            top_n=retention_top_n,
            hard_filters=evaluation_filters,
            initial_done=initial_done,
            initial_passed=initial_passed,
            total_planned=total_planned,
            initial_started_at=initial_started_at,
            accepted_seed_rows=accepted_seed_rows,
            recent_trial_rows=recent_trial_rows,
            fail_reason_counts=fail_reason_counts,
            start_date=development_start_date,
            end_date=development_end_date,
        ):
            yield _postprocess_mdd_sweep_update(update, top_n=top_n, hard_filters=hard_filters)
        return

    total = total_planned if total_planned is not None else len(tasks)
    if total == 0:
        yield {"done": 0, "passed": 0, "total": 0, "elapsed": 0.0, "eta": 0.0, "top_df": pd.DataFrame(), "row": None, "meta": None}
        return

    if not minute_path or not daily_path:
        raise ValueError("missing M1 / D1 data paths")

    _init_worker_data(
        minute_path,
        daily_path,
        capital,
        script_name,
        slip_per_side,
        development_start_date,
        development_end_date,
    )
    started_at = time.time() if initial_started_at is None else initial_started_at
    done = initial_done
    passed = initial_passed
    accepted_rows: list[dict[str, Any]] = list(accepted_seed_rows or [])
    recent_rows: list[dict[str, Any]] = list(recent_trial_rows or [])
    fail_counts: dict[str, int] = dict(fail_reason_counts or {})

    for combo, meta in tasks:
        try:
            row = _run_single_combo(combo)
        except Exception as exc:
            row = {"error": str(exc), "n_trades": 0, "total_return": -1e18, "mdd_amount": 1e18, "mdd_pct": 1e18}

        done += 1
        latest_fail_reasons: list[str] = []
        if "error" not in row:
            latest_fail_reasons = _hard_filter_fail_reasons(row, evaluation_filters)
            if not latest_fail_reasons:
                row = _annotate_mdd_sweep_row(row, hard_filters)
                accepted_rows.append(row)
                _apply_plateau_scores(accepted_rows)
                accepted_rows = _trim_accepted_rows_for_mdd_sweep(
                    accepted_rows,
                    top_n=top_n,
                    hard_filters=hard_filters,
                )
                passed += 1
            else:
                for reason in latest_fail_reasons:
                    fail_counts[reason] = fail_counts.get(reason, 0) + 1
        else:
            latest_fail_reasons = [f"執行失敗: {row['error']}"]
            fail_counts[latest_fail_reasons[0]] = fail_counts.get(latest_fail_reasons[0], 0) + 1

        trial_row: dict[str, Any] = {}
        row_for_trial = _annotate_mdd_sweep_row(row, hard_filters) if "error" not in row else row
        if meta is not None and isinstance(meta, dict):
            trial_row.update({k: v for k, v in meta.items() if k not in {"summary_lines"}})
        trial_row.update(
            {
                "status": "通過" if not latest_fail_reasons else "淘汰",
                "reason": "、".join(latest_fail_reasons) if latest_fail_reasons else "通過硬條件",
                "n_trades": int(row_for_trial.get("n_trades", 0)),
                "total_return": float(row_for_trial.get("total_return", 0.0)),
                "mdd_pct": float(row_for_trial.get("mdd_pct", 0.0)),
            }
        )
        for key, value in row_for_trial.items():
            if key not in trial_row and key != "error":
                trial_row[key] = value
        recent_rows.append(trial_row)
        recent_rows = recent_rows[-30:]

        elapsed = time.time() - started_at
        avg = elapsed / done if done > 0 else 0.0
        eta = avg * max(total - done, 0)
        top_df = pd.DataFrame(accepted_rows) if accepted_rows else pd.DataFrame(columns=RESULT_COLUMNS)
        top_df = _sort_results_df(top_df)
        yield {
            "done": done,
            "passed": passed,
            "total": total,
            "elapsed": elapsed,
            "eta": eta,
            "top_df": top_df,
            "row": row,
            "meta": meta,
            "accepted_rows": list(accepted_rows),
            "recent_trials_df": _build_recent_trials_df(recent_rows),
            "fail_reason_counts": dict(fail_counts),
            "latest_fail_reasons": list(latest_fail_reasons),
        }


def _throttled_optimizer_updates(
    iterator: Iterable[dict[str, Any]],
    *,
    max_workers: int,
) -> Iterable[dict[str, Any]]:
    pending_update: dict[str, Any] | None = None
    last_yield_done = -1
    last_yield_at = 0.0

    for update in iterator:
        pending_update = update
        done = int(update.get("done") or 0)
        total = int(update.get("total") or 0)
        now = time.monotonic()
        stride = _streamlit_update_stride(max_workers=max_workers, total=total)

        should_yield = last_yield_done < 0
        if total > 0 and done >= total:
            should_yield = True
        if done > max(last_yield_done, 0) and (done - max(last_yield_done, 0)) >= stride:
            should_yield = True
        if (now - last_yield_at) >= _STREAMLIT_UPDATE_MIN_INTERVAL_SECONDS:
            should_yield = True

        if should_yield:
            yield update
            last_yield_done = done
            last_yield_at = now
            pending_update = None

    if pending_update is not None:
        yield pending_update


def run_optimizer(
    *,
    mode: str,
    ui_param_specs: list[dict[str, Any]],
    params_meta: list[dict[str, Any]],
    runtime_settings: dict[str, Any],
    hard_filters: dict[str, Any],
    minute_path: str,
    daily_path: str,
    script_name: str,
) -> Iterable[dict[str, Any]]:
    max_workers = int(runtime_settings["max_workers"])
    development_start_date = runtime_settings.get("development_start_date")
    development_end_date = runtime_settings.get("development_end_date")
    apply_cpu_guard(cpu_limit_pct=int(runtime_settings.get("cpu_limit_pct", 100)))
    try:
        fixed_params, variable_specs = build_search_space_from_ui(ui_param_specs, params_meta)
        iterator = _coordinate_cycle_generator(
            mode=mode,
            fixed_params=fixed_params,
            variable_specs=variable_specs,
            minute_path=minute_path,
            daily_path=daily_path,
            capital=int(runtime_settings["capital"]),
            script_name=script_name,
            slip_per_side=float(runtime_settings["slip_per_side"]),
            max_workers=max_workers,
            top_n=int(runtime_settings["top_n"]),
            hard_filters=hard_filters,
            keep_count=int(runtime_settings["seed_keep_count"]),
            development_start_date=int(development_start_date) if development_start_date not in (None, "") else None,
            development_end_date=int(development_end_date) if development_end_date not in (None, "") else None,
        )

        for update in _throttled_optimizer_updates(iterator, max_workers=max_workers):
            yield update
    finally:
        shutdown_cached_worker_executor(wait=False, cancel_futures=False)


def build_0101_wfo_windows(
    *,
    start_date: int,
    end_date: int,
    train_years: int,
    test_years: int,
    step_years: int,
    gap_days: int = 0,
) -> list[dict[str, Any]]:
    train_months = max(1, int(train_years)) * 12
    test_months = max(1, int(test_years)) * 12
    step_months = max(1, int(step_years)) * 12
    start_obj = _date_to_obj(start_date)
    end_obj = _date_to_obj(end_date)
    train_start = start_obj
    windows: list[dict[str, Any]] = []
    index = 1

    while train_start <= end_obj:
        train_end = _shift_months(train_start, train_months) - timedelta(days=1)
        gap_start = train_end + timedelta(days=1)
        gap_end = train_end + timedelta(days=max(0, int(gap_days)))
        test_start = train_end + timedelta(days=max(0, int(gap_days)) + 1)
        test_end = _shift_months(test_start, test_months) - timedelta(days=1)
        if test_start > end_obj or test_end > end_obj:
            break

        windows.append(
            {
                "index": index,
                "label": f"WFO-{index:02d}",
                "train_start_date": _obj_to_date_int(train_start),
                "train_end_date": _obj_to_date_int(train_end),
                "gap_days": max(0, int(gap_days)),
                "gap_start_date": _obj_to_date_int(gap_start) if int(gap_days) > 0 else None,
                "gap_end_date": _obj_to_date_int(gap_end) if int(gap_days) > 0 else None,
                "test_start_date": _obj_to_date_int(test_start),
                "test_end_date": _obj_to_date_int(test_end),
                "train_label": f"{train_start.isoformat()} ~ {train_end.isoformat()}",
                "gap_label": f"{gap_start.isoformat()} ~ {gap_end.isoformat()}" if int(gap_days) > 0 else "無",
                "test_label": f"{test_start.isoformat()} ~ {test_end.isoformat()}",
            }
        )

        next_train_start = _shift_months(train_start, step_months)
        if next_train_start <= train_start:
            break
        train_start = next_train_start
        index += 1

    return windows


def _params_preview(params: Mapping[str, Any], *, limit: int = 6) -> str:
    if not params:
        return "--"
    parts = [f"{name}={_format_param_value(value)}" for name, value in list(params.items())[:limit]]
    suffix = "" if len(params) <= limit else " ..."
    return " / ".join(parts) + suffix


def _evaluate_wfo_candidate_oos(
    *,
    params: Mapping[str, Any],
    minute_bars: list[Any],
    daily_bars: list[Any],
    script_name: str,
    capital: int,
    slip_per_side: float,
    backtest_start_date: int,
    test_start_date: int,
    test_end_date: int,
) -> dict[str, Any]:
    eval_minute_bars, eval_daily_bars = _filter_bars_for_period(
        minute_bars,
        daily_bars,
        start_date=int(backtest_start_date),
        end_date=int(test_end_date),
    )
    _test_minute_bars, test_daily_bars = _filter_bars_for_period(
        minute_bars,
        daily_bars,
        start_date=int(test_start_date),
        end_date=int(test_end_date),
    )
    result = run_0313plus_backtest(
        eval_minute_bars,
        eval_daily_bars,
        dict(params),
        script_name,
        slip_per_side=float(slip_per_side),
    )
    trades = [
        trade
        for trade in list(getattr(result, "trades", []) or [])
        if int(test_start_date) <= int(getattr(trade, "exit_date", 0) or 0) <= int(test_end_date)
    ]
    report = build_report(SimpleNamespace(trades=trades), test_daily_bars, capital)
    trade_metrics = _holdout_trade_metrics(trades)
    return {
        "oos_net_profit": float(trade_metrics["net_profit"]),
        "oos_profit_factor": float(trade_metrics["profit_factor"]),
        "oos_sharpe": _trade_sharpe(trades),
        "oos_return_pct": float(report.get("total_return", 0.0)) * 100.0,
        "oos_mdd_pct": float(report.get("mdd_pct", 0.0)) * 100.0,
        "oos_trade_count": int(trade_metrics["trade_count"]),
    }


def _wfo_candidate_passes(oos_metrics: Mapping[str, Any], hard_filters: Mapping[str, Any]) -> bool:
    min_trades = max(1, int(hard_filters.get("min_trades", 0) or 0))
    min_total_return = float(hard_filters.get("min_total_return", 0.0) or 0.0)
    max_mdd_pct = float(hard_filters.get("max_mdd_pct", 1e18) or 1e18)
    return (
        int(oos_metrics.get("oos_trade_count", 0) or 0) >= min_trades
        and float(oos_metrics.get("oos_return_pct", 0.0) or 0.0) >= min_total_return
        and float(oos_metrics.get("oos_mdd_pct", 0.0) or 0.0) <= max_mdd_pct
    )


def run_0101_multi_wfo_validation(
    *,
    mode: str,
    ui_param_specs: list[dict[str, Any]],
    params_meta: list[dict[str, Any]],
    runtime_settings: dict[str, Any],
    hard_filters: dict[str, Any],
    minute_path: str,
    daily_path: str,
    script_name: str,
    start_date: int,
    end_date: int,
    train_years: int,
    test_years: int,
    step_years: int,
    gap_days: int = 0,
) -> dict[str, Any]:
    windows = build_0101_wfo_windows(
        start_date=int(start_date),
        end_date=int(end_date),
        train_years=int(train_years),
        test_years=int(test_years),
        step_years=int(step_years),
        gap_days=int(gap_days),
    )
    top_n = max(1, int(runtime_settings.get("top_n", 3) or 3))
    minute_bars, daily_bars = load_market_data(minute_path, daily_path)

    folds: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []

    for window in windows:
        fold_runtime_settings = dict(runtime_settings)
        fold_runtime_settings["development_start_date"] = int(window["train_start_date"])
        fold_runtime_settings["development_end_date"] = int(window["train_end_date"])
        fold_runtime_settings["research_profile_tag"] = (
            f"{runtime_settings.get('research_profile_tag') or RESEARCH_PROFILE_TAG_0101}_wfo_{int(window['index']):02d}"
        )

        final_update: dict[str, Any] = {}
        for update in run_optimizer(
            mode=mode,
            ui_param_specs=ui_param_specs,
            params_meta=params_meta,
            runtime_settings=fold_runtime_settings,
            hard_filters=hard_filters,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
        ):
            final_update = dict(update)

        top_df = final_update.get("top_df")
        if not isinstance(top_df, pd.DataFrame) or top_df.empty:
            fold_row = {
                "輪次": str(window["label"]),
                "訓練期間": str(window["train_label"]),
                "Gap": str(window.get("gap_label") or "無"),
                "測試期間": str(window["test_label"]),
                "最佳候選": "--",
                "前 N 名候選": 0,
                "OOS 報酬(%)": 0.0,
                "OOS 最大回撤(%)": 0.0,
                "OOS 交易數": 0,
                "OOS Sharpe": 0.0,
                "是否通過最低門檻": "否",
            }
            fold_rows.append(fold_row)
            folds.append({**window, "fold_row": fold_row, "top_candidates": [], "summary_lines": ["訓練窗沒有產生通過硬條件的候選。"]})
            continue

        top_rows = [
            {key: _json_safe_value(value) for key, value in row.items()}
            for row in top_df.head(top_n).to_dict("records")
        ]
        candidate_rows: list[dict[str, Any]] = []
        for rank, row in enumerate(top_rows, start=1):
            params = _best_params_from_row(row, params_meta)
            if not params:
                continue
            oos_metrics = _evaluate_wfo_candidate_oos(
                params=params,
                minute_bars=minute_bars,
                daily_bars=daily_bars,
                script_name=script_name,
                capital=int(runtime_settings["capital"]),
                slip_per_side=float(runtime_settings["slip_per_side"]),
                backtest_start_date=int(window["train_start_date"]),
                test_start_date=int(window["test_start_date"]),
                test_end_date=int(window["test_end_date"]),
            )
            passed = _wfo_candidate_passes(oos_metrics, hard_filters)
            candidate_rows.append(
                {
                    "排名": rank,
                    "參數摘要": _params_preview(params),
                    "params": {key: _json_safe_value(value) for key, value in params.items()},
                    "train_total_return_pct": _safe_float(row.get("total_return"), 0.0),
                    "train_mdd_pct": _safe_float(row.get("mdd_pct"), 0.0),
                    "train_trade_count": int(round(_safe_float(row.get("n_trades"), 0.0))),
                    "train_robust_score": _safe_float(row.get("robust_score", row.get("composite_score")), 0.0),
                    **oos_metrics,
                    "passed": bool(passed),
                    "是否通過最低門檻": "是" if passed else "否",
                }
            )

        best_candidate = candidate_rows[0] if candidate_rows else {}
        best_passed = bool(best_candidate.get("passed"))
        fold_row = {
            "輪次": str(window["label"]),
            "訓練期間": str(window["train_label"]),
            "Gap": str(window.get("gap_label") or "無"),
            "測試期間": str(window["test_label"]),
            "最佳候選": str(best_candidate.get("參數摘要") or "--"),
            "前 N 名候選": len(candidate_rows),
            "OOS 報酬(%)": float(best_candidate.get("oos_return_pct", 0.0) or 0.0),
            "OOS 最大回撤(%)": float(best_candidate.get("oos_mdd_pct", 0.0) or 0.0),
            "OOS 交易數": int(best_candidate.get("oos_trade_count", 0) or 0),
            "OOS Sharpe": float(best_candidate.get("oos_sharpe", 0.0) or 0.0),
            "是否通過最低門檻": "是" if best_passed else "否",
        }
        fold_rows.append(fold_row)
        folds.append(
            {
                **window,
                "fold_row": fold_row,
                "top_candidates": candidate_rows,
                "summary_lines": [
                    f"{window['label']} 訓練窗保留 {len(candidate_rows)} 組候選。",
                    (
                        f"第 1 名 OOS 報酬 {float(best_candidate.get('oos_return_pct', 0.0) or 0.0):.2f}% / "
                        f"MDD {float(best_candidate.get('oos_mdd_pct', 0.0) or 0.0):.2f}% / "
                        f"交易 {int(best_candidate.get('oos_trade_count', 0) or 0):,} 筆。"
                    ),
                ],
            }
        )

    passed_rows = [row for row in fold_rows if str(row.get("是否通過最低門檻")) == "是"]
    failed_rows = [row for row in fold_rows if str(row.get("是否通過最低門檻")) != "是"]
    oos_returns = [float(row.get("OOS 報酬(%)", 0.0) or 0.0) for row in fold_rows]
    oos_mdds = [float(row.get("OOS 最大回撤(%)", 0.0) or 0.0) for row in fold_rows]
    oos_sharpes = [float(row.get("OOS Sharpe", 0.0) or 0.0) for row in fold_rows]
    worst_row = min(fold_rows, key=lambda row: float(row.get("OOS 報酬(%)", 0.0) or 0.0), default={})
    worst_mdd_row = max(fold_rows, key=lambda row: float(row.get("OOS 最大回撤(%)", 0.0) or 0.0), default={})
    pass_rate = (len(passed_rows) / len(fold_rows)) if fold_rows else 0.0
    avg_return = float(sum(oos_returns) / len(oos_returns)) if oos_returns else 0.0
    avg_mdd = float(sum(oos_mdds) / len(oos_mdds)) if oos_mdds else 0.0
    return_std = float(pd.Series(oos_returns).std()) if len(oos_returns) > 1 else 0.0
    oos_stability_score = max(0.0, min(100.0, pass_rate * 55.0 + max(avg_return, 0.0) * 0.8 - avg_mdd * 0.8 - return_std * 0.6))
    summary = {
        "total_folds": len(fold_rows),
        "passed_folds": len(passed_rows),
        "failed_folds": len(failed_rows),
        "avg_oos_return_pct": avg_return,
        "avg_oos_mdd_pct": avg_mdd,
        "avg_oos_sharpe": float(sum(oos_sharpes) / len(oos_sharpes)) if oos_sharpes else 0.0,
        "worst_fold": str(worst_row.get("輪次") or "--"),
        "worst_fold_return_pct": float(worst_row.get("OOS 報酬(%)", 0.0) or 0.0),
        "worst_fold_mdd": str(worst_mdd_row.get("輪次") or "--"),
        "worst_fold_mdd_pct": float(worst_mdd_row.get("OOS 最大回撤(%)", 0.0) or 0.0),
        "pass_rate": float(pass_rate),
        "oos_stability_score": float(oos_stability_score),
    }
    return {
        "settings": {
            "train_years": int(train_years),
            "test_years": int(test_years),
            "step_years": int(step_years),
            "gap_days": int(gap_days),
            "top_n": int(top_n),
            "start_date": int(start_date),
            "end_date": int(end_date),
        },
        "summary": summary,
        "fold_rows": fold_rows,
        "folds": folds,
        "summary_lines": [
            (
                f"多輪 WFO 共產生 {int(summary['total_folds'])} 輪，"
                f"通過 {int(summary['passed_folds'])} 輪、失敗 {int(summary['failed_folds'])} 輪。"
            ),
            (
                f"平均 OOS 報酬 {float(summary['avg_oos_return_pct']):.2f}% / "
                f"平均 OOS MDD {float(summary['avg_oos_mdd_pct']):.2f}% / "
                f"最差一輪 {summary['worst_fold']} 為 {float(summary['worst_fold_return_pct']):.2f}% / "
                f"穩定度 {float(summary['oos_stability_score']):.1f}。"
            ),
        ],
    }


FORWARD_TEST_LOG_COLUMNS = [
    "決策日期",
    "Baseline 名稱",
    "Challenger 名稱",
    "Challenger 來源輪次",
    "Challenger 排名",
    "選用理由",
    "單次 OOS 結果",
    "多輪 WFO 結果摘要",
    "PBO",
    "DSR",
    "基準滑價假設",
    "本週或下週實際表現",
    "是否通過前測",
    "是否升格正式策略",
]


def ensure_forward_test_log() -> Path:
    _FORWARD_TEST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not _FORWARD_TEST_LOG_PATH.exists():
        with _FORWARD_TEST_LOG_PATH.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=FORWARD_TEST_LOG_COLUMNS)
            writer.writeheader()
    return _FORWARD_TEST_LOG_PATH


def append_forward_test_log(row: Mapping[str, Any]) -> dict[str, Any]:
    target_path = ensure_forward_test_log()
    clean_row = {column: str(row.get(column, "")) for column in FORWARD_TEST_LOG_COLUMNS}
    if not clean_row["決策日期"]:
        clean_row["決策日期"] = datetime.now().isoformat(timespec="seconds")
    with target_path.open("a", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FORWARD_TEST_LOG_COLUMNS)
        writer.writerow(clean_row)
    return {"path": str(target_path), "row": clean_row}


def load_forward_test_log(limit: int | None = None) -> list[dict[str, str]]:
    target_path = ensure_forward_test_log()
    with target_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows = [{str(key): str(value or "") for key, value in row.items()} for row in reader]
    if limit is not None and int(limit) > 0:
        return rows[-int(limit) :]
    return rows


def load_latest_forward_test_entry() -> dict[str, str]:
    rows = load_forward_test_log(limit=1)
    return dict(rows[-1]) if rows else {}


def dataframe_to_csv_bytes(df: pd.DataFrame) -> bytes:
    if df.empty:
        return b""
    buffer = BytesIO()
    df.to_csv(buffer, index=False, encoding="utf-8-sig")
    return buffer.getvalue()
