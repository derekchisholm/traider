"""Logging setup: JSON lines, and library chatter kept out of paid-for logs."""

import json
import logging

import pytest

from traider.log import JsonFormatter, setup_logging

QUIET = ("botocore", "boto3", "urllib3", "aiohttp.access", "asyncio", "anthropic", "httpx",
         "httpcore")  # fmt: skip


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    levels = {name: logging.getLogger(name).level for name in QUIET}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, old in levels.items():
        logging.getLogger(name).setLevel(old)


@pytest.mark.parametrize("level", ["DEBUG", "INFO"])
def test_library_loggers_are_held_at_warning_whatever_the_level(restore_logging, level):
    setup_logging(level)
    assert logging.getLogger().level == getattr(logging, level)
    for name in QUIET:
        assert logging.getLogger(name).level == logging.WARNING, name
    # The model client's request lines (httpx) and its debug output stay out of the logs.
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert not logging.getLogger("anthropic._base_client").isEnabledFor(logging.DEBUG)
    assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)


def test_traider_loggers_still_follow_the_level(restore_logging):
    setup_logging("DEBUG")
    assert logging.getLogger("traider.engine").isEnabledFor(logging.DEBUG)
    assert logging.getLogger("traider.research.run").isEnabledFor(logging.DEBUG)


def test_each_line_is_one_json_object():
    record = logging.LogRecord("traider.x", logging.INFO, __file__, 1, "hello %s", ("you",), None)
    entry = json.loads(JsonFormatter().format(record))
    assert (entry["level"], entry["logger"], entry["message"]) == ("INFO", "traider.x", "hello you")
