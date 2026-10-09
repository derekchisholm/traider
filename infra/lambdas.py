"""The two Lambda functions: the Schwab sign-in endpoint and the expiry watchdog.

Both are deployed as plain source from ``src/traider``. They use only the Python
standard library and boto3, which the Lambda runtime provides, so there is no
build step and nothing to bundle.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pulumi
import pulumi_aws as aws
import pulumi_random as random

from data import Data
from settings import Settings

RUNTIME = "python3.13"
_SOURCE = Path(__file__).resolve().parent.parent / "src" / "traider"
_LAMBDA_TRUST = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)


def lambda_code() -> pulumi.AssetArchive:
    """The ``traider`` package's Python files, laid out as an importable package."""
    files = sorted(_SOURCE.rglob("*.py"))
    return pulumi.AssetArchive(
        {
            f"traider/{path.relative_to(_SOURCE).as_posix()}": pulumi.FileAsset(str(path))
            for path in files
        }
    )


@dataclass(frozen=True)
class AuthApi:
    callback_url: pulumi.Output[str]  # register this with Schwab
    reauth_url: pulumi.Output[str]  # secret: the sign-in link


def _function(
    name: str,
    settings: Settings,
    *,
    handler: str,
    timeout: int,
    environment: dict[str, pulumi.Input[str]],
    statements: list[dict[str, object]],
) -> aws.lambda_.Function:
    """A function with its own role, its own log group and only the access listed."""
    function_name = f"{settings.prefix}-{name}"
    logs = aws.cloudwatch.LogGroup(
        f"{name}-logs",
        name=f"/aws/lambda/{function_name}",
        retention_in_days=settings.log_retention_days,
        tags=settings.tags,
    )
    role = aws.iam.Role(name, assume_role_policy=_LAMBDA_TRUST, tags=settings.tags)
    policy = aws.iam.RolePolicy(
        name,
        role=role.id,
        policy=pulumi.Output.json_dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "Logs",
                        "Effect": "Allow",
                        "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                        "Resource": pulumi.Output.concat(logs.arn, ":*"),
                    },
                    *statements,
                ],
            }
        ),
    )
    return aws.lambda_.Function(
        name,
        name=function_name,
        role=role.arn,
        runtime=RUNTIME,
        handler=handler,
        code=lambda_code(),
        architectures=["arm64"],
        memory_size=256,
        timeout=timeout,
        environment=aws.lambda_.FunctionEnvironmentArgs(variables=environment),
        tags=settings.tags,
        opts=pulumi.ResourceOptions(depends_on=[logs, policy]),
    )


def build_auth(settings: Settings, data: Data) -> AuthApi:
    # The key in the sign-in link, and the key that signs the OAuth state.
    start_key = random.RandomPassword("auth-start-key", length=32, special=False)
    state_secret = random.RandomPassword("auth-state-secret", length=48, special=False)

    api = aws.apigatewayv2.Api(
        "auth",
        name=f"{settings.prefix}-auth",
        protocol_type="HTTP",
        description="traider: Schwab sign-in",
        tags=settings.tags,
    )
    hosted_callback = pulumi.Output.concat(api.api_endpoint, "/callback")
    callback_url: pulumi.Output[str] = (
        pulumi.Output.from_input(settings.callback_url)
        if settings.callback_url
        else hosted_callback
    )

    function = _function(
        "auth",
        settings,
        handler="traider.lambdas.auth.handler",
        timeout=20,
        environment={
            "APP_SECRET_ID": data.app_secret.arn,
            "TOKEN_SECRET_ID": data.token_secret.arn,
            "PUBLIC_URL": api.api_endpoint,
            "CALLBACK_URL": callback_url,
            "START_KEY": start_key.result,
            "STATE_SECRET": state_secret.result,
            "ALERT_TOPIC_ARN": data.topic.arn,
        },
        statements=[
            {
                "Sid": "ReadAppCredentials",
                "Effect": "Allow",
                "Action": "secretsmanager:GetSecretValue",
                "Resource": data.app_secret.arn,
            },
            {
                "Sid": "StoreSignIn",
                "Effect": "Allow",
                "Action": "secretsmanager:PutSecretValue",
                "Resource": data.token_secret.arn,
            },
            {
                "Sid": "Announce",
                "Effect": "Allow",
                "Action": "sns:Publish",
                "Resource": data.topic.arn,
            },
        ],
    )

    integration = aws.apigatewayv2.Integration(
        "auth",
        api_id=api.id,
        integration_type="AWS_PROXY",
        integration_uri=function.invoke_arn,
        integration_method="POST",
        payload_format_version="2.0",
    )
    for name, route_key in (
        ("auth-start", "GET /start"),
        ("auth-callback", "GET /callback"),
        ("auth-exchange", "POST /exchange"),
    ):
        aws.apigatewayv2.Route(
            name,
            api_id=api.id,
            route_key=route_key,
            target=pulumi.Output.concat("integrations/", integration.id),
        )
    aws.apigatewayv2.Stage(
        "auth",
        api_id=api.id,
        name="$default",
        auto_deploy=True,
        # A person signs in once a week. Anything faster than this is not a person.
        default_route_settings=aws.apigatewayv2.StageDefaultRouteSettingsArgs(
            throttling_rate_limit=2, throttling_burst_limit=5
        ),
        tags=settings.tags,
    )
    aws.lambda_.Permission(
        "auth-api",
        action="lambda:InvokeFunction",
        function=function.name,
        principal="apigateway.amazonaws.com",
        source_arn=pulumi.Output.concat(api.execution_arn, "/*/*"),
    )

    reauth_url = pulumi.Output.concat(api.api_endpoint, "/start?k=", start_key.result)
    return AuthApi(callback_url=callback_url, reauth_url=pulumi.Output.secret(reauth_url))


def build_watchdog(settings: Settings, data: Data, reauth_url: pulumi.Output[str]) -> None:
    function = _function(
        "watchdog",
        settings,
        handler="traider.lambdas.watchdog.handler",
        timeout=15,
        environment={
            "TOKEN_SECRET_ID": data.token_secret.arn,
            "ALERT_TOPIC_ARN": data.topic.arn,
            "REAUTH_URL": reauth_url,
            "WARN_HOURS": str(settings.reauth_warn_hours),
        },
        statements=[
            {
                "Sid": "ReadSignIn",
                "Effect": "Allow",
                "Action": "secretsmanager:GetSecretValue",
                "Resource": data.token_secret.arn,
            },
            {
                "Sid": "Alert",
                "Effect": "Allow",
                "Action": "sns:Publish",
                "Resource": data.topic.arn,
            },
        ],
    )
    rule = aws.cloudwatch.EventRule(
        "watchdog",
        name=f"{settings.prefix}-watchdog",
        description="traider: daily check that the Schwab sign-in is not about to expire",
        schedule_expression=settings.watchdog_schedule,
        tags=settings.tags,
    )
    aws.cloudwatch.EventTarget("watchdog", rule=rule.name, arn=function.arn)
    aws.lambda_.Permission(
        "watchdog-schedule",
        action="lambda:InvokeFunction",
        function=function.name,
        principal="events.amazonaws.com",
        source_arn=rule.arn,
    )
