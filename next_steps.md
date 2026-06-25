# Next Steps & Future Opportunities

The original IMPROVEMENT_PLAN_V2 backlog (47 items across Tiers 0–3) is **100% complete** as of June 2026. This document captures post-plan opportunities, operational readiness checklist, and known low-level findings from a June 2026 code audit.

---

## Operational Readiness Checklist

Before switching from paper to live mode, verify:

- [ ] Paper mode PnL is consistently positive over ≥30 days
- [ ] `POLY_PRIVATE_KEY` set in `.env` with a wallet funded with USDC
- [ ] `BANKROLL` set to a small test amount first (e.g., $50–$100)
- [ ] Prometheus metrics endpoint live and scraped (optional but recommended)
- [ ] Log output reviewed for any WARNING/ERROR patterns during paper run
- [ ] `max_trade_pct`, `max_market_exposure_pct`, and `daily_loss_limit_pct` reviewed against your risk tolerance
- [ ] Run `python -m polymarket_copier.main --mode live` in a screen/tmux so it survives SSH disconnect
- [ ] Set up alerting on daily loss circuit breaker events (look for `circuit_breaker_tripped` in structured logs)

---

## Future Improvement Ideas

### Tier A — Highest Value

| ID | Title | Description |
|----|-------|-------------|
| F1 | **Multi-exchange price oracle** | Currently prices come from CLOB midpoint only. Cross-reference with Manifold or Metaculus to detect arbitrage or stale prices. |
| F2 | **Dynamic trader pool sizing** | Currently tracks a fixed `max_top_traders`. Scale pool size up/down based on available bankroll and market count. |
| F3 | **Graduated position sizing** | Add conviction-scaled sizing: highest-scored traders get up to 3× the base Kelly size. |
| F4 | **Position correlation guard** | Before opening, check if token is correlated with existing open positions (same underlying event). Cap total exposure to correlated cluster. |
| F5 | **Live PnL dashboard** | Build a simple terminal UI (Rich/Textual) or web dashboard consuming Prometheus metrics for real-time monitoring. |

### Tier B — Medium Value

| ID | Title | Description |
|----|-------|-------------|
| F6 | **Backtesting harness** | Replay historical leaderboard + activity data to backtest parameter changes offline. |
| F7 | **Multi-wallet parallel tracking** | Currently sequential wallet polling. Run wallet polls as true concurrent tasks instead of sequential within the gather. |
| F8 | **Telegram / Discord alerts** | Push `position_opened`, `position_closed`, `circuit_breaker_tripped` events to a messaging channel. |
| F9 | **Config hot-reload** | Watch `config.yaml` for changes and apply non-critical params (TP/SL fractions, cooldown) without restart. |
| F10 | **Slippage model calibration** | Fit `paper_taker_fee_pct` and `paper_fill_slippage_pct` to real fill data once live trading begins. |

### Tier C — Polish / Nice-to-Have

| ID | Title | Description |
|----|-------|-------------|
| F11 | **Per-market outcome tracking** | Track win/loss by market category (politics, sports, crypto) to identify where copied traders have edge. |
| F12 | **REST API server mode** | Expose a lightweight FastAPI endpoint for portfolio state, manual exits, and config overrides. |
| F13 | **Docker packaging** | Dockerfile + compose for reproducible deploys. |
| F14 | **Grafana dashboard template** | Pre-built dashboard JSON for the Prometheus metrics exported by `metrics.py`. |

---

## Code Audit Findings (June 2026)

Low-severity issues identified in a June 2026 audit. None are blockers but worth addressing in future PRs.

| # | Severity | File | Finding |
|---|----------|------|---------|
| A1 | LOW | `monitor.py:534` | `_seen_trade_ids` eviction cap at 100 per wallet. Fine for current usage; revisit if wallets trade at very high frequency. |
| A2 | LOW | `clob_client.py:78` | Thread pool size hardcoded at `max_workers=2`. Could be a bottleneck under very high order throughput. Consider moving to config. |
| A3 | LOW | `monitor.py:227,450` | `asyncio.gather(return_exceptions=True)` logs failures at WARNING. A persistent per-wallet bug silently disables that wallet. Consider ERROR-level + per-wallet circuit breaker. |
| A4 | LOW | `clob_client.py:393` | `get_order_status()` poll loop has no hard `asyncio.wait_for()` cap beyond M12 deadline. Add explicit timeout guard for defensive depth. |

---

## Completed Work Reference

All 47 IMPROVEMENT_PLAN_V2 items are merged. Summary by PR:

| PR | Items | Description |
|----|-------|-------------|
| #33 | C1–C5 | Critical live-trading correctness fixes |
| #34 | H1, H2, H6, H7 | Trailing stop, R:R floor, directional deviation, entry-price band |
| #35 | H3, H4, H8, H9, H13 | Unrealized PnL, total cap, token validation, supervisor, demotion |
| #36 | H5, H10 | Fee-aware entries, WS reconnect resilience |
| #37 | H14–H16 | Trader selection quality (scoring, dual-window, expectancy) |
| #38 | M8, M9, L5 | Risk refinements (UTC midnight reset, per-token cap, low-entry TP) |
| #39 | H17, M1, M5 | Execution quality (jitter, edge revalidation, FOK/FAK) |
| #40 | M16, M17, L1–L3 | Observability (Prometheus, structured logs, dead code, docstrings) |
| #41 | H18, M3, M4, M11, M12 | Kelly sizing refinement |
| #42 | L4, L7 + chaos | Integration & stress tests |
| #43 | — | Break-even close fix |
| #44 | — | CI/GitHub Actions workflow |
| #45 | H11, H12 | In-memory position cache, entry lock shrink |
| #46 | L5, M8, M9, M12 | Quick-win risk refinements |
| #47 | L2, L3, L4 | Infrastructure: keep-alive, eager creds, recency tuning |
| #64 | — | Shared aiohttp session pool |
| #65 | — | Per-trader activity stats cache |
| #66 | — | Wallet address normalization |
| #67 | — | Startup double DB fetch fix, HTTP error reason logging, exit-lock tests |
