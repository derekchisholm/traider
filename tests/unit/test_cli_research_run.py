"""`traider research run`: the command, the dry run, exit codes, and the real wiring
against the fake Schwab and Finnhub servers and moto."""

import io
import json
import logging
import os
import time
from datetime import timedelta

import boto3
import pytest
from moto import mock_aws

from tests.fakes.finnhub_server import API_KEY
from tests.fakes.research import NOW, golden_llm, market_day
from tests.fakes.schwab_server import APP_KEY, APP_SECRET
from tests.unit.test_research_store import TABLE, make_table
from tests.unit.test_settings_store import TABLE as SETTINGS_TABLE
from tests.unit.test_settings_store import make_table as make_settings_table
from traider import cli
from traider.alerts import LogAlerter
from traider.config import Config
from traider.research.events import FinnhubEvents
from traider.research.market import SchwabMarketData
from traider.research.run import RunDeps, RunOutcome
from traider.research.store import DynamoResearchStore, MemoryResearchStore
from traider.research.trail import LocalTrail, MemoryTrail, S3Trail
from traider.research.wiring import RESEARCH_SCHWAB_MAX_PER_MINUTE, SetupError, build_deps
from traider.schwab.oauth import REFRESH_TOKEN_LIFETIME_S
from traider.schwab.tokens import Grant
from traider.settings import Settings
from traider.settings_store import DynamoSettingsStore
from traider.timeutil import ManualClock

CONFIG = Config(research_table=TABLE)


def fake_build(store=None, llm=None):
    market, events = market_day()
    made = {"store": store or MemoryResearchStore(), "llm": llm or golden_llm()}

    async def build(config, http, *, dry_run, trail_dir, clock):
        made["dry_run"] = dry_run
        made["deps"] = RunDeps(
            store=made["store"],
            market=market,
            events=events,
            llm=made["llm"],
            trail=MemoryTrail,
            alerts=LogAlerter(),
            settings=Settings(),
            clock=ManualClock(NOW),
        )
        return made["deps"]

    return build, made


def test_the_command_exists_and_takes_only_premarket(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["research", "run", "--help"])
    assert exit_.value.code == 0
    assert "--dry-run" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_:
        cli.main(["research", "run", "--kind", "intraday"])
    assert exit_.value.code == 2


def test_it_needs_the_research_table(monkeypatch, capsys):
    for name in list(__import__("os").environ):
        if name.startswith("TRAIDER_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TRAIDER_SYMBOLS", "SPY")
    assert cli.main(["research", "run", "--kind", "premarket"]) == 2
    assert "TRAIDER_RESEARCH_TABLE is not set" in capsys.readouterr().err


async def test_a_dry_run_says_what_it_costs_prints_the_result_and_writes_nothing(tmp_path):
    build, made = fake_build()
    out = io.StringIO()
    code = await cli.research_run(
        CONFIG,
        out,
        kind="premarket",
        dry_run=True,
        force=False,
        trail_dir=str(tmp_path),
        build=build,
        now=NOW,
    )
    assert code == 0
    text = out.getvalue()
    notice, _, printed = text.partition("\n\n")
    assert notice.startswith("Dry run: real calls to Schwab, Finnhub and Claude")
    assert "cost real money" in notice and str(tmp_path) in notice
    report = json.loads(printed)
    assert report["status"] == "ok"
    assert report["posture"]["level"] == "reduced"
    assert [p["symbol"] for p in report["picks"]] == ["NVDA", "AMD", "PLTR"]
    assert made["dry_run"] is True
    assert made["store"].keys == set()


async def test_a_real_run_prints_one_line_and_returns_the_runs_exit_code(tmp_path):
    store = MemoryResearchStore()
    build, _ = fake_build(store)
    out = io.StringIO()
    code = await cli.research_run(
        CONFIG,
        out,
        kind="premarket",
        dry_run=False,
        force=False,
        trail_dir=str(tmp_path),
        build=build,
        now=NOW,
    )
    assert code == 0
    assert out.getvalue().startswith("research premarket ok: premarket-20261009T120000Z-")
    assert await store.acquire_lock("premarket", "someone-else", 3600, NOW)
    build, _ = fake_build(store)
    out = io.StringIO()
    code = await cli.research_run(
        CONFIG,
        out,
        kind="premarket",
        dry_run=False,
        force=True,
        trail_dir=str(tmp_path),
        build=build,
        now=NOW,
    )
    assert code == 2
    assert "locked" in out.getvalue()


