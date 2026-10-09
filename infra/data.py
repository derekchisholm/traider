"""Where state and secrets live: Secrets Manager, Parameter Store, DynamoDB, SNS."""

from __future__ import annotations

from dataclasses import dataclass

import pulumi
import pulumi_aws as aws

from settings import Settings


@dataclass(frozen=True)
class Data:
    app_secret: aws.secretsmanager.Secret
    token_secret: aws.secretsmanager.Secret
    control: aws.ssm.Parameter
    table: aws.dynamodb.Table
    settings_table: aws.dynamodb.Table
    topic: aws.sns.Topic


def build(settings: Settings) -> Data:
    prefix, tags = settings.prefix, settings.tags

    # Only the containers are created here. The values never pass through Pulumi, so
    # they are never in its state: you set the app credentials yourself, and the
    # sign-in function writes the token.
    app_secret = aws.secretsmanager.Secret(
        "schwab-app",
        description='Schwab developer app credentials: {"app_key": "...", "app_secret": "..."}',
        tags=tags,
    )
    token_secret = aws.secretsmanager.Secret(
        "schwab-token",
        description="Schwab refresh token from the last sign-in (written by the sign-in function)",
        tags=tags,
    )

    # The runtime control switch. A live stack starts halted; changing the value later
    # is an operator action, so a deploy must never put it back.
    control = aws.ssm.Parameter(
        "control",
        name=f"/{prefix}/control",
        type="String",
        value="halt" if settings.trading_mode == "live" else "paper",
        allowed_pattern="^(halt|close_only|paper|live)$",
        description="traider control switch: halt | close_only | paper | live",
        tags=tags,
        opts=pulumi.ResourceOptions(ignore_changes=["value"]),
    )

    table = aws.dynamodb.Table(
        "state",
        name=f"{prefix}-state",
        billing_mode="PAY_PER_REQUEST",
        hash_key="pk",
        range_key="sk",
        attributes=[
            aws.dynamodb.TableAttributeArgs(name="pk", type="S"),
            aws.dynamodb.TableAttributeArgs(name="sk", type="S"),
        ],
        point_in_time_recovery=aws.dynamodb.TablePointInTimeRecoveryArgs(enabled=True),
        deletion_protection_enabled=settings.trading_mode == "live",
        tags=tags,
    )

    # Versioned bot settings. Each change is a new item and nothing is ever rewritten,
    # so the history is the audit trail.
    settings_table = aws.dynamodb.Table(
        "settings",
        name=f"{prefix}-settings",
        billing_mode="PAY_PER_REQUEST",
        hash_key="pk",
        range_key="sk",
        attributes=[
            aws.dynamodb.TableAttributeArgs(name="pk", type="S"),
            aws.dynamodb.TableAttributeArgs(name="sk", type="S"),
        ],
        point_in_time_recovery=aws.dynamodb.TablePointInTimeRecoveryArgs(enabled=True),
        deletion_protection_enabled=settings.trading_mode == "live",
        tags=tags,
    )

    topic = aws.sns.Topic("alerts", name=f"{prefix}-alerts", tags=tags)
    if settings.alert_email:
        aws.sns.TopicSubscription(
            "alerts-email", topic=topic.arn, protocol="email", endpoint=settings.alert_email
        )
    return Data(app_secret, token_secret, control, table, settings_table, topic)
