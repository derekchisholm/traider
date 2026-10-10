"""The whole deployment, assembled."""

from __future__ import annotations

import shlex
from dataclasses import dataclass

import pulumi
import pulumi_aws as aws

import bot
import data
import lambdas
import network
import research
import settings as stack_settings


@dataclass(frozen=True)
class Stack:
    outputs: dict[str, pulumi.Input[str]]


def build() -> Stack:
    settings = stack_settings.load()
    net = network.build(settings.prefix, settings.tags)
    store = data.build(settings)
    auth = lambdas.build_auth(settings, store)
    lambdas.build_watchdog(settings, store, auth.reauth_url)

    # The bot gets the sign-in link (for its alerts) from an encrypted parameter.
    reauth_param = aws.ssm.Parameter(
        "reauth-url",
        name=f"/{settings.prefix}/reauth-url",
        type="SecureString",
        value=auth.reauth_url,
        description="traider: the Schwab sign-in link, including its key",
        tags=settings.tags,
    )
    deployed = bot.build(settings, net, store, reauth_param)
    alarms: dict[str, bot.Alarm] = {"TaskStoppedAlarm": deployed.stopped_rule}
    jobs = None
    if settings.research_jobs:
        jobs = research.build(settings, net, store, deployed)
        alarms["ResearchFailedAlarm"] = jobs.failed_rule
        alarms["ResearchNotStartedAlarm"] = jobs.dead_letter_alarm
    bot.alert_topic_policy(store, alarms)

    # What the command line needs on your own machine: the bot's settings and where its
    # secrets live. The trading mode, the control switch and the state table are left
    # out on purpose, so nothing run locally with this can send a live order. The
    # settings table is included so `traider settings` works locally; settings cannot
    # place orders. The same goes for the research table (when research is on), so
    # `traider research seed|show` works: those commands never reach Schwab.
    local: dict[str, pulumi.Input[str]] = {
        "AWS_REGION": aws.get_region_output().region,
        **{k: v for k, v in settings.bot_env.items() if k != "TRAIDER_TRADING_MODE"},
        "TRAIDER_SCHWAB_APP_SECRET_ID": store.app_secret.arn,
        "TRAIDER_SCHWAB_TOKEN_SECRET_ID": store.token_secret.arn,
        "TRAIDER_SETTINGS_TABLE": store.settings_table.name,
    }
    if store.research_table is not None:
        local["TRAIDER_RESEARCH_TABLE"] = store.research_table.name
    if jobs is not None:
        # `traider research run --dry-run` reads the key from the secret; the trail of a
        # local run stays on your machine, so the bucket is left out.
        local["TRAIDER_FINNHUB_SECRET_ID"] = jobs.finnhub_secret.arn
    if settings.callback_url:
        # Paste mode: `traider login` must use the same registered address.
        local["TRAIDER_SCHWAB_CALLBACK_URL"] = settings.callback_url
    local_env = pulumi.Output.all(**local).apply(
        lambda values: "\n".join(f"{name}={shlex.quote(value)}" for name, value in values.items())
    )
    research_outputs: dict[str, pulumi.Input[str]] = (
        {"researchTable": store.research_table.name} if store.research_table is not None else {}
    )
    if jobs is not None:
        research_outputs |= {
            "researchBucket": jobs.bucket.bucket,
            "finnhubSecretArn": jobs.finnhub_secret.arn,
            "researchCluster": jobs.cluster.name,
            "researchLogGroup": jobs.log_group.name,
        }
    return Stack(
        outputs={
            # Register this address as the app's callback URL in the Schwab developer portal.
            "callbackUrl": auth.callback_url,
            # Open this to sign in to Schwab. Secret: `pulumi stack output reauthUrl --show-secrets`
            "reauthUrl": auth.reauth_url,
            "controlParameter": store.control.name,
            "appSecretArn": store.app_secret.arn,
            "tokenSecretArn": store.token_secret.arn,
            "stateTable": store.table.name,
            "settingsTable": store.settings_table.name,
            **research_outputs,
            "alertTopicArn": store.topic.arn,
            "clusterName": deployed.cluster_name,
            "serviceName": deployed.service_name,
            "logGroup": deployed.log_group,
            "image": deployed.image,
            # Environment for running `traider check` or `traider login` on your machine.
            "localEnv": local_env,
        }
    )
