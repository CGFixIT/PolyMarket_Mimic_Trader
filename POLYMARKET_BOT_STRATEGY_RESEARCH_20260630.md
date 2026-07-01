# Polymarket Bot Strategy Research - 2026-06-30

This report compares the current `PolyMarket_Mimic_Trader` strategy against the current public Polymarket API surface, live market data, academic prediction-market research, and practical automated-trading controls.

Scope: engineering and strategy research for this repository. This is not personal financial advice, and it does not validate live profitability.

## Direct verdict

The repo has a reasonable copy-trading safety posture for a retail Python bot: paper mode by default, stale-trade rejection, adverse-price gates, exposure caps, liquidity checks, FOK/FAK order handling, and conservative Kelly controls.

The production problem is integration drift. The current code still calls the legacy Data API leaderboard endpoint, and that endpoint returns HTTP 404 on 2026-06-30. Until that is fixed, trader discovery is broken regardless of how good the scoring model is.

The second problem is measurement. The bot has strong defensive guards, but it does not yet prove that copied trades remain positive after detection latency, spread, depth walk, taker fees, skipped fills, and source-trader survivorship bias. A successful Python Polymarket bot needs that instrumentation before live sizing is increased.

## Current strategy inventory

Repo behavior observed from `main` on 2026-06-30:

- Trader discovery fetches dual leaderboard windows and keeps wallets present in both all-time and recent windows. Code: `polymarket_copier/core/tracker.py:428`, `polymarket_copier/core/tracker.py:441`.
- Trader scoring is a weighted composite of capped Sharpe proxy, consistency, and recency. Code: `polymarket_copier/core/tracker.py:189`, `polymarket_copier/core/tracker.py:195`.
- Eligibility rejects low-sample or low-PnL traders. Config/code defaults include minimum total PnL, minimum trade count, expectancy threshold, recent window filtering, and recency half-life. Code: `polymarket_copier/core/tracker.py:61`, `polymarket_copier/config.py:29`.
- Trade copying defaults to flat 50 percent source size capped at 2 percent bankroll. Kelly sizing exists but is disabled in `config.yaml`. Code/config: `config.yaml:17`, `config.yaml:37`, `polymarket_copier/core/copier.py:357`, `polymarket_copier/core/copier.py:360`.
- Entry gates include stale-trade rejection, adverse price deviation, favorable price-collapse cap, extreme price band, post-fee edge check, market volume check, and exposure caps. Code/config: `config.yaml:33`, `polymarket_copier/core/copier.py:220`, `polymarket_copier/core/copier.py:281`, `polymarket_copier/core/copier.py:314`, `polymarket_copier/core/copier.py:327`.
- Live order handling checks order book depth, uses size-aware slippage, sends FOK entries, uses FAK exits, and reconciles fills. Code: `polymarket_copier/api/clob_client.py:170`, `polymarket_copier/api/clob_client.py:258`, `polymarket_copier/core/copier.py:541`, `polymarket_copier/core/copier.py:771`.
- Exits use range-relative TP/SL, trailing stop, time exit, source-exit mirroring, daily circuit breaker, and exposure release. Code: `polymarket_copier/core/risk_manager.py:300`, `polymarket_copier/core/risk_manager.py:376`, `polymarket_copier/core/risk_manager.py:401`.
- Monitoring is REST polling for wallet activity plus WebSocket price updates for tokens already held. Code: `polymarket_copier/core/monitor.py:13`, `polymarket_copier/core/monitor.py:17`, `polymarket_copier/core/monitor.py:231`.

## Evidence gathered

### Official Polymarket docs

- Trader leaderboard docs now specify `GET /v1/leaderboard` with `category`, `timePeriod`, `orderBy`, `limit`, and `offset`; response fields include `proxyWallet`, `userName`, `vol`, `pnl`, `xUsername`, and `verifiedBadge`.
- WebSocket docs list `wss://ws-subscriptions-clob.polymarket.com/ws/market` for the market channel and `wss://ws-subscriptions-clob.polymarket.com/ws/user` for authenticated user events.
- Market/user WebSocket docs require application-level heartbeats: send `PING` every 10 seconds and expect `PONG`.
- Trading overview says Polymarket CLOB is off-chain matching with on-chain settlement, non-custodial EIP-712 signed orders, and recommends `py-clob-client-v2`.
- Trading overview says new API users should use deposit wallets with signature type `3` and a funder address.
- Rate-limit docs currently show Data API general limit of 1,000 requests per 10 seconds, `/trades` at 200 requests per 10 seconds, `/positions` and `/closed-positions` at 150 requests per 10 seconds, CLOB `/book`, `/price`, and `/midpoint` at 1,500 requests per 10 seconds, and `POST /order` at 5,000 requests per 10 seconds burst with 120,000 per 10 minutes sustained.

