"""Structured JSON logging for the copy trading bot."""

from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "time": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "module": record.module,
            "msg": record.getMessage(),
        }
        if hasattr(record, "data"):
            log_entry["data"] = record.data
        return json.dumps(log_entry)


def log_event(
    logger: logging.Logger,
    event: str,
    level: int = logging.INFO,
    msg: str | None = None,
    **fields,
) -> None:
    """Emit a machine-readable structured event through the JSON 'data' channel (M17).

    `event` is a stable snake_case name (e.g. "position_opened"). `**fields` become
    the structured payload, serialized under the top-level "data" key by JsonFormatter
    (so downstream tooling filters on `.data.event`). A human-readable `msg` is
    optional; when omitted a compact "event=<name> k=v ..." string is synthesized so
    console output stays readable.

    Purely additive: existing printf-style logger.info(...) calls are unaffected. Pass
    only JSON-serializable primitives as field values; avoid the reserved names
    time/level/module/msg/data (they are owned by the formatter).
    """
    payload = {"event": event, **fields}
    if msg is None:
        kv = " ".join(f"{k}={v}" for k, v in fields.items())
        msg = f"event={event}" + (f" {kv}" if kv else "")
    logger.log(level, msg, extra={"data": payload})


def setup_logger(
    name: str = "polymarket_copier",
    level: str = "INFO",
    log_file: str = "trades.log",
) -> logging.Logger:
    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    formatter = JsonFormatter(datefmt="%Y-%m-%dT%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=10_000_000,
            backupCount=5,
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
