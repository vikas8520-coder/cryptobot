"""risk_lib is pure math on price history, so the tests feed it *constructed* series
(perfectly correlated, perfectly anti-correlated, known volatility) where the right
answer is known analytically — no market data, no feather files, no network.

Series are injected through the module's own close cache, which is the same object
_load_close() consults, so every public function is exercised end to end.
"""
import numpy as np
import pandas as pd
import pytest

import risk_lib


@pytest.fixture(autouse=True)
def clean_cache():
    risk_lib._CLOSE_CACHE.clear()
    yield
    risk_lib._CLOSE_CACHE.clear()


def hourly_series(values, start="2026-01-01"):
    idx = pd.date_range(start, periods=len(values), freq="1h")
    return pd.Series([float(v) for v in values], index=idx)


def inject(coin, series):
    risk_lib._CLOSE_CACHE[coin.upper()] = series


def walk(n, step, start=100.0):
    """Deterministic alternating walk: returns flip sign every bar."""
    px, v = [start], start
    for i in range(n - 1):
        v *= (1 + step) if i % 2 == 0 else (1 - step)
        px.append(v)
    return px


# ---------------------------------------------------------------------- coin_of ----

@pytest.mark.parametrize("pair,coin", [
    ("BTC/USDT", "BTC"), ("BTC/USDT:USDT", "BTC"), ("eth/usdt", "ETH"), ("SOL", "SOL"),
])
def test_coin_of(pair, coin):
    assert risk_lib.coin_of(pair) == coin


# ------------------------------------------------------------------- data layer ----

def test_load_close_caches_the_miss_for_an_unknown_coin(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_lib, "DATA_DIR", str(tmp_path))
    assert risk_lib._load_close("NOPE") is None
    assert risk_lib._CLOSE_CACHE["NOPE"] is None        # negative result is cached too


def test_load_close_reads_a_feather_and_caches_it(tmp_path, monkeypatch):
    pytest.importorskip("pyarrow")
    monkeypatch.setattr(risk_lib, "DATA_DIR", str(tmp_path))
    s = hourly_series([100, 101, 102])
    pd.DataFrame({"date": s.index, "close": s.values}).to_feather(
        tmp_path / "BTC_USDT-1h.feather")
    loaded = risk_lib._load_close("btc")                # case-insensitive
    assert loaded.tolist() == [100.0, 101.0, 102.0]
    (tmp_path / "BTC_USDT-1h.feather").unlink()
    assert risk_lib._load_close("BTC") is loaded        # served from cache after deletion


def test_available_coins_lists_feather_files(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_lib, "DATA_DIR", str(tmp_path))
    for name in ("SOL_USDT-1h.feather", "BTC_USDT-1h.feather", "ETH_USDT-4h.feather"):
        (tmp_path / name).touch()
    assert risk_lib.available_coins() == ["BTC", "SOL"]   # sorted, 1h only


def test_hourly_returns_is_empty_without_data(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_lib, "DATA_DIR", str(tmp_path))
    assert risk_lib.hourly_returns(["NOPE"]).empty


def test_hourly_returns_aligns_and_drops_the_first_bar():
    inject("BTC", hourly_series([100, 110, 121]))
    inject("ETH", hourly_series([10, 11, 12.1]))
    r = risk_lib.hourly_returns(["BTC", "ETH"])
    assert list(r.columns) == ["BTC", "ETH"]
    assert len(r) == 2
    assert r["BTC"].tolist() == pytest.approx([0.1, 0.1])


def test_hourly_returns_lookback_window_trims_to_the_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_lib, "DATA_DIR", str(tmp_path))
    inject("BTC", hourly_series(walk(500, 0.01)))
    r = risk_lib.hourly_returns(["BTC"], lookback_days=2)
    assert len(r) == 2 * risk_lib.HOURS_PER_DAY - 1       # 48 closes -> 47 returns


def test_hourly_returns_skips_coins_without_data(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_lib, "DATA_DIR", str(tmp_path))
    inject("BTC", hourly_series([100, 101, 102]))
    assert list(risk_lib.hourly_returns(["BTC", "NOPE"]).columns) == ["BTC"]


# --------------------------------------------------- correlation-aware exposure ----

def test_corr_matrix_of_identical_series_is_all_ones():
    inject("BTC", hourly_series(walk(300, 0.01)))
    inject("ETH", hourly_series([2 * p for p in walk(300, 0.01)]))
    C = risk_lib.corr_matrix(["BTC", "ETH"])
    assert C.loc["BTC", "ETH"] == pytest.approx(1.0)


def test_corr_matrix_is_empty_without_data(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_lib, "DATA_DIR", str(tmp_path))
    assert risk_lib.corr_matrix(["NOPE"]).empty


def test_exposure_of_no_positions_is_zero():
    assert risk_lib.corr_adjusted_exposure({}) == 0.0
    assert risk_lib.corr_adjusted_exposure({"BTC": 0}) == 0.0


def test_perfectly_correlated_positions_get_no_diversification_credit():
    """C = 1 everywhere -> E_eff == sum(stakes): a fat single bet, correctly priced."""
    base = walk(300, 0.01)
    inject("BTC", hourly_series(base))
    inject("ETH", hourly_series([3 * p for p in base]))
    assert risk_lib.corr_adjusted_exposure({"BTC": 100, "ETH": 100}) == pytest.approx(200.0, rel=1e-6)
    assert risk_lib.diversification_ratio({"BTC": 100, "ETH": 100}) == pytest.approx(1.0, rel=1e-6)


