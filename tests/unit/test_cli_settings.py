# tests/unit/test_cli_settings.py
"""`traider settings`: read and change the versioned settings from a terminal."""

import io
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from traider import cli
from traider.config import Config
from traider.settings import Settings
from traider.settings_store import MemorySettingsStore

T0 = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


async def seeded() -> MemorySettingsStore:
    store = MemorySettingsStore()
    await store.write(
        Settings.from_config(Config(symbols=("SPY",))),
        expected_version=0,
        author="bootstrap",
        note="",
        now=T0,
    )
    return store


def invalid_body() -> dict:
    return Settings.from_config(Config(symbols=("SPY",))).model_dump(mode="json") | {
        "strategy": "nope"
    }


async def test_show_prints_the_current_version_as_json():
    out = io.StringIO()
    assert await cli.settings_show(await seeded(), out) == 0
    text = out.getvalue()
    assert text.startswith("version 1 by bootstrap")
    body = json.loads(text.split("\n", 1)[1])
    assert body["pinned_symbols"] == ["SPY"]


async def test_show_on_an_empty_table_says_so():
    out = io.StringIO()
    assert await cli.settings_show(MemorySettingsStore(), out) == 1
    assert "no settings" in out.getvalue()


async def test_show_reports_an_invalid_newest_version():
    store = await seeded()
    store.put_raw(2, invalid_body())
    out = io.StringIO()
    assert await cli.settings_show(store, out) == 1
    assert out.getvalue().startswith("newest version 2 is invalid:")


async def test_apply_repairs_over_an_invalid_newest_version(tmp_path):
    store = await seeded()
    store.put_raw(2, invalid_body())
    body = Settings.from_config(Config(symbols=("SPY",))).model_dump(mode="json")
    path = tmp_path / "s.json"
    path.write_text(json.dumps(body))
    out = io.StringIO()
    assert await cli.settings_apply(store, str(path), out, note="repair", now=T0) == 0
    latest = await store.latest()
    assert latest.version == 3 and latest.note == "repair"
    assert "wrote version 3" in out.getvalue()


async def test_apply_writes_a_new_version_and_prints_the_diff(tmp_path):
    store = await seeded()
    body = (await store.latest()).settings.model_dump(mode="json")
    body["risk"]["max_order_usd"] = "250"
    path = tmp_path / "s.json"
    path.write_text(json.dumps(body))
    out = io.StringIO()
    assert await cli.settings_apply(store, str(path), out, note="smaller", now=T0) == 0
    latest = await store.latest()
    assert latest.version == 2 and latest.author == "cli" and latest.note == "smaller"
    assert latest.settings.risk.max_order_usd == Decimal(250)
    assert "risk.max_order_usd: 500 -> 250" in out.getvalue()


async def test_apply_refuses_an_invalid_file_and_writes_nothing(tmp_path):
    store = await seeded()
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"strategy": "nope"}))
    out = io.StringIO()
    assert await cli.settings_apply(store, str(path), out, note="", now=T0) == 1
    assert (await store.latest()).version == 1
    assert "invalid" in out.getvalue()


async def test_history_lists_versions_newest_first():
    store = await seeded()
    current = await store.latest()
    await store.write(current.settings, expected_version=1, author="cli", note="again", now=T0)
    out = io.StringIO()
    assert await cli.settings_history(store, out) == 0
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("2 ") and "cli" in lines[0] and "again" in lines[0]
    assert lines[1].startswith("1 ")


def test_settings_needs_a_table(monkeypatch, capsys):
    monkeypatch.setenv("TRAIDER_SYMBOLS", "SPY")
    monkeypatch.delenv("TRAIDER_SETTINGS_TABLE", raising=False)
    assert cli.main(["settings", "show"]) == 2
    assert "TRAIDER_SETTINGS_TABLE is not set" in capsys.readouterr().err


def test_settings_help_works():
    with pytest.raises(SystemExit) as exit_:
        cli.main(["settings", "--help"])
    assert exit_.value.code == 0
