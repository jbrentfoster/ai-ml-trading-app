# Risk Management

## Overview

Generating good signals is only half the problem. Position sizing and risk controls determine whether good signals translate into good returns — or whether a bad streak wipes out the portfolio.

The risk layer has five components:

1. **PositionSizer** — how much to buy/sell (Kelly criterion + ATR; realised-Kelly once trade history accumulates)
2. **PortfolioGuard** — seven sequential checks before any order
3. **CircuitBreaker** — automatic trading halt on large daily / weekly losses
4. **TrailingStopManager** — converts bracket take-profits into trailing stops once a long is sufficiently in profit (Phase 3.5, opt-in)
5. **OrderManager** — orchestrates sizing + guard + IBKR submission; handles long-only SELL semantics (close-only)

Before any of these run, signal generation itself drops symbols whose newest cached daily bar is older than `risk.max_bar_staleness_days` (default 3) — week-old prices never reach the order path even if the pipeline missed a run.

---

## Position sizing: Kelly criterion

### The core problem

Given a signal that says "buy AAPL", how many shares should you buy? Too few and gains are negligible. Too many and a single bad trade is catastrophic.

The **Kelly criterion** provides a mathematically optimal answer: the fraction of capital to bet that maximizes long-run wealth growth.

### Full Kelly formula

```
f* = (p × b - q) / b
```

Where:
- **p** = win rate (fraction of past signals that were profitable)
- **q** = 1 - p (loss rate)
- **b** = average win / average loss (the reward-to-risk ratio)

### Example calculation

Suppose recent signal history shows:
- Win rate: p = 0.55 (55% of signals were profitable)
- Average gain: 4%
- Average loss: 2%
- Reward/risk: b = 4/2 = 2.0

```
f* = (0.55 × 2.0 - 0.45) / 2.0
   = (1.10 - 0.45) / 2.0
   = 0.65 / 2.0
   = 0.325  →  32.5% of capital
```

Full Kelly at 32.5% would be an enormous position. In practice, full Kelly leads to excessive volatility and large drawdowns when the model's probability estimates are imperfect (which they always are).

### Fractional Kelly

The project uses **quarter-Kelly** (default):

```
f_applied = kelly_fraction × f*
          = 0.25 × 0.325
          = 0.081  →  8.1% of capital
```

Quarter-Kelly dramatically reduces volatility and drawdowns at the cost of growing wealth roughly half as fast as full Kelly in the best case. This is the standard conservative choice for systematic trading.

### Hard cap

A position can never exceed `kelly_max_position_pct` (default 10%) regardless of Kelly output:

```python
position_pct = min(
    kelly_fraction × kelly_f_star,
    kelly_max_position_pct,          # 10% hard cap
)
```

### Investable equity

Kelly is applied to **investable equity**, not total equity:

```
investable = total_equity × (1 - cash_reserve_pct)
           = $1,000,000 × (1 - 0.20)
           = $800,000
```

With `cash_reserve_pct=0.20`, 20% is always held in cash. This provides a buffer for drawdowns, margin requirements, and opportunities to add positions.

### Realised Kelly vs proxy Kelly

There are actually three sizing paths, picked in priority order:

1. **`kelly_realised`** — once at least `min_trades_for_realised_kelly` (default 30) closed trades exist for the symbol in `trade_log`, Kelly inputs come from *real* outcomes:
   ```
   win_rate     = wins / n_trades
   avg_win_pct  = mean(pnl_pct for winning trades)
   avg_loss_pct = |mean(pnl_pct for losing trades)|
   b            = avg_win_pct / avg_loss_pct
   f*           = (win_rate × b − (1 − win_rate)) / b
   ```
   Negative `f*` is floored to 0 (the long-only system never shorts on a lose-heavy realised history). The walk-forward orchestrator and the live order manager both consume the same helper (`compute_realised_kelly`), filtered to `source='walk_forward'` or `source='live'` respectively.

2. **`kelly_proxy`** — when there aren't yet enough realised trades, fall back to the signal-score proxy: `|ensemble_score|` is used as a stand-in for P(win). This is the cold-start path.

