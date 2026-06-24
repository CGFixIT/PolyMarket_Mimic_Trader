"""Tests for utils/logger.py — JSON formatter, logger setup, and the M17 log_event helper."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler

import pytest

from polymarket_copier.utils.logger import JsonFormatter, log_event, setup_logger


def _make_record(msg="hello %s", args=("world",), level=logging.INFO):
    return logging.LogRecord(
        name="x",
        level=level,
        pathname="m.py",
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


class TestJsonFormatter:
    def test_emits_base_fields(self):
        out = JsonFormatter(datefmt="%Y-%m-%dT%H:%M:%S").format(_make_record())
        parsed = json.loads(out)
        assert set(parsed.keys()) == {"time", "level", "module", "msg"}
        assert parsed["level"] == "INFO"
        # %-arg substitution happens via record.getMessage()
        assert parsed["msg"] == "hello world"
        assert "data" not in parsed

    def test_includes_data_attribute_when_present(self):
        record = _make_record(msg="evt", args=())
        record.data = {"order_id": "abc", "size": 100}
        parsed = json.loads(JsonFormatter().format(record))
        assert parsed["data"] == {"order_id": "abc", "size": 100}

    def test_omits_data_when_absent(self):
        parsed = json.loads(JsonFormatter().format(_make_record(msg="evt", args=())))
        assert "data" not in parsed


class TestSetupLogger:
    @pytest.fixture(autouse=True)
    def _cleanup(self):
        created: list[logging.Logger] = []
        self._created = created
        yield
        for lg in created:
            for h in list(lg.handlers):
                h.close()
                lg.removeHandler(h)

    def _setup(self, **kw):
        lg = setup_logger(**kw)
        self._created.append(lg)
        return lg

    def test_idempotent(self):
        lg1 = self._setup(name="test_idem", log_file="")
        n = len(lg1.handlers)
        lg2 = self._setup(name="test_idem", log_file="")
        assert lg2 is lg1
        assert len(lg2.handlers) == n  # no duplicate handlers

    def test_level_parsing_and_invalid_fallback(self):
        assert self._setup(name="test_lvl_debug", level="DEBUG", log_file="").level == logging.DEBUG
        # An unrecognized level falls back to INFO (getattr default arg).
        assert self._setup(name="test_lvl_bogus", level="BOGUS", log_file="").level == logging.INFO

    def test_creates_file_handler_and_parent_dir(self, tmp_path):
        log_file = tmp_path / "nested" / "sub" / "trades.log"
        lg = self._setup(name="test_filehandler", log_file=str(log_file))
        assert log_file.parent.is_dir()  # mkdir(parents=True) ran
        kinds = {type(h) for h in lg.handlers}
        assert RotatingFileHandler in kinds
        assert any(isinstance(h, logging.StreamHandler) for h in lg.handlers)
        lg.info("written")
        assert log_file.exists()

    def test_without_log_file_console_only(self):
        lg = self._setup(name="test_console_only", log_file="")
        assert len(lg.handlers) == 1
        assert isinstance(lg.handlers[0], logging.StreamHandler)
        assert not isinstance(lg.handlers[0], RotatingFileHandler)


class TestLogEvent:
    """M17: log_event routes a structured payload through the JSON 'data' channel."""

    def _capture(self):
        records: list[logging.LogRecord] = []

        class _Cap(logging.Handler):
            def emit(self, record):
                records.append(record)

        lg = logging.getLogger("test_log_event")
        lg.handlers.clear()
        lg.setLevel(logging.DEBUG)
        lg.addHandler(_Cap())
        lg.propagate = False
        return lg, records

    def test_payload_carries_event_and_fields(self):
        lg, records = self._capture()
        log_event(lg, "position_opened", position_id="p1", size_usdc=50.0)
        assert len(records) == 1
        data = records[0].data
        assert data == {"event": "position_opened", "position_id": "p1", "size_usdc": 50.0}

    def test_synthesizes_human_message_when_msg_omitted(self):
        lg, records = self._capture()
        log_event(lg, "trader_demoted", trader="0xabc", sample=42)
        # Compact "event=<name> k=v ..." string for console readability.
        assert records[0].getMessage() == "event=trader_demoted trader=0xabc sample=42"

    def test_explicit_msg_preserved(self):
        lg, records = self._capture()
        log_event(lg, "position_closed", msg="closed it", pnl=1.5)
        assert records[0].getMessage() == "closed it"
        assert records[0].data["event"] == "position_closed"
        assert records[0].data["pnl"] == 1.5

    def test_level_is_respected(self):
        lg, records = self._capture()
        log_event(lg, "circuit_breaker_tripped", level=logging.WARNING, reason="daily_loss")
        assert records[0].levelno == logging.WARNING

    def test_event_only_no_fields(self):
        lg, records = self._capture()
        log_event(lg, "heartbeat")
        assert records[0].getMessage() == "event=heartbeat"
        assert records[0].data == {"event": "heartbeat"}

    def test_data_is_json_serializable(self):
        # The whole point is downstream machine parsing — payload must round-trip.
        lg, records = self._capture()
        log_event(lg, "position_closed", reason="TAKE_PROFIT", pnl=2.5, shares=100.0)
        formatted = JsonFormatter().format(records[0])
        parsed = json.loads(formatted)
        assert parsed["data"]["event"] == "position_closed"
        assert parsed["data"]["reason"] == "TAKE_PROFIT"
