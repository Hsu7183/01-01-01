from __future__ import annotations

from typing import Any

from src.core.models import BacktestResult, Trade
from src.strategy.strategy_0313plus import _build_daily_anchor, _normalize_backtest_args


def _coerce_choice(value: Any, default: str) -> str:
    text = str(value or "").strip()
    return text or default


def _coerce_toggle(value: Any, default: int = 1) -> int:
    if value in (None, ""):
        return int(default)
    try:
        return 1 if int(float(str(value))) != 0 else 0
    except Exception:
        return int(default)


def _resolve_template_choices(template_choices: dict[str, Any] | None) -> dict[str, Any]:
    choices = dict(template_choices or {})
    return {
        "bias_mode": _coerce_choice(choices.get("bias_mode"), "ma_or_ema_cdp"),
        "entry_mode": _coerce_choice(choices.get("entry_mode"), "nhnl_or_don"),
        "atr_filter_mode": _coerce_choice(choices.get("atr_filter_mode"), "on"),
        "use_atr_stop": _coerce_toggle(choices.get("use_atr_stop"), 1),
        "use_atr_tp": _coerce_toggle(choices.get("use_atr_tp"), 1),
        "use_time_stop": _coerce_toggle(choices.get("use_time_stop"), 1),
        "use_trail_exit": _coerce_toggle(choices.get("use_trail_exit"), 1),
    }


def _resolve_bias(anchor: dict[str, Any], bias_mode: str) -> tuple[bool, bool]:
    ma_long = bool(anchor["ma2D"] > anchor["ma3D"])
    ma_short = bool(anchor["ma2D"] < anchor["ma3D"])
    ema_long = bool(anchor["ema2D"] > anchor["ema3D"])
    ema_short = bool(anchor["ema2D"] < anchor["ema3D"])
    cdp_long = bool(anchor["yC"] > anchor["cdpVal"])
    cdp_short = bool(anchor["yC"] < anchor["cdpVal"])

    if bias_mode == "ma_only_cdp":
        return ma_long and cdp_long, ma_short and cdp_short
    if bias_mode == "ema_only_cdp":
        return ema_long and cdp_long, ema_short and cdp_short
    if bias_mode == "ma_and_ema_cdp":
        return (ma_long and ema_long and cdp_long), (ma_short and ema_short and cdp_short)
    return ((ma_long or ema_long) and cdp_long), ((ma_short or ema_short) and cdp_short)


def _resolve_entry_ready(
    *,
    bar_open: float,
    long_bias: bool,
    short_bias: bool,
    atr_d: float,
    min_atr_d: float,
    long_entry_level_nh: float,
    long_entry_level_don: float,
    short_entry_level_nl: float,
    short_entry_level_don: float,
    entry_mode: str,
    atr_filter_mode: str,
) -> tuple[bool, bool]:
    atr_ok = True if atr_filter_mode == "off" else atr_d >= min_atr_d
    long_nhnl = bar_open >= long_entry_level_nh
    long_don = bar_open >= long_entry_level_don
    short_nhnl = bar_open <= short_entry_level_nl
    short_don = bar_open <= short_entry_level_don

    if entry_mode == "nhnl_only":
        return long_bias and atr_ok and long_nhnl, short_bias and atr_ok and short_nhnl
    if entry_mode == "don_only":
        return long_bias and atr_ok and long_don, short_bias and atr_ok and short_don
    if entry_mode == "nhnl_and_don":
        return long_bias and atr_ok and long_nhnl and long_don, short_bias and atr_ok and short_nhnl and short_don
    return long_bias and atr_ok and (long_nhnl or long_don), short_bias and atr_ok and (short_nhnl or short_don)