Sources:

- https://docs.polymarket.com/api-reference/core/get-trader-leaderboard-rankings.md
- https://docs.polymarket.com/api-reference/core/get-user-activity.md
- https://docs.polymarket.com/market-data/websocket/overview.md
- https://docs.polymarket.com/trading/overview.md
- https://docs.polymarket.com/api-reference/rate-limits.md

### Live Polymarket API checks on 2026-06-30

Legacy endpoint used by this repo:

```text
GET https://data-api.polymarket.com/leaderboard?window=all&limit=10
Result: HTTP 404
```

Current endpoint from docs:

```text
GET https://data-api.polymarket.com/v1/leaderboard?category=OVERALL&timePeriod=ALL&orderBy=PNL&limit=10&offset=0
Result: HTTP 200
```

Top all-time PnL sample:

| Rank | User | Volume | PnL |
| --- | --- | ---: | ---: |
| 1 | Theo4 | 43,013,258.52 | 22,053,933.75 |
| 2 | Fredi9999 | 76,611,316.91 | 16,619,506.63 |
| 3 | swisstony | 1,339,562,780.02 | 13,683,570.75 |
| 4 | kch123 | 293,707,578.62 | 11,386,690.15 |
| 5 | RN1 | 790,257,399.04 | 10,429,481.15 |

Top monthly overall PnL sample:

| Rank | User | Volume | PnL |
| --- | --- | ---: | ---: |
| 1 | mintblade | 17,759,922.23 | 9,238,344.62 |
| 2 | fishalive | 13,281,460.37 | 9,063,378.18 |
| 3 | frostrizz | 23,091,318.16 | 8,928,561.12 |
| 4 | sparklingwater123 | 19,001,698.93 | 8,474,966.27 |
| 5 | GRIMDRIP | 13,603,969.28 | 7,602,742.06 |

Top monthly finance category PnL sample:

| Rank | User | Volume | PnL | X username |
| --- | --- | ---: | ---: | --- |
| 1 | boyau | 2,780,680.92 | 336,833.46 | |
| 2 | donthackme | 1,424,881.33 | 131,982.04 | |
| 3 | KiAr | 890,464.32 | 104,253.21 | |
| 4 | Liquidifier | 482,528.11 | 86,928.71 | |
| 5 | balthazar | 1,389,182.82 | 68,133.56 | balthazarpoly |

Market-liquidity sample from Gamma active events:

| Event | 24h volume | Liquidity | Spread | Fee rate | Accepting orders |
| --- | ---: | ---: | ---: | ---: | --- |
| World Cup Winner | 120,581,696.27 | 183,268,138.75 | 0.001 | 0.03 | true |
| France vs. Sweden - More Markets | 24,105,087.76 | 7,495,737.49 | 0.01 | 0.03 | true |
| France vs. Sweden | 23,344,643.28 | 476,266.05 | 0.001 | 0.03 | true |
| Next Prime Minister of Ethiopia? | 12,085,727.74 | 158,535.74 | 0.004 | null | true |
| France vs. Sweden - Exact Score | 4,911,399.56 | 1,966,578.73 | 0.001 | 0.03 | true |

Low-liquidity Gamma samples included active or stale-looking events with tiny liquidity, null 24h volume, closed markets, `acceptingOrders=false`, and even one unresolved market with a 1.0 spread. This confirms that volume alone is not enough; the bot should gate on `active`, `closed`, `acceptingOrders`, spread, depth, and resolution state.

### Academic and market-structure sources

- Wolfers and Zitzewitz, "Prediction Markets", documents why prediction markets can aggregate dispersed information, but that does not imply every visible top wallet is copyable alpha after latency and costs.
- Manski, "Interpreting the Predictions of Prediction Markets", argues market prices are not mechanically equal to objective probabilities under all trader preference and belief structures. That matters because the bot uses prices as implied odds and trader PnL as skill evidence.
- Kelly, "A New Interpretation of Information Rate", is the mathematical basis for growth-optimal sizing, but Kelly is brittle when edge estimates are noisy. Fractional Kelly and hard exposure caps are appropriate here.
- FINRA algorithmic-trading guidance is not Polymarket-specific, but the operational controls map cleanly: pre-trade risk controls, kill switches, supervision, testing, and monitoring.

