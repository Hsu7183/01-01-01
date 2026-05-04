from __future__ import annotations

from bisect import bisect_left

from src.core.models import MinuteBar, DailyBar, Trade, BacktestResult


def _avg(nums: list[float]) -> float:
    return sum(nums) / len(nums)


def _calc_ema_desc(closes_desc: list[float], length: int, warmbars: int) -> float:
    alpha = 2.0 / (length + 1.0)
    ema = closes_desc[warmbars - 1]
    for idx in range(warmbars - 2, -1, -1):
        ema = alpha * closes_desc[idx] + (1.0 - alpha) * ema
    return ema


def _calc_true_range(today_high: float, today_low: float, prev_close: float) -> float:
    return max(
        today_high - today_low,
        abs(today_high - prev_close),
        abs(today_low - prev_close),
    )


def _normalize_params(params):
    """
    相容：
    1. dict
    2. ParamSpec list
    """
    if isinstance(params, dict):
        return params

    if isinstance(params, list):
        result = {}
        for p in params:
            if hasattr(p, "name") and hasattr(p, "default"):
                result[p.name] = p.default
        if result:
            return result

    raise TypeError("params 必須是 dict 或 ParamSpec list")


def _normalize_backtest_args(arg1, arg2, arg3, arg4):
    """
    相容兩種呼叫方式：

    A. 原始正確版：
       run_0313plus_backtest(minute_bars, daily_bars, params, script_name)

    B. 後續 optimizer 方便呼叫版：
       run_0313plus_backtest(script_name, params, minute_bars, daily_bars)
    """
    # A: minute_bars, daily_bars, params, script_name
    if isinstance(arg4, str):
        minute_bars = arg1
        daily_bars = arg2
        params = arg3
        script_name = arg4
        return minute_bars, daily_bars, _normalize_params(params), script_name

    # B: script_name, params, minute_bars, daily_bars
    if isinstance(arg1, str):
        script_name = arg1
        params = arg2
        minute_bars = arg3
        daily_bars = arg4
        return minute_bars, daily_bars, _normalize_params(params), script_name

    raise TypeError(
        "run_0313plus_backtest 參數格式錯誤，"
        "應為 (minute_bars, daily_bars, params, script_name) "
        "或 (script_name, params, minute_bars, daily_bars)"
    )


def _build_daily_anchor(
    current_date: int,
    daily_bars_asc: list[DailyBar],
    params: dict,
):
    daily_dates = [d.date for d in daily_bars_asc]
    idx = bisect_left(daily_dates, current_date)

    prev_days = daily_bars_asc[:idx]
    prev_desc = list(reversed(prev_days))

    need_days = max(
        5,
        int(params["DonLen"]),
        int(params["ATRLen"]) + 1,
        int(params["EMAWarmBars"]),
    )

    if len(prev_desc) < need_days:
        return None

    yH = prev_desc[0].high
    yL = prev_desc[0].low
    yC = prev_desc[0].close

    ma2D = _avg([x.close for x in prev_desc[:3]])
    ma3D = _avg([x.close for x in prev_desc[:5]])

    warmbars = int(params["EMAWarmBars"])
    closes_desc = [x.close for x in prev_desc[:warmbars]]
    ema2D = _calc_ema_desc(closes_desc, 3, warmbars)
    ema3D = _calc_ema_desc(closes_desc, 5, warmbars)

    don_len = int(params["DonLen"])
    don_slice = prev_desc[:don_len]
    donHiD = max(x.high for x in don_slice)
    donLoD = min(x.low for x in don_slice)

    atr_len = int(params["ATRLen"])
    atr_sum = 0.0
    for i in range(atr_len):
        tr = _calc_true_range(
            prev_desc[i].high,
            prev_desc[i].low,
            prev_desc[i + 1].close
        )
        atr_sum += tr
    atrD = atr_sum / atr_len

    cdpVal = (yH + yL + 2.0 * yC) / 4.0
    nhVal = 2.0 * cdpVal - yL
    nlVal = 2.0 * cdpVal - yH

    longBias = ((ma2D > ma3D) or (ema2D > ema3D)) and (yC > cdpVal)
    shortBias = ((ma2D < ma3D) or (ema2D < ema3D)) and (yC < cdpVal)

    return {
        "yH": yH,
        "yL": yL,
        "yC": yC,
        "ma2D": ma2D,
        "ma3D": ma3D,
        "ema2D": ema2D,
        "ema3D": ema3D,
        "donHiD": donHiD,
        "donLoD": donLoD,
        "atrD": atrD,
        "cdpVal": cdpVal,
        "nhVal": nhVal,
        "nlVal": nlVal,
        "longBias": longBias,
        "shortBias": shortBias,
    }


