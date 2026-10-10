"""What the Pulumi program declares, checked against provider mocks."""

from __future__ import annotations

import json
import re
import shlex
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import ACCOUNT, REGION, deploy
from traider.config import Config, ResearchSettings
from traider.strategy import create_strategy

SERVICE = "aws:ecs/service:Service"
TASK = "aws:ecs/taskDefinition:TaskDefinition"
FUNCTION = "aws:lambda/function:Function"
PARAMETER = "aws:ssm/parameter:Parameter"
SECRET = "aws:secretsmanager/secret:Secret"
ROLE = "aws:iam/role:Role"
ROLE_POLICY = "aws:iam/rolePolicy:RolePolicy"
TABLE = "aws:dynamodb/table:Table"
TOPIC = "aws:sns/topic:Topic"
LOG_GROUP = "aws:cloudwatch/logGroup:LogGroup"
IMAGE = "docker-build:index:Image"
REPOSITORY = "aws:ecr/repository:Repository"
SCALING_TARGET = "aws:appautoscaling/target:Target"
SCHEDULED_ACTION = "aws:appautoscaling/scheduledAction:ScheduledAction"


def container(deployment) -> dict:
    (definition,) = json.loads(deployment.one(TASK).inputs["containerDefinitions"])
    return definition


def environment(deployment) -> dict[str, str]:
    return {item["name"]: item["value"] for item in container(deployment)["environment"]}


def actions(statements) -> set[str]:
    found: set[str] = set()
    for statement in statements:
        action = statement["Action"]
        found.update([action] if isinstance(action, str) else action)
    return found


def resources(statements) -> set[str]:
    found: set[str] = set()
    for statement in statements:
        resource = statement["Resource"]
        found.update([resource] if isinstance(resource, str) else resource)
    return found


# --- only ever one bot ----------------------------------------------------------------


def test_service_runs_at_most_one_task(paper):
    service = paper.one(SERVICE).inputs
    assert service["desiredCount"] == 1


def test_deploys_stop_the_old_task_before_starting_the_new_one(paper):
    service = paper.one(SERVICE).inputs
    assert service["deploymentMinimumHealthyPercent"] == 0
    assert service["deploymentMaximumPercent"] == 100


def test_autoscaling_can_never_run_two_tasks(paper):
    target = paper.one("aws:appautoscaling/target:Target").inputs
    assert (target["minCapacity"], target["maxCapacity"]) == (0, 1)
    for action in paper.of("aws:appautoscaling/scheduledAction:ScheduledAction"):
        assert action.inputs["scalableTargetAction"]["maxCapacity"] <= 1


def test_service_runs_the_bot_task_on_fargate_in_its_own_cluster(paper):
    service = paper.one(SERVICE).inputs
    assert service["cluster"] == paper.one("aws:ecs/cluster:Cluster").arn
    assert service["taskDefinition"] == paper.one(TASK).arn
    assert service["launchType"] == "FARGATE"


# --- market-hours schedule ------------------------------------------------------------


def test_bot_runs_on_a_new_york_weekday_schedule_by_default(paper):
    scheduled = {
        a.name: a.inputs for a in paper.of("aws:appautoscaling/scheduledAction:ScheduledAction")
    }
    assert set(scheduled) == {"bot-start", "bot-stop"}
    start, stop = scheduled["bot-start"], scheduled["bot-stop"]
    assert start["schedule"] == "cron(0 9 ? * MON-FRI *)"
    assert stop["schedule"] == "cron(30 16 ? * MON-FRI *)"
    assert start["timezone"] == stop["timezone"] == "America/New_York"
    assert start["scalableTargetAction"] == {"minCapacity": 1, "maxCapacity": 1}
    assert stop["scalableTargetAction"] == {"minCapacity": 0, "maxCapacity": 0}


def test_schedule_acts_on_this_service(paper):
    target = paper.one(SCALING_TARGET).inputs
    assert target["serviceNamespace"] == "ecs"
    assert target["scalableDimension"] == "ecs:service:DesiredCount"
    assert target["resourceId"] == "service/traider-dev/traider-dev-bot"
    for action in paper.of(SCHEDULED_ACTION):
        for key in ("serviceNamespace", "scalableDimension", "resourceId"):
            assert action.inputs[key] == target[key]


def test_scheduled_scaling_is_not_undone_by_the_next_deploy(paper):
    assert "desiredCount" in paper.one(SERVICE).ignore_changes
    # The schedule moves these limits itself; a deploy must not fight it.
    assert {"minCapacity", "maxCapacity"} <= set(paper.one(SCALING_TARGET).ignore_changes)


def test_always_on_has_no_schedule(live):
    assert live.of("aws:appautoscaling/scheduledAction:ScheduledAction") == []
    assert live.of("aws:appautoscaling/target:Target") == []
    assert "desiredCount" not in live.one(SERVICE).ignore_changes


def test_schedule_times_can_be_changed():
    custom = deploy({"startTime": "08:45", "stopTime": "16:05"})
    schedules = {
        a.name: a.inputs["schedule"]
        for a in custom.of("aws:appautoscaling/scheduledAction:ScheduledAction")
    }
    assert schedules == {
        "bot-start": "cron(45 8 ? * MON-FRI *)",
        "bot-stop": "cron(5 16 ? * MON-FRI *)",
    }


@pytest.mark.parametrize("bad", ["9", "25:00", "09:60", "nine"])
def test_malformed_schedule_time_is_rejected(bad):
    with pytest.raises(Exception, match="startTime"):
        deploy({"startTime": bad})


# --- network --------------------------------------------------------------------------


def test_nothing_can_connect_in_to_the_bot(paper):
    group = paper.one("aws:ec2/securityGroup:SecurityGroup").inputs
    assert not group.get("ingress")
    assert paper.of("aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule") == []
    assert paper.of("aws:ec2/securityGroupRule:SecurityGroupRule") == []


