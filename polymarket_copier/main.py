"""Main entrypoint for the Polymarket copy trading bot v2."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from typing import Optional

from polymarket_copier.api.clob_client import ClobClient
from polymarket_copier.api.gamma_client import GammaClient
from polymarket_copier.config import ConfigError, load_config
from polymarket_copier.core import metrics
from polymarket_copier.core.copier import CopyTrader
from polymarket_copier.core.monitor import TradeMonitor
from polymarket_copier.core.portfolio import PortfolioManager
from polymarket_copier.core.risk_manager import RiskConfig, RiskManager
from polymarket_copier.core.tracker import TrackerClient, TrackerConfig
from polymarket_copier.utils.logger import setup_logger


def _update_tracker_metrics(tracker: TrackerClient) -> None:
    """Refresh the per-tracker Prometheus gauges after a leaderboard refresh."""
    metrics.TRACKED_TRADERS.set(len(tracker.top_traders))
    metrics.LAST_TRACKER_REFRESH.set(tracker.last_refresh())
    for t in tracker.top_traders:
        metrics.TRADER_SCORE.labels(trader_address=t.stats.address, rank=str(t.rank)).set(t.score)


async def run_bot(config_path: Optional[str] = None, mode: Optional[str] = None) -> None:
    config = load_config(config_path=config_path)
    if mode:
        config.mode = mode

    logger = setup_logger(level=config.logging.level, log_file=config.logging.file)

    logger.info("Polymarket Copy Trading Bot v2")
    logger.info(
        "Mode: %s | Bankroll: $%.2f | Max traders: %d",
        config.mode.upper(),
        config.bankroll,
        config.max_tracked_traders,
    )

    # M16: optionally expose Prometheus metrics. No-op if prometheus_client is not
    # installed (start_metrics_server logs a warning and returns False).
    if config.metrics_enabled:
        metrics.start_metrics_server(config.metrics_port)

    risk_cfg = RiskConfig(
        tp_range_fraction=config.risk_management.tp_range_fraction,
        sl_range_fraction=config.risk_management.sl_range_fraction,
        low_entry_threshold=config.risk_management.low_entry_threshold,
        low_entry_tp_fraction=config.risk_management.low_entry_tp_fraction,
        min_tp_abs=config.risk_management.min_tp_abs,
        min_sl_abs=config.risk_management.min_sl_abs,
        min_reward_risk=config.risk_management.min_reward_risk,
        trailing_stop_fraction=config.risk_management.trailing_stop_fraction,
        time_exit_hours=config.risk_management.time_exit_hours,
        time_exit_min_range_move=config.risk_management.time_exit_min_range_move,
        daily_loss_limit_pct=config.risk_management.daily_loss_limit_pct,
        max_market_exposure_pct=config.risk_management.max_market_exposure_pct,
        max_trader_allocation=config.copy_trading.max_trader_allocation,
        cooldown_after_losses=config.risk_management.cooldown_after_losses,
        cooldown_minutes=config.risk_management.cooldown_minutes,
        resolution_blackout_hours=config.risk_management.resolution_blackout_hours,
        max_total_exposure_pct=config.risk_management.max_total_exposure_pct,
    )
    risk_manager = RiskManager(config=risk_cfg, bankroll=config.bankroll)

    portfolio = PortfolioManager(db_path="data/positions.db")
    await portfolio.init()

    # Restore open positions to risk_manager exposure tracking on restart.
    # rehydrate_exposure() registers the exposure and warns (rather than
    # silently carrying) if a since-lowered cap is now breached.
    for pos in await portfolio.get_open_positions():
        risk_manager.rehydrate_exposure(
            market_id=pos.market_id,
            trader_address=pos.trader_address,
            value=pos.entry_price * pos.size_shares,
        )

    gamma_client = GammaClient()
    clob_client = ClobClient(config)

    tracker_cfg = TrackerConfig(
        min_total_pnl=config.trader_selection.min_pnl,
        min_win_rate=config.trader_selection.min_win_rate,
        min_trade_count=config.trader_selection.min_trades,
        min_expectancy=config.trader_selection.min_expectancy,
        half_life_days=config.trader_selection.half_life_days,
        max_top_traders=config.trader_selection.max_top_traders,
        sharpe_cap=config.trader_selection.sharpe_cap,
        sharpe_shrink_min_trades=config.trader_selection.sharpe_shrink_min_trades,
        rebalance_interval_days=config.trader_selection.rebalance_days,
        recent_window_days=config.trader_selection.recent_window_days,
    )
    tracker = TrackerClient(config=tracker_cfg)
    top_traders = await tracker.refresh()
    _update_tracker_metrics(tracker)

    if not top_traders:
        logger.error("No suitable traders found. Check trader_selection thresholds.")
        await portfolio.close()
        await gamma_client.close()
        return

    wallets = tracker.top_wallet_addresses()
    logger.info("Tracking %d wallets", len(wallets))

    copier = CopyTrader(risk_manager, portfolio, clob_client, gamma_client, config)
    copier.update_tracker_win_rates({t.stats.address: t.stats.win_rate for t in top_traders})
    # H18: feed the demonstrated-edge signal (mean per-trade ROI) for Kelly seeding.
    copier.update_tracker_mean_pnl({t.stats.address: t.stats.mean_pnl for t in top_traders})

    monitor = TradeMonitor(
        tracked_wallets=wallets,
        on_trade=copier.handle_trade_event,
        on_price=copier.handle_price_tick,
        poll_interval=config.polling_interval_seconds,
        ws_max_backoff=config.risk_management.ws_max_backoff_seconds,
        poll_jitter=config.poll_jitter_seconds,
    )
    copier.monitor = monitor

    shutdown_event = asyncio.Event()

    def handle_signal() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    async def supervise(name: str, coro_factory, max_restarts: int = 10) -> None:
        """Restart a crashed loop with exponential backoff. Gives up after max_restarts."""
        delay = 1.0
        restarts = 0
        while not shutdown_event.is_set() and restarts < max_restarts:
            try:
                await coro_factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if shutdown_event.is_set():
                    return
                restarts += 1
                logger.error("Loop %s crashed (restart %d/%d): %s", name, restarts, max_restarts, exc)
                if restarts >= max_restarts:
                    logger.critical("Loop %s exceeded max restarts — shutting down", name)
                    shutdown_event.set()
                    return
                await asyncio.sleep(min(delay, 60.0))
                delay *= 2

    async def heartbeat_watchdog() -> None:
        """Alarm if the poll loop stalls for >detection_stall_alert_seconds."""
        stall_limit = config.detection_stall_alert_seconds or config.polling_interval_seconds * 3
        while not shutdown_event.is_set():
            await asyncio.sleep(stall_limit)
            last = monitor.last_poll_completed_at
            if last is not None and (time.time() - last) > stall_limit:
                logger.error(
                    "WATCHDOG: poll loop stalled for %.0fs (limit %.0fs)",
                    time.time() - last,
                    stall_limit,
                )
            elif last is None:
                logger.warning("WATCHDOG: monitor has not completed its first poll yet")

    async def rebalance_loop() -> None:
        while not shutdown_event.is_set():
            await asyncio.sleep(3600)
            if tracker.needs_rebalance:
                new_traders = await tracker.refresh()
                if new_traders:
                    monitor.set_wallets([t.stats.address for t in new_traders])
                    copier.update_tracker_win_rates({t.stats.address: t.stats.win_rate for t in new_traders})
                    copier.update_tracker_mean_pnl({t.stats.address: t.stats.mean_pnl for t in new_traders})
                    _update_tracker_metrics(tracker)
                    logger.info("Rebalanced: now tracking %d wallets", len(monitor._wallets))
            demoted = await copier.check_trader_demotion()
            if demoted:
                remaining = [w for w in monitor._wallets if w not in set(demoted)]
                monitor.set_wallets(remaining)
                logger.info("Demoted %d traders, now tracking %d", len(demoted), len(remaining))

    async def metrics_loop() -> None:
        # M16: refresh the periodically-sampled gauges. Counters/histograms are
        # incremented inline at their event sites; only the point-in-time gauges
        # need this collector. Cheap no-ops when prometheus_client is absent, so
        # the loop runs harmlessly regardless of whether metrics are enabled.
        interval = config.metrics_refresh_seconds
        while not shutdown_event.is_set():
            unrealized = await portfolio.get_open_unrealized_pnl_conservative()
            metrics.BANKROLL.set(risk_manager.bankroll)
            metrics.DAILY_PNL.set(risk_manager.daily_pnl())
            metrics.OPEN_POSITIONS.set(await portfolio.position_count())
            metrics.OPEN_UNREALIZED_PNL.set(unrealized)
            metrics.TOTAL_EXPOSURE.set(risk_manager.total_exposure())
            metrics.CONSECUTIVE_LOSSES.set(risk_manager.consecutive_losses())
            metrics.COOLDOWN_SECONDS_REMAINING.set(risk_manager.cooldown_remaining())
            metrics.TRADING_HALTED.set(1 if risk_manager.is_trading_halted(unrealized_pnl=unrealized) else 0)
            await asyncio.sleep(interval)

    async def exit_check_loop() -> None:
        # H10: tighten exit poll cadence when WS is down so TP/SL latency stays low
        # even without real-time price ticks.
        fast_interval = config.risk_management.exit_poll_fast_seconds
        while not shutdown_event.is_set():
            await copier.check_all_exits()
            interval = fast_interval if not monitor.ws_healthy else config.polling_interval_seconds
            await asyncio.sleep(interval)

    async def shutdown_watcher() -> None:
        await shutdown_event.wait()
        await monitor.stop()

    logger.info("Starting bot...")
    try:
        await asyncio.gather(
            supervise("monitor", lambda: monitor.run()),
            supervise("rebalance", rebalance_loop),
            supervise("exit_check", exit_check_loop),
            supervise("metrics", metrics_loop),
            heartbeat_watchdog(),
            shutdown_watcher(),
        )
    except asyncio.CancelledError:
        pass
    finally:
        await monitor.stop()
        summary = await portfolio.summary()
        logger.info("\n%s", summary)
        await portfolio.close()
        await gamma_client.close()
        logger.info("Bot shut down cleanly")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polymarket Copy Trading Bot v2")
    parser.add_argument("--config", "-c", help="Path to config.yaml", default=None)
    parser.add_argument("--mode", "-m", choices=["paper", "live"], default=None)
    args = parser.parse_args()
    try:
        asyncio.run(run_bot(config_path=args.config, mode=args.mode))
    except ConfigError as e:
        # Invalid configuration — exit cleanly with a clear message instead of
        # dumping a traceback from deep inside load_config.
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
