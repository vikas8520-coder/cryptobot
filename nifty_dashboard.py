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
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="60">
  <title>Nifty 5m Paper Trading Desk</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {{
      --ground:#0E1420; --surface:#161E2E; --surface-2:#1C2739; --line:#26314A;
      --text:#E6EAF2; --muted:#8A97B2; --faint:#5C6884;
      --amber:#F0A83C; --teal:#3FC7A8; --brick:#E5674E;
      --mono:ui-monospace,"SF Mono","Menlo","Consolas",monospace;
      --sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    }}
    :root[data-theme="light"] {{
      --ground:#F1F4F9; --surface:#FFFFFF; --surface-2:#EDF1F7; --line:#DBE1EC;
      --text:#16202E; --muted:#586880; --faint:#93A0B4;
      --amber:#C77E12; --teal:#1A9C80; --brick:#CD4B32;
    }}
    * {{ box-sizing:border-box; }}
    body {{ font-family:var(--sans); margin:1.5rem; background:var(--ground); color:var(--text); transition:background .15s,color .15s; }}
    h1 {{ margin-bottom:.2rem; font-size:1.4rem; }}
    h3 {{ font-size:1.05rem; margin-bottom:.5rem; }}
    .subtitle {{ color:var(--faint); font-size:.85rem; margin-bottom:1.2rem; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr)); gap:.8rem; margin-bottom:1.2rem; }}
    .card {{ background:var(--surface); border-radius:10px; padding:.9rem; border:1px solid var(--line); }}
    .card .label {{ color:var(--faint); font-size:.75rem; text-transform:uppercase; letter-spacing:.03em; }}
    .card .value {{ font-size:1.4rem; font-weight:600; margin-top:.2rem; font-variant-numeric:tabular-nums; }}
    .positive {{ color:var(--teal); }}
    .negative {{ color:var(--brick); }}
    table {{ width:100%; border-collapse:collapse; background:var(--surface); border-radius:10px; overflow:hidden; margin-bottom:1.2rem; border:1px solid var(--line); }}
    th, td {{ padding:.5rem .7rem; text-align:left; border-bottom:1px solid var(--line); font-size:.85rem; }}
    th {{ background:var(--surface-2); color:var(--muted); font-weight:600; }}
    .win td.pnl {{ color:var(--teal); }}
    .loss td.pnl {{ color:var(--brick); }}
    .section {{ margin-bottom:1.5rem; }}
    .log {{ background:var(--surface); border-radius:10px; padding:.8rem; font-family:var(--mono); font-size:.8rem; max-height:220px; overflow:auto; white-space:pre-line; border:1px solid var(--line); color:var(--muted); }}
    canvas {{ max-height:300px; }}
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
    // ---- theme sync with parent dashboard (same-origin, direct read) ----
    function applyTheme(t) {{
      document.documentElement.setAttribute("data-theme", t === "light" ? "light" : "dark");
      if (window._chart) {{
        const s = getComputedStyle(document.documentElement);
        window._chart.data.datasets[0].borderColor = s.getPropertyValue("--teal").trim();
        window._chart.data.datasets[0].backgroundColor = s.getPropertyValue("--teal").trim() + "20";
        window._chart.options.scales.y.grid.color = s.getPropertyValue("--line").trim();
        window._chart.options.scales.y.ticks.color = s.getPropertyValue("--muted").trim();
        window._chart.update("none");
      }}
    }}
    function parentTheme() {{
      try {{ return parent.document.documentElement.getAttribute("data-theme") || "dark"; }}
      catch(e) {{ return "dark"; }}
    }}
    let _curTheme = parentTheme();
    applyTheme(_curTheme);
    // watch parent's <html data-theme> for instant updates
    try {{
      new MutationObserver(() => {{
        const t = parentTheme();
        if (t !== _curTheme) {{ _curTheme = t; applyTheme(t); }}
      }}).observe(parent.document.documentElement, {{ attributes:true, attributeFilter:["data-theme"] }});
    }} catch(e) {{}}
    // backup: postMessage from parent
    window.addEventListener("message", e => {{
      if (e.data && e.data.type === "theme") {{ _curTheme = e.data.theme; applyTheme(e.data.theme); }}
    }});

    // ---- equity chart ----
    const ctx = document.getElementById('equityChart').getContext('2d');
    const data = {equity_json};
    const s0 = getComputedStyle(document.documentElement);
    window._chart = new Chart(ctx, {{
      type: 'line',
      data: {{
        labels: data.map((_, i) => i),
        datasets: [{{
          label: 'Equity (₹)',
          data: data,
          borderColor: s0.getPropertyValue("--teal").trim() || "#3FC7A8",
          backgroundColor: (s0.getPropertyValue("--teal").trim() || "#3FC7A8") + "20",
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
          y: {{ grid: {{ color: s0.getPropertyValue("--line").trim() }}, ticks: {{ color: s0.getPropertyValue("--muted").trim() }} }}
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
