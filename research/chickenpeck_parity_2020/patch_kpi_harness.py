from pathlib import Path

p = Path('research/chickenpeck_parity_2020/kpi_from_single_trades.js')
s = p.read_text(encoding='utf-8')
old = '    trades: kpi.n,'
new = '    trades: kpi.nTrades,'
if s.count(old) != 1:
    raise RuntimeError(f'Expected one nTrades patch target, found {s.count(old)}')
p.write_text(s.replace(old, new), encoding='utf-8')
print('Patched KPI summary to use calcKpi return key nTrades')
