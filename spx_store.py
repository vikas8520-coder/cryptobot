#!/usr/bin/env python3
"""
spx_store.py — the `tradesv3_spx.sqlite` writer. The S&P 500 paper bot's trade ledger.

Same story as apex_store.py: this bot is a standalone process, not a freqtrade instance,
so it must write the ledger itself in FREQTRADE'S DIALECT — the literal column names
stats_lib.py reads (`is_open`, `close_profit_abs`, `fee_open_cost`, `funding_fees`,
`is_short`, ...). Match those and /stats, weekly_review, and the dashboard card all light
up from a one-line map edit, with no consumer learning this ledger has a different author.

The rules, atomicity discipline, and column contract are copied verbatim from
apex_store.py (see that file's header for the full rationale): column names are the
contract; booleans store as 0/1; a reader never sees a half-written ledger (mutate a
private copy, fsync, os.replace); journal mode stays DELETE so the db is one self-contained
file at the moment of the rename.

Only three things differ from apex_store: the DB filename, the default exchange tag
("spx"), and the default stake currency ("USD" — this is US equities, not a USDC perp).
"""
import os
import shutil
import sqlite3
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "tradesv3_spx.sqlite")   # git-ignored by the *.sqlite rule
BUSY_TIMEOUT = 5           # seconds; matches stats_lib._conn's reader-side timeout

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER NOT NULL,
    exchange VARCHAR(25) NOT NULL,
    pair VARCHAR(25) NOT NULL,
    base_currency VARCHAR(25),
    stake_currency VARCHAR(25),
    is_open BOOLEAN NOT NULL,
    fee_open FLOAT NOT NULL,
    fee_open_cost FLOAT,
    fee_open_currency VARCHAR(25),
    fee_close FLOAT NOT NULL,
    fee_close_cost FLOAT,
    fee_close_currency VARCHAR(25),
    open_rate FLOAT NOT NULL,
    open_trade_value FLOAT,
    close_rate FLOAT,
    realized_profit FLOAT,
    close_profit FLOAT,
    close_profit_abs FLOAT,
    stake_amount FLOAT NOT NULL,
    max_stake_amount FLOAT,
    amount FLOAT NOT NULL,
    amount_requested FLOAT,
    open_date DATETIME NOT NULL,
    close_date DATETIME,
    exit_reason VARCHAR(255),
    exit_order_status VARCHAR(100),
    strategy VARCHAR(100),
    enter_tag VARCHAR(255),
    timeframe INTEGER,
    trading_mode VARCHAR(7),
    contract_size FLOAT,
    leverage FLOAT,
    is_short BOOLEAN NOT NULL,
    liquidation_price FLOAT,
    interest_rate FLOAT NOT NULL,
    funding_fees FLOAT,
    funding_fee_running FLOAT,
    record_version INTEGER NOT NULL,
    PRIMARY KEY (id)
);
"""

COLUMNS = (
    "id", "exchange", "pair", "base_currency", "stake_currency", "is_open",
    "fee_open", "fee_open_cost", "fee_open_currency",
    "fee_close", "fee_close_cost", "fee_close_currency",
    "open_rate", "open_trade_value", "close_rate", "realized_profit",
    "close_profit", "close_profit_abs",
    "stake_amount", "max_stake_amount", "amount", "amount_requested",
    "open_date", "close_date", "exit_reason", "exit_order_status",
    "strategy", "enter_tag", "timeframe", "trading_mode", "contract_size",
    "leverage", "is_short", "liquidation_price", "interest_rate",
    "funding_fees", "funding_fee_running", "record_version",
)

DEFAULTS = {
    "exchange": "spx", "base_currency": "SPY", "stake_currency": "USD",
    "is_open": 1, "fee_open": 0.0, "fee_open_cost": 0.0, "fee_open_currency": None,
    "fee_close": 0.0, "fee_close_cost": None, "fee_close_currency": None,
    "open_trade_value": None, "close_rate": None, "realized_profit": 0.0,
    "close_profit": None, "close_profit_abs": None,
    "max_stake_amount": None, "amount_requested": None,
    "close_date": None, "exit_reason": None, "exit_order_status": None,
    "strategy": "SpxSmaCross", "enter_tag": None, "timeframe": 60,
    "trading_mode": "SPOT", "contract_size": 1.0, "leverage": 1.0,
    "is_short": 0, "liquidation_price": None, "interest_rate": 0.0,
    "funding_fees": 0.0, "funding_fee_running": 0.0, "record_version": 2,
}


def fmt_dt(dt=None):
    """Exact freqtrade string shape ('2026-07-15 23:16:29.883659') — naive UTC, space
    separator, microseconds; julianday() in stats_lib parses nothing fancier."""
    dt = dt or datetime.now(timezone.utc)
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def _b(v):
    return 1 if v else 0


def _row(trade):
    out = []
    for c in COLUMNS:
        v = trade.get(c, DEFAULTS.get(c))
        if c in ("is_open", "is_short"):
            v = _b(v)
        out.append(v)
    return out


def _mutate(fn):
    """Run fn against a PRIVATE COPY of the ledger, then os.replace it in — readers see
    the whole old file or the whole new one, never a torn write."""
    tmp = DB_PATH + ".tmp"
    try:
        if os.path.exists(DB_PATH):
            shutil.copy2(DB_PATH, tmp)
        elif os.path.exists(tmp):
            os.remove(tmp)
        conn = sqlite3.connect(tmp, timeout=BUSY_TIMEOUT)
        try:
            conn.execute("PRAGMA journal_mode=DELETE;")
            conn.execute(CREATE_TABLE_SQL)
            result = fn(conn)
            conn.commit()
        finally:
            conn.close()
        fd = os.open(tmp, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, DB_PATH)
        return result
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


def _read(fn):
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    try:
        return fn(conn)
    finally:
        conn.close()


def ensure_db():
    return _mutate(lambda c: True)


def open_trade(trade):
    """Insert one OPEN row. Returns the id sqlite assigned — the engine must adopt it."""
    row = dict(trade)
    row["is_open"] = 1
    row.setdefault("open_date", fmt_dt())
    if row.get("open_trade_value") is None:
        row["open_trade_value"] = (float(row.get("open_rate", 0.0)) * float(row.get("amount", 0.0))
                                   + float(row.get("fee_open_cost") or 0.0))
    row.setdefault("max_stake_amount", row.get("stake_amount"))
    row.setdefault("amount_requested", row.get("amount"))

    cols = ",".join(COLUMNS)
    marks = ",".join("?" for _ in COLUMNS)
    sql = f"INSERT INTO trades ({cols}) VALUES ({marks});"

    def go(conn):
        if row.get("id") is None:
            nxt = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM trades;").fetchone()[0]
            row["id"] = int(nxt)
        conn.execute(sql, _row(row))
        return int(row["id"])

    return _mutate(go)


def close_trade(trade_id, close_rate, close_profit=None, close_profit_abs=None,
                exit_reason="exit_signal", close_date=None, fee_close_cost=None,
                fee_close=None, funding_fees=None):
    """Flip a trade to closed in ONE statement. Returns True if a row was updated."""
    sets = ["is_open = 0", "close_rate = ?", "close_date = ?", "exit_reason = ?",
            "exit_order_status = 'closed'", "close_profit = ?", "close_profit_abs = ?",
            "realized_profit = ?"]
    vals = [float(close_rate), close_date or fmt_dt(), str(exit_reason),
            None if close_profit is None else float(close_profit),
            None if close_profit_abs is None else float(close_profit_abs),
            float(close_profit_abs or 0.0)]
    for col, v in (("fee_close_cost", fee_close_cost), ("fee_close", fee_close),
                   ("funding_fees", funding_fees)):
        if v is not None:
            sets.append(f"{col} = ?")
            vals.append(float(v))
    vals.append(int(trade_id))
    sql = f"UPDATE trades SET {', '.join(sets)} WHERE id = ? AND is_open = 1;"
    return _mutate(lambda c: c.execute(sql, vals).rowcount > 0)


def load_open_trades():
    rows = _read(lambda c: c.execute(
        "SELECT * FROM trades WHERE is_open = 1 ORDER BY id;").fetchall())
    return [dict(r) for r in (rows or [])]


def max_trade_id():
    row = _read(lambda c: c.execute("SELECT COALESCE(MAX(id), 0) FROM trades;").fetchone())
    return int(row[0]) if row else 0


if __name__ == "__main__":
    ensure_db()
    print(f"spx_store: {DB_PATH}")
    print(f"spx_store: {len(load_open_trades())} open, max id {max_trade_id()}")
