from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

from src.backtest.report import build_kpi_snapshot, build_report
from src.optimize.gui_backend import load_market_data
from src.research.param_space import PERSISTENT_TOP10_JSON
from src.strategy.strategy_0313plus import run_0313plus_backtest

from . import ui_runtime_0101 as base
from .config import default_hard_filters, default_paths, default_runtime_settings
from .job_store import create_job_request, is_terminal_status, launch_job_process, read_job_state, request_stop, touch_job_heartbeat
from .parameters import load_strategy_metadata
from .services import (
    RESEARCH_PROFILE_TAG_0101,
    _best_params_from_row,
    _filter_bars_for_period,
    append_forward_test_log,
    build_best_snapshot,
    build_mdd_sweep_summary,
    collect_system_snapshot,
    estimate_run_count,
    grid_run_block_reason,
    load_historical_best_snapshot,
    load_latest_artifact_snapshot,
    load_latest_forward_test_entry,
    resolve_0101_research_periods,
    resolve_effective_workers,
    run_0101_multi_wfo_validation,
    run_0101_development_qualification,
    run_0101_holdout_validation,
)


ANALYSIS_SIGNATURE_KEY = "mq0101_v2_analysis_signature"
SHOW_HOLDOUT_KEY = "mq0101_v2_show_holdout"
XS_PATH_INPUT_KEY = "mq0101_v2_xs_path"
M1_PATH_INPUT_KEY = "mq0101_v2_m1_path"
D1_PATH_INPUT_KEY = "mq0101_v2_d1_path"
PRESET_PATH_INPUT_KEY = "mq0101_v2_preset_path"
LATEST_INPUT_SYNC_KEY = "mq0101_v2_latest_input_sync"
PARAM_DEFAULT_SIGNATURE_KEY = "mq0101_v2_param_default_signature"
WFO_RESULT_SESSION_KEY = "mq0101_v2_wfo_result"
WFO_SIGNATURE_SESSION_KEY = "mq0101_v2_wfo_signature"
AUTO_RUN_WFO_KEY = "mq0101_v2_auto_run_wfo"
FIXED_PARAM_SPECS: dict[str, dict[str, int | float | bool]] = {
    "SysHistDBars": {"enabled": False, "default": 600, "start": 600, "stop": 600, "step": 600},
    "SysHistMBars": {"enabled": False, "default": 20000, "start": 20000, "stop": 20000, "step": 20000},
}
REPORT_CHART_BG = "#ffffff"
REPORT_CHART_GRID = "#d7dbe2"
REPORT_CHART_AXIS = "#374151"
REPORT_CHART_TEXT = "#111827"
REPORT_CHART_BORDER = "#cfd6df"
REPORT_CHART_ACTUAL = "#111827"
REPORT_CHART_THEORY = "#9ca3af"
REPORT_CHART_PROFIT = "#d84a36"
REPORT_CHART_LOSS = "#5b9b45"
REPORT_CHART_BOUNDARY = "#c89211"
REPORT_NAV_CHART_HEIGHT = 500
REPORT_WEEKLY_CHART_HEIGHT = 340

CORE_COMPARE_METRICS: list[tuple[str, str]] = [
    ("總淨利 Net Profit", "淨利"),
    ("總報酬率 Total Return", "總報酬率"),
    ("最大回撤率 Max Drawdown %", "MDD"),
    ("獲利因子 Profit Factor", "PF"),
    ("勝率 Hit Rate", "勝率"),
    ("交易筆數 #Trades", "交易筆數"),
    ("平均單筆損益 Avg Trade PnL", "平均每筆"),
    ("Sharpe Ratio（交易級）", "Sharpe"),
    ("Calmar Ratio", "Calmar"),
    ("最差單週損益 Worst Week PnL", "最差單週"),
]

VERDICT_DISPLAY = {
    "PASS": "通過",
    "WATCH": "觀察",
    "REJECT": "淘汰",
}

TEXT_REPLACEMENTS: list[tuple[str, str]] = [
    ("Final Verdict", "最終結論"),
    ("Walk-Forward", "逐窗驗證"),
    ("Regime", "市場狀態"),
    ("Train", "訓練"),
    ("Test", "驗證"),
    ("Bucket", "分桶"),
    ("Verdict", "結論"),
    ("workers", "背景程序"),
    ("worker", "背景程序"),
    ("gate", "關卡"),
    ("Gate", "關卡"),
    ("PASS", "通過"),
    ("WATCH", "觀察"),
    ("REJECT", "淘汰"),
]


def _date_from_int(date_value: int) -> date:
    text = f"{int(date_value):08d}"
    return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))


def _date_label(date_value: int) -> str:
    text = f"{int(date_value):08d}"
    return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"


def _datetime_label(date_value: int, time_value: int) -> str:
    return f"{_date_label(date_value)} {int(time_value):06d}"


def _shift_back_one_year(boundary: date) -> date:
    target_year = boundary.year - 1
    for day in range(boundary.day, 0, -1):
        try:
            return boundary.replace(year=target_year, day=day)
        except ValueError:
            continue
    return boundary.replace(year=target_year, day=28)


def _backward_year_windows(*, start_date: int, end_date: int) -> list[dict[str, Any]]:
    start_obj = _date_from_int(start_date)
    current_end = _date_from_int(end_date)
    windows: list[dict[str, Any]] = []

    while current_end >= start_obj:
        one_year_back = _shift_back_one_year(current_end)
        window_start = one_year_back + timedelta(days=1)
        is_partial = window_start < start_obj
        if is_partial:
            window_start = start_obj

        windows.append(
            {
                "start_date": int(window_start.strftime("%Y%m%d")),
                "end_date": int(current_end.strftime("%Y%m%d")),
                "label": f"{window_start.isoformat()} ~ {current_end.isoformat()}",
                "is_partial": bool(is_partial),
            }
        )

        current_end = window_start - timedelta(days=1)

    windows.reverse()
    return windows


def _metric_value(metric_map: dict[str, dict[str, Any]], metric_name: str, side: str) -> Any:
    row = metric_map.get(metric_name) or {}
    return row.get(side)


def _verdict_label(verdict: Any, default: str = "--") -> str:
    text = str(verdict or "").strip().upper()
    if not text:
        return default
    return VERDICT_DISPLAY.get(text, str(verdict))


def _localize_text(value: Any) -> str:
    text = str(value or "")
    for source, target in TEXT_REPLACEMENTS:
        text = text.replace(source, target)
    return text


def _format_metric_value(metric_name: str, value: Any) -> str:
    if value in (None, ""):
        return "--"
    try:
        numeric = float(value)
    except Exception:
        return str(value)

    percent_hints = (
        "Rate",
        "Drawdown %",
        "Return",
        "CAGR",
        "Risk of Ruin",
        "Kelly",
        "Turnover",
    )
    money_hints = (
        "PnL",
        "Profit",
        "Loss",
        "Drawdown",
        "Net",
        "VaR",
        "CVaR",
        "Commission",
        "Tax",
        "Cost",
    )
    integer_hints = ("#Trades", "Trading Days", "Time to Recovery")

    if any(hint in metric_name for hint in percent_hints):
        return f"{numeric * 100.0:.2f}%"
    if any(hint in metric_name for hint in integer_hints):
        return f"{int(round(numeric)):,}"
    if any(hint in metric_name for hint in money_hints):
        return f"{numeric:,.0f}"
    if abs(numeric - round(numeric)) < 1e-9 and abs(numeric) >= 1000:
        return f"{int(round(numeric)):,}"
    return f"{numeric:,.2f}"


def _format_points(value: Any) -> str:
    if value in (None, ""):
        return "--"
    try:
        numeric = float(value)
    except Exception:
        return str(value)
    if abs(numeric - round(numeric)) < 1e-9:
        return f"{int(round(numeric)):,}"
    return f"{numeric:,.2f}"


def _format_runtime_metric(value: Any, *, suffix: str = "", precision: int = 1, fallback: str = "--") -> str:
    if value in (None, ""):
        return fallback
    try:
        numeric = float(value)
    except Exception:
        return str(value)
    return f"{numeric:.{precision}f}{suffix}"


def _kpi_metric_map(kpi_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("metric") or ""): {
            "section": str(row.get("section") or ""),
            "theoretical": row.get("theoretical"),
            "actual": row.get("actual"),
        }
        for row in kpi_rows
        if str(row.get("metric") or "").strip()
    }


