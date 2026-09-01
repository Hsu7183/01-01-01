from pathlib import Path

p = Path('research/chickenpeck_parity_2020/run_research.py')
s = p.read_text(encoding='utf-8')

repls = [
(
'''            "entry_execution": "E=A∓FillPen for long / A±FillPen for short",
            "fill_proxy": "Low<=E long / High>=E short",''',
'''            "entry_execution": "record/fill price is anchor A; FillPen is only the conservative must-fill proxy",
            "fill_proxy": "Low<=A-FillPen long / High>=A+FillPen short",'''
),
(
'''        if s > 0:
            anchor = math.floor(opens[i] - offset + 0.5)
            exec_px = anchor - fill_pen
            fill = lows[i] <= exec_px
        else:
            anchor = math.floor(opens[i] + offset + 0.5)
            exec_px = anchor + fill_pen
            fill = highs[i] >= exec_px
        if fill:
            pos = s
            entry = exec_px''',
'''        if s > 0:
            anchor = math.floor(opens[i] - offset + 0.5)
            fill = lows[i] <= anchor - fill_pen
        else:
            anchor = math.floor(opens[i] + offset + 0.5)
            fill = highs[i] >= anchor + fill_pen
        if fill:
            pos = s
            entry = anchor'''
),
(
'''            if s > 0:
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
                actions.append(f"{ts} {int(px)} {'新買' if s > 0 else '新賣'}")''',
'''            if s > 0:
                anchor = xs_round(float(df.open.iloc[gi]) - w.offset)
                px = anchor
                fill = float(df.low.iloc[gi]) <= anchor - w.fill_pen
            else:
                anchor = xs_round(float(df.open.iloc[gi]) + w.offset)
                px = anchor
                fill = float(df.high.iloc[gi]) >= anchor + w.fill_pen
            if fill:
                pos = s; entry = float(px); entry_local = li; stop_pending_local = -1
                ts = df.dt.iloc[gi].strftime("%Y%m%d%H%M")
                actions.append(f"{ts} {int(px)} {'新買' if s > 0 else '新賣'}")'''
),
(
'''        if s > 0:
            eprice = xs_round(float(df.open.iloc[gi]) - w.offset) - w.fill_pen
            filled = float(df.low.iloc[gi]) <= eprice
        else:
            eprice = xs_round(float(df.open.iloc[gi]) + w.offset) + w.fill_pen
            filled = float(df.high.iloc[gi]) >= eprice''',
'''        if s > 0:
            eprice = xs_round(float(df.open.iloc[gi]) - w.offset)
            filled = float(df.low.iloc[gi]) <= eprice - w.fill_pen
        else:
            eprice = xs_round(float(df.open.iloc[gi]) + w.offset)
            filled = float(df.high.iloc[gi]) >= eprice + w.fill_pen'''
),
]

for old, new in repls:
    count = s.count(old)
    if count != 1:
        raise RuntimeError(f'Expected exactly one replacement, found {count}: {old[:120]!r}')
    s = s.replace(old, new)

banner = '''\n# RUNTIME_CONTRACT_PATCH_APPLIED = True\n# Mother-template rule: order/ledger price is anchor A; A±1 is the conservative must-fill proxy.\n'''
if 'RUNTIME_CONTRACT_PATCH_APPLIED' not in s:
    s = banner + s
p.write_text(s, encoding='utf-8')
print('Applied authoritative PMTXD mother-template A / A±1 contract')
