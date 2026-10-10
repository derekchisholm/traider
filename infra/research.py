"""The scheduled research run (opt-in: ``traider:researchJobs``, which needs research on).

Every weekday at 08:00 New York time EventBridge Scheduler starts one Fargate task from
the bot's image: ``traider research run --kind premarket``. It reads the market, sets the
day's posture, has Claude on Bedrock study the best candidates and writes ranked picks to
the research table. Its trail goes to a private S3 bucket. A task that exits non-zero
raises an alert, and so does a run that cannot even be started: what the scheduler
fails to deliver lands in a dead-letter queue, which alarms.

It runs in its own ECS cluster, so the bot's crash alarm (which watches the bot's
cluster) never fires for it, on the bot's subnets and security group.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pulumi
import pulumi_aws as aws

from bot import ECS_TRUST, Bot
from data import Data
from network import Network
from settings import Settings, trail_bucket_name

SCHEDULE = "cron(0 8 ? * MON-FRI *)"
TIMEZONE = "America/New_York"
TRAIL_EXPIRY_DAYS = 400
START_RETRIES = 2  # a failed start is retried this often, within START_MAX_AGE_S
START_MAX_AGE_S = 600
DLQ_RETENTION_S = 14 * 24 * 3600  # the most SQS keeps a message
# Bot settings the research task does not get: it never trades and never reads an account.
NOT_FOR_RESEARCH = frozenset(
    {"TRAIDER_TRADING_MODE", "TRAIDER_SCHWAB_ACCOUNT_HASH", "TRAIDER_SCHWAB_ACCOUNT_LAST4"}
)


@dataclass(frozen=True)
class ResearchJobs:
    bucket: aws.s3.Bucket
    finnhub_secret: aws.secretsmanager.Secret
    cluster: aws.ecs.Cluster
    task: aws.ecs.TaskDefinition
    log_group: aws.cloudwatch.LogGroup
    failed_rule: aws.cloudwatch.EventRule
    dead_letters: aws.sqs.Queue
    dead_letter_alarm: aws.cloudwatch.MetricAlarm


def _trail_bucket(settings: Settings, account: pulumi.Output[str]) -> aws.s3.Bucket:
    """Private, encrypted (SSE-S3), reachable over TLS only, objects gone after 400 days.
    Named with the account id because bucket names are global."""
    bucket = aws.s3.Bucket(
        "research-trail",
        bucket=account.apply(lambda account_id: trail_bucket_name(settings.prefix, account_id)),
        # A paper stack can be destroyed with its trail; a live stack's trail is kept.
        force_destroy=settings.trading_mode != "live",
        tags=settings.tags,
    )
    public_access_block = aws.s3.BucketPublicAccessBlock(
        "research-trail",
        bucket=bucket.id,
        block_public_acls=True,
        block_public_policy=True,
        ignore_public_acls=True,
        restrict_public_buckets=True,
    )
    aws.s3.BucketServerSideEncryptionConfiguration(
        "research-trail",
        bucket=bucket.id,
        rules=[
            aws.s3.BucketServerSideEncryptionConfigurationRuleArgs(
                apply_server_side_encryption_by_default=aws.s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(
                    sse_algorithm="AES256"
                )
            )
        ],
    )
    aws.s3.BucketLifecycleConfiguration(
        "research-trail",
        bucket=bucket.id,
        rules=[
            aws.s3.BucketLifecycleConfigurationRuleArgs(
                id="expire-trail",
                status="Enabled",
                filter=aws.s3.BucketLifecycleConfigurationRuleFilterArgs(prefix=""),
                expiration=aws.s3.BucketLifecycleConfigurationRuleExpirationArgs(
                    days=TRAIL_EXPIRY_DAYS
                ),
            )
        ],
    )
    aws.s3.BucketPolicy(
        "research-trail",
        bucket=bucket.id,
        policy=pulumi.Output.json_dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "TlsOnly",
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "s3:*",
                        "Resource": [bucket.arn, pulumi.Output.concat(bucket.arn, "/*")],
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    }
                ],
            }
        ),
        # S3 rejects a bucket policy while the account default may still block it.
        opts=pulumi.ResourceOptions(depends_on=[public_access_block]),
    )
    return bucket


def build(settings: Settings, network: Network, data: Data, bot: Bot) -> ResearchJobs:
    assert data.research_table is not None, "researchJobs needs the research table"
    prefix, tags = settings.prefix, settings.tags
    region = aws.get_region_output().region
    account = aws.get_caller_identity_output().account_id
    family = f"{prefix}-research"

    bucket = _trail_bucket(settings, account)
    # Created empty. You store {"api_key": "..."} yourself, so the key never passes
    # through Pulumi or its state.
    finnhub_secret = aws.secretsmanager.Secret(
        "finnhub",
        description='Finnhub API key for the research jobs: {"api_key": "..."}',
        tags=tags,
    )
    logs = aws.cloudwatch.LogGroup(
        "research-logs",
        name=f"/traider/{prefix}/research",
        retention_in_days=settings.log_retention_days,
        tags=tags,
    )

    # What the research run's own code may do. Each statement names exact resources,
    # except Bedrock's, which AWS documents on "*" for the Mantle endpoint.
    task_role = aws.iam.Role("research-task", assume_role_policy=ECS_TRUST, tags=tags)
    statements: list[dict[str, Any]] = [
        {
            "Sid": "ReadSecrets",
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": [data.app_secret.arn, data.token_secret.arn, finnhub_secret.arn],
        },
        {
            "Sid": "SaveRotatedRefreshToken",
            "Effect": "Allow",
            "Action": "secretsmanager:PutSecretValue",
            "Resource": data.token_secret.arn,
        },
        {
            "Sid": "Research",
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:Query",
            ],
            "Resource": data.research_table.arn,
        },
        {
            "Sid": "ResearchRunsByDay",
            "Effect": "Allow",
            "Action": "dynamodb:Query",
            "Resource": pulumi.Output.concat(data.research_table.arn, "/index/gsi1"),
        },
        {
            "Sid": "ReadSettings",
            "Effect": "Allow",
            "Action": ["dynamodb:Query", "dynamodb:GetItem"],
            "Resource": data.settings_table.arn,
        },
        {
            "Sid": "Trail",
            "Effect": "Allow",
            "Action": "s3:PutObject",
            "Resource": pulumi.Output.concat(bucket.arn, "/*"),
        },
        {
            "Sid": "Alerts",
            "Effect": "Allow",
            "Action": "sns:Publish",
            "Resource": data.topic.arn,
        },
        {
            "Sid": "BedrockMantle",
            "Effect": "Allow",
            "Action": [
                "bedrock-mantle:CreateInference",
                "bedrock-mantle:GetProject",
                "bedrock-mantle:ListProjects",
            ],
            "Resource": "*",
        },
    ]
    task_policy = aws.iam.RolePolicy(
        "research-task",
        role=task_role.id,
        policy=pulumi.Output.json_dumps({"Version": "2012-10-17", "Statement": statements}),
    )

    # The bot's environment without its trading mode (research never trades; leaving it
    # out also means a live stack's Config does not demand the bot's live-only settings)
    # and without the account identifiers research never uses, plus where research reads
    # and writes.
    environment: dict[str, pulumi.Input[str]] = {
        **{k: v for k, v in settings.bot_env.items() if k not in NOT_FOR_RESEARCH},
        "TRAIDER_SCHWAB_APP_SECRET_ID": data.app_secret.arn,
        "TRAIDER_SCHWAB_TOKEN_SECRET_ID": data.token_secret.arn,
        "TRAIDER_SETTINGS_TABLE": data.settings_table.name,
        "TRAIDER_RESEARCH_TABLE": data.research_table.name,
        "TRAIDER_ALERT_TOPIC_ARN": data.topic.arn,
        "TRAIDER_RESEARCH_BUCKET": bucket.bucket,
        "TRAIDER_FINNHUB_SECRET_ID": finnhub_secret.arn,
        # Bedrock needs a region; set it rather than rely on Fargate providing one.
        "AWS_REGION": region,
    }
    container = {
        "name": "research",
        "image": bot.image,
        "essential": True,
        "command": ["research", "run", "--kind", "premarket"],
        "environment": [
            {"name": name, "value": value} for name, value in sorted(environment.items())
        ],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": logs.name,
                "awslogs-region": region,
                "awslogs-stream-prefix": "research",
            },
        },
        "linuxParameters": {"initProcessEnabled": True},
    }
    task = aws.ecs.TaskDefinition(
        "research",
        family=family,
        cpu="512",
        memory="1024",
        network_mode="awsvpc",
        requires_compatibilities=["FARGATE"],
        runtime_platform=aws.ecs.TaskDefinitionRuntimePlatformArgs(
            cpu_architecture=settings.cpu_architecture, operating_system_family="LINUX"
        ),
        execution_role_arn=bot.execution_role.arn,
        task_role_arn=task_role.arn,
        container_definitions=pulumi.Output.json_dumps([container]),
        skip_destroy=True,
        tags=tags,
        opts=pulumi.ResourceOptions(depends_on=[task_policy]),
    )
    cluster = aws.ecs.Cluster("research", name=family, tags=tags)

    # A start the scheduler gives up on (RunTask refused, after the retries) lands here,
    # and the alarm below says so: no ECS task exists, so the research-failed rule
    # cannot see it.
    dead_letters = aws.sqs.Queue(
        "research-schedule-dlq",
        name=f"{prefix}-research-schedule-dlq",
        sqs_managed_sse_enabled=True,
        message_retention_seconds=DLQ_RETENTION_S,
        tags=tags,
    )

    # The scheduler may start this task family in this cluster and hand it its two roles.
    # Only this account's scheduler may assume the role.
    scheduler_trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "scheduler.amazonaws.com"},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ],
    }
    scheduler_role = aws.iam.Role(
        "research-scheduler",
        assume_role_policy=pulumi.Output.json_dumps(scheduler_trust),
        tags=tags,
    )
    family_arn = pulumi.Output.concat(
        "arn:aws:ecs:", region, ":", account, ":task-definition/", family, ":*"
    )
    scheduler_policy = aws.iam.RolePolicy(
        "research-scheduler",
        role=scheduler_role.id,
        policy=pulumi.Output.json_dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "StartResearchTask",
                        "Effect": "Allow",
                        "Action": "ecs:RunTask",
                        "Resource": family_arn,
                        "Condition": {"ArnEquals": {"ecs:cluster": cluster.arn}},
                    },
                    {
                        "Sid": "PassResearchRoles",
                        "Effect": "Allow",
                        "Action": "iam:PassRole",
                        "Resource": [task_role.arn, bot.execution_role.arn],
                        "Condition": {
                            "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
                        },
                    },
                    {
                        "Sid": "DeadLetters",
                        "Effect": "Allow",
                        "Action": "sqs:SendMessage",
                        "Resource": dead_letters.arn,
                    },
                ],
            }
        ),
    )
    aws.scheduler.Schedule(
        "research-premarket",
        name=f"{prefix}-research-premarket",
        description="traider: the pre-market research run",
        schedule_expression=SCHEDULE,
        schedule_expression_timezone=TIMEZONE,
        # Created disabled: nothing fires until you have stored the Finnhub key, enabled
        # Bedrock access and done a dry run, then set traider:researchScheduleEnabled.
        state="ENABLED" if settings.research_schedule_enabled else "DISABLED",
        # Exactly on time. A start that fails is retried briefly (the lock and
        # skip-if-done make a duplicate harmless); one that still fails goes to the
        # dead-letter queue.
        flexible_time_window=aws.scheduler.ScheduleFlexibleTimeWindowArgs(mode="OFF"),
        target=aws.scheduler.ScheduleTargetArgs(
            arn=cluster.arn,
            role_arn=scheduler_role.arn,
            ecs_parameters=aws.scheduler.ScheduleTargetEcsParametersArgs(
                task_definition_arn=task.arn,
                launch_type="FARGATE",
                task_count=1,
                network_configuration=aws.scheduler.ScheduleTargetEcsParametersNetworkConfigurationArgs(
                    subnets=network.subnet_ids,
                    security_groups=[network.security_group_id],
                    assign_public_ip=True,
                ),
            ),
            retry_policy=aws.scheduler.ScheduleTargetRetryPolicyArgs(
                maximum_retry_attempts=START_RETRIES,
                maximum_event_age_in_seconds=START_MAX_AGE_S,
            ),
            dead_letter_config=aws.scheduler.ScheduleTargetDeadLetterConfigArgs(
                arn=dead_letters.arn
            ),
        ),
        opts=pulumi.ResourceOptions(depends_on=[scheduler_policy]),
    )

    failed_rule = aws.cloudwatch.EventRule(
        "research-failed",
        name=f"{prefix}-research-failed",
        description="traider: a research run failed, could not start or found the lock held",
        event_pattern=pulumi.Output.json_dumps(
            {
                "source": ["aws.ecs"],
                "detail-type": ["ECS Task State Change"],
                "detail": {
                    "clusterArn": [cluster.arn],
                    "group": [f"family:{family}"],
                    "lastStatus": ["STOPPED"],
                    "$or": [
                        {"stopCode": ["TaskFailedToStart"]},
                        {"containers": {"exitCode": [{"anything-but": 0}]}},
                    ],
                },
            }
        ),
        tags=tags,
    )
    aws.cloudwatch.EventTarget(
        "research-failed",
        rule=failed_rule.name,
        arn=data.topic.arn,
        input_transformer=aws.cloudwatch.EventTargetInputTransformerArgs(
            input_paths={"reason": "$.detail.stoppedReason", "code": "$.detail.stopCode"},
            input_template=(
                '"[traider] The research run stopped with an error (<code>): <reason>. '
                "Without a successful run today the bot stands aside. Read the research "
                'logs; docs/runbook.md says what to do."'
            ),
        ),
    )
    # Fires once, when the first undelivered start arrives. It stays in alarm until the
    # queue is emptied (docs/runbook.md), so purge it after reading the message.
    dead_letter_alarm = aws.cloudwatch.MetricAlarm(
        "research-schedule-dlq",
        name=f"{prefix}-research-schedule-failed",
        alarm_description=(
            "traider: the scheduler could not start the research run. Without a successful "
            "run today the bot stands aside. Read the message in the dead-letter queue, then "
            "purge it; docs/runbook.md says what to do."
        ),
        namespace="AWS/SQS",
        metric_name="ApproximateNumberOfMessagesVisible",
        dimensions={"QueueName": dead_letters.name},
        statistic="Maximum",
        period=300,
        evaluation_periods=1,
        comparison_operator="GreaterThanThreshold",
        threshold=0,
        treat_missing_data="notBreaching",
        alarm_actions=[data.topic.arn],
        tags=tags,
    )
    return ResearchJobs(
        bucket, finnhub_secret, cluster, task, logs, failed_rule, dead_letters, dead_letter_alarm
    )
