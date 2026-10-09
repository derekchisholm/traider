import json
import logging
import os
import time

from traider import health
from traider.log import JsonFormatter


def test_fresh_heartbeat_is_alive(tmp_path):
    path = tmp_path / "heartbeat"
    path.write_text("x")
    assert health.is_alive(path, max_age_s=60) is True


def test_old_heartbeat_is_not_alive(tmp_path):
    path = tmp_path / "heartbeat"
    path.write_text("x")
    two_minutes_ago = time.time() - 120
    os.utime(path, (two_minutes_ago, two_minutes_ago))
    assert health.is_alive(path, max_age_s=60) is False


def test_missing_heartbeat_is_not_alive(tmp_path):
    assert health.is_alive(tmp_path / "nope", max_age_s=60) is False


def test_health_command_exit_code_follows_the_heartbeat(tmp_path, monkeypatch):
    path = tmp_path / "heartbeat"
    monkeypatch.setenv("TRAIDER_HEARTBEAT_FILE", str(path))
    assert health.main() == 1
    path.write_text("x")
    assert health.main() == 0


def record(message, *args, level=logging.INFO, **extra) -> logging.LogRecord:
    made = logging.LogRecord("traider.engine", level, __file__, 1, message, args, None)
    for key, value in extra.items():
        setattr(made, key, value)
    return made


def test_log_lines_are_single_line_json():
    line = JsonFormatter().format(record("order %s placed", "1001"))
    assert "\n" not in line
    parsed = json.loads(line)
    assert parsed["message"] == "order 1001 placed"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "traider.engine"
    assert "time" in parsed


def test_log_lines_include_the_traceback_on_errors():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        made = logging.LogRecord(
            "traider", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
        )
    parsed = json.loads(JsonFormatter().format(made))
    assert "ValueError: boom" in parsed["exception"]
    assert "\n" not in JsonFormatter().format(made)