def test_bot_may_only_make_https_connections_out(paper):
    (rule,) = paper.one("aws:ec2/securityGroup:SecurityGroup").inputs["egress"]
    assert (rule["protocol"], rule["fromPort"], rule["toPort"]) == ("tcp", 443, 443)
    assert rule["cidrBlocks"] == ["0.0.0.0/0"]


def test_task_gets_a_public_address_instead_of_a_nat_gateway(paper):
    network = paper.one(SERVICE).inputs["networkConfiguration"]
    assert network["assignPublicIp"] is True
    assert len(network["subnets"]) == 2
    assert paper.of("aws:ec2/natGateway:NatGateway") == []


def test_task_sits_in_this_stacks_subnets_behind_its_locked_down_security_group(paper):
    network = paper.one(SERVICE).inputs["networkConfiguration"]
    subnets = paper.of("aws:ec2/subnet:Subnet")
    assert set(network["subnets"]) == {subnet.id for subnet in subnets}
    assert network["securityGroups"] == [paper.one("aws:ec2/securityGroup:SecurityGroup").id]
    vpc = paper.one("aws:ec2/vpc:Vpc").id
    assert {subnet.inputs["vpcId"] for subnet in subnets} == {vpc}
    assert len({subnet.inputs["availabilityZone"] for subnet in subnets}) == 2


def test_subnets_route_to_the_internet_gateway(paper):
    table = paper.one("aws:ec2/routeTable:RouteTable")
    (route,) = table.inputs["routes"]
    assert route["cidrBlock"] == "0.0.0.0/0"
    assert route["gatewayId"] == paper.one("aws:ec2/internetGateway:InternetGateway").id
    associations = paper.of("aws:ec2/routeTableAssociation:RouteTableAssociation")
    assert {a.inputs["subnetId"] for a in associations} == {
        subnet.id for subnet in paper.of("aws:ec2/subnet:Subnet")
    }
    assert {a.inputs["routeTableId"] for a in associations} == {table.id}


def test_no_load_balancer_and_no_exec_into_the_container(paper):
    service = paper.one(SERVICE).inputs
    assert not service.get("loadBalancers")
    assert not service.get("enableExecuteCommand")


# --- the task -------------------------------------------------------------------------


def test_task_is_small_fargate_on_arm(paper):
    task = paper.one(TASK).inputs
    assert task["requiresCompatibilities"] == ["FARGATE"]
    assert task["networkMode"] == "awsvpc"
    assert (task["cpu"], task["memory"]) == ("256", "512")
    assert task["runtimePlatform"] == {"cpuArchitecture": "ARM64", "operatingSystemFamily": "LINUX"}


def test_image_is_built_for_the_tasks_architecture_and_pinned_by_digest(paper):
    image = paper.one(IMAGE)
    repository = paper.one(REPOSITORY).outputs["repositoryUrl"]
    assert image.inputs["platforms"] == ["linux/arm64"]
    assert image.inputs["push"] is True
    assert image.inputs["tags"] == [f"{repository}:latest"]
    # repository@digest is the form ECS documents, and it changes whenever the image does.
    assert container(paper)["image"] == f"{repository}@{image.outputs['digest']}"


def test_image_is_built_from_this_repositorys_dockerfile(paper):
    image = paper.one(IMAGE).inputs
    root = Path(__file__).resolve().parents[2]
    assert Path(image["context"]["location"]) == root
    assert Path(image["dockerfile"]["location"]) == root / "Dockerfile"
    assert (root / "Dockerfile").is_file()


def test_registry_login_is_for_this_repository_and_not_readable_in_state(paper):
    image = paper.one(IMAGE)
    (registry,) = image.inputs["registries"]
    assert registry["address"] == paper.one(REPOSITORY).outputs["repositoryUrl"]
    assert (registry["username"], registry["password"]) == ("AWS", "ecr-password")
    assert "ecr-password" not in json.dumps(image.readable)


def test_old_images_are_cleaned_up_but_plenty_are_kept(paper):
    policy = json.loads(paper.one("aws:ecr/lifecyclePolicy:LifecyclePolicy").inputs["policy"])
    (rule,) = policy["rules"]
    assert rule["action"] == {"type": "expire"}
    assert rule["selection"]["countType"] == "imageCountMoreThan"
    assert rule["selection"]["countNumber"] >= 30  # one push can count as several images


def test_x86_can_be_chosen_for_both_the_image_and_the_task():
    x86 = deploy({"cpuArchitecture": "X86_64"})
    assert x86.one("docker-build:index:Image").inputs["platforms"] == ["linux/amd64"]
    assert x86.one(TASK).inputs["runtimePlatform"]["cpuArchitecture"] == "X86_64"


def test_a_prebuilt_image_skips_the_docker_build():
    prebuilt = deploy({"image": "123456789012.dkr.ecr.us-west-2.amazonaws.com/traider@sha256:ff"})
    assert prebuilt.of("docker-build:index:Image") == []
    assert container(prebuilt)["image"].endswith("@sha256:ff")


def test_container_has_a_liveness_check_and_time_to_shut_down(paper):
    definition = container(paper)
    assert definition["healthCheck"]["command"] == ["CMD", "python", "-m", "traider.health"]
    assert definition["stopTimeout"] == 120  # the most Fargate allows: time to cancel orders
    assert definition["essential"] is True
    assert definition["command"] == ["run"]


def test_logs_go_to_a_log_group_with_retention(paper):
    log = container(paper)["logConfiguration"]
    assert log["logDriver"] == "awslogs"
    assert log["options"]["awslogs-region"] == REGION
    groups = {g.inputs["name"]: g.inputs for g in paper.of(LOG_GROUP)}
    assert groups[log["options"]["awslogs-group"]]["retentionInDays"] == 30


def test_function_logs_are_kept_as_long_as_the_bots(paper):
    groups = {g.inputs["name"]: g.inputs for g in paper.of(LOG_GROUP)}
    for name in ("auth", "watchdog"):
        function = paper.one(FUNCTION, name).inputs["name"]
        assert groups[f"/aws/lambda/{function}"]["retentionInDays"] == 30


