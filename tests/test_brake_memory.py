"""brake_memory is the bots' track record — if it mis-pairs entries and exits the
history lies. Tests cover the episode lifecycle, the missed-exit recovery path, and
corrupt/absent stores (which must degrade to "no history", never crash a daemon)."""
import json
import os

import pytest

import brake_memory


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Never write the repo's real journal/episodes files."""
    monkeypatch.setattr(brake_memory, "JOURNAL", str(tmp_path / "journal.jsonl"))
    monkeypatch.setattr(brake_memory, "EPISODES", str(tmp_path / "episodes.json"))
    return tmp_path


def episodes():
    return json.load(open(brake_memory.EPISODES))


def journal():
    return [json.loads(x) for x in open(brake_memory.JOURNAL).read().splitlines()]


# ------------------------------------------------------------------- helpers ----

def test_days_between_iso_timestamps():
    assert brake_memory._days("2026-01-01T00:00:00+00:00", "2026-01-11T00:00:00+00:00") == 10


def test_days_is_zero_on_garbage_timestamps():
    assert brake_memory._days("not-a-date", "2026-01-11T00:00:00+00:00") == 0


def test_load_returns_empty_shape_when_file_missing():
    assert brake_memory._load() == {"open": {}, "closed": []}


def test_load_survives_a_corrupt_episodes_file():
    """A half-written JSON (the failure state_io exists to prevent) must not crash."""
    open(brake_memory.EPISODES, "w").write('{"open": {"BTC"')
    assert brake_memory._load() == {"open": {}, "closed": []}


# --------------------------------------------------------------- record_flip ----

def test_flip_above_opens_an_episode_and_journals_it():
    brake_memory.record_flip("BTC", "below", "above", 100.0, 90.0,
                             ts="2026-01-01T00:00:00+00:00", note="hi")
    e = episodes()
    assert e["open"]["BTC"] == {"entry_ts": "2026-01-01T00:00:00+00:00", "entry_price": 100.0}
    assert e["closed"] == []
    assert journal()[0] == {"ts": "2026-01-01T00:00:00+00:00", "coin": "BTC", "from": "below",
                            "to": "above", "price": 100.0, "sma": 90.0, "note": "hi"}


def test_flip_below_closes_the_episode_with_its_return():
    brake_memory.record_flip("BTC", "below", "above", 100.0, 90.0, ts="2026-01-01T00:00:00+00:00")
    brake_memory.record_flip("BTC", "above", "below", 120.0, 118.0, ts="2026-01-11T00:00:00+00:00")
    e = episodes()
    assert e["open"] == {}
    (c,) = e["closed"]
    assert c["coin"] == "BTC" and c["entry_price"] == 100.0 and c["exit_price"] == 120.0
    assert c["ret_pct"] == pytest.approx(20.0)
    assert c["days"] == 10
    assert "note" not in c
    assert len(journal()) == 2


def test_flip_below_without_an_open_episode_records_nothing():
    brake_memory.record_flip("BTC", "above", "below", 90.0, 100.0, ts="2026-01-01T00:00:00+00:00")
    assert episodes() == {"open": {}, "closed": []}
    assert len(journal()) == 1                       # the flip itself is still journalled


def test_double_above_closes_the_stale_episode_as_missed_exit():
    """A missed 'below' flip (crash/corrupt state) must not silently clobber history."""
    brake_memory.record_flip("BTC", "below", "above", 100.0, 90.0, ts="2026-01-01T00:00:00+00:00")
    brake_memory.record_flip("BTC", "below", "above", 150.0, 140.0, ts="2026-02-01T00:00:00+00:00")
    e = episodes()
    (c,) = e["closed"]
    assert c["note"] == "missed exit"
    assert c["ret_pct"] == pytest.approx(50.0)
    assert e["open"]["BTC"]["entry_price"] == 150.0   # the new hold is open


def test_episodes_are_tracked_per_coin():
    brake_memory.record_flip("BTC", "below", "above", 100.0, 90.0, ts="2026-01-01T00:00:00+00:00")
    brake_memory.record_flip("ETH", "below", "above", 10.0, 9.0, ts="2026-01-02T00:00:00+00:00")
    brake_memory.record_flip("BTC", "above", "below", 80.0, 95.0, ts="2026-01-20T00:00:00+00:00")
    e = episodes()
    assert list(e["open"]) == ["ETH"]
    assert e["closed"][0]["ret_pct"] == pytest.approx(-20.0)


