"""
core/metrics.py — Optional Prometheus metrics for the copy-trading bot (M16).

prometheus_client is an OPTIONAL dependency. When it is not installed, every
metric here becomes a no-op so call sites stay unconditional — no scattered
`if _PROM_AVAILABLE:` guards in copier.py / risk_manager.py. This mirrors the
graceful-degradation pattern monitor.py uses for the optional `websockets` dep.

USAGE
-----
    from polymarket_copier.core import metrics
    metrics.BANKROLL.set(risk.bankroll)
    metrics.EXITS.labels(reason="TAKE_PROFIT").inc()
    metrics.EXIT_PNL.labels(reason="STOP_LOSS").observe(pnl)
    metrics.start_metrics_server(9090)   # no-op if prometheus_client absent

Gauges are best refreshed from a single periodic collector (see main.py's
metrics_loop) rather than sprinkling .set() calls everywhere; counters and
histograms are incremented inline at the event call sites.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("polymarket_copier")

# prometheus_client is an optional dep; linter-safe import with graceful fallback.
# When absent, bind the metric-class names to None so the _make(Gauge, ...) call
# sites below still resolve (the kind arg is only used when _PROM_AVAILABLE).
try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server

    _PROM_AVAILABLE = True
except ImportError:
    Counter = Gauge = Histogram = None  # type: ignore[assignment,misc]
    start_http_server = None  # type: ignore[assignment]
    _PROM_AVAILABLE = False


class _NoopMetric:
    """Stand-in returned when prometheus_client is not installed.

    Accepts (and ignores) every call the real Gauge/Counter/Histogram API exposes
    so call sites need no availability guard. labels() returns self so chained
    `.labels(...).inc()` works unchanged.
    """

    def labels(self, *args, **kwargs) -> "_NoopMetric":
        return self

    def set(self, *args, **kwargs) -> None:
        pass

    def inc(self, *args, **kwargs) -> None:
        pass

    def observe(self, *args, **kwargs) -> None:
        pass


def _make(kind, name: str, help_: str, labelnames: tuple = ()):
    """Build a real prometheus metric, or a _NoopMetric when the lib is absent."""
    if not _PROM_AVAILABLE:
        return _NoopMetric()
    if labelnames:
        return kind(name, help_, labelnames)
    return kind(name, help_)


# ─── Gauges (refreshed periodically by main.py's metrics_loop) ────────────────

BANKROLL = _make(Gauge, "copybot_bankroll_usd", "Current bankroll in USDC.")
DAILY_PNL = _make(Gauge, "copybot_daily_pnl_usd", "Realized PnL in the current UTC day (resets at midnight UTC).")
OPEN_POSITIONS = _make(Gauge, "copybot_open_positions", "Number of currently open copy-trade positions.")
OPEN_UNREALIZED_PNL = _make(
    Gauge,
    "copybot_open_unrealized_pnl_usd",
    "Conservative (SL-based) mark-to-market unrealized PnL across open positions; <= 0.",
)
TOTAL_EXPOSURE = _make(Gauge, "copybot_total_exposure_usd", "Total USDC deployed across all open markets.")
TRADING_HALTED = _make(
    Gauge,
    "copybot_trading_halted",
    "1 if new entries are currently blocked (daily-loss limit or post-loss cooldown), else 0.",
)
CONSECUTIVE_LOSSES = _make(Gauge, "copybot_consecutive_losses", "Current consecutive-loss streak count.")
COOLDOWN_SECONDS_REMAINING = _make(
    Gauge,
    "copybot_cooldown_seconds_remaining",
    "Seconds remaining on the active post-loss cooldown (0 if none).",
)
TRACKED_TRADERS = _make(Gauge, "copybot_tracked_traders", "Number of traders currently tracked from the leaderboard.")
LAST_TRACKER_REFRESH = _make(
    Gauge,
    "copybot_last_tracker_refresh_timestamp",
    "Unix timestamp of the last successful tracker refresh().",
)
TRADER_SCORE = _make(
    Gauge,
    "copybot_trader_score",
    "Composite score of each tracked trader.",
    ("trader_address", "rank"),
)

# ─── Counters (incremented inline at event call sites) ────────────────────────

TRADE_EVENTS = _make(
    Counter,
    "copybot_trade_events_total",
    "Trade events received from the monitor.",
    ("trade_type",),
)
POSITIONS_OPENED = _make(
    Counter,
    "copybot_positions_opened_total",
    "Copy positions successfully opened (order filled and persisted).",
)
COPIES_SKIPPED = _make(
    Counter,
    "copybot_copies_skipped_total",
    "Detected BUY events that did not result in a copy.",
    ("reason",),
)
EXITS = _make(Counter, "copybot_exits_total", "Position exits, labeled by ExitReason.", ("reason",))
TRADERS_DEMOTED = _make(
    Counter,
    "copybot_traders_demoted_total",
    "Traders demoted for failing the Wilson win-rate floor.",
)
EXPOSURE_RELEASED = _make(
    Counter,
    "copybot_exposure_released_total",
    "Exposure rollbacks via release_exposure().",
    ("cause",),
)

# ─── Histograms ───────────────────────────────────────────────────────────────

EXIT_PNL = _make(
    Histogram,
    "copybot_exit_pnl_usd",
    "Distribution of per-exit realized PnL in USDC.",
    ("reason",),
)


def prometheus_available() -> bool:
    """Whether prometheus_client is installed (metrics are live vs. no-ops)."""
    return _PROM_AVAILABLE


def start_metrics_server(port: int = 9090) -> bool:
    """Start the Prometheus scrape endpoint on the given port.

    Returns True if the HTTP server started, False if prometheus_client is not
    installed (metrics remain no-ops). Never raises on a missing dependency.
    """
    if not _PROM_AVAILABLE:
        logger.warning("prometheus_client not installed. Metrics disabled. Install with: pip install prometheus-client")
        return False
    start_http_server(port)
    logger.info("Prometheus metrics server started on port %d", port)
    return True