def test_log_retention_can_be_changed():
    custom = deploy({"logRetentionDays": 7})
    assert [g.inputs["retentionInDays"] for g in custom.of(LOG_GROUP)] == [7, 7, 7]


def test_task_runs_as_the_bot_role_and_is_started_with_the_execution_role(paper):
    task = paper.one(TASK).inputs
    assert task["taskRoleArn"] == paper.one(ROLE, "bot-task").arn
    assert task["executionRoleArn"] == paper.one(ROLE, "bot-execution").arn


# --- what the bot is told -------------------------------------------------------------


def test_paper_is_the_default_mode(paper):
    assert environment(paper)["TRAIDER_TRADING_MODE"] == "paper"


def test_bot_is_wired_to_the_resources_this_stack_creates(paper):
    env = environment(paper)
    assert env["TRAIDER_SYMBOLS"] == "SPY,QQQ"
    assert env["TRAIDER_CONTROL_PARAM"] == "/traider-dev/control"
    assert env["TRAIDER_STATE_TABLE"] == paper.one(TABLE, "state").inputs["name"]
    assert env["TRAIDER_SCHWAB_APP_SECRET_ID"].startswith("arn:aws:secretsmanager:")
    assert env["TRAIDER_SCHWAB_TOKEN_SECRET_ID"].startswith("arn:aws:secretsmanager:")
    assert env["TRAIDER_SCHWAB_APP_SECRET_ID"] != env["TRAIDER_SCHWAB_TOKEN_SECRET_ID"]
    assert env["TRAIDER_ALERT_TOPIC_ARN"].startswith("arn:aws:sns:")


def test_strategy_and_risk_settings_reach_the_bot_as_json():
    custom = deploy(
        {
            "strategyParams": {"fast": 3, "slow": 9},
            "risk": {"max_order_usd": 250, "max_orders_per_day": 6},
        }
    )
    env = environment(custom)
    assert json.loads(env["TRAIDER_STRATEGY_PARAMS"]) == {"fast": 3, "slow": 9}
    assert json.loads(env["TRAIDER_RISK"]) == {"max_order_usd": 250, "max_orders_per_day": 6}


def test_optional_settings_are_left_out_when_not_configured(paper):
    env = environment(paper)
    for name in (
        "TRAIDER_FLATTEN_BEFORE_CLOSE_MIN",
        "TRAIDER_SCHWAB_ACCOUNT_LAST4",
        "TRAIDER_RISK",
        "TRAIDER_STRATEGY_PARAMS",
    ):
        assert name not in env


def test_the_sign_in_link_is_injected_as_a_secret_not_a_plain_variable(paper):
    definition = container(paper)
    assert "TRAIDER_REAUTH_URL" not in environment(paper)
    (secret,) = definition["secrets"]
    assert secret["name"] == "TRAIDER_REAUTH_URL"
    reauth = paper.one(PARAMETER, "reauth-url")
    assert reauth.inputs["type"] == "SecureString"
    assert reauth.inputs["value"] == paper.outputs["reauthUrl"]
    assert secret["valueFrom"] == reauth.arn


def test_no_secret_values_appear_in_the_task_definition(paper):
    text = json.dumps(paper.one(TASK).inputs)
    assert "generated-" not in text  # the mock value of every generated secret


# --- configuration is checked before anything is deployed -----------------------------


def test_symbols_are_required():
    with pytest.raises(Exception, match="symbols"):
        deploy({"symbols": None})


def test_live_needs_an_explicit_account():
    with pytest.raises(Exception, match="account"):
        deploy({"tradingMode": "live", "alertEmail": "ops@example.test"})


def test_live_needs_somewhere_to_send_alerts():
    with pytest.raises(Exception, match="alertEmail"):
        deploy({"tradingMode": "live", "accountLast4": "5678"})


def test_a_misspelled_risk_limit_fails_the_deploy_not_the_running_bot():
    with pytest.raises(Exception, match="max_order_usdd"):
        deploy({"risk": {"max_order_usdd": 100}})


def test_an_unknown_strategy_fails_the_deploy():
    with pytest.raises(Exception, match="sma_cross"):
        deploy({"strategy": "moonshot"})


def test_bad_strategy_parameters_fail_the_deploy():
    with pytest.raises(Exception, match="fast"):
        deploy({"strategyParams": {"fast": 9, "slow": 3}})


def test_unknown_trading_mode_fails_the_deploy():
    with pytest.raises(Exception, match="trading_mode"):
        deploy({"tradingMode": "real"})


def test_unknown_cpu_architecture_fails_the_deploy():
    with pytest.raises(Exception, match="cpuArchitecture"):
        deploy({"cpuArchitecture": "RISCV"})


# --- the control switch ---------------------------------------------------------------


def test_control_starts_at_paper_for_a_paper_stack(paper):
    control = paper.one(PARAMETER, "control").inputs
    assert (control["type"], control["value"]) == ("String", "paper")


def test_control_starts_at_halt_for_a_live_stack(live):
    assert live.one(PARAMETER, "control").inputs["value"] == "halt"


def test_control_only_accepts_its_four_values(paper):
    assert paper.one(PARAMETER, "control").inputs["allowedPattern"] == (
        "^(halt|close_only|paper|live)$"
    )


def test_a_deploy_never_resets_the_control_switch(paper):
    assert "value" in paper.one(PARAMETER, "control").ignore_changes


# --- secrets and state ----------------------------------------------------------------


def test_secret_containers_are_created_but_never_their_values(paper):
    assert {s.name for s in paper.of(SECRET)} == {"schwab-app", "schwab-token"}
    assert paper.of("aws:secretsmanager/secretVersion:SecretVersion") == []


def test_state_table_is_on_demand_with_point_in_time_recovery(paper):
    table = paper.one(TABLE, "state").inputs
    assert table["billingMode"] == "PAY_PER_REQUEST"
    assert (table["hashKey"], table["rangeKey"]) == ("pk", "sk")
    assert table["pointInTimeRecovery"] == {"enabled": True}


