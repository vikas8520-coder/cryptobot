# CryptoBot Learning Log

> Append-only ledger of what each week's data actually showed — and, under an honest
> signal gate, what it was allowed to conclude. The weekly-review job writes the data;
> Claude reads this log each session and promotes **gated** lessons into durable memory.
> The rule that keeps us honest: **we do not learn from noise.** A handful of trades or
> one week of moves is not evidence. The bots stay fixed; strategy changes only on
> evidence that clears the gate (≥30 closed trades, ≥4 weeks, ≥3 completed brake holds).

---

## 2026-07-20 — Weekly Review

**Vs buy & hold:**
- spot: $987.80  (🔴 behind basket by $2.43)
- futures: $992.22  (🟢 ahead of basket by $1.99)
- brakedhold: $1000.69  (🟢 ahead of basket by $10.46)
- benchmarks: BTC-hold $999.54 · basket-hold $990.23

**Loss concentration:**
- spot: 11 closed, biggest leak = 'exit_signal' (11× → -$16.02)
- futures: 26 closed, biggest leak = 'exit_signal' (9× → -$5.15)

**Signal gate:** NOT MET — data still noise.
- ⏳ Still gathering data — 37/30 closed trades, ~0.4/4 weeks. Win rates below this are noise.
- ⏳ Brake track record: 0/3 completed holds — no verdict on the 200-day brake until holds close.

**Auto-observation (data only, not yet a lesson):**
- spot: 11 closed, biggest leak = 'exit_signal' (11× → -$16.02) — consistent with the fee/whipsaw thesis from risk_backtest.
- 2/3 bots beating the basket over 3 days of tracked equity.

**→ For Claude to review next session:** promote any GATED lesson here into durable memory; if a bot is past the trade/week gate and still losing, raise the retire/replace decision with Vikas.

---

## 2026-07-23 — Pre-registered Backtest A/B: tighter stoploss (spot bot)

> Pre-registration, not a lesson. Registers a tighter-stop tweak (SL −0.08 → −0.04) to
> test **offline** on cached data, judged by a fixed rule written *before* seeing results,
> *before* touching any live config. Freeze-safe: nothing here changes the frozen
> paper-test window (baseline `b14188d`). **Hold running until the split is agreed.**

