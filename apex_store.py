#!/usr/bin/env python3
"""
apex_store.py — the `tradesv3_apex.sqlite` writer. The ApeX bot's trade ledger.

The other 5 bots get this file for free: freqtrade's ORM owns their sqlite. ApeX is a
standalone process, so the ledger is ours to write — and it has to be written in
FREQTRADE'S DIALECT, because `stats_lib.py:36-73` is literal SQL against literal column
names (`is_open`, `close_profit_abs`, `fee_open_cost`, `funding_fees`, `is_short`, ...).
Match those and `/stats` (telegram_bot.py:211-212), `weekly_review.py:93,118` and
`futures_check` all light up from the one-line map edit in APEX_PLAN 1.3 — no consumer
learns that this ledger has a different author.

Rules, in priority order:

  1. COLUMN NAMES ARE THE CONTRACT. Every name and type below is copied from a live
     freqtrade 2026.6 `trades` table (checked against tradesv3_futures_ls2.sqlite), not
     invented. Dates are TEXT 'YYYY-MM-DD HH:MM:SS.ffffff' because stats_lib computes
     hold time with `julianday(close_date) - julianday(open_date)` — an epoch int or an
     ISO 'T' separator makes that silently return NULL, not an error.
  2. Booleans are stored as 0/1 INTEGERs. stats_lib compares `is_open = 0` and
     `is_short = 1`; Python `True` round-trips as 1 anyway, but never let a bool reach a
     numeric column by accident (the same bool-is-an-int trap as apex_api._f).
  3. A READER MUST NEVER SEE A HALF-WRITTEN LEDGER. stats_lib opens these files
     read-only while the bot is running, so every mutation happens on a private copy
     which is then os.replace()d into place — one atomic inode swap, the same
     tmp+fsync+rename discipline as state_io.save_json. A reader holding the old inode
     keeps a consistent old snapshot instead of a torn read.
  4. Journal mode stays DELETE, never WAL. WAL leaves `-wal`/`-shm` sidecars next to the
     db, and this module's whole atomicity story assumes the db is ONE self-contained
     file at the moment of the rename.

Failure classes this file is written against:
  - a schema drift that makes stats_lib return zeros instead of raising: the CREATE
    TABLE and the INSERT share one COLUMNS tuple, so a typo is an sqlite error at the
    first write, not a quietly empty scoreboard six weeks later.
  - a crash mid-write leaving an unreadable ledger (rule 3).
  - the engine and this module disagreeing about ids: open_trade() returns the id
    sqlite actually assigned, and the caller must use THAT, not a guess.
"""
import os
import shutil
import sqlite3
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "tradesv3_apex.sqlite")   # git-ignored by the *.sqlite rule
BUSY_TIMEOUT = 5           # seconds; matches stats_lib._conn's reader-side timeout

# Freqtrade's own column names/types, trimmed to what this bot can honestly fill in.
# NOT NULL is kept ONLY where open_trade() always supplies a value — a NOT NULL on a
# column we sometimes skip would turn a missing field into a write failure at 3am.
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

# Explicit INSERT column list — never `INSERT INTO trades VALUES (...)`. Positional
# inserts break the instant anyone adds a column; this list is the same one the CREATE
# above declares, in the same order, and is the ONLY place field order is asserted.
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

# Columns the caller may leave out entirely; these defaults keep the NOT NULLs satisfied
# and keep stats_lib's COALESCE()s honest (a NULL fee reads as "free trade").
DEFAULTS = {
    "exchange": "apex", "base_currency": "", "stake_currency": "USDC",
    "is_open": 1, "fee_open": 0.0, "fee_open_cost": 0.0, "fee_open_currency": None,
    "fee_close": 0.0, "fee_close_cost": None, "fee_close_currency": None,
    "open_trade_value": None, "close_rate": None, "realized_profit": 0.0,
    "close_profit": None, "close_profit_abs": None,
    "max_stake_amount": None, "amount_requested": None,
    "close_date": None, "exit_reason": None, "exit_order_status": None,
    "strategy": "ApexSynthetic", "enter_tag": None, "timeframe": 60,
    "trading_mode": "FUTURES", "contract_size": 1.0, "leverage": 1.0,
    "is_short": 0, "liquidation_price": None, "interest_rate": 0.0,
    "funding_fees": 0.0, "funding_fee_running": 0.0, "record_version": 2,
}


