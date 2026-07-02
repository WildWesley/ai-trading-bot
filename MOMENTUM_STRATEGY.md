# Flagship Strategy — Concentrated Momentum Rotation (LOCKED 2026-07-01)

The result of the 2026-07-01 strategy study (10 classic strategies + congress
copy-trade + exit-algorithm sweep + long-window 2019–2026 validation). This is
the recommended replacement for the current 5-minute RSI intraday bot. It is a
**different machine**: long-only, low-turnover, weekly rebalance — not an
intraday signal bot. Adopting it is a rebuild, not a config tweak.

## The rules (locked)

1. **Universe:** liquid large-caps (~325 names, the current stock watchlist —
   an S&P-500-ish set that rarely delists).
2. **Signal — 12-1 momentum:** rank every name by its return from ~6 months ago
   to ~1 week ago (`close[t-5] / close[t-126] - 1`). Skipping the last week
   avoids short-term reversal noise.
3. **Selection:** hold the **top 15** by momentum that are **also above their
   own 200-day SMA** (absolute-trend filter — never hold a downtrending name).
   Fewer than 15 eligible → the remainder sits in cash.
4. **Weighting:** equal weight (~6.7% each).
5. **Rebalance: WEEKLY.** ← the locked-in edge. Weekly (vs monthly) rotates out
   of fading leaders and into new ones fast enough to survive factor reversals
   like 2021, with no cost to the rest of the record. Faster than weekly
   (daily) makes it worse; slower (monthly) leaves the 2021 hole.
6. **Exit ("when needed", no RSI):** at each weekly rebalance a name is sold if
   it (a) drops out of the top 15, or (b) falls below its 200-day SMA. That's
   the entire exit — a trend/relative-strength rule that lets winners run for
   months. RSI-style exits were tested and rejected: they sell winners early.
7. **No leverage. No crypto. No SPY market-timing filter** (it whipsawed — hurt
   returns without cutting drawdown).
8. **Universe stays large-cap — do NOT broaden.** Tested (`bt_universe.py`): a
   broad ~1000-name universe (even liquidity-filtered to >$100M/day) added ~8pts
   CAGR but pushed MaxDD −37% → **−50%** with Sharpe unchanged (~1.35). More
   return, equally more pain, no risk-adjusted gain — and still survivorship-
   inflated. Rejected 2026-07-01: the large-cap universe's −37% drawdown is the
   ceiling we want to keep.

### Optional risk dial — volatility targeting (no leverage)
Scale total exposure by `clip(target_vol / realized_vol, 0, 1.0)` using 21-day
realized vol (decided on prior day's data). Trades some upside for a much
smoother ride and is expected to help most in a real momentum crash:
- **25% target →** ~lower return, MaxDD ~−24% (calmest)
- **30% target →** ~mid, MaxDD ~−28% (balanced)
- **off (1.0) →** max return, MaxDD ~−36–38%
Pick per risk tolerance. Default recommendation if unsure: **off** for max
growth, or **30%** if a −38% drawdown would make you bail.

## Backtested performance (weekly rebalance, no vol-target)

| Window | CAGR | Sharpe | MaxDD | 2021 (stress) |
|---|--:|--:|--:|--:|
| 2019-10 → 2026-07 (incl. COVID + 2022 bear) | ~44% | 1.24 | −36% | +3% |

Beat SPY (~16% CAGR) and every buyable momentum ETF over the full cycle, and
protected in 2022 (−8% vs SPY −20%).

## Honest expectations & caveats (READ THIS)

- **These CAGR figures are survivorship-inflated** (universe = *today's* list of
  winners). The RELATIVE findings (momentum > index; weekly > monthly; trend
  exit > RSI exit) are trustworthy; the ~44% level is NOT a forward forecast.
- **Realistic forward expectation ≈ 16–23%/yr** — the range the real, buyable
  momentum ETFs (MTUM 17%, SPMO 23%) delivered survivorship-free over 2019–2026.
- **Momentum has dead years** (2021: it can go nowhere while the index soars)
  and **~−35% drawdowns**. You must be able to hold through both without bailing.
- **The zero-effort alternative is just buying SPMO** (S&P 500 Momentum ETF):
  ~90% of this idea, survivorship-free, diversified, no maintenance. The custom
  bot's excess over SPMO is largely the survivorship/concentration mirage.

## Reproducibility
Tooling in repo: `bt_momentum.py` (`backtest(C, N=15, market_filter=False,
lookback=126, rebal=5, skip=5)` = the locked config), `bt_long.py`
(2019–2026 vs ETFs), `bt_tweak.py` (rebalance/lookback/concentration sweep),
`bt_riskmgmt.py` (vol-target overlay). Data cached in `data/bars_long/`.
