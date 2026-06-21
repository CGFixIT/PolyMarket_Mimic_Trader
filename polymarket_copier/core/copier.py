"""Copy trade engine v2 — range-relative risk management + resolution blackout."""

from __future__ import annotations

import logging
import time
import uuid

from polymarket_copier.api.clob_client import ClobClient, InsufficientLiquidityError
from polymarket_copier.api.gamma_client import GammaClient
from polymarket_copier.config import AppConfig
from polymarket_copier.core.monitor import PriceTick, TradeEvent, TradeType
from polymarket_copier.core.portfolio import PortfolioManager
from polymarket_copier.core.risk_manager import ExitReason, ExposureCapError, RiskManager
from polymarket_copier.models.types import Order

logger = logging.getLogger("polymarket_copier")

_RESOLUTION_BLACKOUT_HOURS = 24.0


class CopyTrader:
    """Copies trades from tracked wallets with conservative risk parameters."""

    def __init__(
        self,
        risk_manager: RiskManager,
        portfolio: PortfolioManager,
        clob_client: ClobClient,
        gamma_client: GammaClient,
        config: AppConfig,
        monitor=None,
    ):
        self.risk = risk_manager
        self.portfolio = portfolio
        self.clob = clob_client
        self.gamma = gamma_client
        self.config = config
        self.monitor = monitor

    async def handle_trade_event(self, event: TradeEvent) -> None:
        """Called by TradeMonitor on every new detected trade."""
        logger.info(
            "Trade event from %s: %s $%.2f @ %.4f on %s",
            event.wallet_address[:10], event.trade_type.value,
            event.size_usdc, event.price, event.market_id[:10],
        )

        if self.config.mode == "paper":
            logger.info("[PAPER] Processing trade event %s", event.event_id)

        # 2. Only copy entries, not exits.
        if event.trade_type != TradeType.BUY:
            logger.debug("Skipping non-BUY trade")
            return

        # 2a. Portfolio circuit breakers (daily-loss limit, post-loss cooldown).
        #     Checked on the ENTRY path so they cannot be bypassed by opening new
        #     positions — evaluate() only governs exits of already-open positions.
        halt_reason = self.risk.is_trading_halted()
        if halt_reason:
            logger.warning("Skip: trading halted — %s", halt_reason)
            return

        # 2b. Staleness gate. After this many seconds the source's alpha has
        #     decayed and we'd only be buying into their price impact (adverse
        #     selection). 0 disables the gate.
        max_age = self.config.copy_trading.max_trade_age_seconds
        if max_age > 0:
            age = time.time() - event.timestamp
            if age > max_age:
                logger.info("Skip: trade is %.1fs old > max %.1fs", age, max_age)
                return

        # 3. Resolution blackout. Fail CLOSED if market metadata is unavailable —
        #    without it we cannot verify resolve time or volume.
        market = await self.gamma.get_market(event.market_id)
        if market is None and self.config.risk_management.fail_closed_on_missing_data:
            logger.info("Skip: market data unavailable for %s (fail-closed)",
                        event.market_id[:10])
            return
        if market and market.resolve_time:
            hours_to_resolve = (market.resolve_time.timestamp() - time.time()) / 3600
            if 0 < hours_to_resolve < _RESOLUTION_BLACKOUT_HOURS:
                logger.info("Skip: market resolves in %.1fh (blackout)", hours_to_resolve)
                return

        # 4. Price deviation check. Fail CLOSED if the current price is unknown —
        #    falling back to the (stale) event price makes the deviation check
        #    trivially pass and trades on data we couldn't verify.
        current_price = await self.gamma.get_market_price(event.token_id)
        if current_price is None:
            if self.config.risk_management.fail_closed_on_missing_data:
                logger.info("Skip: current price unavailable for token %s (fail-closed)",
                            event.token_id[:10])
                return
            current_price = event.price

        if event.price > 0:
            deviation = abs(current_price - event.price) / event.price
            if deviation > self.config.copy_trading.max_price_deviation:
                logger.info(
                    "Skip: price deviation %.1f%% > max %.1f%%",
                    deviation * 100, self.config.copy_trading.max_price_deviation * 100,
                )
                return

        # 5. Market volume check.
        if market and market.volume_24h < self.config.copy_trading.min_market_volume:
            logger.info(
                "Skip: 24h volume $%.0f < min $%.0f",
                market.volume_24h, self.config.copy_trading.min_market_volume,
            )
            return

        # 6. Compute conservative copy size.
        copy_size_usdc = min(
            event.size_usdc * self.config.copy_trading.size_multiplier,
            self.risk.bankroll * self.config.copy_trading.max_trade_pct,
        )

        if copy_size_usdc <= 0 or current_price <= 0:
            return

        size_shares = copy_size_usdc / current_price

        resolve_ts = None
        if market and market.resolve_time:
            resolve_ts = market.resolve_time.timestamp()

        # 7. Cheap gating checks FIRST — before registering exposure, so that a
        #    rejection here can never leak phantom exposure into the RiskManager.
        count = await self.portfolio.position_count()
        if count >= self.config.copy_trading.max_concurrent_positions:
            logger.info("Skip: max positions (%d) reached", count)
            return

        # 8. Per-trader drawdown stop.
        trader_pnl = await self.portfolio.get_trader_pnl(event.wallet_address)
        if trader_pnl <= -(self.risk.bankroll * self.config.risk_management.drawdown_stop_pct):
            logger.info(
                "Skip: trader %s drawdown stop (pnl=$%.2f)",
                event.wallet_address[:10], trader_pnl,
            )
            return

        # 9. build_position registers market exposure and enforces the exposure cap.
        try:
            pos = self.risk.build_position(
                position_id=str(uuid.uuid4()),
                market_id=event.market_id,
                token_id=event.token_id,
                trader_address=event.wallet_address,
                entry_price=current_price,
                size_shares=size_shares,
                resolve_time=resolve_ts,
            )
        except ExposureCapError as e:
            logger.info("Skip: exposure cap — %s", e)
            return

        order = Order(
            market_id=event.market_id,
            token_id=event.token_id,
            side="BUY",
            price=current_price,
            size_usdc=copy_size_usdc,
        )

        # 10. Place order. On ANY failure, release the exposure build_position
        #     registered so a never-opened position cannot leak the exposure cap.
        try:
            await self.clob.place_order(order)
        except InsufficientLiquidityError as e:
            logger.info("Skip: insufficient liquidity — %s", e)
            self.risk.release_exposure(
                pos.market_id, pos.entry_price * pos.size_shares, pos.trader_address
            )
            return
        except Exception as e:
            logger.error("Order placement failed: %s", e)
            self.risk.release_exposure(
                pos.market_id, pos.entry_price * pos.size_shares, pos.trader_address
            )
            return

        await self.portfolio.open_position(pos)

        if self.monitor:
            self.monitor.subscribe_token(event.token_id)

        logger.info(
            "Copied: %s $%.2f @ %.4f | TP=%.4f SL=%.4f | from %s",
            event.trade_type.value, copy_size_usdc, current_price,
            pos.tp_price, pos.sl_price, event.wallet_address[:10],
        )

    async def handle_price_tick(self, tick: PriceTick) -> None:
        """Called by the monitor's on_price callback for each real-time price update."""
        pos = await self.portfolio.get_position_by_token(tick.token_id)
        if pos is None:
            return

        # evaluate() raises pos.peak_price in-memory when the price makes a new high.
        # Capture the prior peak BEFORE the call so we can detect and persist that
        # new high — comparing tick.price against the just-mutated pos.peak_price
        # would always be False and the DB peak would stay pinned at entry.
        prev_peak = pos.peak_price
        reason = self.risk.evaluate(pos, tick.price)

        if pos.peak_price > prev_peak:
            await self.portfolio.update_peak_price(pos.position_id, pos.peak_price)

        if reason != ExitReason.HOLD:
            await self._exit_position(pos, tick.price, reason)

    async def _exit_position(self, pos, price: float, reason: ExitReason) -> None:
        exit_order = Order(
            market_id=pos.market_id,
            token_id=pos.token_id,
            side="SELL",
            price=price,
            size_usdc=max(price * pos.size_shares, _EPSILON_USDC),
        )

        try:
            await self.clob.place_order(exit_order)
        except Exception as e:
            logger.error("Exit order failed for %s: %s", pos.position_id, e)
            return

        pnl = await self.portfolio.close_position(pos.position_id, price, reason)
        self.risk.record_exit(pos, price)

        if self.monitor:
            self.monitor.unsubscribe_token(pos.token_id)

        logger.info(
            "Exited [%s]: %s pnl=$%.4f @ %.4f",
            reason.name, pos.position_id, pnl, price,
        )

    async def check_all_exits(self) -> None:
        """Poll-based exit check fallback (when WS price feed is unavailable)."""
        positions = await self.portfolio.get_open_positions()
        for pos in positions:
            price = await self.gamma.get_market_price(pos.token_id)
            if price is None:
                continue
            reason = self.risk.evaluate(pos, price)
            if reason != ExitReason.HOLD:
                await self._exit_position(pos, price, reason)


# Smallest non-zero order size to keep the Order model's gt=0 validation happy
# when a position would otherwise round to zero notional.
_EPSILON_USDC = 1e-6