def test_record_flip_defaults_ts_to_now():
    brake_memory.record_flip("BTC", "below", "above", 100.0, 90.0)
    assert episodes()["open"]["BTC"]["entry_ts"].endswith("+00:00")


# ------------------------------------------------------------------ seed_open ----

def test_seed_open_backfills_a_hold_without_journalling_it():
    brake_memory.seed_open("BTC", "2025-12-01T00:00:00+00:00", 50.0)
    assert episodes()["open"]["BTC"]["entry_price"] == 50.0
    assert not os.path.exists(brake_memory.JOURNAL)        # a backfill is not a flip


def test_seed_open_never_overwrites_a_live_episode():
    brake_memory.record_flip("BTC", "below", "above", 100.0, 90.0, ts="2026-01-01T00:00:00+00:00")
    brake_memory.seed_open("BTC", "2020-01-01T00:00:00+00:00", 1.0)
    assert episodes()["open"]["BTC"]["entry_price"] == 100.0


# -------------------------------------------------------------------- summary ----

def test_summary_with_no_history():
    out = brake_memory.summary()
    assert "Completed holds: none yet" in out
    assert "Currently holding" not in out


def test_summary_scores_completed_holds():
    brake_memory.record_flip("BTC", "below", "above", 100.0, 90.0, ts="2026-01-01T00:00:00+00:00")
    brake_memory.record_flip("BTC", "above", "below", 120.0, 118.0, ts="2026-01-11T00:00:00+00:00")
    brake_memory.record_flip("ETH", "below", "above", 100.0, 90.0, ts="2026-01-01T00:00:00+00:00")
    brake_memory.record_flip("ETH", "above", "below", 90.0, 95.0, ts="2026-01-21T00:00:00+00:00")
    out = brake_memory.summary()
    assert "Completed holds: 2 | win rate 50%" in out
    assert "avg +5.0% over ~15d" in out
    assert "Best: BTC +20.0% | Worst: ETH -10.0%" in out


def test_summary_shows_open_holds_with_unrealized_pnl():
    brake_memory.record_flip("BTC", "below", "above", 100.0, 90.0, ts="2026-01-01T00:00:00+00:00")
    out = brake_memory.summary(current_prices={"BTC": 110.0})
    assert "Currently holding (1):" in out
    assert "BTC: since 2026-01-01" in out
    assert "(+10.0% unrealized)" in out


def test_summary_omits_unrealized_for_coins_without_a_price():
    brake_memory.record_flip("BTC", "below", "above", 100.0, 90.0, ts="2026-01-01T00:00:00+00:00")
    assert "unrealized" not in brake_memory.summary(current_prices={"ETH": 10.0})


# ---------------------------------------------------------------------- stats ----

def test_stats_is_all_none_when_empty():
    s = brake_memory.stats()
    assert s == {"completed": 0, "win_rate": None, "avg_ret": None, "avg_days": None,
                 "best": None, "worst": None, "open": []}


def test_stats_mirrors_summary_numbers():
    brake_memory.record_flip("BTC", "below", "above", 100.0, 90.0, ts="2026-01-01T00:00:00+00:00")
    brake_memory.record_flip("BTC", "above", "below", 120.0, 118.0, ts="2026-01-11T00:00:00+00:00")
    brake_memory.record_flip("ETH", "below", "above", 100.0, 90.0, ts="2026-01-01T00:00:00+00:00")
    s = brake_memory.stats(current_prices={"ETH": 105.0})
    assert s["completed"] == 1
    assert s["win_rate"] == 100.0
    assert s["avg_ret"] == pytest.approx(20.0)
    assert s["avg_days"] == 10
    assert s["best"]["coin"] == s["worst"]["coin"] == "BTC"
    (op,) = s["open"]
    assert op["coin"] == "ETH" and op["since"] == "2026-01-01"
    assert op["unreal"] == pytest.approx(5.0)


def test_stats_unrealized_is_none_without_a_current_price():
    brake_memory.record_flip("BTC", "below", "above", 100.0, 90.0, ts="2026-01-01T00:00:00+00:00")
    assert brake_memory.stats()["open"][0]["unreal"] is None
