"""brake_alerts is the product: silence between flips, one message when it matters.
The tests pin the behaviours that make that trustworthy — signal computed on the last
CLOSED candle, first run baselines quietly, flips queue to PENDING *before* the state
is committed (at-least-once delivery), and undelivered alerts survive to the next run.

No exchange and no Telegram are touched: a fake exchange returns canned OHLCV and the
sender is monkeypatched.
"""
import json
import os

import pytest

import brake_alerts
import brake_memory


class FakeExchange:
    """Minimal ccxt stand-in: returns [ts, o, h, l, c, v] rows per coin."""

    def __init__(self, closes_by_coin):
        self.closes_by_coin = closes_by_coin
        self.calls = []

    def fetch_ohlcv(self, symbol, timeframe="1d", limit=None):
        coin = symbol.split("/")[0]
        self.calls.append((coin, timeframe, limit))
        closes = self.closes_by_coin[coin]
        return [[1700000000000 + i * 86400000, c, c, c, c, 1.0] for i, c in enumerate(closes)]


def flat_then(price, tail, n=None):
    """200 closes at `price` followed by `tail` (last element = forming candle)."""
    n = n or brake_alerts.MA_LEN
    return [float(price)] * n + [float(t) for t in tail]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point every state file (brake's and brake_memory's) at tmp_path."""
    for mod, names in ((brake_alerts, ("BASE", "STATE", "PENDING", "WATCHLIST")),
                       (brake_memory, ("JOURNAL", "EPISODES"))):
        for name in names:
            leaf = "" if name == "BASE" else os.path.basename(getattr(mod, name))
            monkeypatch.setattr(mod, name, str(tmp_path / leaf) if leaf else str(tmp_path))
    return tmp_path


@pytest.fixture
def sent(monkeypatch):
    """Capture alerts instead of sending them; delivery result is controllable."""
    out = {"msgs": [], "deliver": True}
    monkeypatch.setattr(brake_alerts, "send_telegram",
                        lambda chat_id, text: (out["msgs"].append((chat_id, text)),
                                               out["deliver"])[1])
    return out


def state_file(tmp_path):
    return json.load(open(tmp_path / "brake_alert_state.json"))


def pending_file(tmp_path):
    return json.load(open(tmp_path / "brake_pending_alerts.json"))


# ------------------------------------------------------------------- helpers ----

@pytest.mark.parametrize("v,expected", [
    (54321.6, "$54,322"), (999.5, "$999.50"), (0.4567, "$0.457"), (1.0, "$1.00"),
])
def test_fmt_scales_precision_to_price(v, expected):
    assert brake_alerts.fmt(v) == expected


def test_load_json_returns_the_default_when_missing(tmp_path):
    assert brake_alerts.load_json(str(tmp_path / "nope.json"), {"a": 1}) == {"a": 1}


def test_load_json_returns_the_default_when_corrupt(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    assert brake_alerts.load_json(str(p), []) == []


def test_load_json_reads_a_good_file(tmp_path):
    p = tmp_path / "ok.json"
    p.write_text('{"coins": ["BTC"]}')
    assert brake_alerts.load_json(str(p), {}) == {"coins": ["BTC"]}


def test_log_prefixes_a_utc_timestamp(capsys):
    brake_alerts.log("hello")
    out = capsys.readouterr().out
    assert out.startswith("[") and "Z] hello" in out


def test_send_telegram_tags_the_activity_feed_and_reports_delivery(monkeypatch):
    calls = {}
    monkeypatch.setattr(brake_alerts, "verified_send",
                        lambda api, chat, text, feed_source=None: calls.update(
                            api=api, chat=chat, text=text, feed=feed_source) or False)
    assert brake_alerts.send_telegram("42", "hi") is False       # honest failure bool
    assert calls == {"api": brake_alerts.API, "chat": "42", "text": "hi", "feed": "brake"}


class StubCcxt:
    """Stands in for the ccxt module: exchange classes that may fail the probe."""

    def __init__(self, reachable):
        self.exchanges = ["binance", "okx", "kraken"]
        self.reachable = reachable
        self.probed = []

    def __getattr__(self, name):
        stub = self

        class Exchange:
            def __init__(self, opts):
                self.opts = opts

            def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
                stub.probed.append(name)
                if name not in stub.reachable:
                    raise ConnectionError(f"{name} geo-blocked")
                return [[0, 1, 1, 1, 1, 1]]
        return Exchange


def test_pick_exchange_returns_the_first_reachable_priority_venue(monkeypatch, capsys):
    stub = StubCcxt(reachable={"okx"})
    monkeypatch.setattr(brake_alerts, "ccxt", stub)
    monkeypatch.setattr(brake_alerts, "EXCHANGE_PRIORITY", ["nosuch", "binance", "okx"])
    name, ex = brake_alerts.pick_exchange()
    assert name == "okx" and ex is not None
    assert stub.probed == ["binance", "okx"]                     # India-first, then fallback
    assert "exchange binance unreachable" in capsys.readouterr().out


def test_pick_exchange_returns_none_when_everything_is_unreachable(monkeypatch):
    monkeypatch.setattr(brake_alerts, "ccxt", StubCcxt(reachable=set()))
    assert brake_alerts.pick_exchange() == (None, None)


def test_acquire_lock_is_exclusive(isolated, monkeypatch):
    held = brake_alerts.acquire_lock("t")
    assert held is not None
    with pytest.raises(SystemExit) as e:
        brake_alerts.acquire_lock("t")            # second run must bow out quietly
    assert e.value.code == 0
    held.close()
    brake_alerts.acquire_lock("t").close()        # released -> obtainable again


# --------------------------------------------------------------- brake_state ----

def test_brake_state_above_and_below_the_line():
    ex = FakeExchange({"BTC": flat_then(100, [120, 999]), "ETH": flat_then(100, [80, 1])})
    assert brake_alerts.brake_state(ex, "BTC")[0] == "above"
    assert brake_alerts.brake_state(ex, "ETH")[0] == "below"


def test_brake_state_ignores_the_still_forming_candle():
    """The last row is today's unfinished candle — a crash there must not flip us."""
    ex = FakeExchange({"BTC": flat_then(100, [120, 1])})
    state, price, sma = brake_alerts.brake_state(ex, "BTC")
    assert (state, price) == ("above", 120.0)     # 1.0 (forming) ignored
    assert sma == pytest.approx((100 * 199 + 120) / 200)


