# IMPROVEMENT_PLAN_V2 — Live-Trading Profitability & Robustness

**Scope:** Make the bot profitable and safe to run **outside paper mode**. Improve trade
accuracy/quality and thresholds, mitigate latency so the ~8 s detection budget is never
missed, and close the strategy/microstructure gaps that make live PnL diverge from paper.

**Method:** Five independent expert audits (low-latency async systems, prediction-market
strategy, risk/sizing quant, trader-selection quant, execution-microstructure & game
theory) read the current `main` source (post-PR #32) in full. This document is the
synthesized, de-duplicated, prioritized result. Every item is **new** — none duplicates
work already merged.

> This PR adds only the planning document. Code changes follow as separate, reviewable PRs
> per the rollout in §7.

---

## 0. Already implemented — explicitly out of scope

So reviewers know these are *not* being re-litigated. All are merged to `main`:

- Fixed-deadline poll loop (drift fix); WebSocket subscription diffing; WS keep-alive tuning.
- Parallel `asyncio.gather` for market + price fetch on the hot path.
- `asyncio.Lock` on exposure dicts; entry TOCTOU lock; connection pooling / `TCPConnector`;
  session-creation race lock; shared rate limiter.
- Range-relative TP/SL bounded `[0,1]`; UTC midnight clock; `tp ≤ sl` guard; boundary
  minimums; non-mutating trailing-stop evaluate (caller-managed peak).
- Async `build_position`/`record_exit`; fill reconciliation on **entries**; exit retry with
  backoff; cold-start priming; FIFO seen-id eviction; fail-closed on missing data.
- Kelly sizing (opt-in, min-trades gate, tracker-prior warm-up); Decimal PnL; realized-lot
  ledger; live slippage cap (PR #31); typed `ConfigError`; config-driven blackout knob.

---

## 1. Executive summary — the core thesis

The bot's **plumbing** is now solid. The remaining profitability gaps cluster in five places,
and the most dangerous are concentrated in **live execution**, which paper mode cannot reveal:

> **Paper assumes immediate full fills at the midpoint plus a flat 2.5 % cost. Live reality is
> resting GTC limit orders at the mid that under-fill or fill adversely, stop-loss exits that
> may never liquidate, no fee/spread in live sizing, no fill confirmation on exits, and a
> double-exit race that double-books PnL and over-sells.** Every one of these makes realized
> live PnL strictly *worse* than the books show.

Layered on top: the **strategy** copies almost any fresh BUY (symmetric deviation gate that
throws away the best-EV entries, no edge/conviction/crowding awareness), the **risk
thresholds** are mis-tuned (an inverted-tight trailing stop that amputates winners, R:R that
goes to 0.13:1 at high entry prices, a circuit breaker blind to unrealized drawdown, no total
deployment cap), **trader selection** is open-loop and statistically fragile (never learns
from its own losses, a multiplicative score that an outlier dominates, an all-time leaderboard
full of survivorship/regime bias, win-rate selection that prefers low-edge favorite-buyers),
and the **infrastructure** has silent-failure modes (a dying poll task or a permanently dead
WebSocket leaves positions unmanaged with no alarm) plus event-loop freezes (blocking CLOB
calls) that directly threaten the 8 s budget.

**Expected impact if addressed:** live PnL stops systematically lagging paper (Tier 0 + H5),
winners stop being amputated and high-entry trades stop being structurally negative-EV
(H1/H2), the bot stops copying its worst entries and starts learning from losers
(H6/H7/H13), and detection/exit can no longer silently stop (H9/H10).

---

## 2. Master priority roadmap

Severity × subsystem. IDs are referenced in the detailed sections. Effort: S < ½ day,
M ≈ 1–2 days, L ≈ 3 days+.

| ID | Finding | Subsystem | Sev | Effort |
|----|---------|-----------|-----|--------|
| **C1** | Blocking CLOB sign+POST freezes the event loop (stalls detection + all exits ~0.1–1.5 s) | Latency | 🔴 CRIT | M |
| **C2** | Orders are GTC limits at the **mid** — entries under-fill/fill adversely; SL exits don't liquidate | Execution | 🔴 CRIT | M |
| **C3** | Exit path has **no fill confirmation** — "placed" treated as "filled"; phantom closes | Execution | 🔴 CRIT | M |
| **C4** | Double-exit race — no per-position guard → double SELL, double-booked PnL & tax lots | Execution | 🔴 CRIT | S |
| **C5** | Rebalance adds wallets that `KeyError` on first poll → never detected until restart | Latency | 🔴 CRIT | S |
| **H1** | Trailing stop inverted-tight (0.15 ⇒ exits on ~5 % pullback, amputates winners) | Risk | 🟠 HIGH | S |
| **H2** | TP/SL risk:reward inverts to 0.13:1 at high entry prices | Risk | 🟠 HIGH | M |
| **H3** | Daily-loss breaker ignores **unrealized** drawdown | Risk | 🟠 HIGH | M |
| **H4** | No **total** portfolio deployment cap (can sink majority of bankroll) | Risk | 🟠 HIGH | S |
| **H5** | Live sizing ignores fee+spread; entry recorded at mid, not actual fill | Execution | 🟠 HIGH | M |
| **H6** | Price-deviation gate is symmetric — rejects **favorable** entries (best EV) | Strategy | 🟠 HIGH | S |
| **H7** | No entry-price band gate — copies 0.97+/0.03- tokens with no edge after cost | Strategy | 🟠 HIGH | S |
| **H8** | YES/NO token never validated vs market metadata — wrong-side fill = 100 % loss | Strategy | 🟠 HIGH | S |
| **H9** | No task supervisor / dead-man's switch — a dead poll loop stalls detection forever, silently | Latency | 🟠 HIGH | M |
| **H10** | WS reconnect/permanent-death gap leaves positions unmanaged; serial fallback exits | Latency | 🟠 HIGH | M |
| **H11** | Per-tick SQLite read+commit on the exit hot path → in-memory position cache | Latency | 🟠 HIGH | M |
| **H12** | Entry lock held across order I/O — serializes copies head-of-line | Latency | 🟠 HIGH | M |
| **H13** | Selection never demotes a tracked trader whose copied trades lose us money | Selection | 🟠 HIGH | M |
| **H14** | Multiplicative score amplifies outliers; `sharpe_proxy` explodes at ~0 variance | Selection | 🟠 HIGH | S–M |
| **H15** | All-time leaderboard window → survivorship + regime bias | Selection | 🟠 HIGH | M |
| **H16** | Win-rate selection favors low-edge favorite-buyers; excludes value bettors | Selection | 🟠 HIGH | M |
| **H17** | Predictable fixed 8 s cadence is front-runnable — no jitter | Execution | 🟠 HIGH | S |
| **H18** | Kelly `p` = trader win-rate, not a per-token probability — sizing is price-inverted | Risk | 🟠 HIGH | M |
| **M1** | No remaining-edge check — copies into the whale's own price impact at the post-move book | Strategy/Exec | 🟡 MED | M |
| **M2** | No crowding / book-share cap — fixed-size copies compound slippage as bots stack | Strat/Risk/Exec | 🟡 MED | M |
| **M3** | Flat 24 h resolution blackout cliff — blanket-skips the high-alpha 6–24 h window | Strategy | 🟡 MED | M |
| **M4** | No conviction signal — sizes off absolute USDC, not size-vs-trader-bankroll | Strategy | 🟡 MED | M |
| **M5** | Market metadata refetched every event — add TTL cache | Latency | 🟡 MED | S |
| **M6** | No volatility/regime adaptation of TP/SL widths (elections vs crypto vs sports) | Risk | 🟡 MED | M |
| **M7** | Correlated same-event positions sized independently (no event-level cap) | Risk | 🟡 MED | L |
| **M8** | Time-exit clips profitable resolution-hold conviction trades | Risk | 🟡 MED | S |
| **M9** | Bankroll never resyncs from live on-chain balance — caps drift | Risk | 🟡 MED | S |
| **M10** | Can't tell whale **adding to a loser** (martingale) from a fresh winner | Strategy | 🟡 MED | L |
| **M11** | No wash-trade / self-cross guard in scoring | Selection | 🟡 MED | M |
| **M12** | `min_trades=50` too few — ±14 pp CI on win rate → selecting on noise | Selection | 🟡 MED | S |
| **M13** | Sharpe ignores trade frequency; held-to-resolution **losses** under-counted | Selection | 🟡 MED | M |
| **M14** | Hold-to-resolution modeled as a sellable mid, not 0/1 settlement | Execution | 🟡 MED | L |
| **M15** | No tick-size / min-order rounding — off-tick/sub-min orders silently rejected | Execution | 🟡 MED | M |
| **M16** | WS head-of-line blocking; subscription diff runs per inbound frame | Latency | 🟡 MED | M |
| **M17** | Serial source-exits with retry backoff block the detection poll | Latency | 🟡 MED | M |
| **L1** | No adaptive ("hot") polling when a tracked wallet is active | Latency | 🟢 LOW | M |
| **L2** | Poll loop builds its own session w/o keepalive; two independent rate limiters | Latency | 🟢 LOW | S |
| **L3** | Lazy live-client init (creds derivation) blocks on the first order | Latency | 🟢 LOW | S |
| **L4** | Recency half-life (14 d) too slow; parse-failure fabricates freshness | Selection | 🟢 LOW | S |
| **L5** | Cooldown counts source/time exits as "losses" | Risk | 🟢 LOW | S |
| **L6** | Maker/taker fee asymmetry & rebates unmodeled (single flat fee) | Execution | 🟢 LOW | S |
| **L7** | No capacity-aware selection; top-5 can be correlated favorite-buyers | Selection | 🟢 LOW | L |

---

## 3. Tier 0 — CRITICAL: live correctness (fix before any live capital)

These are the items that make live trading lose money or corrupt state immediately. Paper mode
hides all of them.

### C1 — Blocking CLOB calls freeze the event loop
**`api/clob_client.py:139` (`create_and_post_order`), `:70` (`get_order_book`), `:157`, `:168`; lazy init `:33-61` (see L3).**
`place_order()` is `async def` but, in live mode, calls the **synchronous** py-clob-client:
EIP-712 signing (CPU, ~5–30 ms secp256k1) + a blocking `requests` POST (100–800 ms), directly
on the loop. While it blocks, the 8 s poller can't fire, the WS can't drain (ping stalls →
spurious reconnect), and **every open position's TP/SL is not evaluated**. The liquidity
pre-check `get_order_book` is a second serial blocking call per BUY.

```python
import asyncio, functools
loop = asyncio.get_running_loop()
signed = await loop.run_in_executor(
    self._signer_pool,  # dedicated ThreadPoolExecutor(max_workers=2)
    functools.partial(self._client.create_and_post_order,
                      token_id=order.token_id, price=order.price, size=size_shares, side=side),
)
# same treatment for get_order_book / cancel / get_balance
```
New knob: `clob_signer_threads: 2`. **Fix C1 first — it is the dominant "8 s budget missed" risk.**

### C2 — Orders are GTC limits at the midpoint
**`api/clob_client.py:139-144`; `Order.order_type` is dead metadata at `models/types.py:29`; exit `core/copier.py:419-426`.**
`create_and_post_order` is called with **no order-type** → py-clob-client defaults to a **GTC
limit**. The `order_type` field on `Order` is never read. Entries are priced at
`current_price`, which is the CLOB **midpoint** (`get_market_price` → `/midpoint`). A BUY limit
at the mid sits *behind* the ask, so it either (a) rests unfilled while the signal decays, or
(b) fills only when the market ticks down to you — i.e. **adverse selection**. Exits build a
SELL limit at the trigger tick — in a fast down-move it trails the book and **never liquidates**,
turning a bounded stop into an unbounded loss. This is the single largest paper-overstates-live gap.

Wire order type through, and make entries **marketable** (price through the book at
`ask × (1 + max_live_slippage_pct)`, FOK) and exits **aggressive taker** (`bid × (1 − cap)`, FAK):
```python
from py_clob_client.clob_types import OrderType
otype = {"GTC": OrderType.GTC, "FOK": OrderType.FOK, "FAK": OrderType.FAK}[order.order_type]
signed = self._client.create_and_post_order(..., order_type=otype)
```
Set `order_type="FOK"` at the entry `Order(...)` and `"FAK"` at the exit `Order(...)`. Treat a
zero-fill FOK/FAK **exit** as a *failure* (retry), not success.

### C3 — Exit path has no fill confirmation
**`core/copier.py:419-455` (`_exit_position`).**
The retry loop sets `placed = True` on the first call that doesn't *raise*, then unconditionally
`close_position()` + `record_exit()`. There is **no `_reconcile_fill` on exits** (entries have
one at `:277/:345`). So a live exit that is accepted-but-unfilled records a phantom realized
PnL, releases exposure, marks the DB closed — while the token is **still in the wallet**. PnL,
exposure caps, and the daily-loss breaker all silently diverge from reality.
Mirror the entry reconciliation: inspect `filled_size`; if 0/partial, re-price aggressively and
retry; only close/record for the **actually-liquidated** shares; carry the remainder forward.

### C4 — Double-exit race
**`core/copier.py:400-417` (`handle_price_tick`, no lock) → `core/portfolio.py:112-145` (`close_position`).**
`handle_price_tick` has no lock, and `close_position` runs `UPDATE … SET status='closed' WHERE
position_id=?` with **no `AND status='open'` guard** (`:124`). Two near-simultaneous triggers
(two WS ticks, or a tick + the `check_all_exits` sweep, or a `SOURCE_EXIT`) both pass
`evaluate()≠HOLD`, both UPDATE, both **INSERT a realized-lot row**, and both call `record_exit`
→ bankroll and `_daily_pnl` mutated twice for one position, **two live SELLs** (an unintended
oversell/short). Make the close atomic and conditional, and gate on `rowcount`:
```python
cur = await db.execute(
    "UPDATE positions SET status='closed', ... WHERE position_id=? AND status='open'", (...))
if cur.rowcount != 1:
    return 0.0            # someone else already closed it — do NOT record_exit / place SELL
```
Belt-and-suspenders: an `asyncio.Lock` keyed by `position_id` around `_exit_position`.

### C5 — Rebalance silently breaks detection for new wallets
**`main.py:111-121` sets `monitor._wallets = [...]`; `_seen_trade_ids` keyed only at construction (`monitor.py:154`).**
A wallet added at rebalance has no `_seen_trade_ids[wallet]` entry → `_filter_new_trades`
`KeyError`s, swallowed by `return_exceptions=True`, logged as a warning. The new trader is
**never successfully polled until restart**. Add a `set_wallets()` method that initializes
seen-ids/priming for added wallets and drops removed ones; call it from `rebalance_loop`
instead of poking the private attribute.
```python
def set_wallets(self, wallets: list[str]) -> None:
    wallets = [w.lower() for w in wallets]
    for w in wallets:
        self._seen_trade_ids.setdefault(w, OrderedDict())   # new wallets absent from
    self._wallets = wallets                                 # _primed_wallets → first poll primes
```

---

## 4. Tier 1 — HIGH: thresholds, sizing, entry quality, resilience

### Risk & sizing

**H1 — Trailing stop is inverted-tight.** `risk_manager.py:547-557`, `trailing_stop_fraction=0.15`.
`trail_sl = peak − (peak − sl)×fraction` puts the stop **85 % of the way up** from the hard SL
to the peak. Entry 0.50 / peak 0.70 → trail 0.651 → exits on a **7 %** pullback; peak 0.55 →
exits on **4.8 %**. Polymarket tokens wiggle 3–7 % on noise, so this stops out essentially every
winner on the first retrace. Re-base the trail on **peak-to-entry run-up** and loosen the default:
```python
run_up   = peak - pos.entry_price
trail_sl = max(peak - run_up * self.cfg.trailing_stop_fraction, pos.sl_price)
```
Set `trailing_stop_fraction = 0.40` (give back 40 % of the run-up → realistic ~12–15 % tolerance).

**H2 — TP/SL R:R inverts at high entries.** `risk_manager.py:511-545`.
TP captures 40 % of *upside* range, SL risks 25 % of *downside* range, so R:R is a function of
entry price and flips adverse above ~0.62:

| entry | TP | SL | up | down | R:R | breakeven win-rate |
|------|------|------|------|------|------|------|
| 0.20 | 0.520 | 0.150 | .320 | .050 | **6.4:1** | 14 % |
| 0.50 | 0.700 | 0.375 | .200 | .125 | **1.6:1** | 38 % |
| 0.82 | 0.892 | 0.615 | .072 | .205 | **0.35:1** | 74 % |
| 0.95 | 0.980 | 0.712 | .030 | .238 | **0.13:1** | 89 % |

Good traders buy favorites (0.80–0.95) — this rule destroys R:R exactly there. Cap SL distance
to enforce a floor R:R:
```python
sl_dist = min(sl_dist, tp_dist / self.cfg.min_reward_risk)   # min_reward_risk = 1.0
```

**H3 — Daily-loss breaker ignores unrealized drawdown.** `risk_manager.py:281-289, 402-417`;
`_daily_pnl` only changes in `record_exit` (realized). With ~20 % deployed the book can be at
−10 % mark-to-market while the breaker stays green, then stops convert it to realized loss all
at once. Have `is_trading_halted(unrealized_pnl)` compare `realized_daily + unrealized` to the
limit; feed it the cached sum of `pos.pnl_at(current_price)`.

**H4 — No total deployment cap.** Caps are per-market (8 %), per-trader (5 %), per-trade (2 %),
`max_concurrent_positions=10` — nothing caps the **sum across markets**. Add
`max_total_exposure_pct=0.30`, enforced in `build_position` under the existing `_exposure_lock`:
```python
total = sum(self._market_exposure.values(), _ZERO) + position_value_d
if float(total) > self.bankroll * self.cfg.max_total_exposure_pct:
    raise ExposureCapError(...)
```

**H18 — Kelly `p` is the wrong input.** `sizing.py:33-52`, called `copier.py:166-194`.
The math is right and `b` *is* derived from price, but `p` is the trader's **historical
win-rate** used as the per-token win probability for a token the market already prices at
`current_price`. These are different quantities — feeding `p=0.60, price=0.85` yields `f*<0`
(skip a strong favorite); `p=0.60, price=0.20` yields `f*=0.50` (half-bankroll). Sizing becomes a
function of price alone, inverted. Derive `p = clamp(price + edge, 0, 1)` with a small bounded
`edge` from demonstrated trader skill, so Kelly reflects edge *over the market line*.

### Execution

**H5 — Live sizing ignores fee + spread.** `copier.py:162-202`; live path `clob_client.py:134-149`
applies neither (paper bakes in 2 % fee + 0.5 % slip). Live entry is at *ask + fee* but recorded
at the *mid*, so the position opens underwater by spread+fee (~3–5 % round trip) **and** TP/SL are
computed off a price you never got. Size and record `entry_price` from the **reconciled
`avg_fill_price`** (real once C2/C3 land); subtract expected round-trip fee from the edge before
copying and re-validate the post-fee TP clears. Reconcile `paper_taker_fee_pct` against
Polymarket's real fee tiers so paper and live agree.

**H17 — Predictable cadence is front-runnable.** `monitor.py:352-358`, fixed 8 s, zero jitter.
Anyone watching the same public whales knows your copy lands in a tight, deterministic window and
front-runs the thin book inside your 1–2 % tolerance. Add bounded jitter and per-wallet phase
offset:
```python
next_deadline += self._poll_interval + random.uniform(-self._poll_jitter, self._poll_jitter)
```
(`poll_jitter_seconds: 2`.) Deeper fix is M1 (revalidate edge instead of racing).

### Strategy — stop copying the worst entries

**H6 — Symmetric price-deviation rejects favorable entries.** `copier.py:140-147`.
`deviation = abs(current − event.price)/event.price` treats a move **in your favor** (whale
bought YES @0.40, now 0.36 — cheaper, more upside to the same target) identically to an adverse
move. You discard the **best-EV** copies and keep only at-par/adverse ones. Make it directional;
only adverse slippage is gated:
```python
signed_dev = (current_price - event.price) / event.price       # +ve = costlier than whale
if signed_dev > ct.max_price_deviation: return                 # adverse → skip
if signed_dev < -ct.max_favorable_deviation: return            # collapsed → likely adverse news
```
(`max_favorable_deviation: 0.15`.)

**H7 — No entry-price band gate.** `copier.py` between `:147` and `:162`.
The bot copies a YES at 0.98 — ~2¢ upside vs ~98¢ downside, negative after the ~2.5 % cost, and
the range-relative engine then sets TP≈1.00 (rarely prints pre-resolution) while the 24 h blackout
force-exits the only path that pays (hold to redemption). Skip extreme prices:
```python
if not (ct.min_entry_price <= current_price <= ct.max_entry_price):  # 0.05 … 0.95
    return
```

**H8 — YES/NO token never validated.** `copier.py` (copies `event.token_id` blindly);
`Market.token_id_yes/no` fetched but unused. A mislabeled/ambiguous Data-API row → buying the
**exact wrong side** = 100 % loss. Cheap guard:
```python
if market and event.token_id not in (market.token_id_yes, market.token_id_no):
    logger.warning("Skip: token not a recognized outcome for market (YES/NO mismatch)"); return
```

### Trader selection — make "copy winners" statistically sound

**H13 — No live feedback demotion.** `main.py:111-121`, `tracker.py:239-288`; the only reaction
(`copier.py:222-229` drawdown skip) just pauses entries and self-clears. Selection is open-loop:
a leaderboard star whose every copied trade loses *us* money stays tracked for up to 7 days. Add
a realized-copy-PnL gate (you already track per-trader PnL & win-rate): when a tracked trader has
≥ N closed copies and a Wilson **upper** bound below `min_win_rate`, drop them from
`monitor._wallets` + the Kelly prior until the next `refresh()` re-qualifies them.

**H14 — Multiplicative score amplifies outliers.** `tracker.py:157` (`sharpe*consistency*recency`),
`:107-112` (`mean_pnl/_EPSILON` when stddev≈0). One lucky low-variance streak → near-infinite
Sharpe → rank 1, despite far less evidence. Cap the Sharpe proxy and shrink for small samples;
prefer a weighted sum of rank/z-scored, capped components:
```python
sharpe = min(mean_pnl / max(stddev_pnl, _STDDEV_FLOOR), 3.0) * (n / (n + K))   # shrink small-n
score  = w1*rank(sharpe) + w2*rank(consistency) + w3*recency
```

**H15 — All-time leaderboard.** `tracker.py:308`, `data_client.py:64` hard-code `window="all"` →
survivorship (blow-ups invisible) + regime bias (rewards last cycle's favorite-buyers). Fetch a
**trailing** window too (`leaderboard_window: "30d"`) and require ranking in **both**; weight
recent trades more.

**H16 — Win-rate favors favorite-buyers.** `tracker.py:154` (`consistency=win_rate*log(n+1)`),
`:190` (`min_win_rate=0.55` hard gate). Win rate ≠ edge: a 0.90-buyer wins 90 % at +11 %/−100 %
(≈0 EV); a value bettor at 0.30→0.45 wins 45 % (**excluded** by the gate) with large positive EV.
Gate and weight on **expectancy / profit-factor** (`mean_pnl` net of fee, `Σwins$/|Σlosses$|`),
not win rate; keep `log(n+1)` as evidence weight.

### Latency / resilience

**H9 — No supervisor / dead-man's switch.** `main.py:134-139` (`gather` of 4 loops, no
`return_exceptions`); `monitor.py:208` swallows a dead poll task. If the poller dies the bot
**looks alive but detects nothing forever**. Add a `supervise()` wrapper that restarts crashed
loops with backoff, and a heartbeat watchdog that alarms (and optionally exits non-zero for an
external restart) when `now − last_poll_completed > k × poll_interval`.
(`detection_stall_alert_seconds`.)

**H10 — WS reconnect gap.** `monitor.py:242-265`; after `_MAX_WS_RETRIES=5` the WS task
`return`s **permanently** (`:258`) and exits fall back to the 8 s `check_all_exits`, which fetches
prices **serially** (`copier.py:497-498`). A fast adverse move during an 80 s backoff blows
through SL unmanaged. Cap backoff (`min(delay,30)`), never permanently abandon the WS, **tighten**
the exit poll when WS is down (`exit_poll_fast_seconds: 2`), and parallelize the fallback:
```python
prices = await asyncio.gather(*(self.gamma.get_market_price(p.token_id) for p in positions))
```

**H11 — Per-tick SQLite on the exit hot path.** `copier.py:406` (`get_positions_by_token`) +
`:414` (`update_peak_price` with commit) per tick; aiosqlite serializes all DB ops. Under a tick
burst this is the bottleneck delaying TP/SL detection. Keep an in-memory
`dict[token_id, list[Position]]` (rehydrated at startup, mutated on open/close/partial), read it
with zero I/O in `handle_price_tick`, mutate peak in memory, and persist peak **debounced**
(`peak_persist_interval_seconds: 30`) or at close.

**H12 — Entry lock spans order I/O.** `copier.py:214-334`. The `_entry_lock` is held from
`position_count()` through `place_order()` + reconciliation, so a second whale's copy can't even
start while the first is mid-POST (the 0.1–1.5 s blocking call from C1). Shrink the critical
section to the TOCTOU-sensitive reserve (count check + `build_position`), release **before**
`place_order`, and track an in-memory `_pending_entries` counter to keep the hard cap exact
without holding the lock over I/O.

---

## 5. Tier 2 — MEDIUM: edge quality, microstructure, capacity

- **M1 — Remaining-edge check.** `copier.py:115-148`. You fetch `current_price` *after* the
  whale moved the book and copy there; `event.price` is only a 2 % deviation gate. Within that
  window you buy **into the whale's impact** with no check that edge remains. Require
  `current_price ≤ whale_price + (fee + buffer)`, i.e. you can still enter ~where they did.
- **M2 — Crowding / book-share cap.** Sizing is blind to book depth; the liquidity check only
  rejects, never sizes down. N bots on the same public whale stack into the same thin book in the
  same 8 s window → super-linear slippage, signal self-destructs. Size
  `min(formula, max_book_share_pct × depth_within_cap)` and discount the Nth copy of an
  already-held token (`crowding_discount`). (Consolidates the strategy/risk/execution crowding findings.)
- **M3 — Tiered resolution blackout.** `copier.py:125-130`. Replace the flat 24 h cliff: hard-skip
  only `< hard_blackout_hours` (6 h); in the 6–24 h tier require a non-extreme price (room for a
  managed exit) and optionally tighten size. Recovers the high-alpha final-day window.
- **M4 — Conviction signal.** Size keys off the whale's **absolute** USDC; a $2k throwaway from a
  $5M book is sized like a $2k conviction bet from a $50k book. Propagate each trader's typical
  size from `tracker.py` and scale by `event.size_usdc / typical` (capped).
- **M5 — Market-metadata TTL cache.** `gamma_client.get_market` refetched every event; metadata is
  static over minutes. Add a 60 s TTL cache (short negative-TTL for `None`) to drop a 50–300 ms leg
  off the hot path on repeat hits.
- **M6 — Regime/vol-adaptive TP/SL.** A single 40/25 split is applied to slow elections and gappy
  crypto/sports alike. Scale the fractions by a per-market category/vol multiplier from Gamma tags.
- **M7 — Event-level correlation cap.** Caps key on `market_id`/`trader`; multiple markets under one
  event are one bet. Add `_event_exposure` + `max_event_exposure_pct` (~0.12).
- **M8 — Don't time-exit winners.** `risk_manager.py:337-348`. A favorite grinding sideways near a
  high price *is* the thesis; the 48 h/<10 % rule dumps it (paying spread+fee) right before it
  resolves. Suppress time-exit when `pnl_at > 0` or within ~3× blackout of resolve.
- **M9 — Bankroll resync.** `get_balance()` is dead code; `risk.bankroll` only drifts by realized
  PnL, so every cap drifts from reality. In `rebalance_loop` (already hourly, live mode) set
  `risk.bankroll` from `await clob.get_balance()` with a `None`/error guard.
- **M10 — Detect averaging-down.** Every BUY is copied as fresh; a whale doubling down on a loser
  (they can ride to resolution; our tight stop can't) is a structural loser to copy. Track
  per-(wallet,token) prior entries from the activity backlog; skip when the new buy is materially
  below the prior average.
- **M11 — Wash-trade guard.** Scoring ingests `/activity` with zero counterparty checks; a wallet
  can self-cross to fake volume/consistency/Sharpe. Discard sub-second same-market round-trips,
  penalize volume concentrated in few illiquid markets, cap any single market's contribution to
  `trade_count`.
- **M12 — Raise `min_trades`.** At n=50, p̂=0.55 the 95 % CI is ±0.138 → [0.41, 0.69]; top-k of a
  noisy distribution ⇒ winner's curse. Raise to ~150–200, or gate on the **Wilson lower bound**.
- **M13 — Frequency & hold-loss bias.** `sharpe_proxy` is per-trade with no frequency
  normalization (5 trades/yr ranks = 200 trades/yr at equal per-trade Sharpe), and losing
  held-to-resolution positions emit no redeem record so they're dropped — biasing win-rate up for
  the hold cohort. Add a trades/week term; infer worthless expiries as −100 % at resolution.
- **M14 — Model 0/1 settlement.** Near resolution the winning side can't sell ~1.0 (no buyer) and
  the losing side can't sell at all; the bot books a mid-price sale that won't happen. If a
  blackout-exit can't fill, value at the implied outcome and handle on-chain redemption.
- **M15 — Tick/min-order rounding.** Raw-float price/size → off-tick or sub-min orders are
  venue-rejected: entries silently skipped, exits stuck open (dust can never exit). Fetch tick &
  min size, round (down for BUY, up for SELL), skip sub-min **before** reserving exposure.
- **M16 — WS head-of-line.** `monitor.py:281-289`. `_maybe_update_subscription` runs per inbound
  frame, and a slow `_on_price` (today's SQLite, H11) stalls the socket read. Drive subscription
  updates off an event/timer, and decouple ingest with a bounded `asyncio.Queue` (drop-oldest).
- **M17 — Async source-exits.** `_handle_source_exit` awaits exits serially **inside** the poll
  path, each up to 3 retries × backoff → a failing exit parks the wallet's next detection for
  seconds. `gather` the per-position exits and/or drain them on a dedicated worker so the poll
  loop never blocks on exit retries.

---

## 6. Tier 3 — LOW: polish

- **L1 — Adaptive "hot" polling.** After a wallet trades, drop *that wallet* to a 2 s interval for
  ~30 s, decaying back to 8 s — within the shared rate-limit budget (don't fan the whole set).
- **L2 — Reuse pooled session.** `monitor._poll_loop` builds its own session without
  `keepalive_timeout`, so idle keep-alive dies between 8 s polls (fresh TLS each time); its
  `AsyncLimiter(25,60)` is also independent of `DataClient`'s `(30,60)` → combined they can exceed
  the real cap and trigger 429s. Route polls through the shared `DataClient`.
- **L3 — Eager live-client init.** `create_or_derive_api_creds()` (blocking) runs lazily inside the
  first live order. Initialize via `run_in_executor` at startup so the first copy doesn't pay it.
- **L4 — Faster recency, safe parse.** Drop `half_life_days` to ~5–7; change `_parse_timestamp`'s
  failure fallback from `time.time()` (which fabricates freshness for dormant traders) to skipping
  the record.
- **L5 — Cooldown reason filter.** `_update_cooldown` counts **any** negative close; a `SOURCE_EXIT`
  or `TIME_EXIT` slightly-negative-after-fees shouldn't trip the 60 min halt. Count only
  `STOP_LOSS`/`TRAILING_STOP`.
- **L6 — Maker/taker fees.** Split `paper_taker_fee_pct` into `maker_fee_pct`/`taker_fee_pct` (with
  rebate support) and apply per actual order type from Polymarket's published schedule.
- **L7 — Capacity-aware, diversified selection.** Weight a trader's score by the median liquidity of
  the markets they trade (edge that lives in thin books we'd move is uncopyable), and diversify the
  final top-5 by penalizing high market-overlap so they aren't all the same favorite-buyers.

---

## 7. Suggested config additions

```yaml
copy_trading:
  max_favorable_deviation: 0.15      # H6  allow cheaper-than-whale entries up to this
  min_entry_price: 0.05              # H7  skip no-edge extremes
  max_entry_price: 0.95              # H7
  max_book_share_pct: 0.15           # M2  cap a copy at 15% of resting depth within slippage
  crowding_discount: 0.5             # M2  size *= 0.5**(prior copies of this token)
  max_conviction_mult: 2.0           # M4  cap size-up from unusually large whale trades
  poll_jitter_seconds: 2             # H17 anti-front-run jitter

risk_management:
  min_reward_risk: 1.0               # H2  SL never wider than TP/min_rr
  trailing_stop_fraction: 0.40       # H1  (raise from 0.15) give back 40% of run-up
  max_total_exposure_pct: 0.30       # H4  aggregate deployment ceiling
  hard_blackout_hours: 6.0           # M3  tiered resolution blackout
  soft_blackout_hours: 24.0          # M3
  near_resolution_size_mult: 0.5     # M3
  max_event_exposure_pct: 0.12       # M7  correlation cap across same-event markets
  maker_fee_pct: 0.0                 # L6
  taker_fee_pct: 0.02                # L6  reconcile vs real Polymarket schedule

trader_selection:
  leaderboard_window: "30d"          # H15 trailing window (require both 30d AND all-time)
  min_trades: 150                    # M12 (raise from 50) tighten win-rate CI
  half_life_days: 6                  # L4  faster decay of cold traders
  min_trade_frequency_per_week: 1.0  # M13 capital-efficiency floor

execution:
  clob_signer_threads: 2             # C1
  market_cache_ttl_seconds: 60       # M5
  market_cache_negative_ttl_seconds: 5
  peak_persist_interval_seconds: 30  # H11
  ws_max_backoff_seconds: 30         # H10
  exit_poll_fast_seconds: 2          # H10 tighten exit poll when WS down
  detection_stall_alert_seconds: 24  # H9  dead-man's switch (≈3× poll_interval)
  hot_poll_interval_seconds: 2       # L1
  hot_poll_window_seconds: 30        # L1
  tick_queue_maxsize: 1000           # M16
```

---

## 8. Phased rollout (each phase = one PR, paper-A/B before any live)

**Phase 1 — Live-correctness gate (Tier 0).** C1, C2, C3, C4, C5. *Nothing goes live before this.*
Add live-execution integration tests: marketable order type asserted; exit reconciliation;
double-exit idempotency (`rowcount` guard); `set_wallets` priming.

**Phase 2 — Risk thresholds & deployment safety.** H1, H2, H3, H4, M8, L5, M9. Pure
risk-math/config; unit-testable with no venue. Biggest paper-visible PnL-shape change (winners
stop being amputated; high-entry R:R floored).

**Phase 3 — Entry quality.** H6, H7, H8, H5, M1, M3, M4. Stop copying the worst entries; price
entries for real fills. Re-run paper A/B; expect higher per-trade EV, fewer trades.

**Phase 4 — Resilience & latency.** H9, H10, H11, H12, M5, M16, M17, L1, L2, L3. Supervisor +
dead-man's switch first (cheapest insurance), then hot-path I/O removal.

**Phase 5 — Trader-selection quality.** H13, H14, H15, H16, M11, M12, M13, L4, L7. Rework scoring
to expectancy-based, trailing-window, outlier-capped, with a live feedback loop.

**Phase 6 — Microstructure realism.** M2, M6, M7, M10, M14, M15, H17, L6. Crowding/book-share
sizing, settlement modeling, tick/min rounding, fee split.

---

## 9. Testing strategy

- **Risk math (Phases 2/3):** table-driven unit tests over the entry-price curve asserting the new
  R:R floor (H2), trailing-stop pullback tolerance (H1), total-exposure rejection (H4), and the
  directional deviation gate (H6) — no network needed.
- **Execution (Phase 1):** mock the py-clob-client; assert order *type* and marketable *price*
  (C2), exit fill-reconciliation paths (C3), and `close_position` idempotency under concurrent
  exits via `rowcount` (C4). Extend `tests/test_integration.py`'s un-awaited-coroutine guards to the
  new async exit path.
- **Resilience (Phase 4):** inject a crashing loop and assert the supervisor restarts it and the
  watchdog fires on a stalled heartbeat (H9); simulate WS death and assert exit cadence tightens
  and never permanently abandons (H10).
- **Selection (Phase 5):** property tests that a single low-variance outlier can't dominate the
  score (H14) and that an expectancy-positive low-win-rate value bettor is now eligible (H16).
- **Live shadow:** before risking capital, run live in a **dry-run** that signs and prices orders
  against the real book but does not submit, logging the would-be fill vs the paper assumption —
  this directly measures the C2/C3/H5 paper-vs-live gap on real depth.

---

*Generated from five independent expert code audits of `main` (post-PR #32). Items are
de-duplicated and prioritized; none duplicate already-merged work (§0). The plan document is the
only change in this PR.*