def test_live_state_table_is_protected_from_deletion(live, paper):
    assert live.one(TABLE, "state").inputs["deletionProtectionEnabled"] is True
    assert not paper.one(TABLE, "state").inputs.get("deletionProtectionEnabled")


def test_alert_email_is_subscribed_when_given(paper):
    subscription = paper.one("aws:sns/topicSubscription:TopicSubscription").inputs
    assert (subscription["protocol"], subscription["endpoint"]) == ("email", "ops@example.test")
    assert subscription["topic"] == paper.one(TOPIC).arn


def test_a_paper_stack_may_run_without_an_alert_address():
    assert deploy().of("aws:sns/topicSubscription:TopicSubscription") == []


# --- least privilege ------------------------------------------------------------------


def test_bot_role_has_exactly_the_access_it_needs(paper):
    statements = paper.policy("bot-task")
    assert actions(statements) == {
        "secretsmanager:GetSecretValue",
        "secretsmanager:PutSecretValue",
        "ssm:GetParameter",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query",
        "sns:Publish",
    }


def test_each_bot_permission_names_exactly_the_resource_it_is_for(paper):
    granted = {s["Sid"]: s["Resource"] for s in paper.policy("bot-task")}
    assert granted == {
        "ReadSchwabSecrets": [
            paper.one(SECRET, "schwab-app").arn,
            paper.one(SECRET, "schwab-token").arn,
        ],
        "SaveRotatedRefreshToken": paper.one(SECRET, "schwab-token").arn,
        "ReadControlSwitch": paper.one(PARAMETER, "control").arn,
        "State": paper.one(TABLE, "state").arn,
        "Settings": paper.one(TABLE, "settings").arn,
        "Alerts": paper.one(TOPIC).arn,
    }


def test_each_policy_is_attached_to_its_own_role_and_nothing_else_is(paper):
    for name in ("bot-task", "bot-execution", "auth", "watchdog"):
        assert paper.one(ROLE_POLICY, name).inputs["role"] == paper.one(ROLE, name).id
    assert len(paper.of(ROLE_POLICY)) == 4
    # The one AWS-managed policy: what ECS needs to pull the image and write logs.
    attachment = paper.one("aws:iam/rolePolicyAttachment:RolePolicyAttachment").inputs
    assert attachment["role"] == paper.one(ROLE, "bot-execution").outputs["name"]
    assert attachment["policyArn"] == (
        "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
    )


def test_each_function_runs_as_its_own_role(paper):
    for name in ("auth", "watchdog"):
        assert paper.one(FUNCTION, name).inputs["role"] == paper.one(ROLE, name).arn


def test_functions_may_only_write_to_their_own_log_group(paper):
    groups = {g.inputs["name"]: g for g in paper.of(LOG_GROUP)}
    for name in ("auth", "watchdog"):
        logs = next(s for s in paper.policy(name) if s["Sid"] == "Logs")
        group = groups[f"/aws/lambda/{paper.one(FUNCTION, name).inputs['name']}"]
        assert logs["Resource"] == f"{group.arn}:*"
        assert set(logs["Action"]) == {"logs:CreateLogStream", "logs:PutLogEvents"}


def test_no_policy_uses_a_wildcard_resource_or_action(paper):
    for policy in paper.of("aws:iam/rolePolicy:RolePolicy"):
        for statement in json.loads(policy.inputs["policy"])["Statement"]:
            assert statement["Effect"] == "Allow"
            for value in resources([statement]) | actions([statement]):
                assert "*" not in value.replace(":*", ""), f"{policy.name}: {value}"
                assert value.count("*") <= 1, f"{policy.name}: {value}"


def test_bot_can_read_both_secrets_but_write_only_the_token(paper):
    statements = paper.policy("bot-task")
    env = environment(paper)
    read = next(s for s in statements if s["Action"] == "secretsmanager:GetSecretValue")
    write = next(s for s in statements if s["Action"] == "secretsmanager:PutSecretValue")
    assert set(read["Resource"]) == {
        env["TRAIDER_SCHWAB_APP_SECRET_ID"],
        env["TRAIDER_SCHWAB_TOKEN_SECRET_ID"],
    }
    assert write["Resource"] == env["TRAIDER_SCHWAB_TOKEN_SECRET_ID"]


def test_bot_cannot_change_its_own_control_switch(paper):
    assert "ssm:PutParameter" not in actions(paper.policy("bot-task"))


def test_execution_role_can_only_fetch_the_sign_in_link_parameter(paper):
    statements = paper.policy("bot-execution")
    assert actions(statements) == {"ssm:GetParameters"}
    assert resources(statements) == {paper.one(PARAMETER, "reauth-url").arn}


def test_sign_in_function_can_read_the_app_secret_and_write_the_token(paper):
    statements = paper.policy("auth")
    env = environment(paper)
    grants = {s["Action"]: s["Resource"] for s in statements if s["Sid"] != "Logs"}
    alerts = grants.pop("sns:Publish")
    assert alerts == env["TRAIDER_ALERT_TOPIC_ARN"]
    assert grants == {
        "secretsmanager:GetSecretValue": env["TRAIDER_SCHWAB_APP_SECRET_ID"],
        "secretsmanager:PutSecretValue": env["TRAIDER_SCHWAB_TOKEN_SECRET_ID"],
    }


def test_watchdog_can_only_read_the_token_and_send_alerts(paper):
    granted = {s["Action"]: s["Resource"] for s in paper.policy("watchdog") if s["Sid"] != "Logs"}
    assert granted == {
        "secretsmanager:GetSecretValue": paper.one(SECRET, "schwab-token").arn,
        "sns:Publish": paper.one(TOPIC).arn,
    }


def test_roles_can_only_be_assumed_by_their_own_service(paper):
    principals = {
        role.name: json.loads(role.inputs["assumeRolePolicy"])["Statement"][0]["Principal"][
            "Service"
        ]
        for role in paper.of("aws:iam/role:Role")
    }
    assert principals == {
        "bot-task": "ecs-tasks.amazonaws.com",
        "bot-execution": "ecs-tasks.amazonaws.com",
        "auth": "lambda.amazonaws.com",
        "watchdog": "lambda.amazonaws.com",
    }


