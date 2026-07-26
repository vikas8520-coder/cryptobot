"""stats_lib SELECTs straight against Freqtrade's trade store, so the tests build a
real (tiny) trades table with the same column names and check both the SQL aggregates
and the text the Telegram /stats command produces."""
import sqlite3

import pytest

import stats_lib

SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY,
    pair TEXT,
    is_open BOOLEAN,
    is_short BOOLEAN DEFAULT 0,
    open_date TEXT,
    close_date TEXT,
    close_profit FLOAT,
    close_profit_abs FLOAT,
    stake_amount FLOAT,
    exit_reason TEXT,
    fee_open_cost FLOAT,
    fee_close_cost FLOAT,
    funding_fees FLOAT
);
"""


def make_db(path, rows):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.executemany(
        "INSERT INTO trades (pair, is_open, is_short, open_date, close_date, close_profit,"
        " close_profit_abs, stake_amount, exit_reason, fee_open_cost, fee_close_cost,"
        " funding_fees) VALUES (:pair, :is_open, :is_short, :open_date, :close_date,"
        " :close_profit, :close_profit_abs, :stake_amount, :exit_reason, :fee_open,"
        " :fee_close, :funding)",
        [{**DEFAULT_ROW, **r} for r in rows])
    c.commit()
    c.close()


DEFAULT_ROW = {"pair": "BTC/USDT", "is_open": 0, "is_short": 0,
               "open_date": "2026-01-01 00:00:00", "close_date": "2026-01-02 00:00:00",
               "close_profit": 0.0, "close_profit_abs": 0.0, "stake_amount": 100.0,
               "exit_reason": "roi", "fee_open": 0.1, "fee_close": 0.1, "funding": 0.0}

CLOSED_TRADES = [
    {"pair": "BTC/USDT", "close_profit": 0.10, "close_profit_abs": 10.0, "exit_reason": "roi",
     "close_date": "2026-01-03 00:00:00"},                                   # +10, 2 days
    {"pair": "ETH/USDT", "close_profit": 0.02, "close_profit_abs": 2.0, "exit_reason": "roi"},
    {"pair": "SOL/USDT", "close_profit": -0.04, "close_profit_abs": -4.0,
     "exit_reason": "stop_loss", "is_short": 1},
]


@pytest.fixture
def bot_db(tmp_path, monkeypatch):
    """Point stats_lib at a temp dir holding a synthetic spot DB."""
    monkeypatch.setattr(stats_lib, "BASE", str(tmp_path))

    def build(rows, key="spot"):
        make_db(str(tmp_path / stats_lib.BOTS[key][1]), rows)
    return build


# ------------------------------------------------------------------ formatters ----

@pytest.mark.parametrize("hrs,expected", [
    (None, "—"), (0.5, "~30m"), (1.0, "~1.0h"), (23.9, "~23.9h"), (24, "~1.0d"), (60, "~2.5d"),
])
def test_fmt_dur(hrs, expected):
    assert stats_lib.fmt_dur(hrs) == expected


@pytest.mark.parametrize("v,expected", [(0, "+$0.00"), (5, "+$5.00"), (-16.021, "-$16.02")])
def test_money_puts_the_sign_outside_the_dollar(v, expected):
    assert stats_lib.money(v) == expected


@pytest.mark.parametrize("gw,gl,expected", [
    (10.0, 5.0, "2.00"), (0.0, 0.0, "—"), (7.0, 0.0, "∞"), (0.0, 3.0, "0.00"),
])
def test_profit_factor(gw, gl, expected):
    assert stats_lib._pf(gw, gl) == expected


# -------------------------------------------------------------------- bot_stats ----

def test_bot_stats_reports_missing_db(monkeypatch, tmp_path):
    monkeypatch.setattr(stats_lib, "BASE", str(tmp_path))
    d = stats_lib.bot_stats("spot")
    assert d["error"] == "db not found"
    assert d["label"] == stats_lib.BOTS["spot"][0]


def test_bot_stats_reports_a_broken_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(stats_lib, "BASE", str(tmp_path))
    sqlite3.connect(str(tmp_path / stats_lib.BOTS["spot"][1])).execute("CREATE TABLE x (a)")
    assert "query failed" in stats_lib.bot_stats("spot")["error"]


def test_bot_stats_on_an_empty_table(bot_db):
    bot_db([])
    d = stats_lib.bot_stats("spot")
    assert d["closed"] == 0 and d["wins"] == 0 and d["net_abs"] == 0.0
    assert d["avg_ratio"] is None and d["reasons"] == [] and d["pairs"] == []
    assert d["n_open"] == 0 and d["open_stake"] == 0.0


def test_bot_stats_aggregates_closed_trades(bot_db):
    bot_db(CLOSED_TRADES)
    d = stats_lib.bot_stats("spot")
    assert d["closed"] == 3
    assert d["wins"] == 2
    assert d["net_abs"] == pytest.approx(8.0)
    assert d["avg_ratio"] == pytest.approx(0.08 / 3)
    assert d["best"] == pytest.approx(0.10) and d["worst"] == pytest.approx(-0.04)
    assert d["avg_win"] == pytest.approx(0.06) and d["avg_loss"] == pytest.approx(-0.04)
    assert d["gross_win"] == pytest.approx(12.0) and d["gross_loss"] == pytest.approx(4.0)
    assert d["fees"] == pytest.approx(0.6)
    assert d["shorts"] == 1
    assert d["avg_hrs"] == pytest.approx((48 + 24 + 24) / 3)


def test_bot_stats_open_trades_are_excluded_from_the_score(bot_db):
    bot_db(CLOSED_TRADES + [{"is_open": 1, "stake_amount": 150.0, "close_profit_abs": None,
                             "close_date": None, "close_profit": None}])
    d = stats_lib.bot_stats("spot")
    assert d["closed"] == 3
    assert d["n_open"] == 1 and d["open_stake"] == pytest.approx(150.0)


def test_bot_stats_groups_by_exit_reason_worst_first(bot_db):
    bot_db(CLOSED_TRADES)
    reasons = stats_lib.bot_stats("spot")["reasons"]
    assert [r["reason"] for r in reasons] == ["stop_loss", "roi"]
    assert reasons[0]["net"] == pytest.approx(-4.0)
    assert reasons[1]["n"] == 2 and reasons[1]["wins"] == 2


def test_bot_stats_groups_by_pair_best_first(bot_db):
    bot_db(CLOSED_TRADES)
    pairs = stats_lib.bot_stats("spot")["pairs"]
    assert [p["pair"] for p in pairs] == ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
    assert pairs[0]["net"] == pytest.approx(10.0)


def test_bot_stats_does_not_write_to_the_bot_db(bot_db, tmp_path):
    """Read-only by construction: we are a guest in the live bot's database."""
    bot_db(CLOSED_TRADES)
    db = tmp_path / stats_lib.BOTS["spot"][1]
    before = db.stat().st_mtime_ns
    stats_lib.bot_stats("spot")
    assert db.stat().st_mtime_ns == before
    with pytest.raises(sqlite3.OperationalError):
        stats_lib._conn(stats_lib.BOTS["spot"][1]).execute("DELETE FROM trades")


