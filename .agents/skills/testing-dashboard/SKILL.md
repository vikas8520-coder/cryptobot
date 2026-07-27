---
name: testing-dashboard
description: How to run and end-to-end test the cryptobot FastAPI dashboard (and telegram_bot cmd_* handlers) on a Linux box with fake Freqtrade bots, stub secrets and seeded state files.
---

# Testing the cryptobot dashboard off-macOS

The real stack is macOS/launchd + Freqtrade. On a Linux dev box there are no bots, no
trade DBs, and the two secret files are git-ignored and absent. Everything below is
stubbed; **never** use a real Telegram token and never start launchd jobs.

## 1. Stub secrets (git-ignored — delete when done, never commit)

```bash
cd <repo>
printf 'def api_pw(port):\n    return "pw"\n' > local_secrets.py
printf 'TG_TOKEN="123:FAKE"\nTG_CHAT="99"\n'   > telegram.conf
```

## 2. Fake Freqtrade REST API

Serve ports **8080-8084** (Spot/Futures/Braked Hold/Scalp/Day Trade) with basic auth
`freqtrader` / `pw` and endpoints `show_config`, `balance`, `profit`, `status`,
`whitelist`, `daily?timescale=N`, `pair_candles`, plus POST `stopentry`, `start`,
`forceexit`. Notes learned the hard way:

- `status` entries must include `trade_id` — `telegram_bot.cmd_status` KeyErrors without it.
- Make `pair_candles` return 422 unless `pair`, `timeframe=1d`, `limit=600` *and* the
  basic-auth header are correct; that turns the chart into a real assertion on
  `freqtrade_api.get(params=...)`.
- Give each port distinct balances/profits so a card cannot pass on hardcoded defaults.

## 3. Seed the git-ignored state files the dashboard reads

`brake_alert_state.json`, `macro_alert_state.json`, `trendline_board.json`,
`diversified_brake_board.json`, `guard_state.json`, `activity_feed.jsonl` (one JSON
object per line with `ts`/`source`/`text`), `equity_history.csv`
(`date,spot,futures,brakedhold,btc_hold,basket_hold`), `funding_monitor.log`
(`YYYY-mm-dd HH:MM | AVG <gross>% <net>%`). Remove them all afterwards.

## 4. Run

`python3 dashboard.py` → http://127.0.0.1:8090. For parity against `main`:
`git worktree add /tmp/main-wt origin/main`, copy the same stubs/state in, run a small
uvicorn wrapper on :8091, then diff parsed `/api/overview`. **Copy the state files again
right before diffing** — several panels embed a "last updated" timestamp, so stale copies
produce spurious diffs.

## 5. Checks worth running

- Bot cards vs the fake API values; header combined equity / "N of 5 bots".
- Every cached panel; `/api/candles?asset=BTC` (600 candles + SMA) and
  `?asset=EVIL` (must be HTTP 400 `{"error":"unknown asset"}` — check the status with
  `curl -w`, the browser only shows the body).
- Kill the fake bots → cards must go OFFLINE, `/api/overview` still 200 and fast, no
  page-level error banner, cached panels untouched.
- Corrupt one state file, delete another → "not yet cached" placeholders, still 200.
  `state_io.load_json` logs `load_json(<file>) UNREADABLE: ...` for an *existing* corrupt
  file and stays silent for a missing one; those log lines are expected, not a failure.
- Offline unit suite: `pip install -r requirements-dev.txt && python3 -m pytest`.

## 6. telegram_bot handlers, headless

`main()` long-polls Telegram — never call it. Instead monkeypatch
`state_io.telegram_conf` to point at the repo's fake `telegram.conf` **before importing**
`telegram_bot` (it hardcodes `/Users/vikasreddy/...`), replace `telegram_bot.send`, then
call `cmd_status("")`, `cmd_profit("")`, `cmd_balance("")`,
`cmd_pause(["spot"])`, `cmd_resume(["spot"])`, `cmd_close(["spot", "41"])`.
Handlers take an args parameter (pass `""`), and bot-name commands want a **list**
(`["spot"]`; a bare string is parsed per-character → "Unknown bot 's'").
`cmd_macro` will report "no macro data" on Linux because of the hardcoded macOS paths —
monkeypatch those constants too if you need it. Re-run pause/close with the fake bots
killed to prove the `freqtrade_api.post` error contract surfaces `❌ … NOT paused`.

## 7. Cleanup

Kill the dashboards and fake bots, delete every seeded file plus `local_secrets.py` and
`telegram.conf`, `git worktree remove /tmp/main-wt`, and confirm `git status` is clean.
Note `/tmp` may be wiped between sessions — keep harness scripts under `/home/ubuntu`.

## Devin Secrets Needed

None. Use only the fake token `TG_TOKEN="123:FAKE"` / `TG_CHAT="99"` and the stub
`api_pw` password `pw`.
