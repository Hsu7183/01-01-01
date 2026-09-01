'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const crypto = require('crypto');

const ROOT = path.resolve('research/chickenpeck_parity_2020');
const OUT = path.join(ROOT, 'output');
const JS_PATH = path.join(ROOT, 'single-trades.patched.js');

function sha256(text) {
  return crypto.createHash('sha256').update(text, 'utf8').digest('hex');
}

function loadCore() {
  let source = fs.readFileSync(JS_PATH, 'utf8');
  const marker = '})();';
  const at = source.lastIndexOf(marker);
  if (at < 0) throw new Error('single-trades.js IIFE terminator not found');
  const exportCode = `\n  globalThis.__singleTradesCore = {\n` +
    `    parseTxt, calcKpi, analysisPeriodFromHeader, getSlipPointsPerTrade, CFG\n` +
    `  };\n`;
  source = source.slice(0, at) + exportCode + source.slice(at);

  const nullElement = new Proxy({}, {
    get(_target, prop) {
      if (prop === 'value') return '';
      if (prop === 'textContent') return '';
      if (prop === 'innerHTML') return '';
      if (prop === 'style') return {};
      if (prop === 'classList') return { add() {}, remove() {} };
      return () => {};
    },
    set() { return true; }
  });

  const context = {
    console,
    URL,
    TextDecoder,
    setTimeout,
    clearTimeout,
    isFinite,
    Math,
    Date,
    Number,
    String,
    Array,
    Object,
    JSON,
    RegExp,
    parseInt,
    parseFloat,
    location: { href: 'http://localhost/mini-single-trades.html?market=mini' },
    alert() {},
    fetch: async () => { throw new Error('fetch is disabled in KPI harness'); },
    document: {
      querySelector() { return null; },
      getElementById() { return null; },
      createElement() { return nullElement; },
      addEventListener() {},
      body: nullElement
    },
    window: {
      FUTURES_PRODUCT_PROFILE: { market: 'mini', pointValue: 50, feePerSide: 18, capital: 250000 },
      FUTURES_MARKET: 'mini',
      addEventListener() {},
      removeEventListener() {}
    }
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(source, context, { filename: JS_PATH, timeout: 10000 });
  if (!context.__singleTradesCore) throw new Error('Core export injection failed');
  return { core: context.__singleTradesCore, evaluatedSource: source };
}

function cumulative(values) {
  let s = 0;
  return values.map(v => (s += v));
}

function normalizeKpi(kpi) {
  if (!kpi) return null;
  const out = {};
  for (const [k, v] of Object.entries(kpi)) {
    if (v instanceof Date) out[k] = v.toISOString();
    else if (typeof v === 'number' && Number.isFinite(v)) out[k] = v;
    else if (v == null || typeof v === 'string' || typeof v === 'boolean') out[k] = v;
  }
  return out;
}

function selectedRow(strategyId, kpi) {
  return {
    strategy_id: strategyId,
    trades: kpi.n,
    total_net_twd: kpi.totalNet,
    total_return_pct: kpi.totalReturnPct,
    cagr: kpi.cagr,
    sharpe: kpi.sharpeTrade,
    sortino: kpi.sortinoTrade,
    calmar: kpi.calmar,
    pf: kpi.pf,
    win_rate: kpi.winRate,
    expectancy_twd: kpi.expectancy,
    max_dd_twd: kpi.maxDd,
    max_dd_pct: kpi.maxDdPct,
    recovery_factor: kpi.recoveryFactor,
    risk_of_ruin: kpi.riskOfRuin,
    worst_day_twd: kpi.worstDayPnl,
    worst_week_twd: kpi.worstWeekPnl,
    avg_hold_min: kpi.avgHoldMin,
    total_fee_twd: kpi.totalFee,
    total_tax_twd: kpi.totalTax,
    total_slip_twd: kpi.totalSlipCost,
    total_cost_twd: kpi.totalCost,
    cost_ratio: kpi.costRatio,
    stability_r2: kpi.stabilityR2
  };
}

function toCsv(rows) {
  if (!rows.length) return '';
  const cols = Object.keys(rows[0]);
  const esc = value => {
    if (value == null) return '';
    const s = String(value);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  return cols.join(',') + '\n' + rows.map(r => cols.map(c => esc(r[c])).join(',')).join('\n') + '\n';
}

function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const { core, evaluatedSource } = loadCore();
  core.CFG.pointValue = 50;
  core.CFG.feePerSide = 18;
  core.CFG.taxRate = 0.00002;
  core.CFG.entrySlipPoints = 0;
  core.CFG.exitSlipPoints = 2;
  core.CFG.capital = 250000;

  const noHeaderSample = [
    '202001020905 12000 新買',
    '202001020910 12020 平賣'
  ].join('\n');
  const noHeaderParsed = core.parseTxt(noHeaderSample);
  if (noHeaderParsed.trades.length !== 1) {
    throw new Error(`Optional-header parser test failed: ${noHeaderParsed.trades.length}`);
  }

  const files = fs.readdirSync(OUT)
    .filter(f => f.endsWith('_IND_THEORY.txt'))
    .sort();
  if (files.length < 3) throw new Error(`Expected at least 3 IND ledgers, found ${files.length}`);

  const detailed = [];
  const summary = [];
  for (const file of files) {
    const indPath = path.join(OUT, file);
    const trdPath = path.join(OUT, file.replace('_IND_THEORY.txt', '_TRD_THEORY.txt'));
    const indText = fs.readFileSync(indPath, 'utf8');
    const trdText = fs.readFileSync(trdPath, 'utf8');
    const ind = core.parseTxt(indText);
    const trd = core.parseTxt(trdText);
    if (JSON.stringify(ind.trades) !== JSON.stringify(trd.trades)) {
      throw new Error(`single-trades.js parsed ledger mismatch: ${file}`);
    }
    const theoPnls = ind.trades.map(t => t.theoNet);
    const slipPoints = core.getSlipPointsPerTrade();
    const slipCost = core.CFG.pointValue * slipPoints;
    const actualPnls = theoPnls.map(v => v - slipCost);
    const period = core.analysisPeriodFromHeader(ind.header);
    const kpiTheo = core.calcKpi(ind.trades, theoPnls, cumulative(theoPnls), 0, period);
    const kpiActual = core.calcKpi(ind.trades, actualPnls, cumulative(actualPnls), slipPoints, period);
    const strategyId = file.replace('_IND_THEORY.txt', '');
    detailed.push({
      strategy_id: strategyId,
      indicator_file: file,
      transaction_file: path.basename(trdPath),
      parsed_trade_count: ind.trades.length,
      parsed_ledgers_equal: true,
      indicator_text_sha256: sha256(indText),
      transaction_text_sha256: sha256(trdText),
      kpi_theoretical: normalizeKpi(kpiTheo),
      kpi_actual: normalizeKpi(kpiActual)
    });
    summary.push(selectedRow(strategyId, kpiActual));
  }

  const audit = {
    engine: 'single-trades.js exact parseTxt/calcKpi executed in Node VM',
    source_file: JS_PATH,
    evaluated_source_sha256: sha256(evaluatedSource),
    configuration: { ...core.CFG },
    optional_header_test: {
      passed: true,
      input_action_rows: 2,
      parsed_trades: noHeaderParsed.trades.length
    },
    strategies: detailed
  };
  fs.writeFileSync(path.join(OUT, 'single_trades_kpi.json'), JSON.stringify(audit, null, 2), 'utf8');
  fs.writeFileSync(path.join(OUT, 'single_trades_kpi.csv'), toCsv(summary), 'utf8');
  console.log(JSON.stringify({ optional_header_test: audit.optional_header_test, summary }, null, 2));
}

main();
