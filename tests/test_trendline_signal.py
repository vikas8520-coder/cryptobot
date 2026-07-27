"""trendline_signal encodes Tori's validity filters, so the tests are mostly
counter-examples: a 2-touch line, a bunched-touch line, a 1-week line and a steep line
must all be REFUSED, because that refusal is the whole edge. Plus the audit fix —
fitting on bars [0, n-2] so a BREAK is reachable exactly once, on the breaking close.

Price series are synthesised (an exact line plus dips onto it), so the expected
geometry is known; the exchange is a fake that replays them.
"""
import pytest

import trendline_signal as ts


class FakeExchange:
    def __init__(self, ohlcv):
        self.ohlcv = ohlcv

    def fetch_ohlcv(self, symbol, timeframe="1d", limit=None):
        return self.ohlcv


def rising_support(n=120, start=100.0, slope=0.5, touch_at=(10, 40, 70, 100), dip=0.0):
    """Closes well above a rising floor, with lows sitting exactly ON the floor at
    `touch_at` (and `dip` below it, to simulate a wick through the line)."""
    rows = []
    for i in range(n):
        line = start + slope * i
        low = line - dip if i in touch_at else line * 1.05
        high = line * 1.20
        close = line * 1.10
        rows.append([1700000000000 + i * 86400000, close, high, low, close, 1.0])
    return rows


# --------------------------------------------------------------- bars_per_week ----

@pytest.mark.parametrize("tf,expected", [("1d", 7), ("4h", 42), ("1h", 168), ("15m", 672)])
def test_bars_per_week(tf, expected):
    assert ts.bars_per_week(tf) == expected


# ------------------------------------------------------------------ find_pivots ----

def test_find_pivots_finds_the_local_low():
    series = [10, 9, 8, 5, 8, 9, 10]
    assert ts.find_pivots(series, 3, "low") == [(3, 5)]


def test_find_pivots_finds_the_local_high():
    series = [1, 2, 3, 9, 3, 2, 1]
    assert ts.find_pivots(series, 3, "high") == [(3, 9)]


def test_find_pivots_ignores_the_unconfirmed_edges():
    """A pivot needs `window` bars either side — the newest bars can't qualify yet."""
    series = [1, 0, 1, 2, 3, 4, 5]
    assert ts.find_pivots(series, 3, "low") == []


def test_find_pivots_collapses_a_flat_plateau_to_its_first_bar():
    series = [5, 4, 3, 1, 1, 1, 3, 4, 5]
    assert ts.find_pivots(series, 3, "low") == [(3, 1)]


def test_find_pivots_keeps_distinct_lows_far_apart():
    series = [5, 4, 3, 1, 3, 4, 5, 4, 3, 1, 3, 4, 5]
    assert [i for i, _ in ts.find_pivots(series, 3, "low")] == [3, 9]


# --------------------------------------------------------------------- fmt/_mk ----

@pytest.mark.parametrize("v,expected", [
    (54321.6, "$54,322"), (999.5, "$999.50"), (0.4567, "$0.4567"), (1.0, "$1.00"),
])
def test_fmt(v, expected):
    assert ts.fmt(v) == expected


def test_mk_long_geometry_puts_the_stop_just_past_the_line():
    cand = {"kind": "support", "ts1": 1700000000000, "slope_norm": 0.001, "n_touches": 3}
    sig = ts._mk("BTC", "BOUNCE_LONG", 110.0, 100.0, cand, [], 0)
    assert sig["side"] == "long"
    assert sig["stop"] == pytest.approx(99.0)            # 1% BELOW the line
    assert sig["target"] == pytest.approx(110.0 + 2 * (110.0 - 99.0))
    assert sig["rr"] == ts.TARGET_R
    assert sig["risk_pct"] == pytest.approx(11 / 110 * 100)
    assert sig["line_id"] == "support:1700000000000:0.001"


def test_mk_short_geometry_mirrors_the_long_case():
    cand = {"kind": "resistance", "ts1": 1, "slope_norm": -0.002, "n_touches": 4}
    sig = ts._mk("BTC", "REJECT_SHORT", 90.0, 100.0, cand, [], 0)
    assert sig["side"] == "short"
    assert sig["stop"] == pytest.approx(101.0)           # 1% ABOVE the line
    assert sig["target"] == pytest.approx(90.0 - 2 * (101.0 - 90.0))


def test_mk_reports_zero_rr_when_the_stop_is_the_wrong_side():
    cand = {"kind": "support", "ts1": 1, "slope_norm": 0.0, "n_touches": 3}
    assert ts._mk("BTC", "BOUNCE_LONG", 90.0, 100.0, cand, [], 0)["rr"] == 0


