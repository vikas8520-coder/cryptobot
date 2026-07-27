# APEX_PLAN.md

## STATUS

**PLAN ONLY — no code has been written.** Nothing in this repo implements any of the
below. No files were created, no dependencies installed, no launchd job loaded, no
Telegram sent.

**Decision: PATH A** — a custom standalone ApeX bot built on ApeX's native `apexomni`
Python SDK (github.com/ApeX-Protocol/apexpro-openapi, v3.4.0). ApeX is not a
Freqtrade/ccxt exchange, so it cannot be a `config_apex.json` + strategy like the other
5 bots. This decision is settled — the spec below is about *how* to build it so it plugs
into the existing ops layer, not *whether* to.

**Dependency-conflict caveat (supersedes the P0 install step below).** `apexomni` pulls
`web3` and pins `setuptools<81`. Installing it into the live `./.venv` risks
destabilizing the 5 **currently running** freqtrade bots, which sit on a pinned
`ccxt 4.5.65` environment. **P0 must use a throwaway venv** (e.g.
`python3 -m venv /tmp/apex-spike`), not `./.venv`. Only after the spike proves the
dependency set is compatible should any install touch the live environment — and even
then, snapshot `pip freeze` first and have a rollback path. The literal
`.venv/bin/pip install apexomni` line preserved in §3 P0 below is the original spec text
and is **overridden by this note**.

---

I read the real files end-to-end (dashboard, watchdog, guard, equity_logger, telegram_bot,
state_io, stats_lib, trade_notifier, the 5 configs, the plists, and freqtrade 2026.6's own
API schemas in `.venv` to pin exact response shapes). Here is the build spec.

---

# ApeX Omni bot — integration contract & build spec

## 0. Ground truth established

- **Freqtrade version the ops layer was written against:** `freqtrade 2026.6`
  (`.venv/bin/freqtrade --version`). Response shapes below are quoted from
  `.venv/lib/python3.12/site-packages/freqtrade/rpc/api_server/api_schemas.py`, not from
  memory.
- **Ports in use:** 8080 spot, 8081 futures, 8082 brakedhold, 8083 scalp, 8084 daytrade
  (`local_secrets.py:9-15`), 8090 dashboard (`dashboard.py:1473`). → **ApeX takes 8085.**
- **Auth is uniform:** every poller does `HTTPBasicAuth("freqtrader", api_pw(port))`
  against `http://127.0.0.1:{port}/api/v1/{ep}`. Username `"freqtrader"` is hard-coded in
  all 9 callers; password comes from `local_secrets.api_pw(port)` (no fallback
  hard-coded in callers).
- **All 5 bots are `dry_run: true`** — confirmed by grep: `config.json:8`,
  `config_futures.json:8`, `config_scalp.json:8`, `config_daytrade.json:8`,
  `config_braked.json:8`, each with `dry_run_wallet: 1000`. No `dry_run` key appears in any
  `*.secret.json` (those hold only `api_server.password` / `jwt_secret_key` / `ws_token`),
  so nothing overrides it.

---

## 1. INTEGRATION CONTRACT

### 1.1 Every endpoint the ops layer polls, per bot