def run_0313plus_backtest(
    arg1,
    arg2,
    arg3,
    arg4,
    point_value: float = 200.0,
    fee_per_side: float = 45.0,
    tax_rate: float = 0.00002,
    slip_per_side: float = 0.0,
) -> BacktestResult:
    minute_bars, daily_bars, params, script_name = _normalize_backtest_args(
        arg1, arg2, arg3, arg4
    )

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

        if posFlag != 0:
            barsHeld = bar_no - entryBarNo
        else:
            barsHeld = 0

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
                    longExitByATR = (entryATRD > 0) and (bar.open <= atrStopLong)
                    longExitByTP = (entryATRD > 0) and (bar.open >= atrTPPriceLong)
                    longExitByTime = (barsHeld >= int(params["TimeStopBars"])) and (maxRunUpPts < minRunPtsByAnchor)
                    longExitByTrail = (maxRunUpPts >= trailStartPtsByAnchor) and ((bestHighSinceEntry - bar.open) >= trailGivePtsByAnchor)
                    longExitByAnchor = (int(params["UseAnchorExit"]) == 1) and (dayAnchorOpen > 0) and (bar.open <= dayAnchorOpen - anchorBackPtsByAnchor)

                    if longExitByATR or longExitByTP or longExitByTime or longExitByTrail or longExitByAnchor:
                        exit_action = "平賣"

                elif posFlag == -1:
                    shortExitByATR = (entryATRD > 0) and (bar.open >= atrStopShort)
                    shortExitByTP = (entryATRD > 0) and (bar.open <= atrTPPriceShort)
                    shortExitByTime = (barsHeld >= int(params["TimeStopBars"])) and (maxRunUpPts < minRunPtsByAnchor)
                    shortExitByTrail = (maxRunUpPts >= trailStartPtsByAnchor) and ((bar.open - bestLowSinceEntry) >= trailGivePtsByAnchor)
                    shortExitByAnchor = (int(params["UseAnchorExit"]) == 1) and (dayAnchorOpen > 0) and (bar.open >= dayAnchorOpen + anchorBackPtsByAnchor)

                    if shortExitByATR or shortExitByTP or shortExitByTime or shortExitByTrail or shortExitByAnchor:
                        exit_action = "平買"

        if posFlag != 0 and (force_exit or exit_action is not None) and current_trade_entry is not None:
            direction = current_trade_entry["direction"]
            entry_price = current_trade_entry["entry_price"]
            exit_price = bar.open

            if direction == 1:
                points = exit_price - entry_price
            else:
                points = entry_price - exit_price

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
                longEntryLevelNH = current_daily_anchor["nhVal"] + float(params["EntryBufferPts"])
                longEntryLevelDon = current_daily_anchor["donHiD"] + float(params["DonBufferPts"])
                shortEntryLevelNL = current_daily_anchor["nlVal"] - float(params["EntryBufferPts"])
                shortEntryLevelDon = current_daily_anchor["donLoD"] - float(params["DonBufferPts"])

                longReady = (
                    current_daily_anchor["longBias"]
                    and current_daily_anchor["atrD"] >= float(params["MinATRD"])
                    and ((bar.open >= longEntryLevelNH) or (bar.open >= longEntryLevelDon))
                )

                shortReady = (
                    current_daily_anchor["shortBias"]
                    and current_daily_anchor["atrD"] >= float(params["MinATRD"])
                    and ((bar.open <= shortEntryLevelNL) or (bar.open <= shortEntryLevelDon))
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