def run_0313plus_modular_backtest(
    arg1,
    arg2,
    arg3,
    arg4,
    *,
    template_choices: dict[str, Any] | None = None,
    point_value: float = 200.0,
    fee_per_side: float = 45.0,
    tax_rate: float = 0.00002,
    slip_per_side: float = 0.0,
) -> BacktestResult:
    minute_bars, daily_bars, params, script_name = _normalize_backtest_args(arg1, arg2, arg3, arg4)
    choices = _resolve_template_choices(template_choices)
    result = BacktestResult(script_name=script_name, params=params, trades=[])

    if not minute_bars or not daily_bars:
        return result

    fixedBeginTime = 84800
    fixedEndTime = 124000
    fixedForceExitTime = 131200
    fixedMALen3 = 5
    fixedEMALen3 = 5
    warmupBars = max(
        fixedMALen3 + 2,
        fixedEMALen3 + 2,
        int(params["DonLen"]) + 2,
        int(params["ATRLen"]) + 2,
        int(params["EMAWarmBars"]) + 2,
    )

    posFlag = 0
    cost = 0.0
    entryATRD = 0.0
    dayEntryCount = 0
    entryBarNo = 0
    bestHighSinceEntry = 0.0
    bestLowSinceEntry = 0.0
    maxRunUpPts = 0.0
    maxRunDnPts = 0.0
    barsHeld = 0
    dayAnchorOpen = 0.0
    minRunPtsByAnchor = 0.0
    trailStartPtsByAnchor = 0.0
    trailGivePtsByAnchor = 0.0
    anchorBackPtsByAnchor = 0.0
    current_trade_entry = None
    current_daily_anchor = None
    current_date = None

    for i, bar in enumerate(minute_bars):
        bar_no = i + 1
        if current_date != bar.date:
            current_date = bar.date
            current_daily_anchor = _build_daily_anchor(bar.date, daily_bars, params)
            posFlag = 0
            cost = 0.0
            entryATRD = 0.0
            dayEntryCount = 0
            entryBarNo = 0
            bestHighSinceEntry = 0.0
            bestLowSinceEntry = 0.0
            maxRunUpPts = 0.0
            maxRunDnPts = 0.0
            barsHeld = 0
            dayAnchorOpen = 0.0
            minRunPtsByAnchor = 0.0
            trailStartPtsByAnchor = 0.0
            trailGivePtsByAnchor = 0.0
            anchorBackPtsByAnchor = 0.0
            current_trade_entry = None

        if current_daily_anchor is None:
            continue

        sessOnEntry = 1 if (bar.time >= fixedBeginTime and bar.time <= fixedEndTime) else 0
        sessOnManage = 1 if (bar.time >= fixedBeginTime and bar.time <= fixedForceExitTime) else 0

        if (bar.time == fixedBeginTime) and (dayAnchorOpen == 0):
            dayAnchorOpen = bar.open
        if dayAnchorOpen > 0:
            minRunPtsByAnchor = dayAnchorOpen * float(params["MinRunPctAnchor"]) * 0.01
            trailStartPtsByAnchor = dayAnchorOpen * float(params["TrailStartPctAnchor"]) * 0.01
            trailGivePtsByAnchor = dayAnchorOpen * float(params["TrailGivePctAnchor"]) * 0.01
            anchorBackPtsByAnchor = dayAnchorOpen * float(params["AnchorBackPct"]) * 0.01

        if posFlag != 0 and i > 0 and bar_no > entryBarNo:
            prev_bar = minute_bars[i - 1]
            if posFlag == 1:
                if prev_bar.high > bestHighSinceEntry:
                    bestHighSinceEntry = prev_bar.high
                if prev_bar.low < bestLowSinceEntry:
                    bestLowSinceEntry = prev_bar.low
                maxRunUpPts = bestHighSinceEntry - cost
                maxRunDnPts = cost - bestLowSinceEntry
            elif posFlag == -1:
                if prev_bar.low < bestLowSinceEntry:
                    bestLowSinceEntry = prev_bar.low
                if prev_bar.high > bestHighSinceEntry:
                    bestHighSinceEntry = prev_bar.high
                maxRunUpPts = cost - bestLowSinceEntry
                maxRunDnPts = bestHighSinceEntry - cost

        barsHeld = bar_no - entryBarNo if posFlag != 0 else 0
        atrStopLong = cost - float(params["ATRStopK"]) * entryATRD
        atrStopShort = cost + float(params["ATRStopK"]) * entryATRD
        atrTPPriceLong = cost + float(params["ATRTakeProfitK"]) * entryATRD
        atrTPPriceShort = cost - float(params["ATRTakeProfitK"]) * entryATRD
        force_exit = False
        exit_action = None

        if sessOnManage == 1 and bar_no > warmupBars:
            if (bar.time >= fixedForceExitTime) and (posFlag != 0):
                force_exit = True
                exit_action = "強制平倉"
            else:
                if posFlag == 1:
                    longExitByATR = bool(choices["use_atr_stop"]) and (entryATRD > 0) and (bar.open <= atrStopLong)
                    longExitByTP = bool(choices["use_atr_tp"]) and (entryATRD > 0) and (bar.open >= atrTPPriceLong)
                    longExitByTime = bool(choices["use_time_stop"]) and (barsHeld >= int(params["TimeStopBars"])) and (maxRunUpPts < minRunPtsByAnchor)
                    longExitByTrail = bool(choices["use_trail_exit"]) and (maxRunUpPts >= trailStartPtsByAnchor) and ((bestHighSinceEntry - bar.open) >= trailGivePtsByAnchor)
                    longExitByAnchor = (int(params["UseAnchorExit"]) == 1) and (dayAnchorOpen > 0) and (bar.open <= dayAnchorOpen - anchorBackPtsByAnchor)
                    if longExitByATR or longExitByTP or longExitByTime or longExitByTrail or longExitByAnchor:
                        exit_action = "平賣"
                elif posFlag == -1:
                    shortExitByATR = bool(choices["use_atr_stop"]) and (entryATRD > 0) and (bar.open >= atrStopShort)
                    shortExitByTP = bool(choices["use_atr_tp"]) and (entryATRD > 0) and (bar.open <= atrTPPriceShort)
                    shortExitByTime = bool(choices["use_time_stop"]) and (barsHeld >= int(params["TimeStopBars"])) and (maxRunUpPts < minRunPtsByAnchor)
                    shortExitByTrail = bool(choices["use_trail_exit"]) and (maxRunUpPts >= trailStartPtsByAnchor) and ((bar.open - bestLowSinceEntry) >= trailGivePtsByAnchor)
                    shortExitByAnchor = (int(params["UseAnchorExit"]) == 1) and (dayAnchorOpen > 0) and (bar.open >= dayAnchorOpen + anchorBackPtsByAnchor)
                    if shortExitByATR or shortExitByTP or shortExitByTime or shortExitByTrail or shortExitByAnchor:
                        exit_action = "平買"

        if posFlag != 0 and (force_exit or exit_action is not None) and current_trade_entry is not None:
            direction = current_trade_entry["direction"]
            entry_price = current_trade_entry["entry_price"]
            exit_price = bar.open
            points = (exit_price - entry_price) if direction == 1 else (entry_price - exit_price)
            gross_pnl = points * point_value
            fee = fee_per_side * 2.0
            tax = round(entry_price * point_value * tax_rate) + round(exit_price * point_value * tax_rate)
            slip_cost = point_value * slip_per_side * 2.0
            net_pnl = gross_pnl - fee - tax - slip_cost
            result.trades.append(
                Trade(
                    entry_date=current_trade_entry["entry_date"],
                    entry_time=current_trade_entry["entry_time"],
                    entry_price=entry_price,
                    entry_action=current_trade_entry["entry_action"],
                    exit_date=bar.date,
                    exit_time=bar.time,
                    exit_price=exit_price,
                    exit_action=exit_action or "強制平倉",
                    direction=direction,
                    points=points,
                    gross_pnl=gross_pnl,
                    fee=fee,
                    tax=tax,
                    slip_cost=slip_cost,
                    net_pnl=net_pnl,
                )
            )
            posFlag = 0
            cost = 0.0
            entryATRD = 0.0
            entryBarNo = 0
            bestHighSinceEntry = 0.0
            bestLowSinceEntry = 0.0
            maxRunUpPts = 0.0
            maxRunDnPts = 0.0
            barsHeld = 0
            current_trade_entry = None
            continue

        if sessOnEntry == 1 and bar_no > warmupBars:
            if posFlag == 0 and dayEntryCount < int(params["MaxEntriesPerDay"]):
                longBias, shortBias = _resolve_bias(current_daily_anchor, choices["bias_mode"])
                longEntryLevelNH = current_daily_anchor["nhVal"] + float(params["EntryBufferPts"])
                longEntryLevelDon = current_daily_anchor["donHiD"] + float(params["DonBufferPts"])
                shortEntryLevelNL = current_daily_anchor["nlVal"] - float(params["EntryBufferPts"])
                shortEntryLevelDon = current_daily_anchor["donLoD"] - float(params["DonBufferPts"])
                longReady, shortReady = _resolve_entry_ready(
                    bar_open=bar.open,
                    long_bias=longBias,
                    short_bias=shortBias,
                    atr_d=current_daily_anchor["atrD"],
                    min_atr_d=float(params["MinATRD"]),
                    long_entry_level_nh=longEntryLevelNH,
                    long_entry_level_don=longEntryLevelDon,
                    short_entry_level_nl=shortEntryLevelNL,
                    short_entry_level_don=shortEntryLevelDon,
                    entry_mode=choices["entry_mode"],
                    atr_filter_mode=choices["atr_filter_mode"],
                )
                if longReady:
                    posFlag = 1
                    cost = bar.open
                    entryATRD = current_daily_anchor["atrD"]
                    dayEntryCount += 1
                    entryBarNo = bar_no
                    bestHighSinceEntry = bar.open
                    bestLowSinceEntry = bar.open
                    maxRunUpPts = 0.0
                    maxRunDnPts = 0.0
                    barsHeld = 0
                    current_trade_entry = {
                        "entry_date": bar.date,
                        "entry_time": bar.time,
                        "entry_price": bar.open,
                        "entry_action": "新買",
                        "direction": 1,
                    }
                elif shortReady:
                    posFlag = -1
                    cost = bar.open
                    entryATRD = current_daily_anchor["atrD"]
                    dayEntryCount += 1
                    entryBarNo = bar_no
                    bestHighSinceEntry = bar.open
                    bestLowSinceEntry = bar.open
                    maxRunUpPts = 0.0
                    maxRunDnPts = 0.0
                    barsHeld = 0
                    current_trade_entry = {
                        "entry_date": bar.date,
                        "entry_time": bar.time,
                        "entry_price": bar.open,
                        "entry_action": "新賣",
                        "direction": -1,
                    }
    return result