| Method | Endpoint | Called by (file:line) | Fields actually consumed |
|---|---|---|---|
| GET | `/api/v1/ping` | `watchdog.py:96-99` | `status` must == `"pong"` |
| GET | `/api/v1/status` | `portfolio_guard.py:114`, `dashboard.py:319`, `telegram_bot.py:62`, `trade_notifier.py:60`, `sharp_move_alert.py:41`, `weekly_digest.py:36` | **must be a JSON array**; per-trade: `trade_id`, `pair`, `is_short`, `stake_amount`, `amount`, `profit_ratio`, `open_rate` |
| GET | `/api/v1/balance` | `portfolio_guard.py:115`, `dashboard.py:317`, `equity_logger.py:38-40`, `telegram_bot.py:89`, `weekly_digest.py:34`, `two_week_report.py:50` | `total` (must be real `int`/`float`) |
| GET | `/api/v1/profit` | `dashboard.py:318`, `telegram_bot.py:79`, `weekly_digest.py:33`, `two_week_report.py:49` | `profit_closed_percent`, `closed_trade_count`, `winrate` (ratio 0-1), `best_pair`, `best_pair_profit_ratio` |
| GET | `/api/v1/show_config` | `dashboard.py:316` | `state` (`"running"`/`"stopped"`/`"paused"`); truthiness also drives the `online` flag at `dashboard.py:321` |
| GET | `/api/v1/whitelist` | `dashboard.py:320` | `whitelist`: list of `"BASE/QUOTE"` strings (`dashboard.py:356` splits on `/`) |
| GET | `/api/v1/daily?timescale=30` | `dashboard.py:338`, `weekly_digest.py:35` (`timescale=7`) | `data`: list of `{starting_balance, abs_profit, trade_count}`, **most-recent-first** (dashboard reverses it at `dashboard.py:340`) |
| GET | `/api/v1/trades` | `trade_notifier.py:61` | `{"trades": [...]}`; per-trade `trade_id`, `is_open`, `pair`, `open_rate`, `close_rate`, `exit_reason`, `profit_ratio`, `profit_abs`, `trade_duration` |
| GET | `/api/v1/pair_candles` | `dashboard.py:178-184` — **hard-coded to port 8082 only**. ApeX does not need this. |
| POST | `/api/v1/forceexit` body `{"tradeid": "<id>"\|"all"}` | `portfolio_guard.py:152, 201`, `telegram_bot.py:302` | returns `{"result": str}`; callers branch on `.get("error")` then `.get("result")` |
| POST | `/api/v1/stopentry` | `portfolio_guard.py:140, 152-153, 176`, `telegram_bot.py:315` | returns `{"status": str}`; caller requires a **dict without** `"error"` (`telegram_bot.py:317`) |
| POST | `/api/v1/reload_config` | `portfolio_guard.py:104, 182`, `telegram_bot.py:329` | returns `{"status": str}` — **this is the RESUME verb**, not a config reload, in this system |

Note: freqtrade's `/ping` is on the *public* router (`api_v1.py:82`, no auth), but every
caller sends Basic auth anyway. The shim should accept both authenticated and
unauthenticated `/ping`.

### 1.2 The minimal REST shim (`apex_api.py`)

FastAPI + uvicorn are already repo dependencies (`dashboard.py:19-23`), so no new stack.
**All handlers must serve from in-memory engine state — never call ApeX inside a request
handler** (see §4 rate-limit risk).

```
GET  /api/v1/ping           -> {"status": "pong"}
GET  /api/v1/show_config    -> {"version":"apex-0.1","dry_run":true,"state":"running",
                                "runmode":"dry_run","max_open_trades":N,
                                "stake_currency":"USDC","stake_amount":"...",
                                "bot_name":"cryptobot-apex","timeframe":"1h",
                                "force_entry_enable":false}
GET  /api/v1/balance        -> {"total": <float>, "total_bot": <float>, "currencies": [...],
                                "stake":"USDC","symbol":"USD","value":<float>,
                                "starting_capital":1000.0,"note":"Simulated balances"}
GET  /api/v1/whitelist      -> {"method":["StaticPairList"],"length":N,"whitelist":["BTC/USDC",...]}
GET  /api/v1/status         -> [ {trade...}, ... ]        # ALWAYS a list, [] when flat
GET  /api/v1/profit         -> {"profit_closed_percent":f,"closed_trade_count":i,"winrate":f,
                                "best_pair":s,"best_pair_profit_ratio":f, ...}
GET  /api/v1/daily?timescale=N -> {"data":[{"date":"YYYY-MM-DD","abs_profit":f,
                                  "rel_profit":f,"starting_balance":f,"fiat_value":f,
                                  "trade_count":i}, ...],   # newest first, len == N
                                  "fiat_display_currency":"USD","stake_currency":"USDC"}
GET  /api/v1/trades?limit=&offset= -> {"trades":[...],"trades_count":i,"offset":0,"total_trades":i}
POST /api/v1/forceexit      body {"tradeid":"3"|"all"} -> {"result":"Created exit order for trade 3."}
POST /api/v1/stopentry      -> {"status":"No more entries will occur from now. Run /reload_config to reset."}
POST /api/v1/reload_config  -> {"status":"Reloading config ..."}
```