async def test_a_run_that_cannot_start_says_why(tmp_path):
    async def broken(config, http, **kwargs):
        raise SetupError("no Finnhub key: set TRAIDER_FINNHUB_SECRET_ID or TRAIDER_FINNHUB_API_KEY")

    out = io.StringIO()
    code = await cli.research_run(
        CONFIG,
        out,
        kind="premarket",
        dry_run=False,
        force=False,
        trail_dir=str(tmp_path),
        build=broken,
    )
    assert code == 1
    assert "cannot start the research run: no Finnhub key" in out.getvalue()


# --- errors are scrubbed, and the model client is closed --------------------------------

SECRETISH = "arn:aws:secretsmanager:us-west-2:123456789012:secret:finnhub-AbCdEf"


async def test_an_unexpected_setup_failure_is_scrubbed_and_cannot_start(tmp_path):
    async def broken(config, http, **kwargs):
        raise RuntimeError(f"AccessDenied on {SECRETISH} api_key=fhsecretvalue0123456789")

    out = io.StringIO()
    code = await cli.research_run(
        CONFIG,
        out,
        kind="premarket",
        dry_run=False,
        force=False,
        trail_dir=str(tmp_path),
        build=broken,
    )
    assert code == 1
    text = out.getvalue()
    assert text.startswith("cannot start the research run: RuntimeError: AccessDenied")
    assert "123456789012" not in text and "fhsecretvalue" not in text


async def test_a_setup_error_is_scrubbed_too(tmp_path):
    async def broken(config, http, **kwargs):
        raise SetupError(f"cannot read {SECRETISH}")

    out = io.StringIO()
    code = await cli.research_run(
        CONFIG,
        out,
        kind="premarket",
        dry_run=False,
        force=False,
        trail_dir=str(tmp_path),
        build=broken,
    )
    assert code == 1
    assert "123456789012" not in out.getvalue()


class ClosingLLM:
    """The golden script, plus the ``aclose`` a real client has."""

    def __init__(self):
        self.inner = golden_llm()
        self.closed = 0

    async def create(self, **request):
        return await self.inner.create(**request)

    async def aclose(self):
        self.closed += 1


@pytest.mark.parametrize("dry_run", [False, True])
async def test_the_model_client_is_closed_after_the_run(tmp_path, dry_run):
    llm = ClosingLLM()
    build, _ = fake_build(llm=llm)
    code = await cli.research_run(
        CONFIG,
        io.StringIO(),
        kind="premarket",
        dry_run=dry_run,
        force=False,
        trail_dir=str(tmp_path),
        build=build,
        now=NOW,
    )
    assert code == 0
    assert llm.closed == 1


async def test_an_unexpected_run_error_closes_the_client_and_is_scrubbed(tmp_path, monkeypatch):
    async def explode(*args, **kwargs):
        raise RuntimeError(f"boom {SECRETISH}")

    monkeypatch.setattr(cli, "run_premarket", explode)
    llm = ClosingLLM()
    build, _ = fake_build(llm=llm)
    out = io.StringIO()
    code = await cli.research_run(
        CONFIG,
        out,
        kind="premarket",
        dry_run=False,
        force=False,
        trail_dir=str(tmp_path),
        build=build,
        now=NOW,
    )
    assert code == 1
    assert llm.closed == 1
    assert "research run failed: RuntimeError: boom" in out.getvalue()
    assert "123456789012" not in out.getvalue()


