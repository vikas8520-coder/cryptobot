#!/usr/bin/env python3
"""
Funding Monitor — watches crypto perpetual funding rates and tells you whether
the market-neutral CARRY trade (long spot + short perp, collect funding) is
currently worth deploying.

Background (from our 2021-2026 analysis): carry is regime-dependent.
  bull euphoria -> fat funding (2021 hit ~37% gross / ~18% net)
  bear/quiet    -> thin or negative (2026 ~0%)
This tool tells you which regime you're in RIGHT NOW, so you know when to act.

Yield math: OKX funding pays every 8h (3x/day). Gross annualized = mean_rate*3*365.
Delta-neutral splits capital ~50/50 (spot + margin), so NET on your capital is
roughly HALF the gross, minus ~1%/yr costs.

Usage:  python funding_monitor.py            # one-shot status
Exit code 0 always; prints ALERT line when carry crosses the attractive threshold.
"""
import requests

COINS = ["BTC", "ETH", "SOL", "XRP", "ADA", "LTC"]
LOOKBACK = 30            # funding events to average (30 * 8h = 10 days)
HIST = "https://www.okx.com/api/v5/public/funding-rate-history?instId={c}-USDT-SWAP&limit={n}"

# thresholds on GROSS annualized carry (net ~= half of these)
STRONG = 20.0   # ~10%+ net  -> genuinely attractive
MODEST = 10.0   # ~5-10% net  -> worth watching


def annualized_gross(coin):
    """Return (gross_annualized_pct, pct_positive) from recent funding history."""
    r = requests.get(HIST.format(c=coin, n=LOOKBACK), timeout=15)
    data = r.json().get("data", [])
    rates = [float(d["realizedRate"]) for d in data if d.get("realizedRate")]
    if not rates:
        return None, None
    mean = sum(rates) / len(rates)
    ann = mean * 3 * 365 * 100          # 3 pays/day
    pct_pos = sum(1 for x in rates if x > 0) / len(rates) * 100
    return ann, pct_pos


def main():
    print("=" * 58)
    print("  FUNDING CARRY MONITOR  (OKX perps, last ~10 days)")
    print("=" * 58)
    print(f"  {'Coin':5} {'gross ann.':>11} {'~net on cap':>12} {'funding +ve':>12}")
    print("  " + "-" * 52)
    grosses = []
    for c in COINS:
        ann, pos = annualized_gross(c)
        if ann is None:
            print(f"  {c:5} {'(no data)':>11}")
            continue
        net = ann / 2 - 1.0             # half gross, minus ~1% costs
        grosses.append(ann)
        print(f"  {c:5} {ann:>10.1f}% {net:>11.1f}% {pos:>11.0f}%")
    print("  " + "-" * 52)

    if not grosses:
        print("  No funding data returned.")
        return
    avg = sum(grosses) / len(grosses)
    avg_net = avg / 2 - 1.0
    print(f"  {'AVG':5} {avg:>10.1f}% {avg_net:>11.1f}%")
    print()

    if avg >= STRONG:
        print(f"  🟢 STRONG CARRY — avg gross {avg:.1f}% (~{avg_net:.1f}% net).")
        print("     Market-neutral yield is attractive. Worth considering IF you")
        print("     have capital. This is the 'deploy' regime.")
        print(f"  >>> ALERT: carry crossed the attractive threshold ({STRONG:.0f}% gross) <<<")
    elif avg >= MODEST:
        print(f"  🟡 MODEST CARRY — avg gross {avg:.1f}% (~{avg_net:.1f}% net).")
        print("     Positive but thin. Watch it; not compelling yet.")
    else:
        print(f"  🔴 SKIP — avg gross {avg:.1f}% (~{avg_net:.1f}% net).")
        print("     Too thin to justify the complexity/costs. Wait for a fatter")
        print("     regime (typically bull euphoria). Do nothing.")
    print("=" * 58)


if __name__ == "__main__":
    main()
