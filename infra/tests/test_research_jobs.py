"""The scheduled research run: opt-in, least privilege, on time, and loud when it fails."""

from __future__ import annotations

import json

import pytest
from conftest import ACCOUNT, REGION, deploy
from traider.config import Config

TASK = "aws:ecs/taskDefinition:TaskDefinition"
ROLE = "aws:iam/role:Role"
ROLE_POLICY = "aws:iam/rolePolicy:RolePolicy"
SECRET = "aws:secretsmanager/secret:Secret"
TABLE = "aws:dynamodb/table:Table"
TOPIC = "aws:sns/topic:Topic"
BUCKET = "aws:s3/bucket:Bucket"
SCHEDULE = "aws:scheduler/schedule:Schedule"
RULE = "aws:cloudwatch/eventRule:EventRule"
CLUSTER = "aws:ecs/cluster:Cluster"
QUEUE = "aws:sqs/queue:Queue"
ALARM = "aws:cloudwatch/metricAlarm:MetricAlarm"
TOPIC_POLICY = "aws:sns/topicPolicy:TopicPolicy"
LOG_GROUP = "aws:cloudwatch/logGroup:LogGroup"


@pytest.fixture(scope="module")
def jobs():
    return deploy({"research": True, "researchJobs": True, "alertEmail": "ops@example.test"})


def container(deployment) -> dict:
    (definition,) = json.loads(deployment.one(TASK, "research").inputs["containerDefinitions"])
    return definition


def environment(deployment) -> dict[str, str]:
    return {item["name"]: item["value"] for item in container(deployment)["environment"]}


def statements(deployment, name) -> dict[str, dict]:
    return {s["Sid"]: s for s in deployment.policy(name)}


# --- opt-in ---------------------------------------------------------------------------


def test_research_jobs_are_off_by_default(paper):
    assert paper.of(BUCKET) == []
    assert paper.of(SCHEDULE) == []
    assert [t for t in paper.of(TASK) if t.name == "research"] == []
    assert {s.name for s in paper.of(SECRET)} == {"schwab-app", "schwab-token"}
    assert "finnhubSecretArn" not in paper.outputs


def test_research_on_alone_creates_no_jobs():
    assert deploy({"research": True}).of(SCHEDULE) == []


@pytest.mark.parametrize("config", [{}, {"research": True}], ids=["default", "research-only"])
def test_without_the_jobs_there_is_no_dead_letter_queue_or_new_alarm(config):
    deployment = deploy({**config, "alertEmail": "ops@example.test"})
    assert deployment.of(QUEUE) == []
    assert deployment.of(ALARM) == []
    policy = json.loads(deployment.one(TOPIC_POLICY).inputs["policy"])
    assert [s["Sid"] for s in policy["Statement"]] == ["TaskStoppedAlarm"]


def test_research_jobs_without_research_are_refused():
    with pytest.raises(Exception, match="researchJobs needs traider:research"):
        deploy({"researchJobs": True})


# --- the trail bucket --------------------------------------------------------------------


def test_the_trail_bucket_is_private_encrypted_tls_only_and_expiring(jobs):
    bucket = jobs.one(BUCKET, "research-trail")
    assert bucket.inputs["bucket"] == f"traider-dev-research-trail-{ACCOUNT}"
    block = jobs.one("aws:s3/bucketPublicAccessBlock:BucketPublicAccessBlock").inputs
    assert block["bucket"] == bucket.id
    assert all(
        block[k]
        for k in (
            "blockPublicAcls",
            "blockPublicPolicy",
            "ignorePublicAcls",
            "restrictPublicBuckets",
        )
    )
    sse = jobs.one(
        "aws:s3/bucketServerSideEncryptionConfiguration:BucketServerSideEncryptionConfiguration"
    ).inputs
    (rule,) = sse["rules"]
    assert rule["applyServerSideEncryptionByDefault"]["sseAlgorithm"] == "AES256"
    lifecycle = jobs.one("aws:s3/bucketLifecycleConfiguration:BucketLifecycleConfiguration").inputs
    (expire,) = lifecycle["rules"]
    assert (expire["status"], expire["expiration"]["days"]) == ("Enabled", 400)
    policy = json.loads(jobs.one("aws:s3/bucketPolicy:BucketPolicy").inputs["policy"])
    (deny,) = policy["Statement"]
    assert (deny["Effect"], deny["Principal"], deny["Action"]) == ("Deny", "*", "s3:*")
    assert deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}
    assert deny["Resource"] == [bucket.arn, f"{bucket.arn}/*"]


