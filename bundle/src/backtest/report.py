from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from math import inf, sqrt
from statistics import mean


def _to_date_obj(yyyymmdd: int):
    s = str(yyyymmdd)
    return datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]))


def build_nav_records(trades, daily_bars, capital: float):
    pnl_by_date = defaultdict(float)
    for t in trades:
        pnl_by_date[t.exit_date] += t.net_pnl

    records = []
    nav = capital

    for d in daily_bars:
        nav_before = nav
        nav += pnl_by_date[d.date]
        nav_after = nav
        records.append({
            "date": d.date,
            "nav_before": nav_before,
            "nav_after": nav_after,
            "daily_pnl": pnl_by_date[d.date],
        })

    return records


def build_trade_nav_records(trades, capital: float):
    records = []
    nav = capital

    for t in trades:
        nav_before = nav
        nav += t.net_pnl
        records.append({
            "date": t.exit_date,
            "time": t.exit_time,
            "nav_before": nav_before,
            "nav_after": nav,
            "trade_pnl": t.net_pnl,
        })

    return records


def calc_mdd(nav_records, capital: float | None = None):
    if not nav_records:
        return 0.0, 0.0

    peak = -inf
    max_dd_amount = 0.0
    max_dd_pct = 0.0

    for r in nav_records:
        nav = r["nav_after"]
        if nav > peak:
            peak = nav

        dd_amount = peak - nav
        if capital is not None and capital > 0:
            dd_pct = dd_amount / capital
        else:
            dd_pct = dd_amount / peak if peak > 0 else 0.0

        if dd_amount > max_dd_amount:
            max_dd_amount = dd_amount

        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    return max_dd_amount, max_dd_pct


def calc_total_return(nav_records, capital: float):
    if not nav_records:
        return 0.0

    final_nav = nav_records[-1]["nav_after"]
    return (final_nav / capital) - 1.0


def _group_key(date_int: int, mode: str):
    d = _to_date_obj(date_int)

    if mode == "day":
        return f"{d.year}-{d.month:02d}-{d.day:02d}"

    if mode == "week":
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"

    if mode == "month":
        return f"{d.year}-{d.month:02d}"

    if mode == "quarter":
        q = (d.month - 1) // 3 + 1
        return f"{d.year}-Q{q}"

    if mode == "halfyear":
        h = 1 if d.month <= 6 else 2
        return f"{d.year}-H{h}"

    if mode == "year":
        return f"{d.year}"

    raise ValueError(f"未知 mode: {mode}")


def calc_period_returns(nav_records, mode: str, capital: float | None = None):
    if not nav_records:
        return []

    groups = []
    current_key = None
    start_nav = None
    end_nav = None
    period_pnl = 0.0

    for r in nav_records:
        k = _group_key(r["date"], mode)

        if current_key is None:
            current_key = k
            start_nav = r["nav_before"]
            end_nav = r["nav_after"]
            period_pnl = float(r.get("daily_pnl", 0.0))
            continue

        if k != current_key:
            denominator = float(capital) if capital is not None and float(capital) > 0 else float(start_nav or 0.0)
            ret = (period_pnl / denominator) if denominator > 0 else 0.0
            groups.append({
                "period": current_key,
                "start_nav": start_nav,
                "end_nav": end_nav,
                "pnl": period_pnl,
                "return": ret,
            })

            current_key = k
            start_nav = r["nav_before"]
            end_nav = r["nav_after"]
            period_pnl = float(r.get("daily_pnl", 0.0))
        else:
            end_nav = r["nav_after"]
            period_pnl += float(r.get("daily_pnl", 0.0))

    if current_key is not None:
        denominator = float(capital) if capital is not None and float(capital) > 0 else float(start_nav or 0.0)
        ret = (period_pnl / denominator) if denominator > 0 else 0.0
        groups.append({
            "period": current_key,
            "start_nav": start_nav,
            "end_nav": end_nav,
            "pnl": period_pnl,
            "return": ret,
        })

    return groups