Sources:

- https://users.nber.org/~jwolfers/papers/Predictionmarkets.pdf
- https://www.nber.org/papers/w10504
- https://www.nber.org/papers/w12107
- https://web.archive.org/web/20060912053624/http://www.faculty.econ.northwestern.edu/faculty/manski/prediction_markets.pdf
- https://www.princeton.edu/~wbialek/rome/refs/kelly_56.pdf
- https://www.finra.org/rules-guidance/key-topics/algorithmic-trading

## Findings

### 1. Blocker: trader discovery uses a dead leaderboard endpoint

Severity: critical.

The repo calls `/leaderboard` with `window=all` or `window=30d`:

- `polymarket_copier/core/tracker.py:441`
- `polymarket_copier/core/tracker.py:442`
- `polymarket_copier/api/data_client.py:65`
- `tests/test_data_client.py:40`

That endpoint returned HTTP 404 during this research pass. The current docs and live API use `/v1/leaderboard`.

Current response fields also changed. The repo appears to expect address-like data in `name` and pseudonym in `pseudonym`:

- `polymarket_copier/core/tracker.py:474`
- `polymarket_copier/core/tracker.py:475`

The current API returns `proxyWallet`, `userName`, `vol`, `pnl`, `xUsername`, and `verifiedBadge`.

Impact: `TrackerClient.refresh()` cannot reliably discover current top traders. The bot can run, but the strategy input is broken.

Recommended fix:

- Change leaderboard URL to `/v1/leaderboard`.
- Map repo windows to current params:
  - `all` -> `timePeriod=ALL`
  - `30d` -> `timePeriod=MONTH`
  - use `category=OVERALL`
  - use `orderBy=PNL`
- Normalize both old and new schemas in one parser:
  - wallet: `proxyWallet` fallback `name`
  - display name: `userName` fallback `pseudonym`
  - volume: `vol`
  - pnl: `pnl`
- Add contract tests for both legacy fixture shape and current API shape.
- Add one integration-smoke test that can be run manually without secrets.

### 2. High: live-trading auth and SDK assumptions are stale

Severity: high for live mode; low for paper mode.

The repo pins `py-clob-client>=0.34.0,<1.0`:

- `requirements.txt:1`

Official trading docs now recommend `py-clob-client-v2`, and the docs require signature type plus funder address for wallet setup. The current repo config exposes private key and L2 credentials but does not expose signature type or funder address:

- `polymarket_copier/config.py:239`
- `polymarket_copier/api/clob_client.py:104`
- `polymarket_copier/api/clob_client.py:111`

Impact: live trading may fail for new deposit-wallet users or behave differently from current documented examples.

Recommended fix:

- Add config fields for `signature_type` and `funder`.
- Evaluate migration from `py-clob-client` to `py-clob-client-v2` in a separate PR.
- Keep paper mode as the default and add an explicit live-mode startup validation that prints redacted wallet/auth mode, never secrets.

### 3. High: WebSocket channel path and heartbeat handling should be reconciled with current docs

Severity: high for exits and price-reactive risk management.

The repo uses:

- `wss://ws-subscriptions-clob.polymarket.com/ws/` in `polymarket_copier/core/monitor.py:57`
- `ping_interval=15` in `polymarket_copier/core/monitor.py:339`
- subscription `"type": "Market"` and `"assets_ids"` in `polymarket_copier/core/monitor.py:366`

Current docs specify:

- market channel path: `/ws/market`
- user channel path: `/ws/user`
- market channel uses `assets_ids`
- user channel uses `markets`
- app-level `PING` every 10 seconds for market/user channels
- optional `custom_feature_enabled` for `best_bid_ask`, `new_market`, and `market_resolved`

Impact: a TCP/WebSocket control-frame ping is not the same thing as an application-level `PING` message if the server expects text messages. Exit feeds can silently degrade to REST polling.

Recommended fix:

- Use `/ws/market` for public market data.
- Add explicit text `PING` every 10 seconds and handle `PONG`.
- Add protocol tests for `book`, `price_change`, `last_trade_price`, `best_bid_ask`, and `market_resolved`.
- Consider enabling `custom_feature_enabled` if using best bid/ask or resolution events.