# --- the sign-in endpoint -------------------------------------------------------------


def test_sign_in_function_is_python_from_this_repository(paper):
    function = paper.one(FUNCTION, "auth").inputs
    assert function["runtime"] == "python3.13"
    assert function["handler"] == "traider.lambdas.auth.handler"
    assert function["architectures"] == ["arm64"]


def test_sign_in_function_knows_its_own_address_and_where_schwab_returns_to(paper):
    variables = paper.one(FUNCTION, "auth").inputs["environment"]["variables"]
    endpoint = "https://abc123.execute-api.us-west-2.amazonaws.com"
    assert variables["PUBLIC_URL"] == endpoint
    assert variables["CALLBACK_URL"] == f"{endpoint}/callback"
    env = environment(paper)
    assert variables["APP_SECRET_ID"] == env["TRAIDER_SCHWAB_APP_SECRET_ID"]
    assert variables["TOKEN_SECRET_ID"] == env["TRAIDER_SCHWAB_TOKEN_SECRET_ID"]
    assert variables["ALERT_TOPIC_ARN"] == env["TRAIDER_ALERT_TOPIC_ARN"]
    assert variables["START_KEY"] != variables["STATE_SECRET"]


def test_callback_address_can_be_overridden_for_paste_mode():
    pasted = deploy({"schwabCallbackUrl": "https://127.0.0.1"})
    variables = pasted.one(FUNCTION, "auth").inputs["environment"]["variables"]
    assert variables["CALLBACK_URL"] == "https://127.0.0.1"
    assert pasted.outputs["callbackUrl"] == "https://127.0.0.1"


def test_only_the_three_sign_in_routes_exist(paper):
    routes = {r.inputs["routeKey"] for r in paper.of("aws:apigatewayv2/route:Route")}
    assert routes == {"GET /start", "GET /callback", "POST /exchange"}


def test_every_route_calls_the_sign_in_function(paper):
    api = paper.one("aws:apigatewayv2/api:Api")
    assert api.inputs["protocolType"] == "HTTP"
    integration = paper.one("aws:apigatewayv2/integration:Integration")
    assert integration.inputs["apiId"] == api.id
    assert integration.inputs["integrationType"] == "AWS_PROXY"
    assert integration.inputs["integrationUri"] == paper.one(FUNCTION, "auth").outputs["invokeArn"]
    # The function reads version 2.0 events; version 1.0 has a different shape.
    assert integration.inputs["payloadFormatVersion"] == "2.0"
    for route in paper.of("aws:apigatewayv2/route:Route"):
        assert route.inputs["apiId"] == api.id
        assert route.inputs["target"] == f"integrations/{integration.id}"


def test_the_api_is_served_from_the_root_of_its_address(paper):
    # PUBLIC_URL and the callback address have no stage prefix; only $default serves there.
    stage = paper.one("aws:apigatewayv2/stage:Stage").inputs
    assert stage["apiId"] == paper.one("aws:apigatewayv2/api:Api").id
    assert stage["name"] == "$default"
    assert stage["autoDeploy"] is True


def test_sign_in_endpoint_is_rate_limited(paper):
    settings = paper.one("aws:apigatewayv2/stage:Stage").inputs["defaultRouteSettings"]
    assert 0 < settings["throttlingRateLimit"] <= 5
    assert 0 < settings["throttlingBurstLimit"] <= 10


def test_only_the_api_may_invoke_the_sign_in_function(paper):
    permission = paper.one("aws:lambda/permission:Permission", "auth-api").inputs
    assert permission["principal"] == "apigateway.amazonaws.com"
    assert permission["action"] == "lambda:InvokeFunction"
    assert permission["function"] == paper.one(FUNCTION, "auth").inputs["name"]
    assert permission["sourceArn"].startswith(f"arn:aws:execute-api:{REGION}:{ACCOUNT}:abc123/")


def test_generated_keys_are_long_and_url_safe(paper):
    for password in paper.of("random:index/randomPassword:RandomPassword"):
        assert password.inputs["length"] >= 32
        assert password.inputs["special"] is False


def test_generated_keys_and_the_sign_in_link_are_encrypted_in_pulumi_state(paper):
    holders = {c.name for c in paper.created if "generated-" in json.dumps(c.inputs)}
    assert holders == {"auth", "watchdog", "reauth-url"}  # the functions and the link parameter
    for created in paper.created:
        assert "generated-" not in json.dumps(created.readable, default=str), created.name


# --- the watchdog ---------------------------------------------------------------------


def test_watchdog_runs_daily_and_is_given_the_sign_in_link(paper):
    function = paper.one(FUNCTION, "watchdog").inputs
    assert function["handler"] == "traider.lambdas.watchdog.handler"
    variables = function["environment"]["variables"]
    assert variables["REAUTH_URL"] == paper.outputs["reauthUrl"]
    assert variables["WARN_HOURS"] == "48"
    env = environment(paper)
    assert variables["TOKEN_SECRET_ID"] == env["TRAIDER_SCHWAB_TOKEN_SECRET_ID"]
    assert variables["ALERT_TOPIC_ARN"] == env["TRAIDER_ALERT_TOPIC_ARN"]
    rule = paper.one("aws:cloudwatch/eventRule:EventRule", "watchdog").inputs
    assert rule["scheduleExpression"].startswith("cron(")
    target = paper.one("aws:cloudwatch/eventTarget:EventTarget", "watchdog").inputs
    assert target["rule"] == rule["name"]
    assert target["arn"].endswith(f":Function/{function['name']}")
    permission = paper.one("aws:lambda/permission:Permission", "watchdog-schedule").inputs
    assert permission["principal"] == "events.amazonaws.com"
    assert permission["function"] == function["name"]
    assert (
        permission["sourceArn"] == paper.one("aws:cloudwatch/eventRule:EventRule", "watchdog").arn
    )


