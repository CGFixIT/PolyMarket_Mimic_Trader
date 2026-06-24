"""Copy trade engine v2 — range-relative risk management + resolution blackout."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from polymarket_copier.api.clob_client import ClobClient, InsufficientLiquidityError
from polymarket_copier.api.gamma_client import GammaClient
from polymarket_copier.config import AppConfig
from polymarket_copier.core.monitor import PriceTick, TradeEvent, TradeType
from polymarket_copier.core.portfolio import PortfolioManager
from polymarket_copier.core.risk_manager import ExitReason, ExposureCapError, RiskManager
from polymarket_copier.core.sizing import kelly_size_usdc
from polymarket_copier.models.types import Order

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
        # C4: per-position asyncio locks prevent a double-exit race between two
        # concurrent triggers (WS tick vs poll sweep vs SOURCE_EXIT).  The DB
        # `AND status='open'` guard in close_position() is the belt; this is the
        # suspenders — it stops us placing two live SELL orders for one position.
        self._exit_locks: dict[str, asyncio.Lock] = {}
        # H13: tracks demoted traders so Kelly sizing stops using them.
        self._demoted_traders: set[str] = set()

    def update_tracker_win_rates(self, rates: dict[str, float]) -> None:
        """Replace the current tracker-win-rate prior with freshly scored data.

        Called by main.py after each TrackerClient.refresh() so Kelly sizing
        always uses the most recent leaderboard win rates as a prior during
        the warm-up period before the bot's own sample is large enough.
        """
        self._tracker_win_rates = dict(rates)

    async def handle_trade_event(self, event: TradeEvent) -> None:
        """Called by TradeMonitor on every new detected trade."""
        decision_start = time.monotonic()
        # detection_latency: monotonic (parse to here) — reliable, no clock skew
        detection_latency = decision_start - event.detected_at
        # wall_age: wall-clock age of the underlying on-chain trade
        wall_age = time.time() - event.timestamp

        logger.info(
            "Trade event from %s: %s $%.2f @ %.4f on %s | "
            "wall_age=%.2fs detect_latency=%.3fs",
            event.wallet_address[:10], event.trade_type.value,
            event.size_usdc, event.price, event.market_id[:10],
            wall_age, detection_latency,
        )

        if self.config.mode == "paper":
            logger.info("[PAPER] Processing trade event %s", event.event_id)

        # 2. Mirror source exits before the non-BUY early return: if the tracked
        #    trader sold a token we hold, treat it as an exit signal.
        if event.trade_type != TradeType.BUY:
            if (
                event.trade_type == TradeType.SELL
                and self.config.copy_trading.mirror_source_exits
            ):
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
                return
            if wall_age > 0 and wall_age > max_age:
                logger.info("Skip: trade is %.1fs old > max %.1fs", wall_age, max_age)
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
            logger.info("Skip: market data unavailable for %s (fail-closed)",
                        event.market_id[:10])
            return
        if market and market.resolve_time:
            blackout_hours = self.config.risk_management.resolution_blackout_hours
            hours_to_resolve = (market.resolve_time.timestamp() - time.time()) / 3600
            if 0 < hours_to_resolve < blackout_hours:
                logger.info("Skip: market resolves in %.1fh (blackout)", hours_to_resolve)
                return

        # H8: Validate the token is a recognized outcome for this market.
        # A mislabeled Data-API row → buying the wrong side = 100% loss.
        if market and (market.token_id_yes or market.token_id_no):
            known = {t for t in (market.token_id_yes, market.token_id_no) if t}
            if event.token_id not in known:
                logger.warning(
                    "Skip: token %s not recognized as YES/NO for market %s",
                    event.token_id[:10], event.market_id[:10],
                )
                return

        # 4. Price deviation check. Fail CLOSED if the current price is unknown.
        if current_price is None:
            if self.config.risk_management.fail_closed_on_missing_data:
                logger.info("Skip: current price unavailable for token %s (fail-closed)",
                            event.token_id[:10])
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
                    signed_dev * 100, current_price, event.price,
                    ct.max_price_deviation * 100,
                )
                return
            if signed_dev < -ct.max_favorable_deviation:
                logger.info(
                    "Skip: price collapsed %.1f%% below whale entry (likely adverse news)",
                    abs(signed_dev) * 100,
                )
                return

        # 4b. H7: entry-price band gate. Skip extreme prices where edge after fees
        # is effectively zero: a YES at 0.97 has ~3¢ upside vs 97¢ downside,
        # turning into a negative-EV trade after the ~2.5% round-trip cost.
        if not (ct.min_entry_price <= current_price <= ct.max_entry_price):
            logger.info(
                "Skip: entry price %.4f outside band [%.2f, %.2f]",
                current_price, ct.min_entry_price, ct.max_entry_price,
            )
            return

        # 4c. H5: pre-copy edge check — skip if round-trip fees consume all upside.
        # Estimate TP at current_price; if adjusted entry (current + round-trip cost)
        # already exceeds estimated TP, there is no profitable path after fees.
        tp_estimate, _ = self.risk._compute_thresholds(current_price)
        adj_entry = current_price * (1.0 + ct.round_trip_fee_pct)
        if tp_estimate <= adj_entry:
            logger.info(
                "Skip: post-fee edge exhausted (tp=%.4f <= adj_entry=%.4f at %.1f%% fee)",
                tp_estimate, adj_entry, ct.round_trip_fee_pct * 100,
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
        #    Default (kelly_enabled=False): flat size_multiplier formula.
        #    Opt-in Kelly: when enabled AND the trader has a large enough closed
        #    sample, size by fractional Kelly using their observed win rate. The
        #    max_trade_pct cap below is always enforced as a hard ceiling.
        max_cap_usdc = self.risk.bankroll * ct.max_trade_pct
        copy_size_usdc = min(event.size_usdc * ct.size_multiplier, max_cap_usdc)

        if ct.kelly_enabled:
            win_rate, sample = await self.portfolio.get_trader_win_rate(
                event.wallet_address
            )
            if sample >= ct.kelly_min_trades:
                copy_size_usdc = kelly_size_usdc(
                    win_rate,
                    current_price,
                    self.risk.bankroll,
                    ct.kelly_fraction_multiplier,
                    ct.max_trade_pct,
                )
            elif ct.kelly_seed_from_tracker and event.wallet_address in self._tracker_win_rates:
                # Our own closed-trade sample is too small to trust (self-reference
                # bias: portfolio win rate is shaped by our TP/SL rules, not the
                # trader's true edge). Fall back to the tracker's leaderboard win
                # rate as a less-biased prior during warm-up.
                tracker_win_rate = self._tracker_win_rates[event.wallet_address]
                copy_size_usdc = kelly_size_usdc(
                    tracker_win_rate,
                    current_price,
                    self.risk.bankroll,
                    ct.kelly_fraction_multiplier,
                    ct.max_trade_pct,
                )
                logger.debug(
                    "Kelly: seeded win_rate=%.3f from tracker (own sample=%d < %d)",
                    tracker_win_rate, sample, ct.kelly_min_trades,
                )

        # Hard ceiling regardless of sizing path.
        copy_size_usdc = min(copy_size_usdc, max_cap_usdc)

        if copy_size_usdc <= 0 or current_price <= 0:
            return

        size_shares = copy_size_usdc / max(current_price, 1e-6)

        resolve_ts = None
        if market and market.resolve_time:
            resolve_ts = market.resolve_time.timestamp()

        # 7–10. The position-count check (7) and open_position() write (10) must be
        #        atomic. Without a lock, concurrent wallet polls via asyncio.gather
        #        both read count=N < max before either writes, opening one extra
        #        position beyond the cap (TOCTOU). The lock also covers the order
        #        placement I/O — acceptable here because we copy at most 5 wallets
        #        and concurrent entry events are rare and brief.
        async with self._entry_lock:
            # 7. Cheap gating checks FIRST — before registering exposure, so that a
            #    rejection here can never leak phantom exposure into the RiskManager.
            count = await self.portfolio.position_count()
            if count >= self.config.copy_trading.max_concurrent_positions:
                logger.info("Skip: max positions (%d) reached", count)
                return

            # 7a. M9: per-token position cap. Inside the entry lock (with the global
            #     count check) so concurrent polls can't both pass and breach it.
            #     Prevents two tracked traders piling unbounded copies onto one token.
            max_per_token = self.config.copy_trading.max_positions_per_token
            if max_per_token > 0:
                token_positions = await self.portfolio.get_positions_by_token(event.token_id)
                if len(token_positions) >= max_per_token:
                    logger.info(
                        "Skip: max positions per token (%d) reached on %s",
                        max_per_token, event.token_id[:10],
                    )
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
                return

            order = Order(
                market_id=event.market_id,
                token_id=event.token_id,
                side="BUY",
                price=current_price,
                size_usdc=copy_size_usdc,
                # C2: FOK ensures entries either fill immediately or cancel — no resting
                # GTC limit at the midpoint that fills adversely or never at all.
                order_type="FOK",
            )

            # 10. Place order. On ANY failure, release the exposure build_position
            #     registered so a never-opened position cannot leak the exposure cap.
            try:
                order_result = await self.clob.place_order(order)
            except InsufficientLiquidityError as e:
                logger.info("Skip: insufficient liquidity — %s", e)
                await self.risk.release_exposure(
                    pos.market_id, pos.entry_price * pos.size_shares, pos.trader_address
                )
                return
            except Exception as e:
                logger.error("Order placement failed: %s", e)
                await self.risk.release_exposure(
                    pos.market_id, pos.entry_price * pos.size_shares, pos.trader_address
                )
                return

            # 10a. Reconcile against the ACTUAL fill. In live trading an order can
            #      partially fill or not fill at all; assuming a full fill would
            #      overstate pos.size_shares (corrupting PnL, TP/SL share counts) and
            #      strand the unfilled exposure that build_position() reserved.
            #      PAPER results report a full fill at fill_price, so this is a no-op
            #      there and paper behaviour is preserved exactly.
            filled_shares, avg_fill_price = self._reconcile_fill(
                order_result, size_shares, current_price
            )

            # Exposure accounting basis: build_position() registered
            # `entry_price * size_shares` at the PRE-fill current_price into BOTH the
            # market and trader buckets. We reconcile against THAT same notional so
            # nothing leaks — releasing the unfilled fraction of the registered
            # notional (not the fill-priced notional) keeps release + remaining ==
            # registered exactly.
            registered_notional = pos.entry_price * pos.size_shares  # == current_price * size_shares

            if filled_shares <= 0.0:
                # No fill: release the FULL registered notional and abort without
                # opening a position or subscribing the token.
                await self.risk.release_exposure(
                    pos.market_id, registered_notional, pos.trader_address
                )
                logger.info(
                    "Skip: order did not fill (0 shares) — released $%.2f exposure on %s",
                    registered_notional, event.market_id[:10],
                )
                return

            if filled_shares < size_shares:
                # Partial fill: release the unfilled fraction of the REGISTERED
                # notional so market+trader exposure reflect only the shares actually
                # acquired. unfilled_fraction is computed against the original
                # size_shares (the notional basis), so released + remaining ==
                # registered_notional.
                unfilled_fraction = (size_shares - filled_shares) / size_shares
                release_value = registered_notional * unfilled_fraction
                await self.risk.release_exposure(
                    pos.market_id, release_value, pos.trader_address
                )
                pos.size_shares = filled_shares
                logger.info(
                    "Partial fill: %.2f/%.2f shares — released $%.2f unfilled exposure on %s",
                    filled_shares, size_shares, release_value, event.market_id[:10],
                )

            # H5: Set entry/peak to the actual average fill price and recompute TP/SL.
            # In PAPER mode this is the slippage/fee-adjusted fill_price (full fill).
            # In LIVE it is the real execution price (avg_fill from reconciled response).
            # Recomputing TP/SL at fill_price ensures thresholds are correct for the
            # price we actually paid — not the midpoint we quoted.
            fill_price = avg_fill_price
            if fill_price != pos.entry_price:
                new_tp, new_sl = self.risk._compute_thresholds(fill_price)
                pos.entry_price = fill_price
                pos.peak_price  = fill_price
                pos.tp_price    = new_tp
                pos.sl_price    = new_sl

            decision_latency = time.monotonic() - decision_start
            logger.info(
                "Latency | wall_age=%.2fs detect_latency=%.3fs decision_latency=%.3fs",
                wall_age, detection_latency, decision_latency,
            )

            await self.portfolio.open_position(pos)

        if self.monitor:
            self.monitor.subscribe_token(event.token_id)

        logger.info(
            "Copied: %s $%.2f @ %.4f (fill %.4f) | TP=%.4f SL=%.4f | from %s",
            event.trade_type.value, copy_size_usdc, current_price, fill_price,
            pos.tp_price, pos.sl_price, event.wallet_address[:10],
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
        # Two tracked traders can each be copied into a SEPARATE position on the
        # same token. Evaluate ALL of them on this tick — fetching only one would
        # orphan the rest, silently missing their TP/SL/trailing/time exits until
        # a poll-based check happens to catch them.
        positions = await self.portfolio.get_positions_by_token(tick.token_id)
        for pos in positions:
            # evaluate() uses effective_peak internally without mutating pos.peak_price.
            # We own the DB write here so concurrent tick handlers can't race on
            # in-memory state.
            reason = self.risk.evaluate(pos, tick.price)

            if pos.peak_price is None or tick.price > pos.peak_price:
                await self.portfolio.update_peak_price(pos.position_id, tick.price)

            if reason != ExitReason.HOLD:
                await self._exit_position(pos, tick.price, reason)

    async def _exit_position(self, pos, price: float, reason: ExitReason) -> None:
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
        exit_shares = pos.size_shares
        exit_order = Order(
            market_id=pos.market_id,
            token_id=pos.token_id,
            side="SELL",
            price=price,
            size_usdc=max(price * exit_shares, _EPSILON_USDC),
            # C2: FAK (Fill-And-Kill / IOC) aggressively hits the bid instead of
            # resting as a GTC limit at the midpoint, which in a fast down-move
            # trails the book and never liquidates (unbounded loss).
            order_type="FAK",
        )

        # Retry up to 3 times with exponential backoff. Only close the DB record
        # after a confirmed fill — a zero-fill or exception leaves the position open
        # so the next price tick or poll sweep can reattempt.
        filled_shares = 0.0
        avg_fill_price = price
        for attempt in range(3):
            try:
                exit_result = await self.clob.place_order(exit_order)
                # C3: reconcile the ACTUAL fill — "placed" ≠ "filled" on live orders.
                # A FAK that returns without raising can still report zero filled_size
                # (thin book, nobody on the bid). Treat zero-fill as a failed attempt
                # rather than a phantom close that reports PnL on shares still held.
                filled_shares, avg_fill_price = self._reconcile_fill(
                    exit_result, exit_shares, price
                )
                if filled_shares > 0.0:
                    break
                logger.warning(
                    "Exit order attempt %d/3: zero fill for %s — will retry",
                    attempt + 1, pos.position_id,
                )
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                if attempt < 2:
                    logger.warning(
                        "Exit order attempt %d/3 failed for %s: %s",
                        attempt + 1, pos.position_id, e,
                    )
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.error(
                        "Exit order permanently failed for %s after 3 attempts: %s — "
                        "manual intervention required",
                        pos.position_id, e,
                    )

        if filled_shares <= 0.0:
            # No confirmed fill — leave position open for re-evaluation next tick.
            return

        # C4: `AND status='open'` in close_position() is the DB-level guard that
        # prevents a double-record even if the application lock were somehow bypassed.
        pnl = await self.portfolio.close_position(pos.position_id, avg_fill_price, reason)
        if pnl == 0.0 and reason != ExitReason.HOLD:
            # close_position returned 0.0 due to the DB race guard (position was
            # already closed by a concurrent exit path). Do not double-call record_exit.
            logger.warning(
                "close_position returned 0.0 for %s — skipping record_exit (already closed)",
                pos.position_id,
            )
            return
        await self.risk.record_exit(pos, avg_fill_price)

        if self.monitor:
            self.monitor.unsubscribe_token(pos.token_id)

        logger.info(
            "Exited [%s]: %s pnl=$%.4f @ %.4f (filled=%.2f/%0.2f shares)",
            reason.name, pos.position_id, pnl, avg_fill_price, filled_shares, exit_shares,
        )

    async def _handle_source_exit(self, event: TradeEvent) -> None:
        """Exit our copy position when the tracked trader exits theirs.

        Only acts on position(s) for the same token that were copied FROM the
        same trader — a coincidental sale by a different wallet is irrelevant to
        our thesis for the position. Multiple traders can hold the same token,
        so close only the matching wallet's copies and leave the others open.
        """
        positions = [
            p
            for p in await self.portfolio.get_positions_by_token(event.token_id)
            if p.trader_address == event.wallet_address
        ]
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
                event.wallet_address[:10], event.token_id[:10], pos.position_id,
            )
            await self._exit_position(pos, exit_price, ExitReason.SOURCE_EXIT)

    async def check_all_exits(self) -> None:
        """Poll-based exit check fallback (when WS price feed is unavailable).

        H10: Fetches prices in parallel (asyncio.gather) instead of serially.
        Serial fetches blocked exit detection by N×RTT (e.g. 5 positions × 200ms = 1s)
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
            centre = (p + z**2 / (2*n)) / denom
            margin = (z * math.sqrt(p*(1-p)/n + z**2/(4*n**2))) / denom
            wilson_upper = centre + margin
            if wilson_upper < min_win_rate:
                demoted.append(addr)
                self._demoted_traders.add(addr)
                logger.warning(
                    "Demoting trader %s: Wilson upper=%.3f < min_win_rate=%.3f (n=%d)",
                    addr[:10], wilson_upper, min_win_rate, sample,
                )
        # Remove demoted traders from win-rate tracking so Kelly stops using them
        for addr in demoted:
            self._tracker_win_rates.pop(addr, None)
        return demoted


# Minimum order size ($0.01) — keeps Order model's gt=0 validation happy and
# is a sensible practical floor for real order books.
_EPSILON_USDC = 0.01
