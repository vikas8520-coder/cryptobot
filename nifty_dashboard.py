#!/usr/bin/env python3
"""
nifty_dashboard.py — web dashboard for the Nifty 5m paper trading bot.

Serves http://127.0.0.1:8092 by default and shows:
- Open and closed paper option trades
- Equity curve, total return, drawdown, win rate, profit factor
- Latest bot status / log tail
"""
import argparse
import json
import os
import sqlite3
from datetime import datetime

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

import nifty_paper_order as porder

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "nifty_paper_trades.sqlite")
LOG = os.path.join(BASE, "nifty_paper_alert.log")
START_CAPITAL = 100000.0

app = FastAPI()


def _read_trades():
    if not os.path.exists(DB):
        return [], []
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    open_ = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE status='open' ORDER BY entry_time").fetchall()]
    closed = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE status='closed' ORDER BY entry_time").fetchall()]
    conn.close()
    return open_, closed


def _compute_stats(closed):
    if not closed:
        return {
            "trades": 0, "win_rate_pct": 0.0, "profit_factor": 0.0,
            "total_return_pct": 0.0, "max_drawdown_pct": 0.0,
            "total_pnl": 0.0, "avg_trade_pct": 0.0,
            "current_capital": START_CAPITAL, "equity": [START_CAPITAL],
        }
    # Use sequential absolute P&L for the equity curve
    capital = START_CAPITAL
    equity = [capital]
    for t in closed:
        capital += float(t["pnl_trade"] or 0)
        equity.append(capital)
    total_return = (capital - START_CAPITAL) / START_CAPITAL
    eq_arr = [e / START_CAPITAL for e in equity]
    peak = eq_arr[0]
    dd = 0.0
    for e in eq_arr:
        if e > peak:
            peak = e
        dd = min(dd, e / peak - 1.0)
    wins = [t for t in closed if (t["pnl_trade"] or 0) > 0]
    losses = [t for t in closed if (t["pnl_trade"] or 0) <= 0]
    win_rate = 100.0 * len(wins) / len(closed) if closed else 0.0
    gross_wins = sum(t["pnl_trade"] for t in wins)
    gross_losses = sum(t["pnl_trade"] for t in losses)
    pf = gross_wins / -gross_losses if gross_losses < 0 else ("inf" if gross_wins > 0 else 0.0)
    avg_trade = sum(t["pnl_trade"] for t in closed) / len(closed)
    return {
        "trades": len(closed),
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(float(pf), 2) if pf != "inf" else pf,
        "total_return_pct": round(total_return * 100, 2),
        "max_drawdown_pct": round(dd * 100, 2),
        "total_pnl": round(sum(t["pnl_trade"] for t in closed), 2),
        "avg_trade_pct": round(avg_trade / START_CAPITAL * 100, 2),
        "current_capital": round(capital, 2),
        "equity": equity,
    }


def _log_tail(n=20):
    if not os.path.exists(LOG):
        return []
    try:
        with open(LOG) as f:
            return f.read().splitlines()[-n:]
    except Exception:
        return []


