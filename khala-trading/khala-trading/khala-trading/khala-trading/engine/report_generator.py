"""
Khala Trading -- Backtest HTML Report Generator
----------------------------------------------------
Turns real backtest_engine.py output (verified zero-lookahead, real
historical data) into a visual report: equity curve + drawdown, win rate
by weekday, P&L distribution, and a full trade log. Self-contained HTML
file, no server needed -- just open it in a browser.
"""

import json


def generate_html_report(symbol, stats, trades, out_path):
    resolved = [t for t in trades if t['outcome'] in ('WIN', 'LOSS')]

    equity = 0.0
    equity_curve = [0.0]
    peak = 0.0
    drawdown_curve = [0.0]
    for t in resolved:
        equity += t['pnl']
        peak = max(peak, equity)
        equity_curve.append(round(equity, 2))
        drawdown_curve.append(round(peak - equity, 2))

    weekday_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    weekday_labels = [weekday_names[wd] for wd in sorted(stats['weekday_breakdown'].keys())]
    weekday_wins = [stats['weekday_breakdown'][wd]['wins'] for wd in sorted(stats['weekday_breakdown'].keys())]
    weekday_losses = [stats['weekday_breakdown'][wd]['losses'] for wd in sorted(stats['weekday_breakdown'].keys())]

    pnl_values = [t['pnl'] for t in resolved]

    data = {
        'symbol': symbol,
        'stats': stats,
        'equity_curve': equity_curve,
        'drawdown_curve': drawdown_curve,
        'weekday_labels': weekday_labels,
        'weekday_wins': weekday_wins,
        'weekday_losses': weekday_losses,
        'pnl_values': pnl_values,
        'trades': resolved[-100:],  # most recent 100 for the table, avoid a huge page
    }

    html = HTML_TEMPLATE.replace('__DATA_JSON__', json.dumps(data, default=str))
    with open(out_path, 'w') as f:
        f.write(html)
    return out_path


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Khala Trading -- Backtest Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  :root {
    --bg: #090b10; --panel: #12151c; --border: #232733;
    --accent: #00d9c0; --green: #2ed573; --red: #ff4757; --text-dim: #7a8699;
  }
  body { background: var(--bg); color: #e8eaf0; font-family: 'JetBrains Mono', monospace; margin: 0; padding: 24px; }
  h1 { font-size: 20px; color: var(--accent); }
  .subtitle { color: var(--text-dim); font-size: 13px; margin-bottom: 24px; }
  .grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
  .card-label { font-size: 10px; color: var(--text-dim); text-transform: uppercase; margin-bottom: 6px; }
  .card-value { font-size: 20px; font-weight: 700; }
  .pos { color: var(--green); } .neg { color: var(--red); }
  .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 20px; }
  .panel-title { font-size: 13px; color: var(--accent); font-weight: 600; margin-bottom: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th { text-align: left; color: var(--text-dim); padding: 6px; border-bottom: 1px solid var(--border); font-size: 10px; text-transform: uppercase; }
  td { padding: 8px 6px; border-bottom: 1px solid var(--border); }
  .charts-row { display: grid; grid-template-columns: 2fr 1fr; gap: 20px; }
  .warning { background: rgba(255,171,0,0.08); border: 1px solid #ffab00; border-radius: 8px; padding: 12px; margin-bottom: 20px; font-size: 12px; color: #ffab00; }
</style>
</head>
<body>
  <h1 id="title"></h1>
  <div class="subtitle">Backtested against real historical data with verified zero lookahead bias. Not a guarantee of live performance.</div>
  <div id="low-sample-warning" class="warning" style="display:none;"></div>

  <div class="grid" id="metrics"></div>

  <div class="charts-row">
    <div class="panel">
      <div class="panel-title">EQUITY CURVE & DRAWDOWN</div>
      <canvas id="equityChart" height="100"></canvas>
    </div>
    <div class="panel">
      <div class="panel-title">WIN/LOSS BY WEEKDAY</div>
      <canvas id="weekdayChart" height="140"></canvas>
    </div>
  </div>

  <div class="panel">
    <div class="panel-title">P&amp;L DISTRIBUTION</div>
    <canvas id="pnlChart" height="60"></canvas>
  </div>

  <div class="panel">
    <div class="panel-title">TRADE LOG (most recent 100)</div>
    <table>
      <thead><tr><th>Direction</th><th>Entry</th><th>SL</th><th>TP1</th><th>Score</th><th>Outcome</th><th>P&amp;L</th></tr></thead>
      <tbody id="trade-rows"></tbody>
    </table>
  </div>

<script>
const data = __DATA_JSON__;
document.getElementById('title').textContent = 'Backtest Report -- ' + data.symbol;

const s = data.stats;
if (s.resolved < 30) {
  const w = document.getElementById('low-sample-warning');
  w.style.display = 'block';
  w.textContent = `Only ${s.resolved} resolved trades -- too few for reliable conclusions. Treat these numbers as rough until you have 50-100+ resolved trades.`;
}

const metrics = [
  ['Win Rate', s.win_rate !== null ? s.win_rate + '%' : 'N/A', null],
  ['Profit Factor', s.profit_factor !== null ? s.profit_factor : 'N/A', null],
  ['Expectancy/Trade', s.expectancy_per_trade !== null ? '$' + s.expectancy_per_trade : 'N/A', s.expectancy_per_trade],
  ['Total P&L', '$' + s.total_pnl, s.total_pnl],
  ['Max Drawdown', '$' + s.max_drawdown, null],
  ['Avg R:R', s.average_rr !== null ? s.average_rr : 'N/A', null],
];
document.getElementById('metrics').innerHTML = metrics.map(([label, value, sign]) => `
  <div class="card">
    <div class="card-label">${label}</div>
    <div class="card-value ${sign !== null ? (sign >= 0 ? 'pos' : 'neg') : ''}">${value}</div>
  </div>
`).join('');

new Chart(document.getElementById('equityChart'), {
  type: 'line',
  data: {
    labels: data.equity_curve.map((_, i) => i),
    datasets: [
      { label: 'Equity ($)', data: data.equity_curve, borderColor: '#00d9c0', backgroundColor: 'rgba(0,217,192,0.08)', fill: true, tension: 0.3, pointRadius: 0 },
      { label: 'Drawdown ($)', data: data.drawdown_curve, borderColor: '#ff4757', backgroundColor: 'rgba(255,71,87,0.08)', fill: true, tension: 0.3, pointRadius: 0, yAxisID: 'y1' },
    ]
  },
  options: { responsive: true, plugins: { legend: { labels: { color: '#7a8699' } } },
    scales: { x: { display: false }, y: { ticks: { color: '#7a8699' }, grid: { color: '#232733' } }, y1: { position: 'right', display: false } } }
});

new Chart(document.getElementById('weekdayChart'), {
  type: 'bar',
  data: { labels: data.weekday_labels, datasets: [
    { label: 'Wins', data: data.weekday_wins, backgroundColor: '#2ed573' },
    { label: 'Losses', data: data.weekday_losses, backgroundColor: '#ff4757' },
  ]},
  options: { responsive: true, plugins: { legend: { labels: { color: '#7a8699' } } },
    scales: { x: { ticks: { color: '#7a8699' }, grid: { display: false } }, y: { ticks: { color: '#7a8699' }, grid: { color: '#232733' } } } }
});

const bucketSize = Math.max(1, Math.ceil((Math.max(...data.pnl_values, 0) - Math.min(...data.pnl_values, 0)) / 12));
const buckets = {};
data.pnl_values.forEach(v => {
  const b = Math.floor(v / bucketSize) * bucketSize;
  buckets[b] = (buckets[b] || 0) + 1;
});
const bucketKeys = Object.keys(buckets).map(Number).sort((a,b) => a-b);
new Chart(document.getElementById('pnlChart'), {
  type: 'bar',
  data: { labels: bucketKeys.map(k => '$' + k), datasets: [{
    label: 'Trades', data: bucketKeys.map(k => buckets[k]),
    backgroundColor: bucketKeys.map(k => k >= 0 ? '#2ed573' : '#ff4757'),
  }]},
  options: { responsive: true, plugins: { legend: { display: false } },
    scales: { x: { ticks: { color: '#7a8699' }, grid: { display: false } }, y: { ticks: { color: '#7a8699' }, grid: { color: '#232733' } } } }
});

document.getElementById('trade-rows').innerHTML = data.trades.slice().reverse().map(t => `
  <tr>
    <td>${t.direction.toUpperCase()}</td>
    <td>${t.entry_price}</td>
    <td>${t.sl_price}</td>
    <td>${t.tp1}</td>
    <td>${t.score}</td>
    <td style="color:${t.outcome === 'WIN' ? '#2ed573' : '#ff4757'}">${t.outcome}</td>
    <td style="color:${t.pnl >= 0 ? '#2ed573' : '#ff4757'}">${t.pnl >= 0 ? '+' : ''}$${t.pnl.toFixed(2)}</td>
  </tr>
`).join('');
</script>
</body>
</html>
"""
