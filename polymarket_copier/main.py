"""Main entrypoint for the Polymarket copy trading bot."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys

from polymarket_copier.api.clob_client import ClobClient
from polymarket_copier.api.data_client import DataClient
from polymarket_copier.api.gamma_client import GammaClient
from polymarket_copier.config import load_config
from polymarket_copier.core.copier import CopyTrader
from polymarket_copier.core.monitor import TradeMonitor
from polymarket_copier.core.portfolio import Portfolio
from polymarket_copier.core.risk_manager import RiskManager
from polymarket_copier.core.tracker import TraderTracker
from polymarket_copier.utils.logger import setup_logger


async def run_bot(config_path: str | None = None, mode: str | None = None) -> None:
    """Main async entry point — discovers traders, monitors, and copies."""
    config = load_config(config_path=config_path)
    if mode:
        config.mode = mode

    logger = setup_logger(
        level=config.logging.level,
        log_file=config.logging.file,
    )

    logger.info("=" * 60)
    logger.info("Polymarket Copy Trading Bot")
    logger.info("Mode: %s", config.mode.upper())
    logger.info("Bankroll: $%.2f", config.bankroll)
    logger.info("Max traders: %d", config.max_tracked_traders)
    logger.info("Poll interval: %ds", config.polling_interval_seconds)
    logger.info("=" * 60)

    if config.mode == "live" and not config.private_key:
        logger.error("POLY_PRIVATE_KEY is required for live trading. Set it in .env")
        sys.exit(1)

    # Initialize clients
    data_client = DataClient()
    gamma_client = GammaClient()
    clob_client = ClobClient(config)

    # Discover top traders
    tracker = TraderTracker(data_client, config.trader_selection, config.max_tracked_traders)
    traders = await tracker.discover()

    if not traders:
        logger.error("No suitable traders found. Check your trader_selection thresholds in config.yaml")
        await data_client.close()
        await gamma_client.close()
        return

    addresses = [t.address for t in traders]

    # Initialize core components
    risk_manager = RiskManager(config.risk_management, config.copy_trading, config.bankroll)
    portfolio = Portfolio()
    portfolio.load()

    copy_trader = CopyTrader(clob_client, gamma_client, risk_manager, portfolio)

    # Set up monitor
    monitor = TradeMonitor(
        data_client=data_client,
        trader_addresses=addresses,
        poll_interval=config.polling_interval_seconds,
        on_trade=copy_trader.handle_trade,
    )

    # Handle shutdown
    shutdown_event = asyncio.Event()

    def handle_signal() -> None:
        logger.info("Shutdown signal received")
        monitor.stop()
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    # Run monitor and exit checker concurrently
    async def exit_check_loop() -> None:
        while not shutdown_event.is_set():
            await copy_trader.check_exits()
            await asyncio.sleep(config.polling_interval_seconds)

    logger.info("Starting copy trading bot...")
    try:
        await asyncio.gather(
            monitor.run(),
            exit_check_loop(),
        )
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("\n" + portfolio.summary())
        portfolio.save()
        await data_client.close()
        await gamma_client.close()
        logger.info("Bot shut down cleanly")


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Polymarket Copy Trading Bot")
    parser.add_argument(
        "--config", "-c",
        help="Path to config.yaml",
        default=None,
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["paper", "live"],
        help="Trading mode (overrides config.yaml)",
        default=None,
    )
    args = parser.parse_args()

    asyncio.run(run_bot(config_path=args.config, mode=args.mode))


if __name__ == "__main__":
    main()
