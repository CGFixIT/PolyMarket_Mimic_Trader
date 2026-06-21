# Profitability & Latency Improvement Plan

> **Audience:** maintainers preparing to run `PolyMarket_Mimic_Trader` outside paper mode.
> **Scope:** trade quality, edge preservation, risk-control correctness, and the
> latency architecture needed so the **8-second detection budget is never blown**.
> **Status:** planning guide only — no behavior is changed by this document. Each
> item lists severity, the root cause in the current code, and a concrete fix.

This review was written from the combined perspective of a prediction-market /
game-theory practitioner and a Python async-systems engineer. The bot is
well-structured and its *range-relative* TP/SL insight is genuinely correct for
bounded `[0,1]` markets. The problems below are about **what happens when real
money and real latency are involved** — where paper mode silently hides the
failure modes.

---

## TL;DR — the three things that will lose money first

1. **Adverse selection from lag (the core economic flaw).** An 8 s poll + Data-API
   indexing lag means you systematically *miss* the high-alpha trades (price
   already moved >2 %, so the deviation gate rejects them) and *only* fill the
   stale, low-information ones. You inherit the worst slice of the trader's flow.
   Fixing this is 80 % of the profitability story. See §1 and §2.

2. **You copy entries but ignore exits.** Smart money's *exit* is at least as
   informative as its entry, yet `handle_trade_event` returns early on any
   non-BUY. You take their entry edge and then manage the position with naive,
   thesis-blind mechanical stops. See §2.1.

3. **Several advertised safety controls are not actually wired in**, and the
   daily-loss circuit breaker can be bypassed by new entries. Paper PnL is
   computed with zero slippage/fees and assumes instant full fills, so it will
   badly overstate live results. See §3 and §4.

---

## 1. Latency architecture — never miss the 8 s budget

**Goal restated:** detection-to-order latency must be bounded and *measured*, and
the poller must never silently fall behind or get rate-limited into missing a
trade.

### 1.1 The hot polling path has no rate limiter (HIGH)
`DataClient` owns an `AsyncLimiter(30, 60)`, but the actual live path —
`monitor.py::_poll_loop` — opens **its own** `aiohttp.ClientSession` with no
limiter, and `tracker.py` does the same. With 5 wallets every 8 s that is
~37.5 req/min against `/activity` *plus* tracker refreshes — over the assumed
30/60 s budget. A single `429` burst means **dropped polls = missed trades**.
`CONTRIBUTING.md` already flags this as known debt.

**Fix:** create one shared `aiohttp.ClientSession` + one shared `AsyncLimiter`
in an injected `HttpClient` object, and pass it to `DataClient`, `GammaClient`,
`TrackerClient`, and `TradeMonitor`. Centralize User-Agent, timeouts, retry, and
the connection pool there. This also removes per-request TCP/TLS setup from the
hot path.

### 1.2 Detection latency is unbounded and uninstrumented (HIGH)
Worst-case time from a tracked trader's fill to our order is:
`(on-chain settle + Data-API index lag) + up-to-8 s poll wait + N sequential
REST calls in handle_trade_event`. None of this is logged, so you cannot tell
whether you are missing the budget.

**Fix:**
- Stamp every `TradeEvent` with `detected_at = time.monotonic()` and the
  trade's own `timestamp`. Log `age_at_detection = now - trade.timestamp` and
  `decision_latency = order_sent - detected_at`. Emit a histogram (even just
  log-based) so the 8 s budget is observable.
- Add a hard **staleness gate**: if `now - trade.timestamp > max_trade_age_s`
  (e.g. 12 s), skip — the alpha is gone and you would only be buying the
  trader's price impact. This is the single most important new filter.

### 1.3 Sequential awaits in the critical path (MEDIUM)
`handle_trade_event` does `get_market()` → `get_market_price()` → `place_order()`
strictly sequentially. Each is a REST round-trip (~100–300 ms). On a copy bot
that is 200–600 ms of avoidable latency per trade.

**Fix:**
- Run `get_market()` and `get_market_price()` concurrently with
  `asyncio.gather`.
- **Prefetch & cache** market metadata (resolve time, volume) for every market a
  tracked wallet touches, refreshed on the rebalance/exit-check loop, not inline.
  Resolve time and 24 h volume do not change second-to-second; fetching them on
  the hot path is wasteful.
- Cache the live midpoint from the WebSocket feed (you already hold a WS
  connection) instead of issuing a fresh `/midpoint` REST call per event.

### 1.4 Poller cold-start dumps the bankroll (CRITICAL)
On the first poll, `_seen_trade_ids` is empty, so **every** recent trade (up to
50 per wallet) is treated as "new" and copied at once. Launching the bot would
fire dozens of stale orders instantly.

