"""One JSON object per log line, which is what CloudWatch Logs Insights wants."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Library chatter is not worth paying to store. The research run's model client
    # (anthropic, over httpx and httpcore) logs every request at INFO or DEBUG; the bot
    # does not use those libraries, so for it nothing changes.
    for noisy in (
        "botocore",
        "boto3",
        "urllib3",
        "aiohttp.access",
        "asyncio",
        "anthropic",
        "httpx",
        "httpcore",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)