def fmt_dt(dt=None):
    """The exact string shape freqtrade writes ('2026-07-15 23:16:29.883659') — naive
    UTC, space separator, microseconds. julianday() in stats_lib parses this and nothing
    fancier, so every date in this module goes through here."""
    dt = dt or datetime.now(timezone.utc)
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")


def _b(v):
    """bool/whatever -> 0|1 int for a BOOLEAN column (rule 2)."""
    return 1 if v else 0


def _row(trade):
    """Project a trade dict onto COLUMNS, filling gaps from DEFAULTS. Unknown keys in the
    dict are IGNORED on purpose: the engine's in-memory trade carries the APEX_PLAN 1.2
    REST fields (trade_id, profit_ratio, open_timestamp, ...) alongside the sqlite ones,
    and one dict serving both surfaces beats two that can drift apart."""
    out = []
    for c in COLUMNS:
        v = trade.get(c, DEFAULTS.get(c))
        if c in ("is_open", "is_short"):
            v = _b(v)
        out.append(v)
    return out


# ---- ATOMIC WRITE BOUNDARY (rule 3) ----

def _mutate(fn):
    """Run fn(conn) against a PRIVATE COPY of the ledger, then swap it in with one
    os.replace. Readers (stats_lib, mode=ro) either see the whole old file or the whole
    new one — never a partial write, never a rolled-back journal.

    The copy costs a full file read per write, which is fine at this bot's write rate
    (a handful of rows an hour) and would not be at freqtrade's. If ApeX ever writes
    per-tick, this is the line to revisit — not the atomicity guarantee."""
    tmp = DB_PATH + ".tmp"
    try:
        if os.path.exists(DB_PATH):
            shutil.copy2(DB_PATH, tmp)
        elif os.path.exists(tmp):
            os.remove(tmp)          # stale tmp from a previous crash; never build on it
        conn = sqlite3.connect(tmp, timeout=BUSY_TIMEOUT)
        try:
            conn.execute("PRAGMA journal_mode=DELETE;")   # rule 4: no -wal/-shm sidecars
            conn.execute(CREATE_TABLE_SQL)
            result = fn(conn)
            conn.commit()
        finally:
            conn.close()
        # fsync the finished copy before the rename, or the swap can outlive the bytes.
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
    """Read-only access, the same way stats_lib does it — we are a guest even in our own
    ledger, because the file we open may be replaced under us at any moment."""
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=BUSY_TIMEOUT)
    conn.row_factory = sqlite3.Row
    try:
        return fn(conn)
    finally:
        conn.close()


# ---- public API ----

def ensure_db():
    """Create the ledger + table if absent. Idempotent; safe to call on every boot."""
    return _mutate(lambda c: True)


def open_trade(trade):
    """Insert one OPEN trade row. Returns the id sqlite assigned (== trade['id'] when the
    caller supplied one). The engine must adopt the returned id: it is what /forceexit
    and every downstream consumer will refer to."""
    row = dict(trade)
    row["is_open"] = 1
    row.setdefault("open_date", fmt_dt())
    # open_trade_value is what freqtrade charges the wallet: notional + entry fee.
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
    """Flip a trade to closed. Returns True if a row was actually updated.

    Every column stats_lib reads for a CLOSED trade is set here in ONE statement —
    is_open, close_rate, close_date, close_profit, close_profit_abs, fee_close_cost,
    exit_reason. A partial close (is_open=0 with a NULL close_profit_abs) would show up
    in /stats as a phantom break-even trade, which is worse than a loud failure."""
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
    """Every still-open row, as plain dicts. Boot reconciliation (APEX_PLAN 4.7): the
    engine restores these into EngineState before serving /status, because a KeepAlive
    restart that reported a flat book while positions were live is exactly the failure
    the guardian would act on."""
    rows = _read(lambda c: c.execute(
        "SELECT * FROM trades WHERE is_open = 1 ORDER BY id;").fetchall())
    return [dict(r) for r in (rows or [])]


def max_trade_id():
    """Highest id in the ledger, 0 if empty — the engine seeds its counter from this so
    ids never restart at 1 and collide with history."""
    row = _read(lambda c: c.execute("SELECT COALESCE(MAX(id), 0) FROM trades;").fetchone())
    return int(row[0]) if row else 0


if __name__ == "__main__":
    ensure_db()
    print(f"apex_store: {DB_PATH}")
    print(f"apex_store: {len(load_open_trades())} open, max id {max_trade_id()}")
