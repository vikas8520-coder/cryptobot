# NIFTYBEES offline backtest harness (freeze-safe)

Standalone, dependency-light (pandas/numpy) backtester for the Nifty 50 paper bot.
Built during the 2026-07 freeze — it **does not touch** `config_nifty.json`,
`nifty_engine.py` or `spx_strategy.py`; it only reads the pre-exported CSVs in
`../data/`. No network calls, fully deterministic.

## Run

```
cd nifty_backtest
/Users/vikasreddy/cryptobot/.venv/bin/python3 run_backtest.py
```

Prints a comparison table and writes `results.json`.

## Method

- Bar-driven, long-only, one position, next-bar-close execution (no look-ahead).
- 200-day SMA "brake" computed on DAILY closes gates entries on all braked variants.
- Hard 5% stop checked against intrabar Low, filled at the stop price (conservative).
- Fees per side: 5 bps (as configured) and a realistic-India case of ~12 bps round trip.
- Sharpe from daily returns of the strategy equity curve; CAGR over the variant's window.
- Data fix: the exported daily CSV has 2 corrupted rows around the 2019-12-19 1:10
  split (a 10x price blip, not a level shift). `load_csv` detects and rescales it.

## Variants

| Variant | What it is | Data |
|---|---|---|
| B&H | Buy & hold NIFTYBEES benchmark (full history and last-1y) | daily |
| V1 CURRENT | SMA(10/30) hourly cross + 200DMA brake + 5% stop + time-stop. `max_hold_cycles=72` at ~5 s/cycle ≈ 6 min, modelled as a 1-bar timeout on hourly data | hourly (1y) |
| V2 | Same as V1 but time-stop disabled | hourly (1y) |
| V3 | Daily braked-hold: enter Close>200DMA, exit Close<200DMA, 5% stop | daily (2009–2026) |
| V3-nostop | V3 without the 5% stop | daily |
| V4a | Daily SMA(20/50) cross + 200DMA brake + 5% stop | daily |
| V4b | Daily SMA(50/200) golden cross + 5% stop | daily |

## Results (2026-07-25 run)

```
variant                                                     total%   CAGR%   maxDD%  trades  win%   PF     Sharpe  tim%
B&H NIFTYBEES (daily, full)                                 769.09   13.11   -36.34    1     100    inf    0.79    100.0
B&H NIFTYBEES (last 1y)                                      -2.71   -2.72   -14.82    1       0    0.0   -0.16     99.2
V1 CURRENT hourly 10/30 +brake +stop +1-bar timeout (5bps)   -1.07   -1.07    -1.11   16    43.8   0.50   -0.86      0.9
V1 CURRENT (12bps RT)                                        -1.38   -1.39    -1.40   16    25.0   0.41   -1.10      0.9
V2 NO timeout (5bps)                                          0.68    0.68    -7.91   16    43.8   1.10    0.17     31.2
V2 NO timeout (12bps RT)                                      0.36    0.36    -8.10   16    43.8   1.06    0.10     31.2
V3 daily 200DMA braked-hold +5% stop (5bps)                  23.08    1.19   -38.50   79    29.1   1.35    0.16     53.2
V3 (12bps RT)                                                21.15    1.10   -39.07   79    29.1   1.33    0.15     53.2
V3-nostop (no 5% stop, 5bps)                                170.03    5.82   -23.00   79    32.9   2.63    0.54     68.5
V4a daily 20/50 cross +brake +stop (5bps)                     7.92    0.44   -32.84   26    26.9   1.22    0.10     33.7
V4b daily 50/200 golden cross +stop (5bps)                   12.27    0.66   -24.73   11    27.3   1.47    0.13     38.0
```

## Findings (plain English)

1. **The 72-cycle time-stop kills the current strategy.** 72 engine cycles ≈ 6
   minutes of wall clock — the bot exits almost immediately after entering, so
   V1 is just paying fees for noise (PF 0.50, negative return, 0.9% time in
   market). Removing it (V2) flips the same signals to roughly breakeven-positive.
   If this system is meant to swing-trade, `max_hold_cycles` as configured is a bug
   in intent, not a parameter choice.
2. **The 5% hard stop badly hurts a daily 200DMA system.** V3 with the stop:
   1.2% CAGR, PF 1.35. Same rule without the stop: 5.8% CAGR, PF 2.63, and a
   *smaller* max drawdown (−23% vs −38.5%) — the stop repeatedly sells local dips
   above the 200DMA and re-buys higher. For a slow trend filter, the filter
   itself is the stop.
3. **Nothing here beats buy & hold** over 2009–2026 (B&H 13.1% CAGR, −36% maxDD).
   The best braked variant (V3-nostop) gives ~44% of B&H's CAGR with ~2/3 of the
   drawdown. The 200DMA brake is a drawdown reducer, not a return enhancer, on
   an index that mostly went up.
4. **Fees matter but aren't the story.** 5 bps → 12 bps round trip shaves a few
   tenths of a percent off each variant; the ranking never changes.
5. **The last year (hourly window) was flat-to-down** (B&H −2.7%), so V2's
   +0.7% while in the market only 31% of the time is actually respectable —
   but 16 trades in one year is too small a sample to conclude anything.

**Bottom line:** the current config as deployed (V1) loses money by construction
because of the time-stop. The cheapest paper-only fix to *test* next is
`max_hold_cycles=0`; the more interesting direction is the daily braked-hold
without the hard stop. All of that stays paper/review-only per the freeze.