# -------------------------------------------------------------------- messages ----

@pytest.mark.parametrize("kind", list(ts.HEAD))
def test_message_renders_every_signal_type(kind):
    cand = {"kind": "support", "ts1": 1, "slope_norm": 0.0, "n_touches": 3}
    msg = ts.message(ts._mk("BTC", kind, 110.0, 100.0, cand, [], 0))
    assert msg.startswith(ts.HEAD[kind].format(c="BTC"))
    assert "3-touch" in msg
    assert "entry ~" in msg and "stop  ~" in msg and "target~" in msg
    assert msg.endswith("— Trendline signal · not financial advice")


# ------------------------------------------------------------------- best_line ----

def line_from(rows, kind):
    series = [r[3] for r in rows] if kind == "support" else [r[2] for r in rows]
    piv = ts.find_pivots(series, ts.PIVOT_WINDOW, "low" if kind == "support" else "high")
    return ts.best_line(piv, series, kind, ts.bars_per_week("1d"), len(series))


def test_best_line_fits_a_valid_support():
    rows = rising_support()
    line = line_from(rows, "support")
    assert line is not None
    assert line["n_touches"] >= ts.MIN_TOUCHES
    assert line["slope"] == pytest.approx(0.5)
    assert line["line"](0) == pytest.approx(100.0)


def test_best_line_refuses_a_two_touch_line():
    line = line_from(rising_support(touch_at=(10, 70)), "support")
    assert line is None


def test_best_line_refuses_touches_that_are_bunched_together():
    """Three dips inside a few candles is one wiggle counted thrice, not three touches."""
    line = line_from(rising_support(touch_at=(40, 44, 48)), "support")
    assert line is None


def test_best_line_refuses_a_line_spanning_under_three_weeks():
    line = line_from(rising_support(touch_at=(40, 48, 56)), "support")   # 16 bars < 21
    assert line is None


def test_best_line_refuses_a_too_steep_line():
    line = line_from(rising_support(slope=20.0), "support")              # >3%/bar
    assert line is None


def test_best_line_refuses_a_line_price_has_broken():
    rows = rising_support()
    rows[80][3] = 1.0                       # a low far under the floor invalidates it
    assert line_from(rows, "support") is None


def test_best_line_tolerates_a_wick_within_the_violation_tolerance():
    rows = rising_support(dip=0.0)
    line_val = 100.0 + 0.5 * 40
    rows[40][3] = line_val * (1 - ts.VIOLATION_TOL_PCT / 2)
    assert line_from(rows, "support") is not None


def test_best_line_prefers_the_line_with_more_touches():
    few = line_from(rising_support(touch_at=(10, 40, 70)), "support")
    many = line_from(rising_support(touch_at=(10, 25, 40, 55, 70, 85)), "support")
    assert many["n_touches"] > few["n_touches"]


def test_best_line_returns_none_without_pivots():
    assert ts.best_line([], [1.0] * 50, "support", 7, 50) is None


def test_best_line_fits_a_falling_resistance():
    rows = []
    for i in range(120):
        line = 200.0 - 0.5 * i
        high = line if i in (10, 40, 70, 100) else line * 0.95
        rows.append([i, line * 0.9, high, line * 0.8, line * 0.9, 1.0])
    series = [r[2] for r in rows]
    piv = ts.find_pivots(series, ts.PIVOT_WINDOW, "high")
    res = ts.best_line(piv, series, "resistance", 7, len(series))
    assert res is not None and res["slope"] == pytest.approx(-0.5)


# -------------------------------------------------------------------- evaluate ----

def test_evaluate_needs_sixty_closed_candles():
    with pytest.raises(ValueError, match="only"):
        ts.evaluate("BTC", FakeExchange(rising_support(n=40)))


def test_evaluate_reports_the_support_line_without_a_signal():
    """Price mid-channel: a valid line exists, but there is nothing to act on."""
    r = ts.evaluate("BTC", FakeExchange(rising_support()))
    assert r["coin"] == "BTC"
    assert r["support"] is not None
    assert r["support"]["level_now"] == pytest.approx(100.0 + 0.5 * 118)
    assert r["signals"] == []