async def test_now_is_read_after_the_setup_right_before_the_run(tmp_path, monkeypatch):
    """The lock's lifetime counts from ``now``: a slow setup must not eat into it."""
    seen = {}
    clock = ManualClock(NOW)
    monkeypatch.setattr(cli, "SystemClock", lambda: clock)

    async def record(deps, now, **kwargs):
        seen["now"] = now
        return RunOutcome("ok", 0, "premarket-x")

    monkeypatch.setattr(cli, "run_premarket", record)
    build, _ = fake_build()

    async def slow_build(config, http, **kwargs):
        deps = await build(config, http, **kwargs)
        clock.advance(300)  # five minutes reading secrets and settings
        return deps

    code = await cli.research_run(
        CONFIG,
        io.StringIO(),
        kind="premarket",
        dry_run=False,
        force=False,
        trail_dir=str(tmp_path),
        build=slow_build,
    )
    assert code == 0
    assert seen["now"] == NOW + timedelta(seconds=300)


# --- the real wiring -------------------------------------------------------------------


@pytest.fixture
def aws_stack():
    """The research table and the three secrets, in moto."""
    with mock_aws():
        secrets = boto3.client("secretsmanager")
        app_arn = secrets.create_secret(
            Name="schwab-app",
            SecretString=json.dumps({"app_key": APP_KEY, "app_secret": APP_SECRET}),
        )["ARN"]
        token_arn = secrets.create_secret(Name="schwab-token")["ARN"]
        finnhub_arn = secrets.create_secret(
            Name="finnhub", SecretString=json.dumps({"api_key": API_KEY})
        )["ARN"]
        make_table()
        yield {"app": app_arn, "token": token_arn, "finnhub": finnhub_arn, "client": secrets}


def stack_config(aws_stack, **overrides) -> Config:
    fields = {
        "research_table": TABLE,
        "aws_region": "us-west-2",
        "schwab_app_secret_id": aws_stack["app"],
        "schwab_token_secret_id": aws_stack["token"],
        "finnhub_secret_id": aws_stack["finnhub"],
    }
    return Config(**(fields | overrides))


def sign_in(aws_stack, schwab, *, lifetime_s=REFRESH_TOKEN_LIFETIME_S, issued_ago_s=0):
    issued = int(time.time()) - issued_ago_s
    grant = Grant(schwab.seed_refresh_token(), issued, issued + lifetime_s, "g-test")
    aws_stack["client"].put_secret_value(SecretId=aws_stack["token"], SecretString=grant.to_json())


async def test_the_wiring_builds_real_adapters_from_the_configuration(aws_stack, tmp_path):
    import aiohttp

    async with aiohttp.ClientSession() as http:
        deps = await build_deps(
            stack_config(aws_stack),
            http,
            dry_run=False,
            trail_dir=tmp_path,
            clock=ManualClock(NOW),
            llm=golden_llm(),
        )
        assert isinstance(deps.store, DynamoResearchStore)
        assert isinstance(deps.market, SchwabMarketData)
        assert isinstance(deps.events, FinnhubEvents)
        assert isinstance(deps.trail("runs/x/"), LocalTrail)  # no bucket configured
        assert deps.settings == Settings.from_config(stack_config(aws_stack))
        assert API_KEY not in repr(deps.events)
        # Research shares Schwab's per-app quota with the bot: it takes a small share.
        assert deps.market._client._limiter._max == RESEARCH_SCHWAB_MAX_PER_MINUTE == 40


async def test_a_dry_run_keeps_its_trail_local_and_sends_no_alert(aws_stack, tmp_path):
    import aiohttp

    config = stack_config(aws_stack, research_bucket="trail", alert_topic_arn="arn:aws:sns:x")
    async with aiohttp.ClientSession() as http:
        dry = await build_deps(
            config,
            http,
            dry_run=True,
            trail_dir=tmp_path,
            clock=ManualClock(NOW),
            llm=golden_llm(),
        )
        real = await build_deps(
            config,
            http,
            dry_run=False,
            trail_dir=tmp_path,
            clock=ManualClock(NOW),
            llm=golden_llm(),
        )
    assert isinstance(dry.trail("runs/x/"), LocalTrail)
    assert isinstance(real.trail("runs/x/"), S3Trail)
    assert isinstance(dry.alerts, LogAlerter)
    assert not isinstance(real.alerts, LogAlerter)


