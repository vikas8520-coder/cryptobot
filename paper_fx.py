"""
[PAPER EQUITY] INR->USD conversion for the combined paper-equity headline.

WHY: the dashboard summed every bot's balance into one "Combined equity" number,
but 3 bots (Nifty/ONGC/ITC) hold INR while the rest hold USD — the sum added
rupees as if they were dollars, inflating the headline to ~$152k (+1284% P&L)
when the real combined paper equity is ~$8.9k. This module converts INR bots
to USD so the combined figure is honest.

DESIGN:
- Live USDINR rate cached to fx_state.json, refreshed at most every FX_CACHE_SEC
  (6h) so a dashboard refresh never does a blocking network call.
- Every yfinance read is try/except -> None; on any failure we fall back to the
  LAST good cached rate, then to FALLBACK_RATE (96.56, the rate the bots were
  sized at on 2026-07-22). The headline must never break if the FX feed is down.
- Single writer: only this module writes fx_state.json (atomic tmp+fsync+replace).
"""
import os, json, time, threading

FX_SYMBOL = "USDINR=X"          # Yahoo: 1 USD in INR
FALLBACK_RATE = 96.56           # bots sized at this on 2026-07-22; used if FX feed dead
FX_CACHE_SEC = 6 * 3600        # 6h — matches the macro job's cadence
FX_STATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fx_state.json")
_lock = threading.Lock()


def _read_cache():
    try:
        d = json.load(open(FX_STATE))
        return float(d.get("rate", 0)), float(d.get("ts", 0))
    except Exception:
        return 0.0, 0.0


def _write_cache(rate):
    tmp = FX_STATE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"rate": rate, "ts": time.time()}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, FX_STATE)
    except Exception:
        pass  # non-fatal: next refresh will retry


def get_usdinr(force=False):
    """Return a USDINR rate. Tries the cached value first (revalidated every
    FX_CACHE_SEC); on miss or forced, fetches live and caches. Any failure
    degrades to the last good cache, then FALLBACK_RATE."""
    rate, ts = _read_cache()
    if not force and rate > 0 and (time.time() - ts) < FX_CACHE_SEC:
        return rate
    try:
        import yfinance as yf
        df = yf.Ticker(FX_SYMBOL).history(period="5d", interval="1d",
                                          auto_adjust=True)
        live = float(df["Close"].iloc[-1]) if len(df) else 0.0
        if live > 0:
            with _lock:
                _write_cache(live)
            return live
    except Exception:
        pass  # keep going to cache/fallback
    # cache may hold an older-but-valid rate; prefer it over the constant
    if rate > 0:
        return rate
    return FALLBACK_RATE


def to_usd(balance, currency):
    """Convert a bot balance to USD. currency is 'USD' or 'INR' (anything
    else is treated as USD). INR is divided by USDINR."""
    if currency == "INR":
        return balance / get_usdinr()
    return float(balance)
