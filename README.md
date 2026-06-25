# PolyMarket_Mimic_Trader
[![CodeQL – PolyMarket Mimic Trader](https://github.com/CGFixIT/PolyMarket_Mimic_Trader/actions/workflows/codeql.yml/badge.svg)](https://github.com/CGFixIT/PolyMarket_Mimic_Trader/actions/workflows/codeql.yml)
[![CI](https://github.com/CGFixIT/PolyMarket_Mimic_Trader/actions/workflows/ci.yml/badge.svg)](https://github.com/CGFixIT/PolyMarket_Mimic_Trader/actions/workflows/ci.yml)

A Python copy-trading bot that monitors the most successful traders on [Polymarket](https://polymarket.com), identifies their new trades in real time, and copies them with conservative, range-relative risk management designed specifically for prediction markets.

## Why This Exists

Only 7.6% of Polymarket wallets are profitable, but the top performers consistently outperform by applying disciplined strategies across many markets. This bot identifies those traders, monitors their activity, and mirrors their entries with tighter risk controls so you capture the same edge with less downside exposure.

## How It Works

```
1. DISCOVER   Fetch the Polymarket leaderboard, score traders by
              Sharpe ratio × Consistency × Recency (not raw PnL)
              Dual-window filter: traders must rank in both all-time
              and recent 30-day windows to qualify

2. MONITOR    Poll tracked wallets every 8s for new BUY trades
              WebSocket feeds real-time prices for open positions
              Per-wallet activity cache reduces redundant API calls

3. COPY       Mirror entries sized by fractional Kelly criterion
              Skip if price moved >2%, volume <$5K, or market resolves <24h
              Fee + spread deducted from edge before sizing

4. MANAGE     Range-relative TP/SL (not flat %), trailing stop,
              per-market 8% exposure cap, daily loss circuit breaker
              In-memory position cache for zero-latency WS exits

5. EXIT       Automated exits on TP, SL, trailing stop, time exit,
              resolution blackout, or daily loss limit
              Source-exit mirroring when tracked trader exits
```

## Key Design Decisions

### Range-Relative Take Profit / Stop Loss

Polymarket tokens are bounded in [0, 1]. Flat-percentage TP/SL breaks at extremes:

| Entry | Naive 15% TP | Range-Relative TP | Range-Relative SL |
|-------|-------------|-------------------|-------------------|
| $0.20 | $0.23 | **$0.52** (40% of $0.80 upside) | $0.15 |
| $0.50 | $0.575 | **$0.70** (40% of $0.50 upside) | $0.375 |
| $0.82 | $0.943 | **$0.892** (40% of $0.18 upside) | $0.615 |
| $0.97 | $1.12 (impossible) | **$1.00** (clamped) | $0.727 |

### Risk-Adjusted Trader Scoring

Traders are ranked by `Sharpe_proxy × Consistency × Recency_weight`, not raw PnL. This filters out lucky concentrated bettors in favor of consistently profitable traders across many markets. Additional guards:
- Sharpe proxy capped at 3.0 and shrunk for small samples to prevent outlier amplification
- Traders must rank in both the all-time and trailing 30-day leaderboard windows
- Expectancy / profit-factor weighted instead of raw win rate to avoid favorite-buyer bias

### Fractional Kelly Sizing

Position sizes are computed via fractional Kelly criterion rather than a flat multiplier. The Kelly probability input is derived from the trader's demonstrated mean ROI (not raw win rate), and activates only once the trader has ≥50 closed trades. A tracker-derived prior is used during warm-up with time-decay weighting so stale leaderboard data contributes less.

### WebSocket + REST Hybrid Monitor

- **WebSocket** feeds real-time prices for positions we hold (sub-second latency for exits)
- **REST polling** detects new trades from tracked wallets (the WS API doesn't filter by wallet)
- Shared `aiohttp.ClientSession` with keep-alive across all API clients eliminates redundant TLS handshakes

## Project Structure

```
polymarket_copier/
├── main.py                    # Async CLI entrypoint, startup sequence, supervisor loops
├── config.py                  # Pydantic v2 settings from .env + config.yaml
├── api/
│   ├── data_client.py         # Polymarket Data API (leaderboard, wallet activity)
│   ├── gamma_client.py        # Gamma API (markets, resolve times, prices)
│   └── clob_client.py         # CLOB API (order placement, depth checks, paper fills)
├── core/
│   ├── tracker.py             # Trader discovery, dual-window scoring, activity cache
│   ├── monitor.py             # WebSocket + REST trade/price monitor, WS reconnect
│   ├── copier.py              # Copy-trade decision engine, entry/exit lock guards
│   ├── risk_manager.py        # Range-relative TP/SL, exposure caps, circuit breakers
│   ├── portfolio.py           # SQLite (WAL mode) position persistence, composite indexes
│   ├── sizing.py              # Fractional Kelly criterion sizing
│   └── metrics.py             # Optional Prometheus metrics (prometheus_client)
├── models/
│   └── types.py               # Pydantic v2 models (Market, Order)
└── utils/
    ├── addresses.py           # Ethereum address normalization (lowercase at all ingestion points)
    └── logger.py              # Structured JSON logging for downstream analysis
```

## Quick Start

### Prerequisites

- Python 3.10+ (tested on 3.10, 3.11, 3.12)
- A Polygon wallet with USDC (for live trading only)

### Installation

```bash
git clone https://github.com/CGFixIT/PolyMarket_Mimic_Trader.git
cd PolyMarket_Mimic_Trader
pip install -r requirements.txt
```

### Configuration

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
POLY_PRIVATE_KEY=       # Your Polygon wallet private key (live mode only)
POLY_API_KEY=           # L2 API key (auto-derived if blank)
POLY_API_SECRET=        # L2 API secret
POLY_API_PASSPHRASE=    # L2 API passphrase
BANKROLL=500            # Starting bankroll in USDC
```

All trading parameters are in `config.yaml`. The defaults are conservative:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `size_multiplier` | 0.5 | Copy at 50% of source trade size (Kelly overrides when enabled) |
| `max_trade_pct` | 0.02 | Max 2% of bankroll per trade |
| `tp_range_fraction` | 0.40 | Take profit at 40% of remaining upside |
| `sl_range_fraction` | 0.25 | Stop loss at 25% of remaining downside |
| `trailing_stop_fraction` | 0.15 | Trail 15% below peak-to-SL gap |
| `max_market_exposure_pct` | 0.08 | Max 8% of bankroll in any single market |
| `max_trader_allocation` | 0.05 | Max 5% of bankroll copied from any single trader |
| `daily_loss_limit_pct` | 0.03 | Halt all trading after 3% daily loss (resets at UTC midnight) |
| `resolution_blackout_hours` | 24 | Never enter markets resolving within 24h |
| `max_concurrent_positions` | 10 | Maximum open positions at once |
| `max_trade_age_seconds` | 12 | Skip trades older than this at detection |
| `cooldown_after_losses` | 3 | Pause new entries after this many consecutive losses |
| `cooldown_minutes` | 60 | Length of the post-loss cooldown |
| `kelly_enabled` | false | Enable fractional Kelly sizing (requires ≥50 closed trades to activate) |
| `kelly_min_trades` | 50 | Minimum closed trades before Kelly activates |
| `kelly_fraction` | 0.25 | Fraction of full Kelly to use (0.25 = quarter-Kelly) |
| `fail_closed_on_missing_data` | true | Skip a copy when market metadata or price can't be verified |
| `mirror_source_exits` | true | Exit when the tracked trader exits (SOURCE_EXIT) |
| `paper_fill_slippage_pct` | 0.005 | Half-spread slippage in paper mode (~0.5%) |
| `paper_taker_fee_pct` | 0.02 | Taker fee in paper mode (Polymarket CLOB rate ~2%) |
| `metrics_enabled` | false | Enable Prometheus metrics scrape endpoint |
| `metrics_port` | 9090 | Port for Prometheus scrape endpoint |

### Running

**Paper mode** (no real trades, logs what would execute):

```bash
python -m polymarket_copier.main --mode paper
```

**Live mode** (requires `POLY_PRIVATE_KEY` in `.env`):

```bash
python -m polymarket_copier.main --mode live
```

## Risk Controls

The bot enforces multiple layers of protection:

1. **Range-relative TP/SL** — thresholds adapt to token price within [0, 1]
2. **Trailing stop** — locks in profit as price rises, never drops below hard SL
3. **Time exit** — closes stale positions after 48h if price barely moved
4. **Per-market exposure cap** — max 8% of bankroll in any single market
5. **Per-trader allocation cap** — max 5% of bankroll copied from any single trader
6. **Daily loss circuit breaker** — halts all trading after 3% daily loss; resets at UTC midnight
7. **Resolution blackout** — never enters markets resolving within 24h
8. **Pre-trade depth check** — verifies ask-side liquidity before BUY orders (live mode)
9. **Per-trader drawdown stop** — stops copying a trader after cumulative -8% session loss
10. **Cooldown** — pauses new entries after 3 consecutive losing trades
11. **Staleness gate** — skips trades older than 12s at detection
12. **Fail-closed gating** — skips a copy when market metadata or price can't be verified
13. **Cold-start guard** — first poll per wallet seeds a baseline; no copies from the backlog
14. **Source exit mirroring** — exits when the tracked trader exits (aligns holding period)
15. **Rate-limited poll path** — `AsyncLimiter` gates REST polls to prevent 429s
16. **Realistic paper fills** — paper mode applies slippage + taker fee
17. **Concurrent exit lock** — per-position `asyncio.Lock` prevents double-SELL race between WebSocket tick and poll sweep
18. **Entry TOCTOU lock** — global `asyncio.Lock` around position-count check + open prevents simultaneous wallet polls from both passing the cap
19. **Fee-aware sizing** — expected round-trip fee deducted from edge before Kelly sizing
20. **Wallet address normalization** — all Ethereum addresses lowercased at ingestion to prevent case-mixing dict lookup misses

## Testing

```bash
# Run all tests (453 tests)
pytest -v

# Run only the integration tests
pytest tests/test_integration.py -v

# Run only the risk manager tests
pytest tests/test_risk_manager.py -v

# Run chaos/resilience tests
pytest tests/test_chaos.py -v

# With coverage report
pytest --cov=polymarket_copier --cov-report=term-missing
```

The test suite includes:
- **Unit tests** for every module (config, models, API clients, risk manager, tracker, portfolio, copier, monitor, sizing, addresses)
- **Integration tests** for the full `TradeMonitor → CopyTrader` callback chain
- **Chaos tests** — network errors, API 429/500s, malformed data, concurrent exits, WS death + recovery
- All tests run offline with mocked API responses

## Architecture

### Data Flow

```
Polymarket Data API                    Polymarket CLOB WebSocket
       │                                        │
       ▼                                        ▼
  TradeMonitor._poll_loop()         TradeMonitor._ws_loop()
  (detect new trades from            (real-time price feed for
   tracked wallets, 8s + jitter)      subscribed token positions)
       │                                        │
       ▼                                        ▼
  CopyTrader.handle_trade_event()   CopyTrader.handle_price_tick()
  (validate, size, place order)      (evaluate TP/SL/trail/time exit)
       │                                        │
       ▼                                        ▼
  RiskManager.build_position()       RiskManager.evaluate()
  (compute range-relative TP/SL,     (check all exit conditions,
   enforce exposure cap)              update trailing peak)
       │                                        │
       ▼                                        ▼
  ClobClient.place_order()           ClobClient.place_order() [SELL]
  (BUY with depth check)             (exit the position)
       │                                        │
       ▼                                        ▼
  PortfolioManager (SQLite)          PortfolioManager (SQLite)
  (persist open position)            (mark closed, record PnL)
```

### Startup Sequence

1. Load config + logger → init `RiskManager`, `PortfolioManager`, shared `aiohttp.ClientSession`
2. Rehydrate market/trader exposure from DB open positions (single DB fetch, passed to both rehydration steps)
3. Fetch top traders via `TrackerClient.refresh()` (dual-window leaderboard, activity cache)
4. Launch concurrent tasks: `monitor.run()`, `rebalance_loop()`, `exit_check_loop()`, `shutdown_watcher()`
5. SIGINT/SIGTERM → set `shutdown_event` → cancel tasks → print portfolio summary → exit

### Key APIs Used

| API | Base URL | Auth | Purpose |
|-----|----------|------|---------|
| Data API | `data-api.polymarket.com` | None | Leaderboard, wallet activity |
| Gamma API | `gamma-api.polymarket.com` | None | Market discovery, resolve times |
| CLOB API | `clob.polymarket.com` | L1/L2 | Order placement, midpoint prices |
| CLOB WebSocket | `ws-subscriptions-clob.polymarket.com` | None | Real-time price feeds |

## Observability

When `metrics_enabled: true` in `config.yaml`, a Prometheus scrape endpoint is exposed on `metrics_port` (default 9090). Available metrics:

- `polymarket_bankroll_usdc` — current bankroll
- `polymarket_daily_pnl_usdc` — today's realized PnL
- `polymarket_open_positions` — count of open positions
- `polymarket_copies_skipped_total` — copies skipped by reason label
- `polymarket_orders_placed_total` — orders placed by side (BUY/SELL)

Structured JSON log events are emitted on the `data` logger channel for downstream analysis:
- `position_opened`, `position_closed` — entry/exit with price, size, PnL
- `copy_skipped` — every skipped copy with a stable reason code
- `circuit_breaker_tripped` — daily loss limit hit
- `trader_demoted` — trader removed from pool after drawdown

## Disclaimer

This software is provided for educational and research purposes. Trading on prediction markets carries financial risk. Past performance of copied traders does not guarantee future results. Always start with paper mode and a small bankroll. The authors are not responsible for any financial losses incurred through use of this software.

## License

MIT