def build_report(result, daily_bars, capital: float):
    nav_records = build_nav_records(result.trades, daily_bars, capital)
    trade_nav_records = build_trade_nav_records(result.trades, capital)
    mdd_amount, mdd_pct = calc_mdd(trade_nav_records, capital=capital)
    total_return = calc_total_return(nav_records, capital)

    return {
        "n_trades": len(result.trades),
        "total_return": total_return,
        "mdd_amount": mdd_amount,
        "mdd_pct": mdd_pct,
        "weekly_returns": calc_period_returns(nav_records, "week", capital=capital),
        "monthly_returns": calc_period_returns(nav_records, "month", capital=capital),
        "quarterly_returns": calc_period_returns(nav_records, "quarter", capital=capital),
        "halfyear_returns": calc_period_returns(nav_records, "halfyear", capital=capital),
        "yearly_returns": calc_period_returns(nav_records, "year", capital=capital),
    }


def _safe_div(numerator: float, denominator: float) -> float:
    if abs(denominator) < 1e-12:
        return 0.0
    return numerator / denominator


def _sample_std(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    avg = sum(values) / len(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return sqrt(max(variance, 0.0))


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    weight = pos - low
    return ordered[low] * (1.0 - weight) + ordered[high] * weight


def _group_trade_pnl_by_period(trades, mode: str) -> list[float]:
    totals = defaultdict(float)
    for trade in trades:
        totals[_group_key(trade.exit_date, mode)] += float(trade.net_pnl)
    return list(totals.values())


def _time_to_recovery_in_trades(trade_nav_records) -> int:
    peak_nav = 0.0
    peak_index = -1
    current_underwater_start = None
    longest = 0
    for idx, record in enumerate(trade_nav_records):
        nav = float(record["nav_after"])
        if nav >= peak_nav:
            peak_nav = nav
            peak_index = idx
            if current_underwater_start is not None:
                longest = max(longest, idx - current_underwater_start)
                current_underwater_start = None
        elif current_underwater_start is None:
            current_underwater_start = peak_index if peak_index >= 0 else idx
    if current_underwater_start is not None and trade_nav_records:
        longest = max(longest, len(trade_nav_records) - 1 - current_underwater_start)
    return max(longest, 0)


def _ulcer_index(nav_records) -> float:
    if not nav_records:
        return 0.0
    peak = -inf
    squares: list[float] = []
    for record in nav_records:
        nav = float(record["nav_after"])
        if nav > peak:
            peak = nav
        drawdown_pct = _safe_div(peak - nav, peak)
        squares.append(drawdown_pct ** 2)
    return sqrt(sum(squares) / len(squares)) if squares else 0.0


def _equity_stability_r2(nav_records) -> float:
    if len(nav_records) <= 1:
        return 0.0
    xs = list(range(len(nav_records)))
    ys = [float(record["nav_after"]) for record in nav_records]
    x_avg = mean(xs)
    y_avg = mean(ys)
    sxx = sum((x - x_avg) ** 2 for x in xs)
    syy = sum((y - y_avg) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return 0.0
    sxy = sum((x - x_avg) * (y - y_avg) for x, y in zip(xs, ys))
    r = sxy / sqrt(sxx * syy)
    return max(0.0, min(1.0, r * r))


def _avg_holding_minutes(trades) -> float:
    if not trades:
        return 0.0
    durations = []
    for trade in trades:
        entry = datetime.strptime(f"{trade.entry_date}{trade.entry_time:06d}", "%Y%m%d%H%M%S")
        exit_ = datetime.strptime(f"{trade.exit_date}{trade.exit_time:06d}", "%Y%m%d%H%M%S")
        durations.append((exit_ - entry).total_seconds() / 60.0)
    return sum(durations) / len(durations)


def _ts_to_datetime(date_int: int, time_int: int):
    return datetime.strptime(f"{int(date_int)}{int(time_int):06d}", "%Y%m%d%H%M%S")


def _date_week_key(dt: datetime) -> str:
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _calc_js_style_kpi(trades, capital: float, slip_per_side: float, point_value: float):
    n = len(trades)
    if n == 0:
        return {}, {}

    theo_pnls = [float(t.net_pnl + t.slip_cost) for t in trades]
    act_pnls = [float(t.net_pnl) for t in trades]
    dates = [_ts_to_datetime(t.exit_date, t.exit_time) for t in trades]

    def calc_one(pnls, include_slip: bool):
        total = 0.0
        equity = []
        for pnl in pnls:
            total += pnl
            equity.append(total)

        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_profit = sum(wins)
        gross_loss = sum(losses)
        avg = sum(pnls) / n
        sum_sq = sum(p * p for p in pnls)
        variance = max(sum_sq / n - avg * avg, 0.0)
        stdev = sqrt(variance)
        sharpe = (avg / stdev) * sqrt(n) if stdev > 0 else None

        downside = [p for p in pnls if p < 0]
        downside_sq = sum(p * p for p in downside)
        downside_dev = sqrt(downside_sq / len(downside)) if downside else 0.0
        sortino = (avg / downside_dev) * sqrt(n) if downside_dev > 0 else None

        peak = 0.0
        max_dd = 0.0
        max_dd_start_idx = 0
        max_dd_end_idx = 0
        cur_peak_idx = 0
        for idx, value in enumerate(equity):
            if value > peak:
                peak = value
                cur_peak_idx = idx
            dd = peak - value
            if dd > max_dd:
                max_dd = dd
                max_dd_start_idx = cur_peak_idx
                max_dd_end_idx = idx

        total_return_pct = _safe_div(total, capital)
        max_dd_pct = _safe_div(max_dd, capital)

        cagr = None
        if dates and dates[-1] > dates[0] and capital > 0:
            days = (dates[-1] - dates[0]).total_seconds() / 86400.0
            years = days / 365.0
            if years > 0:
                final_nav = capital + total
                ratio = final_nav / capital
                if ratio > 0:
                    cagr = ratio ** (1.0 / years) - 1.0

        calmar = (cagr / max_dd_pct) if (cagr is not None and max_dd_pct and max_dd_pct > 0) else None
        time_to_recovery = max_dd_end_idx - max_dd_start_idx if max_dd_end_idx > max_dd_start_idx else 0

        nav = [capital + value for value in equity]
        peak_nav = 0.0
        sum_dd_sq = 0.0
        for value in nav:
            if value > peak_nav:
                peak_nav = value
            dd_pct = _safe_div(peak_nav - value, peak_nav) if peak_nav > 0 else 0.0
            sum_dd_sq += dd_pct * dd_pct
        ulcer = sqrt(sum_dd_sq / len(nav)) if nav else None
        recovery = _safe_div(total, max_dd) if max_dd > 0 else None

        day_map = defaultdict(float)
        week_map = defaultdict(float)
        for pnl, dt in zip(pnls, dates):
            day_map[dt.strftime('%Y%m%d')] += pnl
            week_map[_date_week_key(dt)] += pnl
        worst_day = min([0.0] + list(day_map.values()))
        worst_week = min([0.0] + list(week_map.values()))

        ordered = sorted(pnls)
        idx = int((1.0 - 0.95) * len(ordered))
        idx = min(max(idx, 0), len(ordered) - 1)
        var_loss = -ordered[idx] if ordered else None
        tail = ordered[:idx + 1] if ordered else []
        cvar_loss = -(sum(tail) / len(tail)) if tail else None

        avg_win = gross_profit / len(wins) if wins else 0.0
        avg_loss = gross_loss / len(losses) if losses else 0.0
        payoff = (avg_win / abs(avg_loss)) if avg_loss < 0 else None
        expectancy = avg
        pf = (gross_profit / abs(gross_loss)) if gross_loss < 0 else None
        win_rate = len(wins) / n if n else 0.0
        kelly = None
        if payoff is not None and payoff > 0 and 0 < win_rate < 1:
            p = win_rate
            q = 1.0 - p
            kelly = p - q / payoff

        if variance <= 0:
            risk_ruin = None
        elif avg <= 0:
            risk_ruin = 1.0
        else:
            exponent = -2.0 * avg * capital / variance
            risk_ruin = max(0.0, min(1.0, pow(2.718281828459045, exponent)))

        total_fee = sum(float(t.fee) for t in trades)
        total_tax = sum(float(t.tax) for t in trades)
        total_slip = sum(float(t.slip_cost) for t in trades) if include_slip else 0.0
        total_cost = total_fee + total_tax + total_slip
        total_gross_abs = gross_profit + abs(gross_loss)
        turnover_notional = sum(float(t.entry_price) * point_value for t in trades)
        turnover = _safe_div(turnover_notional, capital)
        cost_ratio = (total_cost / total_gross_abs) if total_gross_abs > 0 else None
        trading_days = len(day_map)
        trades_per_day = (n / trading_days) if trading_days > 0 else None
        avg_hold_min = sum((_ts_to_datetime(t.exit_date, t.exit_time) - _ts_to_datetime(t.entry_date, t.entry_time)).total_seconds() / 60.0 for t in trades) / n

        stability = None
        if len(nav) >= 3:
            xs = [i + 1 for i in range(len(nav))]
            ys = nav
            count = len(xs)
            sum_x = sum(xs)
            sum_y = sum(ys)
            sum_xy = sum(x * y for x, y in zip(xs, ys))
            sum_x2 = sum(x * x for x in xs)
            mean_y = sum_y / count
            ss_tot = sum((y - mean_y) ** 2 for y in ys)
            denom = count * sum_x2 - sum_x * sum_x
            if denom != 0 and ss_tot > 0:
                slope = (count * sum_xy - sum_x * sum_y) / denom
                intercept = mean_y - slope * (sum_x / count)
                ss_res = sum((ys[i] - (slope * xs[i] + intercept)) ** 2 for i in range(count))
                stability = 1.0 - (ss_res / ss_tot)

        return {
            'totalNet': total,
            'totalReturnPct': total_return_pct,
            'cagr': cagr,
            'maxDd': max_dd,
            'maxDdPct': max_dd_pct,
            'ulcerIndex': ulcer,
            'recoveryFactor': recovery,
            'timeToRecoveryTrades': time_to_recovery,
            'worstDayPnl': worst_day,
            'worstWeekPnl': worst_week,
            'varLoss': var_loss,
            'cvarLoss': cvar_loss,
            'riskOfRuin': risk_ruin,
            'volPerTrade': stdev,
            'sharpeTrade': sharpe,
            'sortinoTrade': sortino,
            'calmar': calmar,
            'nTrades': n,
            'winRate': win_rate,
            'avg': avg,
            'avgWin': avg_win,
            'avgLoss': avg_loss,
            'payoff': payoff,
            'expectancy': expectancy,
            'pf': pf,
            'grossProfit': gross_profit,
            'grossLoss': gross_loss,
            'largestWin': max(pnls) if pnls else None,
            'largestLoss': min(pnls) if pnls else None,
            'kelly': kelly,
            'stabilityR2': stability,
            'tradingDays': trading_days,
            'tradesPerDay': trades_per_day,
            'avgHoldMin': avg_hold_min,
            'turnover': turnover,
            'totalFee': total_fee,
            'totalTax': total_tax,
            'totalSlipCost': total_slip,
            'totalCost': total_cost,
            'costRatio': cost_ratio,
        }

    return calc_one(theo_pnls, False), calc_one(act_pnls, True)


def build_kpi_snapshot(result, daily_bars, capital: float, point_value: float = 200.0, slip_per_side: float = 0.0):
    trades = list(result.trades)
    theo, act = _calc_js_style_kpi(trades, capital, slip_per_side, point_value)
    rows = [
        ('建議優化指標', '勝率 Hit Rate', 'winRate'),
        ('Tier 1 - 生存與尾端風險', '最大回撤率 Max Drawdown %', 'maxDdPct'),
        ('Tier 1 - 生存與尾端風險', '最大回撤金額 Max Drawdown', 'maxDd'),
        ('Tier 1 - 生存與尾端風險', '破產風險 Risk of Ruin（近似）', 'riskOfRuin'),
        ('Tier 1 - 生存與尾端風險', '最差單日損益 Worst Day PnL', 'worstDayPnl'),
        ('Tier 1 - 生存與尾端風險', '最差單週損益 Worst Week PnL', 'worstWeekPnl'),
        ('Tier 1 - 生存與尾端風險', '95% VaR（單筆）', 'varLoss'),
        ('Tier 1 - 生存與尾端風險', '95% CVaR（單筆）', 'cvarLoss'),
        ('Tier 1 - 生存與尾端風險', '回神時間 Time to Recovery（筆）', 'timeToRecoveryTrades'),
        ('Tier 1 - 生存與尾端風險', 'Ulcer Index', 'ulcerIndex'),
        ('Tier 1 - 生存與尾端風險', 'Recovery Factor', 'recoveryFactor'),
        ('Tier 2 - 報酬與風險調整後報酬', '總淨利 Net Profit', 'totalNet'),
        ('Tier 2 - 報酬與風險調整後報酬', '總報酬率 Total Return', 'totalReturnPct'),
        ('Tier 2 - 報酬與風險調整後報酬', '年化報酬率 CAGR', 'cagr'),
        ('Tier 2 - 報酬與風險調整後報酬', '單筆波動（交易級 Volatility）', 'volPerTrade'),
        ('Tier 2 - 報酬與風險調整後報酬', 'Sharpe Ratio（交易級）', 'sharpeTrade'),
        ('Tier 2 - 報酬與風險調整後報酬', 'Sortino Ratio（交易級）', 'sortinoTrade'),
        ('Tier 2 - 報酬與風險調整後報酬', 'Calmar Ratio', 'calmar'),
        ('Tier 3 - 交易品質與結構', '交易筆數 #Trades', 'nTrades'),
        ('Tier 3 - 交易品質與結構', '勝率 Hit Rate', 'winRate'),
        ('Tier 3 - 交易品質與結構', '平均單筆損益 Avg Trade PnL', 'avg'),
        ('Tier 3 - 交易品質與結構', '平均獲利 Avg Win', 'avgWin'),
        ('Tier 3 - 交易品質與結構', '平均虧損 Avg Loss', 'avgLoss'),
        ('Tier 3 - 交易品質與結構', '賺賠比 Payoff Ratio', 'payoff'),
        ('Tier 3 - 交易品質與結構', '單筆期望值 Expectancy', 'expectancy'),
        ('Tier 3 - 交易品質與結構', '獲利因子 Profit Factor', 'pf'),
        ('Tier 3 - 交易品質與結構', '總獲利 Gross Profit', 'grossProfit'),
        ('Tier 3 - 交易品質與結構', '總虧損 Gross Loss', 'grossLoss'),
        ('Tier 3 - 交易品質與結構', '最大獲利單 Largest Win', 'largestWin'),
        ('Tier 3 - 交易品質與結構', '最大虧損單 Largest Loss', 'largestLoss'),
        ('Tier 3 - 交易品質與結構', 'Kelly Fraction（理論值）', 'kelly'),
        ('Tier 4 - 路徑與穩定度', 'Equity Stability R²', 'stabilityR2'),
        ('Tier 4 - 路徑與穩定度', 'Alpha / Beta / Correlation', None),
        ('Tier 5 - 成本、槓桿與執行', '交易天數 Trading Days', 'tradingDays'),
        ('Tier 5 - 成本、槓桿與執行', '平均每日交易數 Trades / Day', 'tradesPerDay'),
        ('Tier 5 - 成本、槓桿與執行', '平均持倉時間 Avg Holding Time（分鐘）', 'avgHoldMin'),
        ('Tier 5 - 成本、槓桿與執行', '名目週轉率 Turnover（Notional / Capital）', 'turnover'),
        ('Tier 5 - 成本、槓桿與執行', '成本佔交易金額比 Transaction Cost Ratio', 'costRatio'),
        ('Tier 5 - 成本、槓桿與執行', '手續費總額 Total Commission', 'totalFee'),
        ('Tier 5 - 成本、槓桿與執行', '交易稅總額 Total Tax', 'totalTax'),
        ('Tier 5 - 成本、槓桿與執行', '滑價成本總額 Slippage Cost', 'totalSlipCost'),
        ('Tier 5 - 成本、槓桿與執行', '總交易成本 Total Trading Cost', 'totalCost'),
    ]
    return [
        {
            'section': section,
            'metric': metric,
            'theoretical': (theo.get(key) if key else None),
            'actual': (act.get(key) if key else None),
        }
        for section, metric, key in rows
    ]
