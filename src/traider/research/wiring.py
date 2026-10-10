"""Build a research run's real collaborators from the configuration.

Research signs in to nothing. It builds its own ``TokenManager`` on the bot's stored
Schwab sign-in: it refreshes access tokens and saves a rotated refresh token with the
same newer-wins rule as the bot, and an expired sign-in fails the run.

The settings are read once, here. A dry run writes its trail locally and never alerts
over SNS. The model client is built last, so nothing that can fail afterwards leaves it
open; whoever runs the deps closes it with ``close_llm``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import aiohttp

from traider import app
from traider.alerts import Alerter, LogAlerter, SnsAlerter
from traider.config import Config
from traider.research.events import (
    FINNHUB_BASE,
    EventsUnavailable,
    FinnhubEvents,
    finnhub_key_from_secret,
)
from traider.research.llm import LLM, MantleLLM
from traider.research.market import SchwabMarketData
from traider.research.run import RunDeps
from traider.research.scrub import scrub
from traider.research.store import DynamoResearchStore
from traider.research.trail import LocalTrail, S3Trail, Trail
from traider.schwab.client import API_BASE, SchwabClient
from traider.schwab.oauth import TOKEN_URL
from traider.schwab.tokens import TokenManager
from traider.settings import Settings
from traider.settings_store import DynamoSettingsStore, SettingsInvalid
from traider.timeutil import Clock

log = logging.getLogger(__name__)

DEFAULT_TRAIL_DIR = "./research-trail"
# Research and the bot sign in as the same Schwab app, so they share its per-app request
# quota (about 120 a minute). Research takes a third of it, which leaves the bot room if
# both run at once; a run then takes a few minutes of Schwab calls.
RESEARCH_SCHWAB_MAX_PER_MINUTE = 40


class SetupError(Exception):
    """The run cannot start: something it needs is not configured. Never holds a secret."""


async def load_settings(config: Config, aws: app.Aws) -> Settings:
    """The current settings version, read once. Without a table, the environment's."""
    if not config.settings_table:
        return Settings.from_config(config)
    try:
        latest = await DynamoSettingsStore(aws.table(config.settings_table)).latest()
    except SettingsInvalid as exc:
        raise SetupError(f"the newest settings version is invalid: {exc}") from None
    return latest.settings if latest is not None else Settings.from_config(config)


async def finnhub_key(config: Config, aws: app.Aws) -> str:
    if config.finnhub_api_key:
        return config.finnhub_api_key
    if not config.finnhub_secret_id:
        raise SetupError("no Finnhub key: set TRAIDER_FINNHUB_SECRET_ID or TRAIDER_FINNHUB_API_KEY")
    try:
        return await asyncio.to_thread(
            finnhub_key_from_secret, aws.client("secretsmanager"), config.finnhub_secret_id
        )
    except EventsUnavailable as exc:
        raise SetupError(str(exc)) from None


async def close_llm(llm: LLM) -> None:
    """Close the model client if it has anything to close. Never raises."""
    close = getattr(llm, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception as exc:
        log.warning("could not close the model client: %s", scrub(f"{type(exc).__name__}: {exc}"))


async def build_deps(
    config: Config,
    http: aiohttp.ClientSession,
    *,
    dry_run: bool,
    trail_dir: Path,
    clock: Clock,
    schwab_base_url: str = API_BASE,
    token_url: str = TOKEN_URL,
    finnhub_base_url: str = FINNHUB_BASE,
    llm: LLM | None = None,
) -> RunDeps:
    if not config.research_table:
        raise SetupError("TRAIDER_RESEARCH_TABLE is not set")
    if llm is None and not config.aws_region:
        raise SetupError("no AWS region for Bedrock: set AWS_REGION")
    aws = app.Aws(config.aws_region)
    settings = await load_settings(config, aws)
    key = await finnhub_key(config, aws)
    try:
        events = FinnhubEvents(http, key, base_url=finnhub_base_url)
    except EventsUnavailable as exc:  # its text never holds the key
        raise SetupError(str(exc)) from None
    tokens = TokenManager(
        store=app.token_store(config, aws),
        credentials=app.credentials(config, aws),
        clock=clock,
        token_url=token_url,
    )
    client = SchwabClient(
        http, tokens, base_url=schwab_base_url, max_per_minute=RESEARCH_SCHWAB_MAX_PER_MINUTE
    )

    def trail(prefix: str) -> Trail:
        if dry_run or not config.research_bucket:
            return LocalTrail(trail_dir, prefix)
        return S3Trail(aws.client("s3"), config.research_bucket, prefix)

    alerts: Alerter
    if config.alert_topic_arn and not dry_run:
        alerts = SnsAlerter(config.alert_topic_arn, aws.client("sns"), clock)
    else:
        alerts = LogAlerter()
    store = DynamoResearchStore(aws.table(config.research_table))
    market = SchwabMarketData(client)
    return RunDeps(
        store=store,
        market=market,
        events=events,
        llm=llm or MantleLLM(str(config.aws_region)),  # last: see the module docstring
        trail=trail,
        alerts=alerts,
        settings=settings,
        clock=clock,
    )