@pytest.mark.parametrize(
    "stack",
    ["Dev", "dev_1", "a-stack-name-long-enough-to-overflow-63"],
    ids=["uppercase", "underscore", "too-long"],
)
def test_a_stack_name_s3_would_refuse_fails_at_load(stack):
    with pytest.raises(Exception, match="research trail bucket would be named"):
        deploy({"research": True, "researchJobs": True}, stack=stack)


def test_the_longest_allowed_stack_name_still_deploys():
    stack = "s" * (35 - len("traider-"))  # the bucket name is then exactly 63 characters
    deployment = deploy({"research": True, "researchJobs": True}, stack=stack)
    assert len(deployment.one(BUCKET).inputs["bucket"]) == 63


def test_a_bad_stack_name_is_fine_without_the_jobs():
    assert deploy({"research": True}, stack="Dev").of(BUCKET) == []


def test_a_live_stack_keeps_its_trail_on_destroy(jobs):
    live = deploy(
        {
            "research": True,
            "researchJobs": True,
            "tradingMode": "live",
            "accountLast4": "5678",
            "alertEmail": "ops@example.test",
        },
        stack="prod",
    )
    assert live.one(BUCKET).inputs["forceDestroy"] is False
    assert jobs.one(BUCKET).inputs["forceDestroy"] is True


# --- the task ---------------------------------------------------------------------------


def test_the_task_is_the_bots_image_running_the_premarket_command(jobs):
    task = jobs.one(TASK, "research").inputs
    assert task["family"] == "traider-dev-research"
    assert (task["cpu"], task["memory"]) == ("512", "1024")
    assert task["requiresCompatibilities"] == ["FARGATE"]
    assert task["executionRoleArn"] == jobs.one(ROLE, "bot-execution").arn
    assert task["taskRoleArn"] == jobs.one(ROLE, "research-task").arn
    definition = container(jobs)
    bot = json.loads(jobs.one(TASK, "bot").inputs["containerDefinitions"])[0]
    assert definition["image"] == bot["image"]
    assert definition["command"] == ["research", "run", "--kind", "premarket"]
    assert "secrets" not in definition  # not even the sign-in link
    logs = definition["logConfiguration"]["options"]
    assert logs["awslogs-group"] == "/traider/traider-dev/research"


def test_the_task_is_told_where_everything_is_and_nothing_secret(jobs):
    env = environment(jobs)
    assert env["TRAIDER_RESEARCH_TABLE"] == jobs.one(TABLE, "research").inputs["name"]
    assert env["TRAIDER_SETTINGS_TABLE"] == jobs.one(TABLE, "settings").inputs["name"]
    assert env["TRAIDER_RESEARCH_BUCKET"] == f"traider-dev-research-trail-{ACCOUNT}"
    assert env["TRAIDER_FINNHUB_SECRET_ID"] == jobs.one(SECRET, "finnhub").arn
    assert env["TRAIDER_SCHWAB_TOKEN_SECRET_ID"] == jobs.one(SECRET, "schwab-token").arn
    assert env["TRAIDER_ALERT_TOPIC_ARN"] == jobs.one(TOPIC).arn
    assert "TRAIDER_TRADING_MODE" not in env  # research never trades
    assert "TRAIDER_FINNHUB_API_KEY" not in env
    assert env["AWS_REGION"] == REGION  # Bedrock needs it; not left to Fargate
    config = Config.from_env(env)
    assert (config.trading_mode, config.finnhub_api_key) == ("paper", None)