async def test_without_a_finnhub_key_the_run_cannot_start(aws_stack, tmp_path):
    import aiohttp

    async with aiohttp.ClientSession() as http:
        with pytest.raises(SetupError, match="no Finnhub key"):
            await build_deps(
                stack_config(aws_stack, finnhub_secret_id=None),
                http,
                dry_run=False,
                trail_dir=tmp_path,
                clock=ManualClock(NOW),
                llm=golden_llm(),
            )
        empty = aws_stack["client"].create_secret(Name="empty")["ARN"]
        with pytest.raises(SetupError, match="no value yet"):
            await build_deps(
                stack_config(aws_stack, finnhub_secret_id=empty),
                http,
                dry_run=False,
                trail_dir=tmp_path,
                clock=ManualClock(NOW),
                llm=golden_llm(),
            )


async def test_a_local_key_is_used_without_reading_the_secret(aws_stack, tmp_path):
    import aiohttp

    config = stack_config(aws_stack, finnhub_secret_id=None, finnhub_api_key=API_KEY)
    async with aiohttp.ClientSession() as http:
        deps = await build_deps(
            config,
            http,
            dry_run=False,
            trail_dir=tmp_path,
            clock=ManualClock(NOW),
            llm=golden_llm(),
        )
    assert isinstance(deps.events, FinnhubEvents)


BAD_KEY = "fh secret 0123456789abcdef"  # a space: refused, and never echoed


@pytest.mark.parametrize("where", ["secret", "environment"])
async def test_a_bad_finnhub_key_stops_the_run_before_it_starts_without_showing_it(
    aws_stack, tmp_path, where, caplog
):
    if where == "secret":
        bad = aws_stack["client"].create_secret(
            Name="bad", SecretString=json.dumps({"api_key": BAD_KEY})
        )["ARN"]
        config = stack_config(aws_stack, finnhub_secret_id=bad)
    else:
        config = stack_config(aws_stack, finnhub_secret_id=None, finnhub_api_key=BAD_KEY)
    out = io.StringIO()

    async def build(config, http, **kwargs):
        return await build_deps(config, http, llm=golden_llm(), **kwargs)

    for dry_run in (False, True):
        code = await cli.research_run(
            config,
            out,
            kind="premarket",
            dry_run=dry_run,
            force=False,
            trail_dir=str(tmp_path),
            build=build,
        )
        assert code == 1
    assert "cannot start the research run: the Finnhub API key has invalid characters" in (
        out.getvalue()
    )
    assert "secret 0123" not in out.getvalue() + caplog.text
    assert boto3.resource("dynamodb").Table(TABLE).scan()["Items"] == []  # no META, no lock


async def test_through_the_real_adapters_a_closed_market_writes_nothing(
    aws_stack, schwab, finnhub, tmp_path
):
    sign_in(aws_stack, schwab)
    schwab.market_open = False
    out = io.StringIO()

    async def build(config, http, **kwargs):
        return await build_deps(
            config,
            http,
            schwab_base_url=schwab.base_url,
            token_url=schwab.token_url,
            finnhub_base_url=finnhub.base_url,
            llm=golden_llm(),
            **kwargs,
        )

    code = await cli.research_run(
        stack_config(aws_stack),
        out,
        kind="premarket",
        dry_run=False,
        force=False,
        trail_dir=str(tmp_path),
        build=build,
    )
    assert code == 0
    assert "closed" in out.getvalue()
    items = boto3.resource("dynamodb").Table(TABLE).scan()["Items"]
    assert items == []  # the lock came and went


async def test_through_the_real_adapters_an_expired_sign_in_fails_the_run(
    aws_stack, schwab, finnhub, tmp_path
):
    sign_in(aws_stack, schwab, lifetime_s=60, issued_ago_s=3600)
    out = io.StringIO()

    async def build(config, http, **kwargs):
        return await build_deps(
            config,
            http,
            schwab_base_url=schwab.base_url,
            token_url=schwab.token_url,
            finnhub_base_url=finnhub.base_url,
            llm=golden_llm(),
            **kwargs,
        )

    code = await cli.research_run(
        stack_config(aws_stack),
        out,
        kind="premarket",
        dry_run=False,
        force=False,
        trail_dir=str(tmp_path),
        build=build,
    )
    assert code == 1
    table = boto3.resource("dynamodb").Table(TABLE)
    (meta,) = [i for i in table.scan()["Items"] if i["sk"] == "META"]
    body = json.loads(meta["body"])
    assert body["status"] == "failed"
    assert "no Schwab login" in body["error"]
    assert not [i for i in table.scan()["Items"] if i["sk"].startswith("POSTURE#")]


