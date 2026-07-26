#!/usr/bin/env python3
"""
trendline_signal.py — Tori Trades' "valid trendline" filters, made codeable.

Tori's method is discretionary: a human eyeballs swing points and draws a line.
The VALUABLE half isn't the drawing — it's her rules for when a line is allowed to
be traded at all:

    • at least 3 clean touches            (not a 2-point fluke)
    • touches spaced ~6+ candles apart    (not the same wiggle counted twice)
    • drawn over 3+ weeks of price         (a real trend, not a day's noise)
    • gentle slope (< ~45°)                (steep lines snap constantly)
    • stop placed JUST PAST the line       (wick room, not right at it)
    • target at least 2R                   (make 2x what you risk)

This module fits those lines *deterministically* from swing pivots, applies every
filter, and only then emits a signal — a BOUNCE (price respected the line) or a
BREAK (a candle CLOSED through it). Everything is computed on CLOSED candles, so it
never acts on a still-forming bar. It is a signal aid, not execution and not advice.

Honest caveat: a trendline is inherently subjective, so no algorithm reproduces a
given human's exact line. What this DOES do is remove the two ways discretion lies to
you — cherry-picked lines and under-touched lines — by refusing any line that fails
the filters. That refusal is the edge worth keeping.

Reuses the brake infra (exchange picker, Telegram sender, state pattern) so it fits
the same India-first, alert-only philosophy as brake_alerts.py.
"""
import json
import os
import sys
from datetime import datetime, timezone

# reuse the proven helpers instead of duplicating them
from brake_alerts import (
    BASE, CHAT, SUBSCRIBERS, WATCHLIST, DEFAULT_WATCH,
    pick_exchange, send_telegram, log,
)
from state_io import load_json, save_json

STATE = os.path.join(BASE, "trendline_state.json")
BOARD = os.path.join(BASE, "trendline_board.json")   # rich cache the dashboard reads

# --- data ---
TIMEFRAME = "1d"                # Tori uses 4h; daily is calmer + tax-friendlier for you
FETCH_LIMIT = 400               # ~13 months of daily bars — plenty for multi-week lines

# --- pivot detection ---
PIVOT_WINDOW = 3                # a swing point is the extreme vs 3 bars on each side

# --- Tori's validity filters ---
MIN_TOUCHES = 3                 # >= 3 clean touches
MIN_TOUCH_SPACING = 6           # touches must be >= 6 bars apart
MIN_SPAN_WEEKS = 3              # first touch -> last touch spans >= 3 weeks
TOUCH_TOL_PCT = 0.015           # within 1.5% of the line counts as "touching" it
VIOLATION_TOL_PCT = 0.006       # price may poke through by <0.6% (wick noise) w/o breaking
MAX_SLOPE_PCT_PER_BAR = 0.030   # steeper than 3%/bar is our "> 45°, too steep" proxy

# --- trade geometry ---
STOP_BUFFER_PCT = 0.010         # stop sits 1% PAST the line (wick room, per Tori)
TARGET_R = 2.0                  # take-profit at 2R minimum


def bars_per_week(tf):
    """Crypto trades 7 days/week: daily = 7 bars/wk, 4h = 42, 1h = 168."""
    unit = tf[-1]
    n = int(tf[:-1])
    per_day = {"d": 1, "h": 24 / n, "m": 24 * 60 / n}[unit]
    return per_day * 7


def find_pivots(series, window, kind):
    """Swing pivots: local minima ('low') or maxima ('high'). Returns [(idx, price)]."""
    piv = []
    n = len(series)
    for i in range(window, n - window):
        seg = series[i - window:i + window + 1]
        v = series[i]
        if kind == "low" and v == min(seg):
            piv.append((i, v))
        elif kind == "high" and v == max(seg):
            piv.append((i, v))
    # collapse flat plateaus (equal-price pivots within one window) to the first
    # occurrence — tracked against the last RAW pivot so wide plateaus collapse too
    dedup = []
    last_idx = None
    for idx, pr in piv:
        if dedup and pr == dedup[-1][1] and last_idx is not None and idx - last_idx <= window:
            last_idx = idx
            continue
        dedup.append((idx, pr))
        last_idx = idx
    return dedup


def _line_fn(i1, y1, m):
    return lambda x: y1 + m * (x - i1)