def test_a_live_stacks_research_task_still_starts():
    live = deploy(
        {
            "research": True,
            "researchJobs": True,
            "tradingMode": "live",
            "accountLast4": "5678",
            "alertEmail": "ops@example.test",
        },
        stack="prod",
    )
    assert Config.from_env(environment(live)).research_table == "traider-prod-research"


def test_the_finnhub_secret_is_created_without_a_value(jobs):
    assert jobs.one(SECRET, "finnhub")
    assert jobs.of("aws:secretsmanager/secretVersion:SecretVersion") == []
    text = json.dumps(jobs.outputs) + jobs.one(TASK, "research").inputs["containerDefinitions"]
    assert "api_key" not in text
    assert "finnhubSecretArn" not in jobs.secret_outputs  # an ARN, not a secret


# --- least privilege -----------------------------------------------------------------------


def test_the_research_role_names_exactly_the_resources_it_uses(jobs):
    granted = {sid: s["Resource"] for sid, s in statements(jobs, "research-task").items()}
    research = jobs.one(TABLE, "research").arn
    assert granted == {
        "ReadSecrets": [
            jobs.one(SECRET, "schwab-app").arn,
            jobs.one(SECRET, "schwab-token").arn,
            jobs.one(SECRET, "finnhub").arn,
        ],
        "SaveRotatedRefreshToken": jobs.one(SECRET, "schwab-token").arn,
        "Research": research,
        "ResearchRunsByDay": f"{research}/index/gsi1",
        "ReadSettings": jobs.one(TABLE, "settings").arn,
        "Trail": f"{jobs.one(BUCKET).arn}/*",
        "Alerts": jobs.one(TOPIC).arn,
        "BedrockMantle": "*",
    }


def test_the_research_role_has_exactly_these_actions(jobs):
    found = {sid: s["Action"] for sid, s in statements(jobs, "research-task").items()}
    assert found == {
        "ReadSecrets": "secretsmanager:GetSecretValue",
        "SaveRotatedRefreshToken": "secretsmanager:PutSecretValue",
        "Research": [
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:DeleteItem",
            "dynamodb:Query",
        ],
        "ResearchRunsByDay": "dynamodb:Query",
        "ReadSettings": ["dynamodb:Query", "dynamodb:GetItem"],
        "Trail": "s3:PutObject",
        "Alerts": "sns:Publish",
        "BedrockMantle": [
            "bedrock-mantle:CreateInference",
            "bedrock-mantle:GetProject",
            "bedrock-mantle:ListProjects",
        ],
    }


def _listed(value) -> list[str]:
    return value if isinstance(value, list) else [value]


def test_wildcards_appear_only_where_they_must(jobs):
    """A resource with a "*" is allowed only as one of these, and no action has one."""
    allowed = {
        # every revision of the research task family, for the scheduler
        f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/traider-dev-research:*",
        # the objects in the trail bucket
        f"{jobs.one(BUCKET).arn}/*",
        # a log group's streams
        *(f"{group.arn}:*" for group in jobs.of(LOG_GROUP)),
    }
    for policy in jobs.of(ROLE_POLICY):
        for statement in json.loads(policy.inputs["policy"])["Statement"]:
            actions = _listed(statement["Action"])
            for action in actions:
                assert "*" not in action, (policy.name, statement["Sid"], action)
            for resource in _listed(statement["Resource"]):
                if "*" not in resource:
                    continue
                if resource == "*":
                    assert all(a.startswith("bedrock-mantle:") for a in actions), (
                        policy.name,
                        statement["Sid"],
                    )
                    continue
                assert resource in allowed, (policy.name, statement["Sid"], resource)


def test_the_bot_still_only_reads_research(jobs):
    (statement,) = [s for s in jobs.policy("bot-task") if s["Sid"] == "Research"]
    assert set(statement["Action"]) == {"dynamodb:Query", "dynamodb:GetItem"}