def _html():
    open_, closed = _read_trades()
    stats = _compute_stats(closed)
    log = _log_tail(15)
    equity_json = json.dumps(stats["equity"])
    log_json = json.dumps(log)

    open_rows = ""
    for t in open_:
        open_rows += f"""
        <tr>
          <td>{t['entry_time']}</td>
          <td>{t['side'].upper()} ({t['opt_type']})</td>
          <td>{t['strike']}</td>
          <td>{t['expiry']}</td>
          <td>{t['entry_premium']}</td>
          <td>{t['target_premium']}</td>
          <td>{t['stop_premium']}</td>
          <td>{t['lots']}</td>
        </tr>
        """

    closed_rows = ""
    for t in closed:
        pnl = t['pnl_trade']
        cls = 'win' if pnl > 0 else 'loss'
        closed_rows += f"""
        <tr class="{cls}">
          <td>{t['entry_time']}</td>
          <td>{t['side'].upper()} ({t['opt_type']})</td>
          <td>{t['strike']}</td>
          <td>{t['exit_time']}</td>
          <td>{t['exit_reason']}</td>
          <td class="pnl">{pnl:,.2f}</td>
          <td>{t['ret_pct']}%</td>
        </tr>
        """

    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="60">
  <title>Nifty 5m Paper Trading Desk</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 2rem; background:#0f1115; color:#e0e0e0; }}
    h1 {{ margin-bottom:.2rem; }}
    .subtitle {{ color:#888; font-size:.9rem; margin-bottom:1.5rem; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr)); gap:1rem; margin-bottom:1.5rem; }}
    .card {{ background:#181b21; border-radius:8px; padding:1rem; border:1px solid #2a2d35; }}
    .card .label {{ color:#888; font-size:.8rem; text-transform:uppercase; }}
    .card .value {{ font-size:1.6rem; font-weight:600; margin-top:.3rem; }}
    .positive {{ color:#2ecc71; }}
    .negative {{ color:#e74c3c; }}
    table {{ width:100%; border-collapse:collapse; background:#181b21; border-radius:8px; overflow:hidden; margin-bottom:1.5rem; }}
    th, td {{ padding:.6rem .8rem; text-align:left; border-bottom:1px solid #2a2d35; font-size:.9rem; }}
    th {{ background:#22262d; color:#aaa; }}
    .win td.pnl {{ color:#2ecc71; }}
    .loss td.pnl {{ color:#e74c3c; }}
    .section {{ margin-bottom:2rem; }}
    .log {{ background:#181b21; border-radius:8px; padding:1rem; font-family:monospace; font-size:.85rem; max-height:250px; overflow:auto; white-space:pre-line; border:1px solid #2a2d35; }}
    canvas {{ max-height:320px; }}
  </style>
</head>
<body>
  <h1>Nifty 5m Paper Trading Desk</h1>
  <div class="subtitle">Auto-refresh every 60s · live-paper broker · 2% risk per trade</div>

  <div class="grid">
    <div class="card"><div class="label">Closed trades</div><div class="value">{stats['trades']}</div></div>
    <div class="card"><div class="label">Win rate</div><div class="value">{stats['win_rate_pct']}%</div></div>
    <div class="card"><div class="label">Profit factor</div><div class="value">{stats['profit_factor']}</div></div>
    <div class="card"><div class="label">Total P&L</div><div class="value {'positive' if stats['total_pnl'] >= 0 else 'negative'}">₹{stats['total_pnl']:,.2f}</div></div>
    <div class="card"><div class="label">Total return</div><div class="value {'positive' if stats['total_return_pct'] >= 0 else 'negative'}">{stats['total_return_pct']}%</div></div>
    <div class="card"><div class="label">Max drawdown</div><div class="value negative">{stats['max_drawdown_pct']}%</div></div>
    <div class="card"><div class="label">Current capital</div><div class="value">₹{stats['current_capital']:,.2f}</div></div>
    <div class="card"><div class="label">Avg trade</div><div class="value">{stats['avg_trade_pct']}%</div></div>
  </div>

  <div class="section">
    <h3>Equity curve</h3>
    <canvas id="equityChart"></canvas>
  </div>

  <div class="section">
    <h3>Open positions ({len(open_)})</h3>
    <table>
      <thead>
        <tr><th>Entry</th><th>Side</th><th>Strike</th><th>Expiry</th><th>Entry Prem</th><th>Target</th><th>Stop</th><th>Lots</th></tr>
      </thead>
      <tbody>{open_rows or '<tr><td colspan="8">No open positions</td></tr>'}</tbody>
    </table>
  </div>

  <div class="section">
    <h3>Closed trades</h3>
    <table>
      <thead>
        <tr><th>Entry</th><th>Side</th><th>Strike</th><th>Exit</th><th>Reason</th><th>P&L</th><th>Return</th></tr>
      </thead>
      <tbody>{closed_rows or '<tr><td colspan="7">No closed trades yet</td></tr>'}</tbody>
    </table>
  </div>

  <div class="section">
    <h3>Bot log tail</h3>
    <div class="log">{"<br>".join(log)}</div>
  </div>

  <script>
    const ctx = document.getElementById('equityChart').getContext('2d');
    const data = {equity_json};
    new Chart(ctx, {{
      type: 'line',
      data: {{
        labels: data.map((_, i) => i),
        datasets: [{{
          label: 'Equity (₹)',
          data: data,
          borderColor: '#2ecc71',
          backgroundColor: 'rgba(46, 204, 113, 0.1)',
          tension: 0.2,
          fill: true,
          pointRadius: 2
        }}]
      }},
      options: {{
        responsive: true,
        maintainAspectRatio: false,
        scales: {{
          x: {{ display: false }},
          y: {{ grid: {{ color: '#2a2d35' }} }}
        }},
        plugins: {{ legend: {{ display: false }} }}
      }}
    }});
  </script>
</body>
</html>"""
    return html


@app.get("/", response_class=HTMLResponse)
def index():
    return _html()


@app.get("/api/stats")
def api_stats():
    open_, closed = _read_trades()
    return JSONResponse({"open": open_, "closed": closed, "stats": _compute_stats(closed), "log": _log_tail(20)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