Trade object served by `/status` (superset of everything consumed — mirror freqtrade names
exactly):

```json
{"trade_id":3,"pair":"BTC/USDC","base_currency":"BTC","quote_currency":"USDC",
 "is_open":true,"is_short":false,"exchange":"apex","amount":0.001,
 "stake_amount":100.0,"strategy":"ApexTrend","enter_tag":null,"timeframe":60,
 "open_date":"2026-07-22 04:00:00","open_timestamp":1753156800000,
 "open_rate":98000.0,"current_rate":98500.0,
 "close_date":null,"close_rate":null,"exit_reason":null,
 "profit_ratio":0.0051,"profit_pct":0.51,"profit_abs":0.51,
 "leverage":1.0,"funding_fees":0.0,"trading_mode":"futures",
 "fee_open_cost":0.05,"fee_close_cost":null,"trade_duration":null,
 "orders":[]}
```

**Three traps the shim must not fall into** (each is an already-fixed audit finding in this
repo):

1. `portfolio_guard.py:120-124` treats the bot as offline unless `/status` is a `list`
   **and** `balance.total` is a non-bool `int/float`. An error body like
   `{"detail":"..."}` parses fine and once false-tripped the breaker into force-closing
   everything. On any internal error the shim must return HTTP 500 / non-JSON, **never** a
   200 with a JSON error object.
2. `dashboard.py:332` uses `isinstance(bt, (int,float)) and not isinstance(bt, bool)` —
   same bool-is-a-subclass-of-int trap. Never emit `"total": true/false`.
3. `portfolio_guard.py:137-141` re-POSTs `stopentry` **every 15s** while the breaker is
   tripped. `stopentry`/`reload_config`/`forceexit "all"` must be idempotent and cheap.

### 1.3 Every place a 6th bot must be registered

| File:line | Current | Edit |
|---|---|---|
| `local_secrets.py:9-15` | ports 8080-8084 | add `8085: "<APEX_REST_PW>",   # apex (DEX)` |
| `watchdog.py:45-47` | `BOTS` list of 5 | add `("ApeX", 8085, api_pw(8085))` |
| `dashboard.py:156-162` | `BOTS` 4-tuples with desc | add `("ApeX", 8085, api_pw(8085), "ApeX Omni · DEX perps")` |
| `dashboard.py:116-133` (`read_equity`) | column tuple `("spot","futures","brakedhold","btc_hold","basket_hold")` | add `"apex"` |
| `dashboard.py:1205-1211` | JS `series=[...]` | add `{k:"apex",color:"#...",lw:1.5,dash:[]}` |
| `dashboard.py:812-818` | legend `<span>`s | add ApeX swatch |
| `equity_logger.py:29-30` | `BOTS` (3) | add `("apex", 8085, api_pw(8085))` |
| `equity_logger.py:33` | `FIELDS` | add `"apex"` (safe: `csv.DictWriter` fills missing keys on old rows with `""`) |
| `stats_lib.py:19-25` | key→sqlite map | add `"apex": ("ApeX · Omni DEX", "tradesv3_apex.sqlite")` — this alone wires up `/stats` (`telegram_bot.py:211-212`) and `weekly_review.py:93, 118` with no further edits |
| `telegram_bot.py:29-30` | `BOTS`, `BOTS_BY_NAME` (spot+futures) | add `("ApeX", 8085, ...)` / `"apex": (8085, ...)`; also update the usage strings at `telegram_bot.py:286, 289, 297, 300, 313, 327` which literally say "Use `spot` or `futures`" |
| `portfolio_guard.py:35-37` | `BOTS = [SPOT, FUTURES]` | **do NOT add naively — see §4.3** |
| `sharp_move_alert.py:28-29` | `BOTS` (3) | optional |
| `weekly_digest.py:18`, `two_week_report.py:30`, `trade_notifier.py:23` | `BOTS` (spot+futures) | optional; `trade_notifier` is retired per `watchdog.py:44` |