def fake_adapters(schwab, finnhub):
    async def build(config, http, **kwargs):
        return await build_deps(
            config,
            http,
            schwab_base_url=schwab.base_url,
            token_url=schwab.token_url,
            finnhub_base_url=finnhub.base_url,
            llm=golden_llm(),
            **kwargs,
        )

    return build


@pytest.mark.parametrize("market_open", [False, True])
async def test_a_dry_run_through_the_real_adapters_shows_no_secret_and_writes_nothing(
    aws_stack, schwab, finnhub, tmp_path, caplog, capsys, market_open
):
    # botocore's own DEBUG lines print whole API responses, secret values included.
    # setup_logging pins it at WARNING whatever the log level, and so does this test.
    for noisy in ("botocore", "boto3"):
        caplog.set_level(logging.WARNING, logger=noisy)
    caplog.set_level(logging.DEBUG)  # last: it also sets the capture handler's level
    sign_in(aws_stack, schwab)
    refresh = json.loads(
        aws_stack["client"].get_secret_value(SecretId=aws_stack["token"])["SecretString"]
    )["refresh_token"]
    schwab.market_open = market_open
    out = io.StringIO()

    code = await cli.research_run(
        stack_config(aws_stack, research_bucket="trail", alert_topic_arn="arn:aws:sns:x"),
        out,
        kind="premarket",
        dry_run=True,
        force=False,
        trail_dir=str(tmp_path),
        build=fake_adapters(schwab, finnhub),
    )
    _, _, printed = out.getvalue().partition("\n\n")
    status = json.loads(printed)["status"]
    if market_open:
        # The key really went out: Finnhub was asked, with the key in its header.
        assert any(r["headers"].get("X-Finnhub-Token") == API_KEY for r in finnhub.requests)
    else:
        assert (code, status) == (0, "closed")
        assert finnhub.requests == []
    captured = capsys.readouterr()
    shown = out.getvalue() + captured.out + captured.err + caplog.text
    for secret in (API_KEY, APP_KEY, APP_SECRET, refresh):
        assert secret not in shown
    assert boto3.resource("dynamodb").Table(TABLE).scan()["Items"] == []


class BrokenCloseLLM(ClosingLLM):
    async def aclose(self):
        raise RuntimeError(f"close failed on {SECRETISH}")


async def test_a_client_that_fails_to_close_does_not_change_the_result(tmp_path, caplog):
    build, _ = fake_build(llm=BrokenCloseLLM())
    out = io.StringIO()
    code = await cli.research_run(
        CONFIG,
        out,
        kind="premarket",
        dry_run=False,
        force=False,
        trail_dir=str(tmp_path),
        build=build,
        now=NOW,
    )
    assert code == 0
    assert out.getvalue().startswith("research premarket ok:")
    assert "could not close the model client" in caplog.text
    assert "123456789012" not in caplog.text


# --- the versioned settings, read once -------------------------------------------------


async def write_settings(settings: Settings) -> None:
    store = DynamoSettingsStore(boto3.resource("dynamodb").Table(SETTINGS_TABLE))
    await store.write(settings, expected_version=0, author="test", note="", now=NOW)


async def test_the_newest_settings_version_is_what_the_run_uses(
    aws_stack, schwab, finnhub, tmp_path
):
    make_settings_table()
    config = stack_config(aws_stack, settings_table=SETTINGS_TABLE)
    base = Settings.from_config(config)
    await write_settings(
        base.model_copy(
            update={"research_jobs": base.research_jobs.model_copy(update={"enabled": False})}
        )
    )
    out = io.StringIO()
    code = await cli.research_run(
        config,
        out,
        kind="premarket",
        dry_run=False,
        force=False,
        trail_dir=str(tmp_path),
        build=fake_adapters(schwab, finnhub),
    )
    assert code == 0
    assert out.getvalue().startswith("research premarket disabled:")
    assert boto3.resource("dynamodb").Table(TABLE).scan()["Items"] == []


