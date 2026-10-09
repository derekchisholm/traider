"""Daily check on the Schwab sign-in (Lambda on a schedule).

The refresh token lasts seven days. This sends an alert with the sign-in link
when it is close to expiring, has expired, or was never created, so it gets
renewed before the bot goes blind. The bot also alerts the moment it loses its
login; this is the early warning, and it works even when the bot is not running.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from traider.alerts import publish
from traider.schwab.tokens import SecretsManagerTokenStore, TokenStoreError


@dataclass(frozen=True)
class Settings:
    token_secret_id: str
    alert_topic_arn: str
    reauth_url: str
    warn_hours: float = 48.0

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        return cls(
            token_secret_id=env["TOKEN_SECRET_ID"],
            alert_topic_arn=env["ALERT_TOPIC_ARN"],
            reauth_url=env["REAUTH_URL"],
            warn_hours=float(env.get("WARN_HOURS") or 48),
        )


def check(settings: Settings, *, secrets: Any, sns: Any, now: float) -> dict[str, Any]:
    link = f"Sign in here:\n{settings.reauth_url}"

    def alert(status: str, subject: str, text: str) -> dict[str, Any]:
        publish(sns, settings.alert_topic_arn, subject, text)
        return {"status": status, "subject": subject}

    try:
        grant = SecretsManagerTokenStore(settings.token_secret_id, secrets).load()
    except TokenStoreError as exc:
        return alert(
            "error",
            "Cannot read the Schwab sign-in",
            f"The stored sign-in could not be read: {exc}.\nSigning in again will replace it.\n\n"
            f"{link}",
        )
    if grant is None:
        return alert(
            "missing",
            "Schwab sign-in needed",
            f"Nobody has signed in to Schwab yet, so the bot has no market data.\n\n{link}",
        )
    hours_left = grant.seconds_left(now) / 3600
    if hours_left <= 0:
        return alert(
            "expired",
            "Schwab sign-in has expired",
            "The 7-day Schwab sign-in has run out. The bot cannot trade or read market "
            f"data until you sign in again.\n\n{link}",
        )
    if hours_left <= settings.warn_hours:
        return alert(
            "expiring",
            f"Schwab sign-in expires in {hours_left:.0f} hours",
            "Sign in again before it runs out to keep the bot connected. Signing in early "
            f"is fine; it starts a new 7 days.\n\n{link}",
        )
    return {"status": "ok", "hours_left": round(hours_left, 1)}


def handler(_event: Mapping[str, Any], _context: object) -> dict[str, Any]:
    import boto3  # noqa: PLC0415 - provided by the Lambda runtime

    result = check(
        Settings.from_env(os.environ),
        secrets=boto3.client("secretsmanager"),
        sns=boto3.client("sns"),
        now=time.time(),
    )
    print(result)
    return result
