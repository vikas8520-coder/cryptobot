"""Run SolRSI2_1h unchanged against every candidate coin, one isolated backtest per coin.

WHY one backtest per coin instead of one multi-pair run: max_open_trades=1 means a shared run
would let whichever coin signals first block the others, so P&L would measure queueing luck
rather than each coin's edge. Isolated runs stay directly comparable to the SOL baseline, which
was also a single-pair max_open_trades=1 run.

Backtests read Binance USDT-M data (the India deployment venue) but use OKX market metadata,
because Binance's REST API answers 451 from this machine. Fee is pinned at 0.05% in the config,
so exchange metadata cannot affect costs.

Results are scraped from the printed metrics table rather than --export: freqtrade 2026's
zip exporter ignores --export-filename here and writes timestamped archives to a shared dir,
which cannot be mapped back to a coin under parallel runs.
"""

import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

ROOT = Path("/Users/vikasreddy/cryptobot")
DATA = ROOT / "user_data/data/binance/futures"
WORKERS = 4

ROWS = {                                   # metric label -> output column
    "Total profit %": "profit_pct",
    "Profit factor": "pf",
    "Sharpe (closed trades)": "sharpe",
    "Sharpe (daily wallet balance)": "sharpe_d",
    "Calmar (daily wallet balance)": "calmar",
    "Max % of account underwater": "maxdd_pct",
    "Mean profit p-value": "pval",
    "CAGR %": "cagr_pct",
    "Market change": "mkt_change_pct",
}
SUMMARY = re.compile(
    r"│\s*TOTAL\s*│\s*(\d+)\s*│\s*(-?[\d.]+)\s*│\s*(-?[\d.]+)\s*│\s*(-?[\d.]+)\s*│"
    r"\s*([\d:]+)\s*│\s*(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s*│")
EXITROW = re.compile(r"│\s*(roi|exit_signal|stop_loss|trailing_stop_loss|force_exit)\s*│\s*(\d+)\s*│")


def num(s: str) -> float | None:
    m = re.search(r"-?[\d.]+", s.replace(",", ""))
    return float(m.group()) if m else None


def parse(txt: str) -> dict:
    out = {}
    for line in txt.splitlines():
        if "│" not in line:
            continue
        cells = [c.strip() for c in line.split("│")]
        if len(cells) >= 4 and cells[1] in ROWS:
            out[ROWS[cells[1]]] = num(cells[2])
        m = EXITROW.match(line.strip())
        if m:
            out[f"x_{m.group(1)}"] = int(m.group(2))
    m = SUMMARY.search(txt)
    if m:
        out |= {"trades": int(m.group(1)), "avg_profit_pct": float(m.group(2)),
                "dur": m.group(5), "wins": int(m.group(6)), "losses": int(m.group(8)),
                "win_pct": float(m.group(9))}
    return out


def run(base: str, timerange: str = "20220101-20260630") -> dict:
    cmd = [str(ROOT / ".venv/bin/freqtrade"), "backtesting",
           "-c", str(ROOT / "config_rsi2_screen.json"), "-s", "SolRSI2_1h",
           "--pairs", f"{base}/USDT:USDT", "--timerange", timerange,
           "--datadir", str(ROOT / "user_data/data/binance"), "--cache", "none"]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    got = parse(r.stdout)
    if not got.get("trades"):
        tail = (r.stderr or r.stdout).strip().splitlines()
        return {"coin": base, "error": tail[-1][:110] if tail else "no trades/failed"}
    return {"coin": base} | got


if __name__ == "__main__":
    tr = "20220101-20260630"
    args = sys.argv[1:]
    if args and args[0].startswith("--timerange="):
        tr = args.pop(0).split("=", 1)[1]
    bases = args or sorted({f.name.split("_USDT")[0] for f in DATA.glob("*-1h-futures.feather")})
    rows = []
    with ThreadPoolExecutor(WORKERS) as ex:
        for r in ex.map(lambda b: run(b, tr), bases):
            print(r, flush=True)
            rows.append(r)
    df = pd.DataFrame(rows)
    df.to_csv(ROOT / f"research/screen_{tr}.csv", index=False)
    pd.set_option("display.width", 250)
    cols = [c for c in ["coin", "trades", "profit_pct", "pf", "win_pct", "sharpe_d", "maxdd_pct",
                        "calmar", "pval", "x_roi", "x_exit_signal", "x_stop_loss",
                        "mkt_change_pct"] if c in df]
    print("\n" + df.dropna(subset=["trades"]).sort_values("profit_pct", ascending=False)
          [cols].to_string(index=False))