def test_brake_state_is_above_when_price_equals_the_line():
    ex = FakeExchange({"BTC": flat_then(100, [100, 100])})
    assert brake_alerts.brake_state(ex, "BTC")[0] == "above"


def test_brake_state_refuses_short_history():
    ex = FakeExchange({"BTC": [100.0] * 50})
    with pytest.raises(ValueError, match="need 200"):
        brake_alerts.brake_state(ex, "BTC")


def test_brake_state_requests_enough_candles_for_the_sma():
    ex = FakeExchange({"BTC": flat_then(100, [100, 100])})
    brake_alerts.brake_state(ex, "BTC")
    assert ex.calls[0] == ("BTC", "1d", brake_alerts.FETCH_LIMIT)
    assert brake_alerts.FETCH_LIMIT > brake_alerts.MA_LEN


# ---------------------------------------------------------------------- main ----

@pytest.fixture
def run_main(isolated, monkeypatch, sent):
    def run(closes_by_coin, coins=("BTC",)):
        (isolated / "brake_watchlist.json").write_text(json.dumps({"coins": list(coins)}))
        ex = FakeExchange(closes_by_coin)
        monkeypatch.setattr(brake_alerts, "pick_exchange", lambda: ("fake", ex))
        brake_alerts.main()
        return ex
    return run


def test_first_run_baselines_quietly_with_a_status_summary(run_main, isolated, sent):
    run_main({"BTC": flat_then(100, [120, 1])})
    assert state_file(isolated)["BTC"]["state"] == "above"
    (_, text), = sent["msgs"]
    assert "THE BRAKE — alert backend is live." in text
    assert "🟢 HOLD  BTC" in text
    assert not (isolated / "brake_pending_alerts.json").exists()   # no flip alerts


def test_no_flip_means_no_message(run_main, isolated, sent):
    run_main({"BTC": flat_then(100, [120, 1])})
    sent["msgs"].clear()
    run_main({"BTC": flat_then(100, [121, 1])})
    assert sent["msgs"] == []
    assert state_file(isolated)["BTC"]["state"] == "above"


def test_flip_to_below_sends_the_brake_on_alert(run_main, isolated, sent):
    run_main({"BTC": flat_then(100, [120, 1])})
    sent["msgs"].clear()
    run_main({"BTC": flat_then(100, [50, 1])})
    (_, text), = sent["msgs"]
    assert "🔴 BTC: BRAKE ON — go to cash" in text
    assert "closed BELOW its 200-day line" in text
    assert "not financial advice" in text
    assert state_file(isolated)["BTC"]["state"] == "below"


