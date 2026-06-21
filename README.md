# PolyMarket_Mimic_Trader
[![CodeQL – PolyMarket Mimic Trader](https://github.com/CGFixIT/PolyMarket_Mimic_Trader/actions/workflows/codeql.yml/badge.svg)](https://github.com/CGFixIT/PolyMarket_Mimic_Trader/actions/workflows/codeql.yml)
[![CI](https://github.com/CGFixIT/PolyMarket_Mimic_Trader/actions/workflows/ci.yml/badge.svg)](https://github.com/CGFixIT/PolyMarket_Mimic_Trader/actions/workflows/ci.yml)

A Python copy-trading bot that monitors the most successful traders on [Polymarket](https://polymarket.com), identifies their new trades in real time, and copies them with conservative, range-relative risk management designed specifically for prediction markets.

## Why This Exists

Only 7.6% of Polymarket wallets are profitable, but the top performers consistently outperform by applying disciplined strategies across many markets. This bot identifies those traders, monitors their activity, and mirrors their entries with tighter risk controls so you capture the same edge with less downside exposure.
- NOTE: I havent even tested this yet outside of free emulation environments so use at your own risk if you know what the code is doing. I take no responsibility but wanted to share for fun. will update if I test in real scenario with money attached to it

## How It Works

```
1. DISCOVER   Fetch the Polymarket leaderboard, score traders by
              Sharpe ratio x Consistency x Recency (not raw PnL)

2. MONITOR    Poll tracked wallets every 8s for new BUY trades
              WebSocket feeds real-time prices for open positions

3. COPY       Mirror entries at 0.5x size (max 2% of bankroll per trade)
              Skip if price moved >2%, volume <$5K, or market resolves <24h

4. MANAGE     Range-relative TP/SL (not flat %), trailing stop,
              per-market 8% exposure cap, daily loss circuit breaker

5. EXIT       Automated exits on TP, SL, trailing stop, time exit,
              resolution blackout, or daily loss limit
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

Traders are ranked by `Sharpe_proxy x Consistency x Recency_weight`, not raw PnL. This filters out lucky concentrated bettors in favor of consistently profitable traders across many markets.

### WebSocket + REST Hybrid Monitor

- **WebSocket** feeds real-time prices for positions we hold (sub-second latency for exits)
- **REST polling** detects new trades from tracked wallets (the WS API doesn't filter by wallet)

## Project Structure

```
polymarket_copier/
├── main.py                    # Async CLI entrypoint
├── config.py                  # Settings from .env + config.yaml
├── api/
│   ├── data_client.py         # Polymarket Data API (leaderboard, activity)
│   ├── gamma_client.py        # Gamma API (markets, resolve times, prices)
│   └── clob_client.py         # CLOB API (order placement, depth checks)
├── core/
│   ├── tracker.py             # Trader discovery and Sharpe-based scoring
│   ├── monitor.py             # WebSocket + REST trade/price monitor
│   ├── copier.py              # Copy-trade decision engine
│   ├── risk_manager.py        # Range-relative TP/SL, exposure caps, circuit breakers
│   └── portfolio.py           # SQLite-backed position persistence
├── models/
│   └── types.py               # Pydantic v2 models (Market, Order)
└── utils/
    └── logger.py              # Structured JSON logging
```

## Quick Start

### Prerequisites

- Python 3.9+ (tested on 3.9, 3.10, 3.11, 3.12)
- A Polygon wallet with USDC (for live trading only)

### Installation

```bash
git clone https://github.com/CGFixIT/SafeClaw.git
cd SafeClaw
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
| `size_multiplier` | 0.5 | Copy at 50% of source trade size |
| `max_trade_pct` | 0.02 | Max 2% of bankroll per trade |
| `tp_range_fraction` | 0.40 | Take profit at 40% of remaining upside |
| `sl_range_fraction` | 0.25 | Stop loss at 25% of remaining downside |
| `trailing_stop_fraction` | 0.15 | Trail 15% below peak-to-SL gap |
| `max_market_exposure_pct` | 0.08 | Max 8% of bankroll in any single market |
| `max_trader_allocation` | 0.05 | Max 5% of bankroll copied from any single trader |
| `daily_loss_limit_pct` | 0.03 | Halt all trading (incl. new entries) after 3% daily loss |
| `resolution_blackout_hours` | 24 | Never enter markets resolving within 24h |
| `max_concurrent_positions` | 10 | Maximum open positions at once |
| `max_trade_age_seconds` | 12 | Skip trades older than this at detection (alpha decay / adverse selection) |
| `cooldown_after_losses` | 3 | Pause new entries after this many consecutive losses |
| `cooldown_minutes` | 60 | Length of the post-loss cooldown |
| `fail_closed_on_missing_data` | true | Skip a copy when market metadata or current price can't be verified |

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

1. **Range-relative TP/SL** -- thresholds adapt to token price within [0, 1]
2. **Trailing stop** -- locks in profit as price rises, never drops below hard SL
3. **Time exit** -- closes stale positions after 48h if price barely moved
4. **Per-market exposure cap** -- max 8% of bankroll in any single market
5. **Per-trader allocation cap** -- max 5% of bankroll copied from any single trader
6. **Daily loss circuit breaker** -- halts all trading, including new entries, after 3% daily loss
7. **Resolution blackout** -- never enters or holds positions in markets resolving within 24h
8. **Pre-trade depth check** -- verifies ask-side liquidity before placing BUY orders (live mode)
9. **Per-trader drawdown stop** -- stops copying a trader after cumulative -8% session loss
10. **Cooldown** -- pauses new entries after 3 consecutive losing trades
11. **Staleness gate** -- skips trades older than 12s at detection (alpha has decayed; avoids adverse selection)
12. **Fail-closed gating** -- skips a copy when market metadata or current price can't be verified
13. **Cold-start guard** -- the first poll of each tracked wallet seeds a baseline instead of copying the existing backlog

## Testing

```bash
# Run all 144 tests
pytest -v

# Run only the integration tests (end-to-end monitor -> copier wiring)
pytest tests/test_integration.py -v

# Run only the risk manager tests (range-relative TP/SL math)
pytest tests/test_risk_manager.py -v
```

The test suite includes:
- **Unit tests** for every module (config, models, API clients, risk manager, tracker, portfolio, copier, monitor)
- **Integration tests** that exercise the real `TradeMonitor -> CopyTrader` callback wiring to catch async/await regressions
- All tests run offline with mocked API responses

## Architecture

### Data Flow

```
Polymarket Data API                    Polymarket CLOB WebSocket
       │                                        │
       ▼                                        ▼
  TradeMonitor._poll_loop()         TradeMonitor._ws_loop()
  (detect new trades from            (real-time price feed for
   tracked wallets, 8s interval)      subscribed token positions)
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

### Key APIs Used

| API | Base URL | Auth | Purpose |
|-----|----------|------|---------|
| Data API | `data-api.polymarket.com` | None | Leaderboard, wallet activity |
| Gamma API | `gamma-api.polymarket.com` | None | Market discovery, resolve times |
| CLOB API | `clob.polymarket.com` | L1/L2 | Order placement, midpoint prices |
| CLOB WebSocket | `ws-subscriptions-clob.polymarket.com` | None | Real-time price feeds |

## Disclaimer

This software is provided for educational and research purposes. Trading on prediction markets carries financial risk. Past performance of copied traders does not guarantee future results. Always start with paper mode and small bankrolls. The authors are not responsible for any financial losses incurred through use of this software.

## License

MIT