# ----------------------------------------------------------------- summary_line ----

def test_summary_line_for_a_missing_db():
    assert stats_lib.summary_line({"label": "Spot", "error": "db not found"}) == \
        "• Spot: db not found"


def test_summary_line_no_trades_yet(bot_db):
    bot_db([])
    assert "0 closed · no trades yet" in stats_lib.summary_line(stats_lib.bot_stats("spot"))


def test_summary_line_in_cash_by_design(bot_db):
    bot_db([{"is_open": 1, "stake_amount": 120.0}])
    line = stats_lib.summary_line(stats_lib.bot_stats("spot"))
    assert "in cash by design (1 open, $120)" in line


def test_summary_line_scores_closed_trades(bot_db):
    bot_db(CLOSED_TRADES)
    line = stats_lib.summary_line(stats_lib.bot_stats("spot"))
    assert "3 closed · 67% win · +$8.00 · hold ~1.3d" in line


# ----------------------------------------------------------------------- detail ----

def test_detail_for_a_missing_db():
    assert stats_lib.detail({"label": "Spot", "error": "db not found"}) == "📊 Spot\ndb not found"


def test_detail_when_nothing_has_closed(bot_db):
    bot_db([{"is_open": 1, "stake_amount": 300.0}])
    out = stats_lib.detail(stats_lib.bot_stats("spot"))
    assert "Sitting in cash by design — 1 open position(s) ($300)" in out
    assert "That's the brake working, not a bug." in out


