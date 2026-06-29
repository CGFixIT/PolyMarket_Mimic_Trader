"""Copy trade engine v2 — range-relative risk management + resolution blackout."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from polymarket_copier.api.clob_client import ClobClient, InsufficientLiquidityError
from polymarket_copier.api.gamma_client import GammaClient
from polymarket_copier.config import AppConfig
from polymarket_copier.core import metrics
from polymarket_copier.core.monitor import PriceTick, TradeEvent, TradeType
from polymarket_copier.core.portfolio import PortfolioManager
from polymarket_copier.core.risk_manager import ExitReason, ExposureCapError, Position, RiskManager
from polymarket_copier.core.sizing import kelly_size_from_edge, kelly_size_usdc, roi_to_edge
from polymarket_copier.models.types import Order
from polymarket_copier.utils.addresses import normalize_address
from polymarket_copier.utils.logger import log_event

logger = logging.getLogger("polymarket_copier")


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
        # Serialises the critical section from position-count check through
        # open_position() write. Without this, concurrent wallet polls via
        # asyncio.gather can both read count=N < max before either writes,
        # opening one extra position and breaching the cap (TOCTOU race).
        self._entry_lock = asyncio.Lock()
        # Tracker-observed win rates per wallet address, refreshed by main.py after
        # each TrackerClient.refresh(). Used as a Kelly prior when our own closed-
        # trade sample is too small to trust (kelly_seed_from_tracker=True).
        self._tracker_win_rates: dict[str, float] = {}
        # H18: tracker mean per-trade ROI per wallet — the demonstrated-edge signal
        # the Kelly seed path sizes from (instead of raw win rate).
        self._tracker_mean_pnl: dict[str, float] = {}
        # M4: wall-clock time of the last tracker update, for decaying a stale prior.
        self._tracker_updated_at: float = 0.0
        # C4: per-position asyncio locks prevent a double-exit race between two
        # concurrent triggers (WS tick vs poll sweep vs SOURCE_EXIT).  The DB
        # `AND status='open'` guard in close_position() is the belt; this is the
        # suspenders — it stops us placing two live SELL orders for one position.
        self._exit_locks: dict[str, asyncio.Lock] = {}
        # H13: tracks demoted traders so Kelly sizing stops using them.
        self._demoted_traders: set[str] = set()
        # H11: in-memory position cache keyed by token_id.  Avoids per-tick SQLite
        # reads on the TP/SL evaluation hot path.  Rehydrated at startup via
        # rehydrate_position_cache(); mutated on open/close.
        self._pos_cache: dict[str, list[Position]] = {}
        # Pending peak_price updates — written to in-memory Position objects
        # immediately on each WS tick; flushed to SQLite in a debounced batch so
        # the hot path writes zero bytes to disk per tick (H11).
        self._peak_dirty: dict[str, float] = {}
        self._last_peak_flush: float = 0.0
        # How often (seconds) to flush dirty peak prices to the DB.  At close the
        # final peak is always persisted regardless of this interval.
        self._peak_persist_interval: float = 30.0
        # H12: count of entries that have reserved exposure (build_position succeeded)
        # but whose position row is not yet committed to the DB (open_position pending).
        # Added to position_count() inside the entry lock so the TOCTOU cap holds even
        # while the lock is not held during the order placement I/O.
        self._pending_entries: int = 0

    def update_tracker_win_rates(self, rates: dict[str, float]) -> None:
        """Replace the current tracker-win-rate prior with freshly scored data.

        Called by main.py after each TrackerClient.refresh() so Kelly sizing
        always uses the most recent leaderboard win rates as a prior during
        the warm-up period before the bot's own sample is large enough.
        """
        self._tracker_win_rates = {normalize_address(k): v for k, v in rates.items()}
        self._tracker_updated_at = time.time()  # M4: stamp for prior-decay

    def update_tracker_mean_pnl(self, rois: dict[str, float]) -> None:
        """Replace the tracker mean-per-trade-ROI map (H18 demonstrated-edge signal).

        Called by main.py alongside update_tracker_win_rates after each
        TrackerClient.refresh(). The edge-based Kelly seed path derives a probability
        edge from these ROIs rather than from the favorite-buyer-biased win rate.
        """
        self._tracker_mean_pnl = {normalize_address(k): v for k, v in rois.items()}
        self._tracker_updated_at = time.time()  # M4: stamp for prior-decay

    async def rehydrate_position_cache(self, open_positions: list[Position] | None = None) -> None:
        """Load all open DB positions into the in-memory cache (H11).

        Must be called once at startup after portfolio.init(), before monitor.run().
        On a clean first start this is a no-op.  On restart it restores the
        position set so handle_price_tick() has a warm cache immediately.

        Pass *open_positions* when the caller already holds the result of
        ``portfolio.get_open_positions()`` to avoid a second DB round-trip.
        """
        if open_positions is None:
            open_positions = await self.portfolio.get_open_positions()
        self._pos_cache.clear()
        for p in open_positions:
            self._pos_cache.setdefault(p.token_id, []).append(p)
        logger.info(
            "H11: position cache rehydrated — %d open position(s) across %d token(s)",
            len(open_positions),
            len(self._pos_cache),
        )

    def _remove_pos_from_cache(self, pos: Position) -> None:
        """Remove a position from the in-memory cache (H11 helper)."""
        bucket = self._pos_cache.get(pos.token_id)
        if bucket:
            try:
                bucket.remove(pos)
            except ValueError:
                # Position was already evicted by a concurrent exit — safe to ignore.
                pass
            if not bucket:
                del self._pos_cache[pos.token_id]

    async def _flush_peak_cache(self) -> None:
        """Persist debounced peak_price updates to SQLite in a single batch commit (H11)."""
        self._last_peak_flush = time.monotonic()
        if not self._peak_dirty:
            return
        dirty = dict(self._peak_dirty)
        self._peak_dirty.clear()
        await self.portfolio.batch_update_peak_prices(dirty)

    def _record_skip(self, reason: str, event: TradeEvent, **detail) -> None:
        """Account a skipped copy: bump the skip counter and emit a structured event.

        Centralizes M16/M17 skip telemetry so every abandoned BUY is counted under a
        stable `reason` label and surfaced as a machine-readable `copy_skipped` event,
        without duplicating the boilerplate at each early-return site.
        """
        metrics.COPIES_SKIPPED.labels(reason=reason).inc()
        log_event(
            logger,
            "copy_skipped",
            reason=reason,
            trader=event.wallet_address,
            market_id=event.market_id,
            token_id=event.token_id,
            **detail,
        )

    async def handle_trade_event(self, event: TradeEvent) -> None:
        """Called by TradeMonitor on every new detected trade."""
        decision_start = time.monotonic()
        # detection_latency: monotonic (parse to here) — reliable, no clock skew
        detection_latency = decision_start - event.detected_at
        # wall_age: wall-clock age of the underlying on-chain trade
        wall_age = time.time() - event.timestamp

        logger.info(
            "Trade event from %s: %s $%.2f @ %.4f on %s | wall_age=%.2fs detect_latency=%.3fs",
            event.wallet_address[:10],
            event.trade_type.value,
            event.size_usdc,
            event.price,
            event.market_id[:10],
            wall_age,
            detection_latency,
        )
        metrics.TRADE_EVENTS.labels(trade_type=event.trade_type.value).inc()

        if self.config.mode == "paper":
            logger.info("[PAPER] Processing trade event %s", event.event_id)

        # 2. Mirror source exits before the non-BUY early return: if the tracked
        #    trader sold a token we hold, treat it as an exit signal.
        if event.trade_type != TradeType.BUY:
            if event.trade_type == TradeType.SELL and self.config.copy_trading.mirror_source_exits:
                await self._handle_source_exit(event)
            else:
                logger.debug("Skipping non-BUY trade")
            return

        # 2a. Portfolio circuit breakers (daily-loss limit, post-loss cooldown).
        #     Checked on the ENTRY path so they cannot be bypassed by opening new
        #     positions — evaluate() only governs exits of already-open positions.
        unrealized = await self.portfolio.get_open_unrealized_pnl_conservative()
        halt_reason = self.risk.is_trading_halted(unrealized_pnl=unrealized)
        if halt_reason:
            logger.warning("Skip: trading halted — %s", halt_reason)
            log_event(
                logger,
                "circuit_breaker_tripped",
                level=logging.WARNING,
                reason=halt_reason,
                unrealized_pnl=round(unrealized, 4),
                trader=event.wallet_address,
                market_id=event.market_id,
            )
            metrics.COPIES_SKIPPED.labels(reason="trading_halted").inc()
            return

        # 2b. Staleness gate. After this many seconds the source's alpha has
        #     decayed and we'd only be buying into their price impact (adverse
        #     selection). 0 disables the gate.
        max_age = self.config.copy_trading.max_trade_age_seconds
        if max_age > 0:
            # Sanity-clamp wall_age against NTP jumps: negative means clock skew
            # (treat as fresh), >3600 means definitely stale regardless of config.
            if wall_age > 3600:
                logger.info("Skip: trade is %.1fs old (>1h, always stale)", wall_age)
                self._record_skip("stale_trade", event, wall_age=round(wall_age, 1))
                return
            if wall_age > 0 and wall_age > max_age:
                logger.info("Skip: trade is %.1fs old > max %.1fs", wall_age, max_age)
                self._record_skip("stale_trade", event, wall_age=round(wall_age, 1))
                return

        # 3+4. Fetch market metadata and current price in parallel — both are
        #      independent I/O operations so gathering them halves latency on the
        #      critical detection→copy path.
        market, current_price = await asyncio.gather(
            self.gamma.get_market(event.market_id),
            self.gamma.get_market_price(event.token_id),
        )

        # 3. Resolution blackout. Fail CLOSED if market metadata is unavailable.
        if market is None and self.config.risk_management.fail_closed_on_missing_data:
            logger.info("Skip: market data unavailable for %s (fail-closed)", event.market_id[:10])
            self._record_skip("missing_market_data", event)
            return
        if market and market.resolve_time:
            blackout_hours = self.config.risk_management.resolution_blackout_hours
            hours_to_resolve = (market.resolve_time.timestamp() - time.time()) / 3600
            if 0 < hours_to_resolve < blackout_hours:
                logger.info("Skip: market resolves in %.1fh (blackout)", hours_to_resolve)
                self._record_skip("resolution_blackout", event, hours_to_resolve=round(hours_to_resolve, 1))
                return

        # H8: Validate the token is a recognized outcome for this market.
        # A mislabeled Data-API row → buying the wrong side = 100% loss.
        if market and (market.token_id_yes or market.token_id_no):
            known = {t for t in (market.token_id_yes, market.token_id_no) if t}
            if event.token_id not in known:
                logger.warning(
                    "Skip: token %s not recognized as YES/NO for market %s",
                    event.token_id[:10],
                    event.market_id[:10],
                )
                self._record_skip("unrecognized_token", event)
                return

        # 4. Price deviation check. Fail CLOSED if the current price is unknown.
        if current_price is None:
            if self.config.risk_management.fail_closed_on_missing_data:
                logger.info("Skip: current price unavailable for token %s (fail-closed)", event.token_id[:10])
                self._record_skip("missing_price", event)
                return
            current_price = event.price

        ct = self.config.copy_trading

        if event.price > 0:
            # H6: directional deviation gate — only adverse moves (we'd pay MORE than
            # the whale) gate the copy.  A favorable move (whale bought YES@0.40, price
            # now 0.36) has more upside to the same TP, so it should never be rejected.
            # We also guard against a collapsed price (>max_favorable_deviation below the
            # whale's entry) which likely signals adverse news rather than a good fill.
            signed_dev = (current_price - event.price) / event.price  # +ve = more expensive
            if signed_dev > ct.max_price_deviation:
                logger.info(
                    "Skip: adverse price move +%.1f%% (current=%.4f whale=%.4f > max %.1f%%)",
                    signed_dev * 100,
                    current_price,
                    event.price,
                    ct.max_price_deviation * 100,
                )
                self._record_skip(
                    "adverse_price_move",
                    event,
                    deviation_pct=round(signed_dev * 100, 2),
                    current_price=current_price,
                    whale_price=event.price,
                )
                return
            if signed_dev < -ct.max_favorable_deviation:
                logger.info(
                    "Skip: price collapsed %.1f%% below whale entry (likely adverse news)",
                    abs(signed_dev) * 100,
                )
                self._record_skip(
                    "favorable_collapse",
                    event,
                    deviation_pct=round(signed_dev * 100, 2),
                    current_price=current_price,
                    whale_price=event.price,
                )
                return

        # 4b. H7: entry-price band gate. Skip extreme prices where edge after fees
        # is effectively zero: a YES at 0.97 has ~3¢ upside vs 97¢ downside,
        # turning into a negative-EV trade after the ~2.5% round-trip cost.
        if not (ct.min_entry_price <= current_price <= ct.max_entry_price):
            logger.info(
                "Skip: entry price %.4f outside band [%.2f, %.2f]",
                current_price,
                ct.min_entry_price,
                ct.max_entry_price,
            )
            self._record_skip("entry_price_band", event, current_price=current_price)
            return

        # 4c. H5: pre-copy edge check — skip if round-trip fees consume all upside.
        # Estimate TP at current_price; if adjusted entry (current + round-trip cost)
        # already exceeds estimated TP, there is no profitable path after fees.
        tp_estimate, _ = self.risk._compute_thresholds(current_price)
        adj_entry = current_price * (1.0 + ct.round_trip_fee_pct)
        if tp_estimate <= adj_entry:
            logger.info(
                "Skip: post-fee edge exhausted (tp=%.4f <= adj_entry=%.4f at %.1f%% fee)",
                tp_estimate,
                adj_entry,
                ct.round_trip_fee_pct * 100,
            )
            self._record_skip("post_fee_edge", event, tp_estimate=round(tp_estimate, 4), adj_entry=round(adj_entry, 4))
            return

        # 5. Market volume check.
        if market and market.volume_24h < self.config.copy_trading.min_market_volume:
            logger.info(
                "Skip: 24h volume $%.0f < min $%.0f",
                market.volume_24h,
                self.config.copy_trading.min_market_volume,
            )
            self._record_skip("low_volume", event, volume_24h=market.volume_24h)
            return

        # 6. Compute conservative copy size.
        #    Default (kelly_enabled=False): flat size_multiplier formula.
        #    Opt-in Kelly: when enabled AND the trader has a large enough closed
        #    sample, size by fractional Kelly using their observed win rate. The
        #    max_trade_pct cap below is always enforced as a hard ceiling.
        max_cap_usdc = self.risk.bankroll * ct.max_trade_pct
        copy_size_usdc = min(event.size_usdc * ct.size_multiplier, max_cap_usdc)

        if ct.kelly_enabled:
            win_rate, sample = await self.portfolio.get_trader_win_rate(event.wallet_address)
            if sample >= ct.kelly_min_trades:
                copy_size_usdc = kelly_size_usdc(
                    win_rate,
                    current_price,
                    self.risk.bankroll,
                    ct.kelly_fraction_multiplier,
                    ct.max_trade_pct,
                )
            elif ct.kelly_seed_from_tracker and event.wallet_address in self._tracker_mean_pnl:
                # Our own closed-trade sample is too small to trust (self-reference
                # bias: portfolio win rate is shaped by our TP/SL rules, not the
                # trader's true edge). Seed from the tracker's leaderboard signal.
                # H18: size from the DEMONSTRATED edge (derived from mean per-trade
                # ROI) rather than raw win rate, so a favorite-buyer with a high win
                # rate but ~zero edge is not oversized.
                mean_roi = self._tracker_mean_pnl[event.wallet_address]
                edge = roi_to_edge(mean_roi, current_price)
                # M4: decay the prior toward neutral (zero edge) as it ages — fresh
                # leaderboard data is more reliable. A fully-decayed prior → 0 edge
                # → 0 Kelly size (the correct "no fresh information" outcome).
                decay = 1.0
                if ct.tracker_prior_decay_enabled:
                    hours = max(0.0, (time.time() - self._tracker_updated_at) / 3600.0)
                    decay = 1.0 / (1.0 + hours)
                effective_edge = edge * decay
                copy_size_usdc = kelly_size_from_edge(
                    effective_edge,
                    current_price,
                    self.risk.bankroll,
                    ct.kelly_fraction_multiplier,
                    ct.max_trade_pct,
                    edge_shrink=ct.kelly_edge_shrink,
                    max_edge=ct.kelly_max_edge,
                )
                logger.debug(
                    "Kelly: seeded edge=%.4f (roi=%.4f decay=%.3f) from tracker (own sample=%d < %d)",
                    effective_edge,
                    mean_roi,
                    decay,
                    sample,
                    ct.kelly_min_trades,
                )

        # Hard ceiling regardless of sizing path.
        copy_size_usdc = min(copy_size_usdc, max_cap_usdc)

        if copy_size_usdc <= 0 or current_price <= 0:
            self._record_skip("zero_size", event, copy_size_usdc=round(copy_size_usdc, 4))
            return

        size_shares = copy_size_usdc / max(current_price, 1e-6)

        resolve_ts = None
        if market and market.resolve_time:
            resolve_ts = market.resolve_time.timestamp()

        # 6b. M1: edge revalidation — moved BEFORE the lock (H12).  This is a network
        #     call; holding the entry lock across it would serialize all concurrent copy
        #     events head-of-line for the full RTT.  The price check is still valid: if
        #     the market moved adversely since we sized, skip.  A tiny additional drift
        #     while we wait for the lock is absorbed by the existing FOK/fill-reconcile.
        if ct.revalidate_edge_before_order:
            fresh_price = await self.gamma.get_market_price(event.token_id)
            if fresh_price is None:
                if self.config.risk_management.fail_closed_on_missing_data:
                    logger.info(
                        "Skip: revalidation price unavailable for %s (fail-closed)",
                        event.token_id[:10],
                    )
                    self._record_skip("missing_price", event)
                    return
            else:
                reval_dev = (fresh_price - current_price) / max(current_price, 1e-9)
                if reval_dev > ct.max_price_deviation:
                    logger.info(
                        "Skip: edge collapsed since detection (price %.4f -> %.4f, +%.1f%% > max %.1f%%)",
                        current_price,
                        fresh_price,
                        reval_dev * 100,
                        ct.max_price_deviation * 100,
                    )
                    self._record_skip(
                        "edge_collapsed_revalidation",
                        event,
                        current_price=current_price,
                        fresh_price=fresh_price,
                        deviation_pct=round(reval_dev * 100, 2),
                    )
                    return

        # 7–9. TOCTOU-critical section: cap checks + exposure reservation.
        # H12: the lock now covers only fast, non-blocking operations (DB reads +
        # in-memory exposure reservation).  Order I/O runs outside the lock so a
        # slow network call doesn't serialise concurrent copy events head-of-line.
        # After this block: pos is set, exposure is reserved, _pending_entries is
        # incremented, and pos is in the cache — all visible to concurrent entries.
        pos = None
        async with self._entry_lock:
            # 7. Global position cap.  H12: include _pending_entries — positions whose
            #    exposure is reserved but whose DB row is not yet committed.
            count = await self.portfolio.position_count()
            if count + self._pending_entries >= self.config.copy_trading.max_concurrent_positions:
                logger.info(
                    "Skip: max positions (%d + %d pending) reached",
                    count,
                    self._pending_entries,
                )
                self._record_skip("max_positions", event, count=count)
                return

            # 7a. Per-token cap — use in-memory cache (O(1), no DB read).  The cache
            #     includes pending entries added inside the lock, so two concurrent
            #     events for the same token are serialised correctly.
            max_per_token = self.config.copy_trading.max_positions_per_token
            if max_per_token > 0:
                token_count = len(self._pos_cache.get(event.token_id, []))
                if token_count >= max_per_token:
                    logger.info(
                        "Skip: max positions per token (%d) reached on %s",
                        max_per_token,
                        event.token_id[:10],
                    )
                    self._record_skip("max_per_token", event, max_per_token=max_per_token)
                    return

            # 8. Per-trader drawdown stop.
            trader_pnl = await self.portfolio.get_trader_pnl(event.wallet_address)
            if trader_pnl <= -(self.risk.bankroll * self.config.risk_management.drawdown_stop_pct):
                logger.info(
                    "Skip: trader %s drawdown stop (pnl=$%.2f)",
                    event.wallet_address[:10],
                    trader_pnl,
                )
                self._record_skip("trader_drawdown", event, trader_pnl=round(trader_pnl, 2))
                return

            # 9. build_position registers market exposure and enforces the exposure cap.
            try:
                pos = await self.risk.build_position(
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
                self._record_skip("exposure_cap", event)
                return

            # H11+H12: add to cache inside the lock so a concurrent entry for the same
            # token sees it in the per-token cap check (7a) above.  If the order fails
            # below, _remove_pos_from_cache() undoes this.
            self._pos_cache.setdefault(pos.token_id, []).append(pos)
            # H12: mark as pending so position_count() + _pending_entries is accurate
            # while the lock is not held during order I/O.
            try:
                self._pending_entries += 1
            except Exception:
                # Revert partial state on any unexpected exception while marking pending.
                self._remove_pos_from_cache(pos)
                await self.risk.release_exposure(
                    pos.market_id,
                    pos.entry_price * pos.size_shares,
                    pos.trader_address,
                )
                raise

        # 10. Order I/O + DB persistence — outside the lock (H12).
        # _pending_entries is always decremented in the finally block; cache and
        # exposure are cleaned up on every failure path before the return.
        order = Order(
            market_id=event.market_id,
            token_id=event.token_id,
            side="BUY",
            price=current_price,
            size_usdc=copy_size_usdc,
            # C2/M5: FOK ensures entries fill immediately in full or cancel.
            order_type=ct.entry_order_type,
        )
        try:
            # 10a. Place order.  M12: place_order_with_timeout handles GTC cancel+retry.
            try:
                order_result = await self.clob.place_order_with_timeout(order)
            except InsufficientLiquidityError as e:
                logger.info("Skip: insufficient liquidity — %s", e)
                self._remove_pos_from_cache(pos)
                await self.risk.release_exposure(pos.market_id, pos.entry_price * pos.size_shares, pos.trader_address)
                metrics.EXPOSURE_RELEASED.labels(cause="insufficient_liquidity").inc()
                self._record_skip("insufficient_liquidity", event)
                return
            except Exception as e:
                logger.error("Order placement failed: %s", e)
                self._remove_pos_from_cache(pos)
                await self.risk.release_exposure(pos.market_id, pos.entry_price * pos.size_shares, pos.trader_address)
                metrics.EXPOSURE_RELEASED.labels(cause="order_failed").inc()
                self._record_skip("order_failed", event)
                return

            # 10b. Reconcile against the ACTUAL fill.  In live trading an order can
            #      partially fill or not fill at all; assuming a full fill would
            #      overstate pos.size_shares (corrupting PnL, TP/SL share counts) and
            #      strand the unfilled exposure that build_position() reserved.
            filled_shares, avg_fill_price = self._reconcile_fill(order_result, size_shares, current_price)

            # Exposure accounting basis: build_position() registered
            # `entry_price * size_shares` at the PRE-fill current_price. Reconcile
            # against THAT notional so release + remaining == registered exactly.
            registered_notional = pos.entry_price * pos.size_shares  # == current_price * size_shares

            if filled_shares <= 0.0:
                self._remove_pos_from_cache(pos)
                await self.risk.release_exposure(pos.market_id, registered_notional, pos.trader_address)
                metrics.EXPOSURE_RELEASED.labels(cause="no_fill").inc()
                logger.info(
                    "Skip: order did not fill (0 shares) — released $%.2f exposure on %s",
                    registered_notional,
                    event.market_id[:10],
                )
                self._record_skip("no_fill", event, registered_notional=round(registered_notional, 2))
                return

            if filled_shares < size_shares:
                unfilled_fraction = (size_shares - filled_shares) / size_shares
                release_value = registered_notional * unfilled_fraction
                await self.risk.release_exposure(pos.market_id, release_value, pos.trader_address)
                metrics.EXPOSURE_RELEASED.labels(cause="partial_fill_remainder").inc()
                pos.size_shares = filled_shares
                logger.info(
                    "Partial fill: %.2f/%.2f shares — released $%.2f unfilled exposure on %s",
                    filled_shares,
                    size_shares,
                    release_value,
                    event.market_id[:10],
                )

            # H5: set entry/peak to actual fill price and recompute TP/SL.
            fill_price = avg_fill_price
            if fill_price != pos.entry_price:
                new_tp, new_sl = self.risk._compute_thresholds(fill_price)
                pos.entry_price = fill_price
                pos.peak_price = fill_price
                pos.tp_price = new_tp
                pos.sl_price = new_sl

            decision_latency = time.monotonic() - decision_start
            logger.info(
                "Latency | wall_age=%.2fs detect_latency=%.3fs decision_latency=%.3fs",
                wall_age,
                detection_latency,
                decision_latency,
            )

            await self.portfolio.open_position(pos)
            metrics.POSITIONS_OPENED.inc()
        finally:
            # H12: always decrement — whether the order succeeded, any early return
            # was taken, or an unexpected exception propagated.  position_count() now
            # counts the committed row; _pending_entries must not double-count it.
            if pos is not None:
                self._pending_entries -= 1

        if self.monitor:
            self.monitor.subscribe_token(event.token_id)

        logger.info(
            "Copied: %s $%.2f @ %.4f (fill %.4f) | TP=%.4f SL=%.4f | from %s",
            event.trade_type.value,
            copy_size_usdc,
            current_price,
            fill_price,
            pos.tp_price,
            pos.sl_price,
            event.wallet_address[:10],
        )
        log_event(
            logger,
            "position_opened",
            position_id=pos.position_id,
            trader=event.wallet_address,
            market_id=event.market_id,
            token_id=event.token_id,
            side="BUY",
            size_usdc=round(copy_size_usdc, 4),
            quoted_price=current_price,
            fill_price=fill_price,
            size_shares=round(pos.size_shares, 4),
            tp_price=pos.tp_price,
            sl_price=pos.sl_price,
            mode=self.config.mode,
            event_id=event.event_id,
        )

    @staticmethod
    def _reconcile_fill(
        order_result: object,
        size_shares: float,
        current_price: float,
    ) -> tuple[float, float]:
        """Extract the ACTUAL filled size and average fill price from a CLOB
        order result, with sensible fallbacks for live-result variants.

        Returns ``(filled_shares, avg_fill_price)``.

        PAPER results (``status == "PAPER"``) always report a FULL fill at the
        result's ``fill_price`` — reconciliation is a deliberate no-op for them
        so paper behaviour is byte-for-byte unchanged.

        Live result fields (best-effort, exchange-dependent):
          - filled size  : ``filled_size`` (shares) → ``matched_amount`` (shares)
                           → fall back to a full fill of ``size_shares``.
          - average price: ``avg_price`` → ``fill_price`` → ``price``
                           → fall back to ``current_price``.

        A non-dict / unrecognised result is treated as a full fill at
        ``current_price`` (preserving the prior optimistic assumption only when
        the venue tells us nothing).
        """
        if not isinstance(order_result, dict):
            return size_shares, current_price

        # PAPER → always a full fill at the paper fill_price (no-op path).
        if order_result.get("status") == "PAPER":
            avg = order_result.get("fill_price", current_price)
            return size_shares, float(avg)

        # Live: actual filled size, falling back through known field names. A
        # missing size field means the venue did not report one → assume full
        # fill (no behavioural regression vs. the prior code).
        if "filled_size" in order_result and order_result["filled_size"] is not None:
            filled_shares = float(order_result["filled_size"])
        elif "matched_amount" in order_result and order_result["matched_amount"] is not None:
            filled_shares = float(order_result["matched_amount"])
        else:
            filled_shares = size_shares

        # Average fill price, falling back through known field names then to the
        # pre-fill current_price.
        for key in ("avg_price", "fill_price", "price"):
            value = order_result.get(key)
            if value is not None:
                avg_fill_price = float(value)
                break
        else:
            avg_fill_price = current_price

        return filled_shares, avg_fill_price

    async def handle_price_tick(self, tick: PriceTick) -> None:
        """Called by the monitor's on_price callback for each real-time price update."""
        # H11: read from in-memory cache — no SQLite round-trip per tick.
        # Two tracked traders can each hold a SEPARATE position on the same token;
        # evaluate ALL of them so no position is orphaned on this tick.
        bucket = self._pos_cache.get(tick.token_id)
        if bucket:
            for pos in list(bucket):  # snapshot: _exit_position removes from bucket in-place
                reason = self.risk.evaluate(pos, tick.price)

                if pos.peak_price is None or tick.price > pos.peak_price:
                    # H11: update the in-memory object immediately; DB write is debounced.
                    pos.peak_price = tick.price
                    self._peak_dirty[pos.position_id] = tick.price

                if reason != ExitReason.HOLD:
                    await self._exit_position(pos, tick.price, reason)

        # H11: debounced peak flush — at most one SQLite batch-write per interval.
        # Runs unconditionally so dirty peaks from prior ticks are never stranded
        # if the next flush opportunity falls on a tick with no active positions.
        if self._peak_dirty and (time.monotonic() - self._last_peak_flush) >= self._peak_persist_interval:
            await self._flush_peak_cache()

    async def _exit_position(self, pos, price: float, reason: ExitReason) -> None:
        """Acquire the per-position lock and close the position, skipping if an exit is already in progress."""
        # C4: per-position lock prevents two concurrent exit triggers (WS tick vs
        # poll sweep vs SOURCE_EXIT) from both placing a SELL and recording the lot.
        lock = self._exit_locks.setdefault(pos.position_id, asyncio.Lock())
        if lock.locked():
            logger.debug(
                "Exit already in progress for %s — skipping concurrent trigger",
                pos.position_id,
            )
            return
        async with lock:
            await self._exit_position_locked(pos, price, reason)
        self._exit_locks.pop(pos.position_id, None)

    async def _exit_position_locked(self, pos, price: float, reason: ExitReason) -> None:
        """Place a SELL order with retry/backoff and close the DB record only after a confirmed fill."""
        exit_shares = pos.size_shares
        exit_order = Order(
            market_id=pos.market_id,
            token_id=pos.token_id,
            side="SELL",
            price=price,
            size_usdc=max(price * exit_shares, _EPSILON_USDC),
            # C2/M5: FAK (Fill-And-Kill / IOC, configurable via exit_order_type)
            # aggressively hits the bid instead of resting as a GTC limit at the
            # midpoint, which in a fast down-move trails the book and never
            # liquidates (unbounded loss).
            order_type=self.config.copy_trading.exit_order_type,
        )

        # Retry up to 3 times with exponential backoff. Only close the DB record
        # after a confirmed fill — a zero-fill or exception leaves the position open
        # so the next price tick or poll sweep can reattempt.
        filled_shares = 0.0
        avg_fill_price = price
        for attempt in range(3):
            try:
                # M12: route through the timeout orchestrator so a resting (non-FAK)
                # exit is cancel-confirmed before any re-post — no stacked live SELLs.
                # FAK (default exit) and paper mode delegate straight through.
                exit_result = await self.clob.place_order_with_timeout(exit_order)
                # C3: reconcile the ACTUAL fill — "placed" ≠ "filled" on live orders.
                # A FAK that returns without raising can still report zero filled_size
                # (thin book, nobody on the bid). Treat zero-fill as a failed attempt
                # rather than a phantom close that reports PnL on shares still held.
                filled_shares, avg_fill_price = self._reconcile_fill(exit_result, exit_shares, price)
                if filled_shares > 0.0:
                    break
                logger.warning(
                    "Exit order attempt %d/3: zero fill for %s — will retry",
                    attempt + 1,
                    pos.position_id,
                )
                if attempt < 2:
                    await asyncio.sleep(2**attempt)
            except Exception as e:
                if attempt < 2:
                    logger.warning(
                        "Exit order attempt %d/3 failed for %s: %s",
                        attempt + 1,
                        pos.position_id,
                        e,
                    )
                    await asyncio.sleep(2**attempt)
                else:
                    logger.error(
                        "Exit order permanently failed for %s after 3 attempts: %s — manual intervention required",
                        pos.position_id,
                        e,
                    )

        if filled_shares <= 0.0:
            # No confirmed fill — leave position open for re-evaluation next tick.
            return

        # C4: `AND status='open'` in close_position() is the DB-level guard that
        # prevents a double-record even if the application lock were somehow bypassed.
        pnl = await self.portfolio.close_position(pos.position_id, avg_fill_price, reason)
        if pnl is None:
            # close_position returns None when the position was not found or was already
            # closed by a concurrent exit path (C4 double-exit race guard). Do not
            # double-call record_exit or record metrics for the same close.
            logger.warning(
                "close_position returned None for %s — skipping record_exit (already closed or not found)",
                pos.position_id,
            )
            return
        # H11: position is committed-closed in the DB; evict from in-memory cache and
        # remove any pending peak write that would re-open the DB row's peak_price.
        self._remove_pos_from_cache(pos)
        self._peak_dirty.pop(pos.position_id, None)

        await self.risk.record_exit(pos, avg_fill_price, reason)

        if self.monitor:
            self.monitor.unsubscribe_token(pos.token_id)

        # M16: count the exit and record its PnL distribution AFTER the race-guard
        # return above, so a double-trigger (WS tick vs poll sweep vs SOURCE_EXIT)
        # can never double-count a single close.
        metrics.EXITS.labels(reason=reason.name).inc()
        metrics.EXIT_PNL.labels(reason=reason.name).observe(pnl)

        logger.info(
            "Exited [%s]: %s pnl=$%.4f @ %.4f (filled=%.2f/%0.2f shares)",
            reason.name,
            pos.position_id,
            pnl,
            avg_fill_price,
            filled_shares,
            exit_shares,
        )
        log_event(
            logger,
            "position_closed",
            position_id=pos.position_id,
            reason=reason.name,
            pnl=round(pnl, 4),
            exit_price=avg_fill_price,
            entry_price=pos.entry_price,
            filled_shares=round(filled_shares, 4),
            requested_shares=round(exit_shares, 4),
            trader=pos.trader_address,
            market_id=pos.market_id,
            token_id=pos.token_id,
        )

    async def _handle_source_exit(self, event: TradeEvent) -> None:
        """Exit our copy position when the tracked trader exits theirs.

        Only acts on position(s) for the same token that were copied FROM the
        same trader — a coincidental sale by a different wallet is irrelevant to
        our thesis for the position. Multiple traders can hold the same token,
        so close only the matching wallet's copies and leave the others open.
        """
        # H11: use in-memory cache for the lookup (no DB read).
        positions = [p for p in self._pos_cache.get(event.token_id, []) if p.trader_address == event.wallet_address]
        if not positions:
            return

        # Use the freshest available price; fall back to the event price. Fetch
        # once and reuse across every matching position on this token.
        exit_price = await self.gamma.get_market_price(event.token_id)
        if exit_price is None:
            exit_price = event.price

        for pos in positions:
            logger.info(
                "Source exit signal: trader %s sold %s — closing copy position %s",
                event.wallet_address[:10],
                event.token_id[:10],
                pos.position_id,
            )
            await self._exit_position(pos, exit_price, ExitReason.SOURCE_EXIT)

    async def check_all_exits(self) -> None:
        """Poll-based exit check fallback (when WS price feed is unavailable).

        H10: Fetches prices in parallel (asyncio.gather) instead of serially.
        Serial fetches blocked exit detection by N*RTT (e.g. 5 positions * 200ms = 1s)
        meaning a fast adverse move during a WS outage could blow through SL unmanaged.
        """
        positions = await self.portfolio.get_open_positions()
        if not positions:
            return

        prices = await asyncio.gather(
            *(self.gamma.get_market_price(p.token_id) for p in positions),
            return_exceptions=True,
        )

        for pos, price in zip(positions, prices, strict=True):
            # gather(return_exceptions=True) yields BaseException on failure; skip
            # those and any None (price unavailable). The isinstance check narrows
            # the type so `price` is a plain float below.
            if isinstance(price, BaseException) or price is None:
                continue
            reason = self.risk.evaluate(pos, price)
            if reason != ExitReason.HOLD:
                await self._exit_position(pos, price, reason)

    async def check_trader_demotion(
        self,
        min_trades: int = 10,
    ) -> list[str]:
        """Drop tracked traders whose Wilson upper bound on copy win-rate is below
        config min_win_rate. Returns list of demoted wallet addresses."""
        import math

        demoted = []
        min_win_rate = self.config.trader_selection.min_win_rate
        for addr in list(self._tracker_win_rates.keys()):
            win_rate, sample = await self.portfolio.get_trader_win_rate(addr)
            if sample < min_trades:
                continue
            # Wilson upper confidence bound (95% CI, z=1.645 one-sided)
            z = 1.645
            n = sample
            p = win_rate
            denom = 1 + z**2 / n
            centre = (p + z**2 / (2 * n)) / denom
            margin = (z * math.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))) / denom
            wilson_upper = centre + margin
            if wilson_upper < min_win_rate:
                demoted.append(addr)
                self._demoted_traders.add(addr)
                logger.warning(
                    "Demoting trader %s: Wilson upper=%.3f < min_win_rate=%.3f (n=%d)",
                    addr[:10],
                    wilson_upper,
                    min_win_rate,
                    sample,
                )
                log_event(
                    logger,
                    "trader_demoted",
                    level=logging.WARNING,
                    trader=addr,
                    wilson_upper=round(wilson_upper, 4),
                    min_win_rate=min_win_rate,
                    observed_win_rate=round(win_rate, 4),
                    sample=sample,
                    reason="wilson_below_min_win_rate",
                )
        # Remove demoted traders from the tracker priors so Kelly stops using them
        for addr in demoted:
            self._tracker_win_rates.pop(addr, None)
            self._tracker_mean_pnl.pop(addr, None)  # H18: also drop the edge signal
        if demoted:
            metrics.TRADERS_DEMOTED.inc(len(demoted))
        return demoted


# Minimum order size ($0.01) — keeps Order model's gt=0 validation happy and
# is a sensible practical floor for real order books.
_EPSILON_USDC = 0.01
