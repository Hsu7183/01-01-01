from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from itertools import product
import hashlib
import json
import math
import os

import numpy as np
import pandas as pd
from numba import njit

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "output"
OUT.mkdir(parents=True, exist_ok=True)
DATA_PATH = Path("bundle/data/M1.txt")
START_DATE = 20200101
END_DATE = 20201231
POINT_VALUE = 50.0
FEE_PER_SIDE = 18.0
TAX_RATE = 0.00002
ENTRY_SLIP = 0.0
EXIT_SLIP = 2.0
CAPITAL = 250000.0
FILL_PEN = 1


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def xs_round(x: float) -> int:
    return int(math.floor(x + 0.5))


def load_data() -> pd.DataFrame:
    raw = DATA_PATH.read_bytes()
    df = pd.read_csv(
        DATA_PATH,
        sep=r"\s+",
        names=["date", "time", "open", "high", "low", "close"],
        dtype=np.int64,
        engine="c",
    )
    df = df[(df.date >= START_DATE) & (df.date <= END_DATE)].copy()
    df = df[(df.time >= 84500) & (df.time <= 134500)].copy()
    df.sort_values(["date", "time"], inplace=True, kind="stable")
    df.reset_index(drop=True, inplace=True)
    df["dt"] = pd.to_datetime(
        df.date.astype(str) + df.time.astype(str).str.zfill(6),
        format="%Y%m%d%H%M%S",
    )
    df["month"] = (df.date // 100) % 100
    df["h1"] = df.date <= 20200630
    manifest = {
        "data_path": str(DATA_PATH),
        "data_file_size": len(raw),
        "data_sha256": sha256_bytes(raw),
        "rows_2020_day": int(len(df)),
        "dates": int(df.date.nunique()),
        "first_dt": str(df.dt.iloc[0]),
        "last_dt": str(df.dt.iloc[-1]),
        "missing_force_1312_dates": [
            int(d) for d, g in df.groupby("date") if not bool((g.time == 131200).any())
        ],
        "cost": {
            "point_value": POINT_VALUE,
            "fee_per_side": FEE_PER_SIDE,
            "tax_rate": TAX_RATE,
            "entry_slip_points": ENTRY_SLIP,
            "exit_slip_points": EXIT_SLIP,
            "capital": CAPITAL,
        },
        "execution": {
            "candidate_uses": "current Open and completed bars through t-1 only",
            "entry_anchor": "A=Open±Pullback",
            "entry_execution": "E=A∓FillPen for long / A±FillPen for short",
            "fill_proxy": "Low<=E long / High>=E short",
            "pending": "current bar only; next bar is cancellation block",
            "exit_priority": "force, pending SL next Open, MaxHold Open, TP, SL trigger",
            "same_bar_exit": False,
            "force_flat": 131200,
        },
    }
    (OUT / "data_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    if manifest["missing_force_1312_dates"]:
        print("WARNING missing 13:12", manifest["missing_force_1312_dates"][:20])
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return df


def ema_daily(values: np.ndarray, dates: np.ndarray, n: int) -> np.ndarray:
    out = np.empty(len(values), dtype=np.float64)
    a = 2.0 / (n + 1.0)
    prev = 0.0
    prev_date = -1
    for i, v in enumerate(values):
        if dates[i] != prev_date:
            prev = float(v)
            prev_date = int(dates[i])
        else:
            prev = a * float(v) + (1.0 - a) * prev
        out[i] = prev
    return out


def rolling_mean_daily(values: np.ndarray, dates: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(values), np.nan, dtype=np.float64)
    start = 0
    while start < len(values):
        end = start + 1
        while end < len(values) and dates[end] == dates[start]:
            end += 1
        arr = values[start:end].astype(float)
        cs = np.cumsum(np.r_[0.0, arr])
        for j in range(len(arr)):
            lo = max(0, j - n + 1)
            out[start + j] = (cs[j + 1] - cs[lo]) / (j - lo + 1)
        start = end
    return out


def atr_daily(df: pd.DataFrame, n: int) -> np.ndarray:
    dates = df.date.to_numpy(np.int64)
    h = df.high.to_numpy(float)
    l = df.low.to_numpy(float)
    c = df.close.to_numpy(float)
    tr = np.empty(len(df), dtype=float)
    for i in range(len(df)):
        if i == 0 or dates[i] != dates[i - 1]:
            tr[i] = h[i] - l[i]
        else:
            tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    return rolling_mean_daily(tr, dates, n)


def daybase_0904(df: pd.DataFrame) -> np.ndarray:
    out = np.full(len(df), np.nan, dtype=float)
    for _, idx in df.groupby("date", sort=True).groups.items():
        ids = np.asarray(list(idx), dtype=int)
        gtime = df.time.to_numpy()[ids]
        pos = np.where(gtime == 90400)[0]
        if len(pos):
            base = float(df.open.iloc[ids[pos[0]]])
            out[ids[pos[0]]:] = base
    return out


def minutes_since_0845(t: int) -> int:
    hh = t // 10000
    mm = (t // 100) % 100
    return hh * 60 + mm - (8 * 60 + 45)


def candidate_mtf(df: pd.DataFrame, htf: int, ma_type: str, fast: int, mid: int, slow: int) -> np.ndarray:
    n = len(df)
    sig = np.zeros(n, dtype=np.int8)
    dates = df.date.to_numpy(np.int64)
    times = df.time.to_numpy(np.int64)
    o = df.open.to_numpy(float)
    c = df.close.to_numpy(float)
    base = daybase_0904(df)
    start = 0
    while start < n:
        end = start + 1
        while end < n and dates[end] == dates[start]:
            end += 1
        vals: list[float] = []
        ef = em = es = None
        prev_slow = None
        prev_long = False
        prev_short = False
        af = 2.0 / (fast + 1.0)
        am = 2.0 / (mid + 1.0)
        ass = 2.0 / (slow + 1.0)
        for i in range(start, end):
            mins = minutes_since_0845(int(times[i]))
            if i <= start or mins <= 0 or mins % htf != 0 or times[i] > 131000:
                continue
            v = float(c[i - 1])
            vals.append(v)
            if ma_type == "EMA":
                if ef is None:
                    ef = em = es = v
                else:
                    ef = af * v + (1 - af) * ef
                    em = am * v + (1 - am) * em
                    es = ass * v + (1 - ass) * es
                if len(vals) < slow + 1:
                    prev_slow = es
                    continue
                fval, mval, sval = float(ef), float(em), float(es)
            else:
                if len(vals) < slow + 1:
                    continue
                fval = float(np.mean(vals[-fast:]))
                mval = float(np.mean(vals[-mid:]))
                sval = float(np.mean(vals[-slow:]))
            if prev_slow is None:
                prev_slow = sval
                continue
            long_now = fval > mval > sval and sval > prev_slow
            short_now = fval < mval < sval and sval < prev_slow
            if not np.isnan(base[i]):
                if long_now and not prev_long and o[i] > base[i]:
                    sig[i] = 1
                elif short_now and not prev_short and o[i] < base[i]:
                    sig[i] = -1
            prev_long = bool(long_now)
            prev_short = bool(short_now)
            prev_slow = sval
        start = end
    return sig


def alignment_age(aligned: np.ndarray, dates: np.ndarray) -> np.ndarray:
    age = np.zeros(len(aligned), dtype=np.int16)
    for i in range(len(aligned)):
        if not aligned[i]:
            continue
        if i > 0 and dates[i] == dates[i - 1] and aligned[i - 1]:
            age[i] = age[i - 1] + 1
        else:
            age[i] = 1
    return age


def candidate_pullback_tempo(
    df: pd.DataFrame,
    fast: int,
    mid: int,
    slow: int,
    launch: int,
    pb: int,
    impulse_atr: float,
    retrace_max: float,
    tempo_max: float,
    age_max: int,
) -> np.ndarray:
    dates = df.date.to_numpy(np.int64)
    o = df.open.to_numpy(float)
    h = df.high.to_numpy(float)
    l = df.low.to_numpy(float)
    c = df.close.to_numpy(float)
    base = daybase_0904(df)
    atr = atr_daily(df, 8)
    ef = ema_daily(c, dates, fast)
    em = ema_daily(c, dates, mid)
    es = ema_daily(c, dates, slow)
    long_align = (ef > em) & (em > es)
    short_align = (ef < em) & (em < es)
    long_age = alignment_age(long_align, dates)
    short_age = alignment_age(short_align, dates)
    cond_l = np.zeros(len(df), dtype=bool)
    cond_s = np.zeros(len(df), dtype=bool)
    for i in range(1, len(df)):
        j = i - 1
        k0 = j - pb - launch
        k1 = j - pb
        if k0 < 0 or dates[k0] != dates[j] or dates[k1] != dates[j] or np.isnan(base[i]):
            continue
        a = max(1e-9, atr[j])
        impulse = c[k1] - c[k0]
        retrace = c[j] - c[k1]
        if (
            long_align[j]
            and 2 <= long_age[j] <= age_max
            and impulse >= impulse_atr * a
            and retrace <= 0
            and -retrace <= impulse * retrace_max
            and (-retrace / max(1, pb)) <= (impulse / max(1, launch)) * tempo_max
            and c[j] > em[j]
            and l[j] <= ef[j]
            and o[i] > base[i]
        ):
            cond_l[i] = True
        impulse_s = c[k0] - c[k1]
        retrace_s = c[k1] - c[j]
        if (
            short_align[j]
            and 2 <= short_age[j] <= age_max
            and impulse_s >= impulse_atr * a
            and retrace_s <= 0
            and -retrace_s <= impulse_s * retrace_max
            and (-retrace_s / max(1, pb)) <= (impulse_s / max(1, launch)) * tempo_max
            and c[j] < em[j]
            and h[j] >= ef[j]
            and o[i] < base[i]
        ):
            cond_s[i] = True
    sig = np.zeros(len(df), dtype=np.int8)
    for i in range(len(df)):
        prev_l = cond_l[i - 1] if i > 0 and dates[i] == dates[i - 1] else False
        prev_s = cond_s[i - 1] if i > 0 and dates[i] == dates[i - 1] else False
        if cond_l[i] and not prev_l:
            sig[i] = 1
        elif cond_s[i] and not prev_s:
            sig[i] = -1
    return sig


def candidate_contraction_release(
    df: pd.DataFrame,
    short_n: int,
    long_n: int,
    contract_ratio: float,
    expand_mult: float,
    lookback: int,
    mom_atr: float,
) -> np.ndarray:
    dates = df.date.to_numpy(np.int64)
    o = df.open.to_numpy(float)
    c = df.close.to_numpy(float)
    base = daybase_0904(df)
    ats = atr_daily(df, short_n)
    atl = atr_daily(df, long_n)
    ema = ema_daily(c, dates, long_n)
    cond_l = np.zeros(len(df), dtype=bool)
    cond_s = np.zeros(len(df), dtype=bool)
    for i in range(2, len(df)):
        j = i - 1
        k = j - lookback
        if k < 0 or dates[k] != dates[j] or dates[j - 1] != dates[j] or np.isnan(base[i]):
            continue
        if atl[j] <= 0 or atl[j - 1] <= 0:
            continue
        ratio_prev = ats[j - 1] / atl[j - 1]
        expansion = ats[j] / max(1e-9, ats[j - 1])
        move = c[j] - c[k]
        if ratio_prev <= contract_ratio and expansion >= expand_mult:
            if move >= mom_atr * atl[j] and c[j] > ema[j] and o[i] > base[i]:
                cond_l[i] = True
            elif -move >= mom_atr * atl[j] and c[j] < ema[j] and o[i] < base[i]:
                cond_s[i] = True
    sig = np.zeros(len(df), dtype=np.int8)
    for i in range(len(df)):
        prev_l = cond_l[i - 1] if i > 0 and dates[i] == dates[i - 1] else False
        prev_s = cond_s[i - 1] if i > 0 and dates[i] == dates[i - 1] else False
        if cond_l[i] and not prev_l:
            sig[i] = 1
        elif cond_s[i] and not prev_s:
            sig[i] = -1
    return sig


def candidate_pause_reaccel(
    df: pd.DataFrame,
    fast: int,
    mid: int,
    slow: int,
    lookback: int,
    mom_atr: float,
    pause_atr: float,
    accel_atr: float,
) -> np.ndarray:
    dates = df.date.to_numpy(np.int64)
    o = df.open.to_numpy(float)
    c = df.close.to_numpy(float)
    base = daybase_0904(df)
    atr = atr_daily(df, 8)
    ef = ema_daily(c, dates, fast)
    em = ema_daily(c, dates, mid)
    es = ema_daily(c, dates, slow)
    cond_l = np.zeros(len(df), dtype=bool)
    cond_s = np.zeros(len(df), dtype=bool)
    for i in range(3, len(df)):
        j = i - 1
        k = j - lookback
        if k < 0 or dates[k] != dates[j] or dates[j - 2] != dates[j] or np.isnan(base[i]):
            continue
        a = max(1e-9, atr[j])
        v_prev = c[j - 1] - c[j - 2]
        v_now = c[j] - c[j - 1]
        mom = c[j - 1] - c[k]
        if (
            ef[j] > em[j] > es[j]
            and mom >= mom_atr * a
            and abs(v_prev) <= pause_atr * a
            and v_now >= accel_atr * a
            and o[i] > base[i]
        ):
            cond_l[i] = True
        elif (
            ef[j] < em[j] < es[j]
            and -mom >= mom_atr * a
            and abs(v_prev) <= pause_atr * a
            and -v_now >= accel_atr * a
            and o[i] < base[i]
        ):
            cond_s[i] = True
    sig = np.zeros(len(df), dtype=np.int8)
    for i in range(len(df)):
        prev_l = cond_l[i - 1] if i > 0 and dates[i] == dates[i - 1] else False
        prev_s = cond_s[i - 1] if i > 0 and dates[i] == dates[i - 1] else False
        if cond_l[i] and not prev_l:
            sig[i] = 1
        elif cond_s[i] and not prev_s:
            sig[i] = -1
    return sig


@njit(cache=True)
def screen_engine(
    dates, times, opens, highs, lows, months, signals,
    offset, fill_pen, tp, sl, mh, side_mode,
):
    pos = 0
    entry = 0.0
    entry_i = -1
    stop_pending_i = -1
    cancel_block_i = -1
    last_date = -1
    trades = 0
    total = 0.0
    gp = 0.0
    gl = 0.0
    h1 = 0.0
    h2 = 0.0
    peak = 0.0
    mdd = 0.0
    orphan = 0
    for i in range(len(dates)):
        if dates[i] != last_date:
            if pos != 0:
                orphan += 1
            pos = 0
            entry = 0.0
            entry_i = -1
            stop_pending_i = -1
            cancel_block_i = -1
            last_date = dates[i]
        acted = False
        exit_px = 0.0
        exit_now = False
        if pos != 0 and times[i] >= 131200:
            exit_px = opens[i]
            exit_now = True
        elif pos != 0 and stop_pending_i >= 0 and i > stop_pending_i:
            exit_px = opens[i]
            exit_now = True
        elif pos != 0 and stop_pending_i < 0 and entry_i >= 0 and i >= entry_i + mh:
            exit_px = opens[i]
            exit_now = True
        elif pos != 0 and stop_pending_i < 0 and i > entry_i:
            tp_hit = (pos > 0 and highs[i] >= entry + tp) or (pos < 0 and lows[i] <= entry - tp)
            sl_hit = (pos > 0 and lows[i] <= entry - sl) or (pos < 0 and highs[i] >= entry + sl)
            if tp_hit:
                exit_px = entry + tp if pos > 0 else entry - tp
                exit_now = True
            elif sl_hit:
                stop_pending_i = i
                acted = True
        if exit_now:
            points = (exit_px - entry) if pos > 0 else (entry - exit_px)
            tax_in = math.floor(entry * 50.0 * 0.00002 + 0.5)
            tax_out = math.floor(exit_px * 50.0 * 0.00002 + 0.5)
            net = points * 50.0 - 36.0 - tax_in - tax_out - 100.0
            trades += 1
            total += net
            if net > 0:
                gp += net
            elif net < 0:
                gl += net
            if dates[i] <= 20200630:
                h1 += net
            else:
                h2 += net
            if total > peak:
                peak = total
            dd = peak - total
            if dd > mdd:
                mdd = dd
            pos = 0
            entry = 0.0
            entry_i = -1
            stop_pending_i = -1
            acted = True
        if acted:
            continue
        if i == cancel_block_i:
            continue
        if pos != 0 or times[i] < 90500 or times[i] > 131000:
            continue
        s = int(signals[i])
        if side_mode == 1 and s < 0:
            s = 0
        elif side_mode == -1 and s > 0:
            s = 0
        if s == 0:
            continue
        if s > 0:
            anchor = math.floor(opens[i] - offset + 0.5)
            exec_px = anchor - fill_pen
            fill = lows[i] <= exec_px
        else:
            anchor = math.floor(opens[i] + offset + 0.5)
            exec_px = anchor + fill_pen
            fill = highs[i] >= exec_px
        if fill:
            pos = s
            entry = exec_px
            entry_i = i
            stop_pending_i = -1
        else:
            cancel_block_i = i + 1
    pf = gp / (-gl) if gl < 0 else 999.0 if gp > 0 else 0.0
    return trades, total, pf, mdd, h1, h2, orphan


@dataclass(frozen=True)
class Winner:
    strategy_id: str
    family: str
    core: dict
    offset: int
    fill_pen: int
    tp: int
    sl: int
    mh: int
    side_mode: int
    trades: int
    actual_net_twd: float
    pf: float
    mdd_twd: float
    h1_twd: float
    h2_twd: float


def profile_grid(full: bool = False):
    if full:
        offsets = [3, 5, 8, 12, 15, 20, 25]
        tps = [15, 20, 25, 30, 40, 50, 60]
        sls = [8, 10, 12, 15, 18, 20, 25, 30]
        mhs = [3, 5, 8, 10]
        sides = [0, 1, -1]
        return list(product(offsets, tps, sls, mhs, sides))
    curated = [
        (3, 15, 10, 3), (5, 20, 12, 5), (8, 20, 15, 5),
        (8, 30, 18, 5), (12, 30, 18, 8), (12, 40, 20, 8),
        (15, 30, 20, 5), (15, 40, 25, 8), (20, 40, 25, 8),
        (20, 50, 30, 10), (25, 40, 18, 5), (25, 60, 30, 10),
    ]
    return [(a, b, c, d, s) for a, b, c, d in curated for s in (0, 1, -1)]


def result_dict(core_idx, profile, metrics):
    offset, tp, sl, mh, side = profile
    tr, net, pf, mdd, h1, h2, orphan = metrics
    return {
        "core_idx": int(core_idx), "offset": int(offset), "tp": int(tp), "sl": int(sl),
        "mh": int(mh), "side_mode": int(side), "trades": int(tr),
        "actual_net_twd": float(net), "pf": float(pf), "mdd_twd": float(mdd),
        "h1_twd": float(h1), "h2_twd": float(h2), "orphan": int(orphan),
    }


def search_family(df, family_name: str, cores: list[dict], candidate_builder):
    dates = df.date.to_numpy(np.int64)
    times = df.time.to_numpy(np.int64)
    opens = df.open.to_numpy(np.float64)
    highs = df.high.to_numpy(np.float64)
    lows = df.low.to_numpy(np.float64)
    months = df.month.to_numpy(np.int64)
    sigs = []
    core_rows = []
    for idx, core in enumerate(cores):
        sig = candidate_builder(df, **core).astype(np.int8)
        sigs.append(sig)
        core_rows.append({"core_idx": idx, **core, "candidate_count": int(np.count_nonzero(sig))})
    pd.DataFrame(core_rows).to_csv(OUT / f"{family_name}_cores.csv", index=False)
    # compile
    if sigs:
        screen_engine(dates, times, opens, highs, lows, months, sigs[0], 8, 1, 30, 18, 5, 0)
    stage1 = []
    for ci, sig in enumerate(sigs):
        for prof in profile_grid(False):
            m = screen_engine(dates, times, opens, highs, lows, months, sig, prof[0], FILL_PEN, prof[1], prof[2], prof[3], prof[4])
            stage1.append(result_dict(ci, prof, m))
    s1 = pd.DataFrame(stage1)
    s1.sort_values(["actual_net_twd", "pf"], ascending=False, inplace=True)
    s1.to_csv(OUT / f"{family_name}_stage1.csv", index=False)
    eligible_core = s1[(s1.trades >= 8) & (s1.actual_net_twd > 0) & (s1.h1_twd > 0) & (s1.h2_twd > 0)].core_idx.drop_duplicates().head(6).tolist()
    if len(eligible_core) < 4:
        eligible_core = s1.core_idx.drop_duplicates().head(6).tolist()
    stage2 = []
    full_profiles = profile_grid(True)
    for ci in eligible_core:
        sig = sigs[int(ci)]
        for prof in full_profiles:
            m = screen_engine(dates, times, opens, highs, lows, months, sig, prof[0], FILL_PEN, prof[1], prof[2], prof[3], prof[4])
            stage2.append(result_dict(ci, prof, m))
    s2 = pd.DataFrame(stage2)
    s2.sort_values(["actual_net_twd", "pf"], ascending=False, inplace=True)
    s2.to_csv(OUT / f"{family_name}_stage2.csv", index=False)
    passes = s2[
        (s2.trades >= 15)
        & (s2.actual_net_twd > 0)
        & (s2.pf >= 1.10)
        & (s2.h1_twd > 0)
        & (s2.h2_twd > 0)
        & (s2.orphan == 0)
    ].copy()
    if passes.empty:
        passes = s2[
            (s2.trades >= 8)
            & (s2.actual_net_twd > 0)
            & (s2.pf > 1.0)
            & (s2.h1_twd > 0)
            & (s2.h2_twd > 0)
            & (s2.orphan == 0)
        ].copy()
    if passes.empty:
        print("NO PASS", family_name)
        return None, sigs, s2
    # Robust ranking: prefer both-side, then net after drawdown penalty, then PF.
    passes["rank_score"] = passes.actual_net_twd - 0.20 * passes.mdd_twd + np.where(passes.side_mode == 0, 250.0, 0.0)
    passes.sort_values(["rank_score", "pf", "trades"], ascending=False, inplace=True)
    row = passes.iloc[0]
    core = cores[int(row.core_idx)]
    winner = Winner(
        strategy_id="",
        family=family_name,
        core=core,
        offset=int(row.offset), fill_pen=FILL_PEN, tp=int(row.tp), sl=int(row.sl), mh=int(row.mh), side_mode=int(row.side_mode),
        trades=int(row.trades), actual_net_twd=float(row.actual_net_twd), pf=float(row.pf), mdd_twd=float(row.mdd_twd),
        h1_twd=float(row.h1_twd), h2_twd=float(row.h2_twd),
    )
    print("WINNER", family_name, json.dumps(asdict(winner), ensure_ascii=False))
    return winner, sigs, s2


def ledger_reference(df: pd.DataFrame, sig: np.ndarray, w: Winner):
    rows = []
    actions = []
    for day, gidx in df.groupby("date", sort=True).groups.items():
        ids = np.asarray(list(gidx), dtype=int)
        pos = 0
        entry = 0.0
        entry_local = -1
        stop_pending_local = -1
        cancel_block_local = -1
        for li, gi in enumerate(ids):
            tm = int(df.time.iloc[gi])
            acted = False
            reason = None
            out = None
            if pos and tm >= 131200:
                out = float(df.open.iloc[gi]); reason = "FORCE"
            elif pos and stop_pending_local >= 0 and li > stop_pending_local:
                out = float(df.open.iloc[gi]); reason = "SL_NEXT_OPEN"
            elif pos and stop_pending_local < 0 and li >= entry_local + w.mh:
                out = float(df.open.iloc[gi]); reason = "MAXHOLD_OPEN"
            elif pos and stop_pending_local < 0 and li > entry_local:
                tp_hit = (pos > 0 and df.high.iloc[gi] >= entry + w.tp) or (pos < 0 and df.low.iloc[gi] <= entry - w.tp)
                sl_hit = (pos > 0 and df.low.iloc[gi] <= entry - w.sl) or (pos < 0 and df.high.iloc[gi] >= entry + w.sl)
                if tp_hit:
                    out = entry + w.tp if pos > 0 else entry - w.tp; reason = "TP"
                elif sl_hit:
                    stop_pending_local = li
                    acted = True
            if out is not None:
                egi = ids[entry_local]
                points = (out - entry) if pos > 0 else (entry - out)
                tax = xs_round(entry * POINT_VALUE * TAX_RATE) + xs_round(out * POINT_VALUE * TAX_RATE)
                theo = points * POINT_VALUE - 2 * FEE_PER_SIDE - tax
                actual = theo - (ENTRY_SLIP + EXIT_SLIP) * POINT_VALUE
                entry_dt = df.dt.iloc[egi]
                exit_dt = df.dt.iloc[gi]
                rows.append({
                    "entry_dt": entry_dt.isoformat(sep=" "), "exit_dt": exit_dt.isoformat(sep=" "),
                    "side": int(pos), "entry": int(entry), "exit": int(out), "gross_points": float(points),
                    "fee_twd": int(2 * FEE_PER_SIDE), "tax_twd": int(tax), "theo_net_twd": float(theo),
                    "slip_twd": float((ENTRY_SLIP + EXIT_SLIP) * POINT_VALUE), "actual_net_twd": float(actual),
                    "reason": reason, "hold_bars": int(li - entry_local),
                })
                ts = exit_dt.strftime("%Y%m%d%H%M")
                actions.append(f"{ts} {int(out)} {'強制平倉' if reason == 'FORCE' else ('平賣' if pos > 0 else '平買')}")
                pos = 0; entry = 0.0; entry_local = -1; stop_pending_local = -1; acted = True
            if acted:
                continue
            if li == cancel_block_local:
                continue
            if pos or tm < 90500 or tm > 131000:
                continue
            s = int(sig[gi])
            if w.side_mode == 1 and s < 0: s = 0
            if w.side_mode == -1 and s > 0: s = 0
            if not s:
                continue
            if s > 0:
                anchor = xs_round(float(df.open.iloc[gi]) - w.offset)
                px = anchor - w.fill_pen
                fill = float(df.low.iloc[gi]) <= px
            else:
                anchor = xs_round(float(df.open.iloc[gi]) + w.offset)
                px = anchor + w.fill_pen
                fill = float(df.high.iloc[gi]) >= px
            if fill:
                pos = s; entry = float(px); entry_local = li; stop_pending_local = -1
                ts = df.dt.iloc[gi].strftime("%Y%m%d%H%M")
                actions.append(f"{ts} {int(px)} {'新買' if s > 0 else '新賣'}")
            else:
                cancel_block_local = li + 1
    return pd.DataFrame(rows), actions


def ledger_trade_theory(df: pd.DataFrame, sig: np.ndarray, w: Winner):
    # Independent replay of the transaction script's THEORY state.  It deliberately
    # ignores Filled/actual broker prices; those belong in a separate ACTUAL ledger.
    trades = []
    log = []
    cur_day = None
    state = "FLAT"
    side = 0
    px_in = 0.0
    entry_bar = -1
    stop_bar = -1
    cancel_bar = -1
    day_local = -1
    for gi in range(len(df)):
        d = int(df.date.iloc[gi])
        if d != cur_day:
            cur_day = d
            state = "FLAT"; side = 0; px_in = 0.0; entry_bar = -1; stop_bar = -1; cancel_bar = -1; day_local = 0
        else:
            day_local += 1
        tm = int(df.time.iloc[gi])
        command = False
        out = None
        why = None
        if state == "HOLD" and tm >= 131200:
            out = float(df.open.iloc[gi]); why = "FORCE"
        elif state == "SL_WAIT" and day_local > stop_bar:
            out = float(df.open.iloc[gi]); why = "SL_NEXT_OPEN"
        elif state == "HOLD" and day_local >= entry_bar + w.mh:
            out = float(df.open.iloc[gi]); why = "MAXHOLD_OPEN"
        elif state == "HOLD" and day_local > entry_bar:
            hit_tp = (side > 0 and df.high.iloc[gi] >= px_in + w.tp) or (side < 0 and df.low.iloc[gi] <= px_in - w.tp)
            hit_sl = (side > 0 and df.low.iloc[gi] <= px_in - w.sl) or (side < 0 and df.high.iloc[gi] >= px_in + w.sl)
            if hit_tp:
                out = px_in + w.tp if side > 0 else px_in - w.tp; why = "TP"
            elif hit_sl:
                state = "SL_WAIT"; stop_bar = day_local; command = True
        if out is not None:
            e = trades[-1]
            points = out - px_in if side > 0 else px_in - out
            tax = xs_round(px_in * POINT_VALUE * TAX_RATE) + xs_round(out * POINT_VALUE * TAX_RATE)
            theo = points * POINT_VALUE - 2 * FEE_PER_SIDE - tax
            actual = theo - (ENTRY_SLIP + EXIT_SLIP) * POINT_VALUE
            e.update({
                "exit_dt": df.dt.iloc[gi].isoformat(sep=" "), "exit": int(out), "gross_points": float(points),
                "fee_twd": int(2 * FEE_PER_SIDE), "tax_twd": int(tax), "theo_net_twd": float(theo),
                "slip_twd": float((ENTRY_SLIP + EXIT_SLIP) * POINT_VALUE), "actual_net_twd": float(actual),
                "reason": why, "hold_bars": int(day_local - entry_bar),
            })
            ts = df.dt.iloc[gi].strftime("%Y%m%d%H%M")
            log.append(f"{ts} {int(out)} {'強制平倉' if why == 'FORCE' else ('平賣' if side > 0 else '平買')}")
            state = "FLAT"; side = 0; px_in = 0.0; entry_bar = -1; stop_bar = -1; command = True
        if command:
            continue
        if day_local == cancel_bar:
            continue
        if state != "FLAT" or tm < 90500 or tm > 131000:
            continue
        s = int(sig[gi])
        if w.side_mode == 1 and s < 0: s = 0
        if w.side_mode == -1 and s > 0: s = 0
        if s == 0:
            continue
        if s > 0:
            eprice = xs_round(float(df.open.iloc[gi]) - w.offset) - w.fill_pen
            filled = float(df.low.iloc[gi]) <= eprice
        else:
            eprice = xs_round(float(df.open.iloc[gi]) + w.offset) + w.fill_pen
            filled = float(df.high.iloc[gi]) >= eprice
        if filled:
            side = s; px_in = float(eprice); entry_bar = day_local; state = "HOLD"
            trades.append({"entry_dt": df.dt.iloc[gi].isoformat(sep=" "), "side": int(s), "entry": int(eprice)})
            ts = df.dt.iloc[gi].strftime("%Y%m%d%H%M")
            log.append(f"{ts} {int(eprice)} {'新買' if s > 0 else '新賣'}")
        else:
            cancel_bar = day_local + 1
    complete = pd.DataFrame([x for x in trades if "exit_dt" in x])
    return complete, log


def write_strategy_outputs(df, winner: Winner, sig: np.ndarray):
    ref, a1 = ledger_reference(df, sig, winner)
    trd, a2 = ledger_trade_theory(df, sig, winner)
    cols = ["entry_dt","exit_dt","side","entry","exit","gross_points","fee_twd","tax_twd","theo_net_twd","slip_twd","actual_net_twd","reason","hold_bars"]
    ref = ref[cols].reset_index(drop=True)
    trd = trd[cols].reset_index(drop=True)
    ref_csv = ref.to_csv(index=False, lineterminator="\n")
    trd_csv = trd.to_csv(index=False, lineterminator="\n")
    header = (
        f"PARAM,StrategyID={winner.strategy_id},StartDate={START_DATE},EndDate={END_DATE},"
        f"EntrySlip={ENTRY_SLIP},ExitSlip={EXIT_SLIP},PointValue={int(POINT_VALUE)},FeePerSide={int(FEE_PER_SIDE)}"
    )
    ind_txt = header + "\n" + "\n".join(a1) + ("\n" if a1 else "")
    trd_txt = header + "\n" + "\n".join(a2) + ("\n" if a2 else "")
    stem = winner.strategy_id
    (OUT / f"{stem}_IND_THEORY.txt").write_text(ind_txt, encoding="utf-8")
    (OUT / f"{stem}_TRD_THEORY.txt").write_text(trd_txt, encoding="utf-8")
    (OUT / f"{stem}_trades.csv").write_text(ref_csv, encoding="utf-8")
    (OUT / f"{stem}_TRD_trades.csv").write_text(trd_csv, encoding="utf-8")
    parity = {
        "strategy_id": stem,
        "action_rows_indicator": len(a1),
        "action_rows_trade_theory": len(a2),
        "trade_rows_indicator": len(ref),
        "trade_rows_trade_theory": len(trd),
        "action_sha256_indicator": sha256_bytes(ind_txt.encode()),
        "action_sha256_trade_theory": sha256_bytes(trd_txt.encode()),
        "trade_csv_sha256_indicator": sha256_bytes(ref_csv.encode()),
        "trade_csv_sha256_trade_theory": sha256_bytes(trd_csv.encode()),
        "actions_equal": a1 == a2,
        "trades_equal": ref_csv == trd_csv,
    }
    (OUT / f"{stem}_parity.json").write_text(json.dumps(parity, indent=2), encoding="utf-8")
    if not parity["actions_equal"] or not parity["trades_equal"]:
        raise AssertionError(f"parity failed {stem}: {parity}")
    return parity


def main():
    df = load_data()
    families = []

    mtf_cores = []
    for htf in (3, 5):
        for ma_type, tuples in {
            "SMA": [(1,2,3),(2,3,5),(3,5,8)],
            "EMA": [(2,4,8),(3,5,8),(4,8,11),(5,8,13)],
        }.items():
            for fast, mid, slow in tuples:
                mtf_cores.append({"htf":htf,"ma_type":ma_type,"fast":fast,"mid":mid,"slow":slow})
    families.append(("MTF_PERMISSION", mtf_cores, candidate_mtf))

    pb_cores = []
    for fast, mid, slow in [(3,8,13),(5,13,21),(8,21,34)]:
        for launch, pb, impulse_atr, retrace_max, tempo_max, age_max in product(
            (2,3,5), (1,2), (0.5,0.8,1.2), (0.35,0.55), (0.8,1.2), (8,13)
        ):
            pb_cores.append({"fast":fast,"mid":mid,"slow":slow,"launch":launch,"pb":pb,
                             "impulse_atr":impulse_atr,"retrace_max":retrace_max,"tempo_max":tempo_max,"age_max":age_max})
    families.append(("PULLBACK_TEMPO", pb_cores, candidate_pullback_tempo))

    vr_cores = []
    for short_n, long_n, contract, expand, lookback, mom in product(
        (2,3,5), (13,20,34), (0.45,0.60,0.75), (1.10,1.25,1.50), (3,5,8), (0.4,0.8,1.2)
    ):
        if short_n < long_n:
            vr_cores.append({"short_n":short_n,"long_n":long_n,"contract_ratio":contract,
                             "expand_mult":expand,"lookback":lookback,"mom_atr":mom})
    families.append(("VOL_RELEASE", vr_cores, candidate_contraction_release))

    pr_cores = []
    for fast, mid, slow in [(3,8,13),(5,13,21),(8,21,34)]:
        for lookback, mom, pause, accel in product((3,5,8),(0.5,0.8,1.2),(0.15,0.25,0.40),(0.20,0.35,0.50)):
            pr_cores.append({"fast":fast,"mid":mid,"slow":slow,"lookback":lookback,
                             "mom_atr":mom,"pause_atr":pause,"accel_atr":accel})
    families.append(("PAUSE_REACCEL", pr_cores, candidate_pause_reaccel))

    winners = []
    winner_sigs = []
    for fam, cores, builder in families:
        w, sigs, _ = search_family(df, fam, cores, builder)
        if w is not None:
            w = Winner(strategy_id=f"CP2020_{len(winners)+1:02d}_{fam}", **{k:v for k,v in asdict(w).items() if k != "strategy_id"})
            winners.append(w)
            # locate matching core
            ci = cores.index(w.core)
            winner_sigs.append(sigs[ci])
            if len(winners) >= 3:
                break
    if len(winners) < 3:
        raise RuntimeError(f"Only {len(winners)} profitable families found")

    parity_all = []
    for w, sig in zip(winners, winner_sigs):
        parity_all.append(write_strategy_outputs(df, w, sig))
    (OUT / "winner_registry.json").write_text(json.dumps([asdict(w) for w in winners], ensure_ascii=False, indent=2), encoding="utf-8")
    pd.DataFrame([asdict(w) | {"core": json.dumps(w.core, ensure_ascii=False, sort_keys=True)} for w in winners]).to_csv(OUT / "winner_registry.csv", index=False)
    (OUT / "parity_summary.json").write_text(json.dumps(parity_all, indent=2), encoding="utf-8")
    print("STOP CONDITION MET: 3 profitable families")
    print(json.dumps([asdict(w) for w in winners], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