**Fix:** on the first poll per wallet, **seed `_seen_trade_ids` without acting**
(prime the set, emit nothing). Only trades appearing *after* startup, and within
the staleness window (§1.2), should generate events.

### 1.5 `seen` set eviction can re-fire old trades (MEDIUM)
`_filter_new_trades` evicts overflow with `set.pop()`, which removes an
*arbitrary* element — possibly a *recent* id, which then re-appears as "new" on
the next poll and gets re-copied (duplicate position).

**Fix:** replace the `set` with a `collections.OrderedDict` (or `deque`) and
evict **oldest-first** (FIFO). Bound by count *and* by age.

### 1.6 Stagger / jitter the per-wallet polls (LOW)
All wallets are polled in one synchronized burst every 8 s, creating a thundering
herd against the Data API. Spread polls across the interval (or use a token
bucket) to smooth load and reduce 429 risk.

### 1.7 WebSocket does **not** reduce *entry* latency (clarification)
The README implies the WS feed mitigates the 8 s delay. It does not — WS only
streams prices for tokens you **already hold**. Entry detection is 100 % on the
REST poll. The only ways to truly cut entry latency are: (a) poll faster within
rate limits, (b) the staleness gate so you skip races you have already lost, and
(c) if/when Polymarket exposes a wallet-filtered or on-chain trade stream,
subscribe to it. Consider an on-chain mempool/log subscription (Polygon
websocket RPC on the CTF Exchange contract) as a future low-latency source.

---

## 2. Strategy & game theory — preserving the edge