def test_perfectly_anticorrelated_positions_cancel_out():
    up = walk(300, 0.01)
    inject("BTC", hourly_series(up))
    inject("ETH", hourly_series([1 / p for p in up]))     # mirror image: corr ≈ -1
    eff = risk_lib.corr_adjusted_exposure({"BTC": 100, "ETH": 100})
    assert eff < 5.0
    assert risk_lib.diversification_ratio({"BTC": 100, "ETH": 100}) > 10


def test_uncorrelated_positions_shrink_exposure_towards_sqrt_sum_of_squares():
    rng = np.random.default_rng(7)
    n = 4000
    a = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    b = 100 * np.cumprod(1 + rng.normal(0, 0.01, n))
    inject("BTC", hourly_series(a))
    inject("ETH", hourly_series(b))
    eff = risk_lib.corr_adjusted_exposure({"BTC": 100, "ETH": 100})
    assert eff == pytest.approx(np.sqrt(2) * 100, rel=0.1)   # ~141, not 200


def test_unknown_coins_are_treated_as_perfectly_correlated(tmp_path, monkeypatch):
    """The conservative default for a risk cap: no data == no diversification credit."""
    monkeypatch.setattr(risk_lib, "DATA_DIR", str(tmp_path))
    assert risk_lib.corr_adjusted_exposure({"AAA": 100, "BBB": 100}) == pytest.approx(200.0)


def test_exposure_accepts_full_pair_strings():
    base = walk(300, 0.01)
    inject("BTC", hourly_series(base))
    assert risk_lib.corr_adjusted_exposure({"BTC/USDT:USDT": 100}) == pytest.approx(100.0)


def test_diversification_ratio_is_one_when_there_is_nothing_at_risk():
    assert risk_lib.diversification_ratio({}) == 1.0


# ------------------------------------------------------ volatility-scaled sizing ----

def test_daily_vol_is_none_for_an_unknown_coin(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_lib, "DATA_DIR", str(tmp_path))
    assert risk_lib.daily_vol("NOPE") is None


def test_daily_vol_is_none_with_too_few_days():
    inject("BTC", hourly_series(walk(48, 0.01)))          # 2 days -> < 5 daily returns
    assert risk_lib.daily_vol("BTC") is None


def test_daily_vol_is_none_for_a_flat_price():
    inject("BTC", hourly_series([100.0] * 24 * 40))
    assert risk_lib.daily_vol("BTC") is None


def test_daily_vol_matches_the_std_of_daily_returns():
    px = walk(24 * 40, 0.02)
    inject("BTC", hourly_series(px))
    s = hourly_series(px)
    expected = s.resample("1D").last().dropna().tail(31).pct_change().dropna().std()
    assert risk_lib.daily_vol("BTC", 30) == pytest.approx(float(expected))


def test_vol_scaled_stake_falls_back_to_the_midpoint_without_data(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_lib, "DATA_DIR", str(tmp_path))
    assert risk_lib.vol_scaled_stake("NOPE", 1000, floor=40, cap=180) == 110.0


def test_vol_scaled_stake_is_the_risk_budget_over_vol(monkeypatch):
    monkeypatch.setattr(risk_lib, "daily_vol", lambda coin, lb=30: 0.03)
    assert risk_lib.vol_scaled_stake("BTC", 1000) == pytest.approx(0.003 * 1000 / 0.03)


def test_calm_coins_get_more_and_wild_coins_less(monkeypatch):
    vols = {"CALM": 0.01, "WILD": 0.05}
    monkeypatch.setattr(risk_lib, "daily_vol", lambda coin, lb=30: vols[coin])
    assert risk_lib.vol_scaled_stake("CALM", 1000) > risk_lib.vol_scaled_stake("WILD", 1000)


def test_vol_scaled_stake_is_clamped_at_both_ends(monkeypatch):
    monkeypatch.setattr(risk_lib, "daily_vol", lambda coin, lb=30: 0.0001)
    assert risk_lib.vol_scaled_stake("CALM", 1000, cap=180) == 180.0
    monkeypatch.setattr(risk_lib, "daily_vol", lambda coin, lb=30: 5.0)
    assert risk_lib.vol_scaled_stake("WILD", 1000, floor=40) == 40.0


# ------------------------------------------------------- adaptive drawdown limit ----

def test_btc_vol_regime_is_one_without_data(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_lib, "DATA_DIR", str(tmp_path))
    assert risk_lib.btc_vol_regime() == 1.0


def test_btc_vol_regime_is_recent_over_baseline(monkeypatch):
    monkeypatch.setattr(risk_lib, "daily_vol",
                        lambda coin, lb: 0.06 if lb == 14 else 0.03)
    assert risk_lib.btc_vol_regime() == pytest.approx(2.0)


def test_adaptive_dd_limit_defaults_to_base_in_a_normal_regime(monkeypatch):
    monkeypatch.setattr(risk_lib, "btc_vol_regime", lambda lookback_days=14: 1.0)
    assert risk_lib.adaptive_dd_limit() == pytest.approx(0.10)


def test_adaptive_dd_limit_widens_in_a_violent_market(monkeypatch):
    monkeypatch.setattr(risk_lib, "btc_vol_regime", lambda lookback_days=14: 1.5)
    assert risk_lib.adaptive_dd_limit() == pytest.approx(0.15)


@pytest.mark.parametrize("regime,expected", [(0.1, 0.08), (99.0, 0.20)])
def test_adaptive_dd_limit_is_clamped(monkeypatch, regime, expected):
    monkeypatch.setattr(risk_lib, "btc_vol_regime", lambda lookback_days=14: regime)
    assert risk_lib.adaptive_dd_limit() == pytest.approx(expected)