def test_flip_back_above_sends_the_brake_off_alert(run_main, sent):
    run_main({"BTC": flat_then(100, [50, 1])})
    run_main({"BTC": flat_then(100, [500, 1])})
    (_, text), = sent["msgs"][1:]
    assert "🟢 BTC: BRAKE OFF — safe to hold" in text


def test_since_timestamp_only_moves_on_a_flip(run_main, isolated):
    run_main({"BTC": flat_then(100, [120, 1])})
    since = state_file(isolated)["BTC"]["since"]
    run_main({"BTC": flat_then(100, [130, 1])})
    assert state_file(isolated)["BTC"]["since"] == since
    run_main({"BTC": flat_then(100, [50, 1])})
    assert state_file(isolated)["BTC"]["since"] != since


def test_a_flip_is_recorded_in_brake_memory(run_main, isolated):
    run_main({"BTC": flat_then(100, [120, 1])})
    run_main({"BTC": flat_then(100, [50, 1])})
    episodes = json.load(open(brake_memory.EPISODES))
    assert episodes["open"] == {} or "BTC" not in episodes["open"]
    journal = open(brake_memory.JOURNAL).read()
    assert '"from": "above"' in journal and '"to": "below"' in journal


def test_a_broken_memory_write_still_lets_the_alert_out(run_main, monkeypatch, sent):
    run_main({"BTC": flat_then(100, [120, 1])})
    monkeypatch.setattr(brake_memory, "record_flip",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    sent["msgs"].clear()
    run_main({"BTC": flat_then(100, [50, 1])})
    assert "BRAKE ON" in sent["msgs"][0][1]


def test_undelivered_alerts_stay_pending_for_the_next_run(run_main, isolated, sent):
    run_main({"BTC": flat_then(100, [120, 1])})
    sent["deliver"] = False
    run_main({"BTC": flat_then(100, [50, 1])})
    (queued,) = pending_file(isolated)
    assert "BRAKE ON" in queued["text"]

    sent["deliver"] = True
    sent["msgs"].clear()
    run_main({"BTC": flat_then(100, [50, 1])})     # no new flip, but the retry lands
    assert "BRAKE ON" in sent["msgs"][0][1]
    assert pending_file(isolated) == []


def test_pending_is_written_before_the_state_is_committed(run_main, isolated, monkeypatch):
    """Crash-safe ordering: dying between the two writes must lose the alert, not the flip."""
    run_main({"BTC": flat_then(100, [120, 1])})
    order = []
    real_save = brake_alerts.save_json

    def spy(path, obj, indent=None):
        order.append(os.path.basename(path))
        return real_save(path, obj, indent=indent)
    monkeypatch.setattr(brake_alerts, "save_json", spy)
    run_main({"BTC": flat_then(100, [50, 1])})
    assert order.index("brake_pending_alerts.json") < order.index("brake_alert_state.json")


def test_an_unfetchable_coin_keeps_its_previous_state(run_main, isolated, sent):
    run_main({"BTC": flat_then(100, [120, 1]), "ETH": flat_then(10, [12, 1])},
             coins=("BTC", "ETH"))
    before = state_file(isolated)["ETH"]
    run_main({"BTC": flat_then(100, [130, 1]), "ETH": [1.0] * 10},   # ETH history too short
             coins=("BTC", "ETH"))
    assert state_file(isolated)["ETH"] == before
    assert sent["msgs"][1:] == []


def test_watchlist_drives_which_coins_are_checked(run_main, isolated):
    ex = run_main({"BTC": flat_then(100, [120, 1]), "ETH": flat_then(10, [12, 1])},
                  coins=("BTC", "ETH"))
    assert {c for c, _, _ in ex.calls} == {"BTC", "ETH"}
    assert set(state_file(isolated)) == {"BTC", "ETH"}


def test_main_exits_when_no_exchange_is_reachable(isolated, monkeypatch):
    monkeypatch.setattr(brake_alerts, "pick_exchange", lambda: (None, None))
    with pytest.raises(SystemExit) as e:
        brake_alerts.main()
    assert e.value.code == 1


def test_subscriber_coin_filter_suppresses_unwanted_alerts(run_main, isolated, monkeypatch, sent):
    run_main({"BTC": flat_then(100, [120, 1])})
    monkeypatch.setattr(brake_alerts, "SUBSCRIBERS",
                        [{"channel": "telegram", "chat_id": "1", "coins": ["ETH"]}])
    sent["msgs"].clear()
    run_main({"BTC": flat_then(100, [50, 1])})
    assert sent["msgs"] == []
    assert pending_file(isolated) == []