def test_watchdog_timing_can_be_changed():
    custom = deploy({"reauthWarnHours": 72, "watchdogSchedule": "cron(0 13 * * ? *)"})
    assert custom.one(FUNCTION, "watchdog").inputs["environment"]["variables"]["WARN_HOURS"] == "72"
    rule = custom.one("aws:cloudwatch/eventRule:EventRule", "watchdog").inputs
    assert rule["scheduleExpression"] == "cron(0 13 * * ? *)"


# --- Lambda package -------------------------------------------------------------------


def test_lambda_package_is_the_bots_python_source_and_nothing_else():
    from lambdas import lambda_code

    files = set(lambda_code().assets)
    assert "traider/lambdas/auth.py" in files
    assert "traider/lambdas/watchdog.py" in files
    assert "traider/schwab/oauth.py" in files
    assert "traider/__init__.py" in files
    assert all(name.startswith("traider/") and name.endswith(".py") for name in files)
    assert not any("__pycache__" in name or "/tests/" in name for name in files)


# --- outputs --------------------------------------------------------------------------


def test_outputs_tell_you_what_to_do_next(paper):
    out = paper.outputs
    assert out["callbackUrl"] == "https://abc123.execute-api.us-west-2.amazonaws.com/callback"
    assert out["reauthUrl"].startswith(
        "https://abc123.execute-api.us-west-2.amazonaws.com/start?k="
    )
    assert out["controlParameter"] == "/traider-dev/control"
    assert out["appSecretArn"].startswith("arn:aws:secretsmanager:")
    assert out["tokenSecretArn"].startswith("arn:aws:secretsmanager:")
    assert {
        "stateTable",
        "alertTopicArn",
        "clusterName",
        "serviceName",
        "logGroup",
        "localEnv",
    } <= set(out)


def test_the_sign_in_link_output_is_marked_secret(paper):
    assert "reauthUrl" in paper.secret_outputs
    assert "callbackUrl" not in paper.secret_outputs


def local_env(deployment) -> dict[str, str]:
    """The localEnv output, read the way a shell or an env-file loader reads it."""
    pairs = [shlex.split(line) for line in deployment.outputs["localEnv"].splitlines()]
    assert all(len(pair) == 1 for pair in pairs)
    return dict(pair[0].split("=", 1) for pair in pairs)


def test_local_env_output_lets_you_run_the_cli_against_the_stack(paper):
    lines = local_env(paper)
    assert lines["AWS_REGION"] == REGION
    assert lines["TRAIDER_SYMBOLS"] == "SPY,QQQ"
    assert lines["TRAIDER_SCHWAB_APP_SECRET_ID"] == paper.outputs["appSecretArn"]
    assert lines["TRAIDER_SCHWAB_TOKEN_SECRET_ID"] == paper.outputs["tokenSecretArn"]
    assert "k=" not in paper.outputs["localEnv"]  # the sign-in key stays out of it
    assert Config.from_env(lines).symbols == ("SPY", "QQQ")


def test_local_env_carries_the_bots_own_settings_so_checks_and_backtests_match_it():
    custom = deploy(
        {
            "strategyParams": {"fast": 3, "slow": 9},
            "risk": {"max_order_usd": 250, "settled_cash_only": False},
            "orderType": "MARKET",
            "accountLast4": "0042",
        }
    )
    local, deployed = Config.from_env(local_env(custom)), Config.from_env(environment(custom))
    for name in ("symbols", "strategy", "strategy_params", "risk", "order_type", "account_last4"):
        assert getattr(local, name) == getattr(deployed, name), name


def test_local_env_never_arms_live_trading_from_your_own_machine(live):
    lines = local_env(live)
    assert "TRAIDER_TRADING_MODE" not in lines
    assert "TRAIDER_CONTROL_PARAM" not in lines
    assert "TRAIDER_STATE_TABLE" not in lines
    assert Config.from_env(lines).trading_mode == "paper"
    assert lines["TRAIDER_SCHWAB_ACCOUNT_LAST4"] == "5678"


def test_local_env_leaves_the_command_line_on_its_own_callback_when_sign_in_is_hosted(paper):
    # `traider login` is the paste-the-address flow. It cannot use the hosted callback.
    assert "TRAIDER_SCHWAB_CALLBACK_URL" not in local_env(paper)


def test_local_env_uses_the_configured_callback_in_paste_mode():
    pasted = deploy({"schwabCallbackUrl": "https://127.0.0.1:8182"})
    assert local_env(pasted)["TRAIDER_SCHWAB_CALLBACK_URL"] == "https://127.0.0.1:8182"


def test_stacks_do_not_share_names(paper, live):
    assert (
        paper.one(PARAMETER, "control").inputs["name"]
        != live.one(PARAMETER, "control").inputs["name"]
    )
    assert paper.one(SERVICE).inputs["name"] != live.one(SERVICE).inputs["name"]


# --- noticing a bot that will not stay up -------------------------------------------------


def test_a_task_that_crashes_or_cannot_start_raises_an_alert(paper):
    rule = paper.one("aws:cloudwatch/eventRule:EventRule", "bot-stopped")
    pattern = json.loads(rule.inputs["eventPattern"])
    assert pattern["source"] == ["aws.ecs"]
    assert pattern["detail-type"] == ["ECS Task State Change"]
    detail = pattern["detail"]
    assert detail["clusterArn"] == [paper.one("aws:ecs/cluster:Cluster").arn]
    assert detail["lastStatus"] == ["STOPPED"]
    # A clean stop (the 16:30 schedule, a deploy) exits 0 and must not page anyone.
    assert detail["$or"] == [
        {"stopCode": ["TaskFailedToStart"]},
        {"containers": {"exitCode": [{"anything-but": 0}]}},
    ]
    target = paper.one("aws:cloudwatch/eventTarget:EventTarget", "bot-stopped").inputs
    assert target["rule"] == rule.inputs["name"]
    assert target["arn"] == paper.one(TOPIC).arn
    assert "[traider]" in target["inputTransformer"]["inputTemplate"]


