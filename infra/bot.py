"""The bot itself: container image, Fargate task, service and its market-hours schedule."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pulumi
import pulumi_aws as aws
import pulumi_docker_build as docker_build

from data import Data
from network import Network
from settings import Settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ECS_TRUST = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)


@dataclass(frozen=True)
class Bot:
    cluster_name: pulumi.Output[str]
    service_name: pulumi.Output[str]
    log_group: pulumi.Output[str]
    image: pulumi.Output[str]


def _image(settings: Settings) -> pulumi.Output[str]:
    """Build and push the image, unless a prebuilt one was configured."""
    repository = aws.ecr.Repository(
        "bot",
        name=settings.prefix,
        force_delete=True,  # so `pulumi destroy` works; the images are rebuildable
        image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(
            scan_on_push=True
        ),
        tags=settings.tags,
    )
    aws.ecr.LifecyclePolicy(
        "bot",
        repository=repository.name,
        policy=json.dumps(
            {
                "rules": [
                    {
                        "rulePriority": 1,
                        # One push can count as several images (the image itself plus
                        # its index and attestation), so this is roughly ten deploys.
                        "description": "keep the last 30 images",
                        "selection": {
                            "tagStatus": "any",
                            "countType": "imageCountMoreThan",
                            "countNumber": 30,
                        },
                        "action": {"type": "expire"},
                    }
                ]
            }
        ),
    )
    if settings.image:
        return pulumi.Output.from_input(settings.image)
    login = aws.ecr.get_authorization_token_output(registry_id=repository.registry_id)
    platform = (
        docker_build.Platform.LINUX_ARM64
        if settings.cpu_architecture == "ARM64"
        else docker_build.Platform.LINUX_AMD64
    )
    image = docker_build.Image(
        "bot",
        tags=[pulumi.Output.concat(repository.repository_url, ":latest")],
        context=docker_build.BuildContextArgs(location=str(_REPO_ROOT)),
        dockerfile=docker_build.DockerfileArgs(location=str(_REPO_ROOT / "Dockerfile")),
        platforms=[platform],
        push=True,
        registries=[
            docker_build.RegistryArgs(
                address=repository.repository_url,
                username=login.user_name,
                password=pulumi.Output.secret(login.password),
            )
        ],
    )
    # repository@digest: the form ECS documents. It changes whenever the image does,
    # which is what rolls the task, and it can never point at a different build.
    return pulumi.Output.concat(repository.repository_url, "@", image.digest)


def _alert_when_the_task_dies(
    prefix: str, tags: dict[str, str], cluster: aws.ecs.Cluster, data: Data
) -> None:
    """Send an alert when a task crashes or cannot start. The bot cannot report its own
    death, and a deploy made outside market hours is not exercised until the next open.
    Clean stops (the evening schedule, a deploy) exit 0 and stay quiet."""
    rule = aws.cloudwatch.EventRule(
        "bot-stopped",
        name=f"{prefix}-bot-stopped",
        description="traider: the bot's task crashed or could not start",
        event_pattern=pulumi.Output.json_dumps(
            {
                "source": ["aws.ecs"],
                "detail-type": ["ECS Task State Change"],
                "detail": {
                    "clusterArn": [cluster.arn],
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
        "bot-stopped",
        rule=rule.name,
        arn=data.topic.arn,
        input_transformer=aws.cloudwatch.EventTargetInputTransformerArgs(
            input_paths={"reason": "$.detail.stoppedReason", "code": "$.detail.stopCode"},
            input_template=(
                "\"[traider] The bot's task stopped unexpectedly (<code>): <reason>. "
                'ECS will try to start it again; check the logs if this repeats."'
            ),
        ),
    )
    aws.sns.TopicPolicy(
        "alerts",
        arn=data.topic.arn,
        policy=pulumi.Output.json_dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "TaskStoppedAlarm",
                        "Effect": "Allow",
                        "Principal": {"Service": "events.amazonaws.com"},
                        "Action": "sns:Publish",
                        "Resource": data.topic.arn,
                        "Condition": {"ArnEquals": {"aws:SourceArn": rule.arn}},
                    }
                ],
            }
        ),
    )


def build(settings: Settings, network: Network, data: Data, reauth_param: aws.ssm.Parameter) -> Bot:
    prefix, tags = settings.prefix, settings.tags
    region = aws.get_region_output().region
    image = _image(settings)

    logs = aws.cloudwatch.LogGroup(
        "bot-logs",
        name=f"/ecs/{prefix}",
        retention_in_days=settings.log_retention_days,
        tags=tags,
    )

    # The execution role is what ECS uses to start the task: pull the image, write logs,
    # and fetch the one parameter injected as a secret.
    execution_role = aws.iam.Role("bot-execution", assume_role_policy=_ECS_TRUST, tags=tags)
    aws.iam.RolePolicyAttachment(
        "bot-execution-managed",
        role=execution_role.name,
        policy_arn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
    )
    execution_policy = aws.iam.RolePolicy(
        "bot-execution",
        role=execution_role.id,
        policy=pulumi.Output.json_dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "SignInLink",
                        "Effect": "Allow",
                        "Action": "ssm:GetParameters",
                        "Resource": reauth_param.arn,
                    }
                ],
            }
        ),
    )

    # The task role is what the bot's own code can do. Each statement names exact
    # resources. Note what is missing: it cannot change the control switch.
    task_role = aws.iam.Role("bot-task", assume_role_policy=_ECS_TRUST, tags=tags)
    statements: list[dict[str, Any]] = [
        {
            "Sid": "ReadSchwabSecrets",
            "Effect": "Allow",
            "Action": "secretsmanager:GetSecretValue",
            "Resource": [data.app_secret.arn, data.token_secret.arn],
        },
        {
            "Sid": "SaveRotatedRefreshToken",
            "Effect": "Allow",
            "Action": "secretsmanager:PutSecretValue",
            "Resource": data.token_secret.arn,
        },
        {
            "Sid": "ReadControlSwitch",
            "Effect": "Allow",
            "Action": "ssm:GetParameter",
            "Resource": data.control.arn,
        },
        {
            "Sid": "State",
            "Effect": "Allow",
            "Action": [
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:Query",
            ],
            "Resource": data.table.arn,
        },
        {
            "Sid": "Settings",
            "Effect": "Allow",
            "Action": ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem"],
            "Resource": data.settings_table.arn,
        },
        {
            "Sid": "Alerts",
            "Effect": "Allow",
            "Action": "sns:Publish",
            "Resource": data.topic.arn,
        },
    ]
    if data.research_table is not None:
        # Read only, by key. Not the index (that is for reports), and no writes: the
        # research jobs own this table.
        statements.append(
            {
                "Sid": "Research",
                "Effect": "Allow",
                "Action": ["dynamodb:Query", "dynamodb:GetItem"],
                "Resource": data.research_table.arn,
            }
        )
    task_policy = aws.iam.RolePolicy(
        "bot-task",
        role=task_role.id,
        policy=pulumi.Output.json_dumps({"Version": "2012-10-17", "Statement": statements}),
    )

    environment: dict[str, pulumi.Input[str]] = {
        **settings.bot_env,
        "TRAIDER_SCHWAB_APP_SECRET_ID": data.app_secret.arn,
        "TRAIDER_SCHWAB_TOKEN_SECRET_ID": data.token_secret.arn,
        "TRAIDER_CONTROL_PARAM": data.control.name,
        "TRAIDER_STATE_TABLE": data.table.name,
        "TRAIDER_SETTINGS_TABLE": data.settings_table.name,
        "TRAIDER_ALERT_TOPIC_ARN": data.topic.arn,
    }
    if data.research_table is not None:
        environment["TRAIDER_RESEARCH_TABLE"] = data.research_table.name
    container = {
        "name": "bot",
        "image": image,
        "essential": True,
        "command": ["run"],
        "environment": [
            {"name": name, "value": value} for name, value in sorted(environment.items())
        ],
        # Injected by ECS at start; it contains the sign-in key, so it is not a plain variable.
        "secrets": [{"name": "TRAIDER_REAUTH_URL", "valueFrom": reauth_param.arn}],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": logs.name,
                "awslogs-region": region,
                "awslogs-stream-prefix": "bot",
            },
        },
        # Replaces the task if the bot's loop stops turning. Says nothing about Schwab.
        "healthCheck": {
            "command": ["CMD", "python", "-m", "traider.health"],
            "interval": 30,
            "timeout": 5,
            "retries": 3,
            "startPeriod": 60,
        },
        "stopTimeout": 120,  # the most Fargate allows: time to cancel working orders
        "linuxParameters": {"initProcessEnabled": True},
    }
    task = aws.ecs.TaskDefinition(
        "bot",
        family=f"{prefix}-bot",
        cpu="256",
        memory="512",
        network_mode="awsvpc",
        requires_compatibilities=["FARGATE"],
        runtime_platform=aws.ecs.TaskDefinitionRuntimePlatformArgs(
            cpu_architecture=settings.cpu_architecture, operating_system_family="LINUX"
        ),
        execution_role_arn=execution_role.arn,
        task_role_arn=task_role.arn,
        container_definitions=pulumi.Output.json_dumps([container]),
        # Keep old revisions registered: a rollback after a failed deploy needs one.
        skip_destroy=True,
        tags=tags,
        opts=pulumi.ResourceOptions(depends_on=[execution_policy, task_policy]),
    )

    cluster = aws.ecs.Cluster("bot", name=prefix, tags=tags)
    _alert_when_the_task_dies(prefix, tags, cluster, data)
    scheduled = not settings.always_on
    service = aws.ecs.Service(
        "bot",
        name=f"{prefix}-bot",
        cluster=cluster.arn,
        task_definition=task.arn,
        launch_type="FARGATE",
        desired_count=1,
        # Never two bots: a deploy stops the old task before it starts the new one.
        deployment_minimum_healthy_percent=0,
        deployment_maximum_percent=100,
        deployment_circuit_breaker=aws.ecs.ServiceDeploymentCircuitBreakerArgs(
            enable=True, rollback=True
        ),
        network_configuration=aws.ecs.ServiceNetworkConfigurationArgs(
            subnets=network.subnet_ids,
            security_groups=[network.security_group_id],
            assign_public_ip=True,
        ),
        enable_execute_command=False,
        propagate_tags="SERVICE",
        wait_for_steady_state=False,
        tags=tags,
        # With a schedule, the running count belongs to the schedule, not to deploys.
        opts=pulumi.ResourceOptions(ignore_changes=["desiredCount"] if scheduled else None),
    )

    if scheduled:
        resource_id = pulumi.Output.concat("service/", cluster.name, "/", service.name)
        target = aws.appautoscaling.Target(
            "bot",
            service_namespace="ecs",
            scalable_dimension="ecs:service:DesiredCount",
            resource_id=resource_id,
            min_capacity=0,
            max_capacity=1,
            # The scheduled actions below move these limits; a deploy must not move them back.
            opts=pulumi.ResourceOptions(ignore_changes=["minCapacity", "maxCapacity"]),
        )
        for name, (hour, minute), count in (
            ("bot-start", settings.start_time, 1),
            ("bot-stop", settings.stop_time, 0),
        ):
            aws.appautoscaling.ScheduledAction(
                name,
                name=f"{prefix}-{name}",
                service_namespace=target.service_namespace,
                scalable_dimension=target.scalable_dimension,
                resource_id=target.resource_id,
                schedule=f"cron({minute} {hour} ? * MON-FRI *)",
                timezone="America/New_York",
                scalable_target_action=aws.appautoscaling.ScheduledActionScalableTargetActionArgs(
                    min_capacity=count, max_capacity=count
                ),
            )
    return Bot(
        cluster_name=cluster.name, service_name=service.name, log_group=logs.name, image=image
    )