### 4. High: repo docs/config/code disagree on sizing and trailing stops

Severity: high for operator expectations.

The README says position sizes are computed via fractional Kelly, but `config.yaml` has `kelly_enabled: false`:

- `README.md:58`
- `config.yaml:37`

The config model default for trailing stop is 0.40, but `config.yaml` sets 0.15:

- `polymarket_copier/config.py:187`
- `polymarket_copier/core/risk_manager.py:126`
- `config.yaml:48`

The code default for trader recency half-life is 7 days, while `config.yaml` sets 14:

- `polymarket_copier/config.py:30`
- `polymarket_copier/core/tracker.py:72`
- `config.yaml:13`

Impact: operators will misread risk behavior. This is especially dangerous for a trading bot because "docs say conservative" and "config says aggressive" are both plausible interpretations depending on which file someone reads.

Recommended fix:

- Decide whether sample config or code defaults are canonical.
- Update README language to say "Kelly-capable, disabled by default" unless enabling it.
- Add a config consistency test that loads `config.yaml` and asserts intentional values.

### 5. Medium-high: copy trading is latency constrained by REST wallet polling

Severity: medium-high for profitability, low for correctness.

The monitor is honest about the architecture: WebSocket is for price feeds after the bot holds a position; new source-wallet trades are detected by REST polling. Code comments state public market channel events are not wallet-filtered.

- `polymarket_copier/core/monitor.py:13`
- `polymarket_copier/core/monitor.py:17`
- `polymarket_copier/core/monitor.py:506`

The repo mitigates stale copy risk with `max_trade_age_seconds: 12`, adverse price deviation, post-fee edge checks, and FOK entries. That is the right direction. The missing piece is measurement.

Impact: the bot may avoid bad late copies, but it cannot prove profitability unless it records detection latency, skipped-trade reasons, fill slippage, and post-copy PnL by source wallet and market type.

Recommended fix:

- Persist per-trade telemetry:
  - source trade timestamp
  - local detection timestamp
  - order submitted timestamp
  - fill timestamp
  - source price
  - observed top of book
  - executed price
  - skipped reason
  - market volume/liquidity/spread at decision time
  - realized PnL including fees
- Add daily paper/live calibration reports.
- Do not enable Kelly sizing until this telemetry shows stable positive edge after costs.

### 6. Medium: rate limiting is probably overconservative, but adaptive throttling is safer than hardcoding faster polling

Severity: medium.

The README/monitor architecture warns about low request budgets. Current official docs allow materially higher Data API and CLOB request rates than the repo assumes. But public rate limits can still be Cloudflare-shaped, IP-shaped, or temporarily changed.

Impact: the bot may under-monitor too few wallets or use a slower polling interval than necessary. Conversely, blindly lowering poll interval can trigger 429s or bans.

Recommended fix:

- Keep conservative defaults.
- Add adaptive rate limiting based on observed `429`, latency, and success rate.
- Record per-endpoint request rate metrics.
- Use a separate config for wallet-poll concurrency and Data API budget.

### 7. Medium: leaderboard social metadata is useful context, not alpha

Severity: medium.

The current `/v1/leaderboard` response includes `xUsername` and `verifiedBadge`. In the finance category sample, some top traders expose X usernames, but most do not.

Impact: social identity can help human review and fraud/spam filtering. It is not a robust trading signal by itself. Social hype is survivorship-biased and easy to manipulate.

Recommended fix:

- Store `xUsername` and `verifiedBadge` as metadata only.
- Do not increase sizing because a wallet has a social identity.
- Consider a manual allowlist/denylist for source wallets after reviewing behavior.

### 8. Medium: market selection needs explicit fee/spread/resolution filters

Severity: medium.

Live Gamma samples show highly liquid markets with 0.001 spreads and fee rate 0.03, but also tiny-liquidity or closed/stale markets. Some markets have `feeSchedule=null`; others are `takerOnly=true`.

The repo already has volume and price guards, but successful Polymarket bots need event-level controls:

- skip closed markets
- skip non-accepting markets
- skip markets with missing or one-sided book depth
- skip high-spread markets unless the source price is still favorable after depth walk and fees
- skip markets near resolution or with ambiguous settlement conditions
- differentiate sports/finance/politics/liquidity profile instead of treating all markets as equivalent

