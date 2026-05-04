from __future__ import annotations

from functools import lru_cache
from typing import Any

from src.backtest.report import build_report
from src.data.data_loader import load_d1, load_m1
from src.data.dedupe_loader import dedupe
from src.research.modular_0313plus import is_modular_0313plus_template_choices
from src.strategy.strategy_0313plus import run_0313plus_backtest
from src.strategy.strategy_0313plus_modular import run_0313plus_modular_backtest

from .types import BacktestMetrics, CandidateProposal, ResearchConfig


def _coerce_numeric(value: Any) -> int | float | str:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    text = str(value).strip()
    try:
        parsed = float(text)
    except ValueError:
        return text
    return int(parsed) if parsed.is_integer() else parsed


@lru_cache(maxsize=4)
def _load_market_data(minute_path: str, daily_path: str) -> tuple[list[Any], list[Any]]:
    return dedupe(load_m1(minute_path)), dedupe(load_d1(daily_path))


def _extract_metrics(report: dict[str, Any]) -> dict[str, Any]:
    yearly_values: list[float] = []
    metrics = {
        "n_trades": int(report.get("n_trades", 0)),
        "total_return": float(report.get("total_return", 0.0)) * 100.0,
        "mdd_pct": float(report.get("mdd_pct", 0.0)) * 100.0,
    }
    for item in report.get("yearly_returns", []) or []:
        period = str(item.get("period", "")).strip()
        if not period:
            continue
        year_return = float(item.get("return", 0.0)) * 100.0
        yearly_values.append(year_return)
        metrics[f"year_return_{period}"] = year_return
    if yearly_values:
        year_avg = sum(yearly_values) / len(yearly_values)
        year_std = (sum((value - year_avg) ** 2 for value in yearly_values) / len(yearly_values)) ** 0.5
        loss_years = sum(1 for value in yearly_values if value < 0)
    else:
        year_avg = 0.0
        year_std = 0.0
        loss_years = 0
    metrics["year_avg_return"] = year_avg
    metrics["year_return_std"] = year_std
    metrics["loss_years"] = loss_years
    metrics["composite_score"] = (
        float(metrics["total_return"]) * 0.20
        + float(year_avg) * 0.15
        - float(metrics["mdd_pct"]) * 0.55
        - float(year_std) * 0.10
        - float(loss_years) * 5.0
    )
    return metrics


def _hard_filter_fail_reason(metrics: dict[str, Any], config: ResearchConfig) -> str | None:
    reasons: list[str] = []
    if int(metrics["n_trades"]) < int(config.min_trades):
        reasons.append(f"n_trades < {int(config.min_trades)}")
    if float(metrics["total_return"]) < float(config.min_total_return):
        reasons.append(f"total_return < {float(config.min_total_return):.2f}%")
    if float(metrics["mdd_pct"]) > float(config.max_mdd_pct):
        reasons.append(f"mdd_pct > {float(config.max_mdd_pct):.2f}%")
    return " | ".join(reasons) if reasons else None


def _format_trade_number(value: Any) -> str:
    try:
        numeric = float(value)
    except Exception:
        return str(value)
    if abs(numeric - round(numeric)) < 1e-9:
        return str(int(round(numeric)))
    return f"{numeric:.4f}".rstrip("0").rstrip(".")


def _direction_text(direction: Any) -> str:
    try:
        return "LONG" if int(direction) >= 0 else "SHORT"
    except Exception:
        return str(direction)


def _build_trade_lines(trades: list[Any]) -> list[str]:
    lines: list[str] = []
    for idx, trade in enumerate(trades or [], start=1):
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


def proposal_to_runtime_params(proposal: CandidateProposal) -> dict[str, Any]:
    return {str(name): _coerce_numeric(value) for name, value in proposal.params.items()}


def evaluate_candidate(config: ResearchConfig, proposal: CandidateProposal) -> BacktestMetrics:
    params = proposal_to_runtime_params(proposal)
    try:
        minute_bars, daily_bars = _load_market_data(config.minute_path, config.daily_path)
        if is_modular_0313plus_template_choices(proposal.template_choices):
            result = run_0313plus_modular_backtest(
                minute_bars,
                daily_bars,
                params,
                config.runtime_script_name,
                template_choices=proposal.template_choices,
                slip_per_side=float(config.slip_per_side),
            )
        else:
            result = run_0313plus_backtest(
                minute_bars,
                daily_bars,
                params,
                config.runtime_script_name,
                slip_per_side=float(config.slip_per_side),
            )
        report = build_report(result, daily_bars, int(config.capital))
        metrics = _extract_metrics(report)
        fail_reason = _hard_filter_fail_reason(metrics, config)
        trade_lines = _build_trade_lines(getattr(result, "trades", []) or [])
        return BacktestMetrics(
            total_return=float(metrics["total_return"]),
            mdd_pct=float(metrics["mdd_pct"]),
            n_trades=int(metrics["n_trades"]),
            year_avg_return=float(metrics["year_avg_return"]),
            year_return_std=float(metrics["year_return_std"]),
            loss_years=int(metrics["loss_years"]),
            composite_score=float(metrics["composite_score"]),
            fail_reason=fail_reason,
            passed_hard_filters=fail_reason is None,
            trade_lines=trade_lines,
        )
    except Exception as exc:
        return BacktestMetrics(
            total_return=-1e18,
            mdd_pct=1e18,
            n_trades=0,
            year_avg_return=0.0,
            year_return_std=0.0,
            loss_years=0,
            composite_score=-1e18,
            fail_reason=f"runtime_error: {exc}",
            passed_hard_filters=False,
            trade_lines=[],
        )