def test_only_the_bot_its_functions_and_that_alarm_may_publish_alerts(paper):
    policy = paper.one("aws:sns/topicPolicy:TopicPolicy").inputs
    topic = paper.one(TOPIC).arn
    assert policy["arn"] == topic
    (statement,) = json.loads(policy["policy"])["Statement"]
    assert statement["Effect"] == "Allow"
    assert statement["Principal"] == {"Service": "events.amazonaws.com"}
    assert statement["Action"] == "sns:Publish"
    assert statement["Resource"] == topic
    rule = paper.one("aws:cloudwatch/eventRule:EventRule", "bot-stopped").arn
    assert statement["Condition"] == {"ArnEquals": {"aws:SourceArn": rule}}


def test_old_task_definitions_are_kept_so_a_failed_deploy_can_roll_back(paper):
    assert paper.one(TASK).inputs["skipDestroy"] is True


def test_the_bot_stops_after_it_starts():
    with pytest.raises(Exception, match="stopTime"):
        deploy({"startTime": "16:30", "stopTime": "09:00"})


def test_option_settings_reach_the_bot():
    deployed = deploy(
        {"risk": {"allow_options": True}, "optionChainDays": 30, "optionChainStrikes": 12}
    )
    checked = Config.from_env(environment(deployed))
    assert checked.risk.allow_options is True
    assert (checked.option_chain_days, checked.option_chain_strikes) == (30, 12)


def test_options_are_off_unless_asked_for(paper):
    assert Config.from_env(environment(paper)).risk.allow_options is False


def test_a_bad_option_setting_fails_the_preview():
    with pytest.raises(Exception, match="option_chain_days"):
        deploy({"optionChainDays": 0})


# --- the example configuration ----------------------------------------------------------

# Settings the example shows with a value that is not the default.
NOT_DEFAULTS = {
    "flattenBeforeCloseMin",
    "accountLast4",
    "accountHash",
    "alertEmail",
    "schwabCallbackUrl",
    "image",
}


def example_config(*, everything: bool) -> dict[str, Any]:
    """Pulumi.example.yaml as stack configuration, optionally with every setting switched on."""
    text = (Path(__file__).resolve().parents[1] / "Pulumi.example.yaml").read_text()
    if everything:
        text = re.sub(r"^  # (traider:)", r"  \1", text, flags=re.MULTILINE)
        text = re.sub(r"^  #   (\S)", r"    \1", text, flags=re.MULTILINE)
    values = yaml.safe_load(text)["config"]
    return {key.removeprefix("traider:"): v for key, v in values.items() if key != "aws:region"}


def test_example_configuration_deploys_as_it_is():
    assert set(example_config(everything=False)) == {"pinnedSymbols"}
    assert deploy(example_config(everything=False)).of(SERVICE)


def test_example_configuration_deploys_with_every_setting_switched_on():
    config = example_config(everything=True)
    assert set(config) >= NOT_DEFAULTS
    full = deploy(config)
    assert environment(full)["TRAIDER_FLATTEN_BEFORE_CLOSE_MIN"] == "10"
    assert environment(full)["TRAIDER_SCHWAB_ACCOUNT_LAST4"] == "1234"


def test_example_configuration_shows_the_real_defaults():
    config = example_config(everything=True)
    spelled_out = deploy({k: v for k, v in config.items() if k not in NOT_DEFAULTS})
    minimal = deploy({"pinnedSymbols": config["pinnedSymbols"]})
    # The bot ends up with the same settings whether or not they are written down ...
    written, unwritten = (Config.from_env(environment(d)) for d in (spelled_out, minimal))
    assert written.model_copy(update={"strategy_params": {}}) == unwritten
    one, other = (
        create_strategy(c.strategy, c.symbols, c.strategy_params) for c in (written, unwritten)
    )
    assert (one.fast, one.slow, one.position_usd) == (other.fast, other.slow, other.position_usd)
    # ... and so does everything around it.
    around = [
        {(c.type, c.name): c.inputs for c in deployment.created if c.type != TASK}
        for deployment in (spelled_out, minimal)
    ]
    assert around[0] == around[1]


# --- the documentation ------------------------------------------------------------------

DOCS = [
    Path(__file__).resolve().parents[2] / "README.md",
    Path(__file__).resolve().parents[2] / "docs" / "runbook.md",
]


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_stack_output_the_docs_tell_you_to_read_exists(doc, paper):
    named = set(re.findall(r"pulumi stack output (\w+)", doc.read_text(encoding="utf-8")))
    assert named, "the docs should use at least one stack output"
    researched = deploy({"research": True, "researchJobs": True, "pinnedSymbols": []})
    assert named <= set(paper.outputs) | set(researched.outputs)


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_setting_the_docs_mention_is_in_the_example_configuration(doc):
    named = set(re.findall(r"traider:([A-Za-z0-9]+)", doc.read_text(encoding="utf-8")))
    assert named, "the docs should mention at least one setting"
    assert named <= set(example_config(everything=True))


def test_resource_names_the_docs_spell_out_match_what_the_stack_creates(paper):
    # The README shows the kill switch command with the parameter's name written out.
    readme = DOCS[0].read_text(encoding="utf-8")
    assert f"--name {paper.outputs['controlParameter']} --value halt --overwrite" in readme


# --- versioned settings ---------------------------------------------------------------


def test_settings_table_is_on_demand_with_point_in_time_recovery(paper):
    table = paper.one(TABLE, "settings").inputs
    assert table["name"] == "traider-dev-settings"
    assert table["billingMode"] == "PAY_PER_REQUEST"
    assert (table["hashKey"], table["rangeKey"]) == ("pk", "sk")
    assert table["pointInTimeRecovery"] == {"enabled": True}


def test_live_settings_table_is_protected_from_deletion(live, paper):
    assert live.one(TABLE, "settings").inputs["deletionProtectionEnabled"] is True
    assert not paper.one(TABLE, "settings").inputs.get("deletionProtectionEnabled")