### 2.1 Mirror exits, not just entries (HIGH)
`handle_trade_event` returns on any non-BUY trade, so a tracked trader **selling**
produces no action. But their exit encodes information (thesis played out, news,
or they now think it's overvalued). Holding with our own stops throws that away.

**Fix:** when a tracked trader SELLs a token we hold a copied position in, treat
it as a first-class exit signal — at minimum scale out proportionally, ideally
exit fully. Add an `ExitReason.SOURCE_EXIT`. This aligns our holding period with
the smart money's actual conviction window.

### 2.2 The deviation gate creates negative selection (HIGH)
`max_price_deviation = 0.02` rejects any trade where price already moved >2 %.
Combined with lag, this means the trades you *can* fill are disproportionately
the ones the market **didn't** react to — i.e. the trader's least informative
flow. You are adversely selected into their noise.

**Fix:** reframe the gate around **edge after slippage**, not raw deviation.
Estimate the trader's edge (see §2.4) and only require that *expected value net of
the price you'd actually pay* is still positive. A 4 % move into a trade with
15 % expected edge is still worth copying; a 1 % move into a 1 % edge is not.

### 2.3 Win-rate-driven scoring favors favorite-buyers (HIGH)
`consistency = win_rate × log(trades)` rewards traders who buy heavy favorites
(buy YES at 0.95 → wins 95 % of the time, but risks 95¢ to make 5¢). Win rate is
the wrong primary axis in a probability market.

**Fix:** score on **price-adjusted / risk-adjusted return per trade**, not win
rate. Compute return on risk = `pnl / amount_staked`, and weight by it. Keep win
rate only as a soft tiebreaker. Reward traders whose realized returns beat the
implied probability they paid.

### 2.4 Sharpe proxy mixes skill with bet size (MEDIUM)
`sharpe_proxy = mean_pnl / stddev_pnl` runs on **absolute dollar** PnL per trade.
A whale and a minnow of equal skill score differently purely because of stake
size. Variance of dollar PnL is dominated by position sizing, not edge.

**Fix:** normalize each trade's PnL by its stake (return %), *then* compute the
Sharpe-like ratio. This isolates skill from bankroll.

### 2.5 PnL estimation excludes hold-to-resolution traders (HIGH)
`_compute_trader_stats` only records a `TradeRecord` when a BUY is later matched
to a SELL (FIFO). On Polymarket, **the most common profitable pattern is buying
and holding to resolution** — those positions never produce a SELL, so they are
invisible to the stats. Result: the scorer is biased toward scalpers and may
filter out the best buy-and-hold traders entirely (they show 0 matched trades →
win_rate 0 → ineligible).

**Fix:** incorporate market **resolution** as a synthetic exit (price → 0 or 1)
when matching. Pull resolved-market outcomes and treat an unmatched BUY in a
resolved market as closing at the resolution value. Also: FIFO matching ignores
share-quantity mismatches between buy and sell — match on shares, not just count.

### 2.6 No edge / EV gate or Kelly-aware sizing (MEDIUM)
Sizing is `min(source_size × 0.5, 2 % bankroll)` — purely mechanical, with no
relationship to the *strength* of the signal. A trade with 1 % edge gets the same
size as one with 20 % edge.

**Fix:** introduce an explicit edge estimate (e.g. trader's historical realized
edge × recency, or distance from a fair-value reference) and scale size by a
**fractional Kelly** (¼–½ Kelly) capped by `max_trade_pct`. Bet more when the
signal is strong, less when marginal.

### 2.7 Multiple tracked traders → same token not handled (MEDIUM)
`get_position_by_token(token_id)` returns a single open position, and the copier
keys exits by token. If two tracked wallets buy the same token, the second copy
collides — only one is price-tracked/exited correctly.

**Fix:** key positions by `position_id`, maintain a `token_id → {position_ids}`
index, and fan a price tick out to **all** positions on that token.

---

## 3. Risk-control correctness — wired vs. advertised

### 3.1 Daily-loss circuit breaker is bypassable on entry (CRITICAL)
The breaker is only evaluated inside `RiskManager.evaluate()` — the **exit** path.
`handle_trade_event` (the **entry** path) never checks daily PnL, so once the
daily limit is hit, the bot will still happily **open new positions**.

**Fix:** check the daily-loss (and a global kill-switch) at the top of
`handle_trade_event` before any sizing or order. Centralize "is trading halted?"
in one method used by both paths.

### 3.2 `cooldown_after_losses` / `cooldown_minutes` are not implemented (HIGH)
Both are in `config.yaml` and `config.py`, and the README lists "Cooldown — pauses
after 3 consecutive losing trades" as an active control — but **no code reads
them**. The advertised protection does not exist.

**Fix:** track consecutive losses in `RiskManager.record_exit`; when the count
hits `cooldown_after_losses`, set a `cooldown_until = now + cooldown_minutes*60`
and reject entries until it passes (reset the counter on any win).

### 3.3 `max_trader_allocation` (5 %/trader) is not enforced (HIGH)
Configured and implied by the README, but the copier only checks a per-trader
**drawdown** stop — never the **allocation** cap. One hot trader could absorb far
more than 5 % of bankroll across many concurrent positions.

**Fix:** track live exposure per `trader_address` and reject/trim copies that
would exceed `max_trader_allocation × bankroll`.

### 3.4 Fail-open gates on missing market data (MEDIUM)
- If `get_market()` returns `None`, the **volume check is skipped entirely**
  (`if market and market.volume_24h < ...`) — the trade goes through on no data.
- If `get_market_price()` returns `None`, `current_price` falls back to
  `event.price`, making the deviation check trivially pass (`|p−p| = 0`) and
  trading on a possibly-stale price.

Both **fail open** (trade on missing data). For real money they should **fail
closed** (skip when you cannot verify volume, resolve time, or current price).

### 3.5 In-memory bankroll drifts from on-chain balance (MEDIUM)
`record_exit` mutates `self.bankroll += pnl` using *estimated* fill PnL. This
number drives all position sizing and exposure caps, but is never reconciled with
the real USDC balance via `clob.get_balance()`. Errors compound.

**Fix:** periodically (exit-check loop / post-trade) resync `risk.bankroll` to the
actual on-chain balance.

### 3.6 Stop-loss semantics deserve scrutiny in probability space (MEDIUM — design)
A mechanical SL on a `[0,1]` probability is not obviously correct. If smart money
buys YES at 0.40 expecting 0.60 fair value, a dip to the SL (~0.30) stops you out
exactly when the position is *more* attractive — unless the thesis is invalidated.
Price-based stops convert a probabilistic edge into realized losses on noise, and
the tight `trailing_stop_fraction = 0.15` will get chopped out by normal intraday
swings (these markets routinely move 10–20 % on no news).

**Recommendation:** treat this as an explicit, **backtested** design decision, not
a default. Prefer **thesis/event-based** exits (source-trader exit §2.1, news,
resolution approach) over pure price stops; if keeping price stops, widen the
trail and validate against historical data before trusting it with capital.

---

## 4. Execution realism — the paper-vs-live gap

### 4.1 Paper mode assumes zero slippage and instant full fills (CRITICAL for evaluation)
- Paper `place_order` "fills" at the requested price with no spread, no slippage,
  no fees, and 100 % fill. The synthetic order book is a flat `0.50/0.51`.
- The whole position lifecycle assumes the GTC limit **fills instantly and fully
  at `order.price`**. In live trading a GTC limit may rest unfilled, partially
  fill, or fill worse.

**Consequence:** paper PnL will **materially overstate** live results, especially
on the thin, fast markets where copy trading competes. Do not size live capital
off paper performance without a slippage/fees model.

**Fix:**
- Model fills in paper mode by **walking the real order book** (you already fetch
  it for the live depth check) and applying realistic slippage + any fees.
- In live mode, use **marketable limit / FOK or IOC** for copy entries (the
  `Order` model already supports `FOK`), poll order status, handle partial fills,
  and reprice or cancel on timeout. Record the **actual** fill price/size into the
  position, not the requested one.

### 4.2 Double-exit race between WS and poll loops (MEDIUM)
Both `handle_price_tick` (WS) and `exit_check_loop`→`check_all_exits` (poll) can
fire `_exit_position` for the same position. `close_position` is not idempotent,
so two SELL orders can be sent.

**Fix:** guard exits with a per-position lock / status check (`closing` flag), and
make `close_position` a no-op if the position is already closed.

### 4.3 Order placement has no retry/timeout/idempotency (MEDIUM)
A transient network error on `place_order` is caught and the trade is dropped (or,
on exit, the position is left open with exposure released inconsistently). No
idempotency key, no bounded retry.

**Fix:** wrap order placement in a bounded retry with jitter and an idempotency
key; on exit failure, keep retrying / re-queue rather than silently abandoning.

---

## 5. Suggested config additions

```yaml
copy_trading:
  max_trade_age_seconds: 12      # NEW — staleness gate (§1.2); skip stale alpha
  min_edge_after_slippage: 0.03  # NEW — EV gate replacing raw deviation (§2.2)
  mirror_source_exits: true      # NEW — act on tracked-trader SELLs (§2.1)
  kelly_fraction: 0.25           # NEW — fractional-Kelly sizing (§2.6)

risk_management:
  fail_closed_on_missing_data: true  # NEW — §3.4
  resync_bankroll_every_n_exits: 5   # NEW — §3.5
  # cooldown_after_losses / cooldown_minutes already exist — WIRE THEM (§3.2)
  # max_trader_allocation already exists — ENFORCE IT (§3.3)

observability:
  log_latency_histogram: true        # NEW — §1.2
```

---

## 6. Prioritized roadmap

| # | Item | Severity | Effort | Section |
|---|------|----------|--------|---------|
| 1 | Cold-start seed (don't copy 50 stale trades on launch) | 🔴 Critical | S | 1.4 |
| 2 | Daily-loss breaker on the entry path | 🔴 Critical | S | 3.1 |
| 3 | Staleness gate + latency instrumentation | 🔴 High | M | 1.2 |
| 4 | Shared session + rate limiter on hot path | 🔴 High | M | 1.1 |
| 5 | Wire cooldown + enforce per-trader allocation | 🔴 High | S | 3.2, 3.3 |
| 6 | Mirror source exits | 🟠 High | M | 2.1 |
| 7 | Fix scoring: returns-based, resolution-aware PnL | 🟠 High | L | 2.3–2.5 |
| 8 | Fail-closed on missing market data | 🟠 Medium | S | 3.4 |
| 9 | FIFO seen-eviction + age bound | 🟠 Medium | S | 1.5 |
| 10 | Realistic paper fills (book-walk + slippage/fees) | 🟠 Medium | M | 4.1 |
| 11 | EV/edge gate + fractional-Kelly sizing | 🟡 Medium | L | 2.2, 2.6 |
| 12 | Concurrent hot-path fetches + market cache | 🟡 Medium | M | 1.3 |
| 13 | Multi-trader same-token position handling | 🟡 Medium | M | 2.7 |
| 14 | Double-exit lock + idempotent close | 🟡 Medium | S | 4.2 |
| 15 | Live marketable-limit + fill reconciliation | 🟡 Medium | L | 4.1 |
| 16 | Bankroll ↔ on-chain resync | 🟡 Medium | S | 3.5 |
| 17 | Backtest stop-loss design in probability space | 🟡 Design | L | 3.6 |

**Recommended first PR (safety-critical, small):** items **1, 2, 5, 8, 9** — these
are low-effort correctness/safety fixes that close advertised-but-missing controls
and the most dangerous money-losing bugs before any live capital is risked.

**Second PR (edge preservation):** items **3, 4, 6, 10** — bound and measure
latency, stop adverse selection, and make paper results trustworthy.

**Third PR (alpha):** items **7, 11, 12, 13, 15** — better trader selection,
EV-aware sizing, and real execution.

---

## 7. What's already good (keep it)

- **Range-relative TP/SL** is the right mental model for bounded markets — keep it.
- Clean module separation (monitor / copier / risk / portfolio / tracker).
- Exposure is correctly **released** when an order fails before opening (no
  phantom-exposure leak).
- WAL-mode SQLite persistence with a restart path that restores exposure.
- Risk-adjusted *intent* in scoring (Sharpe × consistency × recency) — the axes
  are right; only the inputs (§2.3–2.5) need correcting.
- Graceful WS degradation to poll-only mode.
