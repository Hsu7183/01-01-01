from __future__ import annotations

import json
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass
class AutoOptConfig:
    n_rounds: int = 3
    sample_size: int = 400
    top_k: int = 50
    capital: float = 1_000_000.0

    # 硬篩選
    max_mdd_pct: float = 0.35
    min_annual_return: float = -1.0
    min_n_trades: int = 1


def _rand(low, high):
    if isinstance(low, int) and isinstance(high, int):
        return random.randint(int(low), int(high))
    return random.uniform(low, high)


def _calc_years_from_daily_bars(daily_bars) -> float:
    if not daily_bars or len(daily_bars) < 2:
        return 1.0

    start = daily_bars[0].date
    end = daily_bars[-1].date

    s = str(start)
    e = str(end)

    y1, m1, d1 = int(s[0:4]), int(s[4:6]), int(s[6:8])
    y2, m2, d2 = int(e[0:4]), int(e[4:6]), int(e[6:8])

    from datetime import datetime
    dt1 = datetime(y1, m1, d1)
    dt2 = datetime(y2, m2, d2)

    days = max((dt2 - dt1).days, 1)
    return max(days / 365.25, 1 / 365.25)


def _extract_yearly_return_map(yearly_returns) -> dict[str, float]:
    """
    你的 report.py 回傳的 yearly_returns 是 list[dict]，每個元素像：
    {
        "period": "2022",
        "start_nav": ...,
        "end_nav": ...,
        "return": 0.12
    }
    """
    result = {}

    if not yearly_returns:
        return result

    if isinstance(yearly_returns, list):
        for row in yearly_returns:
            if isinstance(row, dict):
                period = row.get("period")
                ret = row.get("return")
                if period is not None and ret is not None:
                    result[str(period)] = float(ret)

    elif isinstance(yearly_returns, dict):
        for k, v in yearly_returns.items():
            result[str(k)] = float(v)

    return result


def _extract_quarterly_returns(quarterly_returns) -> list[float]:
    vals = []
    if not quarterly_returns:
        return vals

    if isinstance(quarterly_returns, list):
        for row in quarterly_returns:
            if isinstance(row, dict) and row.get("return") is not None:
                vals.append(float(row["return"]))

    return vals


def _stability_from_yearly_map(yearly_map: dict[str, float]) -> float:
    if not yearly_map:
        return 0.0

    vals = list(yearly_map.values())
    if len(vals) == 0:
        return 0.0

    mean = sum(vals) / len(vals)
    var = sum((x - mean) ** 2 for x in vals) / len(vals)
    std = math.sqrt(var)
    return 1.0 / (1.0 + std)


def _calc_annual_return_from_total_return(total_return: float, years: float) -> float:
    if years <= 0:
        return total_return

    base = 1.0 + total_return
    if base <= 0:
        return -1.0

    return base ** (1.0 / years) - 1.0


def _normalize_report(rep: dict[str, Any], daily_bars, capital: float) -> dict[str, Any]:
    total_return = float(rep.get("total_return", 0.0))
    mdd_pct = float(rep.get("mdd_pct", 0.0))
    mdd_amount = float(rep.get("mdd_amount", 0.0))
    n_trades = int(rep.get("n_trades", 0))

    yearly_map = _extract_yearly_return_map(rep.get("yearly_returns", []))
    quarterly_vals = _extract_quarterly_returns(rep.get("quarterly_returns", []))

    years = _calc_years_from_daily_bars(daily_bars)
    annual_return = _calc_annual_return_from_total_return(total_return, years)

    worst_quarter = min(quarterly_vals) if quarterly_vals else 0.0
    avg_trades_per_year = n_trades / years if years > 0 else n_trades
    stability = _stability_from_yearly_map(yearly_map)

    return {
        "n_trades": n_trades,
        "total_return": total_return,
        "annual_return": annual_return,
        "mdd_amount": mdd_amount,
        "mdd_pct": mdd_pct,
        "yearly_map": yearly_map,
        "quarterly_vals": quarterly_vals,
        "worst_quarter": worst_quarter,
        "avg_trades_per_year": avg_trades_per_year,
        "stability": stability,
        "raw_report": rep,
    }


def _score(rep: dict[str, Any]) -> float:
    """
    第一版評分：
    - 年化報酬越高越好
    - MDD 越低越好
    - 年度穩定度越高越好
    """
    annual_return = rep.get("annual_return", 0.0)
    mdd_pct = rep.get("mdd_pct", 1.0)
    stability = rep.get("stability", 0.0)

    return annual_return * 0.5 - mdd_pct * 0.3 + stability * 0.2


