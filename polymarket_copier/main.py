"""Main entrypoint for the Polymarket copy trading bot v2."""

from __future__ import annotations

import argparse
import asyncio
import signal
from typing import Optional

from polymarket_copier.api.clob_client import ClobClient
from polymarket_copier.api.gamma_client import GammaClient
from polymarket_copier.config import load_config
from polymarket_copier.core.copier import CopyTrader
from polymarket_copier.core.monitor import TradeMonitor
from polymarket_copier.core.portfolio import PortfolioManager
from polymarket_copier.core.risk_manager import RiskConfig, RiskManager
from polymarket_copier.core.tracker import TrackerClient, TrackerConfig
from polymarket_copier.utils.logger import setup_logger


async def run_bot(config_path: Optional[str] = None, mode: Optional[str] = None) -> None:
    config = load_config(config_path=config_path)
    if mode:
        config.mode = mode

    logger = setup_logger(level=config.logging.level, log_file=config.logging.file)

    logger.info("Polymarket Copy Trading Bot v2")
    logger.info(
        "Mode: %s | Bankroll: $%.2f | Max traders: %d",
        config.mode.upper(), config.bankroll, config.max_tracked_traders,
    )

    risk_cfg = RiskConfig(
        tp_range_fraction=config.risk_management.tp_range_fraction,
        sl_range_fraction=config.risk_management.sl_range_fraction,
        min_tp_abs=config.risk_management.min_tp_abs,
        min_sl_abs=config.risk_management.min_sl_abs,
        trailing_stop_fraction=config.risk_management.trailing_stop_fraction,
        time_exit_hours=config.risk_management.time_exit_hours,
        time_exit_min_range_move=config.risk_management.time_exit_min_range_move,
        daily_loss_limit_pct=config.risk_management.daily_loss_limit_pct,
        max_market_exposure_pct=config.risk_management.max_market_exposure_pct,
        resolution_blackout_hours=config.risk_management.resolution_blackout_hours,
    )
    risk_manager = RiskManager(config=risk_cfg, bankroll=config.bankroll)

    portfolio = PortfolioManager(db_path="data/positions.db")
    await portfolio.init()

    # Restore open positions to risk_manager exposure tracking on restart
    for pos in await portfolio.get_open_positions():
        risk_manager._market_exposure[pos.market_id] = (
            risk_manager._market_exposure.get(pos.market_id, 0.0)
            + pos.entry_price * pos.size_shares
        )

    gamma_client = GammaClient()
    clob_client = ClobClient(config)

    tracker_cfg = TrackerConfig(
        min_total_pnl=config.trader_selection.min_pnl,
        min_win_rate=config.trader_selection.min_win_rate,
        min_trade_count=config.trader_selection.min_trades,
        half_life_days=config.trader_selection.half_life_days,
        max_top_traders=config.trader_selection.max_top_traders,
        rebalance_interval_days=config.trader_selection.rebalance_days,
    )
    tracker = TrackerClient(config=tracker_cfg)
    top_traders = await tracker.refresh()

    if not top_traders:
        logger.error("No suitable traders found. Check trader_selection thresholds.")
        await portfolio.close()
        await gamma_client.close()
        return

    wallets = tracker.top_wallet_addresses()
    logger.info("Tracking %d wallets", len(wallets))

    copier = CopyTrader(risk_manager, portfolio, clob_client, gamma_client, config)

    monitor = TradeMonitor(
        tracked_wallets=wallets,
        on_trade=copier.handle_trade_event,
        on_price=copier.handle_price_tick,
        poll_interval=config.polling_interval_seconds,
    )
    copier.monitor = monitor

    shutdown_event = asyncio.Event()

    def handle_signal() -> None:
        logger.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    async def rebalance_loop() -> None:
        while not shutdown_event.is_set():
            await asyncio.sleep(3600)
            if tracker.needs_rebalance:
                new_traders = await tracker.refresh()
                if new_traders:
                    monitor._wallets = [t.stats.address for t in new_traders]
                    logger.info("Rebalanced: now tracking %d wallets", len(monitor._wallets))

    async def exit_check_loop() -> None:
        while not shutdown_event.is_set():
            await copier.check_all_exits()
            await asyncio.sleep(config.polling_interval_seconds)

    async def shutdown_watcher() -> None:
        await shutdown_event.wait()
        await monitor.stop()

    logger.info("Starting bot...")
    try:
        await asyncio.gather(
            monitor.run(),
            rebalance_loop(),
            exit_check_loop(),
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
    asyncio.run(run_bot(config_path=args.config, mode=args.mode))


if __name__ == "__main__":
    main()