`.gitignore` needs **no** edit: `*.secret.json:6`, `*.sqlite:22-24`, `*_state.json:33`,
`*.log/*.err:27-28` already cover the new artifacts. `config_apex.json` becomes tracked,
matching the other 5.

### 1.4 launchd

Pattern from `~/Library/LaunchAgents/com.vikas.bot.futures.plist` (freqtrade binary +
`--config/--strategy/--db-url/--datadir`, `KeepAlive`, `RunAtLoad`, stdout/stderr into
`~/cryptobot/bot_*.log|.err`). The ApeX equivalent runs our own module instead:

```xml
<!-- ~/Library/LaunchAgents/com.vikas.bot.apex.plist -->
<dict>
  <key>Label</key><string>com.vikas.bot.apex</string>
  <key>WorkingDirectory</key><string>/Users/vikasreddy/cryptobot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/vikasreddy/cryptobot/.venv/bin/python3</string>
    <string>apex_engine.py</string>
    <string>--config</string><string>config_apex.json</string>
  </array>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/Users/vikasreddy/cryptobot/bot_apex.log</string>
  <key>StandardErrorPath</key><string>/Users/vikasreddy/cryptobot/bot_apex.err</string>
</dict>
```

`logrotate.py` needs no edit — it globs (`logrotate.py:16-20`). `watchdog.py:49 DAEMONS`
watches guardian/traderjoy via `launchctl`; the ApeX bot is covered by the port-ping path
instead, consistent with the other 5.

---

## 2. GAP LIST — what freqtrade gave away for free

