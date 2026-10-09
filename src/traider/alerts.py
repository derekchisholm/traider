"""Operator alerts over SNS.

Imported by the Lambda functions, so this module may only use the standard
library (boto3 clients are passed in).
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Protocol

log = logging.getLogger(__name__)

SUBJECT_PREFIX = "[traider] "
_SNS_SUBJECT_MAX = 100


class _Clock(Protocol):
    def now(self) -> datetime: ...


class Alerter(Protocol):
    async def send(self, key: str, subject: str, message: str) -> None:
        """Send an alert. ``key`` identifies the condition, for rate limiting."""
        ...


_URL_QUERY = re.compile(r"(https?://[^\s?#]+)[?#]\S*")


def _for_log(message: str) -> str:
    """Alert text as it may appear in logs: links lose their query string, because the
    sign-in link carries a key and logs are read by more people than alerts are."""
    return _URL_QUERY.sub(r"\1?[redacted]", message)


def publish(client: Any, topic_arn: str, subject: str, message: str) -> None:
    """Publish one alert. SNS subjects must be a single line of at most 100 characters."""
    flat = " ".join(subject.split())
    client.publish(
        TopicArn=topic_arn,
        Subject=(SUBJECT_PREFIX + flat)[:_SNS_SUBJECT_MAX],
        Message=message,
    )


class LogAlerter:
    """Writes alerts to the log. Used when no SNS topic is configured, and in tests."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, key: str, subject: str, message: str) -> None:
        self.sent.append((key, subject, message))
        log.warning("ALERT %s: %s | %s", key, subject, _for_log(message))


class SnsAlerter:
    """Publishes to SNS, at most once per ``min_interval_s`` for the same key.

    Alerting must never take the bot down, so failures are logged and swallowed.
    """

    def __init__(
        self, topic_arn: str, client: Any, clock: _Clock, *, min_interval_s: float = 900.0
    ) -> None:
        self._topic_arn = topic_arn
        self._client = client
        self._clock = clock
        self._min_interval_s = min_interval_s
        self._last_sent: dict[str, datetime] = {}

    async def send(self, key: str, subject: str, message: str) -> None:
        now = self._clock.now()
        last = self._last_sent.get(key)
        if last is not None and (now - last).total_seconds() < self._min_interval_s:
            return
        log.warning("ALERT %s: %s | %s", key, subject, _for_log(message))
        try:
            await asyncio.to_thread(publish, self._client, self._topic_arn, subject, message)
        except Exception:
            log.exception("could not publish alert %s", key)
            return
        self._last_sent[key] = now
