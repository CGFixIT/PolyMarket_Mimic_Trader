# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
pytest -v

# Run unit tests only (no integration)
pytest -v -m "not integration"

# Run a single test file
pytest tests/test_risk_manager.py -v

# Lint
ruff check .

# Run the bot (paper mode is default)
python -m polymarket_copier.main --mode paper --config config.yaml

# Run in live mode (requires POLY_PRIVATE_KEY in .env)
python -m polymarket_copier.main --mode live --config config.yaml
```

Line length is 120 characters (configured in `pyproject.toml`).

## Architecture

An async copy-trading bot for Polymarket (prediction markets). It identifies successful traders on the leaderboard, watches their wallet activity via REST polling, and mirrors their trades with range-relative risk controls.

**Data flow:**
```
Polymarket Data API → TradeMonitor (8s polling + WebSocket prices)
                              ↓
                        CopyTrader (validates & sizes)
                              ↓
                      RiskManager (computes TP/SL)  +  PortfolioManager (SQLite)
                              ↓
                         ClobClient (order placement)
```

### Key modules

| Module | Path | Role |
|--------|------|------|
| `TrackerClient` | `core/tracker.py` | Scores traders via Sharpe × Consistency × Recency; refreshes every 7 days |
| `TradeMonitor` | `core/monitor.py` | Polls Data API for new wallet trades; feeds prices via WebSocket |
| `CopyTrader` | `core/copier.py` | Validates each trade event (price deviation, staleness, market metadata) and decides whether to copy |
| `RiskManager` | `core/risk_manager.py` | Computes TP/SL, enforces exposure caps, tracks daily losses, applies trailing stops |
| `PortfolioManager` | `core/portfolio.py` | SQLite (WAL mode) for persisting open/closed positions and PnL |
| `DataClient` | `api/data_client.py` | Polymarket Data API (no auth) — leaderboard, wallet activity |
| `GammaClient` | `api/gamma_client.py` | Polymarket Gamma API (no auth) — market metadata, resolution times |
| `ClobClient` | `api/clob_client.py` | Polymarket CLOB API (L1/L2 auth) — order placement; simulates fills in paper mode |
| `Config` | `config.py` | Loads `.env` + `config.yaml` via Pydantic |

Configuration lives in `config.yaml` (strategy parameters) and `.env` (secrets). See `.env.example` for required keys.

## Critical design rules

These rules exist because Polymarket tokens are bounded `[0, 1]`—flat percentage TP/SL break at price extremes (e.g. 15% TP from $0.97 = $1.12, impossible).

1. **Always use range-relative TP/SL.** Only `RiskManager._compute_thresholds()` is correct. Never apply flat percentage offsets to entry price.
2. **Always call `RiskManager.build_position()` before placing a live order.** This enforces the bankroll exposure cap.
3. **Never enter markets resolving in <24 hours.** Enforced in `CopyTrader`.
4. **Never retry a failed order.** A stale market retried = double position.
5. **Paper mode is the default.** Live trading requires `--mode live` flag plus `POLY_PRIVATE_KEY` in `.env`.

## Risk parameters (defaults in `config.yaml`)

- TP threshold: 40% of remaining upside range
- SL threshold: 25% of remaining downside range
- Max bankroll per trade: 2%; per-trader cap: 5%
- Daily loss circuit breaker: 3%
- Per-market exposure cap: 8%
- Time-based exit: 48 hours
- Resolution blackout: 24 hours before resolve
