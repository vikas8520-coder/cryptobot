"""Offline tests for the CoinDCX adapter.

No network: every test either exercises pure logic or monkeypatches
`coindcx_api._get` with canned payloads captured from the live API on
2026-07-28. The cases here are the quirks that would silently corrupt a
backtest rather than raise — reversed candles, a resample that drops volume, a
fee read as a fraction when the API quotes percent, an order book depth that
returns HTTP 200 with nothing in it.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "user_data"))

import coindcx_api as api  # noqa: E402

HOUR = 3_600_000


# ---- SYMBOL MAPPING ----

@pytest.mark.parametrize("symbol,pair", [
    ("SOL/USDT:USDT", "B-SOL_USDT"),
    ("LINK/USDT:USDT", "B-LINK_USDT"),
    ("ADA/USDT:USDT", "B-ADA_USDT"),
])
def test_symbol_mapping_roundtrips(symbol, pair):
    assert api.ft_to_dcx(symbol) == pair
    assert api.dcx_to_ft(pair) == symbol


def test_dcx_to_ft_rejects_garbage():
    # Must return None, not raise: it runs over every pair the exchange lists,
    # including ones with ids we have never seen.
    assert api.dcx_to_ft("nonsense") is None


# ---- CANDLES ----

def _payload(times):
    """CoinDCX-shaped candle rows, NEWEST-FIRST like the real endpoint."""
    return [{"time": t, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
             "volume": 10.0} for t in sorted(times, reverse=True)]


def test_candles_are_reversed_to_ascending(monkeypatch):
    # The endpoint answers newest-first; anything downstream assumes ascending.
    times = [0, HOUR, 2 * HOUR]
    monkeypatch.setattr(api, "_get", lambda *a, **k: _payload(times))
    rows = api.candles("B-SOL_USDT", "1h")
    assert [r[0] for r in rows] == times


def test_candles_rejects_unsupported_interval(monkeypatch):
    # 4h is NOT a CoinDCX interval — this must fail closed rather than silently
    # return 1h data mislabelled as 4h.
    monkeypatch.setattr(api, "_get", lambda *a, **k: pytest.fail("must not call"))
    assert api.candles("B-SOL_USDT", "4h") == []


def test_candles_skips_malformed_rows_without_losing_the_page(monkeypatch):
    monkeypatch.setattr(api, "_get", lambda *a, **k: [
        {"time": HOUR, "open": 1, "high": 2, "low": 1, "close": 1, "volume": 1},
        {"time": 2 * HOUR, "open": None},  # truncated row
    ])
    assert [r[0] for r in api.candles("B-SOL_USDT", "1h")] == [HOUR]


def test_candles_clamps_limit_to_server_cap(monkeypatch):
    seen = {}
    monkeypatch.setattr(api, "_get", lambda url, params=None, **k: seen.update(params) or [])
    api.candles("B-SOL_USDT", "1h", limit=5000)
    assert seen["limit"] == api.MAX_LIMIT  # server 422s above 1000


def test_candles_range_dedups_overlapping_windows(monkeypatch):
    # Windows can repeat the seam candle; a duplicate timestamp would double-count
    # volume and break freqtrade's index assumptions.
    monkeypatch.setattr(api, "RATE_SLEEP", 0)
    monkeypatch.setattr(api, "candles", lambda p, i, s=None, e=None, limit=None: [
        [s, 1.0, 2.0, 0.5, 1.5, 10.0], [s + HOUR, 1.0, 2.0, 0.5, 1.5, 10.0],
    ])
    rows = api.candles_range("B-SOL_USDT", "1h", 0, 5 * HOUR)
    stamps = [r[0] for r in rows]
    assert len(stamps) == len(set(stamps))
    assert stamps == sorted(stamps)


# ---- RESAMPLE (the 4h informative SolRSI2_1h depends on) ----

def _series(n, start=0):
    """n hourly candles with distinct, checkable OHLCV."""
    return [[start + i * HOUR, 10.0 + i, 20.0 + i, 1.0 + i, 15.0 + i, 100.0]
            for i in range(n)]


def test_resample_1h_to_4h_aggregates_correctly():
    rows = _series(8)
    out = api.resample(rows, "1h", "4h")
    assert len(out) == 2
    ts, o, h, low, c, v = out[0]
    assert ts == 0
    assert o == rows[0][1]            # first open
    assert h == max(r[2] for r in rows[:4])
    assert low == min(r[3] for r in rows[:4])
    assert c == rows[3][4]            # last close
    assert v == pytest.approx(400.0)  # volume is summed, not averaged


def test_resample_drops_trailing_partial_bucket():
    # A half-formed 4h candle would let the strategy act on the future.
    out = api.resample(_series(6), "1h", "4h")
    assert [r[0] for r in out] == [0]


def test_resample_buckets_align_to_4h_utc_boundaries():
    # Must land on 00/04/08/... UTC like every real exchange, or the informative
    # merge silently offsets against the 1h series.
    out = api.resample(_series(8, start=12 * HOUR), "1h", "4h")
    assert all(r[0] % (4 * HOUR) == 0 for r in out)


def test_resample_rejects_non_multiple_timeframe():
    assert api.resample(_series(8), "1h", "15m") == []


# ---- ORDER BOOK ----

RAW_BOOK = {"ts": 1785288804928,
            "bids": {"73.60": "10", "73.58": "5", "73.59": "7"},
            "asks": {"73.62": "3", "73.61": "9"}}


def test_orderbook_dict_becomes_sorted_ccxt_lists(monkeypatch):
    monkeypatch.setattr(api, "_get", lambda *a, **k: RAW_BOOK)
    ob = api.orderbook("B-SOL_USDT", 10)
    assert ob["bids"] == [[73.60, 10.0], [73.59, 7.0], [73.58, 5.0]]  # descending
    assert ob["asks"] == [[73.61, 9.0], [73.62, 3.0]]                 # ascending
    assert ob["bids"][0][0] < ob["asks"][0][0]


@pytest.mark.parametrize("asked,used", [(1, 10), (5, 10), (10, 10), (30, 20), (99, 50), (1000, 50)])
def test_orderbook_snaps_to_supported_depth(monkeypatch, asked, used):
    # Depths 5 and 100 return HTTP 200 with an EMPTY book, which reads as a dead
    # market instead of an error — so unsupported depths must never be sent.
    seen = {}

    def fake(url, params=None, **k):
        seen["depth"] = int(url.rstrip("/").rsplit("/", 1)[-1])
        return RAW_BOOK

    monkeypatch.setattr(api, "_get", fake)
    api.orderbook("B-SOL_USDT", asked)
    assert seen["depth"] == used
    assert seen["depth"] in api.ORDERBOOK_DEPTHS


def test_orderbook_survives_failed_fetch(monkeypatch):
    monkeypatch.setattr(api, "_get", lambda *a, **k: None)
    assert api.orderbook("B-SOL_USDT") == {"bids": [], "asks": [], "timestamp": None}


# ---- DEFENSIVE I/O ----

@pytest.mark.parametrize("fn,expected", [
    (api.markets_details, []), (api.active_futures_pairs, []),
    (api.futures_realtime, {}), (api.spot_tickers, {}),
])
def test_readers_degrade_to_empty_on_failure(monkeypatch, fn, expected):
    monkeypatch.setattr(api, "_get", lambda *a, **k: None)
    assert fn() == expected


def test_readers_reject_wrong_shaped_payloads(monkeypatch):
    # A 200 carrying an error dict must not be mistaken for a market list.
    monkeypatch.setattr(api, "_get", lambda *a, **k: {"status": "error"})
    assert api.markets_details() == []
    assert api.active_futures_pairs() == []


def test_signed_post_without_credentials_is_a_noop(monkeypatch):
    # Guards the dry-run bot: no keys must mean no request, not an unsigned one.
    monkeypatch.setattr(api.requests, "post", lambda *a, **k: pytest.fail("must not post"))
    assert api.signed_post("/x", {}, "", "") is None


def test_signed_post_signs_the_exact_body_sent(monkeypatch):
    import hashlib
    import hmac
    captured = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured.update(body=data, headers=headers)
        return Resp()

    monkeypatch.setattr(api.requests, "post", fake_post)
    api.signed_post("/exchange/v1/x", {"pair": "B-SOL_USDT"}, "key", "secret")
    expected = hmac.new(b"secret", captured["body"].encode(), hashlib.sha256).hexdigest()
    assert captured["headers"]["X-AUTH-SIGNATURE"] == expected
    assert captured["headers"]["X-AUTH-APIKEY"] == "key"
    assert "timestamp" in captured["body"]


def test_inr_margin_is_what_orders_actually_request(monkeypatch):
    # The entire tax case for CoinDCX (no TDS, slab rate) rests on this field.
    # USDT margin here would be a silently different, worse-taxed product.
    captured = {}
    monkeypatch.setattr(api, "signed_post",
                        lambda path, body, k, s: captured.update(body) or {})
    api.create_futures_order("B-SOL_USDT", "buy", 1.0, "k", "s")
    assert captured["order"]["margin_currency_short_name"] == "INR"


# ---- MARKET PARSING (ccxt shim) ----

INSTRUMENT = {
    "status": "active", "max_leverage_long": 5.0, "price_increment": 0.01,
    "quantity_increment": 0.01, "min_quantity": 0.01, "max_quantity": 950000.0,
    "min_notional": 6.0, "max_notional": 13500000.0, "min_price": 0.441,
    "max_price": 736.0, "maker_fee": 0.0236, "taker_fee": 0.059,
    "unit_contract_value": 1.0, "is_inverse": False,
}


def test_parse_market_converts_percent_fees_to_fractions():
    # CoinDCX quotes 0.059 meaning 0.059%. Passing it through unscaled would
    # model a 5.9% taker fee and make every strategy look catastrophic.
    import coindcx_ft

    m = coindcx_ft.coindcx()._parse_market("B-SOL_USDT", "SOL/USDT:USDT", INSTRUMENT)
    assert m["taker"] == pytest.approx(0.00059)
    assert m["maker"] == pytest.approx(0.000236)


def test_parse_market_shape_matches_ccxt_swap_contract():
    import coindcx_ft

    m = coindcx_ft.coindcx()._parse_market("B-SOL_USDT", "SOL/USDT:USDT", INSTRUMENT)
    assert (m["swap"], m["contract"], m["linear"], m["spot"]) == (True, True, True, False)
    assert m["id"] == "B-SOL_USDT" and m["settle"] == "USDT"
    # TICK_SIZE precision mode: these are step sizes, not decimal counts.
    assert m["precision"] == {"price": 0.01, "amount": 0.01}
    assert m["limits"]["leverage"]["max"] == 5.0
    assert m["limits"]["cost"]["min"] == 6.0


def test_adapter_registers_itself_for_freqtrade():
    # freqtrade gates on `name in ccxt.exchanges` and then getattr()s it; both
    # halves must hold or the exchange cannot be constructed at all.
    import ccxt
    import ccxt.async_support as ccxt_async
    import freqtrade.exchange as fx

    import coindcx_ft

    assert "coindcx" in ccxt.exchanges
    assert getattr(ccxt, "coindcx", None) is coindcx_ft.coindcx
    assert getattr(ccxt_async, "coindcx", None) is coindcx_ft._CoindcxAsync
    assert fx.Coindcx is coindcx_ft.Coindcx


def test_adapter_does_not_advertise_data_coindcx_lacks():
    # Claiming these would make freqtrade request funding history and websockets
    # that do not exist, which fails at runtime instead of at config time.
    import coindcx_ft

    has = coindcx_ft.coindcx().has
    assert has["fetchFundingRateHistory"] is False
    assert has["watchOHLCV"] is False
    assert coindcx_ft.Coindcx._ft_has["ohlcv_candle_limit"] == api.MAX_LIMIT
    assert coindcx_ft.Coindcx._ft_has["l2_limit_range"] == list(api.ORDERBOOK_DEPTHS)