async def test_an_invalid_newest_settings_version_stops_the_run_before_it_starts(
    aws_stack, schwab, finnhub, tmp_path
):
    settings_table = make_settings_table()
    config = stack_config(aws_stack, settings_table=SETTINGS_TABLE)
    await write_settings(Settings.from_config(config))
    settings_table.put_item(
        Item={"pk": "SETTINGS", "sk": "V#000000002", "body": "{not json", "at": NOW.isoformat()}
    )
    out = io.StringIO()
    code = await cli.research_run(
        config,
        out,
        kind="premarket",
        dry_run=False,
        force=False,
        trail_dir=str(tmp_path),
        build=fake_adapters(schwab, finnhub),
    )
    assert code == 1
    assert "cannot start the research run: the newest settings version is invalid" in (
        out.getvalue()
    )
    assert boto3.resource("dynamodb").Table(TABLE).scan()["Items"] == []


# --- main: a dry run's stdout is the notice and the JSON, nothing else -----------------


def test_a_dry_runs_stdout_is_only_the_notice_and_the_report(monkeypatch, capsys, tmp_path):
    for name in list(os.environ):
        if name.startswith("TRAIDER_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TRAIDER_SYMBOLS", "SPY")
    monkeypatch.setenv("TRAIDER_RESEARCH_TABLE", TABLE)
    monkeypatch.setattr(cli, "SystemClock", lambda: ManualClock(NOW))
    build, _ = fake_build()

    async def noisy_build(config, http, **kwargs):
        chatter = logging.getLogger("traider.research.run")
        chatter.warning("a warning the run logs")
        chatter.info("an info line the run logs")
        return await build(config, http, **kwargs)

    monkeypatch.setitem(cli.research_run.__kwdefaults__, "build", noisy_build)
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        root.handlers[:] = []  # a fresh process: main sets up the logging itself
        code = cli.main(
            ["research", "run", "--kind", "premarket", "--dry-run", "--trail-dir", str(tmp_path)]
        )
    finally:
        root.handlers[:], root.level = handlers, level
    assert code == 0
    captured = capsys.readouterr()
    notice, _, printed = captured.out.partition("\n\n")
    assert notice + "\n\n" == cli.DRY_RUN_NOTICE.format(where=tmp_path)
    assert json.loads(printed)["status"] == "ok"  # the rest of stdout is one JSON document
    assert "a warning the run logs" not in captured.out
    assert "an info line the run logs" not in captured.out
    assert "a warning the run logs" in captured.err  # warnings go to stderr
    assert "an info line the run logs" not in captured.err


# --- C2a: the scorecard --------------------------------------------------------------------


async def test_the_scorecard_kind_runs_the_scorecard(tmp_path):
    store = MemoryResearchStore()
    build, _ = fake_build(store)
    out = io.StringIO()
    code = await cli.research_run(
        CONFIG,
        out,
        kind="scorecard",
        dry_run=False,
        force=False,
        trail_dir=str(tmp_path),
        build=build,
        now=NOW,
    )
    assert code == 0
    assert out.getvalue().startswith("research scorecard ok: scorecard-20261009T120000Z-")
    assert ("SCORE#2026-10-09", "SUMMARY") in store.keys


async def test_a_scorecard_dry_run_says_it_calls_no_model(tmp_path):
    build, made = fake_build()
    out = io.StringIO()
    code = await cli.research_run(
        CONFIG,
        out,
        kind="scorecard",
        dry_run=True,
        force=False,
        trail_dir=str(tmp_path),
        build=build,
        now=NOW,
    )
    assert code == 0
    notice, _, printed = out.getvalue().partition("\n\n")
    assert notice.startswith("Dry run: real calls to Schwab (daily bars)")
    assert "no model is called" in notice and "cost real money" not in notice
    assert json.loads(printed)["summary"]["picks"] == 0
    assert made["store"].keys == set()


def test_the_scorecard_kind_parses():
    args = cli._parser().parse_args(["research", "run", "--kind", "scorecard", "--dry-run"])
    assert (args.kind, args.dry_run) == ("scorecard", True)
