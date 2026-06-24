"""Tests for core/metrics.py — optional Prometheus metrics with graceful no-op fallback (M16)."""

from __future__ import annotations

from polymarket_copier.core import metrics
from polymarket_copier.core.metrics import _NoopMetric, _make, prometheus_available, start_metrics_server


class TestNoopMetric:
    """When prometheus_client is absent, every metric op must be a harmless no-op."""

    def test_labels_returns_self_for_chaining(self):
        m = _NoopMetric()
        assert m.labels(reason="x") is m
        # chained .labels(...).inc() must not raise
        m.labels(a=1, b=2).inc()

    def test_set_inc_observe_are_noops(self):
        m = _NoopMetric()
        # None of these raise or return anything meaningful
        assert m.set(1.0) is None
        assert m.inc() is None
        assert m.inc(5) is None
        assert m.observe(3.14) is None


class TestMake:
    def test_make_returns_noop_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(metrics, "_PROM_AVAILABLE", False)
        m = _make(None, "copybot_test_x", "help", ("label",))
        assert isinstance(m, _NoopMetric)
        # Usable without error
        m.labels(label="v").inc()

    def test_make_builds_real_metric_when_available(self, monkeypatch):
        # Build a real prometheus metric only if the lib is importable; otherwise
        # this branch is environment-gated and skipped.
        try:
            from prometheus_client import Counter
        except ImportError:
            return  # optional dep not installed in this environment
        monkeypatch.setattr(metrics, "_PROM_AVAILABLE", True)
        c = _make(Counter, "copybot_unit_test_counter_total", "help", ("reason",))
        assert not isinstance(c, _NoopMetric)
        c.labels(reason="ok").inc()  # exercises the real API


class TestModuleMetrics:
    """The declared module-level metrics must all expose the no-op/real API uniformly."""

    def test_all_metrics_support_their_operations(self):
        # Counters / gauges
        metrics.TRADE_EVENTS.labels(trade_type="BUY").inc()
        metrics.POSITIONS_OPENED.inc()
        metrics.COPIES_SKIPPED.labels(reason="low_volume").inc()
        metrics.EXITS.labels(reason="TAKE_PROFIT").inc()
        metrics.EXPOSURE_RELEASED.labels(cause="no_fill").inc()
        metrics.TRADERS_DEMOTED.inc(2)
        metrics.BANKROLL.set(500.0)
        metrics.DAILY_PNL.set(-12.5)
        metrics.OPEN_POSITIONS.set(3)
        metrics.TOTAL_EXPOSURE.set(150.0)
        metrics.TRADING_HALTED.set(1)
        metrics.TRADER_SCORE.labels(trader_address="0xabc", rank="1").set(0.9)
        # Histogram
        metrics.EXIT_PNL.labels(reason="STOP_LOSS").observe(-5.0)


class TestServerAndAvailability:
    def test_prometheus_available_returns_bool(self):
        assert isinstance(prometheus_available(), bool)

    def test_start_server_returns_false_when_unavailable(self, monkeypatch):
        monkeypatch.setattr(metrics, "_PROM_AVAILABLE", False)
        # Must not raise on a missing dependency; returns False.
        assert start_metrics_server(9099) is False

    def test_start_server_starts_when_available(self, monkeypatch):
        called = {}

        def fake_start(port):
            called["port"] = port

        monkeypatch.setattr(metrics, "_PROM_AVAILABLE", True)
        monkeypatch.setattr(metrics, "start_http_server", fake_start, raising=False)
        assert start_metrics_server(9098) is True
        assert called["port"] == 9098