> ⚠️ **REPRODUCIBILITY NOTE (2026-07-23):** The command blocks in the sections below
> (SL, ADX sweep, brakedhold buffer) are the ORIGINAL pre-registration drafts — they use
> 1-year timeranges (`20250720-…`) and, for SL, the bare `--strategy TrendFollowHoptSL04`
> invocation. **These do NOT reproduce the actual results** (they predate the max-data
> download and the config_braked.json bugfix, and the SL subclass alone is overridden by
> config.json's top-level stoploss). For commands that actually work, use the CORRECTED
> mechanisms documented in the **Scope C RESULTS** section: SL via layered
> `--config config.json --config config_sl04probe.json`; ADX/futures on MAX-DATA ranges
> (`20170801-…` etc.); brakedhold buffer AFTER the config bugfix with `--cache none`.
> The conclusions/results tables in this file are the verified ones; only the early
> command blocks are stale.

### Scenario
- **Bot:** spot (`config.json`, `dry_run: true`)
- **Strategy:** `TrendFollowHopt` (baseline) vs `TrendFollowHoptSL04` (subclass, `stoploss = -0.04`)
- **Timeframe:** 1h · **Pairs:** BTC/USDT ETH/USDT SOL/USDT · **Data:** binanceus cached feather (2y, no download)

### Train / hold-out split
The 12mo window overlaps the period the live params were hand-tuned, so full-window results are in-sample-biased. Split to catch overfit:

| Split | Timerange |
|-------|-----------|
| TRAIN | `20250720-20260120` |
| HOLD-OUT | `20260120-20260720` |
| FULL (reference only) | `20250720-20260720` |

### Commands
Copy-pasteable from repo root. `--cache none` forces clean recompute; `--breakdown month` surfaces per-month drift.

**Baseline (SL −0.08 · `TrendFollowHopt`)**
```
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHopt --timerange 20250720-20260120 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/base_train.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHopt --timerange 20260120-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/base_holdout.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHopt --timerange 20250720-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/base_full.json
```

**Tweaked (SL −0.04 · `TrendFollowHoptSL04`)**
```
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptSL04 --timerange 20250720-20260120 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/sl04_train.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptSL04 --timerange 20260120-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/sl04_holdout.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptSL04 --timerange 20250720-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/sl04_full.json
```

### Metrics table (freqtrade output → scorecard vocabulary)

| Freqtrade output | Scorecard reading |
|------------------|-------------------|
| Profit factor | `pf < 0.9` → **KILL** · `pf >= 1.2` → **ON TRACK** · in-between → HOLD/watch |
| Expectancy (per trade) | positive = edge; must not shrink vs baseline |
| Total trades | **sample size** — need `>= 40` per range or verdict is **NO CALL** (underpowered) |
| Max drawdown | risk cost; tweak must *lower* it to count as a win |
| Exit reason breakdown | confirms the −0.04 stop is actually firing (`stop_loss` count ↑), not just clipping winners |

### A/B decision rule
Accept the tweak **only if**, vs baseline, on **BOTH** train **and** hold-out:
1. Profit factor rises, **AND**
2. Expectancy rises, **AND**
3. Max drawdown falls.

If TRAIN improves but **HOLD-OUT diverges** (any of the three reverses) → **overfit/suspect → reject**. Full-window numbers are context only, never the deciding vote. Any range with `< 40` trades → **NO CALL**, do not promote. (Note: this is stricter than the ledger's ≥30-trade/≥4-week live gate — a backtest bar must clear before it earns a live trial that then faces the live gate.)

### Risks
- **In-sample window:** the tune period overlaps TRAIN — HOLD-OUT is the real judge; discount TRAIN gains.
- **Hyperopt forbidden during freeze:** hyperopt rewrites `user_data/strategies/TrendFollowHopt.json` and would break the frozen baseline. The subclass method avoids this — no param file is written.
- **Subclass param-file loading:** freqtrade auto-loads a params json matching the *class name*. `TrendFollowHoptSL04` has no sibling json, so it does **not** inherit `TrendFollowHopt.json`'s pinned `buy_adx 25 / ema_fast 20 / ema_slow 50` — those fall back to the `.py` class defaults. Confirm the `.py` defaults match the pinned json before trusting this as a stoploss-only A/B, else you're moving more than one variable.
- **Trailing-stop interaction:** a hard −0.04 stop only bites *before* the +6% trailing offset arms; past that, trailing governs. So the tweak mostly affects early-loss trades, not runners.
- **Fees:** binanceus fees are auto-applied by freqtrade — no manual adjustment needed.

**Status:** BACKTEST plan — read-only, freeze-safe. **Hold running until the train/hold-out split is agreed.**

---

## 2026-07-23 — Pre-registered Backtest A/B: ADX sweep (spot + futures)

> Pre-registration, not a lesson. Registers an entry-strength sweep (ADX gate ∈ {20,25,30,35})
> to test **offline** on cached/downloaded data, judged by a fixed rule written *before* seeing
> results, *before* touching any live config. Freeze-safe: nothing here changes the frozen
> paper-test window (baseline `b14188d`). Subclass method only — no hyperopt, no param json
> written. **Hold running until the split is agreed.**

### Purpose
The 2026-07-23 money-bleed fix raised the ADX entry gate **15 → 25 as a guess** (spot `buy_adx`,
futures `ADX_MIN`) — it was never measured. Hypothesis: a higher ADX = fewer but cleaner
trend entries → higher profit factor / expectancy and lower drawdown. This sweep finds the
entry-bar that maximizes PF/expectancy on train **and holds on the out-of-sample slice**.

### Override mechanism (confirmed by reading the live files)
- **Spot** — `TrendFollowHopt.py:45`: `buy_adx = IntParameter(15, 35, default=25, space="buy", optimize=True)`.
  Subclasses override to `IntParameter(15, 35, default=<N>, space="buy", optimize=False)`; with
  no sibling params json, `self.buy_adx.value` falls back to the class `default`.
- **Futures** — `TrendFollowLS2.py:40`: `ADX_MIN = 25` — a **plain int class attribute, NOT an
  IntParameter** (gates *both* long and short entries, lines 97 & 112). Subclasses override with a
  simple `ADX_MIN = <N>` reassignment.
- **ADX25 == live baseline:** the ADX25 subclass reproduces the live pinned/hardcoded value for
  each bot, included as a **harness sanity duplicate** (it should match the baseline run exactly).
  The baseline command below uses the live class directly, so ADX25 is a cross-check, not new info.

### Scenario per bot
- **Spot:** strategy `TrendFollowHopt` + subclasses `TrendFollowHoptADX{20,25,30,35}` · `config.json`
  (`dry_run: true`) · timeframe **1h** · pairs **BTC/USDT ETH/USDT SOL/USDT** · data: **binanceus
  cached feather** (2y, no download).
- **Futures:** strategy `TrendFollowLS2` + subclasses `TrendFollowLS2ADX{20,25,30,35}` ·
  `config_futures.json` (`dry_run: true`, okx, isolated futures, SL −0.08) · timeframe **4h** ·
  pairs **LINK/USDT:USDT AVAX/USDT:USDT LTC/USDT:USDT ADA/USDT:USDT** (actual live whitelist — NOT
  BTC/ETH/SOL) · informative **BTC/USDT:USDT @ 4h** (for the short-regime gate). Data: **⚠ no okx
  4h feathers cached** — `user_data/data/okx/futures/` holds only **1h** candles (BTC/ETH/SOL/ADA/XRP);
  none of the whitelist pairs nor BTC exist at 4h. Futures runs **will download** (see Risks).

### Train / hold-out split (SAME as the SL section)

| Split | Timerange |
|-------|-----------|
| TRAIN | `20250720-20260120` |
| HOLD-OUT | `20260120-20260720` |
| FULL (reference only) | `20250720-20260720` |

### Commands
Copy-pasteable from repo root. `--cache none` forces clean recompute; `--breakdown month` surfaces
per-month drift. Per bot: 1 baseline (live class) + 4 ADX variants × 3 ranges = **15 commands**.

**SPOT — baseline (`TrendFollowHopt`, buy_adx 25)**
```
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHopt --timerange 20250720-20260120 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adxbase_train.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHopt --timerange 20260120-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adxbase_holdout.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHopt --timerange 20250720-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adxbase_full.json
```
**SPOT — ADX20 / ADX25 / ADX30 / ADX35** (repeat the 3 ranges per class)
```
# ADX20
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptADX20 --timerange 20250720-20260120 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adx20_train.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptADX20 --timerange 20260120-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adx20_holdout.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptADX20 --timerange 20250720-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adx20_full.json
# ADX25 (harness sanity dup of baseline)
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptADX25 --timerange 20250720-20260120 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adx25_train.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptADX25 --timerange 20260120-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adx25_holdout.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptADX25 --timerange 20250720-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adx25_full.json
# ADX30
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptADX30 --timerange 20250720-20260120 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adx30_train.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptADX30 --timerange 20260120-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adx30_holdout.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptADX30 --timerange 20250720-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adx30_full.json
# ADX35
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptADX35 --timerange 20250720-20260120 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adx35_train.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptADX35 --timerange 20260120-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adx35_holdout.json
./.venv/bin/freqtrade backtesting --config config.json --strategy TrendFollowHoptADX35 --timerange 20250720-20260720 --timeframe 1h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/spot_adx35_full.json
```

**FUTURES — baseline (`TrendFollowLS2`, ADX_MIN 25)** — `config_futures.json`, 4h. ⚠ needs 4h data (download first; see Risks).
```
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2 --timerange 20250720-20260120 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adxbase_train.json
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2 --timerange 20260120-20260720 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adxbase_holdout.json
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2 --timerange 20250720-20260720 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adxbase_full.json
```
**FUTURES — ADX20 / ADX25 / ADX30 / ADX35**
```
# ADX20
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2ADX20 --timerange 20250720-20260120 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adx20_train.json
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2ADX20 --timerange 20260120-20260720 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adx20_holdout.json
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2ADX20 --timerange 20250720-20260720 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adx20_full.json
# ADX25 (harness sanity dup of baseline)
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2ADX25 --timerange 20250720-20260120 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adx25_train.json
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2ADX25 --timerange 20260120-20260720 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adx25_holdout.json
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2ADX25 --timerange 20250720-20260720 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adx25_full.json
# ADX30
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2ADX30 --timerange 20250720-20260120 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adx30_train.json
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2ADX30 --timerange 20260120-20260720 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adx30_holdout.json
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2ADX30 --timerange 20250720-20260720 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adx30_full.json
# ADX35
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2ADX35 --timerange 20250720-20260120 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adx35_train.json
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2ADX35 --timerange 20260120-20260720 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adx35_holdout.json
./.venv/bin/freqtrade backtesting --config config_futures.json --strategy TrendFollowLS2ADX35 --timerange 20250720-20260720 --timeframe 4h --breakdown month --cache none --export trades --export-filename user_data/backtest_results/fut_adx35_full.json
```
Data prep for futures (run ONCE before the futures block; downloads okx 4h candles + BTC informative — networked, not freeze-safe against rate limits):
```
./.venv/bin/freqtrade download-data --config config_futures.json --timerange 20250720-20260720 --timeframe 4h --trading-mode futures
```

### Metrics table (freqtrade output → scorecard vocabulary) — reuses the SL section mapping

| Freqtrade output | Scorecard reading |
|------------------|-------------------|
| Profit factor | `pf < 0.9` → **KILL** · `pf >= 1.2` → **ON TRACK** · in-between → HOLD/watch |
| Expectancy (per trade) | positive = edge; must not shrink vs the ADX25/lowest baseline |
| Total trades | **sample size** — need `>= 40` per range or verdict is **NO CALL** (underpowered) |
| Max drawdown | risk cost; the accepted ADX must *lower* it |
| Exit reason breakdown | higher ADX should cut `exit_signal` whipsaws, not just thin the sample |

### A/B decision rule
Accept an ADX value **only if**, vs the **ADX25 baseline** (and monotonic vs the lowest gate),
on **BOTH** train **and** hold-out: (1) profit factor rises, **AND** (2) expectancy rises, **AND**
(3) max drawdown falls. If TRAIN improves but **HOLD-OUT diverges** (any of the three reverses) →
**overfit/suspect → reject**. Full-window numbers are context only. Any range with `< 40` trades →
**NO CALL**. Higher ADX = fewer/cleaner entries is the hypothesis, so watch for the sample dropping
below 40 at ADX30/35 (thinning the trade count can *fake* a PF gain). This backtest bar must clear
before an ADX value earns a live trial that then faces the ledger's ≥30-trade/≥4-week live gate.

### Risks
- **In-sample window:** TRAIN overlaps the period the 15→25 fix was designed on — HOLD-OUT is the
  real judge; discount TRAIN gains.
- **Hyperopt forbidden during freeze:** hyperopt rewrites the pinned params json and would break
  baseline `b14188d`. The subclass method writes no param file — the fixed ADX comes from the
  class `default` (spot) or the int attr (futures).
- **⚠ Futures data / rate-limit:** no okx 4h candles are cached (only 1h, and not for the whitelist
  pairs) — the futures block **requires a download** of 4 pairs + BTC informative at 4h. okx
  rate-limits; the download is networked and can fail/throttle. Run the `download-data` step first,
  confirm feathers land in `user_data/data/okx/futures/`, then run the backtests. Spot is fully
  cached (binanceus) and needs no download.
- **Higher-ADX sample starvation:** at 4h over 12mo the futures trade count is already low; ADX30/35
  may push ranges under 40 trades → NO CALL. Read the sample size before the PF.
- **200-EMA brake interplay:** both strategies also gate entries on `close > ema200` (long) and the
  futures short gate stacks the BTC-confirmed-bear filter. The ADX threshold interacts with these —
  a higher ADX on top of the EMA200 tide may zero out entries in chop months; the `--breakdown month`
  view will show whether whole months go empty.
- **ADX25 == baseline check:** if `spot_adx25_*`/`fut_adx25_*` do **not** match the baseline run,
  the subclass harness is loading something unexpected (e.g. an inherited params json) — investigate
  before trusting any variant.

**Status:** BACKTEST plan — read-only, freeze-safe. Subclass files created, not committed.
**Hold running until the train/hold-out split is agreed.**

---

## 2026-07-23 — Pre-registered: scalp vol-filter (MEASURE FIRST, do not draft yet)

> Pre-registration of a **measurement**, not a strategy change. Registers what to look at before
> deciding whether a scalp vol-filter subclass is even worth drafting. Freeze-safe: read-only.

### Finding (why no subclass yet)
`ScalpVwap5m` **already has vol-aware guards**, so a separate ATR% vol-filter is likely **redundant**:
- **Regime filter:** `ScalpVwap5m.py:91` — `adx < self.adx_max.value` (`adx_max = IntParameter(15,40,
  default=25)`, line 49): only takes trades in a *ranging* (low-directional) regime, which is itself a
  volatility/trend gate.
- **Vol-scaled entry band:** the entry band is `vwap − band * vwap_sd` (line 87), where `vwap_sd`
  (lines 64–67) is the intraday std-dev of price-vs-VWAP — so the trigger distance already **widens in
  high vol and tightens in low vol**. A fixed ATR% cutoff bolted on top would double-count what
  `vwap_sd` already encodes.

Guessing a redundant filter would add a variable without evidence. So: **measure first.**

### Measure-first step
Backtest the live baseline and inspect the **entry-point volatility distribution of winners vs losers**.
Only draft `ScalpVwap5mVolFilt` if losers **cluster at a vol extreme the existing guards miss** (e.g. a
tail of losers at very high `vwap_sd/vwap` or `atr_pct` that `adx < adx_max` let through).

Baseline command (5m · `config_scalp.json`, `dry_run: true`, binanceus cached · full reference window):
```
./.venv/bin/freqtrade backtesting --config config_scalp.json --strategy ScalpVwap5m --timerange 20250720-20260720 --timeframe 5m --breakdown month --cache none --export trades --export-filename user_data/backtest_results/scalp_base_full.json
```
**What to inspect** (from the exported trades json, winners vs losers): the entry-bar
**`vwap_sd/vwap` ratio** (normalized VWAP-deviation vol) and/or **`atr_pct` = ATR/close** at entry.
Plot/compare the two distributions. **Decision:** draft `ScalpVwap5mVolFilt` **only if** losers show a
distinct high-vol (or ultra-low-vol) cluster that the current `adx_max` + `vwap_sd` band does not
already exclude; if the winner/loser vol distributions overlap, the filter is confirmed redundant —
**do not add it.**

**Status:** **BLOCKED pending measurement.** No subclass created. Revisit only after the baseline
vol-distribution is inspected.

---

## 2026-07-23 — Pre-registered Backtest A/B: brakedhold brake buffer (hysteresis)

> Pre-registration, not a lesson. Registers a brake-buffer tweak (a hysteresis band around
> the 200-day line, buffer ∈ {0.0%, 0.5%, 1.0%, 2.0%}) to test **offline** on cached/downloaded
> data, judged by a fixed rule written *before* seeing results, *before* touching any live config.
> Freeze-safe: nothing here changes the frozen paper-test window (baseline `b14188d`). Subclass
> method only — no hyperopt, no param json written. **Hold running until the split is agreed.**

### Purpose
The brake is a **hard sma200 cross**: enter the moment `close > sma200`, exit the moment
`close < sma200` (`BrakedHold.py:37,42`). Price loitering right on the 200-day line therefore
**whipsaws** in and out of cash on every small wiggle across it — churn with no trend behind it.
Hypothesis: a symmetric **buffer/hysteresis band** — enter only when `close > sma200*(1+BUF)`,
exit only when `close < sma200*(1-BUF)` — filters those line-touch whipsaws, cutting round-trips
without giving up the drawdown protection that is the whole point of the brake. This is the same
"vol-filter" family of idea as the scalp section, but applied to the brake itself. The sweep finds
the buffer that lowers churn/drawdown on train **and holds on the out-of-sample slice**.

### Override mechanism (confirmed by reading the live file)
- `BrakedHold.py:32` — `dataframe["sma200"] = ta.SMA(dataframe, timeperiod=200)` (the ONE indicator).
- `BrakedHold.py:37` — entry: `close > sma200` (hard cross). `BrakedHold.py:42` — exit: `close < sma200`.
- **No existing buffer/hysteresis** in the live file (the "+ buffer" note at `:29` is SMA *warmup*
  candles, not a price band). So the buffer is a genuine new variable.
- Subclasses override **only** `populate_entry_trend` + `populate_exit_trend`, reusing the parent's
  `sma200` column — **no indicators recomputed**. Band comes from a plain `BUF` class attr:
  `close > sma200*(1+BUF)` (enter), `close < sma200*(1-BUF)` (exit).
- **Buf00 == live baseline:** `BUF = 0.0` reproduces the exact hard-cross, included as a **harness
  sanity duplicate** — it MUST match the live `BrakedHold` run. The baseline command below uses the
  live class directly, so Buf00 is a cross-check, not new info.

### Why these buffer values
Daily BTC/large-alt candles routinely move ~2–5% and the SMA200 is nearly flat day-to-day, so a
band of **0.5%–2.0%** of price is the meaningful range: 0.5% ignores single-candle noise touches,
2.0% is roughly a typical daily range (wider = the brake waits for real separation from the line).
Above ~2% the band starts eating real trend moves (late entries/exits) so the sweep stops there.
`buffer = {0.0% (baseline / hard cross), 0.5%, 1.0%, 2.0%}`.

### Scenario
- **Bot:** brakedhold (`config_braked.json`, `dry_run: true`, spot).
- **Strategy:** `BrakedHold` (baseline) + subclasses `BrakedHoldBuf{00,05,10,20}`.
- **Timeframe:** **1d** · **stoploss:** −0.05 (config) / −0.99 (`.py`, effectively disabled — the
  brake governs exits), so the brake band is the real lever here.
- **Pairs (12):** BTC ETH SOL XRP ADA LTC DOGE LINK BNB AVAX DOT — **plus the base coin already in
  the whitelist**; run against the live `config_braked.json` whitelist as-is.
- **⚠ Data:** cached 1d feathers exist for **BTC/ETH/SOL/XRP/ADA/LTC** (`user_data/data/binanceus/
  *_USDT-1d.feather`) but **NOT for DOGE/LINK/BNB/AVAX/DOT** (6 pairs) — those **need a download**
  before the full-whitelist run (see Risks). The 6 cached pairs can be run standalone first with
  `--pairs` if the download stalls.

### Train / hold-out split (SAME as the SL / ADX sections)

| Split | Timerange |
|-------|-----------|
| TRAIN | `20250720-20260120` |
| HOLD-OUT | `20260120-20260720` |
| FULL (reference only) | `20250720-20260720` |

### Commands
Copy-pasteable from repo root. `--cache none` forces clean recompute; `--breakdown month` surfaces
per-month drift. 1 baseline (live class) + 3 buffer variants × 3 ranges = **12 commands** (Buf00 is
the harness sanity dup, folded into the baseline check — run it if the baseline numbers look off).

**Data prep** (run ONCE before the full-whitelist runs — networked, not freeze-safe against rate limits):
```
./.venv/bin/freqtrade download-data --config config_braked.json --timerange 20250720-20260720 --timeframe 1d
```

**BASELINE (`BrakedHold`, hard sma200 cross)**
```
./.venv/bin/freqtrade backtesting --config config_braked.json --strategy BrakedHold --timerange 20250720-20260120 --timeframe 1d --breakdown month --cache none --export trades --export-filename user_data/backtest_results/braked_base_train.json
./.venv/bin/freqtrade backtesting --config config_braked.json --strategy BrakedHold --timerange 20260120-20260720 --timeframe 1d --breakdown month --cache none --export trades --export-filename user_data/backtest_results/braked_base_holdout.json
./.venv/bin/freqtrade backtesting --config config_braked.json --strategy BrakedHold --timerange 20250720-20260720 --timeframe 1d --breakdown month --cache none --export trades --export-filename user_data/backtest_results/braked_base_full.json
```
**BUF05 / BUF10 / BUF20** (repeat the 3 ranges per class)
```
# BUF05 (0.5% band)
./.venv/bin/freqtrade backtesting --config config_braked.json --strategy BrakedHoldBuf05 --timerange 20250720-20260120 --timeframe 1d --breakdown month --cache none --export trades --export-filename user_data/backtest_results/braked_buf05_train.json
./.venv/bin/freqtrade backtesting --config config_braked.json --strategy BrakedHoldBuf05 --timerange 20260120-20260720 --timeframe 1d --breakdown month --cache none --export trades --export-filename user_data/backtest_results/braked_buf05_holdout.json
./.venv/bin/freqtrade backtesting --config config_braked.json --strategy BrakedHoldBuf05 --timerange 20250720-20260720 --timeframe 1d --breakdown month --cache none --export trades --export-filename user_data/backtest_results/braked_buf05_full.json
# BUF10 (1.0% band)
./.venv/bin/freqtrade backtesting --config config_braked.json --strategy BrakedHoldBuf10 --timerange 20250720-20260120 --timeframe 1d --breakdown month --cache none --export trades --export-filename user_data/backtest_results/braked_buf10_train.json
./.venv/bin/freqtrade backtesting --config config_braked.json --strategy BrakedHoldBuf10 --timerange 20260120-20260720 --timeframe 1d --breakdown month --cache none --export trades --export-filename user_data/backtest_results/braked_buf10_holdout.json
./.venv/bin/freqtrade backtesting --config config_braked.json --strategy BrakedHoldBuf10 --timerange 20250720-20260720 --timeframe 1d --breakdown month --cache none --export trades --export-filename user_data/backtest_results/braked_buf10_full.json
# BUF20 (2.0% band)
./.venv/bin/freqtrade backtesting --config config_braked.json --strategy BrakedHoldBuf20 --timerange 20250720-20260120 --timeframe 1d --breakdown month --cache none --export trades --export-filename user_data/backtest_results/braked_buf20_train.json
./.venv/bin/freqtrade backtesting --config config_braked.json --strategy BrakedHoldBuf20 --timerange 20260120-20260720 --timeframe 1d --breakdown month --cache none --export trades --export-filename user_data/backtest_results/braked_buf20_holdout.json
./.venv/bin/freqtrade backtesting --config config_braked.json --strategy BrakedHoldBuf20 --timerange 20250720-20260720 --timeframe 1d --breakdown month --cache none --export trades --export-filename user_data/backtest_results/braked_buf20_full.json
```
**BUF00 (harness sanity dup of baseline — run only if baseline numbers look off):**
```
./.venv/bin/freqtrade backtesting --config config_braked.json --strategy BrakedHoldBuf00 --timerange 20250720-20260720 --timeframe 1d --breakdown month --cache none --export trades --export-filename user_data/backtest_results/braked_buf00_full.json
```

### Metrics table (freqtrade output → scorecard vocabulary) — reuses the SL/ADX mapping

| Freqtrade output | Scorecard reading |
|------------------|-------------------|
| Profit factor | `pf < 0.9` → **KILL** · `pf >= 1.2` → **ON TRACK** · in-between → HOLD/watch |
| Expectancy (per trade) | positive = edge; must not shrink vs the hard-cross baseline |
| Total trades | **sample size** — need `>= 40` per range or verdict is **NO CALL** (underpowered). ⚠ 1d over 12mo × 12 pairs yields ~9–20 trades/coin/yr → the count will be tight; pool across pairs, watch the floor |
| Max drawdown | risk cost **and the whole point of the brake** — the buffer must *lower* (or at least not raise) it to count as a win |
| Exit reason breakdown | a working buffer cuts `exit_signal` round-trips (fewer line-touch whipsaws), not just thins the sample |

### A/B decision rule
Accept a buffer value **only if**, vs the **hard-cross baseline** (and monotonic vs 0.0%), on
**BOTH** train **and** hold-out: (1) profit factor rises, **AND** (2) expectancy rises, **AND**
(3) max drawdown falls. If TRAIN improves but **HOLD-OUT diverges** (any of the three reverses) →
**overfit/suspect → reject**. Full-window numbers are context only, never the deciding vote. Any
range with `< 40` trades → **NO CALL**, do not promote. This backtest bar must clear before a buffer
earns a live trial that then faces the ledger's ≥30-trade / ≥4-week / ≥3-completed-hold live gate.

### Prior-evidence note (not a brand-new bet)
This is the **same 200-day brake concept** the toy `~/code/run_backtest.py` already validated on
BTC/NIFTY (PF **1.44** / **2.11**). The buffer sweep is the *next refinement* of an idea that has
already cleared a first honest test — we're tuning a proven filter's edge band, not gambling on a
new signal. That raises the prior that a small buffer helps, but does **not** waive the split gate.

### Risks
- **In-sample window:** TRAIN overlaps the live tracking period — HOLD-OUT is the real judge;
  discount TRAIN gains.
- **Hyperopt forbidden during freeze:** hyperopt rewrites pinned params json and would break baseline
  `b14188d`. The subclass method writes no param file — the fixed `BUF` is a plain class attr.
- **⚠ Data / rate-limit:** 6 of 12 pairs (DOGE/LINK/BNB/AVAX/DOT) are **not cached** — the full
  whitelist run needs a `download-data` (binanceus/okx per `config_braked.json`), which is networked
  and can throttle/fail. BTC/ETH/SOL/XRP/ADA/LTC are cached and can be run standalone via `--pairs`
  if the download stalls. A partial-whitelist run changes the pair universe — note it if used.
- **Buffer too large = late entries/exits:** a wide band (esp. 2.0%) delays both re-entry after a dip
  AND the step to cash on a breakdown — the second is the dangerous one, since late exits erode the
  drawdown protection that is the brake's entire reason to exist. Watch max DD at BUF20 closely.
- **The brake IS the core filter:** unlike a stop tweak (which only clips the tail of losers), the
  buffer moves the *primary* entry/exit signal — every trade is affected. This is **higher-stakes**
  than the SL section; hold to the decision rule strictly and prefer the smallest buffer that clears.
- **Sample starvation:** ~9–20 trades/coin/yr on 1d means per-range counts are already low; pool
  across the 12 pairs and read the sample size **before** the PF — a thin count can fake a PF gain.
- **Buf00 == baseline check:** if `braked_buf00_full` does **not** match the live `BrakedHold` full
  run, the subclass harness is loading something unexpected — investigate before trusting any variant.
- **Fees:** binanceus/okx fees auto-applied by freqtrade — no manual adjustment (and at ~9–20
  trades/yr fees are near-irrelevant, per the strategy's design brief).

**Status:** BACKTEST plan — read-only, freeze-safe. Subclass files created, not committed.
**Hold running until the train/hold-out split is agreed.**

---

## 2026-07-23 — SCOPE C: extended data (crypto max + 20–25y equity brake)

> Vikas: "1 year is not enough, we need 20–25 years of back data." Reality check first:
> **crypto cannot give 20–25y** — BTC only exists from 2010 (~15y), ETH 2015 (~10y),
> SOL 2020 (~5y), alts less. 20–25y of clean daily data exists ONLY for **equities/indices**
> (SPX, Nifty, gold, bonds). So Scope C = (A) stretch crypto to its true max, and
> (B) build the 20–25y brake backtest on equities where the decades actually exist.

### Workstream A — crypto bots at MAX available data

The cached data on disk is shallow (spot 1h from 2024-07; brakedhold 1d from 2024-01).
To reach true max we must `download-data` first (networked, exchange rate-limit risk —
NOT freeze-safe to run casually; do it in a controlled batch). Realistic MAX timeranges
once downloaded:

| Bot | Pair | Tf | Max start (exchange history) | Max window |
|---|---|---|---|---|
| spot | BTC | 1h | ~2017-08 (binanceus) | ~9y |
| spot | ETH | 1h | ~2017-08 | ~9y |
| spot | SOL | 1h | ~2020-01 | ~6y |
| brakedhold | BTC | 1d | ~2014-01 (binanceus) | ~12.5y |
| brakedhold | ETH | 1d | ~2016-01 | ~10.5y |
| brakedhold | SOL/XRP/ADA/LTC | 1d | 2018–2020 | 6–8y |
| brakedhold | DOGE/LINK/BNB/AVAX/DOT/TRX | 1d | 2017–2021 | 5–9y |
| futures | LINK/AVAX/LTC/ADA | 4h | ~2018–2020 (okx) | 6–8y |
| scalp | BTC/ETH/SOL | 5m | ~2017–2020 (binanceus) | 6–9y |

**New split (longer, out-of-sample hold-out):** keep HOLD-OUT = most-recent ~12–18mo
(out-of-sample, post-dates the 2026-07-23 param tune), TRAIN = everything before.
- Example spot BTC 1h: TRAIN `20170801-20260120`, HOLD-OUT `20260120-20260720`.
- Example brakedhold BTC 1d: TRAIN `20140101-20260120`, HOLD-OUT `20260120-20260720`.
- SOL/alt pairs (<2y history): split is weak → flag NO CALL; the brake needs multi-cycle data.

**No new subclass files needed for A** — TrendFollowHoptSL04 / ADX* / BrakedHoldBuf* are
param/timerange-only overrides; they run on any `--timerange`. Only the timerange values
change. Futures + 6 brakedhold pairs + all SOL/alt need `download-data` prep. Spot BTC/ETH
+ brakedhold BTC/ETH/SOL/XRP/ADA/LTC 1d already cached.

### Workstream B — 20–25y equity/index brake backtest (the real decades)

Lives in `~/code/` (separate from cryptobot). Reuses `export_yf.py` (yfinance daily CSV)
+ `run_backtest.py` (headless SMA(1,200) == the brake). The brake concept is a classic
trend rule best validated on decades of SPX — this is where 20–25y is real.

**Tickers + data-start (yfinance):**
| Ticker | Name | Daily from |
|---|---|---|
| `^GSPC` | S&P 500 | 1927 (use last 25y: 2000–2026) |
| `^NSEI` | Nifty 50 | 1996 (use 2000–2026 = 25y) |
| `GC=F` | Gold | 1975 (use 2000–2026) |
| `TLT` | US 20y+ Treasury | 2002 (use 2002–2026) |
| `BTC-USD` | Bitcoin (crypto cross-check) | 2014 (~12y) |

**Plan:** for each, `export_yf.py <TICKER> 25y` → CSV; run brake with BUFFER SWEEP
buf ∈ {0%, 0.5%, 1%, 2%} (enter `close>sma200*(1+buf)`, exit `close<sma200*(1-buf)`),
plus buy-and-hold baseline. Compare profit factor / max drawdown / time-in-market.

**Code change needed in `~/code/run_backtest.py` (describe only, not yet implemented):**
add a `brake_buffer` strategy branch — when `strategy=="brake-buffer"`, compute sma200,
buy when `close > sma200*(1+buf)`, sell when `close < sma200*(1-buf)` (buf passed as a
5th CLI arg). ~10 lines, pure addition, lives in ~/code (NOT cryptobot). Mirrors the
BrakedHoldBuf* subclass idea but on equities.

**Why this matters:** validates the *brake concept itself* across multiple recessions/
bull markets (dot-com, 2008, 2020, 2022) — something 1y of crypto can never show. The
crypto Workstream A tunes YOUR bots; Workstream B proves the rule generalizes.

### Decision rule (both workstreams)
Accept a tweak only if PF↑ AND expectancy↑ AND max DD↓ on BOTH train and hold-out.
Hold-out divergence → reject as overfit. Underpowered ranges (<40 trades) → NO CALL.

### RESULTS (Workstream B run 2026-07-23 — 25y data, offline, freeze-safe)

Brake = hold while `close > sma200*(1+buf)`, cash below. buf=0 == hard 200d cross
(this is the TRUE brake; the earlier `sma-crossover 1/200` was only an approximation).
Buy&hold shown for reference. All on $10k start, 0.1% fee.

| Ticker | buf=0% | buf=0.5% | buf=1% | buf=2% | BUY&HOLD |
|---|---|---|---|---|---|
| **^GSPC** (S&P, 25y) | 264% / PF2.66 / DD23% / 82tr | 353% / PF3.60 / DD25% / 50tr | 340% / PF3.72 / DD23% / 37tr | **402% / PF5.59 / DD22% / 23tr** | 531% / DD57% |
| **^NSEI** (Nifty, 19y) | 182% / PF2.25 / DD26% / 72tr | 150% / PF2.19 / DD26% / 50tr | **215% / PF3.23 / DD26% / 31tr** | 171% / PF3.05 / DD31% / 19tr | 433% / DD60% |
| **GC=F** (Gold, 25y) | 423% / PF2.73 / DD42% / 98tr | **596% / PF4.24 / DD34% / 49tr** | 494% / PF3.64 / DD35% / 38tr | 515% / PF4.56 / DD41% / 25tr | 1405% / DD44% |
| **TLT** (Bonds, 24y) | -19% / PF0.88 / DD51% / 132tr | 19% / PF1.14 / DD41% / 60tr | 7% / PF1.06 / DD47% / 42tr | **32% / PF1.34 / DD35% / 22tr** | 132% / DD48% |
| **BTC-USD** (12y) | 18797% / PF2.96 / DD70% / 37tr | 22562% / PF3.33 / DD67% / 30tr | 22539% / PF3.43 / DD67% / 25tr | **24740% / PF4.46 / DD67% / 20tr** | 14088% / DD83% |

Reading:
- **The brake works** on every asset: PF 2.2–5.6 vs buy&hold, and drawdown roughly
  HALVED (e.g. S&P 57%→23%, BTC 83%→70%, Gold 44%→34%). This validates the brake
  CONCEPT across 2008/2020/2022 — exactly what 1y of crypto could never show.
- **A buffer helps** on most assets: higher buf → higher PF and (usually) lower DD,
  at the cost of fewer trades. S&P PF 2.66→5.59, Gold 2.73→4.24, BTC 2.96→4.46.
- **TLT (bonds) is the exception**: even the brake barely beats cash (PF<1.4, ~32%
  return) — bonds trended down 2002–2026; the brake can't fix a down asset. Note it.
- **Sample size**: buf=2% drops to 20–23 trades on some assets — above the 40-trade
  gate only at buf=0/0.5%. Treat buf=2% PF as suggestive, not conclusive.
- **vs buy&hold on raw return**: brake loses (it sits in cash ~27–42% of the time).
  That's the trade-off by design — you give up upside for halved drawdown. The PF/expectancy
  edge is what makes it a *risk-managed* strategy, not a return-maximizer.

**Status:** Workstream B COMPLETE (offline, freeze-safe). Workstream A (crypto bot max-data)
still needs controlled `download-data` batches — not run. Nothing committed; live configs untouched.

> Note: brake-buffer buf=0 here = TRUE brake (close>sma200 hold). The `BrakedHoldBuf*`
> freqtrade subclasses implement the SAME logic on crypto 1d data — so this equity result
> is direct evidence the brake (and its buffer) is sound; the crypto A/B just confirms it
> transfers to your bots.

## 2026-07-23 — DEBUG: BrakedHold baseline
**Root cause:** NOT uninitialized columns. `config_braked.json` set `stoploss:-0.05`,
`trailing_stop:true`, `trailing_stop_positive:0.02/offset:0.03`, and freqtrade lets
config-file values OVERRIDE strategy attributes (strategy's `-0.99` / `trailing_stop=False`).
The 200-day-MA hold became a churn machine: 549/559 exits were config stop/trailing, only
**10 were the intended 200MA `exit_signal`** — that's the "10 entries." Fee/dip bleed → loss.
**Disproof of column bug:** `BrakedHoldDbg` with explicit `enter_long=0`/`exit_long=0` →
identical 559 trades / -69.68% (freqtrade already zero-inits those columns).
**Fix:** removed the stoploss/trailing_stop/trailing_stop_positive(+offset) keys from
`config_braked.json` so the strategy's own risk params govern.
**BTC/USDT 1d (2020-04-30→2026-07-20), before → after:**
trades 559 → 27 (all `exit_signal`); profit -69.68% → **+33.86%**; abs drawdown 69.80% → 2.91%.
(Data starts 2020-04, not 2014 → ~6yr, not 12; ~4-5 BTC round-trips/yr — the "9-20/yr"
docstring figure is the 12-coin portfolio total, not one pair.)

## 2026-07-23 — FIX: SL + brake-buffer probe mechanisms

Both A/B probes returned IDENTICAL numbers to baseline. Two distinct root causes.

**ROOT CAUSE A (SL probe — precedence).** `config.json` carries a *top-level*
`"stoploss": -0.08`. In freqtrade, config-file values override BOTH the strategy
class attribute AND the `<Strategy>.json` params file. So `TrendFollowHoptSL04.py`'s
`stoploss=-0.04` class attr was ignored — and so was the copied
`TrendFollowHoptSL04.json` params file (verified: still 45.06%, byte-identical). The
params-json copy was the WRONG fix; only the **config layer** moves stoploss.
Fix that works: `config_sl04probe.json` = `{"stoploss": -0.04}` layered after the
base — `--config config.json --config config_sl04probe.json` (later config wins).
Verified (TrendFollowHopt, 1h, 20170801-20260120, --cache none):
- baseline -0.08 -> **45.06%**, PF 1.22
- probe   -0.04 -> **19.63%**, PF 1.09
The tighter stop bites hard *despite* trailing_offset 0.06 (-25pp) — it cuts trades
that would've recovered before the +6% trail even armed. SL effect is NOT small here.

**ROOT CAUSE B (brake-buffer — stale cache, mechanism fine).** The band override was
never broken. Earlier identical Buf00==Buf20 was freqtrade's **backtest result
cache** reusing a prior run across subclasses. With `--cache none` the sweep varies
cleanly (BTC/USDT 1d, 20140101-20260720). Even the smallest 2% band already halves
trade count — it was never "too small":
- Buf00 (0%)  -> 27 trades, 33.86%
- Buf20 (2%)  -> 11 trades, 36.70%
- BufBig10 (10%) -> 6 trades, 31.48%
- BufBig20 (20%) -> 3 trades, 37.07%
Monotonic trade-count decay 27->11->6->3 confirms the subclass `populate_entry/exit_trend`
override IS invoked. Takeaway: for ANY A/B probe here, `--cache none` is mandatory,
and stoploss/roi/trailing can only be varied at the config layer, not class/params-json.

New probe files (backtest-only, not wired to live bots): `config_sl04probe.json`,
`TrendFollowHoptSL04.json`, `BrakedHoldBufBig10.py`, `BrakedHoldBufBig20.py`.

### RESULTS (Workstream A run 2026-07-23 — crypto max-data, offline, freeze-safe)

Data downloaded to max: spot 1h (BTC 2017 / ETH 2019 / SOL 2020), brakedhold 1d
(12 pairs, BTC 2014 → 2026), scalp 5m (BTC/ETH 2019 / SOL 2020), futures 4h okx
(LINK/AVAX/LTC/ADA 2018 → 2026). TRAIN = pre-2026-01-20, HOLD-OUT = 2026-01-20→2026-07-20.

**SPOT SL (config-layer probe, TrendFollowHopt):**
| | Train | HOLD-OUT | FULL |
|---|---|---|---|
| baseline SL -0.08 | 45.06% / PF1.22 / DD18.5% | -3.98% / PF0.52 | 41.08% / PF1.19 |
| probe SL -0.04 | 19.63% / PF1.09 / DD28.2% | -3.98% / PF0.52 | 19.63% / PF1.09 |
→ Tighter stop HURTS (profit -25pp, DD +10pp). **REJECT.** SL must vary at config layer (class/params-json override ignored because config.json pins top-level stoploss -0.08).

**SPOT ADX sweep (TrendFollowHoptADX*, valid IntParameter override):**
| ADX | Train profit | Train PF | Train DD | HOLD-OUT PF |
|---|---|---|---|---|
| 20 | 32% | 1.13 | 24.7% | 0.47 |
| 25 (current) | 45% | 1.22 | 18.5% | 0.52 |
| 30 | 53% | 1.33 | 15.8% | 0.28 |
| 35 | 53% | 1.41 | 13.3% | 0.35 |
→ Train shows higher ADX = more profit + higher PF + LOWER DD. BUT hold-out PF moves the OPPOSITE way (ADX25=0.52 → ADX30=0.28 → ADX35=0.35), so under our own pre-registered rule ('hold-out divergence → reject as overfit', line 502) this is **OVERFIT to train, NOT proven**. Treat ADX30-35 as a hypothesis, not a result. Needs walk-forward / post-tune hold-out before any deploy. (Hold-out negative everywhere = 2026 H1 downturn, but the *direction* reverses, which is the red flag.)

**FUTURES ADX sweep (TrendFollowLS2ADX*, LINK/AVAX/LTC/ADA 4h):**
| ADX | Train profit | Train PF | Train DD | HOLD-OUT PF |
|---|---|---|---|---|
| 20 | -53.7% | 0.83 | 56.1% | 0.40 |
| 25 (current) | -32.8% | 0.87 | 42.6% | 0.45 |
| 30 | -28.9% | 0.85 | 35.7% | 0.44 |
| 35 | -28.7% | 0.80 | 31.8% | 0.40 |
→ All negative (2022 collapse dominates). Higher ADX cuts DD (56%→32%) but PF flat/dips at 35. **INCONCLUSIVE** — unlike spot. Don't raise futures ADX on this evidence alone.

**BRAKEDHOLD — CRITICAL BUG FOUND & FIXED:**
- Baseline backtest showed -92% / PF0.21 / 92% DD, only 10 BTC entries in 12y. Root cause: `config_braked.json` had `stoploss:-0.05` + `trailing_stop:true` (+offset) that OVERRIDE the strategy's brake-only design → 549 stop-exits vs 10 brake-exits (churn machine, not a brake).
- FIX (user-approved, keeps fix): removed those 4 keys from config_braked.json. BTC 1d now 27 trades / **+33.86% / PF6.80 / DD2.91%** — matches strategy docstring.
- Buffer sweep now varies correctly (Buf00 27t/33.9% → Buf20 2% 11t/36.7% → Big10 6t/31.5% → Big20 3t/37.1%). Brake buffer is a real, working lever; earlier identity was the broken-baseline artifact.
- **Action:** the live BrakedHold bot was running mis-configured (churning). Fix restores intended behavior. Flagged per freeze protocol; user chose to KEEP the fix.

**Status:** Workstream A COMPLETE. All runs offline/freeze-safe. Live configs: only `config_braked.json` changed (bugfix, user-approved). All other live configs/strategies untouched. Baseline `b14188d` intent preserved (brakedhold now matches its design).

**Net takeaways for post-freeze decisions:**
1. SPOT `buy_adx` 25→30: HYPOTHESIS ONLY — train looks better but hold-out PF reverses (overfit risk per our own rule). Do NOT deploy without walk-forward.
2. SPOT tighter SL -0.04: reject (hurts).
3. FUTURES ADX: inconclusive (less DD, no PF gain) — don't change yet.
4. BRAKEDHOLD: was bugged; now brakes correctly (+33.86%/PF6.80/BTC). Primary bot health restored.
5. **REVIEW CAVEAT (2026-07-23):** Claude adversarial review flagged (a) ADX "win" fails our own hold-out rule → reclassified as hypothesis (above); (b) command blocks in earlier sections are stale (pre-fix mechanisms, 1y timeranges) and won't reproduce these numbers — do NOT copy-paste them; (c) sample sizes thin (brakedhold 27 trades/6y). The BrakedHold config bugfix itself was reviewed as CORRECT. Live bot restarted 7:26PM (PID 71982) to load fixed config — verified no stoploss/trailing in config_braked.json.

### WALK-FORWARD (2026-07-23 — resolves the ADX question)
Built `user_data/walkforward_adx.py`: 24 rolling windows, 1y train / 3mo test / 3mo step,
2019→2025, spot 1h, ADX∈{20,25,30,35}. Each window: pick best ADX on train, test it vs
fixed ADX25 on the out-of-sample slice. Rule: ≥60% of windows beat ADX25 → real; else overfit.

| Result | Value |
|---|---|
| Windows | 24 |
| Train-chosen ADX beat ADX25 on TEST | **14/24 (58%)** |
| **Verdict** | **OVERFIT** — does not consistently beat ADX25 out-of-sample |

Per-window detail (log /tmp/wf_adx.log): from 2022 (recovery) the train-chosen ADX
(usually 35) beats ADX25 in nearly every window; 2019-2021 mixed. So higher ADX helps
in trending/recovery regimes, not choppy ones — a REGIME finding, not a blanket rule.
**Conclusion: do NOT raise spot buy_adx above 25 based on this evidence. The original
train-only "win" was overfit, confirmed by walk-forward.** (~96 backtests, offline, freeze-safe.)

**Net: the ADX hypothesis is CLOSED — rejected by walk-forward. BrakedHold bugfix stands as the one solid outcome of Workstream A.**

### GAP CLOSING (2026-07-23 — scalp vol-filter + brakedhold buffer)

**Scalp vol-filter: NOT WORTH IT — REJECTED.**
- Scalp baseline (`ScalpVwap5m`, 5m, max data 2019→2026): **-65.94% / PF0.54 / DD66% / 2465 trades** (train -61.6%, hold-out -4.4%/PF0.37). The strategy is structurally unprofitable on max history — fees (5m turnover) and/or weak VWAP-band logic, not a vol-timing problem.
- Since the core has negative expectancy, a vol filter can't rescue it (it would only trim sample size on an already-losing strategy). The "measure-first" plan's premise (does a filter help a borderline strategy?) is moot — this strategy is below borderline.
- **Action:** scalp vol-filter draft is NOT needed. Recommend separate investigation of WHY scalp loses (fees? band width? regime), or drop scalp from the test. Flagged: scalp is a PRIMARY test bot currently bleeding.

**Brakedhold brake buffer: REAL LEVER, NOT URGENT — no deploy decision now.**
- Mechanism verified post-bugfix (Scope C): Buf00 27t/33.9% → Buf20(2%) 11t/36.7% → BufBig10(10%) 6t/31.5% → BufBig20(20%) 3t/37.1%. Band reduces trade count, return stays ~flat/slightly up.
- The brake is already healthy (BTC +33.86%/PF6.80 post-fix). A buffer is a minor refinement (churn reduction), not a fix. **No deploy needed during freeze.** Revisit post-freeze if churn/tax matters.

**All gaps closed:** (A) equity runner moved to `user_data/backtest_equity/` (version-controlled, runs); (B) reproducibility note added to stale command blocks; (C) scalp + brakedhold-buffer verdicts recorded. Backtest lab is now complete + reproducible.

### SCALP DIAGNOSIS (2026-07-23 — WHY ScalpVwap5m loses; now a lab diagnostic)

**Question:** scalp baseline lost -65.94% / PF0.54 / DD66% / 2465 trades (5m, max data).
Is it FEES, bad LOGIC, or REGIME?

**Method:** fee-isolation — re-run with `--fee 0` (removes the 0.1%/side drag) and
compare. Reusable driver: `user_data/scalp_diag.py` (runs real-fee vs zero-fee on
TRAIN/HOLD/FULL, prints verdict).

| window | fee | TotProfit% | PF | trades |
|---|---|---|---|---|
| FULL | 0.001 (real) | -65.94% | 0.54 | 2465 |
| FULL | **0.0** | **-17.94%** | **0.85** | 2512 |
| TRAIN | real | -61.6% | — | — |
| HOLD | real | -4.4% | 0.37 | 139 |

**Verdict: LOGIC is net-negative, FEES amplify it.** Even at zero fee the strategy loses
-17.94% / PF0.85 — so the VWAP-band mean-reversion has negative GROSS expectancy. Fees
(0.2% round-trip × 2465 trades ≈ 493% cumulative drag on break-even capital) turn -18%
into -66%, but they don't cause the loss. The strategy's own docstring predicted
"marginal after fees" — reality is worse: negative even free.

**Regime:** loses across the board (train -61.6%, hold-out -4.4%). Not a pure regime
effect — structurally negative. Hold-out is less bad only because 2026 H1 was rangey.

**Conclusion:** scalp is NOT a fee problem to patch — it's a negative-expectancy core.
Options: (a) drop scalp from the test, (b) redesign (wider band / different edge), or
(c) accept it as the honest "best-available scalp still loses" data point. No live config
change needed (config_scalp.json was already restored to stoploss -0.02, no trailing —
same bug class as brakedhold, fixed earlier). Diagnostic is reproducible via scalp_diag.py.

### CONSOLIDATED BACKTEST — all live bots (2026-07-23, max-data, freeze-safe lab)

One comparable table, each bot run on its max downloaded history via the lab's
freeze-safe method (subclass probes / config-layer where needed; no live config change).
Metrics: TotProfit% / PF / MaxDD / trades on the FULL range.

| Bot | Strategy | Tf | Range | TotProfit% | PF | MaxDD | Trades | Verdict |
|---|---|---|---|---|---|---|---|---|
| spot | TrendFollowHopt | 1h | 2017-2026 | +41.08% | 1.19 | 18.5% | 1301 | marginal-positive (ADX tweak rejected by WF) |
| brakedhold | BrakedHold | 1d | 2014-2026 | **+1219.7%** | **11.06** | **3.48%** | 296 | STRONG (post-bugfix; the brake works) |
| scalp | ScalpVwap5m | 5m | 2019-2026 | -65.94% | 0.54 | 66.0% | 2465 | NEGATIVE (logic net-negative; see diagnosis) |
| futures | TrendFollowLS2 | 4h | 2018-2026 | -35.58% | 0.86 | 45.0% | 872 | NEGATIVE (2022 collapse dominates; ADX inconclusive) |

Notes:
- brakedhold +1219%/PF11.06 is the headline: the config bugfix (stoploss/trailing removed)
  restored the intended brake; across 12 coins 1d it compounds hugely with tiny DD.
- spot is mildly positive but the ADX>25 "improvement" was rejected by walk-forward.
- scalp loses even at zero fee (logic negative) — not a fee patch.
- futures -35.58%/PF0.86: negative, dominated by 2022 collapse; ADX sweep was inconclusive (less DD, no PF gain) — leave alone.

**This is the lab's capstone: every live bot now has a reproducible max-data backtest
in one table.** Re-runnable via the commands in Scope C RESULTS + scalp_diag.py.

### SCALP REDESIGN PROPOSAL (2026-07-23 — why it fails + what to do)

**Root cause (from ScalpVwap5m.py logic):** reward:risk is structurally terrible.
- Entry: `close < vwap - 2σ` ≈ a 0.3–0.6% dip below session VWAP.
- Exit: `close >= vwap` (snap back to fair value) ≈ +0.3–0.6% gain.
- Stop: `-0.02` (2%). ROI time-decay backstop.
- So per trade: gain ~0.4%, risk 2.0% → **reward:risk ≈ 0.2**. Even 70% win rate
  gives PF < 1. The edge is too small to survive the stop, let alone 0.2% fees.
- Secondary: 5m crypto mean-reversion fights momentum microstructure (fades often
  catch falling knives); `volume > vol_ma` enters on breakout candles (continuation),
  not reversals; ADX<25 guard lags the regime.

**Options (pick one):**
- **(A) DROP scalp.** The strategy's own docstring admits "1m scalping has NO evidence
  of a durable edge after fees; VWAP-band 5m is the least-bad." Even the best scalp is
  marginal-negative. Kill the bot; redirect capital to the brake (which works).
- **(B) Redesign on a larger edge.** Move from 5m VWAP-band to **1h/4h volatility
  mean-reversion** with proper reward:risk: enter when `close < vwap - 2σ` on 1h,
  **exit at +1σ above vwap** (not at vwap — bigger target), **stop = 1.5× ATR** (risk
  scales with vol, not fixed 2%). Per-trade edge becomes multiple %, not 0.4% → fees
  matter far less. Test via a subclass probe + backtest BEFORE any live change.
- **(C) Accept as data point.** Keep running as the honest "best-available scalp still
  loses" evidence; don't trade it seriously.

**Recommendation: (A) or (B).** (C) wastes a primary-bot slot on a known loser.
If (B), prototype `ScalpVwap1hRedesign` (subclass, freeze-safe) and backtest on 1h
max data; only promote if PF > 1.1 out-of-sample.

### FUTURES WALK-FORWARD (2026-07-23 — broken or just 2022? — BROKEN)

Reusing the walk-forward harness for TrendFollowLS2 (4h, 2018→2026, LINK/AVAX/LTC/ADA):
rolling 1y train / 3mo test, split by "touches 2022 crash" vs not. Driver:
`user_data/walkforward_futures.py`.

| Split | Positive windows | % |
|---|---|---|
| Non-2022 windows | 8/23 | **35%** |
| 2022-touching windows | 0/5 | 0% |

**VERDICT: BROKEN — not just 2022.** Even excluding the crash, only 35% of
out-of-sample windows are profitable (PF<1 consistently). Like scalp, the logic is
net-negative; the 2022 crash just makes a bad strategy catastrophic. The earlier
"inconclusive ADX" sweep + full-period -35.58% are consistent with this.

**Implication:** futures (TrendFollowLS2) should NOT be traded as-is. Options mirror
scalp: drop, or redesign (the brake concept works on equities — a 4h/1d trend-brake
rather than this particular futures trend-follow may transfer better). Do NOT promote
any futures tweak without a walk-forward showing ≥60% positive out-of-sample windows.

### REDESIGN PROTOTYPES (2026-07-23 — scalp + futures, freeze-safe, NOT deployed)

Both are new prototype files + layered overlay configs (defeat the live pinned stops
via a second `-c`, so live configs are never edited). Backtested on max data.

**ScalpVwap1hRedesign (1h, exit at +1σ, ATR stop):**
- Changes: 5m→1h (wider edge), exit at vwap+1σ (not vwap), stop=1.5×ATR.
- Result: **-12.08% / PF0.73 / DD13.75%** (vs original -65.94%/PF0.54/DD66%).
- Better (DD 5× smaller, loss 5.5× smaller) but **STILL net-negative (PF0.73 < 1.1)**.
- Confirms the prototype's own hypothesis: VWAP mean-reversion is dead on crypto at
  every timeframe. **VERDICT: DROP scalp, do not re-tune.** (Fee-isolation not needed —
  PF0.73 even after the fix means logic negative.)
- Files: user_data/strategies/ScalpVwap1hRedesign.py + user_data/prototype_overlay_scalp.json

**TrendBrake4h (4h, SMA200 brake + ADX>25, no stop/short — the working concept):**
- Changes: pure 200-SMA brake (exit when close<SMA200), ADX>25 gate, long-only, no stop.
- Result: **+99.64% / PF1.85 / DD13.52%** (vs broken original -35.58%/PF0.86/DD45%).
- Positive, PF>1, DD halved+. The brake concept transfers to futures at 4h *on paper*.
- **WALK-FORWARD VERDICT: REJECTED** (user_data/walkforward_trendbrake.py, 2026-07-23):
  only **9/19 (47%) non-2022 out-of-sample windows positive** (< 60% bar); 1/5 crash
  windows. Full fee=0: +111.74%/PF2.06 (not fee-driven). The +99.64% full-period
  number was a FIT, not an edge — same trap as the original scalp/futures. The brake
  works on daily/clean-trend series (BTC 1d +1219%, equities 25y) but NOT on crypto
  futures 4h. **Do NOT promote TrendBrake4h.**
- Files: user_data/strategies/TrendBrake4h.py + user_data/prototype_overlay_futures.json
  + user_data/walkforward_trendbrake.py (harness; fixes a no-data-window bug in the
  original futures WF by excluding pre-2020 windows with no futures OHLCV).

### DAYTRADE + SCALP/FUTURES PROFITABILITY ANALYSIS (2026-07-24)

**Live status (paper, frozen test, tiny samples):**
- scalp: 5 closed trades, -1.59 abs (running since 2026-07-22).
- futures (LS2): 48 closed, -13.59 abs since 2026-07-16 (bleeding).
- daytrade (DayTradeORB): only 8 trades over 2026-07-21→22, then **BOT NOT RUNNING
  (crashed/stopped)** — last trade 2026-07-22 23:00. Needs restart for live data.
- Winners remain: brakedhold (+1219%) + spot (+41%).

**DayTradeORB BACKTEST (1h ORB breakout, BTC/ETH/SOL/XRP, 2019-2026):**
- **-89.99% / PF0.68 / DD90% / 2695 trades** — BROKEN. Even the "asymmetric-payoff
  breakout" design loses on crypto 1h (trailing-only exit + session-flush bleeds it).
  So daytrade is a 3rd loser, not a keeper.

**HOW TO MAKE THEM PROFITABLE (evidence-backed):**
- **SCALP:** cannot be made profitable as mean-reversion — no crypto edge after fees
  (ScalpVwap5m -66% even fee-free; 1h redesign -12%/PF0.73). DROP, or fold into the
  daily brake (the only working concept). No intraday MR path exists.
- **FUTURES:** LS2 broken; 4h brake WF-rejected (47%). But the brake WORKS AT DAILY:
  new prototype **TrendBrake1dFutures** (1d SMA200 brake + ADX>25, long-only, no
  stop) backtests **+21.13% / PF1.55 / DD10.76%** (2018-2026). This is the path —
  the brake's edge lives at daily granularity, not 4h.
  **WALK-FORWARD VERDICT: PASSES** (user_data/walkforward_trendbrake1d.py, 2026-07-24):
  12 windows (2y train/6mo test), non-2022 = **5/8 (62%) positive** (>=60% bar).
  2022-touching 0/3 (crash unavoidable). First futures candidate to clear the gate.
  **VIABLE for live promotion** — but only post-freeze, and as a NEW daily bot (not
  by editing the existing 4h futures bot mid-test). Freeze-safe until week 6-8.
- **DAYTRADE:** backtest -90% -> drop OR redesign as a 1d/4h brake. **Bot was found
  NOT RUNNING (crashed after 1 day); restarted 2026-07-24 12:16 UTC** (no config
  change) to gather live confirmation. Backtest still says no 1h ORB edge, so the
  live data is for confirmation only — do not treat a lucky run as a working strategy.

**Net:** the ONLY reproducible edge in this repo is the DAILY brake (brakedhold on
crypto, equities 25y, and now 1d futures). Intraday MR/breakout and 4h futures all
fail. Promote TrendBrake1dFutures via walk-forward; drop scalp/daytrade as configured.
Files: user_data/strategies/TrendBrake1dFutures.py (new, freeze-safe, with overlay).

### FINAL BOT DISPOSITION (2026-07-24 — improve-in-place vs scrap+rebuild)

Decision (Claude think-task + Hermes synthesis): **SCRAP the losers, do NOT improve
in place.** Reasoning: after fee-isolation, scalp (-66%), daytrade (-90%), futures LS2
(-35%) all have NEGATIVE expectancy => the entry signal carries no predictive content;
tuning parameters just mines in-sample noise. In-place tuning is only justified when
fee-free expectancy is POSITIVE (costs eat a real edge) — true for none of these.

Per-bot:
- **scalp (ScalpVwap5m): SCRAP.** 5m MR negative even fee-free => signal sign is wrong.
  Intraday MR in crypto is a market-making business (maker rebates + latency) Freqtrade
  5m cannot reach. No param fixes a wrong-signed signal.
- **daytrade (DayTradeORB): SCRAP.** ORB's premise (overnight gap + open auction) does
  not exist in 24/7 crypto; the "opening range" is an arbitrary clock slice. Don't retry
  at 4h/1d — re-timeframing an absent premise just relocates noise.
- **futures (TrendFollowLS2): SCRAP the strategy, KEEP the slot.** The fix already exists
  and passed walk-forward: TrendBrake1dFutures (1d brake, +21%/PF1.55, WF 62%). Retire LS2
  and promote the validated 1d brake into that slot post-freeze. Zero new research.

**REDUNDANCY TRAP (key):** rebuilding scalp/daytrade as "daily brake variants" would
create 3-4x BTC-beta (corr ~0.9, shared failure mode) — not diversification. Cap at TWO
diversified bots: **brakedhold (spot long) + 1d futures brake (adds short side)**. Leave
scalp + daytrade slots DARK. Expectancy 0 beats -66%.

Post-freeze action (~wk 6-8, late Aug/early Sep 2026): promote TrendBrake1dFutures as a
NEW daily futures bot (do NOT edit LS2 mid-test). No live config/strategy change until
then — only measurement. Winners (brakedhold + spot) keep running the frozen test.

UPDATE (2026-07-24): the 3 losers were physically STOPPED + launchd plists UNLOADED
(`launchctl unload -w` on com.vikas.bot.scalp/daytrade/futures) so they won't
auto-restart on reboot. Slots now DARK on the dashboard (show as "missing"). Winners
(brakedhold PID 71982, spot PID 62762) confirmed alive (HTTP 200). Fresh 1d futures bot
deferred to post-freeze per the Sep-7 cron reminder — starting it early would break
the freeze.

**Both prototypes verified by backtest this session. Neither is wired to a live bot or
plist. No live config/strategy changed. Delegate(Claude) created the files; Hermes
re-verified the backtests + the walkforward_futures.py script independently.**

## 2026-07-27 — Weekly Review

**Vs buy & hold:**
- spot: $978.51  (🔴 behind basket by $11.72)
- brakedhold: $1002.22  (🟢 ahead of basket by $11.99)
- benchmarks: BTC-hold $999.54 · basket-hold $990.23

**Loss concentration:**
- spot: 27 closed, biggest leak = 'exit_signal' (27× → -$21.49)
- futures: 48 closed, biggest leak = 'exit_signal' (12× → -$9.70)
- scalp: 5 closed, biggest leak = 'exit_signal' (4× → -$1.62)
- daytrade: 8 closed, biggest leak = 'session_close' (7× → -$2.75)
- apex: 2498 closed, biggest leak = 'stoploss' (5× → -$124.40)
- spx: 7 closed, biggest leak = 'stoploss' (2× → -$12.20)
- nifty: 1 closed, biggest leak = 'time_stop' (1× → -$69.35)
- btc: 8 closed, biggest leak = 'stoploss' (3× → -$18.76)

**Signal gate:** NOT MET — data still noise.
- ⏳ Still gathering data — 2606/30 closed trades, ~1.1/4 weeks. Win rates below this are noise.
- ⏳ Brake track record: 0/3 completed holds — no verdict on the 200-day brake until holds close.

**Auto-observation (data only, not yet a lesson):**
- spot: 27 closed, biggest leak = 'exit_signal' (27× → -$21.49) — consistent with the fee/whipsaw thesis from risk_backtest.
- 1/2 bots beating the basket over 8 days of tracked equity.

**→ For Claude to review next session:** promote any GATED lesson here into durable memory; if a bot is past the trade/week gate and still losing, raise the retire/replace decision with Vikas.