class AutoOptimizer:
    def __init__(self, ranges, base, bt, rp, m1, d1, cfg: AutoOptConfig):
        self.ranges = ranges
        self.base = base
        self.bt = bt
        self.rp = rp
        self.m1 = m1
        self.d1 = d1
        self.cfg = cfg

    def _build_params(self, new):
        p = dict(self.base)
        p.update(new)

        for k, v in list(p.items()):
            if isinstance(v, float):
                p[k] = round(v, 6)

        return p

    def _call_backtest(self, params):
        """
        先嘗試原始正確介面：
        run_0313plus_backtest(minute_bars, daily_bars, params, script_name)

        若你的其他地方仍有舊版介面，才 fallback。
        """
        try:
            return self.bt(self.m1, self.d1, params, "0313plus")
        except TypeError:
            return self.bt("0313plus", params, self.m1, self.d1)

    def _call_report(self, result):
        """
        對齊你目前的 report.py：
        build_report(result, daily_bars, capital)
        """
        return self.rp(result, self.d1, self.cfg.capital)

    def _passes_filters(self, rep: dict[str, Any]) -> bool:
        if rep["mdd_pct"] > self.cfg.max_mdd_pct:
            return False
        if rep["annual_return"] < self.cfg.min_annual_return:
            return False
        if rep["n_trades"] < self.cfg.min_n_trades:
            return False
        return True

    def _run_one(self, new):
        params = self._build_params(new)

        result = self._call_backtest(params)
        if result is None:
            return None

        rep_raw = self._call_report(result)
        rep = _normalize_report(rep_raw, self.d1, self.cfg.capital)

        if not self._passes_filters(rep):
            return None

        return {
            **new,
            "score": _score(rep),
            "annual_return": rep["annual_return"],
            "total_return": rep["total_return"],
            "mdd_pct": rep["mdd_pct"],
            "mdd_amount": rep["mdd_amount"],
            "n_trades": rep["n_trades"],
            "avg_trades_per_year": rep["avg_trades_per_year"],
            "worst_quarter": rep["worst_quarter"],
            "stability": rep["stability"],
        }

    def _shrink(self, df: pd.DataFrame):
        new = {}

        for k in self.ranges:
            vals = df[k]

            low = vals.quantile(0.1)
            high = vals.quantile(0.9)
            span = high - low

            if span == 0:
                new[k] = self.ranges[k]
                continue

            low2 = low - span * 0.1
            high2 = high + span * 0.1

            # 保持在原始宇宙範圍內
            orig_low, orig_high = self.ranges[k]
            low2 = max(low2, orig_low)
            high2 = min(high2, orig_high)

            new[k] = (low2, high2)

        return new

    def run(self):
        all_df = []

        for r in range(self.cfg.n_rounds):
            print(f"\n=== Round {r+1} ===")

            rows = []
            start_ts = time.time()

            for i in range(self.cfg.sample_size):
                new = {k: _rand(v[0], v[1]) for k, v in self.ranges.items()}
                res = self._run_one(new)

                if res:
                    rows.append(res)

                if i == 0 or (i + 1) % 20 == 0:
                    done = i + 1
                    elapsed = time.time() - start_ts
                    speed = done / elapsed if elapsed > 0 else 0.0
                    remain = (self.cfg.sample_size - done) / speed if speed > 0 else 0.0
                    pct = done / self.cfg.sample_size * 100.0

                    print(
                        f"[ {done}/{self.cfg.sample_size} ] "
                        f"{pct:5.1f}% | "
                        f"elapsed: {elapsed:5.1f}s | "
                        f"ETA: {remain:5.1f}s | "
                        f"valid: {len(rows)}"
                    )

            if not rows:
                print("No valid results in this round")
                continue

            df = pd.DataFrame(rows).sort_values("score", ascending=False)
            top = df.head(self.cfg.top_k)

            top.to_csv(
                f"auto_opt_round_{r+1}.csv",
                index=False,
                encoding="utf-8-sig",
            )

            self.ranges = self._shrink(top)
            all_df.append(top)

        if not all_df:
            print("\nNo optimization result.")
            return

        final = pd.concat(all_df).sort_values("score", ascending=False)
        final.to_csv("auto_opt_best.csv", index=False, encoding="utf-8-sig")

        Path("auto_opt_ranges.json").write_text(
            json.dumps(self.ranges, indent=2, ensure_ascii=False),
            encoding="utf-8-sig",
        )

        print("\n=== DONE ===")
        print(final.head(10).to_string(index=False))