3. **Fixed fallback** — when there's less than `kelly_min_trades=10` of *any* signal history, size to risk exactly 1% of investable equity per trade:
   ```
   risk_per_trade = investable_equity × 0.01
   position_size  = risk_per_trade / stop_distance
   ```
   Where `stop_distance` is derived from ATR (see below). This is why early in the system's life you'll see uniform small positions — the fallback is active until signal history accumulates.

---

## Stop loss and take profit: ATR-based

### What ATR is, in plain English

**ATR (Average True Range)** is a measure of how much a stock typically moves in a single day. Think of it as the stock's "personality":

- A quiet stock like **KO** (Coca-Cola) might have ATR = $1 — on a normal day, its high and low are about $1 apart.
- A volatile stock like **TSLA** might have ATR = $18 — on a normal day, the range between high and low is about $18.
- The "14" in `atr_14` just means it's the average over the last 14 trading days.

More precisely, "true range" on any given day is the largest of:
1. Today's high minus today's low
2. Today's high minus yesterday's close (captures overnight gaps up)
3. Yesterday's close minus today's low (captures overnight gaps down)

ATR is the 14-day average of that. We compute it in `data/indicators.py` and store one value per bar in `indicator_snapshots`.

### Why this matters for stops

Fixed percentage stops (e.g., always stop at -2%) ignore volatility. A 2% stop is tight for TSLA (which easily swings 3% on a normal day — you'd get stopped out by noise) but generous for JNJ (which might only swing 0.5% per day — a 2% stop lets you lose much more than the stock typically moves before reacting).

ATR adapts stops to each symbol's typical daily movement:

```
stop_loss   = entry - ATR × atr_stop_multiplier       (for BUY)
take_profit = entry + ATR × atr_take_profit_multiplier (for BUY)
```

With defaults `atr_stop_multiplier=2.0` and `atr_take_profit_multiplier=3.0`:

```
TSLA: ATR = $18
  Stop:        $391 - 2.0 × $18 = $355  (-9.2%)
  Take profit: $391 + 3.0 × $18 = $445  (+13.8%)

JNJ: ATR = $1.20
  Stop:        $239 - 2.0 × $1.20 = $236.60  (-1.0%)
  Take profit: $239 + 3.0 × $1.20 = $242.60  (+1.5%)
```

TSLA gets a wider stop (9.2% below entry) because its normal daily swing is already huge — a tight stop would fire on routine noise. JNJ gets a tight stop (1.0% below entry) because it barely moves — a wide stop would let losses run past anything "normal" before cutting. Same `atr_stop_multiplier=2.0` setting produces appropriate stops for both.

The reward-to-risk ratio (TP distance / stop distance) is always `atr_take_profit_multiplier / atr_stop_multiplier = 3.0/2.0 = 1.5`. This means even a 40% win rate is theoretically profitable if the losses are consistently contained.

---

## PortfolioGuard: seven sequential checks

Every potential order passes through seven checks in order. If any check fails, the order is REJECTED with a reason recorded in `order_decisions`. (Before the guard runs, `OrderManager.process` itself short-circuits with `REJECTED_TOO_SMALL` if `PositionSizer` returned `< 1 share` — see below.)

### Check 1: Circuit breaker
```python
if circuit_breaker.is_halted():
    return REJECTED, "circuit breaker active"
```
If the circuit breaker is triggered, no new orders are placed. See the circuit breaker section below.

### Check 2: Stop-price sanity
```python
if signal == "BUY" and not (stop_price < entry_price):
    return REJECTED, "stop on wrong side of entry"
if signal == "SELL" and not (stop_price > entry_price):
    return REJECTED, "stop on wrong side of entry"
if entry_price <= 0 or stop_price <= 0:
    return REJECTED, "non-positive price"
```
Catches a NaN ATR (which collapses to `stop == entry`) or a sign-flip in stop placement — either of which would turn the safety stop into an instant or inverse-direction trigger.

### Check 3: Portfolio drawdown
```python
current_drawdown = (peak_equity - current_equity) / peak_equity
if current_drawdown > max_portfolio_drawdown_pct:
    return REJECTED, "portfolio drawdown exceeded"
```
Prevents digging a deeper hole when the portfolio is already down significantly.

### Check 4: Position size
```python
if proposed_position_value / total_equity > max_position_size_pct:
    return REJECTED, "position too large"
```
Hard cap on any single position as a fraction of total equity (default 10%, `kelly_max_position_pct`).

### Check 5: Sector exposure
```python
sector = _SECTOR_MAP.get(symbol)
current_sector_exposure = sum(position_values for symbol in sector)
if (current_sector_exposure + new_position) / equity > max_sector_exposure_pct:
    return REJECTED, "sector exposure limit"
```
Prevents concentration in a single sector (default 30% cap). The `_SECTOR_MAP` in `portfolio_guard.py` covers the entire active universe plus commonly-traded large-caps; unknown symbols pass through with a warning.

### Check 6: Correlation
```python
# Count existing positions with Pearson r >= correlation_threshold (0.7)
n_correlated = count_highly_correlated_positions(symbol, existing_positions)
if n_correlated >= max_correlated_positions:  # default: 3
    return REJECTED, "too many correlated positions"
```
Prevents a portfolio of highly similar positions that all lose together. Pearson correlation is computed over the last 60 bars of daily returns.

### Check 7: Duplicate
```python
if symbol in current_positions:
    return REJECTED, "already holding"

# GOOG/GOOGL are the same underlying company — treated as duplicates
if (symbol == "GOOG" and "GOOGL" in positions) or \
   (symbol == "GOOGL" and "GOOG" in positions):
    return REJECTED, "GOOG/GOOGL duplicate"
```
Prevents double-buying the same (or economically equivalent) position.

---

## Long-only SELL handling

When `trading.allow_short_selling=False` (the default), `OrderManager.process()` intercepts SELL signals **before** sizing and the portfolio guard:

- **SELL after long held** → `_close_long_position()` market-sells to flatten. Decision recorded as `CLOSED_LONG`. Closing reduces risk; sizing and the guard are bypassed.
- **SELL from flat** → no order placed. Decision recorded as `REJECTED_NO_POSITION`.

Shorts are never opened. The same gate is enforced inside the walk-forward bracket simulator so backtest P&L reflects the live execution path — a 2026-04-30 audit found 66% of unfiltered WF trades were short opens that the live runner would never have executed.

## Sizing short-circuit: `REJECTED_TOO_SMALL`

`OrderManager.process()` checks `pos_size.shares < 1` immediately after `PositionSizer.calculate()` and returns `REJECTED_TOO_SMALL` without calling the PortfolioGuard or submitting any IBKR order. This covers (a) Kelly/fixed sizing producing `position_value < entry_price` for a high-priced stock, and (b) `_get_latest_close()` returning 0 because no bars were cached. Without this check, a 0-share "APPROVED" decision would land in `order_decisions` and IBKR would either reject or silently no-op a 0-share bracket order.

---

## Circuit breaker

The circuit breaker is a trading halt that activates automatically when losses exceed defined thresholds, and resets automatically after a cooling-off period.

### Trigger conditions

```python
if daily_loss_pct >= circuit_breaker_daily_loss_pct:    # default: 3%
    circuit_breaker.trigger("Daily loss limit exceeded")

if weekly_loss_pct >= circuit_breaker_weekly_loss_pct:  # default: 7%
    circuit_breaker.trigger("Weekly loss limit exceeded")
```

### How the loss percentages are computed

`signal_runner.py` Phase 1 pulls `realized_pnl + unrealized_pnl` from `IBKRConnection.get_account_summary()` each run and compares it against an equity baseline cached in the `equity_snapshots` table. The first live run only seeds the baseline; from run #2 onward the comparison is automatic — no manual click required to trip the halt. (Intra-day triggering via `reqPnL` streaming is on the roadmap but not yet wired up.)

### State persistence

The circuit breaker state is stored in `circuit_breaker_log` (SQLite), not in memory. This means:
- The dashboard, signal runner, and scheduler all share the same state
- The state persists across restarts — a triggered breaker stays triggered even after rebooting
- Multiple processes can't create a race condition by reading stale in-memory state

### Auto-reset

```python
if triggered_at + timedelta(hours=reset_hours) < now:   # default: 24h
    circuit_breaker.auto_reset()
```

After `circuit_breaker_reset_hours` (default 24), the breaker resets automatically. This provides the cooling-off period but doesn't require manual intervention for routine daily-loss events.

### Dashboard controls (Page 8)

The circuit breaker status is shown prominently at the top of Page 8:
- **Green banner**: trading enabled
- **Red banner**: halted, with reason and trigger time

Sidebar buttons allow manual trigger and reset — useful for testing or if you want to pause trading manually.

---

## OrderManager

`OrderManager` orchestrates the full decision process for each signal:

```python
def evaluate(signal, symbol, entry_price):
    # 1. Size the position
    position_size = sizer.compute(symbol, entry_price, signal_history)

    # 2. Run through portfolio guard
    guard_result = portfolio_guard.check(symbol, position_size)

    if not guard_result.approved:
        return OrderDecision(decision="REJECTED", reason=guard_result.reason)

    if dry_run:
        return OrderDecision(decision="DRY_RUN")

    # 3. Submit bracket order to IBKR (only if paper_orders_enabled=True)
    order = ibkr.place_bracket_order(
        symbol=symbol,
        action="BUY" if signal == "BUY" else "SELL",
        quantity=position_size.shares,
        entry_price=entry_price,
        stop_loss_price=position_size.stop_price,
        take_profit_price=position_size.take_profit_price,
    )
    return OrderDecision(decision="APPROVED", order_id=order.order_id)
```

All decisions — APPROVED, REJECTED, and DRY_RUN — are persisted to `order_decisions` with full detail: shares, entry, stop, take profit, position value, and reject reason.

---

## Bracket orders

When orders are submitted to IBKR, they're placed as **bracket orders** — three linked legs that together form a complete trade plan: "here's what I'll pay, here's where I bail out, here's where I take profits."

```
Entry order:       BUY 10 AAPL LIMIT @ $266.00
  └─ Stop loss:    SELL 10 AAPL STOP @ $254.00  (auto-cancels if entry fills)
  └─ Take profit:  SELL 10 AAPL LIMIT @ $286.00  (auto-cancels if entry fills)
```

### What each leg does

- **Entry (parent):** a LIMIT order — "buy 10 shares at $266 or cheaper." If price spikes above $266 before we fill, we don't chase; the order just sits unfilled. Once it fills, we own the position.
- **Stop loss (child):** a STOP order — "if price falls to $254, sell at the market." Defines the maximum loss before we enter.
- **Take profit (child):** a LIMIT order — "if price rises to $286, sell at that price." Defines the profit target.

### How IBKR ties them together

Once the entry fills:
- If price falls to the stop → stop loss executes, take profit auto-cancels.
- If price rises to the take profit → take profit executes, stop loss auto-cancels.

The two exit legs are linked by IBKR's **OCA** (One-Cancels-All) group — when one fills, the server cancels the other automatically. This prevents the nightmare scenario where both fire and you end up accidentally short.

### Two small wrinkles we fixed

Two IBKR quirks require explicit handling in `IBKRConnection.place_bracket_order`:

1. **Tick-size rounding**: IBKR rejects orders with prices finer than $0.01 (error 110). `ib_insync` passes floats through a 32-bit wire format that can drift a $202.52 limit to $202.52000427246094, tripping the check. We round entry/stop/TP to `round(price, 2)` before placing.
2. **GTC on every leg**: The default time-in-force is DAY, which cancels any unfilled leg at the 4 PM close. If the signal runner fires outside regular trading hours (or the bracket sits overnight waiting for a take-profit), DAY-TIF legs silently disappear. We set `leg.tif = "GTC"` on all three legs so they survive until filled or explicitly cancelled.

---

## Trailing stops (Phase 3.5 — opt-in)

A fixed take-profit has a built-in problem: it **caps** your upside. If you buy AAPL at $266 with a TP at $286, and AAPL rockets to $320, you still sold at $286. The `$34` of additional gain goes to whoever bought from you.

A **trailing stop** solves this by replacing the fixed take-profit with a stop that:
- Starts some distance below the current price.
- **Ratchets up** as the price rises (never moves down).
- Triggers when price falls back by the trail distance from its peak.

The phrase is literal: the stop "trails" the price upward. You only exit when the trend reverses, not when you hit an arbitrary target.

### How the conversion works

The signal runner's new Phase 3.5 (`risk/trailing_stop.py`) walks every open long position each day and decides whether to convert its bracket's take-profit into a trailing stop. Two config settings drive the decision:

- **`trailing_stop_activation_atr` (default 2.0)** — how much profit is required before conversion activates, in ATR units.
- **`trailing_stop_trail_atr` (default 2.0)** — the trailing distance, in ATR units.

When `current_price ≥ entry + activation_atr × ATR`, the manager:

1. Cancels the bracket's LMT take-profit leg.
2. Cancels the bracket's STP stop-loss leg.
3. Submits a new standalone GTC `TRAIL` order with `auxPrice = trail_atr × ATR`.

### Worked example — AAPL with default 2.0 / 2.0

Say you bought 10 shares of AAPL at $266 and AAPL's ATR is $3.

| Price reaches | What happens |
|---|---|
| $266 → $270 | Nothing (haven't hit activation at $266 + 2 × $3 = $272) |
| $266 → $272 | **Convert.** TP at $286 cancelled. STP at $260 cancelled. New trailing stop placed $6 below market → stop sits at $266 (break-even). |
| $272 → $310 | Trailing stop ratchets up with price → stop is now at $304. |
| $310 → $304 | Stop triggers. Sold at $304. Locked in $38/share instead of the original $20 take-profit at $286. |

With defaults matching (2.0 / 2.0), the trailing stop sits **at entry** the moment it activates — guaranteed break-even protection. If you use `activation=3.0 / trail=2.0`, the stop lands at entry + 1×ATR (locked-in profit at activation). If you use `activation=1.0 / trail=2.0`, the stop lands at entry - 1×ATR (still a small loss possible, but better than the original bracket stop).

### Why not just use a trailing stop from the start?

You could submit a trailing stop at entry time instead of a fixed bracket. The tradeoff:

- **Bracket with fixed TP**: guaranteed risk/reward ratio (1.5× reward-to-risk with defaults), but caps upside at the TP level.
- **Trailing from entry**: unlimited upside, but the initial stop is much closer to entry (giving routine volatility a chance to stop you out on the same day you entered).

The conversion approach gets both: the bracket protects you through the noisy early phase of the trade, and once you've earned some buffer (the activation threshold), we switch to "let winners run" mode. This is the classic tradition of Darvas/Livermore-style trend-following adapted into an automated rule.

### Conversion ordering — why Cancel-TP → Cancel-STP → Submit-TRAIL

The order of those three IBKR calls matters and is a deliberate safety trade-off (see `risk/trailing_stop.py:7-21`):

- Canceling the TP first removes the upside cap without losing the downside stop.
- Canceling the STP second leaves a sub-second window where the position has **no** stop — acceptable in normal markets, the cost of ordering operations sequentially.
- Submitting the TRAIL last re-establishes downside protection.

The alternative order (submit TRAIL first, cancel bracket legs after) would create a more dangerous window: the STP and TRAIL live briefly in **different** OCA groups, so a severe gap-down could trigger both and leave the account short by the position size. Having a short window of "no stop" is strictly safer than a short window of "two overlapping stops on unrelated OCA groups."

### When Phase 3.5 runs

Phase 3.5 is **opt-in** — three things must all be true:

1. `trading.paper_orders_enabled = True` (or LIVE mode)
2. `risk.trailing_stop_enabled = True`
3. The signal runner is NOT in `--dry-run` mode

Otherwise the phase returns immediately with no output. It's idempotent: positions that already have a TRAIL order open are skipped, so running the phase multiple times per day (e.g., if you manually re-run `signal_runner.py`) doesn't double-convert or damage anything.

Short positions are skipped — this codebase is long-only (`allow_short_selling=False` by default). If short selling is ever enabled, a symmetric BUY-side trailing stop path would need to be added.

---