def test_detail_when_there_are_no_trades_at_all(bot_db):
    bot_db([])
    assert "No trades yet." in stats_lib.detail(stats_lib.bot_stats("spot"))


def test_detail_full_breakdown(bot_db):
    bot_db(CLOSED_TRADES + [{"is_open": 1, "stake_amount": 50.0}])
    out = stats_lib.detail(stats_lib.bot_stats("spot"))
    assert "Closed: 3 · Win rate: 67% (2W/1L) · 1 shorts" in out
    assert "Net P&L: +$8.00  ·  avg/trade +2.67%" in out
    assert "Avg win +6.00% · avg loss -4.00% · profit factor 3.00" in out
    assert "Best +10.0% · Worst -4.0% · hold ~1.3d" in out
    assert "Fee drag: +$8.60 gross → +$8.00 net · $0.60 fees (ate 5% of gross winnings)" in out
    assert "  stop_loss: 1× · 0%W · -$4.00 avg -4.0%" in out
    assert "  roi: 2× · 100%W · +$12.00 avg +6.0%" in out
    assert "🟢 BTC +$10.00 · ETH +$2.00" in out
    assert "🔴 SOL -$4.00" in out
    assert "1 still open ($50 staked)" in out
    assert "not financial advice" in out


def test_detail_mentions_funding_only_for_futures_trades(bot_db):
    bot_db([{"close_profit": 0.01, "close_profit_abs": 1.0, "funding": -0.25}])
    assert "funding $-0.25" in stats_lib.detail(stats_lib.bot_stats("spot"))


def test_detail_omits_funding_when_zero(bot_db):
    bot_db(CLOSED_TRADES)
    assert "funding" not in stats_lib.detail(stats_lib.bot_stats("spot"))


def test_detail_caps_the_coin_lists_at_three_each(bot_db):
    rows = [{"pair": f"C{i}/USDT", "close_profit": 0.01, "close_profit_abs": float(i)}
            for i in range(1, 6)]
    rows += [{"pair": f"L{i}/USDT", "close_profit": -0.01, "close_profit_abs": -float(i)}
             for i in range(1, 6)]
    bot_db(rows)
    out = stats_lib.detail(stats_lib.bot_stats("spot"))
    winners = [line for line in out.splitlines() if line.strip().startswith("🟢")][0]
    losers = [line for line in out.splitlines() if line.strip().startswith("🔴")][0]
    assert winners.count("+$") == 3 and "C5 +$5.00" in winners and "C2" not in winners
    assert losers.count("-$") == 3 and "L5 -$5.00" in losers and "L2" not in losers


# ------------------------------------------------------------ stats entry point ----

def test_stats_without_args_summarises_every_bot(bot_db):
    bot_db(CLOSED_TRADES)
    out = stats_lib.stats()
    assert out.startswith("📊 TRADE STATS (paper)")
    for label, _ in stats_lib.BOTS.values():
        assert label in out
    assert "3 closed · 67% win" in out                      # spot has data
    assert out.count("db not found") == len(stats_lib.BOTS) - 1


def test_stats_rejects_an_unknown_bot():
    assert stats_lib.stats(["nope"]).startswith("❌ Unknown bot")


@pytest.mark.parametrize("alias,key", [
    ("braked", "brakedhold"), ("hold", "brakedhold"), ("fut", "futures"),
    ("SPOT", "spot"), ("daytrade", "daytrade"),
])
def test_stats_resolves_aliases_and_case(alias, key, tmp_path, monkeypatch):
    monkeypatch.setattr(stats_lib, "BASE", str(tmp_path))
    assert stats_lib.BOTS[key][0] in stats_lib.stats([alias])


def test_stats_with_a_bot_key_returns_the_detail_view(bot_db):
    bot_db(CLOSED_TRADES)
    assert stats_lib.stats(["spot"]).startswith("📊 STATS · ")