def best_line(pivots, series, kind, bpw, n):
    """Fit the strongest VALID trendline of one kind.

    kind='support': line sits UNDER price, touching swing lows (uptrend floor).
    kind='resistance': line sits OVER price, touching swing highs (downtrend ceiling).

    Tries every pair of pivots as the line's anchor, keeps only lines that pass all of
    Tori's filters, and returns the one with the most touches (ties broken by the most
    recent last touch). Returns a dict or None.
    """
    best = None
    P = pivots
    for a in range(len(P)):
        for b in range(a + 1, len(P)):
            i1, y1 = P[a]
            i2, y2 = P[b]
            if i2 - i1 < MIN_TOUCH_SPACING:
                continue
            m = (y2 - y1) / (i2 - i1)
            if abs(m) / y1 > MAX_SLOPE_PCT_PER_BAR:        # slope / "> 45°" filter
                continue
            line = _line_fn(i1, y1, m)

            # the line must not be violated from its first anchor to now
            ok = True
            for x in range(i1, n):
                lv = line(x)
                if lv <= 0:
                    ok = False
                    break
                if kind == "support" and series[x] < lv * (1 - VIOLATION_TOL_PCT):
                    ok = False
                    break
                if kind == "resistance" and series[x] > lv * (1 + VIOLATION_TOL_PCT):
                    ok = False
                    break
            if not ok:
                continue

            # count touches among pivots that sit on the line
            touches = [(idx, pr) for (idx, pr) in P
                       if idx >= i1 and abs(pr - line(idx)) / line(idx) <= TOUCH_TOL_PCT]
            if len(touches) < MIN_TOUCHES:
                continue
            # enforce spacing between consecutive touches
            spaced = [touches[0]]
            for t in touches[1:]:
                if t[0] - spaced[-1][0] >= MIN_TOUCH_SPACING:
                    spaced.append(t)
            if len(spaced) < MIN_TOUCHES:
                continue
            span = spaced[-1][0] - spaced[0][0]
            if span < MIN_SPAN_WEEKS * bpw:                 # 3+ weeks filter
                continue

            cand = {"kind": kind, "i1": i1, "y1": y1, "slope": m, "line": line,
                    "touches": spaced, "n_touches": len(spaced),
                    "last_touch": spaced[-1][0], "first_touch": spaced[0][0]}
            if best is None or (cand["n_touches"], cand["last_touch"]) > \
                    (best["n_touches"], best["last_touch"]):
                best = cand
    return best