def _ensure_dataframe(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value
    return pd.DataFrame()


def _daily_nav_dataframe(*, daily_bars: list[Any], trades: list[Any], capital: int) -> pd.DataFrame:
    if not daily_bars:
        return pd.DataFrame()

    actual_daily = defaultdict(float)
    theoretical_daily = defaultdict(float)
    for trade in trades or []:
        trade_date = int(getattr(trade, "exit_date", 0) or 0)
        actual_pnl = float(getattr(trade, "net_pnl", 0.0) or 0.0)
        theoretical_pnl = actual_pnl + float(getattr(trade, "slip_cost", 0.0) or 0.0)
        actual_daily[trade_date] += actual_pnl
        theoretical_daily[trade_date] += theoretical_pnl

    actual_nav = float(capital)
    theoretical_nav = float(capital)
    rows: list[dict[str, Any]] = []
    for bar in daily_bars:
        trade_date = int(bar.date)
        actual_pnl = float(actual_daily.get(trade_date, 0.0))
        theoretical_pnl = float(theoretical_daily.get(trade_date, 0.0))
        actual_nav += actual_pnl
        theoretical_nav += theoretical_pnl
        rows.append(
            {
                "date_int": trade_date,
                "日期": _date_label(trade_date),
                "理論淨值": theoretical_nav,
                "滑價淨值": actual_nav,
                "理論單日損益": theoretical_pnl,
                "滑價單日損益": actual_pnl,
            }
        )
    return pd.DataFrame(rows)


def _weekly_pnl_dataframe(nav_df: pd.DataFrame) -> pd.DataFrame:
    if nav_df.empty:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for row in nav_df.to_dict("records"):
        current_date = _date_from_int(int(row["date_int"]))
        iso_year, iso_week, _ = current_date.isocalendar()
        week_start = current_date - timedelta(days=current_date.weekday())
        rows.append(
            {
                "週別": f"{iso_year}-W{iso_week:02d}",
                "週起始日": week_start.isoformat(),
                "理論週損益": float(row["理論單日損益"]),
                "滑價週損益": float(row["滑價單日損益"]),
            }
        )

    weekly_df = pd.DataFrame(rows)
    if weekly_df.empty:
        return weekly_df
    return (
        weekly_df.groupby(["週別", "週起始日"], as_index=False)[["理論週損益", "滑價週損益"]]
        .sum()
        .reset_index(drop=True)
    )


def _window_return_rows(*, nav_df: pd.DataFrame, start_date: int, end_date: int, capital: int) -> pd.DataFrame:
    if nav_df.empty:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for window in _backward_year_windows(start_date=start_date, end_date=end_date):
        mask = (nav_df["date_int"] >= int(window["start_date"])) & (nav_df["date_int"] <= int(window["end_date"]))
        window_df = nav_df.loc[mask].copy()
        if window_df.empty:
            continue

        theoretical_pnl = float(window_df["理論單日損益"].sum())
        actual_pnl = float(window_df["滑價單日損益"].sum())
        rows.append(
            {
                "期間": window["label"] + ("（不足 1 年）" if bool(window["is_partial"]) else ""),
                "理論報酬率": f"{(theoretical_pnl / float(capital)) * 100.0:.2f}%",
                "滑價報酬率": f"{(actual_pnl / float(capital)) * 100.0:.2f}%",
                "理論淨利": f"{theoretical_pnl:,.0f}",
                "滑價淨利": f"{actual_pnl:,.0f}",
            }
        )
    return pd.DataFrame(rows)


def _trade_detail_dataframe(trades: list[Any]) -> pd.DataFrame:
    if not trades:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    cumulative_theoretical = 0.0
    cumulative_actual = 0.0

    for index, trade in enumerate(trades, start=1):
        theoretical_net = float(getattr(trade, "net_pnl", 0.0) or 0.0) + float(getattr(trade, "slip_cost", 0.0) or 0.0)
        actual_net = float(getattr(trade, "net_pnl", 0.0) or 0.0)
        cumulative_theoretical += theoretical_net
        cumulative_actual += actual_net
        direction = "多單" if int(getattr(trade, "direction", 1) or 1) >= 0 else "空單"
        rows.append(
            {
                "#": index,
                "進場時間": _datetime_label(int(getattr(trade, "entry_date", 0) or 0), int(getattr(trade, "entry_time", 0) or 0)),
                "出場時間": _datetime_label(int(getattr(trade, "exit_date", 0) or 0), int(getattr(trade, "exit_time", 0) or 0)),
                "方向": direction,
                "類別": f"{getattr(trade, 'entry_action', '--')}→{getattr(trade, 'exit_action', '--')}",
                "進場點位": float(getattr(trade, "entry_price", 0.0) or 0.0),
                "出場點位": float(getattr(trade, "exit_price", 0.0) or 0.0),
                "點數": float(getattr(trade, "points", 0.0) or 0.0),
                "手續費": float(getattr(trade, "fee", 0.0) or 0.0),
                "交易稅": float(getattr(trade, "tax", 0.0) or 0.0),
                "理論淨損益": theoretical_net,
                "累積理論淨損益": cumulative_theoretical,
                "實際淨損益": actual_net,
                "累積實際淨損益": cumulative_actual,
            }
        )
    return pd.DataFrame(rows)


@st.cache_data(show_spinner=False)
def _cached_period_analysis(
    *,
    params_json: str,
    minute_path: str,
    daily_path: str,
    script_name: str,
    capital: int,
    slip_per_side: float,
    start_date: int,
    end_date: int,
) -> dict[str, Any]:
    params = json.loads(params_json)
    minute_bars, daily_bars = load_market_data(minute_path, daily_path)
    result = run_0313plus_backtest(
        minute_bars,
        daily_bars,
        params,
        script_name,
        slip_per_side=float(slip_per_side),
    )
    _period_minute_bars, period_daily_bars = _filter_bars_for_period(
        minute_bars,
        daily_bars,
        start_date=int(start_date),
        end_date=int(end_date),
    )
    trades = [
        trade
        for trade in list(getattr(result, "trades", []) or [])
        if int(start_date) <= int(getattr(trade, "exit_date", 0) or 0) <= int(end_date)
    ]
    period_result = SimpleNamespace(trades=trades)
    report = build_report(period_result, period_daily_bars, capital)
    kpi_rows = build_kpi_snapshot(period_result, period_daily_bars, capital, slip_per_side=float(slip_per_side))
    metric_map = _kpi_metric_map(kpi_rows)
    nav_df = _daily_nav_dataframe(daily_bars=period_daily_bars, trades=trades, capital=capital)
    weekly_df = _weekly_pnl_dataframe(nav_df)
    yearly_df = _window_return_rows(nav_df=nav_df, start_date=int(start_date), end_date=int(end_date), capital=int(capital))
    monthly_df = pd.DataFrame(report.get("monthly_returns", []) or [])
    if not monthly_df.empty:
        monthly_df = monthly_df.rename(columns={"period": "月份", "pnl": "淨利", "return": "報酬率"})
        monthly_df["報酬率"] = monthly_df["報酬率"].map(lambda value: f"{float(value) * 100.0:.2f}%")
        monthly_df["淨利"] = monthly_df["淨利"].map(lambda value: f"{float(value):,.0f}")
    return {
        "period_label": f"{_date_label(start_date)} ~ {_date_label(end_date)}",
        "start_date": int(start_date),
        "end_date": int(end_date),
        "trade_count": len(trades),
        "kpi_rows": kpi_rows,
        "metric_map": metric_map,
        "nav_df": nav_df,
        "weekly_df": weekly_df,
        "yearly_df": yearly_df,
        "monthly_df": monthly_df,
        "trade_df": _trade_detail_dataframe(trades),
        "positive_week_ratio": _positive_week_ratio(weekly_df),
    }


def _top_candidate_params(top_df: pd.DataFrame, params_meta: list[dict[str, Any]], *, limit: int) -> list[dict[str, int | float]]:
    if top_df.empty or limit <= 0:
        return []

    candidates: list[dict[str, int | float]] = []
    seen: set[str] = set()
    for row in top_df.head(max(limit * 3, limit)).to_dict("records"):
        params = _best_params_from_row(row, params_meta)
        if not params:
            continue
        signature = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if signature in seen:
            continue
        seen.add(signature)
        candidates.append(params)
        if len(candidates) >= limit:
            break
    return candidates


def _profit_factor(gross_profit: float, gross_loss: float) -> float | None:
    if gross_loss <= 1e-9:
        return None
    return float(gross_profit) / float(gross_loss)


def _max_drawdown_ratio(values: pd.Series) -> float:
    series = pd.to_numeric(values, errors="coerce").dropna()
    if series.empty:
        return 0.0
    running_peak = series.cummax()
    drawdown_values: list[float] = []
    for peak, value in zip(running_peak.tolist(), series.tolist(), strict=False):
        peak_float = float(peak or 0.0)
        if peak_float <= 1e-9:
            continue
        drawdown_values.append(max(0.0, (peak_float - float(value)) / peak_float))
    return max(drawdown_values) if drawdown_values else 0.0


def _positive_week_ratio(weekly_df: pd.DataFrame) -> float | None:
    if weekly_df.empty or "滑價週損益" not in weekly_df.columns:
        return None
    values = pd.to_numeric(weekly_df["滑價週損益"], errors="coerce").fillna(0.0)
    active_values = values.loc[values.abs() > 1e-9]
    if active_values.empty:
        return None
    return float((active_values > 0.0).sum()) / float(len(active_values))


@st.cache_data(show_spinner=False)
def _cached_param_family_period_analysis(
    *,
    params_list_json: str,
    minute_path: str,
    daily_path: str,
    script_name: str,
    capital: int,
    slip_per_side: float,
    start_date: int,
    end_date: int,
) -> dict[str, Any]:
    params_list = list(json.loads(params_list_json) or [])
    if not params_list:
        return {}

    minute_bars, daily_bars = load_market_data(minute_path, daily_path)
    _period_minute_bars, period_daily_bars = _filter_bars_for_period(
        minute_bars,
        daily_bars,
        start_date=int(start_date),
        end_date=int(end_date),
    )
    if not period_daily_bars:
        return {}

    weight = 1.0 / float(len(params_list))
    actual_daily = defaultdict(float)
    theoretical_daily = defaultdict(float)
    actual_gross_profit = 0.0
    actual_gross_loss = 0.0
    theoretical_gross_profit = 0.0
    theoretical_gross_loss = 0.0
    total_trades = 0
    member_rows: list[dict[str, Any]] = []

    for index, params in enumerate(params_list, start=1):
        result = run_0313plus_backtest(
            minute_bars,
            daily_bars,
            params,
            script_name,
            slip_per_side=float(slip_per_side),
        )
        trades = [
            trade
            for trade in list(getattr(result, "trades", []) or [])
            if int(start_date) <= int(getattr(trade, "exit_date", 0) or 0) <= int(end_date)
        ]
        total_trades += len(trades)
        member_actual_net = 0.0
        member_theoretical_net = 0.0
        for trade in trades:
            trade_date = int(getattr(trade, "exit_date", 0) or 0)
            actual_pnl = float(getattr(trade, "net_pnl", 0.0) or 0.0)
            theoretical_pnl = actual_pnl + float(getattr(trade, "slip_cost", 0.0) or 0.0)
            scaled_actual = actual_pnl * weight
            scaled_theoretical = theoretical_pnl * weight
            actual_daily[trade_date] += scaled_actual
            theoretical_daily[trade_date] += scaled_theoretical
            member_actual_net += actual_pnl
            member_theoretical_net += theoretical_pnl
            if scaled_actual >= 0.0:
                actual_gross_profit += scaled_actual
            else:
                actual_gross_loss += abs(scaled_actual)
            if scaled_theoretical >= 0.0:
                theoretical_gross_profit += scaled_theoretical
            else:
                theoretical_gross_loss += abs(scaled_theoretical)

        member_rows.append(
            {
                "名次": index,
                "權重": f"{weight * 100.0:.1f}%",
                "交易筆數": len(trades),
                "組合貢獻滑價淨利": member_actual_net * weight,
                "單體滑價淨利": member_actual_net,
                "單體理論淨利": member_theoretical_net,
            }
        )

    actual_nav = float(capital)
    theoretical_nav = float(capital)
    rows: list[dict[str, Any]] = []
    for bar in period_daily_bars:
        trade_date = int(bar.date)
        actual_pnl = float(actual_daily.get(trade_date, 0.0))
        theoretical_pnl = float(theoretical_daily.get(trade_date, 0.0))
        actual_nav += actual_pnl
        theoretical_nav += theoretical_pnl
        rows.append(
            {
                "date_int": trade_date,
                "日期": _date_label(trade_date),
                "理論淨值": theoretical_nav,
                "滑價淨值": actual_nav,
                "理論單日損益": theoretical_pnl,
                "滑價單日損益": actual_pnl,
            }
        )

    nav_df = pd.DataFrame(rows)
    weekly_df = _weekly_pnl_dataframe(nav_df)
    theoretical_net = float(nav_df["理論淨值"].iloc[-1] - float(capital)) if not nav_df.empty else 0.0
    actual_net = float(nav_df["滑價淨值"].iloc[-1] - float(capital)) if not nav_df.empty else 0.0
    theoretical_mdd = _max_drawdown_ratio(nav_df["理論淨值"]) if not nav_df.empty else 0.0
    actual_mdd = _max_drawdown_ratio(nav_df["滑價淨值"]) if not nav_df.empty else 0.0
    worst_week = None
    if not weekly_df.empty and "滑價週損益" in weekly_df.columns:
        worst_week = float(pd.to_numeric(weekly_df["滑價週損益"], errors="coerce").min())

    kpi_rows = [
        {
            "section": "策略組合",
            "metric": "總淨利 Net Profit",
            "theoretical": theoretical_net,
            "actual": actual_net,
        },
        {
            "section": "策略組合",
            "metric": "總報酬率 Total Return",
            "theoretical": theoretical_net / float(capital),
            "actual": actual_net / float(capital),
        },
        {
            "section": "策略組合",
            "metric": "最大回撤率 Max Drawdown %",
            "theoretical": theoretical_mdd,
            "actual": actual_mdd,
        },
        {
            "section": "策略組合",
            "metric": "獲利因子 Profit Factor",
            "theoretical": _profit_factor(theoretical_gross_profit, theoretical_gross_loss),
            "actual": _profit_factor(actual_gross_profit, actual_gross_loss),
        },
        {
            "section": "策略組合",
            "metric": "交易筆數 #Trades",
            "theoretical": total_trades,
            "actual": total_trades,
        },
        {
            "section": "策略組合",
            "metric": "最差單週損益 Worst Week PnL",
            "theoretical": worst_week,
            "actual": worst_week,
        },
    ]

    return {
        "period_label": f"{_date_label(start_date)} ~ {_date_label(end_date)}",
        "start_date": int(start_date),
        "end_date": int(end_date),
        "trade_count": total_trades,
        "member_count": len(params_list),
        "weight": weight,
        "kpi_rows": kpi_rows,
        "metric_map": _kpi_metric_map(kpi_rows),
        "nav_df": nav_df,
        "weekly_df": weekly_df,
        "yearly_df": _window_return_rows(nav_df=nav_df, start_date=int(start_date), end_date=int(end_date), capital=int(capital)),
        "monthly_df": pd.DataFrame(),
        "trade_df": pd.DataFrame(),
        "member_df": pd.DataFrame(member_rows),
        "positive_week_ratio": _positive_week_ratio(weekly_df),
    }


def _strategy_family_summary_rows(label: str, snapshots: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for period_name, snapshot in snapshots:
        metric_map = snapshot.get("metric_map") or {}
        rows.append(
            {
                "方案": label,
                "區段": period_name,
                "期間": snapshot.get("period_label", "--"),
                "滑價淨利": _format_metric_value("總淨利 Net Profit", _metric_value(metric_map, "總淨利 Net Profit", "actual")),
                "總報酬": _format_metric_value("總報酬率 Total Return", _metric_value(metric_map, "總報酬率 Total Return", "actual")),
                "MDD": _format_metric_value(
                    "最大回撤率 Max Drawdown %",
                    _metric_value(metric_map, "最大回撤率 Max Drawdown %", "actual"),
                ),
                "PF": _format_metric_value("獲利因子 Profit Factor", _metric_value(metric_map, "獲利因子 Profit Factor", "actual")),
                "交易數": _format_metric_value("交易筆數 #Trades", _metric_value(metric_map, "交易筆數 #Trades", "actual")),
                "正週比例": _format_metric_value("總報酬率 Total Return", snapshot.get("positive_week_ratio")),
            }
        )
    return rows


def _analysis_signature(
    *,
    current_snapshot: dict[str, Any],
    research_periods: dict[str, Any],
    minute_path: str,
    daily_path: str,
    script_name: str,
    capital: int,
    slip_per_side: float,
) -> str:
    params = current_snapshot.get("params") if isinstance(current_snapshot, dict) else {}
    if not isinstance(params, dict):
        params = {}
    payload = {
        "params": params,
        "development_start_date": research_periods.get("development_start_date"),
        "development_end_date": research_periods.get("development_end_date"),
        "holdout_start_date": research_periods.get("holdout_start_date"),
        "holdout_end_date": research_periods.get("holdout_end_date"),
        "minute_path": minute_path,
        "daily_path": daily_path,
        "script_name": script_name,
        "capital": int(capital),
        "slip_per_side": float(slip_per_side),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _verdict_rank(verdict: str) -> int:
    return {"PASS": 2, "WATCH": 1, "REJECT": 0}.get(str(verdict or "").upper(), -1)


def _combined_verdict(*verdicts: str) -> str:
    cleaned = [str(verdict or "").upper() for verdict in verdicts if str(verdict or "").strip()]
    if not cleaned:
        return ""
    if any(verdict == "REJECT" for verdict in cleaned):
        return "REJECT"
    if any(verdict == "WATCH" for verdict in cleaned):
        return "WATCH"
    return "PASS"


@st.cache_data(show_spinner=False)
def _cached_development_qualification(
    *,
    params_json: str,
    minute_path: str,
    daily_path: str,
    script_name: str,
    capital: int,
    slip_per_side: float,
    snapshot_metrics_json: str,
    research_profile_tag: str,
    development_years: int | None,
    development_start_date: int,
    development_end_date: int,
    holdout_start_date: int,
    holdout_end_date: int,
    development_label: str,
    holdout_label: str,
) -> dict[str, Any]:
    params = json.loads(params_json)
    snapshot_metrics = json.loads(snapshot_metrics_json) if snapshot_metrics_json else {}
    research_periods = {
        "research_profile_tag": research_profile_tag,
        "development_years": development_years,
        "development_start_date": int(development_start_date),
        "development_end_date": int(development_end_date),
        "holdout_start_date": int(holdout_start_date),
        "holdout_end_date": int(holdout_end_date),
        "development_label": development_label,
        "holdout_label": holdout_label,
        "development_year_label": development_label,
        "holdout_year_label": holdout_label,
    }
    return run_0101_development_qualification(
        params=params,
        minute_path=minute_path,
        daily_path=daily_path,
        script_name=script_name,
        capital=int(capital),
        slip_per_side=float(slip_per_side),
        snapshot_metrics=snapshot_metrics,
        research_periods=research_periods,
    )


@st.cache_data(show_spinner=False)
def _cached_holdout_validation(
    *,
    params_json: str,
    minute_path: str,
    daily_path: str,
    script_name: str,
    capital: int,
    research_profile_tag: str,
    development_years: int | None,
    development_start_date: int,
    development_end_date: int,
    holdout_start_date: int,
    holdout_end_date: int,
    development_label: str,
    holdout_label: str,
) -> dict[str, Any]:
    params = json.loads(params_json)
    research_periods = {
        "research_profile_tag": research_profile_tag,
        "development_years": development_years,
        "development_start_date": int(development_start_date),
        "development_end_date": int(development_end_date),
        "holdout_start_date": int(holdout_start_date),
        "holdout_end_date": int(holdout_end_date),
        "development_label": development_label,
        "holdout_label": holdout_label,
        "development_year_label": development_label,
        "holdout_year_label": holdout_label,
    }
    return run_0101_holdout_validation(
        params=params,
        minute_path=minute_path,
        daily_path=daily_path,
        script_name=script_name,
        capital=int(capital),
        research_periods=research_periods,
    )


def _walk_forward_dataframe(qualification_report: dict[str, Any]) -> pd.DataFrame:
    walk_forward = qualification_report.get("walk_forward") if isinstance(qualification_report, dict) else {}
    df = pd.DataFrame(list((walk_forward or {}).get("rows") or []))
    if df.empty:
        return df
    for column in ("Train 滑價淨利", "Test 滑價淨利"):
        if column in df.columns:
            df[column] = df[column].map(lambda value: f"{float(value):,.0f}")
    for column in ("Train 報酬率", "Test 報酬率", "Test MDD"):
        if column in df.columns:
            df[column] = df[column].map(lambda value: f"{float(value):.2f}%")
    for column in ("Train PF", "Test PF"):
        if column in df.columns:
            df[column] = df[column].map(lambda value: f"{float(value):.2f}")
    for column in ("Train 交易筆數", "Test 交易筆數"):
        if column in df.columns:
            df[column] = df[column].map(lambda value: f"{int(value):,}")
    if "狀態" in df.columns:
        df["狀態"] = df["狀態"].map(_verdict_label)
    return df.rename(
        columns={
            "Train 區間": "訓練區間",
            "Test 區間": "驗證區間",
            "Train 滑價淨利": "訓練滑價淨利",
            "Train 報酬率": "訓練報酬率",
            "Train PF": "訓練 PF",
            "Train 交易筆數": "訓練交易筆數",
            "Test 滑價淨利": "驗證滑價淨利",
            "Test 報酬率": "驗證報酬率",
            "Test PF": "驗證 PF",
            "Test MDD": "驗證 MDD",
            "Test 交易筆數": "驗證交易筆數",
            "狀態": "結論",
        }
    )


def _regime_dataframe(qualification_report: dict[str, Any]) -> pd.DataFrame:
    regime = qualification_report.get("regime") if isinstance(qualification_report, dict) else {}
    df = pd.DataFrame(list((regime or {}).get("rows") or []))
    if df.empty:
        return df
    if "滑價淨利" in df.columns:
        df["滑價淨利"] = df["滑價淨利"].map(lambda value: f"{float(value):,.0f}")
    if "勝率" in df.columns:
        df["勝率"] = df["勝率"].map(lambda value: f"{float(value):.2f}%")
    if "平均每筆" in df.columns:
        df["平均每筆"] = df["平均每筆"].map(lambda value: f"{float(value):,.0f}")
    if "正利潤占比" in df.columns:
        df["正利潤占比"] = df["正利潤占比"].map(lambda value: f"{float(value) * 100.0:.1f}%")
    return df.rename(columns={"Bucket": "分桶"})


def _dynamic_slippage_bucket_dataframe(qualification_report: dict[str, Any]) -> pd.DataFrame:
    dynamic_slippage = qualification_report.get("dynamic_slippage") if isinstance(qualification_report, dict) else {}
    bucket_counts = dict(dynamic_slippage.get("bucket_counts") or {})
    rows = [
        {"波動層級": "低波動", "額外滑價": "+0.0 點", "交易筆數": int(bucket_counts.get("低波動", 0) or 0)},
        {"波動層級": "中波動", "額外滑價": "+0.5 點", "交易筆數": int(bucket_counts.get("中波動", 0) or 0)},
        {"波動層級": "高波動", "額外滑價": "+1.0 點", "交易筆數": int(bucket_counts.get("高波動", 0) or 0)},
    ]
    return pd.DataFrame(rows)


def _render_snapshot_panel(title: str, snapshot: dict[str, Any]) -> None:
    st.markdown(f"#### {title}")
    st.caption(str(snapshot.get("period_label") or "--"))
    metric_map = snapshot.get("metric_map") or {}
    metric_rows = [
        ("理論淨利", _format_metric_value("總淨利 Net Profit", _metric_value(metric_map, "總淨利 Net Profit", "theoretical"))),
        ("滑價淨利", _format_metric_value("總淨利 Net Profit", _metric_value(metric_map, "總淨利 Net Profit", "actual"))),
        ("理論 MDD", _format_metric_value("最大回撤率 Max Drawdown %", _metric_value(metric_map, "最大回撤率 Max Drawdown %", "theoretical"))),
        ("滑價 MDD", _format_metric_value("最大回撤率 Max Drawdown %", _metric_value(metric_map, "最大回撤率 Max Drawdown %", "actual"))),
        ("理論 PF", _format_metric_value("獲利因子 Profit Factor", _metric_value(metric_map, "獲利因子 Profit Factor", "theoretical"))),
        ("滑價 PF", _format_metric_value("獲利因子 Profit Factor", _metric_value(metric_map, "獲利因子 Profit Factor", "actual"))),
    ]
    for left_item, right_item in ((metric_rows[0], metric_rows[1]), (metric_rows[2], metric_rows[3]), (metric_rows[4], metric_rows[5])):
        cols = st.columns(2)
        cols[0].metric(left_item[0], left_item[1])
        cols[1].metric(right_item[0], right_item[1])


def _snapshot_actual(snapshot: dict[str, Any] | None, metric_name: str) -> Any:
    metric_map = (snapshot or {}).get("metric_map") or {}
    return _metric_value(metric_map, metric_name, "actual")


def _snapshot_summary_row(label: str, snapshot: dict[str, Any] | None) -> dict[str, str]:
    snapshot = snapshot or {}
    return {
        "區段": label,
        "期間": str(snapshot.get("period_label") or "--"),
        "滑價淨利": _format_metric_value("總淨利 Net Profit", _snapshot_actual(snapshot, "總淨利 Net Profit")),
        "總報酬": _format_metric_value("總報酬率 Total Return", _snapshot_actual(snapshot, "總報酬率 Total Return")),
        "MDD": _format_metric_value("最大回撤率 Max Drawdown %", _snapshot_actual(snapshot, "最大回撤率 Max Drawdown %")),
        "PF": _format_metric_value("獲利因子 Profit Factor", _snapshot_actual(snapshot, "獲利因子 Profit Factor")),
        "交易數": _format_metric_value("交易筆數 #Trades", _snapshot_actual(snapshot, "交易筆數 #Trades")),
    }


def _decision_message(verdict: str, *, has_holdout: bool) -> tuple[str, str]:
    text = str(verdict or "").upper()
    if not has_holdout:
        return "info", "下一步：先按「測試驗證區」，不要只用開發區結果做決定。"
    if text == "PASS":
        return "success", "初步可進人工複核：再看三段比較、交易明細與實際上線限制。"
    if text == "WATCH":
        return "warning", "目前只能列入觀察：先檢查驗證區延續性、MDD 與交易數。"
    return "error", "目前不建議上線：優先縮小參數範圍或調整硬性條件後重跑。"


def _render_next_action_panel(*, has_candidate: bool, has_holdout: bool, has_wfo: bool) -> None:
    st.markdown("## 現在要做什麼")
    if not has_candidate:
        st.info("步驟 1：先按下方「開始執行」，先跑出最佳化候選。")
    elif not has_holdout:
        st.warning("步驟 2：先按「步驟 2：只跑單次 OOS」，或直接按「一鍵跑完整驗證（OOS + WFO）」。")
    elif not has_wfo:
        st.warning("步驟 3：單次 OOS 已完成，現在請按「步驟 3：計算多輪 WFO」。")
    else:
        st.success("步驟 1 到 3 已完成。現在先看下方「多層策略驗證與決策系統」，再決定是否進前測。")

    if not has_candidate:
        statuses = ("下一步", "待步驟 1", "待步驟 2", "待步驟 3")
    elif not has_holdout:
        statuses = ("已完成", "下一步", "待步驟 2", "待步驟 3")
    elif not has_wfo:
        statuses = ("已完成", "已完成", "下一步", "待步驟 3")
    else:
        statuses = ("已完成", "已完成", "已完成", "現在看這裡")

    flow_rows = [
        {"步驟": "1", "動作": "開始執行", "狀態": statuses[0], "說明": "先跑最佳化，產生最佳候選。"},
        {"步驟": "2", "動作": "測試驗證區", "狀態": statuses[1], "說明": "這是單次 OOS，只作初篩。"},
        {"步驟": "3", "動作": "計算多輪 WFO", "狀態": statuses[2], "說明": "這一步會一起處理 WFO 與 gap。"},
        {"步驟": "4", "動作": "看決策系統", "狀態": statuses[3], "說明": "最後再看 PBO / DSR、前測與建議。"},
    ]
    st.dataframe(pd.DataFrame(flow_rows), width="stretch", hide_index=True)


def _render_decision_overview(
    *,
    current_snapshot: dict[str, Any],
    qualification_report: dict[str, Any],
    development_snapshot: dict[str, Any],
    holdout_snapshot: dict[str, Any] | None,
    full_snapshot: dict[str, Any] | None,
    holdout_validation: dict[str, Any] | None,
) -> None:
    dev_verdict = str(qualification_report.get("verdict") or "REJECT")
    holdout_verdict = str((holdout_validation or {}).get("final_verdict") or "")
    has_holdout = bool(holdout_snapshot and full_snapshot and holdout_validation)
    final_verdict = _combined_verdict(dev_verdict, holdout_verdict) if has_holdout else ""
    headline_verdict = final_verdict or dev_verdict
    message_kind, message = _decision_message(headline_verdict, has_holdout=has_holdout)

    st.markdown("## 單次驗證總覽")
    st.caption("這裡仍是原本單次 OOS / 開發區守門摘要，只能當初篩與補充資訊；正式升級判斷請看下方的多層決策系統。")
    getattr(st, message_kind)(message)

    cards = st.columns(4)
    cards[0].metric("開發區結論", _verdict_label(dev_verdict))
    cards[1].metric("驗證區結論", _verdict_label(holdout_verdict, default="待測試") if holdout_verdict else "待測試")
    cards[2].metric("單次驗證判定", _verdict_label(final_verdict, default="待驗收") if final_verdict else "待驗收")
    cards[3].metric("穩健分數", base._format_number(current_snapshot.get("robust_score", current_snapshot.get("composite_score"))))

    rows = [_snapshot_summary_row("最佳化區", development_snapshot)]
    if has_holdout:
        rows.append(_snapshot_summary_row("驗證區", holdout_snapshot))
        rows.append(_snapshot_summary_row("全期間", full_snapshot))
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _risk_points(value: float, *, low: float, high: float, max_points: float) -> float:
    if value <= low:
        return 0.0
    if value >= high:
        return float(max_points)
    span = max(float(high) - float(low), 1e-9)
    return float(max_points) * ((float(value) - float(low)) / span)


def _overfit_risk_report(
    *,
    view: dict[str, Any],
    params_meta: list[dict[str, Any]],
    current_snapshot: dict[str, Any],
    development_snapshot: dict[str, Any],
    holdout_snapshot: dict[str, Any] | None,
    qualification_report: dict[str, Any],
    holdout_validation: dict[str, Any] | None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total_score = 0.0

    def add(name: str, points: float, max_points: float, status: str, note: str) -> None:
        nonlocal total_score
        clean_points = max(0.0, min(float(points), float(max_points)))
        total_score += clean_points
        rows.append(
            {
                "風險項目": name,
                "分數": f"{clean_points:.1f} / {float(max_points):.0f}",
                "狀態": status,
                "說明": note,
                "_points": clean_points,
                "_max": float(max_points),
            }
        )

    trial_count = int(view.get("done") or view.get("total") or 0)
    if trial_count <= 0:
        add("搜尋次數", 4.0, 15.0, "資料不足", "目前沒有完整測試組數，暫以中低風險處理。")
    else:
        trial_points = min(15.0, max(0.0, (math.log10(max(trial_count, 1)) - 2.0) * 5.0))
        if trial_count >= 10_000:
            status = "高"
        elif trial_count >= 1_000:
            status = "中"
        else:
            status = "低"
        add("搜尋次數", trial_points, 15.0, status, f"本輪已測 {trial_count:,} 組；測越多越需要多重測試懲罰。")

    top_df = _ensure_dataframe(view.get("current_top_df"))
    top_rows = len(top_df)
    if top_rows < 3:
        add("候選數", 12.0, 12.0, "高", "少於 3 組候選，容易把決策押在單一最佳參數。")
    elif top_rows < 6:
        add("候選數", 7.0, 12.0, "中", f"目前只有 {top_rows} 組候選，可判斷 Top 3，但穩定區樣本偏少。")
    else:
        add("候選數", 2.0, 12.0, "低", f"目前有 {top_rows} 組候選，可觀察參數區域。")

    param_df, param_names = _param_stability_source(top_df, params_meta)
    if param_df.empty or not param_names:
        add("參數穩定區", 12.0, 15.0, "資料不足", "候選資料不足，無法判斷穩定區是否存在。")
    else:
        diverse_params = 0
        evaluated_params = 0
        for name in param_names:
            values = pd.to_numeric(param_df[name], errors="coerce").dropna()
            if values.empty:
                continue
            evaluated_params += 1
            if values.nunique() >= min(3, len(values)):
                diverse_params += 1
        diversity_ratio = (diverse_params / evaluated_params) if evaluated_params else 1.0
        stability_points = _risk_points(diversity_ratio, low=0.25, high=0.85, max_points=15.0)
        if diversity_ratio >= 0.75:
            status = "高"
        elif diversity_ratio >= 0.45:
            status = "中"
        else:
            status = "低"
        add(
            "參數穩定區",
            stability_points,
            15.0,
            status,
            f"{diverse_params}/{evaluated_params} 個參數在候選群中分散；太分散代表可能沒有穩定平台。",
        )

    plateau_score = _float_or_none(current_snapshot.get("plateau_score"))
    if plateau_score is None:
        add("平台分數", 8.0, 10.0, "資料不足", "缺少平台分數，無法確認最佳點旁邊是否也有效。")
    elif plateau_score < 0.0:
        add("平台分數", 10.0, 10.0, "高", f"平台分數 {plateau_score:.2f}，代表最佳點附近不穩。")
    elif plateau_score < 1.0:
        add("平台分數", 5.0, 10.0, "中", f"平台分數 {plateau_score:.2f}，仍需要更多鄰近驗證。")
    else:
        add("平台分數", 1.0, 10.0, "低", f"平台分數 {plateau_score:.2f}，最佳點附近相對可接受。")

    worst_window = _float_or_none(current_snapshot.get("worst_window_return"))
    if worst_window is None:
        add("多段最差窗", 6.0, 10.0, "資料不足", "缺少最差年窗資料。")
    elif worst_window < -10.0:
        add("多段最差窗", 10.0, 10.0, "高", f"最差年窗 {worst_window:.2f}%，有明顯失效區段。")
    elif worst_window < 0.0:
        add("多段最差窗", 6.0, 10.0, "中", f"最差年窗 {worst_window:.2f}%，仍有虧損區段。")
    else:
        add("多段最差窗", 1.0, 10.0, "低", f"最差年窗 {worst_window:.2f}%，多段延續性較好。")

    dev_return = _snapshot_metric_number(development_snapshot, "總報酬率 Total Return")
    holdout_return = _snapshot_metric_number(holdout_snapshot, "總報酬率 Total Return")
    if holdout_snapshot is None:
        add("驗證區落差", 18.0, 25.0, "待驗收", "尚未測驗證區，這是目前最大的未知風險。")
    elif holdout_return is None:
        add("驗證區落差", 20.0, 25.0, "資料不足", "驗證區沒有可用報酬率。")
    elif holdout_return <= 0.0:
        add("驗證區落差", 25.0, 25.0, "高", "驗證區滑價報酬為負，回測優勢沒有延續。")
    elif dev_return and dev_return > 0.0:
        retention = float(holdout_return) / max(float(dev_return), 1e-9)
        gap_points = _risk_points(1.0 - retention, low=0.30, high=0.85, max_points=25.0)
        if retention < 0.25:
            status = "高"
        elif retention < 0.55:
            status = "中"
        else:
            status = "低"
        add("驗證區落差", gap_points, 25.0, status, f"驗證區保留開發區報酬約 {retention * 100.0:.1f}%。")
    else:
        add("驗證區落差", 10.0, 25.0, "觀察", "開發區報酬率不明顯，無法計算保留率。")

    slip_score = _float_or_none(current_snapshot.get("slip_stress_score"))
    if slip_score is None:
        add("滑價壓測", 8.0, 13.0, "資料不足", "缺少滑價壓測分數。")
    elif slip_score < 0.0:
        add("滑價壓測", 13.0, 13.0, "高", f"滑價壓測分數 {slip_score:.2f}，成本上升後可能失效。")
    elif slip_score < 1.0:
        add("滑價壓測", 6.0, 13.0, "中", f"滑價壓測分數 {slip_score:.2f}，仍需保守處理。")
    else:
        add("滑價壓測", 1.0, 13.0, "低", f"滑價壓測分數 {slip_score:.2f}。")

    dev_verdict = str((qualification_report or {}).get("verdict") or "")
    holdout_verdict = str((holdout_validation or {}).get("final_verdict") or "")
    final_verdict = _combined_verdict(dev_verdict, holdout_verdict) if holdout_verdict else dev_verdict
    if str(final_verdict).upper() == "REJECT":
        add("審查結論", 10.0, 10.0, "高", "資格審查已淘汰，不能上線。")
    elif str(final_verdict).upper() == "WATCH":
        add("審查結論", 5.0, 10.0, "中", "目前只能觀察，不能直接上線。")
    else:
        add("審查結論", 1.0, 10.0, "低", "目前審查結論通過，但仍需 forward test。")

    score = max(0.0, min(100.0, total_score))
    if score >= 70.0:
        level = "高"
        verdict = "不建議上線"
        message = "過度最佳化風險偏高：先不要換策略，優先縮小參數、增加驗收或空手。"
    elif score >= 40.0:
        level = "中"
        verdict = "只能觀察"
        message = "風險中等：最多做模擬盤或小資金，不適合直接放大。"
    else:
        level = "低"
        verdict = "可進複核"
        message = "風險相對可控：仍需舊策略守門與 8 到 12 週 forward test。"
    return {"score": score, "level": level, "verdict": verdict, "message": message, "rows": rows}


def _render_overfit_risk_panel(
    *,
    view: dict[str, Any],
    params_meta: list[dict[str, Any]],
    current_snapshot: dict[str, Any],
    development_snapshot: dict[str, Any],
    holdout_snapshot: dict[str, Any] | None,
    qualification_report: dict[str, Any],
    holdout_validation: dict[str, Any] | None,
) -> None:
    report = _overfit_risk_report(
        view=view,
        params_meta=params_meta,
        current_snapshot=current_snapshot,
        development_snapshot=development_snapshot,
        holdout_snapshot=holdout_snapshot,
        qualification_report=qualification_report,
        holdout_validation=holdout_validation,
    )
    st.markdown("## 過度最佳化風險")
    st.caption("這是 PBO proxy：用搜尋次數、候選穩定度、多段落差與滑價壓測估計資料探勘風險。")

    columns = st.columns(3)
    columns[0].metric("風險分數", f"{float(report['score']):.1f} / 100")
    columns[1].metric("風險等級", str(report["level"]))
    columns[2].metric("使用建議", str(report["verdict"]))

    if str(report["level"]) == "高":
        st.error(str(report["message"]))
    elif str(report["level"]) == "中":
        st.warning(str(report["message"]))
    else:
        st.info(str(report["message"]))

    rows = pd.DataFrame(list(report.get("rows") or []))
    if not rows.empty:
        st.dataframe(rows[["風險項目", "分數", "狀態", "說明"]], width="stretch", hide_index=True, height=300)


def _params_signature(params: dict[str, Any] | None) -> str:
    clean_params: dict[str, int | float | str] = {}
    for name, value in sorted((params or {}).items()):
        try:
            numeric = float(value)
        except Exception:
            clean_params[str(name)] = str(value)
            continue
        if abs(numeric - round(numeric)) < 1e-9:
            clean_params[str(name)] = int(round(numeric))
        else:
            clean_params[str(name)] = round(float(numeric), 8)
    return json.dumps(clean_params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _history_float(value: Any, default: float) -> float:
    try:
        numeric = float(value)
    except Exception:
        return float(default)
    if pd.isna(numeric):
        return float(default)
    return float(numeric)


def _candidate_history_key(row: dict[str, Any]) -> tuple[float, float, float, float, float]:
    return (
        _history_float(row.get("robust_score", row.get("composite_score")), -1e18),
        _history_float(row.get("total_return"), -1e18),
        -_history_float(row.get("mdd_pct"), 1e18),
        _history_float(row.get("year_avg_return"), -1e18),
        _history_float(row.get("n_trades"), -1e18),
    )


def _load_distinct_incumbent_snapshot(
    *,
    current_params: dict[str, Any],
    params_meta: list[dict[str, Any]],
    slip_per_side: float,
    research_profile_tag: str,
) -> dict[str, Any]:
    if not current_params or not PERSISTENT_TOP10_JSON.exists():
        return {}
    try:
        payload = json.loads(PERSISTENT_TOP10_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}

    current_signature = _params_signature(current_params)
    rows = []
    for row in list(payload.get("rows") or []):
        if not isinstance(row, dict):
            continue
        if str(row.get("research_profile_tag") or "") != str(research_profile_tag or ""):
            continue
        try:
            row_slip = float(row.get("slip_per_side"))
        except Exception:
            continue
        if abs(row_slip - float(slip_per_side)) > 1e-9:
            continue
        params = _best_params_from_row(row, params_meta)
        if not params or _params_signature(params) == current_signature:
            continue
        candidate = dict(row)
        candidate["_params"] = params
        rows.append(candidate)

    if not rows:
        return {}
    incumbent_row = max(rows, key=_candidate_history_key)
    snapshot = build_best_snapshot(incumbent_row, [str(item["name"]) for item in params_meta])
    if not snapshot:
        return {}
    snapshot["params"] = dict(incumbent_row.get("_params") or snapshot.get("params") or {})
    snapshot["saved_at"] = incumbent_row.get("saved_at") or incumbent_row.get("source_saved_at") or payload.get("saved_at")
    snapshot["strategy_signature"] = incumbent_row.get("strategy_signature")
    return snapshot


def _snapshot_metric_number(snapshot: dict[str, Any] | None, metric_name: str) -> float | None:
    return _float_or_none(_snapshot_actual(snapshot, metric_name))


def _switch_decision(
    *,
    deployment_verdict: str,
    incumbent_holdout: dict[str, Any],
    challenger_holdout: dict[str, Any],
) -> tuple[str, str, str]:
    if str(deployment_verdict or "").upper() == "REJECT":
        return "候選淘汰", "error", "新策略沒有通過驗收，不建議取代舊策略；若舊策略也已失效，下週應空手。"
    if str(deployment_verdict or "").upper() == "WATCH":
        return "列入觀察", "warning", "新策略仍是觀察等級，不換舊策略；最多只能進模擬盤。"

    challenger_return = _snapshot_metric_number(challenger_holdout, "總報酬率 Total Return")
    incumbent_return = _snapshot_metric_number(incumbent_holdout, "總報酬率 Total Return")
    challenger_mdd = _snapshot_metric_number(challenger_holdout, "最大回撤率 Max Drawdown %")
    incumbent_mdd = _snapshot_metric_number(incumbent_holdout, "最大回撤率 Max Drawdown %")
    challenger_pf = _snapshot_metric_number(challenger_holdout, "獲利因子 Profit Factor")
    incumbent_pf = _snapshot_metric_number(incumbent_holdout, "獲利因子 Profit Factor")
    challenger_trades = _snapshot_metric_number(challenger_holdout, "交易筆數 #Trades")

    if challenger_return is None or challenger_return <= 0:
        return "不換策略", "warning", "新策略驗證區沒有正報酬，不切換。"
    if challenger_trades is not None and challenger_trades < 4:
        return "樣本不足", "warning", "新策略驗證區交易數太少，不足以切換。"
    if incumbent_return is None:
        return "小資金試跑", "info", "找不到可比較的舊策略；新策略只能先進模擬盤或小資金試跑。"

    return_delta = float(challenger_return) - float(incumbent_return)
    mdd_delta = None if challenger_mdd is None or incumbent_mdd is None else float(challenger_mdd) - float(incumbent_mdd)
    pf_delta = None if challenger_pf is None or incumbent_pf is None else float(challenger_pf) - float(incumbent_pf)
    incumbent_failed = float(incumbent_return) <= 0 or (incumbent_pf is not None and float(incumbent_pf) < 1.0)
    clearly_better = return_delta >= 0.03 and (mdd_delta is None or mdd_delta <= 0.03) and (pf_delta is None or pf_delta >= -0.05)

    if incumbent_failed and float(challenger_return) > 0 and (challenger_pf is None or float(challenger_pf) >= 1.05):
        return "可替換舊策略", "success", "舊策略驗證區偏弱，新策略通過驗收且維持正報酬，可列入替換候選。"
    if clearly_better:
        return "可替換舊策略", "success", "新策略驗證區明顯勝出，且 MDD / PF 沒有明顯惡化，可進人工複核後替換。"
    if return_delta > 0:
        return "暫不更換", "info", "新策略有改善但幅度不夠大，先維持舊策略並觀察下一週。"
    return "維持舊策略", "warning", "新策略沒有打敗舊策略，維持舊策略或空手。"


def _render_incumbent_challenger_panel(
    *,
    current_snapshot: dict[str, Any],
    params_meta: list[dict[str, Any]],
    minute_path: str,
    daily_path: str,
    script_name: str,
    capital: int,
    slip_per_side: float,
    research_periods: dict[str, Any],
    deployment_verdict: str,
    challenger_holdout: dict[str, Any],
    challenger_full: dict[str, Any],
) -> None:
    st.markdown("## 舊策略 / 新策略守門")
    st.caption("每週跑出來的是挑戰者；只有明顯勝過舊策略且風險沒惡化，才建議更換。")

    incumbent_snapshot = _load_distinct_incumbent_snapshot(
        current_params=dict(current_snapshot.get("params") or {}),
        params_meta=params_meta,
        slip_per_side=float(slip_per_side),
        research_profile_tag=str(research_periods.get("research_profile_tag") or RESEARCH_PROFILE_TAG_0101),
    )
    if not incumbent_snapshot:
        label, kind, message = _switch_decision(
            deployment_verdict=deployment_verdict,
            incumbent_holdout={},
            challenger_holdout=challenger_holdout,
        )
        getattr(st, kind)(f"{label}：{message}")
        return

    incumbent_params_json = json.dumps(
        incumbent_snapshot.get("params") or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    with st.spinner("正在比對舊策略與本次新策略..."):
        incumbent_holdout = _cached_period_analysis(
            params_json=incumbent_params_json,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            capital=int(capital),
            slip_per_side=float(slip_per_side),
            start_date=int(research_periods["holdout_start_date"]),
            end_date=int(research_periods["holdout_end_date"]),
        )
        incumbent_full = _cached_period_analysis(
            params_json=incumbent_params_json,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            capital=int(capital),
            slip_per_side=float(slip_per_side),
            start_date=int(research_periods["development_start_date"]),
            end_date=int(research_periods["holdout_end_date"]),
        )

    label, kind, message = _switch_decision(
        deployment_verdict=deployment_verdict,
        incumbent_holdout=incumbent_holdout,
        challenger_holdout=challenger_holdout,
    )
    getattr(st, kind)(f"{label}：{message}")

    rows = []
    rows.extend(_strategy_family_summary_rows("舊策略", [("驗證區", incumbent_holdout), ("全期間", incumbent_full)]))
    rows.extend(_strategy_family_summary_rows("新策略", [("驗證區", challenger_holdout), ("全期間", challenger_full)]))
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    with st.expander("查看舊策略與新策略參數", expanded=False):
        compare_rows = []
        incumbent_params = dict(incumbent_snapshot.get("params") or {})
        challenger_params = dict(current_snapshot.get("params") or {})
        for meta in params_meta:
            name = str(meta.get("name") or "")
            if not name:
                continue
            old_value = incumbent_params.get(name)
            new_value = challenger_params.get(name)
            if old_value is None and new_value is None:
                continue
            compare_rows.append(
                {
                    "參數": name,
                    "舊策略": base._format_number(old_value, 4) if old_value is not None else "--",
                    "新策略": base._format_number(new_value, 4) if new_value is not None else "--",
                }
            )
        st.dataframe(pd.DataFrame(compare_rows), width="stretch", hide_index=True, height=320)


def _render_qualification_report(
    *,
    current_snapshot: dict[str, Any],
    qualification_report: dict[str, Any],
    holdout_validation: dict[str, Any] | None,
) -> None:
    st.markdown("## 上線資格審查")
    st.caption("這一區把開發區最佳化結果轉成真正的策略審查報告，先看能不能活，再看是否值得進驗證區驗收。")

    if not qualification_report:
        st.info("目前尚未產生開發區審查報告。")
        return

    walk_forward = qualification_report.get("walk_forward") or {}
    regime = qualification_report.get("regime") or {}
    dynamic_slippage = qualification_report.get("dynamic_slippage") or {}
    dev_verdict = str(qualification_report.get("verdict") or "REJECT")
    holdout_verdict = str((holdout_validation or {}).get("final_verdict") or "")
    final_verdict = _combined_verdict(dev_verdict, holdout_verdict) if holdout_verdict else ""

    row1 = st.columns(4)
    row1[0].metric("開發區結論", _verdict_label(dev_verdict))
    row1[1].metric("目前上線判定", _verdict_label(final_verdict, default="待驗證區驗收") if final_verdict else "待驗證區驗收")
    row1[2].metric("逐窗驗證通過率", f"{float(walk_forward.get('pass_rate') or 0.0) * 100.0:.1f}%")
    row1[3].metric("市場狀態集中度", f"{float(regime.get('dominant_share') or 0.0) * 100.0:.1f}%")

    row2 = st.columns(4)
    row2[0].metric("最差驗證切片", f"{float(walk_forward.get('worst_test_return_pct') or 0.0):.2f}%")
    row2[1].metric("動態滑價淨利", f"{float(dynamic_slippage.get('net_profit') or 0.0):,.0f}")
    row2[2].metric("平台分數", base._format_number(current_snapshot.get("plateau_score")))
    row2[3].metric("穩健分數", base._format_number(current_snapshot.get("robust_score", current_snapshot.get("composite_score"))))

    row3 = st.columns(4)
    row3[0].metric("動態滑價 PF / MDD", f"{float(dynamic_slippage.get('profit_factor') or 0.0):.2f} / {float(dynamic_slippage.get('mdd_pct') or 0.0):.2f}%")
    row3[1].metric("最差年窗", base._format_percent(current_snapshot.get("worst_window_return")))
    row3[2].metric("優勢市場狀態", _localize_text(regime.get("strongest_bucket") or "--"))
    row3[3].metric("驗證區結論", _verdict_label(holdout_verdict, default="待測試") if holdout_verdict else "待測試")

    summary_lines = [_localize_text(line) for line in list(qualification_report.get("summary_lines") or []) if str(line).strip()]
    gate_df = pd.DataFrame(list(qualification_report.get("gate_rows") or []))
    if not gate_df.empty:
        gate_df = gate_df.rename(columns={"Gate": "審查關卡", "狀態": "結論", "觀察": "說明"})
        if "結論" in gate_df.columns:
            gate_df["結論"] = gate_df["結論"].map(_verdict_label)
        for column in ("審查關卡", "說明"):
            if column in gate_df.columns:
                gate_df[column] = gate_df[column].map(_localize_text)

    summary_cols = st.columns((1.05, 0.95))
    with summary_cols[0]:
        if summary_lines:
            base._render_bullet_block("審查重點", summary_lines)
    with summary_cols[1]:
        if not gate_df.empty:
            st.markdown("### 審查關卡總覽")
            st.dataframe(gate_df, width="stretch", hide_index=True, height=210)

    detail_cols = st.columns(2)
    with detail_cols[0]:
        with st.expander("查看逐窗驗證明細", expanded=False):
            st.caption("固定規格：訓練 36 個月 / 驗證 6 個月 / 每次前推 3 個月。")
            wf_df = _walk_forward_dataframe(qualification_report)
            if wf_df.empty:
                st.info("目前沒有逐窗驗證切片資料。")
            else:
                st.dataframe(wf_df, width="stretch", hide_index=True, height=320)

    with detail_cols[1]:
        with st.expander("查看市場狀態與滑價壓測明細", expanded=False):
            regime_df = _regime_dataframe(qualification_report)
            if regime_df.empty:
                st.info("目前沒有市場狀態分桶資料。")
            else:
                st.dataframe(regime_df, width="stretch", hide_index=True, height=260)

            dynamic_rows = [
                {
                    "情境": _localize_text(dynamic_slippage.get("scenario_label") or "波動分層滑價"),
                    "淨利": f"{float(dynamic_slippage.get('net_profit') or 0.0):,.0f}",
                    "PF": f"{float(dynamic_slippage.get('profit_factor') or 0.0):.2f}",
                    "MDD": f"{float(dynamic_slippage.get('mdd_pct') or 0.0):.2f}%",
                    "衰減": f"{float(dynamic_slippage.get('decay_pct') or 0.0):.1f}%",
                    "結論": _verdict_label(dynamic_slippage.get("verdict")),
                }
            ]
            st.dataframe(pd.DataFrame(dynamic_rows), width="stretch", hide_index=True)
            st.dataframe(_dynamic_slippage_bucket_dataframe(qualification_report), width="stretch", hide_index=True)


def _render_metric_cards(title: str, snapshot: dict[str, Any]) -> None:
    st.markdown(f"### {title}")
    metric_map = snapshot.get("metric_map") or {}
    metrics = [
        ("理論淨利", _format_metric_value("總淨利 Net Profit", _metric_value(metric_map, "總淨利 Net Profit", "theoretical"))),
        ("滑價淨利", _format_metric_value("總淨利 Net Profit", _metric_value(metric_map, "總淨利 Net Profit", "actual"))),
        ("理論 MDD", _format_metric_value("最大回撤率 Max Drawdown %", _metric_value(metric_map, "最大回撤率 Max Drawdown %", "theoretical"))),
        ("滑價 MDD", _format_metric_value("最大回撤率 Max Drawdown %", _metric_value(metric_map, "最大回撤率 Max Drawdown %", "actual"))),
        ("理論 PF", _format_metric_value("獲利因子 Profit Factor", _metric_value(metric_map, "獲利因子 Profit Factor", "theoretical"))),
        ("滑價 PF", _format_metric_value("獲利因子 Profit Factor", _metric_value(metric_map, "獲利因子 Profit Factor", "actual"))),
    ]
    for offset in range(0, len(metrics), 3):
        cols = st.columns(3)
        for col, (label, value) in zip(cols, metrics[offset : offset + 3]):
            col.metric(label, value)


def _week_start_from_label(week_label: Any) -> str:
    try:
        year_text, week_text = str(week_label).split("-W", 1)
        return date.fromisocalendar(int(year_text), int(week_text), 1).isoformat()
    except Exception:
        return ""


def _chart_boundary_layers(*, field_name: str, boundary_date: int | None, label: str) -> list[alt.Chart]:
    if not boundary_date:
        return []
    boundary_text = _date_label(int(boundary_date))
    boundary_df = pd.DataFrame(
        {
            field_name: [pd.to_datetime(boundary_text)],
            "分隔線": [f"{label} {boundary_text}"],
        }
    )
    return [
        alt.Chart(boundary_df)
        .mark_rule(color=REPORT_CHART_BOUNDARY, strokeDash=[6, 4], size=1.5)
        .encode(x=alt.X(f"{field_name}:T")),
        alt.Chart(boundary_df)
        .mark_text(align="left", baseline="top", dx=6, dy=6, color=REPORT_CHART_BOUNDARY, fontSize=11, fontWeight="bold")
        .encode(x=alt.X(f"{field_name}:T"), y=alt.value(0), text=alt.Text("分隔線:N")),
    ]


def _style_report_chart(chart: alt.TopLevelMixin, *, height: int) -> alt.TopLevelMixin:
    return (
        chart.properties(height=height, background=REPORT_CHART_BG)
        .configure_view(fill=REPORT_CHART_BG, stroke=REPORT_CHART_BORDER)
        .configure_axis(
            labelColor=REPORT_CHART_AXIS,
            titleColor=REPORT_CHART_AXIS,
            domainColor=REPORT_CHART_BORDER,
            tickColor=REPORT_CHART_BORDER,
            gridColor=REPORT_CHART_GRID,
            gridOpacity=0.75,
            labelFontSize=10,
            titleFontSize=11,
        )
        .configure_legend(
            labelColor=REPORT_CHART_TEXT,
            titleColor=REPORT_CHART_TEXT,
            orient="top",
            labelFontSize=11,
            titleFontSize=11,
            symbolStrokeWidth=2,
        )
    )


def _first_activity_date_int(nav_df: pd.DataFrame) -> int | None:
    if not isinstance(nav_df, pd.DataFrame) or nav_df.empty or "date_int" not in nav_df.columns:
        return None
    pnl_columns = [column for column in ("理論單日損益", "滑價單日損益") if column in nav_df.columns]
    if not pnl_columns:
        return int(nav_df["date_int"].min())
    pnl_frame = nav_df[pnl_columns].apply(pd.to_numeric, errors="coerce").fillna(0.0).abs()
    active_mask = pnl_frame.gt(1e-9).any(axis=1)
    if active_mask.any():
        return int(nav_df.loc[active_mask, "date_int"].min())
    return int(nav_df["date_int"].min())


def _trim_nav_for_chart(nav_df: pd.DataFrame) -> tuple[pd.DataFrame, int | None]:
    if not isinstance(nav_df, pd.DataFrame) or nav_df.empty:
        return pd.DataFrame(), None
    first_activity = _first_activity_date_int(nav_df)
    if first_activity is None:
        return nav_df.copy(), None
    trimmed = nav_df.loc[nav_df["date_int"].astype(int) >= int(first_activity)].copy()
    return trimmed if not trimmed.empty else nav_df.copy(), first_activity


def _trim_weekly_for_chart(weekly_df: pd.DataFrame, *, first_activity: int | None) -> pd.DataFrame:
    if not isinstance(weekly_df, pd.DataFrame) or weekly_df.empty or first_activity is None:
        return weekly_df.copy() if isinstance(weekly_df, pd.DataFrame) else pd.DataFrame()
    chart_df = weekly_df.copy()
    if "週起始日" not in chart_df.columns:
        chart_df["週起始日"] = chart_df["週別"].map(_week_start_from_label)
    chart_df["週起始日"] = pd.to_datetime(chart_df["週起始日"], errors="coerce")
    first_date = pd.to_datetime(_date_label(int(first_activity)), errors="coerce")
    if pd.isna(first_date):
        return chart_df.dropna(subset=["週起始日"])
    first_week = first_date - pd.Timedelta(days=int(first_date.dayofweek))
    chart_df = chart_df.loc[chart_df["週起始日"] >= first_week].copy()
    return chart_df.dropna(subset=["週起始日"])


def _render_nav_and_weekly(
    title: str,
    snapshot: dict[str, Any],
    *,
    boundary_date: int | None = None,
    boundary_label: str = "驗證區起點",
) -> None:
    st.markdown(f"### {title}")
    nav_df, first_activity = _trim_nav_for_chart(_ensure_dataframe(snapshot.get("nav_df")))
    weekly_df = snapshot.get("weekly_df")
    if boundary_date:
        st.caption(f"橘色虛線是分隔線：{boundary_label}；開發區期間以左側選擇為準。")
    if first_activity:
        st.caption(f"圖表從第一筆有損益的日期開始顯示：{_date_label(first_activity)}。")

    st.markdown("#### 資產曲線")
    if isinstance(nav_df, pd.DataFrame) and not nav_df.empty:
        chart_df = nav_df[["日期", "理論淨值", "滑價淨值"]].copy()
        chart_df["日期"] = pd.to_datetime(chart_df["日期"], errors="coerce")
        chart_df = chart_df.dropna(subset=["日期"])
        chart_df = chart_df.melt(id_vars=["日期"], var_name="曲線", value_name="淨值")
        x_axis = alt.Axis(format="%Y/%m", labelAngle=-38, tickCount=7, labelOverlap=True, grid=True, title="日期")
        line = (
            alt.Chart(chart_df)
            .mark_line(point=False, strokeWidth=2.1)
            .encode(
                x=alt.X(
                    "日期:T",
                    title="日期",
                    axis=x_axis,
                ),
                y=alt.Y("淨值:Q", title="累積淨值", axis=alt.Axis(format=",.0f"), scale=alt.Scale(zero=False)),
                color=alt.Color(
                    "曲線:N",
                    scale=alt.Scale(domain=["滑價淨值", "理論淨值"], range=[REPORT_CHART_ACTUAL, REPORT_CHART_THEORY]),
                    legend=alt.Legend(title="", orient="top"),
                ),
                strokeDash=alt.StrokeDash(
                    "曲線:N",
                    scale=alt.Scale(domain=["滑價淨值", "理論淨值"], range=[[1, 0], [6, 4]]),
                    legend=None,
                ),
                tooltip=[
                    alt.Tooltip("日期:T", title="日期", format="%Y-%m-%d"),
                    alt.Tooltip("曲線:N", title="曲線"),
                    alt.Tooltip("淨值:Q", title="淨值", format=",.0f"),
                ],
            )
        )
        chart = alt.layer(line, *_chart_boundary_layers(field_name="日期", boundary_date=boundary_date, label=boundary_label)).properties(
            height=REPORT_NAV_CHART_HEIGHT
        )
        st.altair_chart(_style_report_chart(chart, height=REPORT_NAV_CHART_HEIGHT), width="stretch")
    else:
        st.info("目前沒有可顯示的資產曲線。")

    st.markdown("#### 每週損益")
    if isinstance(weekly_df, pd.DataFrame) and not weekly_df.empty:
        chart_df = _trim_weekly_for_chart(weekly_df, first_activity=first_activity)
        if "週起始日" not in chart_df.columns:
            chart_df["週起始日"] = chart_df["週別"].map(_week_start_from_label)
        chart_df["週起始日"] = pd.to_datetime(chart_df["週起始日"], errors="coerce")
        chart_df = chart_df.dropna(subset=["週起始日"])
        chart_df["方向"] = chart_df["滑價週損益"].map(lambda value: "週獲利" if float(value or 0.0) >= 0.0 else "週虧損")
        zero_df = pd.DataFrame({"基準": [0]})
        bars = (
            alt.Chart(chart_df)
            .mark_bar(size=3)
            .encode(
                x=alt.X(
                    "週起始日:T",
                    title="週別",
                    axis=alt.Axis(format="%Y/%m", labelAngle=-38, tickCount=7, labelOverlap=True, grid=True),
                ),
                y=alt.Y("滑價週損益:Q", title="每週損益", axis=alt.Axis(format=",.0f")),
                color=alt.Color(
                    "方向:N",
                    scale=alt.Scale(domain=["週獲利", "週虧損"], range=[REPORT_CHART_PROFIT, REPORT_CHART_LOSS]),
                    legend=alt.Legend(title="", orient="top"),
                ),
                tooltip=[
                    alt.Tooltip("週別:N", title="週別"),
                    alt.Tooltip("理論週損益:Q", title="理論週損益", format=",.0f"),
                    alt.Tooltip("滑價週損益:Q", title="滑價週損益", format=",.0f"),
                ],
            )
        )
        zero_rule = alt.Chart(zero_df).mark_rule(color="#6b7280", size=1).encode(y=alt.Y("基準:Q"))
        chart = alt.layer(
            zero_rule,
            bars,
            *_chart_boundary_layers(field_name="週起始日", boundary_date=boundary_date, label=boundary_label),
        ).properties(height=REPORT_WEEKLY_CHART_HEIGHT)
        st.altair_chart(_style_report_chart(chart, height=REPORT_WEEKLY_CHART_HEIGHT), width="stretch")
    else:
        st.info("目前沒有可顯示的每週損益。")


def _render_period_compare_table(
    *,
    development_snapshot: dict[str, Any],
    holdout_snapshot: dict[str, Any],
    full_snapshot: dict[str, Any],
) -> None:
    development_metrics = development_snapshot.get("metric_map") or {}
    holdout_metrics = holdout_snapshot.get("metric_map") or {}
    full_metrics = full_snapshot.get("metric_map") or {}

    rows: list[dict[str, Any]] = []
    for metric_name, label in CORE_COMPARE_METRICS:
        rows.append(
            {
                "指標": label,
                "最佳化區理論": _format_metric_value(metric_name, _metric_value(development_metrics, metric_name, "theoretical")),
                "最佳化區滑價": _format_metric_value(metric_name, _metric_value(development_metrics, metric_name, "actual")),
                "驗證區理論": _format_metric_value(metric_name, _metric_value(holdout_metrics, metric_name, "theoretical")),
                "驗證區滑價": _format_metric_value(metric_name, _metric_value(holdout_metrics, metric_name, "actual")),
                "全期間理論": _format_metric_value(metric_name, _metric_value(full_metrics, metric_name, "theoretical")),
                "全期間滑價": _format_metric_value(metric_name, _metric_value(full_metrics, metric_name, "actual")),
            }
        )

    st.markdown("## 三段比較總表")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _render_full_kpi_compare(
    *,
    development_snapshot: dict[str, Any],
    holdout_snapshot: dict[str, Any],
    full_snapshot: dict[str, Any],
) -> None:
    dev_rows = development_snapshot.get("kpi_rows") or []
    holdout_rows = holdout_snapshot.get("kpi_rows") or []
    full_rows = full_snapshot.get("kpi_rows") or []
    if not dev_rows or not holdout_rows or not full_rows:
        return

    holdout_map = {str(row.get("metric")): row for row in holdout_rows}
    full_map = {str(row.get("metric")): row for row in full_rows}
    rows: list[dict[str, Any]] = []
    for row in dev_rows:
        metric_name = str(row.get("metric") or "")
        if not metric_name:
            continue
        holdout_row = holdout_map.get(metric_name) or {}
        full_row = full_map.get(metric_name) or {}
        rows.append(
            {
                "分類": str(row.get("section") or ""),
                "指標": metric_name,
                "最佳化區理論": _format_metric_value(metric_name, row.get("theoretical")),
                "最佳化區滑價": _format_metric_value(metric_name, row.get("actual")),
                "驗證區理論": _format_metric_value(metric_name, holdout_row.get("theoretical")),
                "驗證區滑價": _format_metric_value(metric_name, holdout_row.get("actual")),
                "全期間理論": _format_metric_value(metric_name, full_row.get("theoretical")),
                "全期間滑價": _format_metric_value(metric_name, full_row.get("actual")),
            }
        )

    st.markdown("### 完整 KPI 對照")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, height=680)


def _render_trade_table(title: str, trade_df: pd.DataFrame, *, height: int = 420) -> None:
    st.markdown(f"### {title}")
    if trade_df.empty:
        st.info("目前沒有逐筆交易資料。")
        return
    st.dataframe(trade_df, width="stretch", hide_index=True, height=height)


def _render_strategy_family_tab(
    *,
    view: dict[str, Any],
    params_meta: list[dict[str, Any]],
    minute_path: str,
    daily_path: str,
    script_name: str,
    capital: int,
    slip_per_side: float,
    research_periods: dict[str, Any],
    development_snapshot: dict[str, Any],
    holdout_snapshot: dict[str, Any],
    full_snapshot: dict[str, Any],
) -> None:
    st.markdown("### 參數家族等權組合")
    st.caption("先測同一策略的前幾名參數能否組成比較穩的家族，避免把決策押在單一最佳參數。")

    top_df = _ensure_dataframe(view.get("current_top_df"))
    if top_df.empty:
        st.info("目前沒有最佳候選清單，請先完成一輪最佳化。")
        return

    max_members = min(10, len(top_df))
    if max_members < 2:
        st.info("候選組合少於 2 組，暫時無法做參數家族比較。")
        return

    default_members = min(5, max_members)
    member_count = st.slider(
        "組合前幾名參數",
        min_value=2,
        max_value=max_members,
        value=default_members,
        step=1,
        key="mq0101_v2_family_member_count",
    )
    cols = st.columns((1.0, 2.0))
    run_family = cols[0].button("計算等權組合", type="primary", width="stretch", key="mq0101_v2_family_run")
    cols[1].caption("做法：Top N 每組分配相同資金權重，各自回測後把每日損益相加，再看三段結果。")
    if run_family:
        st.session_state["mq0101_v2_family_ready_n"] = int(member_count)

    ready_count = int(st.session_state.get("mq0101_v2_family_ready_n") or 0)
    if ready_count <= 0:
        st.info("按下「計算等權組合」後，這裡會比較單一最佳與 Top N 等權組合。")
        return

    candidates = _top_candidate_params(top_df, params_meta, limit=ready_count)
    if len(candidates) < 2:
        st.warning("可用候選參數不足，無法組合。")
        return

    params_list_json = json.dumps(candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with st.spinner(f"正在回測 Top {len(candidates)} 等權參數家族..."):
        combo_development = _cached_param_family_period_analysis(
            params_list_json=params_list_json,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            capital=int(capital),
            slip_per_side=float(slip_per_side),
            start_date=int(research_periods["development_start_date"]),
            end_date=int(research_periods["development_end_date"]),
        )
        combo_holdout = _cached_param_family_period_analysis(
            params_list_json=params_list_json,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            capital=int(capital),
            slip_per_side=float(slip_per_side),
            start_date=int(research_periods["holdout_start_date"]),
            end_date=int(research_periods["holdout_end_date"]),
        )
        combo_full = _cached_param_family_period_analysis(
            params_list_json=params_list_json,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            capital=int(capital),
            slip_per_side=float(slip_per_side),
            start_date=int(research_periods["development_start_date"]),
            end_date=int(research_periods["holdout_end_date"]),
        )

    if not combo_development or not combo_holdout or not combo_full:
        st.warning("參數家族回測沒有產生完整結果。")
        return

    compare_rows = []
    compare_rows.extend(
        _strategy_family_summary_rows(
            "單一最佳",
            [
                ("最佳化區", development_snapshot),
                ("驗證區", holdout_snapshot),
                ("全期間", full_snapshot),
            ],
        )
    )
    compare_rows.extend(
        _strategy_family_summary_rows(
            f"Top {len(candidates)} 等權",
            [
                ("最佳化區", combo_development),
                ("驗證區", combo_holdout),
                ("全期間", combo_full),
            ],
        )
    )
    st.dataframe(pd.DataFrame(compare_rows), width="stretch", hide_index=True)

    holdout_single_profit = _metric_value(holdout_snapshot.get("metric_map") or {}, "總淨利 Net Profit", "actual")
    holdout_combo_profit = _metric_value(combo_holdout.get("metric_map") or {}, "總淨利 Net Profit", "actual")
    holdout_single_mdd = _metric_value(holdout_snapshot.get("metric_map") or {}, "最大回撤率 Max Drawdown %", "actual")
    holdout_combo_mdd = _metric_value(combo_holdout.get("metric_map") or {}, "最大回撤率 Max Drawdown %", "actual")
    if holdout_combo_profit is not None and holdout_single_profit is not None:
        profit_delta = float(holdout_combo_profit) - float(holdout_single_profit)
        mdd_delta = None
        if holdout_combo_mdd is not None and holdout_single_mdd is not None:
            mdd_delta = float(holdout_combo_mdd) - float(holdout_single_mdd)
        if profit_delta > 0 and (mdd_delta is None or mdd_delta <= 0):
            st.success("參數家族在驗證區同時改善淨利且沒有增加 MDD，值得進一步做策略變體測試。")
        elif profit_delta > 0:
            st.info("參數家族提高了驗證區淨利，但 MDD 也可能增加，要看風險是否能接受。")
        else:
            st.warning("參數家族沒有改善驗證區淨利，代表問題可能在策略邏輯，不只是單一參數。")

    member_df = _ensure_dataframe(combo_full.get("member_df"))
    if not member_df.empty:
        member_df = member_df.copy()
        for column in ("組合貢獻滑價淨利", "單體滑價淨利", "單體理論淨利"):
            if column in member_df.columns:
                member_df[column] = member_df[column].map(lambda value: f"{float(value):,.0f}")
        with st.expander("查看組合成員", expanded=False):
            st.dataframe(member_df, width="stretch", hide_index=True, height=260)
            st.dataframe(pd.DataFrame(candidates), width="stretch", hide_index=True, height=260)

    _render_nav_and_weekly(
        f"Top {len(candidates)} 等權組合圖表",
        combo_full,
        boundary_date=int(research_periods["holdout_start_date"]),
        boundary_label="驗證區起點",
    )


def _render_top_candidate_holdout_comparison(
    *,
    view: dict[str, Any],
    params_meta: list[dict[str, Any]],
    minute_path: str,
    daily_path: str,
    script_name: str,
    capital: int,
    slip_per_side: float,
    research_periods: dict[str, Any],
    candidate_count: int = 3,
) -> None:
    top_df = _ensure_dataframe(view.get("current_top_df"))
    candidates = _top_candidate_params(top_df, params_meta, limit=int(candidate_count))
    if len(candidates) < 2:
        return

    rows: list[dict[str, Any]] = []
    with st.spinner(f"正在比對 Top {len(candidates)} 候選的驗證區表現..."):
        for rank, params in enumerate(candidates, start=1):
            params_json = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            snapshot = _cached_period_analysis(
                params_json=params_json,
                minute_path=minute_path,
                daily_path=daily_path,
                script_name=script_name,
                capital=int(capital),
                slip_per_side=float(slip_per_side),
                start_date=int(research_periods["holdout_start_date"]),
                end_date=int(research_periods["holdout_end_date"]),
            )
            metric_map = snapshot.get("metric_map") or {}
            param_text = " / ".join(f"{name}={base._format_number(value, 4)}" for name, value in list(params.items())[:8])
            if len(params) > 8:
                param_text += " / ..."
            rows.append(
                {
                    "名次": rank,
                    "驗證區滑價淨利": _format_metric_value(
                        "總淨利 Net Profit",
                        _metric_value(metric_map, "總淨利 Net Profit", "actual"),
                    ),
                    "驗證區 MDD": _format_metric_value(
                        "最大回撤率 Max Drawdown %",
                        _metric_value(metric_map, "最大回撤率 Max Drawdown %", "actual"),
                    ),
                    "驗證區 PF": _format_metric_value(
                        "獲利因子 Profit Factor",
                        _metric_value(metric_map, "獲利因子 Profit Factor", "actual"),
                    ),
                    "交易數": _format_metric_value(
                        "交易筆數 #Trades",
                        _metric_value(metric_map, "交易筆數 #Trades", "actual"),
                    ),
                    "參數": param_text,
                }
            )

    st.markdown("### Top 3 候選驗證區驗收")
    st.caption("同一輪最佳化至少保留 3 組策略參數，先看它們在驗證區是否分歧。")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, height=180)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        numeric = float(value)
    except Exception:
        return None
    if pd.isna(numeric):
        return None
    return numeric


def _candidate_validation_verdict(
    *,
    net_profit: float | None,
    mdd: float | None,
    profit_factor: float | None,
    trade_count: float | None,
) -> str:
    if trade_count is not None and trade_count < 3:
        return "樣本少"
    if net_profit is None:
        return "無資料"
    if net_profit <= 0:
        return "淘汰"
    if profit_factor is not None and profit_factor < 1.05:
        return "觀察"
    if mdd is not None and mdd > 0.18:
        return "觀察"
    return "通過"


def _candidate_period_row(
    *,
    rank: int,
    window: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    metric_map = snapshot.get("metric_map") or {}
    net_profit = _float_or_none(_metric_value(metric_map, "總淨利 Net Profit", "actual"))
    total_return = _float_or_none(_metric_value(metric_map, "總報酬率 Total Return", "actual"))
    mdd = _float_or_none(_metric_value(metric_map, "最大回撤率 Max Drawdown %", "actual"))
    profit_factor = _float_or_none(_metric_value(metric_map, "獲利因子 Profit Factor", "actual"))
    metric_trades = _float_or_none(_metric_value(metric_map, "交易筆數 #Trades", "actual"))
    trade_count = metric_trades if metric_trades is not None else _float_or_none(snapshot.get("trade_count"))
    verdict = _candidate_validation_verdict(
        net_profit=net_profit,
        mdd=mdd,
        profit_factor=profit_factor,
        trade_count=trade_count,
    )
    period_label = str(window.get("label") or snapshot.get("period_label") or "")
    if bool(window.get("is_partial")):
        period_label += "（不足 1 年）"
    return {
        "候選": f"Top {rank}",
        "區段": period_label,
        "滑價淨利": _format_metric_value("總淨利 Net Profit", net_profit),
        "滑價報酬率": _format_metric_value("總報酬率 Total Return", total_return),
        "MDD": _format_metric_value("最大回撤率 Max Drawdown %", mdd),
        "PF": _format_metric_value("獲利因子 Profit Factor", profit_factor),
        "交易數": "--" if trade_count is None else f"{int(round(float(trade_count))):,}",
        "判定": verdict,
        "_trade_count": float(trade_count or 0.0),
        "_net_profit": float(net_profit or 0.0),
        "_return_pct": float(total_return or 0.0) * 100.0,
        "_mdd_pct": float(mdd or 0.0) * 100.0,
    }


def _multiperiod_validation_rows(
    *,
    candidates: list[dict[str, int | float]],
    minute_path: str,
    daily_path: str,
    script_name: str,
    capital: int,
    slip_per_side: float,
    research_periods: dict[str, Any],
) -> list[dict[str, Any]]:
    windows = _backward_year_windows(
        start_date=int(research_periods["development_start_date"]),
        end_date=int(research_periods["holdout_end_date"]),
    )
    rows: list[dict[str, Any]] = []
    for rank, params in enumerate(candidates, start=1):
        params_json = json.dumps(params, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        for window in windows:
            snapshot = _cached_period_analysis(
                params_json=params_json,
                minute_path=minute_path,
                daily_path=daily_path,
                script_name=script_name,
                capital=int(capital),
                slip_per_side=float(slip_per_side),
                start_date=int(window["start_date"]),
                end_date=int(window["end_date"]),
            )
            rows.append(_candidate_period_row(rank=rank, window=window, snapshot=snapshot))

    if not rows:
        return []
    counts_by_period: dict[str, float] = defaultdict(float)
    for row in rows:
        counts_by_period[str(row["區段"])] += float(row.get("_trade_count") or 0.0)
    return [row for row in rows if counts_by_period.get(str(row["區段"]), 0.0) > 0.0]


def _render_top_candidate_multiperiod_validation(
    *,
    view: dict[str, Any],
    params_meta: list[dict[str, Any]],
    minute_path: str,
    daily_path: str,
    script_name: str,
    capital: int,
    slip_per_side: float,
    research_periods: dict[str, Any],
    candidate_count: int = 3,
) -> None:
    st.markdown("### Top 3 多段驗收")
    st.caption("把同一輪保留下來的 Top 3 參數逐段回測，避免只因單一驗證區剛好漂亮就下判斷。")

    top_df = _ensure_dataframe(view.get("current_top_df"))
    candidates = _top_candidate_params(top_df, params_meta, limit=int(candidate_count))
    if len(candidates) < 2:
        st.info("候選參數不足，請先完成至少 3 組候選的最佳化。")
        return

    candidate_signature = json.dumps(candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if st.button("計算 Top 3 多段驗收", type="primary", key="mq0101_v2_multiperiod_run"):
        st.session_state["mq0101_v2_multiperiod_signature"] = candidate_signature

    if st.session_state.get("mq0101_v2_multiperiod_signature") != candidate_signature:
        st.info("按下「計算 Top 3 多段驗收」後，這裡會產生逐年熱圖與明細表。")
        return

    with st.spinner(f"正在回測 Top {len(candidates)} 候選的多段驗收..."):
        rows = _multiperiod_validation_rows(
            candidates=candidates,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            capital=int(capital),
            slip_per_side=float(slip_per_side),
            research_periods=research_periods,
        )
    if not rows:
        st.warning("多段驗收沒有產生可用交易資料。")
        return

    validation_df = pd.DataFrame(rows)
    chart_df = validation_df.copy()
    chart_df["滑價報酬率(%)"] = pd.to_numeric(chart_df["_return_pct"], errors="coerce")
    chart_df["熱圖文字"] = chart_df["滑價報酬率(%)"].map(lambda value: "--" if pd.isna(value) else f"{float(value):.1f}%")
    heatmap = (
        alt.Chart(chart_df)
        .mark_rect(stroke="#ffffff", strokeWidth=1)
        .encode(
            x=alt.X("區段:N", title="驗收區段", sort=list(dict.fromkeys(chart_df["區段"].tolist())), axis=alt.Axis(labelAngle=-30)),
            y=alt.Y("候選:N", title="候選參數", sort=[f"Top {index}" for index in range(1, len(candidates) + 1)]),
            color=alt.Color(
                field="滑價報酬率(%)",
                type="quantitative",
                title="滑價報酬率",
                scale=alt.Scale(scheme="redyellowgreen"),
            ),
            tooltip=[
                alt.Tooltip("候選:N", title="候選"),
                alt.Tooltip("區段:N", title="區段"),
                alt.Tooltip("滑價淨利:N", title="滑價淨利"),
                alt.Tooltip("滑價報酬率:N", title="滑價報酬率"),
                alt.Tooltip("MDD:N", title="MDD"),
                alt.Tooltip("PF:N", title="PF"),
                alt.Tooltip("交易數:N", title="交易數"),
                alt.Tooltip("判定:N", title="判定"),
            ],
        )
    )
    labels = (
        alt.Chart(chart_df)
        .mark_text(color=REPORT_CHART_TEXT, fontSize=12)
        .encode(
            x=alt.X("區段:N", sort=list(dict.fromkeys(chart_df["區段"].tolist()))),
            y=alt.Y("候選:N", sort=[f"Top {index}" for index in range(1, len(candidates) + 1)]),
            text=alt.Text("熱圖文字:N"),
        )
    )
    st.altair_chart(_style_report_chart(heatmap + labels, height=260), width="stretch")

    display_columns = ["候選", "區段", "滑價淨利", "滑價報酬率", "MDD", "PF", "交易數", "判定"]
    st.dataframe(validation_df[display_columns], width="stretch", hide_index=True, height=320)

    pass_counts = (
        validation_df.groupby("候選")["判定"]
        .apply(lambda values: sum(1 for value in values if value == "通過"))
        .reset_index(name="通過段數")
    )
    best_count = int(pass_counts["通過段數"].max()) if not pass_counts.empty else 0
    if best_count <= 1:
        st.warning("Top 3 在多段驗收中沒有形成穩定通過，先不要只靠單一驗證區結果決策。")
    else:
        st.info("看熱圖時，優先找多個時間段都偏綠、且不是只有一段暴衝的候選。")


def _param_stability_source(top_df: pd.DataFrame, params_meta: list[dict[str, Any]]) -> tuple[pd.DataFrame, list[str]]:
    source_df = _ensure_dataframe(top_df).copy()
    if source_df.empty:
        return pd.DataFrame(), []

    param_names = [
        str(meta.get("name"))
        for meta in params_meta
        if str(meta.get("name") or "") and str(meta.get("name")) in source_df.columns and str(meta.get("name")) not in FIXED_PARAM_SPECS
    ]
    if not param_names:
        return pd.DataFrame(), []

    output = source_df.head(30).copy()
    output["候選"] = [f"#{index}" for index in range(1, len(output) + 1)]
    for column in param_names + ["robust_score", "total_return", "mdd_pct", "n_trades", "year_avg_return", "slip_stress_score"]:
        if column in output.columns:
            output[column] = pd.to_numeric(output[column], errors="coerce")
    return output, param_names


def _render_param_stability_map(*, view: dict[str, Any], params_meta: list[dict[str, Any]]) -> None:
    st.markdown("### 參數穩定區圖")
    st.caption("用目前最佳候選群看參數是否集中在一片穩定區，還是只有孤立的一個尖峰。")

    source_df, param_names = _param_stability_source(_ensure_dataframe(view.get("current_top_df")), params_meta)
    if source_df.empty or not param_names:
        st.info("目前沒有足夠候選資料可以畫參數穩定區。")
        return

    diverse_params = [name for name in param_names if source_df[name].nunique(dropna=True) > 1]
    usable_params = diverse_params or param_names
    if len(source_df) < 6:
        st.info("目前保留候選較少，圖形只能看 Top 候選分布；若要看真正穩定區，下一輪可把保留前幾名暫時調高到 10。")

    metric_options = [
        ("robust_score", "穩健分數"),
        ("total_return", "總報酬率"),
        ("mdd_pct", "MDD"),
        ("n_trades", "交易數"),
        ("year_avg_return", "年均報酬"),
        ("slip_stress_score", "滑價壓測分數"),
    ]
    metric_options = [(key, label) for key, label in metric_options if key in source_df.columns and source_df[key].notna().any()]
    if not metric_options:
        st.info("候選資料沒有可用的評分欄位。")
        return

    controls = st.columns(3)
    x_param = controls[0].selectbox("X 參數", usable_params, key="mq0101_v2_stability_x")
    y_choices = [name for name in usable_params if name != x_param]
    if not y_choices:
        y_choices = [x_param]
    y_param = controls[1].selectbox("Y 參數", y_choices, key="mq0101_v2_stability_y")
    metric_key = controls[2].selectbox(
        "顏色指標",
        [key for key, _label in metric_options],
        format_func=lambda key: dict(metric_options).get(key, key),
        key="mq0101_v2_stability_metric",
    )
    metric_label = dict(metric_options).get(metric_key, metric_key)

    chart_df = source_df.copy()
    chart_df[metric_label] = pd.to_numeric(chart_df[metric_key], errors="coerce")
    if "n_trades" not in chart_df.columns:
        chart_df["n_trades"] = 1
    chart_df = chart_df.dropna(subset=[x_param, y_param, metric_label])
    if chart_df.empty:
        st.info("選定的參數組合沒有足夠數值資料。")
        return

    scatter = (
        alt.Chart(chart_df)
        .mark_circle(size=130, opacity=0.88, stroke="#ffffff", strokeWidth=0.8)
        .encode(
            x=alt.X(f"{x_param}:Q", title=x_param, scale=alt.Scale(zero=False)),
            y=alt.Y(f"{y_param}:Q", title=y_param, scale=alt.Scale(zero=False)),
            color=alt.Color(f"{metric_label}:Q", title=metric_label, scale=alt.Scale(scheme="redyellowgreen")),
            size=alt.Size("n_trades:Q", title="交易數", scale=alt.Scale(range=[70, 260])),
            tooltip=[
                alt.Tooltip("候選:N", title="候選"),
                alt.Tooltip(f"{x_param}:Q", title=x_param),
                alt.Tooltip(f"{y_param}:Q", title=y_param),
                alt.Tooltip(f"{metric_label}:Q", title=metric_label, format=".2f"),
                alt.Tooltip("n_trades:Q", title="交易數", format=",.0f"),
            ],
        )
    )
    st.altair_chart(_style_report_chart(scatter, height=380), width="stretch")

    summary_rows = []
    for param_name in usable_params[:8]:
        values = pd.to_numeric(source_df[param_name], errors="coerce").dropna()
        if values.empty:
            continue
        summary_rows.append(
            {
                "參數": param_name,
                "候選不同值": int(values.nunique()),
                "最小": base._format_number(values.min(), 4),
                "最大": base._format_number(values.max(), 4),
                "中位數": base._format_number(values.median(), 4),
            }
        )
    if summary_rows:
        st.dataframe(pd.DataFrame(summary_rows), width="stretch", hide_index=True, height=240)


def _render_runtime_overview(*, view: dict[str, Any], system_snapshot: dict[str, Any]) -> None:
    elapsed_seconds = float(view.get("elapsed_seconds") or 0.0)
    tested_per_minute = ((float(view.get("done") or 0) / elapsed_seconds) * 60.0) if elapsed_seconds > 1e-9 else None
    passed_per_minute = ((float(view.get("passed") or 0) / elapsed_seconds) * 60.0) if elapsed_seconds > 1e-9 else None
    average_seconds_per_test = (elapsed_seconds / float(view.get("done") or 0)) if float(view.get("done") or 0) > 0 else None

    st.markdown("## 即時執行監控")
    monitor_row1 = st.columns(5)
    monitor_row1[0].metric("CPU 即時使用率", _format_runtime_metric(system_snapshot.get("cpu_pct"), suffix="%", precision=1))
    monitor_row1[1].metric("記憶體即時使用率", _format_runtime_metric(system_snapshot.get("memory_pct"), suffix="%", precision=1))
    monitor_row1[2].metric("背景程序負載", _format_runtime_metric(system_snapshot.get("worker_load_pct"), suffix="%", precision=1))
    monitor_row1[3].metric("平均測試組數/分鐘", _format_runtime_metric(tested_per_minute, precision=1))
    monitor_row1[4].metric("平均通過組數/分鐘", _format_runtime_metric(passed_per_minute, precision=1))

    monitor_row2 = st.columns(5)
    monitor_row2[0].metric("配置背景程序數", f"{int(system_snapshot.get('configured_workers') or 0):,}")
    monitor_row2[1].metric("可用 CPU 核心數", f"{int(system_snapshot.get('effective_cpu_count') or 0):,}")
    monitor_row2[2].metric(
        "記憶體已用 / 總量",
        f"{_format_runtime_metric(system_snapshot.get('memory_used_gb'), precision=1)} / {_format_runtime_metric(system_snapshot.get('memory_total_gb'), precision=1)} GB",
    )
    monitor_row2[3].metric("平均每組秒數", _format_runtime_metric(average_seconds_per_test, suffix=" 秒", precision=2))
    monitor_row2[4].metric("目前步驟", str(view.get("step_note") or "--"))


def _render_period_overview(
    *,
    current_snapshot: dict[str, Any],
    historical_snapshot: dict[str, Any],
    research_periods: dict[str, Any],
    system_snapshot: dict[str, Any],
    mode: str,
    heading: str = "最佳化區結果",
    caption: str = "右側改成單頁工作流：先看最佳化區，再決定是否測試驗證區，最後檢查全期間。",
) -> None:
    st.markdown(f"## {heading}")
    st.caption(caption)

    current_primary_score = current_snapshot.get("robust_score", current_snapshot.get("composite_score"))
    historical_primary_score = historical_snapshot.get("robust_score", historical_snapshot.get("composite_score"))
    cards = st.columns(6)
    cards[0].metric("最佳化區期間", str(research_periods.get("development_label") or "--"))
    cards[1].metric("驗證區期間", str(research_periods.get("holdout_label") or "--"))
    cards[2].metric("穩健分數", base._format_number(current_primary_score))
    cards[3].metric("歷史最佳穩健分數", base._format_number(historical_primary_score))
    cards[4].metric("總報酬", base._format_percent(current_snapshot.get("total_return")))
    cards[5].metric("MDD", base._format_percent(current_snapshot.get("mdd_pct")))

    cards2 = st.columns(6)
    cards2[0].metric("交易筆數", base._format_number(current_snapshot.get("n_trades"), 0))
    cards2[1].metric("年均報酬", base._format_percent(current_snapshot.get("year_avg_return")))
    cards2[2].metric("單邊滑價", f"{base._format_number(current_snapshot.get('slip_per_side') or 2.0, 1)} 點")
    cards2[3].metric("搜尋模式", base.MODE_OPTIONS.get(mode, mode))
    cards2[4].metric("有效背景程序數", f"{int(system_snapshot.get('configured_workers') or 0):,}")
    cards2[5].metric(
        "CPU / 記憶體上限",
        f"{system_snapshot.get('cpu_limit_pct', '--')}% / {system_snapshot.get('memory_limit_pct', '--')}%",
    )

    params = current_snapshot.get("params") if isinstance(current_snapshot, dict) else {}
    if isinstance(params, dict) and params:
        param_text = " / ".join(f"{name}={base._format_number(value, 4)}" for name, value in list(params.items())[:8])
        if len(params) > 8:
            param_text += " / ..."
        st.caption(f"最佳參數：{param_text}")
    if current_snapshot.get("worst_window_return") is not None or current_snapshot.get("slip_stress_score") is not None:
        st.caption(
            "防過度最佳化排名已啟用：會一起參考開發區年度表現、2/3/4 點滑價壓力，以及鄰近參數平台穩定度。"
        )


def _mdd_sweep_enabled(hard_filters: dict[str, Any]) -> bool:
    return str(hard_filters.get("mdd_mode") or "fixed") == "sweep"


def _render_mdd_sweep_summary(*, top_df: pd.DataFrame, hard_filters: dict[str, Any]) -> None:
    if not _mdd_sweep_enabled(hard_filters) or not isinstance(top_df, pd.DataFrame) or top_df.empty:
        return
    summary_df = build_mdd_sweep_summary(top_df.to_dict("records"), hard_filters)
    if summary_df.empty:
        return
    st.markdown("### MDD 門檻批次結果")
    st.caption("同一批策略結果依 MDD 門檻分層；MDD 仍是硬過濾，超過範圍上限的組合會淘汰。")
    display_df = summary_df.drop(columns=["_threshold"], errors="ignore")
    st.dataframe(display_df, width="stretch", hide_index=True, height=260)


def _wfo_result_signature(
    *,
    mode: str,
    ui_specs: list[dict[str, Any]],
    runtime_settings: dict[str, Any],
    hard_filters: dict[str, Any],
    minute_path: str,
    daily_path: str,
    script_name: str,
    research_periods: dict[str, Any],
    wfo_settings: dict[str, int],
) -> str:
    payload = {
        "mode": mode,
        "ui_specs": ui_specs,
        "runtime_settings": {
            key: runtime_settings.get(key)
            for key in (
                "capital",
                "slip_per_side",
                "max_workers",
                "top_n",
                "seed_keep_count",
            )
        },
        "hard_filters": hard_filters,
        "minute_path": minute_path,
        "daily_path": daily_path,
        "script_name": script_name,
        "research_periods": {
            key: research_periods.get(key)
            for key in (
                "development_start_date",
                "development_end_date",
                "holdout_start_date",
                "holdout_end_date",
                "research_profile_tag",
            )
        },
        "wfo_settings": wfo_settings,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))


def _render_wfo_result(result: dict[str, Any]) -> None:
    summary = dict(result.get("summary") or {})
    cols = st.columns(5)
    cols[0].metric("總輪數", f"{int(summary.get('total_folds') or 0):,}")
    cols[1].metric("通過輪數", f"{int(summary.get('passed_folds') or 0):,}")
    cols[2].metric("失敗輪數", f"{int(summary.get('failed_folds') or 0):,}")
    cols[3].metric("平均 OOS 報酬", f"{float(summary.get('avg_oos_return_pct') or 0.0):.2f}%")
    cols[4].metric("平均 OOS MDD", f"{float(summary.get('avg_oos_mdd_pct') or 0.0):.2f}%")
    extra_cols = st.columns(4)
    extra_cols[0].metric("最差一輪報酬", f"{float(summary.get('worst_fold_return_pct') or 0.0):.2f}%")
    extra_cols[1].metric("最差一輪 MDD", f"{float(summary.get('worst_fold_mdd_pct') or 0.0):.2f}%")
    extra_cols[2].metric("OOS 穩定度", f"{float(summary.get('oos_stability_score') or 0.0):.1f}")
    extra_cols[3].metric("通過率", f"{float(summary.get('pass_rate') or 0.0) * 100.0:.1f}%")

    worst_fold = str(summary.get("worst_fold") or "--")
    worst_return = float(summary.get("worst_fold_return_pct") or 0.0)
    st.caption(f"最差一輪：{worst_fold}，OOS 報酬 {worst_return:.2f}% 。")

    fold_df = pd.DataFrame(list(result.get("fold_rows") or []))
    if not fold_df.empty:
        st.dataframe(fold_df, width="stretch", hide_index=True, height=280)

    folds = list(result.get("folds") or [])
    if folds:
        with st.expander("查看各輪 Top N 候選 OOS 明細", expanded=False):
            for fold in folds:
                candidate_rows = list(fold.get("top_candidates") or [])
                st.markdown(f"#### {fold.get('label', '--')}：{fold.get('train_label', '--')} -> {fold.get('test_label', '--')}")
                if not candidate_rows:
                    st.info("此輪沒有通過硬條件的候選。")
                    continue
                candidate_df = pd.DataFrame(candidate_rows)
                display_columns = [
                    "排名",
                    "參數摘要",
                    "train_total_return_pct",
                    "train_mdd_pct",
                    "train_trade_count",
                    "train_robust_score",
                    "oos_return_pct",
                    "oos_mdd_pct",
                    "oos_trade_count",
                    "oos_profit_factor",
                    "是否通過最低門檻",
                ]
                display_columns = [column for column in display_columns if column in candidate_df.columns]
                st.dataframe(candidate_df[display_columns], width="stretch", hide_index=True, height=180)


def _render_multi_wfo_panel(
    *,
    mode: str,
    ui_specs: list[dict[str, Any]],
    params_meta: list[dict[str, Any]],
    runtime_settings: dict[str, Any],
    hard_filters: dict[str, Any],
    minute_path: str,
    daily_path: str,
    script_name: str,
    research_periods: dict[str, Any],
    wfo_settings: dict[str, int],
) -> dict[str, Any] | None:
    st.divider()
    st.markdown("## 步驟 3：多輪 WFO 驗證")
    st.caption("先完成單次 OOS，再按這裡。每一輪都會重新切 train / test，並把 gap 一起算進去。")

    signature = _wfo_result_signature(
        mode=mode,
        ui_specs=ui_specs,
        runtime_settings=runtime_settings,
        hard_filters=hard_filters,
        minute_path=minute_path,
        daily_path=daily_path,
        script_name=script_name,
        research_periods=research_periods,
        wfo_settings=wfo_settings,
    )
    cached_signature = str(st.session_state.get(WFO_SIGNATURE_SESSION_KEY) or "")
    cached_result = st.session_state.get(WFO_RESULT_SESSION_KEY) if cached_signature == signature else None
    auto_run_wfo = bool(st.session_state.get(AUTO_RUN_WFO_KEY, False))

    setting_text = (
        f"訓練窗 {int(wfo_settings['train_years'])} 年 / "
        f"測試窗 {int(wfo_settings['test_years'])} 年 / "
        f"步長 {int(wfo_settings['step_years'])} 年 / "
        f"gap {int(wfo_settings.get('gap_days', 0))} 天 / "
        f"Top N {int(runtime_settings.get('top_n') or 0)}"
    )
    st.caption(setting_text)

    run_clicked = st.button(
        "步驟 3：計算多輪 WFO",
        width="stretch",
        key="mq0101_v2_run_wfo",
        disabled=not bool(ui_specs),
    )
    should_run = bool(run_clicked) or bool(auto_run_wfo and not (isinstance(cached_result, dict) and cached_result))
    if should_run:
        with st.spinner("正在計算多輪 WFO；每一輪都會重新跑訓練窗最佳化，時間會比單次驗證長。"):
            result = run_0101_multi_wfo_validation(
                mode=mode,
                ui_param_specs=ui_specs,
                params_meta=params_meta,
                runtime_settings=runtime_settings,
                hard_filters=hard_filters,
                minute_path=minute_path,
                daily_path=daily_path,
                script_name=script_name,
                start_date=int(research_periods["development_start_date"]),
                end_date=int(research_periods["holdout_end_date"]),
                train_years=int(wfo_settings["train_years"]),
                test_years=int(wfo_settings["test_years"]),
                step_years=int(wfo_settings["step_years"]),
                gap_days=int(wfo_settings.get("gap_days", 0)),
            )
        st.session_state[WFO_SIGNATURE_SESSION_KEY] = signature
        st.session_state[WFO_RESULT_SESSION_KEY] = result
        cached_result = result
        st.session_state[AUTO_RUN_WFO_KEY] = False
    elif auto_run_wfo and isinstance(cached_result, dict) and cached_result:
        st.session_state[AUTO_RUN_WFO_KEY] = False

    if isinstance(cached_result, dict) and cached_result:
        _render_wfo_result(cached_result)
        return cached_result
    else:
        st.info("尚未計算多輪 WFO。完成第 2 層後，第 3 層 gap、防污染、第 4 層 PBO / DSR，以及第 5 層 Forward Test 才會逐步開放。")
    return None


def _risk_level_from_value(value: float, *, low_max: float, medium_max: float) -> str:
    if value <= low_max:
        return "低"
    if value <= medium_max:
        return "中"
    return "高"


def _approx_pbo_dsr_report(
    *,
    view: dict[str, Any],
    params_meta: list[dict[str, Any]],
    current_snapshot: dict[str, Any],
    development_snapshot: dict[str, Any],
    holdout_snapshot: dict[str, Any] | None,
    qualification_report: dict[str, Any],
    holdout_validation: dict[str, Any] | None,
    wfo_result: dict[str, Any] | None,
) -> dict[str, Any]:
    risk_report = _overfit_risk_report(
        view=view,
        params_meta=params_meta,
        current_snapshot=current_snapshot,
        development_snapshot=development_snapshot,
        holdout_snapshot=holdout_snapshot,
        qualification_report=qualification_report,
        holdout_validation=holdout_validation,
    )
    pbo = float(risk_report.get("score") or 0.0) / 100.0
    if isinstance(wfo_result, dict) and wfo_result:
        summary = dict(wfo_result.get("summary") or {})
        pbo = min(1.0, max(0.0, pbo * 0.65 + (1.0 - float(summary.get("pass_rate") or 0.0)) * 0.35))
    pbo_level = _risk_level_from_value(pbo, low_max=0.25, medium_max=0.55)

    base_sharpe = float((holdout_validation or {}).get("sharpe") or 0.0)
    trial_count = max(1, int(view.get("done") or view.get("total") or 1))
    top_count = max(1, len(_ensure_dataframe(view.get("current_top_df"))))
    dsr = float(base_sharpe - 0.15 * math.log10(trial_count) - 0.05 * math.log(top_count))
    if isinstance(wfo_result, dict) and wfo_result:
        dsr = float((dsr + float((wfo_result.get("summary") or {}).get("avg_oos_sharpe") or 0.0)) / 2.0)
    if dsr >= 1.0:
        dsr_level = "高"
    elif dsr >= 0.3:
        dsr_level = "中"
    else:
        dsr_level = "低"
    return {
        "pbo": pbo,
        "pbo_level": pbo_level,
        "dsr": dsr,
        "dsr_level": dsr_level,
        "risk_report": risk_report,
        "definition": "PBO / DSR 目前是可落地近似版：PBO 由既有過度最佳化風險分數與 WFO 失敗率折算；DSR 由 Sharpe 扣除搜尋次數與候選數懲罰。",
    }


def _single_oos_summary_rows(holdout_validation: dict[str, Any] | None, research_periods: dict[str, Any]) -> list[dict[str, Any]]:
    if not holdout_validation:
        return []
    return [
        {
            "訓練期間": research_periods.get("development_label", "--"),
            "測試期間": research_periods.get("holdout_label", "--"),
            "單次 OOS 報酬": f"{float(holdout_validation.get('total_return_pct') or 0.0):.2f}%",
            "單次 OOS 最大回撤": f"{float(holdout_validation.get('mdd_pct') or 0.0):.2f}%",
            "單次 OOS 勝率": f"{float(holdout_validation.get('win_rate_pct') or 0.0):.2f}%",
            "單次 OOS 交易數": f"{int(holdout_validation.get('trade_count') or 0):,}",
            "是否達最低門檻": _verdict_label(str(holdout_validation.get("final_verdict") or "REJECT")),
        }
    ]


def _yes_no_flag(value: Any) -> bool:
    return str(value or "").strip() in {"是", "Y", "YES", "True", "true", "1"}


def _current_forward_test_snapshot(
    *,
    current_snapshot: dict[str, Any],
    historical_snapshot: dict[str, Any],
) -> dict[str, Any]:
    entry = load_latest_forward_test_entry()
    if not entry:
        return {}

    current_names = {
        str(current_snapshot.get("strategy_signature") or "").strip(),
        str(current_snapshot.get("saved_at") or "").strip(),
    }
    current_names = {name for name in current_names if name}
    baseline_names = {
        str(historical_snapshot.get("strategy_signature") or "").strip(),
        str(historical_snapshot.get("saved_at") or "").strip(),
    }
    baseline_names = {name for name in baseline_names if name}

    challenger_name = str(entry.get("Challenger 名稱") or "").strip()
    baseline_name = str(entry.get("Baseline 名稱") or "").strip()
    if current_names and challenger_name and challenger_name not in current_names:
        return {}
    if baseline_names and baseline_name and baseline_name not in baseline_names:
        return {}

    passed = _yes_no_flag(entry.get("是否通過前測"))
    promoted = _yes_no_flag(entry.get("是否升格正式策略"))
    actual_result = str(entry.get("本週或下週實際表現") or "").strip()
    decision_date = str(entry.get("決策日期") or "").strip()
    if promoted:
        status = "已升格"
        note = "最新前測紀錄顯示 Challenger 已通過前測並標記為正式策略。"
    elif passed:
        status = "前測通過"
        note = "最新前測紀錄顯示 Challenger 已通過前測，但尚未標記為正式升格。"
    elif actual_result:
        status = "前測未通過"
        note = "最新前測紀錄已有實際結果，但目前未標記為通過。"
    else:
        status = "前測中"
        note = "已建立前測紀錄，正在等待實際表現更新。"
    if decision_date:
        note = f"{note} 最近紀錄日期：{decision_date}。"

    return {
        "entry": entry,
        "status": status,
        "note": note,
        "passed": passed,
        "promoted": promoted,
    }


def _slippage_stress_status(holdout_validation: dict[str, Any] | None) -> tuple[str, str]:
    rows = list((holdout_validation or {}).get("slippage_results") or [])
    if not rows:
        return "待測", "尚未完成滑價壓力測試。"
    positive_rows = [row for row in rows if bool(row.get("positive_return"))]
    slip5 = next((row for row in rows if abs(float(row.get("slip_per_side") or 0.0) - 5.0) < 1e-9), None)
    if len(positive_rows) == len(rows) and slip5 and float(slip5.get("total_return_pct") or 0.0) > 0.0:
        return "通過", "2/3/4/5 點滑價皆仍維持正報酬。"
    if len(positive_rows) >= 3:
        return "觀察", "部分壓力滑價仍為正，但中高滑價下需降級評價。"
    return "淘汰", "滑價升高後快速失效。"


def _stability_area_status(current_snapshot: dict[str, Any]) -> tuple[str, str]:
    plateau = _float_or_none(current_snapshot.get("plateau_score"))
    if plateau is None:
        return "資料不足", "尚無穩定區分數。"
    if plateau < 0.0:
        return "尖峰高風險", f"平台分數 {plateau:.2f}，第一名像單點尖峰。"
    if plateau < 1.0:
        return "觀察", f"平台分數 {plateau:.2f}，附近參數尚未形成明確穩定區。"
    return "穩定", f"平台分數 {plateau:.2f}，附近參數表現相對連續。"


def _decision_recommendation(
    *,
    holdout_validation: dict[str, Any] | None,
    wfo_result: dict[str, Any] | None,
    risk_report: dict[str, Any],
    stability_status: str,
    slippage_status: str,
    forward_snapshot: dict[str, Any] | None = None,
) -> tuple[str, str]:
    if bool((forward_snapshot or {}).get("promoted")):
        return "讓 Challenger 升格", str((forward_snapshot or {}).get("note") or "最新前測紀錄已標記升格。")
    if bool((forward_snapshot or {}).get("passed")):
        return "讓 Challenger 升格", "最新前測紀錄已通過，可進入升格人工複核。"
    if not holdout_validation:
        return "暫停上線，繼續研究", "尚未完成單次 OOS，不能進入前測。"
    if not isinstance(wfo_result, dict) or not wfo_result:
        return "暫停上線，繼續研究", "尚未完成多輪 WFO，單次 OOS 只能初篩。"
    pbo_level = str(risk_report.get("pbo_level") or "高")
    dsr_level = str(risk_report.get("dsr_level") or "低")
    wfo_summary = dict(wfo_result.get("summary") or {})
    pass_rate = float(wfo_summary.get("pass_rate") or 0.0)
    if (
        str(holdout_validation.get("final_verdict") or "REJECT") == "REJECT"
        or pbo_level == "高"
        or dsr_level == "低"
        or stability_status == "尖峰高風險"
        or slippage_status == "淘汰"
        or pass_rate < 0.50
    ):
        return "暫停上線，繼續研究", "至少一個核心守門條件未通過，不應讓新策略升格。"
    if slippage_status == "觀察" or pbo_level == "中" or dsr_level == "中" or pass_rate < 0.70:
        return "維持 Baseline", "Challenger 仍需更多樣本；維持現役策略或空手較保守。"
    return "讓 Challenger 進前測", "多層驗證達到前測門檻，但前測通過前仍不能升格正式策略。"


def _render_forward_test_log_controls(
    *,
    current_snapshot: dict[str, Any],
    historical_snapshot: dict[str, Any],
    holdout_validation: dict[str, Any] | None,
    wfo_result: dict[str, Any] | None,
    risk_report: dict[str, Any],
    recommendation: str,
    slip_per_side: float,
) -> None:
    with st.expander("Forward Test 前測池紀錄", expanded=False):
        baseline_name = st.text_input(
            "Baseline 名稱",
            value=str(historical_snapshot.get("strategy_signature") or historical_snapshot.get("saved_at") or "baseline"),
            key="mq0101_forward_baseline_name",
        )
        challenger_name = st.text_input(
            "Challenger 名稱",
            value=str(current_snapshot.get("strategy_signature") or current_snapshot.get("saved_at") or "challenger"),
            key="mq0101_forward_challenger_name",
        )
        first_fold = {}
        if isinstance(wfo_result, dict):
            folds = list(wfo_result.get("folds") or [])
            first_fold = next((fold for fold in folds if (fold.get("top_candidates") or [])), folds[0] if folds else {})
        reason = st.text_area(
            "選用理由",
            value=f"系統建議：{recommendation}。單次 OOS 只作初篩，需進前測池觀察。",
            key="mq0101_forward_reason",
        )
        actual_result = st.text_input("本週或下週實際表現", value="", key="mq0101_forward_actual_result")
        pass_forward = st.checkbox("是否通過前測", value=False, key="mq0101_forward_passed")
        promote = st.checkbox("是否升格正式策略", value=False, key="mq0101_forward_promoted")
        if st.button("寫入 forward_test_log.csv", key="mq0101_write_forward_log", width="stretch"):
            log_result = append_forward_test_log(
                {
                    "Baseline 名稱": baseline_name,
                    "Challenger 名稱": challenger_name,
                    "Challenger 來源輪次": str(first_fold.get("label") or ""),
                    "Challenger 排名": "1",
                    "選用理由": reason,
                    "單次 OOS 結果": (
                        f"return={float((holdout_validation or {}).get('total_return_pct') or 0.0):.2f}%, "
                        f"mdd={float((holdout_validation or {}).get('mdd_pct') or 0.0):.2f}%, "
                        f"trades={int((holdout_validation or {}).get('trade_count') or 0)}"
                    ),
                    "多輪 WFO 結果摘要": json.dumps((wfo_result or {}).get("summary") or {}, ensure_ascii=False),
                    "PBO": f"{float(risk_report.get('pbo') or 0.0):.3f}",
                    "DSR": f"{float(risk_report.get('dsr') or 0.0):.3f}",
                    "基準滑價假設": f"{float(slip_per_side):.1f}",
                    "本週或下週實際表現": actual_result,
                    "是否通過前測": "是" if pass_forward else "否",
                    "是否升格正式策略": "是" if promote else "否",
                }
            )
            st.success(f"已寫入 {log_result['path']}")


def _render_multilayer_decision_system(
    *,
    view: dict[str, Any],
    params_meta: list[dict[str, Any]],
    current_snapshot: dict[str, Any],
    historical_snapshot: dict[str, Any],
    development_snapshot: dict[str, Any],
    holdout_snapshot: dict[str, Any] | None,
    qualification_report: dict[str, Any],
    holdout_validation: dict[str, Any] | None,
    wfo_result: dict[str, Any] | None,
    research_periods: dict[str, Any],
    slip_per_side: float,
) -> None:
    st.divider()
    st.markdown("## 多層策略驗證與決策系統")
    st.caption("讀法很簡單：先看第 1 層單次 OOS，再看第 2 / 3 層 WFO + gap，最後才看第 4 / 5 層 PBO / DSR 與 Forward Test。")

    single_oos_ready = bool(holdout_validation)
    wfo_ready = isinstance(wfo_result, dict) and bool(wfo_result)
    risk_ready = single_oos_ready and wfo_ready

    single_oos_rows = _single_oos_summary_rows(holdout_validation, research_periods)
    if single_oos_rows:
        st.markdown("### 第 1 層：單次 OOS 初篩")
        st.warning("單次 OOS 僅為初篩，不得單獨作為正式策略升級依據。")
        st.dataframe(pd.DataFrame(single_oos_rows), width="stretch", hide_index=True)
    else:
        st.info("第 1 層單次 OOS 尚未完成。")

    stability_status, stability_note = _stability_area_status(current_snapshot)
    slippage_status, slippage_note = _slippage_stress_status(holdout_validation)
    forward_snapshot = _current_forward_test_snapshot(
        current_snapshot=current_snapshot,
        historical_snapshot=historical_snapshot,
    )
    if risk_ready:
        risk_report = _approx_pbo_dsr_report(
            view=view,
            params_meta=params_meta,
            current_snapshot=current_snapshot,
            development_snapshot=development_snapshot,
            holdout_snapshot=holdout_snapshot,
            qualification_report=qualification_report,
            holdout_validation=holdout_validation,
            wfo_result=wfo_result,
        )
        recommendation, recommendation_note = _decision_recommendation(
            holdout_validation=holdout_validation,
            wfo_result=wfo_result,
            risk_report=risk_report,
            stability_status=stability_status,
            slippage_status=slippage_status,
            forward_snapshot=forward_snapshot,
        )
        risk_status = f"PBO {risk_report['pbo']:.2f}({risk_report['pbo_level']}) / DSR {risk_report['dsr']:.2f}({risk_report['dsr_level']})"
        risk_note = risk_report["definition"]
        if forward_snapshot:
            forward_status = str(forward_snapshot.get("status") or "待前測")
            forward_note = str(forward_snapshot.get("note") or "最新前測紀錄已載入。")
        else:
            forward_status = "待前測"
            forward_note = "前測通過前不得升格正式策略。"
    else:
        risk_report = {
            "pbo": None,
            "pbo_level": "待測",
            "dsr": None,
            "dsr_level": "待測",
            "risk_report": {},
            "definition": "需在單次 OOS 與多輪 WFO 都完成後，才會啟用 PBO / DSR 過度最佳化檢查。",
        }
        if not single_oos_ready:
            recommendation = "等待第 1 層完成"
            recommendation_note = "先完成單次 OOS，確認這個候選至少通過初篩。"
        else:
            recommendation = "等待第 2 層完成"
            recommendation_note = "單次 OOS 已完成，但還不能直接下判斷；請先跑多輪 WFO。"
        risk_status = "待測"
        risk_note = risk_report["definition"]
        forward_status = "待前置完成"
        forward_note = "需先完成前四層中的必要前置，再開放前測池紀錄。"

    wfo_summary = dict((wfo_result or {}).get("summary") or {})
    wfo_status = f"{float(wfo_summary.get('pass_rate') or 0.0) * 100.0:.1f}%" if wfo_ready else "待測"
    gap_status = f"gap {int(((wfo_result or {}).get('settings') or {}).get('gap_days') or 0)} 天" if wfo_ready else "待測"
    gap_note = "目前先做 train/test 中間 gap。" if wfo_ready else "需先完成多輪 WFO，才會確認本次 gap / purge 防污染設定。"
    slippage_status_display = slippage_status if single_oos_ready else "待測"
    slippage_note_display = slippage_note if single_oos_ready else "需先完成單次 OOS，才會產生 2/3/4/5 點滑價壓力測試。"

    layer_rows = [
        {"層級": "第 1 層", "項目": "單次 OOS", "狀態": _verdict_label(str((holdout_validation or {}).get("final_verdict") or ""), default="待測"), "說明": "只作初篩。"},
        {"層級": "第 2 層", "項目": "多輪 WFO", "狀態": wfo_status, "說明": "多輪樣本外通過率。"},
        {"層級": "第 3 層", "項目": "gap / purge 防污染", "狀態": gap_status, "說明": gap_note},
        {"層級": "第 4 層", "項目": "PBO / DSR", "狀態": risk_status, "說明": risk_note},
        {"層級": "第 5 層", "項目": "Forward Test", "狀態": forward_status, "說明": forward_note},
        {"層級": "壓力", "項目": "2/3/4/5 點滑價", "狀態": slippage_status_display, "說明": slippage_note_display},
        {"層級": "穩定", "項目": "穩定區 / 尖峰警示", "狀態": stability_status, "說明": stability_note},
    ]
    st.dataframe(pd.DataFrame(layer_rows), width="stretch", hide_index=True, height=300)

    cards = st.columns(5)
    cards[0].metric("最終建議", recommendation)
    if risk_ready:
        cards[1].metric("PBO", f"{float(risk_report['pbo']):.2f}", str(risk_report["pbo_level"]))
        cards[2].metric("DSR", f"{float(risk_report['dsr']):.2f}", str(risk_report["dsr_level"]))
    else:
        cards[1].metric("PBO", "待測", "等待前置層")
        cards[2].metric("DSR", "待測", "等待前置層")
    cards[3].metric("滑價壓測", slippage_status_display)
    cards[4].metric("Forward Test", forward_status)

    if recommendation == "暫停上線，繼續研究":
        st.error(recommendation_note)
    elif recommendation.startswith("等待第"):
        st.info(recommendation_note)
    elif recommendation == "維持 Baseline":
        st.warning(recommendation_note)
    else:
        st.info(recommendation_note)

    if holdout_validation and isinstance(wfo_result, dict) and wfo_result:
        _render_forward_test_log_controls(
            current_snapshot=current_snapshot,
            historical_snapshot=historical_snapshot,
            holdout_validation=holdout_validation,
            wfo_result=wfo_result,
            risk_report=risk_report,
            recommendation=recommendation,
            slip_per_side=slip_per_side,
        )
    else:
        st.caption("Forward Test 前測池會在單次 OOS 與多輪 WFO 都具備後開放紀錄。")


def _render_running_workspace(
    *,
    view: dict[str, Any],
    active_job_id: str,
    historical_snapshot: dict[str, Any],
    research_periods: dict[str, Any],
    system_snapshot: dict[str, Any],
    mode: str,
    hard_filters: dict[str, Any],
) -> None:
    base._render_status_notices(view)
    _render_runtime_overview(view=view, system_snapshot=system_snapshot)
    with st.expander("檢視背景程序監控", expanded=True):
        base._render_worker_monitor(active_job_id=active_job_id, active_status=str(view.get("active_status") or ""))
    base._render_progress(view)
    current_snapshot = view.get("current_snapshot") or {}
    if isinstance(current_snapshot.get("params"), dict) and current_snapshot.get("params"):
        _render_period_overview(
            current_snapshot=current_snapshot,
            historical_snapshot=historical_snapshot,
            research_periods=research_periods,
            system_snapshot=system_snapshot,
            mode=mode,
            heading="最佳化區即時摘要",
            caption="這裡會隨目前第一名候選即時更新，讓你不用等整輪結束才看到最佳化區狀態。",
        )
        if not view["current_top_df"].empty:
            st.markdown("### 目前最佳候選")
            base._render_dataframe(view["current_top_df"], param_label_map=st.session_state.get("mq01_param_label_map") or {}, height=260)
            _render_mdd_sweep_summary(top_df=view["current_top_df"], hard_filters=hard_filters)
        _render_multilayer_decision_system(
            view=view,
            params_meta=[],
            current_snapshot=current_snapshot,
            historical_snapshot=historical_snapshot,
            development_snapshot={},
            holdout_snapshot=None,
            qualification_report={},
            holdout_validation=None,
            wfo_result=None,
            research_periods=research_periods,
            slip_per_side=0.0,
        )
        if not view["current_recent_df"].empty:
            st.markdown("### 最近測試")
            base._render_dataframe(view["current_recent_df"], param_label_map=st.session_state.get("mq01_param_label_map") or {}, height=260)


def _render_post_run_workspace(
    *,
    view: dict[str, Any],
    params_meta: list[dict[str, Any]],
    minute_path: str,
    daily_path: str,
    script_name: str,
    capital: int,
    slip_per_side: float,
    current_snapshot: dict[str, Any],
    historical_snapshot: dict[str, Any],
    research_periods: dict[str, Any],
    development_snapshot: dict[str, Any],
    qualification_report: dict[str, Any],
    system_snapshot: dict[str, Any],
    mode: str,
    ui_specs: list[dict[str, Any]],
    runtime_settings: dict[str, Any],
    hard_filters: dict[str, Any],
    wfo_settings: dict[str, int],
    holdout_requested: bool,
    holdout_snapshot: dict[str, Any] | None,
    full_snapshot: dict[str, Any] | None,
    holdout_validation: dict[str, Any] | None,
) -> str:
    base._render_status_notices(view)
    if not holdout_validation:
        _render_next_action_panel(
            has_candidate=bool(current_snapshot.get("params")),
            has_holdout=False,
            has_wfo=False,
        )
    _render_decision_overview(
        current_snapshot=current_snapshot,
        qualification_report=qualification_report,
        development_snapshot=development_snapshot,
        holdout_snapshot=holdout_snapshot,
        full_snapshot=full_snapshot,
        holdout_validation=holdout_validation,
    )

    st.divider()
    st.markdown("## 步驟 2：單次 OOS 驗收")
    st.caption("這段資料不參與最佳化，只拿來做一次樣本外初篩。跑完這一步，才會開放步驟 3 的多輪 WFO。")
    action_cols = st.columns((1.2, 1.2))
    with action_cols[0]:
        test_clicked = st.button(
            f"步驟 2：只跑單次 OOS（{research_periods.get('holdout_label', '--')}）",
            type="primary",
            width="stretch",
            key="mq0101_v2_holdout_button",
            disabled=not bool(current_snapshot.get("params")),
        )
    with action_cols[1]:
        full_validation_clicked = st.button(
            "一鍵跑完整驗證（OOS + WFO）",
            width="stretch",
            key="mq0101_v2_full_validation_button",
            disabled=not bool(current_snapshot.get("params")),
        )
    st.caption("如果你只是想先看單次 OOS，就按左邊；如果你要直接跑到多輪 WFO 與決策摘要，就按右邊。")

    if not holdout_requested:
        st.info("現在先按上方按鈕跑驗證。完成後，畫面才會開放多輪 WFO 與完整五層決策。")
        with st.expander("查看開發區審查與最佳化區細節", expanded=False):
            _render_qualification_report(
                current_snapshot=current_snapshot,
                qualification_report=qualification_report,
                holdout_validation=holdout_validation,
            )
            _render_metric_cards("最佳化區理論 / 滑價摘要", development_snapshot)
            if isinstance(development_snapshot.get("yearly_df"), pd.DataFrame) and not development_snapshot["yearly_df"].empty:
                st.markdown("### 最佳化區年度報酬（往回推）")
                st.dataframe(development_snapshot["yearly_df"], width="stretch", hide_index=True, height=260)
            _render_nav_and_weekly("最佳化區圖表", development_snapshot)
            _render_trade_table("最佳化區逐筆交易", _ensure_dataframe(development_snapshot.get("trade_df")), height=320)
        if full_validation_clicked:
            return "full_validation"
        if test_clicked:
            return "holdout_only"
        return ""

    if not holdout_snapshot or not full_snapshot or not holdout_validation:
        st.warning("驗證區 / 全期間分析尚未完成，請稍候。")
        if full_validation_clicked:
            return "full_validation"
        if test_clicked:
            return "holdout_only"
        return ""

    wfo_result = _render_multi_wfo_panel(
        mode=mode,
        ui_specs=ui_specs,
        params_meta=params_meta,
        runtime_settings=runtime_settings,
        hard_filters=hard_filters,
        minute_path=minute_path,
        daily_path=daily_path,
        script_name=script_name,
        research_periods=research_periods,
        wfo_settings=wfo_settings,
    )
    _render_next_action_panel(
        has_candidate=True,
        has_holdout=True,
        has_wfo=bool(wfo_result),
    )
    _render_multilayer_decision_system(
        view=view,
        params_meta=params_meta,
        current_snapshot=current_snapshot,
        historical_snapshot=historical_snapshot,
        development_snapshot=development_snapshot,
        holdout_snapshot=holdout_snapshot,
        qualification_report=qualification_report,
        holdout_validation=holdout_validation,
        wfo_result=wfo_result,
        research_periods=research_periods,
        slip_per_side=float(slip_per_side),
    )

    dev_verdict = str(qualification_report.get("verdict") or "REJECT")
    holdout_verdict = str(holdout_validation.get("final_verdict") or "REJECT")
    deployment_verdict = _combined_verdict(dev_verdict, holdout_verdict)

    slippage_df = pd.DataFrame(list(holdout_validation.get("slippage_results") or []))
    if not slippage_df.empty:
        slippage_df = slippage_df.rename(
            columns={
                "slip_per_side": "單邊滑價",
                "net_profit": "淨利",
                "trade_count": "交易筆數",
                "win_rate_pct": "勝率",
                "avg_net_profit": "平均每筆",
                "profit_factor": "PF",
                "sharpe": "Sharpe",
                "total_return_pct": "報酬率",
                "mdd_pct": "MDD",
                "positive_return": "是否正報酬",
            }
        )
        for column in ("淨利", "平均每筆"):
            if column in slippage_df.columns:
                slippage_df[column] = slippage_df[column].map(lambda value: f"{float(value):,.0f}")
        for column in ("勝率", "報酬率", "MDD"):
            if column in slippage_df.columns:
                slippage_df[column] = slippage_df[column].map(lambda value: f"{float(value):.2f}%")
        if "PF" in slippage_df.columns:
            slippage_df["PF"] = slippage_df["PF"].map(lambda value: f"{float(value):.2f}")
        if "Sharpe" in slippage_df.columns:
            slippage_df["Sharpe"] = slippage_df["Sharpe"].map(lambda value: f"{float(value):.2f}")
        if "是否正報酬" in slippage_df.columns:
            slippage_df["是否正報酬"] = slippage_df["是否正報酬"].map(lambda value: "是" if bool(value) else "否")
        if "單邊滑價" in slippage_df.columns:
            slippage_df["單邊滑價"] = slippage_df["單邊滑價"].map(lambda value: f"{float(value):.1f}")
        if "交易筆數" in slippage_df.columns:
            slippage_df["交易筆數"] = slippage_df["交易筆數"].map(lambda value: f"{int(value):,}")

    with st.expander("進階補充分析（需要時再看）", expanded=False):
        _render_overfit_risk_panel(
            view=view,
            params_meta=params_meta,
            current_snapshot=current_snapshot,
            development_snapshot=development_snapshot,
            holdout_snapshot=holdout_snapshot,
            qualification_report=qualification_report,
            holdout_validation=holdout_validation,
        )
        _render_mdd_sweep_summary(top_df=view["current_top_df"], hard_filters=hard_filters)
        _render_top_candidate_holdout_comparison(
            view=view,
            params_meta=params_meta,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            capital=int(capital),
            slip_per_side=float(slip_per_side),
            research_periods=research_periods,
            candidate_count=3,
        )
        _render_period_compare_table(
            development_snapshot=development_snapshot,
            holdout_snapshot=holdout_snapshot,
            full_snapshot=full_snapshot,
        )
        _render_incumbent_challenger_panel(
            current_snapshot=current_snapshot,
            params_meta=params_meta,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            capital=int(capital),
            slip_per_side=float(slip_per_side),
            research_periods=research_periods,
            deployment_verdict=deployment_verdict,
            challenger_holdout=holdout_snapshot,
            challenger_full=full_snapshot,
        )

        compare_panels = st.columns(3)
        with compare_panels[0]:
            _render_snapshot_panel("最佳化區", development_snapshot)
        with compare_panels[1]:
            _render_snapshot_panel("驗證區", holdout_snapshot)
        with compare_panels[2]:
            _render_snapshot_panel("全期間", full_snapshot)

        detail_cols = st.columns((1.0, 1.0))
        with detail_cols[0]:
            if isinstance(holdout_snapshot.get("monthly_df"), pd.DataFrame) and not holdout_snapshot["monthly_df"].empty:
                st.markdown("### 驗證區月度表")
                st.dataframe(holdout_snapshot["monthly_df"], width="stretch", hide_index=True, height=260)
        with detail_cols[1]:
            if not slippage_df.empty:
                st.markdown("### 驗證區滑價敏感度")
                st.dataframe(slippage_df, width="stretch", hide_index=True, height=260)

    dev_actual = _metric_value(development_snapshot.get("metric_map") or {}, "總淨利 Net Profit", "actual")
    holdout_actual = _metric_value(holdout_snapshot.get("metric_map") or {}, "總淨利 Net Profit", "actual")
    full_actual = _metric_value(full_snapshot.get("metric_map") or {}, "總淨利 Net Profit", "actual")
    holdout_pf = _metric_value(holdout_snapshot.get("metric_map") or {}, "獲利因子 Profit Factor", "actual")
    holdout_mdd = _metric_value(holdout_snapshot.get("metric_map") or {}, "最大回撤率 Max Drawdown %", "actual")

    summary_lines = [
        f"開發區審查為 {_verdict_label(dev_verdict)}，單次 OOS 驗收為 {_verdict_label(holdout_verdict)}；這兩者只能先做初篩，不能直接決定是否升格。",
        f"最佳化區（{development_snapshot.get('period_label', '--')}）滑價淨利 { _format_metric_value('總淨利 Net Profit', dev_actual) }，代表這組參數在歷史開發區是否具備基本存活能力。",
        f"單次 OOS（{holdout_snapshot.get('period_label', '--')}）滑價淨利 { _format_metric_value('總淨利 Net Profit', holdout_actual) }，PF { _format_metric_value('獲利因子 Profit Factor', holdout_pf) }，MDD { _format_metric_value('最大回撤率 Max Drawdown %', holdout_mdd) }。",
        f"全期間（{full_snapshot.get('period_label', '--')}）滑價淨利 { _format_metric_value('總淨利 Net Profit', full_actual) }，僅作背景參考，不可把全期間第一名視為唯一依據。",
    ]

    detail_tabs = st.tabs(["摘要說明", "圖表", "策略組合", "KPI", "交易明細", "開發區審查"])
    with detail_tabs[0]:
        st.markdown("### 先看順序")
        st.markdown(
            "\n".join(
                [
                    "- 先看上方「多層策略驗證與決策系統」，它才是最後決策主畫面。",
                    "- 圖表、KPI、交易明細都放在後面，只是拿來複核，不是第一眼先看。",
                    "- 如果還沒跑 WFO，下一步就是按上方的「步驟 3：計算多輪 WFO」。",
                ]
            )
        )
        st.markdown("### 這輪結果摘要")
        st.markdown("\n".join(f"- {line}" for line in summary_lines))
        st.markdown("### 各頁用途")
        st.markdown(
            "\n".join(
                [
                    "- `圖表`：看淨值曲線與週損益。",
                    "- `策略組合`：看前幾名候選、參數家族與穩定區。",
                    "- `KPI`：看完整指標表。",
                    "- `交易明細`：看逐筆交易。",
                    "- `開發區審查`：回頭檢查最佳化區為什麼通過或淘汰。",
                ]
            )
        )
    with detail_tabs[1]:
        _render_nav_and_weekly(
            "全期間分段圖表",
            full_snapshot,
            boundary_date=int(research_periods["holdout_start_date"]),
            boundary_label="驗證區起點",
        )
        with st.expander("只看驗證區圖表", expanded=False):
            _render_nav_and_weekly("驗證區圖表", holdout_snapshot)
    with detail_tabs[2]:
        _render_strategy_family_tab(
            view=view,
            params_meta=params_meta,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            capital=int(capital),
            slip_per_side=float(slip_per_side),
            research_periods=research_periods,
            development_snapshot=development_snapshot,
            holdout_snapshot=holdout_snapshot,
            full_snapshot=full_snapshot,
        )
        st.divider()
        _render_top_candidate_multiperiod_validation(
            view=view,
            params_meta=params_meta,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            capital=int(capital),
            slip_per_side=float(slip_per_side),
            research_periods=research_periods,
            candidate_count=3,
        )
        st.divider()
        _render_param_stability_map(view=view, params_meta=params_meta)
    with detail_tabs[3]:
        _render_full_kpi_compare(
            development_snapshot=development_snapshot,
            holdout_snapshot=holdout_snapshot,
            full_snapshot=full_snapshot,
        )
    with detail_tabs[4]:
        _render_trade_table("驗證區逐筆交易", _ensure_dataframe(holdout_snapshot.get("trade_df")), height=360)
        _render_trade_table("全期間逐筆交易", _ensure_dataframe(full_snapshot.get("trade_df")), height=360)
    with detail_tabs[5]:
        _render_qualification_report(
            current_snapshot=current_snapshot,
            qualification_report=qualification_report,
            holdout_validation=holdout_validation,
        )
        _render_metric_cards("最佳化區理論 / 滑價摘要", development_snapshot)
        if isinstance(development_snapshot.get("yearly_df"), pd.DataFrame) and not development_snapshot["yearly_df"].empty:
            st.markdown("### 最佳化區年度報酬（往回推）")
            st.dataframe(development_snapshot["yearly_df"], width="stretch", hide_index=True, height=260)
        _render_nav_and_weekly("最佳化區圖表", development_snapshot)
    if full_validation_clicked:
        return "full_validation"
    if test_clicked:
        return "holdout_only"
    return ""


def _render_landing_guide() -> None:
    st.markdown("## 操作總覽")
    st.info("主線只有四步：設定路徑與條件、跑最佳化、測驗證區、看最終判定。其他表格都只是查證用。")

    flow_cols = st.columns(4)
    flow_cards = [
        ("1 設定", "左側確認 XS、M1、D1、preset 與硬性過濾。"),
        ("2 最佳化", "按開始後只盯即時監控與目前最佳候選。"),
        ("3 驗收", "開發區有結果後，按單次 OOS 或一鍵完整驗證。"),
        ("4 判斷", "先看決策總覽，再查圖表、KPI、交易明細。"),
    ]
    for column, (title, body) in zip(flow_cols, flow_cards, strict=False):
        with column:
            st.markdown(f"#### {title}")
            st.caption(body)

    st.markdown("### 先看這三個地方")
    focus_cols = st.columns(3)
    focus_cols[0].metric("左側路徑", "先確認")
    focus_cols[1].metric("執行中", "看監控")
    focus_cols[2].metric("跑完後", "看總覽")
    st.caption("如果只是要測流程，先不用讀完所有說明，讓流程跑完一輪比較重要。")

    with st.expander("查看完整操作說明", expanded=False):
        st.markdown(
            "\n".join(
                [
                    "### 建議操作順序",
                    "1. 確認左側 `XS / M1 / D1 / preset` 路徑正確。",
                    "2. 設定搜尋模式、資源限制與硬性過濾。",
                    "3. 需要時展開參數設定，調整參數起訖值與步長。",
                    "4. 按開始後先看 `即時執行監控` 和 `背景程序監控`。",
                    "5. 跑完先按 `步驟 2：只跑單次 OOS`，或直接按 `一鍵跑完整驗證（OOS + WFO）`。",
                    "6. 再看 `多層策略驗證與決策系統`，最後才用圖表、KPI 與逐筆交易做複核。",
                ]
            )
        )
        st.markdown(
            "\n".join(
                [
                    "### 判斷順序",
                    "- 先看開發區是否被淘汰。",
                    "- 再看驗證區是否通過。",
                    "- 接著確認 MDD、PF、交易數和滑價後淨利。",
                    "- 最後才查圖表、KPI 與逐筆交易。",
                ]
            )
        )

    st.divider()


def _sync_latest_strategy_inputs(*, path_defaults: Any) -> None:
    if XS_PATH_INPUT_KEY not in st.session_state:
        st.session_state[XS_PATH_INPUT_KEY] = path_defaults.xs_path
    if M1_PATH_INPUT_KEY not in st.session_state:
        st.session_state[M1_PATH_INPUT_KEY] = path_defaults.minute_path
    if D1_PATH_INPUT_KEY not in st.session_state:
        st.session_state[D1_PATH_INPUT_KEY] = path_defaults.daily_path
    if PRESET_PATH_INPUT_KEY not in st.session_state:
        st.session_state[PRESET_PATH_INPUT_KEY] = path_defaults.param_preset_path

    latest_artifact = load_latest_artifact_snapshot()
    latest_xs_path = str(latest_artifact.get("best_strategy_xs_path") or "").strip()
    latest_saved_at = str(latest_artifact.get("saved_at") or "").strip()
    baseline_preset_path = str(path_defaults.param_preset_path)
    sync_stamp = f"{latest_saved_at}|{latest_xs_path}|{baseline_preset_path}"

    if (
        latest_saved_at
        and latest_xs_path
        and Path(latest_xs_path).exists()
        and Path(baseline_preset_path).exists()
        and st.session_state.get(LATEST_INPUT_SYNC_KEY) != sync_stamp
    ):
        st.session_state[XS_PATH_INPUT_KEY] = latest_xs_path
        st.session_state[PRESET_PATH_INPUT_KEY] = baseline_preset_path
        st.session_state[LATEST_INPUT_SYNC_KEY] = sync_stamp


def _normalize_param_specs(default_specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for spec in default_specs:
        updated = dict(spec)
        fixed_spec = FIXED_PARAM_SPECS.get(str(updated.get("name") or ""))
        if fixed_spec:
            updated.update(fixed_spec)
        normalized.append(updated)
    return normalized


def _param_defaults_signature(*, xs_path: str, preset_path: str, default_specs: list[dict[str, Any]]) -> str:
    payload = {
        "reset_version": 2,
        "xs_path": str(xs_path),
        "preset_path": str(preset_path),
        "specs": [
            {
                "name": spec.get("name"),
                "enabled": bool(spec.get("enabled")),
                "type": spec.get("type"),
                "default": spec.get("default"),
                "start": spec.get("start"),
                "stop": spec.get("stop"),
                "step": spec.get("step"),
            }
            for spec in default_specs
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _reset_param_widget_state_if_needed(*, xs_path: str, preset_path: str, default_specs: list[dict[str, Any]]) -> None:
    signature = _param_defaults_signature(xs_path=xs_path, preset_path=preset_path, default_specs=default_specs)
    if st.session_state.get(PARAM_DEFAULT_SIGNATURE_KEY) == signature:
        return
    for spec in default_specs:
        name = str(spec["name"])
        st.session_state.pop(f"mq01_enabled_{name}", None)
        st.session_state.pop(f"mq01_start_{name}", None)
        st.session_state.pop(f"mq01_stop_{name}", None)
        st.session_state.pop(f"mq01_step_{name}", None)
    st.session_state.pop("mq01_enabled_defaults_mode", None)
    st.session_state[PARAM_DEFAULT_SIGNATURE_KEY] = signature


def _enforce_fixed_param_state(default_specs: list[dict[str, Any]]) -> None:
    for spec in default_specs:
        name = str(spec.get("name") or "")
        fixed_spec = FIXED_PARAM_SPECS.get(name)
        if not fixed_spec:
            continue
        st.session_state.pop(f"mq01_enabled_{name}", None)
        st.session_state.pop(f"mq01_start_{name}", None)
        st.session_state.pop(f"mq01_stop_{name}", None)
        st.session_state.pop(f"mq01_step_{name}", None)


def _apply_mode_enabled_defaults(mode: str, default_specs: list[dict[str, Any]]) -> None:
    state_key = "mq01_enabled_defaults_mode"
    if st.session_state.get(state_key) == mode:
        return
    enable_all = mode == "smart"
    for spec in default_specs:
        name = str(spec.get("name") or "")
        st.session_state.pop(f"mq01_enabled_{name}", None)
        spec["enabled"] = bool(enable_all and name not in FIXED_PARAM_SPECS)
    st.session_state[state_key] = mode


def _fixed_number_display(*, label: str, key: str, value: int | float, value_type: str) -> None:
    if value_type == "int":
        st.number_input(label, value=int(value), step=1, format="%d", label_visibility="collapsed", disabled=True, key=key)
        return
    st.number_input(label, value=float(value), step=0.01, format="%.4f", label_visibility="collapsed", disabled=True, key=key)


def render_app() -> None:
    if base.PROFILE_SESSION_KEY not in st.session_state:
        st.session_state[base.PROFILE_SESSION_KEY] = RESEARCH_PROFILE_TAG_0101

    initial_active_job_id = str(st.session_state.get("mq01_active_job_id") or "").strip()
    initial_active_state = read_job_state(initial_active_job_id) if initial_active_job_id else {}
    initial_active_status = str(initial_active_state.get("status") or "")
    running_at_page_start = bool(initial_active_job_id) and not is_terminal_status(initial_active_status)

    st.set_page_config(page_title="策略研究開發工作台", layout="wide")
    st.title("策略研究開發工作台")
    st.caption("左邊設定，右邊照順序操作：先最佳化、再單次 OOS、再多輪 WFO，最後看多層策略驗證與決策系統。")

    if not running_at_page_start:
        st.markdown("### 頁面導覽與操作說明")
        st.caption("如果你是第一次進來，先把下面這份完整說明讀過一輪，再開始設定與執行，會比直接盯著結果表更容易理解。")
        _render_landing_guide()

    path_defaults = default_paths()
    runtime_defaults = default_runtime_settings()
    hard_filter_defaults = default_hard_filters()
    _sync_latest_strategy_inputs(path_defaults=path_defaults)

    with st.sidebar:
        st.subheader("快速流程")
        st.caption("先照這個順序按，不用一開始就把所有圖表都讀完。")
        st.markdown(
            "\n".join(
                [
                    "1. `開始執行`：先跑最佳化。",
                    "2. `步驟 2：只跑單次 OOS`：做單次 OOS 初篩。",
                    "3. `一鍵跑完整驗證（OOS + WFO）`：直接跑到多輪驗證與決策摘要。",
                    "4. 看 `多層策略驗證與決策系統`：再決定要不要前測。",
                ]
            )
        )

        st.subheader("資料路徑")
        xs_path = st.text_input("XS 路徑", key=XS_PATH_INPUT_KEY)
        minute_path = st.text_input("M1 路徑", key=M1_PATH_INPUT_KEY)
        daily_path = st.text_input("D1 路徑", key=D1_PATH_INPUT_KEY)
        preset_path = st.text_input("參數範圍 preset", key=PRESET_PATH_INPUT_KEY)
        st.caption("每次最佳策略落盤後，`XS 路徑` 會自動切到最新策略；參數範圍仍使用基準 preset，避免被最佳值鎖成單點。")

        st.subheader("搜尋模式")
        mode = st.selectbox(
            "模式",
            options=list(base.MODE_OPTIONS.keys()),
            format_func=lambda key: base.MODE_OPTIONS[key],
            index=0,
        )

        st.subheader("執行設定")
        capital = int(st.number_input("本金", value=int(runtime_defaults["capital"]), step=100_000, format="%d"))
        slip_per_side = float(
            st.number_input("單邊滑價", value=float(runtime_defaults["slip_per_side"]), min_value=0.0, step=0.1, format="%.2f")
        )
        development_years = int(
            st.selectbox(
                "開發區年份",
                options=[1, 2, 3, 4, 5],
                index=max(0, min(4, int(runtime_defaults.get("development_years", 5)) - 1)),
                format_func=lambda value: f"{int(value)} 年",
                key="mq0101_v2_development_years",
            )
        )
        st.caption("1 年開發 / 約 5 年驗證；2 年開發 / 約 4 年驗證；以此類推。")
        requested_workers = int(
            st.number_input("最大背景程序數", value=int(runtime_defaults["max_workers"]), min_value=1, step=1, format="%d")
        )
        cpu_limit_pct = int(
            st.number_input("CPU 使用率上限(%)", value=int(runtime_defaults["cpu_limit_pct"]), min_value=1, max_value=100, step=5, format="%d")
        )
        memory_limit_pct = int(
            st.number_input(
                "記憶體使用率上限(%)",
                value=int(runtime_defaults["memory_limit_pct"]),
                min_value=1,
                max_value=100,
                step=5,
                format="%d",
            )
        )
        effective_workers = resolve_effective_workers(requested_workers=requested_workers, cpu_limit_pct=cpu_limit_pct)
        top_n = int(
            st.number_input(
                "保留前幾名",
                value=int(runtime_defaults["top_n"]),
                min_value=3,
                step=1,
                format="%d",
                key="mq0101_v2_top_n",
            )
        )
        seed_keep_count = int(
            st.number_input(
                "每個參數保留前幾名",
                value=int(runtime_defaults["seed_keep_count"]),
                min_value=3,
                step=1,
                format="%d",
                key="mq0101_v2_seed_keep_count",
            )
        )
        st.caption("WFO 設定只在步驟 3「計算多輪 WFO」時使用，不會影響前面的單次最佳化。")
        wfo_train_years = int(
            st.number_input(
                "WFO 訓練窗(年)",
                value=int(runtime_defaults.get("wfo_train_years", 4)),
                min_value=1,
                max_value=10,
                step=1,
                format="%d",
                key="mq0101_v2_wfo_train_years",
            )
        )
        wfo_test_years = int(
            st.number_input(
                "WFO 測試窗(年)",
                value=int(runtime_defaults.get("wfo_test_years", 1)),
                min_value=1,
                max_value=5,
                step=1,
                format="%d",
                key="mq0101_v2_wfo_test_years",
            )
        )
        wfo_step_years = int(
            st.number_input(
                "WFO 滑動步長(年)",
                value=int(runtime_defaults.get("wfo_step_years", 1)),
                min_value=1,
                max_value=5,
                step=1,
                format="%d",
                key="mq0101_v2_wfo_step_years",
            )
        )
        wfo_gap_days = int(
            st.number_input(
                "WFO gap 天數",
                value=int(runtime_defaults.get("wfo_gap_days", 0)),
                min_value=0,
                max_value=365,
                step=1,
                format="%d",
                key="mq0101_v2_wfo_gap_days",
            )
        )
        st.caption("建議先用 4 年訓練 / 1 年測試 / 1 年步長 / gap 5 天；1 年訓練較像敏感度測試。")
        st.caption(f"CPU 上限 {cpu_limit_pct}% | 記憶體上限 {memory_limit_pct}% | 可用背景程序數 {effective_workers}")

        st.subheader("硬性過濾")
        min_trades = int(st.number_input("最少交易筆數", value=int(hard_filter_defaults["min_trades"]), min_value=0, step=10, format="%d"))
        min_total_return = float(
            st.number_input("最低總報酬(%)", value=float(hard_filter_defaults["min_total_return"]), step=1.0, format="%.2f")
        )
        mdd_mode = str(
            st.selectbox(
                "MDD 門檻模式",
                options=["fixed", "sweep"],
                index=0 if str(hard_filter_defaults.get("mdd_mode", "fixed")) != "sweep" else 1,
                format_func=lambda value: "固定門檻" if value == "fixed" else "範圍測試",
                key="mq0101_v2_mdd_mode",
            )
        )
        if mdd_mode == "sweep":
            mdd_start_pct = float(
                st.number_input(
                    "MDD 起點(%)",
                    value=float(hard_filter_defaults.get("mdd_start_pct", 3.0)),
                    min_value=0.0,
                    step=1.0,
                    format="%.2f",
                    key="mq0101_v2_mdd_start_pct",
                )
            )
            mdd_end_pct = float(
                st.number_input(
                    "MDD 終點(%)",
                    value=float(hard_filter_defaults.get("mdd_end_pct", hard_filter_defaults["max_mdd_pct"])),
                    min_value=0.0,
                    step=1.0,
                    format="%.2f",
                    key="mq0101_v2_mdd_end_pct",
                )
            )
            mdd_step_pct = float(
                st.number_input(
                    "MDD 步長(%)",
                    value=float(hard_filter_defaults.get("mdd_step_pct", 1.0)),
                    min_value=0.01,
                    step=0.5,
                    format="%.2f",
                    key="mq0101_v2_mdd_step_pct",
                )
            )
            max_mdd_pct = max(mdd_start_pct, mdd_end_pct)
            st.caption("範圍測試會一次跑完同一批策略結果，再用不同 MDD 門檻分層；不需要一個門檻按一次。")
        else:
            max_mdd_pct = float(
                st.number_input("最大 MDD(%)", value=float(hard_filter_defaults["max_mdd_pct"]), step=1.0, format="%.2f")
            )
            mdd_start_pct = max_mdd_pct
            mdd_end_pct = max_mdd_pct
            mdd_step_pct = 1.0

    path_errors = [path for path in (xs_path, minute_path, daily_path, preset_path) if not Path(path).exists()]
    if path_errors:
        st.error("以下路徑不存在，請先確認：\n" + "\n".join(path_errors))
        return

    try:
        script_name, params_meta, default_specs = load_strategy_metadata(xs_path, preset_path)
    except Exception as exc:
        st.exception(exc)
        return
    default_specs = _normalize_param_specs(default_specs)
    _reset_param_widget_state_if_needed(xs_path=xs_path, preset_path=preset_path, default_specs=default_specs)

    base._remember_param_label_map(params_meta)
    _apply_mode_enabled_defaults(mode, default_specs)
    _enforce_fixed_param_state(default_specs)

    research_periods = resolve_0101_research_periods(
        minute_path=minute_path,
        daily_path=daily_path,
        development_years=development_years,
    )
    if not research_periods:
        st.error("無法解析 01-01 的研究期間，請先確認 M1 / D1 資料是否完整。")
        return
    research_profile_tag = str(research_periods.get("research_profile_tag") or RESEARCH_PROFILE_TAG_0101)
    if st.session_state.get(base.PROFILE_SESSION_KEY) != research_profile_tag:
        base._reset_profile_state()
        st.session_state[base.PROFILE_SESSION_KEY] = research_profile_tag
    st.sidebar.caption(
        f"本輪開發區：{research_periods.get('development_label', '--')}；驗證區：{research_periods.get('holdout_label', '--')}"
    )

    historical_snapshot = load_historical_best_snapshot(
        [str(item["name"]) for item in params_meta],
        slip_per_side=slip_per_side,
        research_profile_tag=research_profile_tag,
    )

    config_hidden = bool(st.session_state.get("mq01_hide_config", False))
    toggle_label = "展開參數設定" if config_hidden else "收起參數設定"
    if st.button(toggle_label, width="content", key="mq0101_toggle_config_single_page"):
        st.session_state["mq01_hide_config"] = not config_hidden
        st.rerun()

    if not config_hidden:
        st.subheader("參數設定")
        st.caption(f"目前策略為 `{script_name}`，參數挑選仍維持左邊欄位設定，右邊只負責把結果清楚攤開。")
        header_cols = st.columns([1.0, 1.8, 1.4, 1.4, 1.2, 0.8])
        header_cols[0].markdown("**啟用**")
        header_cols[1].markdown("**參數**")
        header_cols[2].markdown("**起點**")
        header_cols[3].markdown("**終點**")
        header_cols[4].markdown("**步長**")
        header_cols[5].markdown("**型別**")

        for spec in default_specs:
            name = str(spec["name"])
            value_type = str(spec["type"])
            is_fixed_param = name in FIXED_PARAM_SPECS
            row_cols = st.columns([1.0, 1.8, 1.4, 1.4, 1.2, 0.8])
            if is_fixed_param:
                row_cols[0].checkbox("固定", value=False, disabled=True, key=f"mq01_fixed_enabled_{name}", label_visibility="collapsed")
            else:
                row_cols[0].checkbox("啟用", value=bool(spec["enabled"]), key=f"mq01_enabled_{name}", label_visibility="collapsed")
            row_cols[1].markdown(f"**{name}**  \n{base._clean_param_label(str(spec['label']))}")
            with row_cols[2]:
                if is_fixed_param:
                    _fixed_number_display(label="起點", key=f"mq01_fixed_start_{name}", value=spec["start"], value_type=value_type)
                else:
                    base._number_input(label="起點", key=f"mq01_start_{name}", value=spec["start"], value_type=value_type, step=spec["step"])
            with row_cols[3]:
                if is_fixed_param:
                    _fixed_number_display(label="終點", key=f"mq01_fixed_stop_{name}", value=spec["stop"], value_type=value_type)
                else:
                    base._number_input(label="終點", key=f"mq01_stop_{name}", value=spec["stop"], value_type=value_type, step=spec["step"])
            with row_cols[4]:
                if is_fixed_param:
                    _fixed_number_display(label="步長", key=f"mq01_fixed_step_{name}", value=spec["step"], value_type=value_type)
                else:
                    base._number_input(
                        label="步長",
                        key=f"mq01_step_{name}",
                        value=spec["step"],
                        value_type=value_type,
                        step=spec["step"] if value_type == "float" else 1,
                    )
            row_cols[5].write(base.TYPE_VALUE_LABELS.get(value_type, value_type))

    ui_specs = _normalize_param_specs(base._current_ui_specs(default_specs))
    try:
        estimated_total = estimate_run_count(mode, ui_specs, params_meta, seed_keep_count=seed_keep_count)
    except Exception as exc:
        estimated_total = 0
        if not config_hidden:
            st.warning(f"參數設定無法估算總組數：{exc}")
    else:
        if not config_hidden:
            st.info(f"預估本輪會測試 **{estimated_total:,}** 組。")

    run_block_reason = grid_run_block_reason(mode, estimated_total)
    if run_block_reason and not config_hidden:
        st.error(run_block_reason)

    runtime_settings = {
        "cpu_limit_pct": cpu_limit_pct,
        "memory_limit_pct": memory_limit_pct,
        "capital": capital,
        "slip_per_side": slip_per_side,
        "requested_workers": requested_workers,
        "effective_workers": effective_workers,
        "max_workers": effective_workers,
        "top_n": top_n,
        "seed_keep_count": seed_keep_count,
        "research_profile_tag": research_profile_tag,
        "development_years": int(development_years),
        "development_start_date": int(research_periods["development_start_date"]),
        "development_end_date": int(research_periods["development_end_date"]),
        "holdout_start_date": int(research_periods["holdout_start_date"]),
        "holdout_end_date": int(research_periods["holdout_end_date"]),
        "wfo_train_years": int(wfo_train_years),
        "wfo_test_years": int(wfo_test_years),
        "wfo_step_years": int(wfo_step_years),
        "wfo_gap_days": int(wfo_gap_days),
    }
    wfo_settings = {
        "train_years": int(wfo_train_years),
        "test_years": int(wfo_test_years),
        "step_years": int(wfo_step_years),
        "gap_days": int(wfo_gap_days),
    }
    hard_filters = {
        "min_trades": min_trades,
        "min_total_return": min_total_return,
        "max_mdd_pct": max_mdd_pct,
        "mdd_mode": mdd_mode,
        "mdd_start_pct": mdd_start_pct,
        "mdd_end_pct": mdd_end_pct,
        "mdd_step_pct": mdd_step_pct,
    }
    draft_pre_summary = base._build_pre_run_summary(
        mode_label=base.MODE_OPTIONS[mode],
        ui_specs=ui_specs,
        estimated_total=estimated_total,
        runtime_settings=runtime_settings,
        hard_filters=hard_filters,
        research_periods=research_periods,
    )
    package_root = Path(__file__).resolve().parent.parent

    active_job_id = str(st.session_state.get("mq01_active_job_id") or "").strip()
    controller_token = base._controller_token()
    if active_job_id:
        touch_job_heartbeat(active_job_id, controller=controller_token)

    view = base._build_live_view_state(
        params_meta=params_meta,
        slip_per_side=slip_per_side,
        xs_path=xs_path,
        minute_path=minute_path,
        daily_path=daily_path,
        script_name=script_name,
        active_job_id=active_job_id,
        research_profile_tag=research_profile_tag,
    )

    system_snapshot = collect_system_snapshot(
        max_workers=effective_workers,
        requested_workers=requested_workers,
        cpu_limit_pct=cpu_limit_pct,
        memory_limit_pct=memory_limit_pct,
    )

    if view["running_now"]:

        @st.fragment(run_every=4)
        def _render_live_running_workspace() -> None:
            current_active_job_id = str(st.session_state.get("mq01_active_job_id") or "").strip()
            if current_active_job_id:
                touch_job_heartbeat(current_active_job_id, controller=controller_token)

            refreshed_view = base._build_live_view_state(
                params_meta=params_meta,
                slip_per_side=slip_per_side,
                xs_path=xs_path,
                minute_path=minute_path,
                daily_path=daily_path,
                script_name=script_name,
                active_job_id=current_active_job_id,
                research_profile_tag=research_profile_tag,
            )
            if not refreshed_view["running_now"]:
                st.rerun()
                return

            refreshed_system_snapshot = collect_system_snapshot(
                max_workers=effective_workers,
                requested_workers=requested_workers,
                cpu_limit_pct=cpu_limit_pct,
                memory_limit_pct=memory_limit_pct,
            )
            _render_running_workspace(
                view=refreshed_view,
                active_job_id=current_active_job_id,
                historical_snapshot=historical_snapshot,
                research_periods=research_periods,
                system_snapshot=refreshed_system_snapshot,
                mode=mode,
                hard_filters=hard_filters,
            )

        _render_live_running_workspace()
        _, stop_clicked = base._render_action_bar(
            run_disabled=True,
            stop_enabled=True,
            export_payload=view["export_payload"],
            key_suffix=active_job_id or "running",
        )
        if not view["export_payload"]:
            st.caption("執行中尚未整理出可下載的 XS / TXT，若要保留當前結果可先停止並等待輸出完成。")
        if stop_clicked and active_job_id:
            request_stop(active_job_id)
            st.rerun()
        return

    run_clicked, _ = base._render_action_bar(
        run_disabled=bool(run_block_reason),
        stop_enabled=False,
        export_payload=view["export_payload"],
        key_suffix=active_job_id or "idle",
    )

    if run_clicked:
        base._reset_profile_state()
        job_id = create_job_request(
            {
                "mode": mode,
                "mode_label": base.MODE_OPTIONS[mode],
                "xs_path": xs_path,
                "minute_path": minute_path,
                "daily_path": daily_path,
                "script_name": script_name,
                "ui_param_specs": ui_specs,
                "params_meta": params_meta,
                "runtime_settings": runtime_settings,
                "hard_filters": hard_filters,
                "estimated_total": estimated_total,
                "pre_run_summary": draft_pre_summary,
            }
        )
        touch_job_heartbeat(job_id, controller=controller_token)
        launch_job_process(job_id, package_root=str(package_root))
        st.session_state["mq01_active_job_id"] = job_id
        st.session_state["mq01_hide_config"] = True
        st.session_state["mq01_live_export_cache"] = {}
        st.session_state[SHOW_HOLDOUT_KEY] = False
        st.session_state[AUTO_RUN_WFO_KEY] = False
        st.session_state.pop(WFO_RESULT_SESSION_KEY, None)
        st.session_state.pop(WFO_SIGNATURE_SESSION_KEY, None)
        st.rerun()

    current_snapshot = view.get("current_snapshot") or {}
    analysis_signature = _analysis_signature(
        current_snapshot=current_snapshot,
        research_periods=research_periods,
        minute_path=minute_path,
        daily_path=daily_path,
        script_name=script_name,
        capital=capital,
        slip_per_side=slip_per_side,
    )
    if st.session_state.get(ANALYSIS_SIGNATURE_KEY) != analysis_signature:
        st.session_state[ANALYSIS_SIGNATURE_KEY] = analysis_signature
        st.session_state[SHOW_HOLDOUT_KEY] = False
        st.session_state[AUTO_RUN_WFO_KEY] = False

    if not isinstance(current_snapshot.get("params"), dict) or not current_snapshot.get("params"):
        base._render_status_notices(view)
        st.info("目前還沒有最佳化結果。先在左邊選參數與區間，再按「開始最佳化」。")
        return

    params_json = json.dumps(current_snapshot["params"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snapshot_metrics_json = json.dumps(
        {
            "robust_score": current_snapshot.get("robust_score"),
            "composite_score": current_snapshot.get("composite_score"),
            "plateau_score": current_snapshot.get("plateau_score"),
            "worst_window_return": current_snapshot.get("worst_window_return"),
            "slip_stress_score": current_snapshot.get("slip_stress_score"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with st.spinner("正在整理最佳化區完整分析..."):
        development_snapshot = _cached_period_analysis(
            params_json=params_json,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            capital=int(capital),
            slip_per_side=float(slip_per_side),
            start_date=int(research_periods["development_start_date"]),
            end_date=int(research_periods["development_end_date"]),
        )
        qualification_report = _cached_development_qualification(
            params_json=params_json,
            minute_path=minute_path,
            daily_path=daily_path,
            script_name=script_name,
            capital=int(capital),
            slip_per_side=float(slip_per_side),
            snapshot_metrics_json=snapshot_metrics_json,
            research_profile_tag=research_profile_tag,
            development_years=int(development_years),
            development_start_date=int(research_periods["development_start_date"]),
            development_end_date=int(research_periods["development_end_date"]),
            holdout_start_date=int(research_periods["holdout_start_date"]),
            holdout_end_date=int(research_periods["holdout_end_date"]),
            development_label=str(research_periods.get("development_label") or ""),
            holdout_label=str(research_periods.get("holdout_label") or ""),
        )

    holdout_requested = bool(st.session_state.get(SHOW_HOLDOUT_KEY, False))
    holdout_snapshot: dict[str, Any] | None = None
    full_snapshot: dict[str, Any] | None = None
    holdout_validation: dict[str, Any] | None = None

    if holdout_requested:
        with st.spinner("正在整理驗證區與全期間比較..."):
            holdout_snapshot = _cached_period_analysis(
                params_json=params_json,
                minute_path=minute_path,
                daily_path=daily_path,
                script_name=script_name,
                capital=int(capital),
                slip_per_side=float(slip_per_side),
                start_date=int(research_periods["holdout_start_date"]),
                end_date=int(research_periods["holdout_end_date"]),
            )
            full_snapshot = _cached_period_analysis(
                params_json=params_json,
                minute_path=minute_path,
                daily_path=daily_path,
                script_name=script_name,
                capital=int(capital),
                slip_per_side=float(slip_per_side),
                start_date=int(research_periods["development_start_date"]),
                end_date=int(research_periods["holdout_end_date"]),
            )
            holdout_validation = _cached_holdout_validation(
                params_json=params_json,
                minute_path=minute_path,
                daily_path=daily_path,
                script_name=script_name,
                capital=int(capital),
                research_profile_tag=research_profile_tag,
                development_years=int(development_years),
                development_start_date=int(research_periods["development_start_date"]),
                development_end_date=int(research_periods["development_end_date"]),
                holdout_start_date=int(research_periods["holdout_start_date"]),
                holdout_end_date=int(research_periods["holdout_end_date"]),
                development_label=str(research_periods.get("development_label") or ""),
                holdout_label=str(research_periods.get("holdout_label") or ""),
            )

    post_action = _render_post_run_workspace(
        view=view,
        params_meta=params_meta,
        minute_path=minute_path,
        daily_path=daily_path,
        script_name=script_name,
        capital=int(capital),
        slip_per_side=float(slip_per_side),
        current_snapshot=current_snapshot,
        historical_snapshot=historical_snapshot,
        research_periods=research_periods,
        development_snapshot=development_snapshot,
        qualification_report=qualification_report,
        system_snapshot=system_snapshot,
        mode=mode,
        ui_specs=ui_specs,
        runtime_settings=runtime_settings,
        hard_filters=hard_filters,
        wfo_settings=wfo_settings,
        holdout_requested=holdout_requested,
        holdout_snapshot=holdout_snapshot,
        full_snapshot=full_snapshot,
        holdout_validation=holdout_validation,
    )
    if post_action == "full_validation":
        st.session_state[SHOW_HOLDOUT_KEY] = True
        st.session_state[AUTO_RUN_WFO_KEY] = True
        st.rerun()
    if post_action == "holdout_only":
        st.session_state[SHOW_HOLDOUT_KEY] = True
        st.session_state[AUTO_RUN_WFO_KEY] = False
        st.rerun()