def test_evaluate_emits_a_bounce_when_the_low_tags_a_held_support():
    rows = rising_support()
    cur = len(rows) - 2                       # last CLOSED bar (the last row is forming)
    line_val = 100.0 + 0.5 * cur
    rows[cur][3] = line_val * (1 - ts.VIOLATION_TOL_PCT / 2)   # wick onto the line
    rows[cur][4] = line_val * 1.01                             # ...but closed above it
    sig, = ts.evaluate("BTC", FakeExchange(rows))["signals"]
    assert sig["type"] == "BOUNCE_LONG" and sig["side"] == "long"
    assert sig["stop"] < line_val < sig["entry"] < sig["target"]


def test_a_deep_stop_hunt_wick_is_not_a_bounce():
    """The wick that would invalidate the line tomorrow must not read as "held" today."""
    rows = rising_support()
    cur = len(rows) - 2
    line_val = 100.0 + 0.5 * cur
    rows[cur][3] = line_val * 0.90            # pierced far past the tolerance
    rows[cur][4] = line_val * 1.01
    assert ts.evaluate("BTC", FakeExchange(rows))["signals"] == []


def test_evaluate_emits_a_breakdown_when_the_close_is_through_support():
    rows = rising_support()
    cur = len(rows) - 2
    line_val = 100.0 + 0.5 * cur
    rows[cur][3] = line_val * 0.90
    rows[cur][4] = line_val * 0.95            # CLOSED below the line
    sig, = ts.evaluate("BTC", FakeExchange(rows))["signals"]
    assert sig["type"] == "BREAK_DOWN" and sig["side"] == "short"


def test_breaks_are_reachable_because_the_line_is_fitted_through_yesterday():
    """Audit fix: including today's bar in the fit would disqualify any line it breaks,
    making BREAK_* dead code. The support must survive the very bar that breaks it."""
    rows = rising_support()
    cur = len(rows) - 2
    rows[cur][3] = rows[cur][4] = 1.0         # today smashes through the floor
    r = ts.evaluate("BTC", FakeExchange(rows))
    assert r["support"] is not None
    assert [s["type"] for s in r["signals"]] == ["BREAK_DOWN"]


def falling_resistance(n=120, start=200.0, slope=-0.5, touch_at=(10, 40, 70, 100)):
    """Closes well under a falling ceiling, with highs sitting ON it at `touch_at`."""
    rows = []
    for i in range(n):
        line = start + slope * i
        high = line if i in touch_at else line * 0.95
        rows.append([1700000000000 + i * 86400000, line * 0.9, high, line * 0.85,
                     line * 0.9, 1.0])
    return rows


def test_evaluate_emits_a_rejection_when_price_stalls_at_resistance():
    rows = falling_resistance()
    cur = len(rows) - 2
    line_val = 200.0 - 0.5 * cur
    rows[cur][2] = line_val * (1 + ts.VIOLATION_TOL_PCT / 2)   # wick up to the ceiling
    rows[cur][1] = rows[cur][4] = line_val * 0.99              # ...but closed under it
    sig, = ts.evaluate("BTC", FakeExchange(rows))["signals"]
    assert sig["type"] == "REJECT_SHORT" and sig["side"] == "short"
    assert sig["entry"] < line_val < sig["stop"]


def test_evaluate_emits_a_breakout_when_the_close_clears_resistance():
    rows = falling_resistance()
    cur = len(rows) - 2
    line_val = 200.0 - 0.5 * cur
    rows[cur][2] = line_val * 1.10
    rows[cur][1] = rows[cur][4] = line_val * 1.05              # CLOSED above the line
    r = ts.evaluate("BTC", FakeExchange(rows))
    assert r["resistance"] is not None
    sig, = r["signals"]
    assert sig["type"] == "BREAK_UP" and sig["side"] == "long"


def test_a_wick_far_above_resistance_is_not_a_rejection():
    rows = falling_resistance()
    cur = len(rows) - 2
    line_val = 200.0 - 0.5 * cur
    rows[cur][2] = line_val * 1.10                             # pierced way past tolerance
    rows[cur][1] = rows[cur][4] = line_val * 0.99
    assert ts.evaluate("BTC", FakeExchange(rows))["signals"] == []


def test_best_line_refuses_a_resistance_price_has_already_broken():
    rows = falling_resistance()
    rows[80][2] = 10_000.0
    assert line_from(rows, "resistance") is None


def test_line_identity_survives_a_sliding_fetch_window():
    """The line_id is anchored on the exchange timestamp, so the same economic line
    keeps its id as older candles fall out of the window (no daily re-alerts)."""
    rows = rising_support(n=140)
    cur = len(rows) - 2
    line_val = 100.0 + 0.5 * cur
    rows[cur][3] = line_val * 0.90
    rows[cur][4] = line_val * 0.95
    first = ts.evaluate("BTC", FakeExchange(rows))["signals"][0]["line_id"]
    second = ts.evaluate("BTC", FakeExchange(rows[5:]))["signals"][0]["line_id"]
    assert first == second


