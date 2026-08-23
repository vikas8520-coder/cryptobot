#!/usr/bin/env python3
"""
nifty_paper_order.py — paper trade journal for the 5m Nifty breakout rule.

Tracks virtual long-option positions in a SQLite database. `nifty_paper_alert.py`
calls this on every tick; no real money or broker is involved until a live broker
class is plugged in.

Schema: `trades` table with open/closed states, entry/exit prices, P&L.
"""
import os
import sqlite3
from datetime import datetime, time as dtime

import numpy as np
import pandas as pd

DB = "/Users/vikasreddy/cryptobot/nifty_paper_trades.sqlite"
LOT = 65
DELTA = 0.50
DEFAULT_SPREAD = 0.5


def init_db(path=DB):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,  -- 'open' or 'closed'
            side TEXT NOT NULL,    -- 'call' or 'put'
            opt_type TEXT NOT NULL,
            strike REAL,
            expiry TEXT,
            entry_time TEXT,
            entry_premium REAL,
            entry_index REAL,
            target_premium REAL,
            stop_premium REAL,
            target_index REAL,
            stop_index REAL,
            lots REAL,
            capital_risk_pct REAL,
            entry_capital REAL,
            exit_time TEXT,
            exit_premium REAL,
            exit_index REAL,
            exit_reason TEXT,
            pnl_per_option REAL,
            pnl_trade REAL,
            ret_pct REAL,
            notes TEXT
        )
        """
    )
    conn.commit()
    return conn


def get_open_trades(conn):
    cur = conn.cursor()
    cur.execute("SELECT * FROM trades WHERE status='open' ORDER BY entry_time")
    rows = cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _last_closed_trade(conn):
    cur = conn.cursor()
    cur.execute("SELECT * FROM trades WHERE status='closed' ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def _bar_index(df, entry_time):
    idx = df.index[df["Date"] == entry_time].tolist()
    if idx:
        return idx[0]
    return None


def _heuristic_same_bar(side, open_, entry_index, target_index, stop_index):
    """Return 'target' or 'stop' using the 5m bar open direction."""
    if side == "call":
        return "target" if open_ >= entry_index else "stop"
    return "target" if open_ <= entry_index else "stop"


def _process_single_trade(df, t):
    """Check target/stop/eod for one open paper trade using 5m bars."""
    entry_i = _bar_index(df, t["entry_time"])
    if entry_i is None:
        return None
    n = len(df)
    close = df["Close"].values
    high = df["High"].values
    low = df["Low"].values
    open_ = df["Open"].values
    dates = df["Date"]
    side = t["side"]
    target_index = t["target_index"]
    stop_index = t["stop_index"]
    p0 = t["entry_premium"]

    start_i = entry_i + 1
    if start_i >= n:
        return None

    # Process all complete bars after entry; stop at first exit.
    for i in range(start_i, n):
        if side == "call":
            hit_target = high[i] >= target_index
            hit_stop = low[i] <= stop_index
        else:
            hit_target = low[i] <= target_index
            hit_stop = high[i] >= stop_index

        if hit_target and hit_stop:
            decision = _heuristic_same_bar(side, open_[i], t["entry_index"],
                                           target_index, stop_index)
            if decision == "target":
                hit_stop = False
            else:
                hit_target = False

        if hit_target:
            exit_index = target_index
            reason = "target"
        elif hit_stop:
            exit_index = stop_index
            reason = "stop"
        else:
            # EOD: last bar of the trading day or the last bar we have.
            is_last = (i == n - 1 or
                       pd.Timestamp(dates.iloc[i]).normalize() !=
                       pd.Timestamp(dates.iloc[i + 1]).normalize())
            if not is_last:
                continue
            if side == "call":
                exit_index = close[i]
            else:
                exit_index = close[i]
            reason = "eod"

        if side == "call":
            p_exit = p0 + DELTA * (exit_index - t["entry_index"])
        else:
            p_exit = p0 + DELTA * (t["entry_index"] - exit_index)

        pnl_per = p_exit - p0 - 2.0 * DEFAULT_SPREAD
        pnl_trade = pnl_per * t["lots"] * LOT
        entry_capital = t.get("entry_capital") or 100000.0
        ret = pnl_trade / entry_capital
        return {
            "exit_time": str(dates.iloc[i]),
            "exit_premium": round(p_exit, 2),
            "exit_index": round(exit_index, 2),
            "exit_reason": reason,
            "pnl_per_option": round(pnl_per, 2),
            "pnl_trade": round(pnl_trade, 2),
            "ret_pct": round(ret * 100, 2),
        }
    return None


def process_open_trades(conn, df, send_fn=None):
    """Update any open paper trades. Return list of closed trades."""
    closed = []
    for t in get_open_trades(conn):
        result = _process_single_trade(df, t)
        if result is None:
            continue
        # close trade
        conn.execute(
            """
            UPDATE trades SET status='closed', exit_time=?, exit_premium=?,
                exit_index=?, exit_reason=?, pnl_per_option=?, pnl_trade=?, ret_pct=?,
                notes='Paper closed'
            WHERE id=?
            """,
            (result["exit_time"], result["exit_premium"], result["exit_index"],
             result["exit_reason"], result["pnl_per_option"], result["pnl_trade"],
             result["ret_pct"], t["id"]),
        )
        conn.commit()
        t.update(result)
        closed.append(t)
        if send_fn:
            send_fn(t)
    return closed


def open_trade(conn, signal, capital=100000.0, risk_pct=0.02):
    """Record a new paper trade from a signal."""
    p0 = signal["entry_premium"]
    risk_per_lot = LOT * (p0 * 0.05 + 2.0 * DEFAULT_SPREAD)
    lots = (capital * risk_pct) / risk_per_lot if risk_per_lot > 0 else 0.0
    conn.execute(
        """
        INSERT INTO trades (status, side, opt_type, strike, expiry, entry_time,
            entry_premium, entry_index, target_premium, stop_premium, target_index,
            stop_index, lots, capital_risk_pct, entry_capital, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("open", signal["side"], signal["opt_type"], signal["strike"],
         signal["expiry"], signal["bar_time"], signal["entry_premium"],
         signal["entry_index"], signal["target_premium"], signal["stop_premium"],
         signal["target_index"], signal["stop_index"], lots, risk_pct,
         capital, "Paper order"),
    )
    conn.commit()
    return lots


def format_pnl_msg(trade):
    direction = "CALL" if trade["side"] == "call" else "PUT"
    sign = "+" if trade["pnl_trade"] >= 0 else ""
    return (
        f"🟠 PAPER TRADE CLOSED\n"
        f"Direction: {direction} ({trade['opt_type']})\n"
        f"Entry: {trade['entry_time']}\n"
        f"Exit: {trade['exit_time']} ({trade['exit_reason']})\n"
        f"Strike: {trade['strike']} | Lots: {trade['lots']:.2f}\n"
        f"P&L: ₹{sign}{trade['pnl_trade']:.2f} ({trade['ret_pct']:.2f}%)\n"
    )
