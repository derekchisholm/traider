# tests/unit/test_cli_settings.py
"""`traider settings`: read and change the versioned settings from a terminal."""

import io
import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError

from traider import cli
from traider.config import Config
from traider.settings import Settings
from traider.settings_store import MemorySettingsStore, SettingsConflict

T0 = datetime(2026, 10, 9, 13, 0, tzinfo=UTC)


class ConflictingStore(MemorySettingsStore):
    async def write(self, *args, **kwargs):
        raise SettingsConflict("version 2 already exists")


class DeniedStore(MemorySettingsStore):
    async def latest(self):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "Query")


def settings_file(tmp_path) -> str:
    body = Settings.from_config(Config(symbols=("SPY",))).model_dump(mode="json")
    path = tmp_path / "s.json"
    path.write_text(json.dumps(body))
    return str(path)


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


async def test_show_prints_only_the_json_body_on_stdout():
    out, err = io.StringIO(), io.StringIO()
    assert await cli.settings_show(await seeded(), out, err) == 0
    body = json.loads(out.getvalue())  # the whole of stdout is JSON
    assert body["pinned_symbols"] == ["SPY"]
    assert err.getvalue().startswith("version 1 by bootstrap at ")


async def test_show_on_an_empty_table_says_so_on_stderr():
    out, err = io.StringIO(), io.StringIO()
    assert await cli.settings_show(MemorySettingsStore(), out, err) == 1
    assert out.getvalue() == ""
    assert "no settings" in err.getvalue()


async def test_show_output_applies_as_a_new_version_with_no_changes(tmp_path):
    store = await seeded()
    out, err = io.StringIO(), io.StringIO()
    assert await cli.settings_show(store, out, err) == 0
    saved = tmp_path / "settings.json"
    saved.write_text(out.getvalue())
    applied = io.StringIO()
    assert await cli.settings_apply(store, str(saved), applied, note="same", now=T0) == 0
    latest = await store.latest()
    assert latest is not None
    assert (latest.version, latest.diff) == (2, {})
    assert applied.getvalue() == "wrote version 2\n"


async def test_show_version_prints_that_versions_body_and_can_be_rolled_back_to(tmp_path):
    store = await seeded()
    body = (await store.latest()).settings.model_dump(mode="json")
    body["risk"]["max_order_usd"] = "250"
    newer = tmp_path / "new.json"
    newer.write_text(json.dumps(body))
    assert await cli.settings_apply(store, str(newer), io.StringIO(), note="", now=T0) == 0
    out, err = io.StringIO(), io.StringIO()
    assert await cli.settings_show(store, out, err, version=1) == 0
    assert json.loads(out.getvalue())["risk"]["max_order_usd"] == "500"
    assert err.getvalue().startswith("version 1 by bootstrap")
    old = tmp_path / "old.json"
    old.write_text(out.getvalue())
    applied = io.StringIO()
    assert await cli.settings_apply(store, str(old), applied, note="rollback", now=T0) == 0
    latest = await store.latest()
    assert latest is not None and latest.version == 3
    assert latest.settings.risk.max_order_usd == Decimal(500)
    assert "risk.max_order_usd: 250 -> 500" in applied.getvalue()


async def test_show_of_a_missing_version_returns_1_with_a_message():
    out, err = io.StringIO(), io.StringIO()
    assert await cli.settings_show(await seeded(), out, err, version=7) == 1
    assert out.getvalue() == ""
    assert "no settings version 7" in err.getvalue()


def test_show_version_is_a_command_line_option(monkeypatch, capsys):
    monkeypatch.setenv("TRAIDER_SYMBOLS", "SPY")
    monkeypatch.setenv("TRAIDER_SETTINGS_TABLE", "settings")
    store = MemorySettingsStore()
    monkeypatch.setattr(cli, "_settings_store", lambda config: store)
    assert cli.main(["settings", "show", "--version", "3"]) == 1
    assert "no settings version 3" in capsys.readouterr().err


async def test_show_reports_an_invalid_newest_version():
    store = await seeded()
    store.put_raw(2, invalid_body())
    out, err = io.StringIO(), io.StringIO()
    assert await cli.settings_show(store, out, err) == 1
    assert out.getvalue() == ""
    assert err.getvalue().startswith("settings version 2 is invalid:")
    assert "newest version" not in err.getvalue()


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


async def test_apply_refuses_a_newest_item_with_no_readable_version(tmp_path):
    store = await seeded()
    store.put_raw(2, invalid_body())
    # Corrupt both the version field and the sort key, so no version number can be read.
    store._items[2]["version"] = "x"
    store._items[2]["sk"] = "V#bad"
    out = io.StringIO()
    assert await cli.settings_apply(store, settings_file(tmp_path), out, note="", now=T0) == 1
    assert out.getvalue() == (
        "newest settings item is damaged and has no readable version; "
        "delete it from the settings table, then apply again\n"
    )
    assert set(store._items) == {1, 2}


async def test_apply_reports_a_conflict_and_returns_1(tmp_path):
    out = io.StringIO()
    store = ConflictingStore()
    assert await cli.settings_apply(store, settings_file(tmp_path), out, note="", now=T0) == 1
    assert "not written" in out.getvalue()


def test_settings_reports_an_aws_error_in_one_line(monkeypatch, capsys):
    monkeypatch.setenv("TRAIDER_SYMBOLS", "SPY")
    monkeypatch.setenv("TRAIDER_SETTINGS_TABLE", "settings")
    monkeypatch.setattr(cli, "_settings_store", lambda config: DeniedStore())
    assert cli.main(["settings", "show"]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "AccessDeniedException" in err


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