def evaluate(coin, ex):
    """Fetch, fit lines, and return the single most actionable signal for a coin.

    CRITICAL detail (audit fix): lines are fitted and validity-checked on bars
    [0, n-2] ONLY — yesterday and back. If the current closed bar were included,
    any line it breaks would be disqualified by the validity loop before the break
    test ran, making BREAK_UP/BREAK_DOWN mathematically unreachable (proven dead
    code in the 2026-07-19 audit). Fitting through yesterday and testing today
    against the extrapolated line makes breaks detectable exactly once, on the
    first closed bar through the line."""
    ohlcv = ex.fetch_ohlcv(f"{coin}/USDT", timeframe=TIMEFRAME, limit=FETCH_LIMIT)
    ohlcv = ohlcv[:-1]                      # drop the still-forming candle
    if len(ohlcv) < 60:
        raise ValueError(f"{coin}: only {len(ohlcv)} closed candles")
    highs = [c[2] for c in ohlcv]
    lows = [c[3] for c in ohlcv]
    closes = [c[4] for c in ohlcv]
    n = len(closes)
    fit_n = n - 1                           # fit/validate on bars [0, n-2]
    bpw = bars_per_week(TIMEFRAME)

    sup = best_line(find_pivots(lows[:fit_n], PIVOT_WINDOW, "low"),
                    lows[:fit_n], "support", bpw, fit_n)
    res = best_line(find_pivots(highs[:fit_n], PIVOT_WINDOW, "high"),
                    highs[:fit_n], "resistance", bpw, fit_n)

    cur = n - 1
    for cand in (sup, res):
        if cand:
            # window-invariant identity (exchange timestamp of the anchor candle +
            # price-normalized slope) so the same economic line keeps the same id
            # as the fetch window slides — no more daily re-alerts on old news
            cand["ts1"] = ohlcv[cand["i1"]][0]
            cand["slope_norm"] = round(cand["slope"] / cand["y1"], 5)
            cand["level_now"] = cand["line"](cur)   # today's line level, for display
    close_now, low_now, high_now = closes[cur], lows[cur], highs[cur]
    signals = []

    # Bounces/rejections must also respect the wick bound (verified refutation fix):
    # with the current bar excluded from fitting, a deep stop-hunt wick THROUGH the
    # line that closed back inside would otherwise read as "support held" — while the
    # very same wick invalidates that line on the next run. A true bounce's wick may
    # only pierce by the violation tolerance the line itself is held to.
    if sup:
        lv = sup["line"](cur)
        if close_now < lv * (1 - VIOLATION_TOL_PCT):
            signals.append(_mk(coin, "BREAK_DOWN", close_now, lv, sup, closes, cur))
        elif (low_now <= lv * (1 + TOUCH_TOL_PCT) and close_now >= lv
              and low_now >= lv * (1 - VIOLATION_TOL_PCT)):
            signals.append(_mk(coin, "BOUNCE_LONG", close_now, lv, sup, closes, cur))

    if res:
        lv = res["line"](cur)
        if close_now > lv * (1 + VIOLATION_TOL_PCT):
            signals.append(_mk(coin, "BREAK_UP", close_now, lv, res, closes, cur))
        elif (high_now >= lv * (1 - TOUCH_TOL_PCT) and close_now <= lv
              and high_now <= lv * (1 + VIOLATION_TOL_PCT)):
            signals.append(_mk(coin, "REJECT_SHORT", close_now, lv, res, closes, cur))

    # rank: confirmed breaks are the strongest events, then bounces/rejections
    order = {"BREAK_UP": 0, "BREAK_DOWN": 1, "BOUNCE_LONG": 2, "REJECT_SHORT": 3}
    signals.sort(key=lambda s: order[s["type"]])
    return {"coin": coin, "close": close_now, "support": sup, "resistance": res,
            "signals": signals}


def _mk(coin, kind, price, line_val, cand, closes, cur):
    """Attach entry / stop / 2R target geometry to a raw signal."""
    long_side = kind in ("BOUNCE_LONG", "BREAK_UP")
    if long_side:
        stop = line_val * (1 - STOP_BUFFER_PCT)
        risk = price - stop
        target = price + TARGET_R * risk
    else:
        stop = line_val * (1 + STOP_BUFFER_PCT)
        risk = stop - price
        target = price - TARGET_R * risk
    rr = TARGET_R if risk > 0 else 0
    return {"coin": coin, "type": kind, "side": "long" if long_side else "short",
            "entry": price, "stop": stop, "target": target, "risk_pct": abs(risk) / price * 100,
            "rr": rr, "line_val": line_val, "n_touches": cand["n_touches"],
            "line_id": f"{cand['kind']}:{cand['ts1']}:{cand['slope_norm']}"}


def fmt(v):
    if v >= 1000:
        return f"${v:,.0f}"
    return f"${v:,.4f}" if v < 1 else f"${v:,.2f}"


HEAD = {
    "BOUNCE_LONG":  "🟢 {c}: BOUNCE — support line held",
    "BREAK_UP":     "🚀 {c}: BREAKOUT — closed above resistance",
    "BREAK_DOWN":   "🔴 {c}: BREAKDOWN — closed below support",
    "REJECT_SHORT": "⚠️ {c}: REJECTED at resistance",
}
BODY = {
    "BOUNCE_LONG":  "Price came down to a valid {n}-touch support line and held ({e} vs line {l}). "
                    "Tori's bounce setup.",
    "BREAK_UP":     "Price closed ABOVE a valid {n}-touch resistance line ({e} > {l}) — possible trend change up.",
    "BREAK_DOWN":   "Price closed BELOW a valid {n}-touch support line ({e} < {l}) — the floor broke; step aside.",
    "REJECT_SHORT": "Price tagged a valid {n}-touch resistance line and stalled ({e} vs {l}) — not a place to buy.",
}