| Capability | How the 5 existing bots get it | What the ApeX bot must build |
|---|---|---|
| **Order lifecycle** | freqtrade internal; `order_types` market/limit + `unfilledtimeout` entry/exit in minutes (`config_scalp.json:16-25`, `config.json:16-21`) | `create_order_v3` + poll fills via WS/REST; cancel-on-timeout mirroring the same `unfilledtimeout` keys in `config_apex.json`; handle partial fills (freqtrade's `nr_of_successful_entries`) |
| **DRY-RUN / paper mode** | `dry_run: true` + `dry_run_wallet: 1000` (all 5 configs, line 8-9) | **SDK has none.** Build a fill simulator: take best bid/ask from the depth stream, apply the config `fee` (precedent: `config_scalp.json:14 "fee": 0.001`), debit/credit a simulated wallet seeded at 1000. Every write path must branch on `dry_run` at one choke point, not scattered |
| **Position / trade tracking** | freqtrade `Trade` ORM objects | own `Trade` dataclass with the exact freqtrade field names in §1.2 — cheapest way to satisfy 9 consumers |
| **Trades store** | `tradesv3_*.sqlite`, one per bot, path set by `--db-url` in each plist; queried read-only by `stats_lib.py:28-33` | `tradesv3_apex.sqlite` with a `trades` table using **freqtrade's column names**, because `stats_lib` SQL is literal: `is_open, close_profit, close_profit_abs, exit_reason, open_date, close_date, fee_open_cost, fee_close_cost, funding_fees, is_short, pair, stake_amount` (`stats_lib.py:36-73`). Match those and `/stats`, `weekly_review`, `futures_check` work with the one-line map edit in §1.3 |
| **OHLCV feed** | freqtrade downloads + maintains dataframes; `--datadir user_data/data/okx` in the plist | ApeX SDK `klines` → in-memory candle cache + periodic top-up; persist to `user_data/data/apex/` so a restart doesn't re-pull history |
| **Strategy loop** | freqtrade's main loop, `internals.process_throttle_secs: 5` (`config.json:83`) | own `while True:` loop honoring the same throttle key. Per CLAUDE.md: catch-all / print / sleep / continue — the daemon never dies |
| **REST shim** | `api_server` block in each config (`config.json:64-72`) | `apex_api.py` on a uvicorn thread inside the engine process, port 8085, Basic auth `freqtrader:api_pw(8085)` |
| **Telegram/feed parity** | fills surface via the activity feed; alerts go through `state_io.verified_send(..., feed_source=...)` (`state_io.py:83-104`, mirrored to the dashboard by `append_feed`, `state_io.py:29-42`) | use `verified_send` with `feed_source="apex"` — never fire-and-forget |
| **Atomic state** | `state_io.save_json/save_text` (`state_io.py:45-80`) | same helpers for `apex_state.json` (git-ignored by the `*_state.json` rule) |

---

## 3. PHASED BUILD PLAN

### P0 — SDK spike + testnet auth (no repo integration)

- **Create:** `apex_spike.py` (throwaway, top-level, later deleted or folded into
  `apex_client.py`).
- **Do:** `.venv/bin/pip install apexomni` (pulls `web3`, `mnemonic`); on-chain/zk key
  registration against **testnet**; instantiate HTTP + WS clients.
  > ⚠️ **Overridden by the STATUS caveat at the top of this file** — do this in a
  > throwaway venv, not the live `./.venv`, to avoid destabilizing the 5 running bots.
- **Verify:** one command prints (a) account equity, (b) a BTC depth snapshot, (c) 10
  klines — the *actual output*, not "it worked". Confirm `web3`/`eth-*` don't conflict with
  the pinned `ccxt 4.5.65` env (`freqtrade --version`).
- **Gate:** do not proceed until testnet auth is reproducible from a cold process.

### P1 — REST shim + register with the ops layer, engine flat-forever

- **Create:** `config_apex.json` (`dry_run:true`, `dry_run_wallet:1000`, port 8085, static
  pair whitelist, `add_config_files: ["config_apex.secret.json"]` — mirroring
  `config.json:86-88`), `config_apex.secret.json`, `apex_api.py`, `apex_engine.py` (loop
  that holds zero positions and just heartbeats), `com.vikas.bot.apex.plist`.
- **Edit:** `local_secrets.py:9-15`; `watchdog.py:45-47`; `dashboard.py:156-162`.
- **Verify:** `curl -u freqtrader:<APEX_REST_PW> 127.0.0.1:8085/api/v1/{ping,status,balance,profit,show_config,whitelist,daily?timescale=30}`
  returns the §1.2 shapes; the dashboard card renders "ApeX · online · $1000";
  `watchdog.py` prints `broken: none healthy`. **Ask before loading the plist**
  (CLAUDE.md: don't restart launchd jobs unless asked).

### P2 — paper engine + strategy + trades store

- **Create:** `apex_client.py` (SDK wrapper: retries, rate-limit budget, reconnect),
  `apex_store.py` (sqlite, freqtrade-compatible `trades` table), `apex_strategy.py`
  (indicators + entry/exit signals), fill simulator inside `apex_engine.py`.
- **Edit:** `stats_lib.py:19-25`; `equity_logger.py:29-33`;
  `dashboard.py:116-133, 812-818, 1205-1211`; `telegram_bot.py:29-30` (+ the usage strings).
- **Verify:** force a synthetic signal → a paper trade opens, appears in `/status`, closes
  into `tradesv3_apex.sqlite`; `.venv/bin/python stats_lib.py apex` prints a real
  breakdown; next `equity_logger` run writes a non-empty `apex` column in
  `equity_history.csv`.
- **Gate:** run ≥2 weeks paper before P3 — the same evidence gate as
  `GO_LIVE_CHECKLIST.md` Phase 0 (≥30 closed trades, ≥4 weeks, beats buy-and-hold).

### P3 — live, tiny size, guard integration

- **Edit:** `config_apex.json` → `dry_run:false`, minimum viable size;
  `portfolio_guard.py:35-37` **only under the §4.3 decision**.
- **Create:** an ApeX section in `GO_LIVE_CHECKLIST.md` — its Phase 4 ("withdrawal
  permission OFF", "IP-allowlist the keys") is **not achievable on a DEX** and needs
  replacing, not copying.
- **Verify:** one real trade watched end-to-end (fill → shim `/status` → dashboard →
  sqlite → Telegram), then a rehearsed kill switch
  (`launchctl bootout gui/$(id -u)/com.vikas.bot.apex`) with a documented answer to "what
  happens to the open position when the process dies?"

---

## 4. RISKS SPECIFIC TO THIS SYSTEM

**4.1 It breaks the all-paper invariant the ops layer was designed around.** Confirmed:
every one of the 5 configs is `dry_run: true` with `dry_run_wallet: 1000`. That constant is
baked into the ops layer as an assumption, not a config: `dashboard.py:35
START_EACH = 1000.0` and the headline P&L at `dashboard.py:389-395`; `equity_logger.py:28
START = 1000.0` anchoring both benchmarks. A live ApeX bot funded with, say, $50 renders as
a **-95% loss** in the dashboard header and poisons the equity chart. Either seed the ApeX
display baseline separately or exclude it from the headline math.

**4.2 Key custody is categorically worse than the CEX case.** ApeX registration derives the
API key/secret/passphrase from an Ethereum key / mnemonic. `GO_LIVE_CHECKLIST.md` Phase 4's
two core mitigations — *withdrawal permission OFF* and *IP-allowlist* — do not exist here:
the seed **is** the funds, and its compromise is unrecoverable and irreversible. This
machine currently stores localhost passwords as `__REDACTED__`-style literals
(`local_secrets.py:9-15`), which is fine for paper and not fine next to a hot wallet.
Phase 2 of the checklist (FileVault, strong REST creds) becomes a hard prerequisite, not a
"before-live" nice-to-have.

**4.3 The guardian would mix paper dollars with real dollars.**
`portfolio_guard.py:130-133` sums `balances.values()` across `BOTS` and trips at 10%
drawdown from the combined peak. Add a live ApeX bot to `portfolio_guard.py:35-37` and:
(a) a paper bot's fake drawdown can force-exit **real** ApeX positions at market; (b) paper
gains can mask a real ApeX loss so the breaker never fires on the account that actually
matters. Also `portfolio_guard.py:188-190` indexes `statuses["Spot"]` / `statuses["Futures"]`
by literal key — RULE 2 stays spot-vs-futures regardless. **Recommendation:** do not add
ApeX to the shared guard. Run a second guard instance with its own state file and its own
(tighter) thresholds, preserving the single-writer rule from CLAUDE.md. Today the answer to
"does the guard watch ApeX?" is **no**, and that is a deliberate choice to make, not an
oversight to fix.

**4.4 No paper mode in the SDK means the dry-run number is our bug.** For the other 5 bots,
dry-run P&L is freqtrade's problem and is battle-tested. Here, an optimistic fill simulator
(filling at mid instead of the far side, ignoring slippage/funding) produces a paper track
record that passes the evidence gate and then loses money live. Simulate pessimistically:
fill at the taker side of the book, always charge fees, always accrue funding.

**4.5 Rate limits and poll amplification.** Combined steady-state load on port 8085:
guardian every 15s × 2 endpoints (`portfolio_guard.py:39`), watchdog every 900s (plist
`StartInterval`), plus **6 calls per bot per dashboard refresh** (`dashboard.py:316-338`) on
every page load. If any handler proxies to ApeX synchronously, one dashboard tab becomes a
sustained burst against the DEX. The shim must be a pure read of engine memory; the engine
owns all exchange I/O on its own throttle.

**4.6 Freqtrade semantics leak into the ops layer's vocabulary.** `reload_config` means
*resume* here (`portfolio_guard.py:182`, `telegram_bot.py:329`), `stopentry` means *pause
new entries but keep managing open ones* (`telegram_bot.py:320-321`), and
`forceexit {"tradeid":"all"}` must close everything (`portfolio_guard.py:152`). ApeX perps
additionally have concepts freqtrade doesn't model at this layer — liquidation price,
funding, reduce-only. Implement the three verbs with freqtrade meanings, and never let a
shim POST return `{"error": ...}` inside a 200 (`telegram_bot.py:316-319` explicitly exists
because `/pause` once lied when a bot was down).

**4.7 Restart reconciliation.** `KeepAlive` + Mac sleep means the process dies and returns
with positions possibly still open on-chain. Freqtrade reconciles from its DB + exchange on
boot; the ApeX engine must do the same explicitly (load sqlite → fetch live positions →
reconcile → only then serve `/status`), or the shim will report a flat book while real risk
is on.

---

## 5. Summary tables

### Files to CREATE

| File | Purpose |
|---|---|
| `apex_client.py` | ApeX SDK wrapper — auth/registration, HTTP+WS, retries, rate-limit budget, reconnect |
| `apex_engine.py` | Main daemon: strategy loop, order lifecycle, paper fill simulator, position tracking, reconciliation on boot; hosts the shim on a thread |
| `apex_strategy.py` | Indicators + entry/exit signals (the freqtrade-strategy equivalent) |
| `apex_store.py` | `tradesv3_apex.sqlite` writer, freqtrade-compatible `trades` columns |
| `apex_api.py` | REST shim on :8085 — the 9 GETs + 3 POSTs of §1.2 |
| `config_apex.json` | Tracked config: `dry_run`, wallet, pairs, fee, unfilledtimeout, `api_server.listen_port: 8085` |
| `config_apex.secret.json` | Credentials — git-ignored by `.gitignore:6` |
| `~/Library/LaunchAgents/com.vikas.bot.apex.plist` | launchd job (§1.4) |
| `apex_spike.py` | P0 throwaway; delete after `apex_client.py` lands |

### Files to EDIT

| File:line | Change | Phase |
|---|---|---|
| `local_secrets.py:9-15` | add `8085` password | P1 |
| `watchdog.py:45-47` | add `("ApeX", 8085, api_pw(8085))` to `BOTS` | P1 |
| `dashboard.py:156-162` | add ApeX 4-tuple to `BOTS` | P1 |
| `dashboard.py:116-133` | add `"apex"` to `read_equity` columns | P2 |
| `dashboard.py:1205-1211` | add `apex` equity-chart series | P2 |
| `dashboard.py:812-818` | add ApeX legend swatch | P2 |
| `equity_logger.py:29-30, 33` | add ApeX to `BOTS` and `FIELDS` | P2 |
| `stats_lib.py:19-25` | add `"apex"` → `tradesv3_apex.sqlite` (auto-wires `/stats` + `weekly_review.py:93,118`) | P2 |
| `telegram_bot.py:29-30` (+ usage strings at `:286,289,297,300,313,327`) | add ApeX to `BOTS` / `BOTS_BY_NAME` | P2 |
| `portfolio_guard.py:35-37` | **decision required** — recommend a separate guard instance, not a 3rd entry (§4.3) | P3 |
| `GO_LIVE_CHECKLIST.md` Phase 2/4 | DEX addendum: seed custody replaces "withdrawal permission OFF" + IP allowlist | P3 |
| `sharp_move_alert.py:28-29`, `weekly_digest.py:18`, `two_week_report.py:30`, `trade_notifier.py:23` | optional coverage extensions (`trade_notifier` retired per `watchdog.py:44`) | P2/P3 |
| `.gitignore` | **no change needed** — `*.secret.json`, `*.sqlite`, `*_state.json`, `*.log/.err` already covered | — |

No files were modified, nothing was restarted, no Telegram was sent.