def test_bot_is_told_where_its_settings_live(paper):
    assert (
        environment(paper)["TRAIDER_SETTINGS_TABLE"] == paper.one(TABLE, "settings").inputs["name"]
    )
    assert paper.outputs["settingsTable"] == paper.one(TABLE, "settings").inputs["name"]


def test_bot_can_read_and_add_settings_versions_but_not_delete_or_rewrite_them(paper):
    (statement,) = [s for s in paper.policy("bot-task") if s["Sid"] == "Settings"]
    assert set(statement["Action"]) == {"dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem"}


def test_local_env_lets_the_cli_edit_settings(paper):
    assert local_env(paper)["TRAIDER_SETTINGS_TABLE"] == paper.one(TABLE, "settings").inputs["name"]


# --- the opt-in research table ------------------------------------------------------------


def test_research_is_off_by_default(paper):
    assert [t for t in paper.of(TABLE) if t.name == "research"] == []
    assert "TRAIDER_RESEARCH_TABLE" not in environment(paper)
    assert "Research" not in {s["Sid"] for s in paper.policy("bot-task")}
    assert "researchTable" not in paper.outputs
    assert "TRAIDER_RESEARCH_TABLE" not in local_env(paper)


def test_research_on_creates_an_indexed_table_the_bot_can_only_read():
    on = deploy({"research": True, "pinnedSymbols": []})
    table = on.one(TABLE, "research").inputs
    assert table["name"] == "traider-dev-research"
    assert (table["hashKey"], table["rangeKey"]) == ("pk", "sk")
    (index,) = table["globalSecondaryIndexes"]
    assert (index["name"], index["hashKey"], index["rangeKey"]) == ("gsi1", "gsi1pk", "gsi1sk")
    assert index["projectionType"] == "ALL"
    assert environment(on)["TRAIDER_RESEARCH_TABLE"] == table["name"]
    (statement,) = [s for s in on.policy("bot-task") if s["Sid"] == "Research"]
    assert set(statement["Action"]) == {"dynamodb:Query", "dynamodb:GetItem"}
    assert statement["Resource"] == on.one(TABLE, "research").arn  # the table, not its index
    assert on.outputs["researchTable"] == table["name"]


def test_the_research_table_has_point_in_time_recovery_and_live_deletion_protection():
    paper_on = deploy({"research": True})
    live_on = deploy(
        {
            "research": True,
            "tradingMode": "live",
            "accountLast4": "5678",
            "alertEmail": "ops@example.test",
        },
        stack="prod",
    )
    assert paper_on.one(TABLE, "research").inputs["pointInTimeRecovery"] == {"enabled": True}
    assert live_on.one(TABLE, "research").inputs["deletionProtectionEnabled"] is True
    assert not paper_on.one(TABLE, "research").inputs.get("deletionProtectionEnabled")


def test_local_env_lets_the_cli_seed_and_show_research():
    on = deploy({"research": True})
    assert local_env(on)["TRAIDER_RESEARCH_TABLE"] == on.one(TABLE, "research").inputs["name"]


def test_a_live_stack_with_research_on_still_keeps_live_trading_out_of_local_env():
    live_on = deploy(
        {
            "research": True,
            "tradingMode": "live",
            "accountLast4": "5678",
            "alertEmail": "ops@example.test",
        }
    )
    lines = local_env(live_on)
    assert lines["TRAIDER_RESEARCH_TABLE"] == live_on.one(TABLE, "research").inputs["name"]
    assert "TRAIDER_TRADING_MODE" not in lines
    assert "TRAIDER_CONTROL_PARAM" not in lines
    assert "TRAIDER_STATE_TABLE" not in lines
    assert Config.from_env(lines).trading_mode == "paper"


def test_research_with_no_pinned_symbols_is_a_configuration_the_bot_accepts():
    on = deploy({"research": True, "pinnedSymbols": []})
    for env in (environment(on), local_env(on)):
        checked = Config.from_env(env)
        assert checked.symbols == ()
        assert checked.research_table == on.one(TABLE, "research").inputs["name"]


def test_research_with_pinned_symbols_keeps_them():
    on = deploy({"research": True, "pinnedSymbols": ["spy"]})
    assert Config.from_env(environment(on)).symbols == ("SPY",)


def test_without_research_a_pinned_symbol_is_required():
    with pytest.raises(Exception, match="symbol"):
        deploy({"pinnedSymbols": []})
    with pytest.raises(Exception, match="symbol"):
        deploy({"pinnedSymbols": None})


def test_pinned_symbols_and_symbols_cannot_both_be_set():
    with pytest.raises(Exception, match="pinnedSymbols"):
        deploy({"pinnedSymbols": ["SPY"], "symbols": ["QQQ"]})


def test_pinned_symbols_is_the_same_setting_as_symbols():
    pinned = deploy({"pinnedSymbols": ["SPY", "QQQ"]})
    assert environment(pinned)["TRAIDER_SYMBOLS"] == "SPY,QQQ"


def test_research_settings_reach_the_bot_as_json():
    on = deploy({"research": True, "researchSettings": {"min_score": 75}})
    assert json.loads(environment(on)["TRAIDER_RESEARCH"]) == {"min_score": 75}
    assert Config.from_env(environment(on)).research.min_score == 75


def test_a_bad_research_setting_fails_the_preview():
    with pytest.raises(Exception, match="min_score"):
        deploy({"research": True, "researchSettings": {"min_score": 500}})
    with pytest.raises(Exception, match="research"):
        deploy({"research": True, "researchSettings": {"nope": 1}})


def test_research_settings_are_not_sent_when_none_are_given():
    assert "TRAIDER_RESEARCH" not in environment(deploy({"research": True}))


def test_example_configuration_shows_research_off_and_the_real_research_defaults():
    config = example_config(everything=True)
    assert config["research"] is False
    defaults = {
        name: float(value) if isinstance(value, Decimal) else value
        for name, value in ResearchSettings().model_dump().items()
    }
    assert config["researchSettings"] == defaults
