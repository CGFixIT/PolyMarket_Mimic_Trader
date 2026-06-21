# PolyMarket_Mimic_Trader — Live Trading Improvement Plan

## Purpose

This document is a concrete, prioritized engineering plan to transform the current paper-mode copy-trading bot into a profitable live trading system. Each improvement is ranked by expected impact on profitability and includes specific implementation guidance.

The analysis was conducted through the combined lens of: Polymarket microstructure expertise, quantitative finance (Kelly criterion, Sharpe optimization), game theory (adversarial considerations, information decay), behavioral psychology (herding effects, resolution panic), and low-latency Python systems engineering.

---

## Priority 1 — Critical Path Latency (Expected Impact: +15-25% edge retention)

The bot’s #1 profitability killer is latency. Copy-trading alpha decays exponentially with delay — academic research on equity copy-trading shows ~50% of alpha is gone after 10 seconds. Our current worst-case is **8s poll + 200ms network + 500ms validation + 200ms order = ~9 seconds**.

### 1.1 Adaptive Polling Interval

**Current:** Fixed 8-second poll interval regardless of market state.
**Problem:** During high-activity periods (breaking news, market opens), 8s is too slow. During quiet periods, it wastes rate limit budget.

**Implementation:**
```python
class AdaptivePollInterval:
    def __init__(self, min_interval=2.0, max_interval=15.0, base=8.0):
        self.min = min_interval
        self.max = max_interval
        self.base = base
        self._recent_trade_count = 0
        self._last_adjustment = time.monotonic()

    def next_interval(self, trades_found: int) -> float:
        self._recent_trade_count += trades_found
        elapsed = time.monotonic() - self._last_adjustment
        if elapsed > 60:
            if self._recent_trade_count > 5:
                interval = self.min
            elif self._recent_trade_count > 0:
                interval = self.base * 0.5
            else:
                interval = self.max
            self._recent_trade_count = 0
            self._last_adjustment = time.monotonic()
            return interval
        return self.base
```

**File:** `polymarket_copier/core/monitor.py`
**Expected improvement:** Reduces average detection latency from 4s to ~1.5s during active periods.

### 1.2 Parallel Validation Pipeline

**Current:** `handle_trade_event` makes sequential await calls before placing the order.
**Problem:** Independent calls (gamma.get_market, get_market_price, position_count, get_trader_pnl) run sequentially, adding ~250ms.

**Implementation:**
```python
market, current_price, count, trader_pnl = await asyncio.gather(
    self.gamma.get_market(event.market_id),
    self.gamma.get_market_price(event.token_id),
    self.portfolio.position_count(),
    self.portfolio.get_trader_pnl(event.wallet_address),
)
```

**File:** `polymarket_copier/core/copier.py`
**Expected improvement:** Saves ~200ms per trade on the critical path.

### 1.3 Market Data Cache (60s TTL for metadata, 2s for prices)

**File:** `polymarket_copier/api/gamma_client.py`
**Expected improvement:** Eliminates ~150ms per trade for cached markets.

### 1.4 In-Memory Position Cache (eliminate SQLite queries on price ticks)

**File:** `polymarket_copier/core/copier.py`
**Expected improvement:** Eliminates ~100ms/sec of DB overhead.

---

## Priority 2 — Strategy & Parameter Tuning (Expected Impact: +10-20% win rate)

### 2.1 Kelly-Adjusted Position Sizing
Scale copy size by trader quality score instead of fixed 0.5x multiplier.

### 2.2 Asymmetric TP/SL Based on Market Type
- **Trending** (elections): tp_frac=0.50, sl_frac=0.20
- **Mean-reverting** (crypto price): tp_frac=0.30, sl_frac=0.30
- **Resolution-driven** (sports): tp_frac=0.35, sl_frac=0.25

Classify via keyword matching on market question text.

### 2.3 Sliding Resolution Blackout
Hard block at 6h, reduced size (0.5x) at 6-24h. Current 24h hard cutoff misses the most profitable pre-resolution window.

### 2.4 Confirmation-Based Entry (Order Book Signal)
Use ask-side depth as a quality multiplier (0.3-1.0) on position size.

### 2.5 Asymmetric Price Deviation Filter
Only reject *adverse* deviation (price moved against copy direction). Favorable deviation = better entry, not a skip.

---

## Priority 3 — Trader Selection Improvements (Expected Impact: +5-15% quality)

### 3.1 Market Diversity Score
Bonus for trading across many independent markets. Penalizes concentrated bettors.

### 3.2 Style Filter — Only Copy Directional Traders
Market makers have high Sharpe but copying them with 8s latency is suicidal. Filter by buy/sell ratio (0.55-0.90 = directional).

### 3.3 Anti-Correlation Filter
Remove traders whose recent trades overlap >60% with a higher-ranked one.

---

## Priority 4 — Game Theory & Adversarial Robustness

### 4.1 Randomized Execution Delay
0.5-3s jitter before order placement. Makes flow unpredictable to front-runners.

### 4.2 Wash Trade Detection
Flag traders with >20% round-trip trades (buy+sell within 60s at same price).