def message(sig):
    c = sig["coin"]
    head = HEAD[sig["type"]].format(c=c)
    body = BODY[sig["type"]].format(n=sig["n_touches"], e=fmt(sig["entry"]), l=fmt(sig["line_val"]))
    plan = (f"\n\nIf you take it ({sig['side'].upper()}):"
            f"\n  entry ~ {fmt(sig['entry'])}"
            f"\n  stop  ~ {fmt(sig['stop'])}  (just past the line · risking {sig['risk_pct']:.1f}%)"
            f"\n  target~ {fmt(sig['target'])}  ({TARGET_R:.0f}R)")
    return f"{head}\n\n{body}{plan}\n\n— Trendline signal · not financial advice"


def scan(coins):
    """Print a one-shot table for every coin — useful without waiting for alerts."""
    src, ex = pick_exchange()
    if ex is None:
        log("no reachable exchange")
        sys.exit(1)
    log(f"data source: {src} | timeframe: {TIMEFRAME}")
    print(f"\n{'COIN':6}{'CLOSE':>12}{'SUPPORT':>12}{'RESIST':>12}  SIGNAL")
    print("-" * 60)
    for coin in coins:
        try:
            r = evaluate(coin, ex)
        except Exception as e:
            print(f"{coin:6}  skip: {e}")
            continue
        s_val = fmt(r["support"]["level_now"]) if r["support"] else "—"
        r_val = fmt(r["resistance"]["level_now"]) if r["resistance"] else "—"
        sig = r["signals"][0]["type"] if r["signals"] else "(inside channel)"
        print(f"{coin:6}{fmt(r['close']):>12}{s_val:>12}{r_val:>12}  {sig}")
    print()


def main():
    """Alert loop: only speak when a NEW trendline event appears (state-deduped)."""
    coins = load_json(WATCHLIST, {"coins": DEFAULT_WATCH}).get("coins", DEFAULT_WATCH)
    state = load_json(STATE, {})            # {coin: {"type","line_id"}}
    first_run = not state

    src, ex = pick_exchange()
    if ex is None:
        log("no reachable exchange; aborting")
        sys.exit(1)
    log(f"data source: {src} | timeframe: {TIMEFRAME} | watching: {', '.join(coins)}")
    now_iso = datetime.now(timezone.utc).isoformat()

    new_state = {}
    board = []
    alerts = []
    for coin in coins:
        try:
            r = evaluate(coin, ex)
        except Exception as e:
            log(f"skip {coin}: {e}")
            if coin in state:
                new_state[coin] = state[coin]
            continue
        top = r["signals"][0] if r["signals"] else None
        # rich board row for the dashboard — line levels evaluated at TODAY's bar,
        # so the board always agrees with the alert prices from the same run
        sup_now = r["support"]["level_now"] if r["support"] else None
        res_now = r["resistance"]["level_now"] if r["resistance"] else None
        board.append({"coin": coin, "close": r["close"],
                      "support": sup_now, "resistance": res_now,
                      "signal": top["type"] if top else None,
                      "side": top["side"] if top else None,
                      "entry": top["entry"] if top else None,
                      "stop": top["stop"] if top else None,
                      "target": top["target"] if top else None,
                      "rr": top["rr"] if top else None,
                      "touches": top["n_touches"] if top else None})
        if top is None:
            new_state[coin] = {"type": "none", "line_id": ""}
            log(f"  {coin}: inside channel / no valid setup")
            continue
        key = {"type": top["type"], "line_id": top["line_id"]}
        new_state[coin] = key
        changed = state.get(coin, {}) != key
        log(f"  {coin}: {top['type']}  {fmt(top['entry'])} (line {fmt(top['line_val'])}, "
            f"{top['n_touches']} touches){'  *NEW*' if changed else ''}")
        if changed and not first_run:
            alerts.append(top)

    save_json(STATE, new_state, indent=2)         # atomic — see state_io.py
    save_json(BOARD, {"updated": now_iso, "source": src, "timeframe": TIMEFRAME,
                      "coins": board}, indent=2)

    if first_run:
        log("baseline established — will alert on new trendline events from next run")
        return
    for sig in alerts:
        msg = message(sig)
        for sub in SUBSCRIBERS:
            if sub["channel"] == "telegram":
                send_telegram(sub["chat_id"], msg)
        log(f"ALERT sent: {sig['coin']} {sig['type']}")
    if not alerts:
        log("no new trendline events — staying quiet")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--scan":
        coins = load_json(WATCHLIST, {"coins": DEFAULT_WATCH}).get("coins", DEFAULT_WATCH)
        scan(coins)
    else:
        main()