def test_roles_are_assumed_only_by_their_service(jobs):
    def principal(name):
        trust = json.loads(jobs.one(ROLE, name).inputs["assumeRolePolicy"])
        return trust["Statement"][0]["Principal"]["Service"]

    assert principal("research-task") == "ecs-tasks.amazonaws.com"
    assert principal("research-scheduler") == "scheduler.amazonaws.com"


def test_only_this_accounts_scheduler_may_assume_its_role(jobs):
    trust = json.loads(jobs.one(ROLE, "research-scheduler").inputs["assumeRolePolicy"])
    (statement,) = trust["Statement"]
    assert statement["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}}


def test_the_scheduler_may_start_only_this_task_and_pass_only_its_roles(jobs):
    found = statements(jobs, "research-scheduler")
    run = found["StartResearchTask"]
    assert run["Action"] == "ecs:RunTask"
    assert run["Resource"] == (
        f"arn:aws:ecs:{REGION}:{ACCOUNT}:task-definition/traider-dev-research:*"
    )
    assert run["Condition"] == {"ArnEquals": {"ecs:cluster": jobs.one(CLUSTER, "research").arn}}
    passing = found["PassResearchRoles"]
    assert passing["Action"] == "iam:PassRole"
    assert passing["Resource"] == [
        jobs.one(ROLE, "research-task").arn,
        jobs.one(ROLE, "bot-execution").arn,
    ]
    assert passing["Condition"] == {
        "StringEquals": {"iam:PassedToService": "ecs-tasks.amazonaws.com"}
    }
    dead = found["DeadLetters"]
    assert (dead["Action"], dead["Resource"]) == (
        "sqs:SendMessage",
        jobs.one(QUEUE, "research-schedule-dlq").arn,
    )
    assert set(found) == {"StartResearchTask", "PassResearchRoles", "DeadLetters"}


# --- the schedule ---------------------------------------------------------------------------


def test_it_runs_weekdays_at_8_new_york_time_on_time_and_retries_a_failed_start_briefly(jobs):
    schedule = jobs.one(SCHEDULE).inputs
    assert schedule["scheduleExpression"] == "cron(0 8 ? * MON-FRI *)"
    assert schedule["scheduleExpressionTimezone"] == "America/New_York"
    assert schedule["flexibleTimeWindow"] == {"mode": "OFF"}
    target = schedule["target"]
    assert target["retryPolicy"] == {"maximumRetryAttempts": 2, "maximumEventAgeInSeconds": 600}
    assert target["arn"] == jobs.one(CLUSTER, "research").arn
    assert target["roleArn"] == jobs.one(ROLE, "research-scheduler").arn
    ecs = target["ecsParameters"]
    assert ecs["taskDefinitionArn"] == jobs.one(TASK, "research").arn
    assert (ecs["launchType"], ecs["taskCount"]) == ("FARGATE", 1)


def test_it_runs_on_the_bots_network(jobs):
    service = jobs.one("aws:ecs/service:Service").inputs["networkConfiguration"]
    network = jobs.one(SCHEDULE).inputs["target"]["ecsParameters"]["networkConfiguration"]
    assert network["subnets"] == service["subnets"]
    assert network["securityGroups"] == service["securityGroups"]
    assert network["assignPublicIp"] is True


def test_it_has_its_own_cluster_so_the_bots_crash_alarm_stays_quiet(jobs):
    research = jobs.one(CLUSTER, "research").inputs["name"]
    assert research == "traider-dev-research"
    bot_rule = json.loads(jobs.one(RULE, "bot-stopped").inputs["eventPattern"])
    assert bot_rule["detail"]["clusterArn"] == [jobs.one(CLUSTER, "bot").arn]


# --- failures are loud --------------------------------------------------------------------


def test_a_research_task_that_fails_raises_an_alert(jobs):
    rule = jobs.one(RULE, "research-failed")
    detail = json.loads(rule.inputs["eventPattern"])["detail"]
    assert detail["clusterArn"] == [jobs.one(CLUSTER, "research").arn]
    assert detail["group"] == ["family:traider-dev-research"]
    assert detail["lastStatus"] == ["STOPPED"]
    assert detail["$or"] == [
        {"stopCode": ["TaskFailedToStart"]},
        {"containers": {"exitCode": [{"anything-but": 0}]}},
    ]
    target = jobs.one("aws:cloudwatch/eventTarget:EventTarget", "research-failed").inputs
    assert target["arn"] == jobs.one(TOPIC).arn
    assert "[traider] The research run" in target["inputTransformer"]["inputTemplate"]


def test_a_start_the_scheduler_gives_up_on_lands_in_a_dead_letter_queue(jobs):
    queue = jobs.one(QUEUE, "research-schedule-dlq")
    assert queue.inputs["name"] == "traider-dev-research-schedule-dlq"
    assert queue.inputs["sqsManagedSseEnabled"] is True
    assert queue.inputs["messageRetentionSeconds"] == 14 * 24 * 3600
    target = jobs.one(SCHEDULE).inputs["target"]
    assert target["deadLetterConfig"] == {"arn": queue.arn}


def test_a_dead_letter_raises_an_alert(jobs):
    alarm = jobs.one(ALARM, "research-schedule-dlq").inputs
    assert (alarm["namespace"], alarm["metricName"]) == (
        "AWS/SQS",
        "ApproximateNumberOfMessagesVisible",
    )
    assert alarm["dimensions"] == {"QueueName": "traider-dev-research-schedule-dlq"}
    assert (alarm["comparisonOperator"], alarm["threshold"]) == ("GreaterThanThreshold", 0)
    assert (alarm["period"], alarm["evaluationPeriods"]) == (300, 1)
    assert alarm["treatMissingData"] == "notBreaching"
    assert alarm["alarmActions"] == [jobs.one(TOPIC).arn]


def test_each_alarm_may_publish_to_the_topic_and_nothing_else_may(jobs):
    policy = json.loads(jobs.one(TOPIC_POLICY).inputs["policy"])
    found = {
        s["Sid"]: (s["Principal"]["Service"], s["Condition"]["ArnEquals"]["aws:SourceArn"])
        for s in policy["Statement"]
    }
    assert found == {
        "TaskStoppedAlarm": ("events.amazonaws.com", jobs.one(RULE, "bot-stopped").arn),
        "ResearchFailedAlarm": ("events.amazonaws.com", jobs.one(RULE, "research-failed").arn),
        "ResearchNotStartedAlarm": (
            "cloudwatch.amazonaws.com",
            jobs.one(ALARM, "research-schedule-dlq").arn,
        ),
    }
    for statement in policy["Statement"]:
        assert (statement["Action"], statement["Resource"]) == ("sns:Publish", jobs.one(TOPIC).arn)


# --- outputs and local use ------------------------------------------------------------------


def local_env(deployment) -> dict[str, str]:
    import shlex

    pairs = [shlex.split(line) for line in deployment.outputs["localEnv"].splitlines()]
    return dict(pair[0].split("=", 1) for pair in pairs)


def test_outputs_say_where_the_research_run_lives(jobs):
    out = jobs.outputs
    assert out["researchBucket"] == f"traider-dev-research-trail-{ACCOUNT}"
    assert out["finnhubSecretArn"] == jobs.one(SECRET, "finnhub").arn
    assert out["researchCluster"] == "traider-dev-research"
    assert out["researchLogGroup"] == "/traider/traider-dev/research"


def test_local_env_lets_you_dry_run_research_and_still_cannot_trade(jobs):
    lines = local_env(jobs)
    assert lines["TRAIDER_FINNHUB_SECRET_ID"] == jobs.one(SECRET, "finnhub").arn
    assert "TRAIDER_RESEARCH_BUCKET" not in lines  # a local run keeps its trail locally
    assert "TRAIDER_TRADING_MODE" not in lines
    assert "TRAIDER_CONTROL_PARAM" not in lines