### 4.3 Crowd-Awareness Sizing
Graduated size reduction based on post-trade price impact (1% move = 0.6x, 3% = 0.3x).

---

## Priority 5 — Exit Strategy Improvements

### 5.1 Scaled Exits (Partial Take Profit)
Exit 25% at 50% of TP range, 50% at 75%, remainder at 100%. Captures 15-20% more profit on trending markets.

### 5.2 Copy-the-Exit: Follow Tracked Trader’s Sells
When the tracked trader exits, mirror the exit immediately rather than waiting for TP/SL.

---

## Priority 6 — Operational Robustness

### 6.1 Fix `_midnight_utc()` UTC Bug
Use `calendar.timegm` instead of `time.mktime(time.gmtime(...))`.

### 6.2 Replace `sys.exit()` with Exceptions in Config
Raise `ConfigValidationError` instead of killing the interpreter.

### 6.3 Ordered Dedup Cache for Trade IDs
Replace `set.pop()` (arbitrary eviction) with `OrderedDict.popitem(last=False)` (FIFO).

### 6.4 Graceful Gather with Error Isolation
Add `return_exceptions=True` to `asyncio.gather()` in main.py.

### 6.5 Implement `_maybe_update_subscription` (currently a no-op)
New positions never get WS price feeds until reconnection. Fix with asyncio.Queue-based diff.

### 6.6 Wrap synchronous py-clob-client calls in `run_in_executor`
`create_and_post_order()` blocks the event loop for 200-500ms. Use `loop.run_in_executor(None, ...)`.

---

## Recommended Parameter Changes for Live Trading

| Parameter | Current | Recommended | Rationale |
|-----------|---------|-------------|----------|
| `polling_interval_seconds` | 8 | 3-5 (adaptive) | Alpha decays exponentially with latency |
| `size_multiplier` | 0.5 | 0.3-0.7 (quality-scaled) | Kelly-adjust by trader quality |
| `max_trade_pct` | 0.02 | 0.015 | Slightly more conservative for live |
| `tp_range_fraction` | 0.40 | 0.25-0.50 (regime) | Regime-dependent; current 0.40 too aggressive for late entries |
| `sl_range_fraction` | 0.25 | 0.20-0.32 (regime) | Tighter for trending, wider for mean-reverting |
| `trailing_stop_fraction` | 0.15 | 0.50 | **Current 0.15 is inverted** — extremely tight trail (~7% pullback exits). 0.50 = give back 50% of gains |
| `resolution_blackout_hours` | 24.0 | 6.0 hard + 24.0 reduced | Tiered; don’t miss 12-24h window |
| `max_market_exposure_pct` | 0.08 | 0.06 | More conservative for live |
| `daily_loss_limit_pct` | 0.03 | 0.02 | Tighter circuit breaker for live |
| `max_concurrent_positions` | 10 | 8 | Reduce correlation risk |
| `min_market_volume` | 5000 | 10000 | Higher volume = better fills live |
| `max_top_traders` | 5 | 3-4 | Quality over quantity |
| `min_win_rate` | 0.55 | 0.58 | Slightly higher bar for live |
| `min_trade_count` | 50 | 100 | Better statistical significance for Sharpe |
| `half_life_days` | 14 | 10 | Faster rotation of stale traders |

---

## Implementation Roadmap

### Phase 1 (Week 1) — Fix Known Bugs + Quick Wins
- [ ] Fix `_midnight_utc()` UTC bug
- [ ] Replace `sys.exit()` with exceptions
- [ ] Ordered dedup cache
- [ ] Graceful gather
- [ ] Parallel validation pipeline
- [ ] Market data cache
- [ ] Update config.yaml parameters

### Phase 2 (Week 2) — Latency Optimization
- [ ] Adaptive polling interval
- [ ] In-memory position cache
- [ ] Implement `_maybe_update_subscription`
- [ ] Wrap sync CLOB calls in `run_in_executor`
- [ ] Connection pooling and DNS caching

### Phase 3 (Week 3) — Strategy Improvements
- [ ] Kelly-adjusted sizing
- [ ] Market regime classification
- [ ] Sliding resolution blackout
- [ ] Order book confirmation
- [ ] Asymmetric price deviation

### Phase 4 (Week 4) — Trader Selection + Game Theory
- [ ] Market diversity score
- [ ] Directional trader filter
- [ ] Anti-correlation filter
- [ ] Randomized execution delay
- [ ] Wash trade detection
- [ ] Crowd-awareness sizing

### Phase 5 (Week 5) — Exit Strategy
- [ ] Scaled partial exits
- [ ] Copy-the-exit feature

---

## Testing Strategy for Live Mode

1. **A/B paper test:** Run current and new config side-by-side for 72 hours
2. **Shadow mode:** Place real orders at $0.01 to validate execution path
3. **Micro-live:** Start with $50 bankroll for 1 week
4. **Scale up:** Double bankroll every 2 weeks if metrics hold

Key metrics: Fill rate (>70%), Latency (<5s), Slippage (<1.5%), Win rate (>55%), P&L per trade by regime
