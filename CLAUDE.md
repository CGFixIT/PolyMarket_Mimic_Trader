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

Line length is 120 characters (configured in `pyproject.toml`). Tests use `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed). Shared fixtures are in `tests/conftest.py`.

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
| `KellySizer` | `core/sizing.py` | Fractional Kelly sizing (opt-in; only activates when `kelly_enabled=true` AND trader has ≥20 closed trades) |
| `DataClient` | `api/data_client.py` | Polymarket Data API (no auth) — leaderboard, wallet activity; 30 req/60s |
| `GammaClient` | `api/gamma_client.py` | Polymarket Gamma API (no auth) — market metadata; prices fetched from CLOB midpoint endpoint |
| `ClobClient` | `api/clob_client.py` | Polymarket CLOB API (L1/L2 auth) — order placement; simulates fills in paper mode |
| `Config` | `config.py` | Pydantic v2; models: `AppConfig`, `TraderSelectionConfig`, `CopyTradingConfig`, `RiskManagementConfig` |

Configuration lives in `config.yaml` (strategy parameters) and `.env` (secrets). See `.env.example` for required keys.

### Trader scoring formula

`Score = Sharpe_proxy × Consistency × Recency_weight`

- `Sharpe_proxy = mean_pnl_per_trade / stddev_pnl_per_trade`
- `Consistency = win_rate × log(trade_count + 1)`
- `Recency_weight = exp(−λ × days_since_last_trade)` where `λ = ln(2) / half_life_days`

### main.py startup sequence

1. Load config + logger → init `RiskManager`, `PortfolioManager`, `GammaClient`, `ClobClient`
2. Rehydrate market/trader exposure from DB open positions
3. Fetch top traders via `TrackerClient.refresh()`
4. Launch concurrent tasks: `monitor.run()`, `rebalance_loop()`, `exit_check_loop()`, `shutdown_watcher()`
5. SIGINT/SIGTERM → set `shutdown_event` → cancel tasks → print portfolio summary → exit

## Critical design rules

These rules exist because Polymarket tokens are bounded `[0, 1]`—flat percentage TP/SL break at price extremes (e.g. 15% TP from $0.97 = $1.12, impossible).

1. **Always use range-relative TP/SL.** Only `RiskManager._compute_thresholds()` is correct. Never apply flat percentage offsets to entry price. Formula: `TP = entry + (1 − entry) × 0.40`, clamped to `[0, 1]`.
2. **Always call `RiskManager.build_position()` before placing a live order.** Call `release_exposure()` if the order subsequently fails—this rolls back the cap reservation atomically.
3. **Never enter markets resolving in <24 hours.** Enforced in `CopyTrader`.
4. **Never retry a failed order.** A stale market retried = double position.
5. **Paper mode is the default.** Live trading requires `--mode live` flag plus `POLY_PRIVATE_KEY` in `.env`.
6. **All monitor/copier callbacks are `async def` and must be `await`-ed at call sites.** Integration tests in `tests/test_integration.py` specifically guard against un-awaited coroutine regressions.
7. **Cold-start guard in `TradeMonitor`**: the first poll per wallet only seeds the baseline (no copies). Never remove this—it prevents replaying the entire backlog on startup.

## Custom exceptions

- `ExposureCapError` — raised by `build_position()` when per-market or per-trader cap would be breached
- `InvalidPriceError` — raised when a price falls outside `[0.0, 1.0]`
- `InsufficientLiquidityError` — raised by `ClobClient` when depth check fails

## Risk parameters (defaults in `config.yaml`)

- TP threshold: 40% of remaining upside range
- SL threshold: 25% of remaining downside range
- Max bankroll per trade: 2%; per-trader cap: 5%
- Daily loss circuit breaker: 3%
- Per-market exposure cap: 8%
- Time-based exit: 48 hours with <10% range movement trigger
- Resolution blackout: 24 hours before resolve
- Trailing stop: activates after price exceeds entry; `trail_sl = peak − (peak − entry) × 0.15`
- Cooldown: 60-min pause after 3 consecutive losses; any win resets counter
