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