Recommended fix:

- Add decision-time snapshots from Gamma and CLOB into the trade log.
- Gate on `active`, `closed`, `acceptingOrders`, `spread`, best bid/ask, fee schedule, and resolution metadata.
- Add category-level performance attribution before expanding the tracked wallet list.

### 9. Medium: paper fills are intentionally conservative, but should be reconciled against real fills

Severity: medium.

Paper mode uses slippage plus taker fee. That is better than optimistic midpoint paper trading. However, Polymarket order books can be discontinuous and size-sensitive. A constant paper slippage model is still only an approximation.

Recommended fix:

- In paper mode, optionally fetch real book depth and compute simulated VWAP at decision time.
- Compare synthetic paper fill to live top-of-book/liquidity even before live trading.
- Report paper/live delta by market and order size.

### 10. Low: strategy naming should stop implying "mimic top wallet equals edge"

Severity: low, but important for operator discipline.

Academic prediction-market research supports information aggregation at the market level. It does not prove that copying visible top wallets after the fact is profitable. A stronger framing is:

"Use leaderboard wallets as candidates, then require measurable post-cost copyability before allocating risk."

That distinction matters. The bot should not trust leaderboard PnL without testing whether the edge survives copy latency and execution cost.

## What a successful Python Polymarket bot should do

A production-survivable Python bot for this use case should have the following shape:

- Current API adapters with schema tests against recorded fixtures and live smoke tests.
- Separate modules for market data, wallet activity, order execution, risk, persistence, and reporting.
- Event-driven internals with idempotent trade processing and explicit state transitions.
- Pre-trade risk checks that fail closed on missing price, stale data, missing order book, exposure cap breach, or auth uncertainty.
- Depth-aware order sizing using best bid/ask and VWAP, not midpoint assumptions.
- Strict secret handling with no keys in logs, tracebacks, fixtures, or reports.
- Paper/live parity metrics: what the bot would have done, what live market depth allowed, and what the realized outcome was.
- Kill switches for daily loss, total exposure, repeated API failures, repeated auth failures, and repeated order failures.
- Source-trader attribution: every copied trade tied back to wallet, market, latency, price delta, and realized PnL.
- Conservative fractional Kelly only after enough bot-specific closed trades, not just source-wallet history.

## Recommended implementation sequence

1. Fix leaderboard endpoint and schema parsing.
2. Add recorded fixtures for current `/v1/leaderboard` and `/activity` responses.
3. Reconcile WebSocket URL, subscription casing, and text heartbeat handling with current docs.
4. Add signature type and funder config; evaluate `py-clob-client-v2` migration.
5. Add trade-decision telemetry and skipped-reason persistence.
6. Add paper-mode VWAP simulation from real order book snapshots.
7. Add a daily report that ranks source wallets by copied-trade PnL, not source-wallet leaderboard PnL.
8. Keep Kelly disabled until the bot has its own statistically meaningful post-cost sample.

## Draft backlog

### P0

- `tracker`: migrate to `/v1/leaderboard`.
- `tracker`: parse `proxyWallet` and `userName`.
- `tests`: update Data API tests for current params.
- `monitor`: update WebSocket endpoint to `/ws/market`.
- `monitor`: add text `PING`/`PONG` heartbeat loop.

### P1

- `config`: add `signature_type` and `funder`.
- `clob_client`: validate live auth mode on startup.
- `config/docs`: resolve Kelly and trailing-stop default drift.
- `portfolio`: persist decision-time market snapshot fields.
- `metrics`: emit detection latency, fill latency, slippage, and skipped-reason counters.

### P2

- `reports`: add daily paper/live calibration report.
- `tracker`: store leaderboard social metadata as non-sizing metadata.
- `risk`: add category-aware exposure caps after enough attribution data exists.
- `research`: periodically re-check official docs for endpoint/schema drift.

## Remaining assumptions and limits

- Chrome extension control was not available in this environment, and the in-app browser timed out on the docs page. Research used public HTTP docs, public APIs, and command-line checks instead.
- No authenticated Polymarket account, private wallet data, or live order placement was used.
- No authenticated X/social-media account was used. Social evidence is limited to official leaderboard metadata such as `xUsername` and `verifiedBadge`.
- Live API data is a point-in-time sample from 2026-06-30 and will change.
- This report does not claim the bot is profitable. It identifies what must be fixed and measured before that claim is defensible.