# ------------------------------------------------------------------------ main ----

@pytest.fixture
def wired(tmp_path, monkeypatch):
    """main()/scan() with tmp state files, a fake exchange and a captured sender."""
    monkeypatch.setattr(ts, "STATE", str(tmp_path / "trendline_state.json"))
    monkeypatch.setattr(ts, "BOARD", str(tmp_path / "trendline_board.json"))
    monkeypatch.setattr(ts, "WATCHLIST", str(tmp_path / "watchlist.json"))
    sent = []
    monkeypatch.setattr(ts, "send_telegram", lambda chat, text: sent.append(text) or True)

    def run(rows_by_coin, coins=("BTC",)):
        (tmp_path / "watchlist.json").write_text('{"coins": %s}' % list(coins).__repr__()
                                                 .replace("'", '"'))

        class MultiCoin:
            def fetch_ohlcv(self, symbol, timeframe="1d", limit=None):
                return rows_by_coin[symbol.split("/")[0]]
        monkeypatch.setattr(ts, "pick_exchange", lambda: ("fake", MultiCoin()))
        ts.main()
    return run, sent, tmp_path


def breakdown_rows():
    rows = rising_support()
    cur = len(rows) - 2
    line_val = 100.0 + 0.5 * cur
    rows[cur][3] = line_val * 0.90
    rows[cur][4] = line_val * 0.95
    return rows


def test_main_first_run_baselines_without_alerting(wired):
    run, sent, tmp_path = wired
    run({"BTC": breakdown_rows()})
    assert sent == []
    import json
    assert json.load(open(ts.STATE))["BTC"]["type"] == "BREAK_DOWN"
    board = json.load(open(ts.BOARD))
    assert board["timeframe"] == "1d" and board["source"] == "fake"
    assert board["coins"][0]["signal"] == "BREAK_DOWN"
    assert board["coins"][0]["rr"] == ts.TARGET_R


def test_main_alerts_once_per_new_event(wired):
    run, sent, _ = wired
    rows = breakdown_rows()
    run({"BTC": rows})                       # baseline
    run({"BTC": rows})                       # same event -> stay quiet
    assert sent == []


def test_main_alerts_when_the_signal_changes(wired):
    run, sent, _ = wired
    run({"BTC": rising_support()})           # baseline: no setup
    run({"BTC": breakdown_rows()})
    assert len(sent) == 1 and "BREAKDOWN" in sent[0]


def test_main_records_no_setup_coins_as_none(wired):
    run, sent, _ = wired
    run({"BTC": rising_support()})
    import json
    assert json.load(open(ts.STATE))["BTC"] == {"type": "none", "line_id": ""}
    assert json.load(open(ts.BOARD))["coins"][0]["signal"] is None


def test_main_keeps_prior_state_for_a_coin_that_fails_to_fetch(wired):
    run, sent, _ = wired
    rows = breakdown_rows()
    run({"BTC": rows})
    run({"BTC": rising_support(n=30)})       # too few candles -> evaluate() raises
    import json
    assert json.load(open(ts.STATE))["BTC"]["type"] == "BREAK_DOWN"
    assert sent == []


def test_main_exits_without_an_exchange(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "STATE", str(tmp_path / "s.json"))
    monkeypatch.setattr(ts, "WATCHLIST", str(tmp_path / "w.json"))
    monkeypatch.setattr(ts, "pick_exchange", lambda: (None, None))
    with pytest.raises(SystemExit) as e:
        ts.main()
    assert e.value.code == 1


# ------------------------------------------------------------------------ scan ----

def test_scan_prints_a_row_per_coin(monkeypatch, capsys):
    rows = breakdown_rows()

    class MultiCoin:
        def fetch_ohlcv(self, symbol, timeframe="1d", limit=None):
            if symbol.startswith("BAD"):
                return rows[:30]             # too short -> skipped, not fatal
            return rows
    monkeypatch.setattr(ts, "pick_exchange", lambda: ("fake", MultiCoin()))
    ts.scan(["BTC", "BAD"])
    out = capsys.readouterr().out
    assert "BREAK_DOWN" in out
    assert "BAD     skip:" in out


def test_scan_exits_without_an_exchange(monkeypatch):
    monkeypatch.setattr(ts, "pick_exchange", lambda: (None, None))
    with pytest.raises(SystemExit) as e:
        ts.scan(["BTC"])
    assert e.value.code == 1
