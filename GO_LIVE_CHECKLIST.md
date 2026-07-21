# Go-Live Checklist — CryptoBot

**Status as of 2026-07-20: NOT live. All 3 bots are `dry_run: true` with no API keys.**
This is the ordered path *if* you ever decide to trade real money. It is **not** a recommendation
to do so now — the honest verdict remains *preserver, not income*, and your bigger opportunity is
building a crypto product, not trading this small account. Work top to bottom; **do not skip a phase.**

---

## Phase 0 — The evidence gate (spend $0 until this passes)
The strategy has not earned real money yet. Current paper record ≈ **1 win / 32 trades**.

- [ ] **Signal gate cleared** (from `weekly_review.py`): **≥30 closed trades**, **≥4 weeks** of live paper
      data, **≥3 completed brake hold episodes**. Below this, results are NOISE — the code says so itself.
- [ ] `/review` (or the Sunday `weekly_review`) prints a **positive, gated verdict** — not "⏳ still gathering data."
- [ ] The paper equity curve beats (or at least matches) simple **buy-and-hold** over the same window.
      If it doesn't beat buy-and-hold on paper, live money is strictly worse (fees + tax make it worse still).

> If Phase 0 never passes, that IS the answer: keep it as a paper lab. Stop here.

---

## Phase 1 — Decide scope (smallest possible)
- [ ] **Pick ONE bot only.** Recommended: `brakedhold` (spot, binanceus) — the cleanest, most
      India-tax-compatible sleeve. **Do NOT go live with the futures/OKX bot** (leverage = fastest way to
      lose a small account; also the worst India-tax fit).
- [ ] **Decide a tiny amount you can 100% afford to lose** (think "tuition," not "investment"). Start
      with the minimum that lets a trade execute — not a meaningful sum.
- [ ] Confirm the `-27%` hard stoploss decision. Live stoploss is **-0.27** (not the -0.10 in code) and
      looks overfit. Decide deliberately: keep, or set a real value, before any live trade.

---

## Phase 2 — Security hardening (before real keys ever touch this machine)
Real API keys turn this laptop into a target. Close the audit's deferred "before-live" items:

- [ ] **Dashboard/API auth** — the local dashboard (`:8090`) and freqtrade REST APIs (`:8080/8082`)
      currently use weak/basic creds fine for paper. Set strong passwords + JWT before real keys exist.
- [ ] **Machine hygiene** — full-disk encryption ON (FileVault), OS + Python deps patched, no untrusted
      software running alongside.
- [ ] **`telegram.conf` stays `chmod 600`**; confirm no secrets are in any file that could sync to git/cloud.
- [ ] **Never** commit keys. Keep them out of `config*.json` history.

---

## Phase 3 — Exchange account + funding
- [ ] **KYC-verified account** on the chosen exchange (binanceus for the brakedhold bot). Note: your US IP
      is why binance-global 451s — binanceus is the working venue.
- [ ] Fund it with **only** the Phase-1 tiny amount, in the bot's `stake_currency` (**USDT**).
- [ ] Verify you can withdraw a small test amount back out (prove the off-ramp works BEFORE relying on it).

---

## Phase 4 — API keys (least privilege)
- [ ] Create keys with **trade permission ONLY**. **Withdrawal permission = OFF.** (If keys leak, a thief
      can trade but cannot drain funds.)
- [ ] **IP-allowlist** the keys to this machine's IP.
- [ ] Paste into `config_braked.json` → `exchange.key` / `exchange.secret` (currently placeholders).

---

## Phase 5 — Config flip + sizing
- [ ] Set `"dry_run": false` in `config_braked.json` **only** (leave the others paper).
- [ ] **Cut sizing down:** `tradable_balance_ratio` is 0.99 (99% deployed) — for first-live set it much
      lower. `max_open_trades` is 12 on brakedhold — reduce so each position is tiny.
- [ ] Keep the **exposure cap at 4** (correlation: crypto coins move together ~0.68+, so 12 "positions"
      is really ~2-3 bets). Don't raise it for live.
- [ ] Re-run a paper session with the NEW sizing to confirm nothing breaks, THEN flip dry_run.

---

## Phase 6 — India tax & compliance
- [ ] Understand the cost: **30% flat tax on gains, 1% TDS deducted on every sell, NO loss offset.**
      Fees + 1%-per-sell + 30% mean the strategy must clear a high bar just to break even after tax.
- [ ] Set up **record-keeping** for every trade (date, pair, buy/sell, INR value) for filing.
- [ ] Confirm you're comfortable that a churny strategy (many sells) is heavily penalized by 1% TDS —
      this favors the low-turnover `brakedhold` approach over active trading.

---

## Phase 7 — Controlled first-live rollout
- [ ] Go live **during waking hours** you can watch. Do not flip it and walk away.
- [ ] Confirm the **guardian circuit breaker** is armed: halts all trading if combined balance drops
      **>10% from peak** on 2 consecutive polls (manual `/reset` to resume). Verify it's running.
- [ ] Watch the **first real trade** end-to-end: entry fills, stop is set, Telegram alert fires, dashboard
      + `/stats` reflect it. One clean real trade before trusting it unattended.
- [ ] **Kill switch rehearsed** (see below) — know how to stop it in 5 seconds before you need to.

---

## Phase 8 — Ongoing
- [ ] Weekly `/review` — is live tracking paper? If live underperforms paper, **stop** (slippage/fees real).
- [ ] Re-check the gate monthly; be willing to go back to paper.
- [ ] Keep the amount tiny until there's a long, boring, *profitable-after-tax* live track record.

---

## KILL SWITCH — how to stop everything (memorize this)
```bash
# stop the live bot immediately
launchctl bootout gui/$(id -u)/com.vikas.bot.brakedhold
# or flip it back to paper: set "dry_run": true in config_braked.json, then restart
# guardian auto-halts at >10% drawdown; manual halt via Telegram /pause
```
Funds stay on the exchange (keys can't withdraw). Stopping the bot stops new orders; existing
positions remain until you close them manually on the exchange.

---

## Honest cost summary
| | Cost |
|---|---|
| Phase 0 (evidence) | $0 — just time; may never pass, and that's fine |
| Real money at risk | Only what you decide in Phase 1 — treat as fully losable |
| India tax drag | 30% on gains + 1% per sell + no loss offset |
| The real question | Is a proven, tiny, after-tax edge worth your attention vs. building a product? |

**Default recommendation: stay in paper. Go live only if Phase 0 genuinely passes AND you still want to.**
